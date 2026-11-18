"""Store additions for the outbox drain (#3706, RFC 0019 O2).

``release`` (claimed -> pending), ``claim(exclude_ids=)``, ``operator_authored``,
schema v2, ``peek_pending`` and the bounded ``scan_entries(newest=)``. Hermetic:
the autouse ``tmp_config_dir`` redirects ``state_dir()``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cw._lock_guard import held_locks
from cw.config import state_dir
from cw.outbox import (
    MAX_DETAIL_CHARS,
    OUTBOX_SCHEMA_VERSION,
    RELEASE_REASONS,
    OutboxClaimError,
    OutboxEntry,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxSchemaError,
    OutboxState,
    OutboxTransitionError,
    OutboxValidationError,
    _layout,
    claim,
    list_entries,
    peek_pending,
    release,
    settle,
)
from tests._reconcile_helpers import dead_launch_marker
from tests._shared.outbox_builders import CLIENT, claim_as, enqueue

_SCHEMA_V2 = 2
_BAD_TOKEN = "0" * 32
_MISSING_ID = "f" * 32
_THREE = 3


def _record(entry_id: str) -> Path:
    return state_dir() / "outbox" / CLIENT / f"{entry_id}.json"


def _claimed(marker: str = "m1") -> tuple[str, str]:
    enqueue(marker=marker)
    (entry,) = claim(CLIENT)
    assert entry.claim_token is not None
    return entry.id, entry.claim_token


class TestRelease:
    def test_release_returns_a_pending_entry_with_reason(self) -> None:
        entry_id, token = _claimed()
        before = list_entries(CLIENT)[0]

        released = release(
            CLIENT,
            entry_id,
            claim_token=token,
            reason=OutboxReason.TRANSIENT_FAILURE,
            detail="gh refused",
        )

        assert released.state is OutboxState.PENDING
        assert released.reason is OutboxReason.TRANSIENT_FAILURE
        assert released.detail == "gh refused"
        assert released.claim_token is None
        assert released.claimant is None
        assert released.claimed_at is None
        assert released.claimed_from is None
        last = released.transitions[-1]
        assert last.initiator == "release"
        assert last.from_state is OutboxState.CLAIMED
        assert last.to_state is OutboxState.PENDING
        assert released.updated_at >= before.updated_at
        assert list_entries(CLIENT)[0] == released

    @pytest.mark.parametrize("reason", sorted(RELEASE_REASONS))
    def test_each_release_reason_accepted(self, reason: OutboxReason) -> None:
        entry_id, token = _claimed()

        released = release(CLIENT, entry_id, claim_token=token, reason=reason)

        assert released.reason is reason

    def test_release_reasons_are_exactly_the_two(self) -> None:
        assert (
            frozenset(
                {OutboxReason.BINDING_UNREACHABLE, OutboxReason.TRANSIENT_FAILURE}
            )
            == RELEASE_REASONS
        )

    @pytest.mark.parametrize(
        "reason",
        [
            OutboxReason.CLAIM_RECOVERED,
            OutboxReason.POSTED,
            OutboxReason.READBACK_UNVERIFIED,
            OutboxReason.READBACK_MISMATCH,
        ],
    )
    def test_other_reasons_refused(self, reason: OutboxReason) -> None:
        entry_id, token = _claimed()

        with pytest.raises(OutboxTransitionError):
            release(CLIENT, entry_id, claim_token=token, reason=reason)

        assert list_entries(CLIENT)[0].state is OutboxState.CLAIMED

    def test_release_of_an_entry_claimed_from_unverified_is_refused(self) -> None:
        entry_id, token = _claimed()
        settle(
            CLIENT,
            entry_id,
            claim_token=token,
            state=OutboxState.UNVERIFIED,
            reason=OutboxReason.READBACK_UNVERIFIED,
        )
        (reclaimed,) = claim(CLIENT, state=OutboxState.UNVERIFIED)
        before = _record(entry_id).read_bytes()

        with pytest.raises(OutboxTransitionError):
            release(
                CLIENT,
                entry_id,
                claim_token=reclaimed.claim_token or "",
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

        assert _record(entry_id).read_bytes() == before

    def test_wrong_token_raises(self) -> None:
        entry_id, _ = _claimed()
        with pytest.raises(OutboxClaimError):
            release(
                CLIENT,
                entry_id,
                claim_token=_BAD_TOKEN,
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

    def test_unclaimed_entry_raises(self) -> None:
        entry = enqueue().entry
        with pytest.raises(OutboxClaimError):
            release(
                CLIENT,
                entry.id,
                claim_token=_BAD_TOKEN,
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

    def test_unknown_entry_raises(self) -> None:
        enqueue()
        with pytest.raises(OutboxClaimError):
            release(
                CLIENT,
                _MISSING_ID,
                claim_token=_BAD_TOKEN,
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

    def test_bad_entry_id_is_a_validation_error(self) -> None:
        with pytest.raises(OutboxValidationError):
            release(
                CLIENT,
                "../x",
                claim_token=_BAD_TOKEN,
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

    def test_stale_release_after_recovery_raises_and_does_not_overwrite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enqueue()
        (entry,) = claim_as(monkeypatch, dead_launch_marker())
        # The next claim recovers the dead-owner claim before claiming again.
        (again,) = claim(CLIENT)
        assert again.claim_token != entry.claim_token
        before = _record(entry.id).read_bytes()

        with pytest.raises(OutboxClaimError):
            release(
                CLIENT,
                entry.id,
                claim_token=entry.claim_token or "",
                reason=OutboxReason.TRANSIENT_FAILURE,
            )

        assert _record(entry.id).read_bytes() == before

    def test_released_entry_is_claimable_and_keeps_its_reason(self) -> None:
        entry_id, token = _claimed()
        release(
            CLIENT,
            entry_id,
            claim_token=token,
            reason=OutboxReason.BINDING_UNREACHABLE,
        )

        (reclaimed,) = claim(CLIENT)

        assert reclaimed.state is OutboxState.CLAIMED
        assert reclaimed.reason is OutboxReason.BINDING_UNREACHABLE
        assert _layout.read_record(CLIENT, entry_id) == reclaimed

    def test_detail_over_cap_refused(self) -> None:
        entry_id, token = _claimed()
        with pytest.raises(OutboxValidationError):
            release(
                CLIENT,
                entry_id,
                claim_token=token,
                reason=OutboxReason.TRANSIENT_FAILURE,
                detail="x" * (MAX_DETAIL_CHARS + 1),
            )
        assert list_entries(CLIENT)[0].state is OutboxState.CLAIMED

    def test_failed_write_leaves_the_claim_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry_id, token = _claimed()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OutboxRecordError(msg)

        monkeypatch.setattr("cw.outbox._store.layout.write_record", _boom)
        with pytest.raises(OutboxRecordError):
            release(
                CLIENT,
                entry_id,
                claim_token=token,
                reason=OutboxReason.TRANSIENT_FAILURE,
            )
        monkeypatch.undo()

        entry = list_entries(CLIENT)[0]
        assert entry.state is OutboxState.CLAIMED
        assert entry.claim_token == token

    def test_release_holds_only_the_outbox_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry_id, token = _claimed()
        seen: list[tuple[str, ...]] = []
        real_write = _layout.write_record

        def _spy(client: str, entry: OutboxEntry) -> None:
            seen.append(tuple(lock.name for lock in held_locks()))
            real_write(client, entry)

        monkeypatch.setattr("cw.outbox._store.layout.write_record", _spy)

        release(
            CLIENT, entry_id, claim_token=token, reason=OutboxReason.TRANSIENT_FAILURE
        )

        assert seen == [("outbox",)]


class TestClaimExclude:
    def test_exclude_ids_skips_the_listed_ids(self) -> None:
        first = enqueue(marker="a").entry
        second = enqueue(marker="b").entry

        (claimed,) = claim(CLIENT, exclude_ids=frozenset({first.id}))

        assert claimed.id == second.id

    def test_default_claims_the_oldest(self) -> None:
        first = enqueue(marker="a").entry
        enqueue(marker="b")

        (claimed,) = claim(CLIENT)

        assert claimed.id == first.id

    def test_excluding_everything_returns_empty(self) -> None:
        ids = frozenset(enqueue(marker=m).entry.id for m in ("a", "b"))

        assert claim(CLIENT, exclude_ids=ids) == []
        assert {e.state for e in list_entries(CLIENT)} == {OutboxState.PENDING}


class TestOperatorAuthored:
    def test_default_is_false(self) -> None:
        assert enqueue().entry.operator_authored is False

    def test_true_round_trips_through_the_record(self) -> None:
        entry = enqueue(operator_authored=True).entry

        assert _layout.read_record(CLIENT, entry.id).operator_authored is True

    def test_duplicate_enqueue_keeps_the_stored_value(self) -> None:
        enqueue(operator_authored=True)

        result = enqueue(operator_authored=False)

        assert result.created is False
        assert result.entry.operator_authored is True


class TestSchemaV2:
    def test_new_records_are_v2(self) -> None:
        entry = enqueue().entry
        data = json.loads(_record(entry.id).read_text(encoding="utf-8"))

        assert OUTBOX_SCHEMA_VERSION == _SCHEMA_V2
        assert data["schema_version"] == _SCHEMA_V2
        assert data["operator_authored"] is False

    def test_v1_record_without_operator_authored_loads(self) -> None:
        entry = enqueue().entry
        path = _record(entry.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = 1
        del data["operator_authored"]
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = _layout.read_record(CLIENT, entry.id)

        assert loaded.schema_version == 1
        assert loaded.operator_authored is False

    def test_v3_record_refused_and_not_rewritten(self) -> None:
        entry = enqueue().entry
        path = _record(entry.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = OUTBOX_SCHEMA_VERSION + 1
        raw = json.dumps(data)
        path.write_text(raw, encoding="utf-8")

        with pytest.raises(OutboxSchemaError):
            _layout.read_record(CLIENT, entry.id)
        assert claim(CLIENT) == []
        assert path.read_text(encoding="utf-8") == raw


def _set_mtime(entry_id: str, stamp: int) -> None:
    os.utime(_record(entry_id), (stamp, stamp))


class TestPeekPending:
    def test_only_pending_roles_and_ids(self) -> None:
        pending = enqueue(marker="p").entry
        knowledge = enqueue(marker="k", role=OutboxRole.KNOWLEDGE).entry
        entry_id, token = _claimed(marker="d")
        settle(
            CLIENT,
            entry_id,
            claim_token=token,
            state=OutboxState.DELIVERED,
            reason=OutboxReason.POSTED,
        )

        peek = peek_pending(CLIENT, scan_limit=10)

        assert peek.entry_ids == frozenset({pending.id, knowledge.id})
        assert peek.roles == frozenset({OutboxRole.TICKETS, OutboxRole.KNOWLEDGE})
        assert peek.truncated is False

    def test_unverified_and_claimed_do_not_count(self) -> None:
        entry_id, token = _claimed(marker="u")
        settle(
            CLIENT,
            entry_id,
            claim_token=token,
            state=OutboxState.UNVERIFIED,
            reason=OutboxReason.READBACK_UNVERIFIED,
        )
        _claimed(marker="c")

        peek = peek_pending(CLIENT, scan_limit=10)

        assert peek.entry_ids == frozenset()
        assert peek.roles == frozenset()

    def test_exclude_ids_removes_the_held_entries_and_their_roles(self) -> None:
        held = enqueue(marker="k", role=OutboxRole.KNOWLEDGE).entry
        kept = enqueue(marker="t").entry

        peek = peek_pending(CLIENT, scan_limit=10, exclude_ids=frozenset({held.id}))

        assert peek.entry_ids == frozenset({kept.id})
        assert peek.roles == frozenset({OutboxRole.TICKETS})

    def test_truncated_when_more_records_than_the_limit(self) -> None:
        for marker in ("a", "b", "c"):
            enqueue(marker=marker)

        peek = peek_pending(CLIENT, scan_limit=2)

        assert peek.truncated is True
        assert len(peek.entry_ids) == _THREE - 1

    def test_parses_the_newest_mtime_records_only(self) -> None:
        old = enqueue(marker="old").entry
        mid = enqueue(marker="mid").entry
        new = enqueue(marker="new").entry
        _set_mtime(old.id, 1_000)
        _set_mtime(mid.id, 2_000)
        _set_mtime(new.id, 3_000)

        peek = peek_pending(CLIENT, scan_limit=2)

        assert peek.entry_ids == frozenset({mid.id, new.id})

    def test_unknown_client_creates_nothing(self) -> None:
        peek = peek_pending("nobody", scan_limit=10)

        assert peek.entry_ids == frozenset()
        assert peek.truncated is False
        assert not (state_dir() / "outbox" / "nobody").exists()

    def test_takes_no_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        enqueue()

        def _no_lock(_client: str) -> None:
            msg = "peek_pending must not lock"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.outbox._store._outbox_lock", _no_lock)

        assert len(peek_pending(CLIENT, scan_limit=10).entry_ids) == 1


class TestScanNewest:
    def test_newest_orders_by_mtime(self) -> None:
        first = enqueue(marker="a").entry
        second = enqueue(marker="b").entry
        _set_mtime(first.id, 5_000)
        _set_mtime(second.id, 1_000)

        newest = _layout.scan_entries(CLIENT, newest=1)

        assert [e.id for e in newest] == [first.id]

    def test_sidecars_are_never_parsed(self) -> None:
        entry = enqueue().entry
        directory = state_dir() / "outbox" / CLIENT
        for name in (
            "drain.lock",
            "drain.log",
            "drain.held",
            "drain.unreachable.tickets",
        ):
            (directory / name).write_text("{not json", encoding="utf-8")

        assert [e.id for e in _layout.scan_entries(CLIENT)] == [entry.id]
        assert [e.id for e in _layout.scan_entries(CLIENT, newest=10)] == [entry.id]

    def test_default_is_unbounded(self) -> None:
        for marker in ("a", "b", "c"):
            enqueue(marker=marker)

        assert len(_layout.scan_entries(CLIENT)) == _THREE
