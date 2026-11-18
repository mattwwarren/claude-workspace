"""Tests for the per-client outbox store (#3705, RFC 0019 O1).

The store is a pure file-backed queue: no subprocess, no network, no tracker
call. Every test is hermetic through the autouse ``tmp_config_dir`` (which
redirects ``state_dir()``), ``_assume_initial_pid_namespace`` (so
``current_launch_marker()`` is non-None inside a container) and
``_lock_invariants`` fixtures; nothing is set up per file.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

import cw.outbox as outbox_pkg
from cw._lock_guard import (
    CwLockOrderError,
    HeldLock,
    LockRank,
    held_locks,
)
from cw.config import sessions_lock, state_dir
from cw.dev_queue.storage import dev_queue_lock
from cw.history import _history_lock
from cw.launch_marker import (
    LaunchOwnerState,
    ProbeState,
    ProcessProbe,
    current_launch_marker,
)
from cw.models import LaunchMarker
from cw.outbox import (
    ALLOWED_TRANSITIONS,
    DEFAULT_CLAIM_LIMIT,
    MAX_BODY_BYTES,
    MAX_DETAIL_CHARS,
    MAX_MARKER_CHARS,
    MAX_PART_CHARS,
    MAX_PARTS,
    OUTBOX_SCHEMA_VERSION,
    REASONS_BY_STATE,
    EnqueueResult,
    OutboxClaimError,
    OutboxEntry,
    OutboxError,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxSchemaError,
    OutboxState,
    OutboxTransitionError,
    OutboxValidationError,
    _layout,
    claim,
    claimant_state,
    enqueue,
    list_clients,
    list_entries,
    make_entry_id,
    read_parts,
    settle,
    transition_allowed,
)
from cw.outbox._lock import _outbox_lock
from tests._reconcile_helpers import dead_launch_marker, live_launch_marker
from tests._shared.lock_helpers import _hold_flock

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_CLIENT = "acme"
_BODY = "hello world"
_JOIN_TIMEOUT_S = 10.0
_BLOCKED_JOIN_S = 0.3
_THREADS = 6
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_MODE_MASK = 0o777
_MARKER_PID = 4242
_MARKER_START_NS = 1_000_000
_ANCIENT = "2020-01-01T00:00:00Z"
_RECENT = "2026-11-18T12:00:00Z"
_UNKNOWN_SCHEMA = OUTBOX_SCHEMA_VERSION + 1
_BAD_TOKEN = "0" * 32


def _enqueue(
    client: str = _CLIENT,
    *,
    role: OutboxRole = OutboxRole.TICKETS,
    ticket: str = "T-1",
    marker: str = "m1",
    producer: str = "codex-review",
    parts: Sequence[str] = (_BODY,),
    redacted: bool = False,
) -> EnqueueResult:
    return enqueue(
        client,
        role=role,
        ticket=ticket,
        marker=marker,
        producer=producer,
        parts=parts,
        redacted=redacted,
    )


def _client_dir(client: str = _CLIENT) -> Path:
    return state_dir() / "outbox" / client


def _record_file(entry_id: str, client: str = _CLIENT) -> Path:
    return _client_dir(client) / f"{entry_id}.json"


def _body_file(entry_id: str, client: str = _CLIENT) -> Path:
    return _client_dir(client) / f"{entry_id}.body"


def _claim_as(
    monkeypatch: pytest.MonkeyPatch,
    marker: LaunchMarker | None,
    client: str = _CLIENT,
    **kwargs: Any,
) -> list[OutboxEntry]:
    """Claim as *marker*: ``current_launch_marker`` is patched where used."""
    with monkeypatch.context() as patch:
        patch.setattr("cw.outbox._store.current_launch_marker", lambda: marker)
        return claim(client, **kwargs)


def _claim_one(entry_id_marker: str = "m1") -> OutboxEntry:
    _enqueue(marker=entry_id_marker)
    claimed = claim(_CLIENT)
    assert len(claimed) == 1
    return claimed[0]


def _states(client: str = _CLIENT) -> dict[str, OutboxState]:
    return {e.id: e.state for e in list_entries(client)}


class TestValidation:
    @pytest.mark.parametrize(
        "client", ["", "../etc", "a/b", ".hidden", "-lead", "sp ace", "a\x00b"]
    )
    def test_bad_client_rejected(self, client: str) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(client)

    @pytest.mark.parametrize("ticket", ["", "a/b", "a b", "..", "T.1"])
    def test_bad_ticket_rejected(self, ticket: str) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(ticket=ticket)

    @pytest.mark.parametrize(
        "marker",
        ["", "-lead", "has space", "bad/slash", "x" * (MAX_MARKER_CHARS + 1)],
    )
    def test_bad_marker_rejected(self, marker: str) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(marker=marker)

    def test_marker_at_cap_accepted(self) -> None:
        assert _enqueue(marker="x" * MAX_MARKER_CHARS).created

    @pytest.mark.parametrize("producer", ["", "Upper", "1lead", "has space", "p" * 65])
    def test_bad_producer_rejected(self, producer: str) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(producer=producer)

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(OutboxValidationError):
            enqueue(
                _CLIENT,
                role=cast("OutboxRole", "nonsense"),
                ticket="T-1",
                marker="m1",
                producer="p",
                parts=("x",),
                redacted=False,
            )

    def test_errors_share_a_base(self) -> None:
        for cls in (
            OutboxValidationError,
            OutboxSchemaError,
            OutboxRecordError,
            OutboxClaimError,
            OutboxTransitionError,
        ):
            assert issubclass(cls, OutboxError)


class TestCaps:
    def test_part_over_max_part_chars(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=("x" * (MAX_PART_CHARS + 1),))

    def test_part_at_max_part_chars_accepted(self) -> None:
        assert _enqueue(parts=("x" * MAX_PART_CHARS,)).created

    def test_too_many_parts(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=["x"] * (MAX_PARTS + 1))

    def test_total_over_max_body_bytes(self) -> None:
        parts = ["x" * MAX_PART_CHARS] * 18
        assert sum(len(p) for p in parts) > MAX_BODY_BYTES
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=parts)

    def test_empty_parts_rejected(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=())

    def test_empty_string_part_rejected(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=("ok", ""))

    def test_unencodable_surrogate_rejected(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=("bad \ud800 surrogate",))

    def test_rejection_writes_nothing(self) -> None:
        with pytest.raises(OutboxValidationError):
            _enqueue(parts=())
        assert not (state_dir() / "outbox").exists()


class TestEnqueue:
    def test_creates_body_and_record(self) -> None:
        result = _enqueue()

        assert result.created is True
        assert result.body_matches is True
        entry = result.entry
        assert entry.state is OutboxState.PENDING
        assert entry.reason is None
        assert entry.client == _CLIENT
        assert entry.schema_version == OUTBOX_SCHEMA_VERSION
        assert entry.claim_token is None
        assert entry.claimant is None
        assert _body_file(entry.id).read_bytes() == _BODY.encode()
        assert _record_file(entry.id).is_file()
        assert entry.body_sha256 == hashlib.sha256(_BODY.encode()).hexdigest()
        assert entry.body_bytes == len(_BODY.encode())

    def test_body_stored_verbatim_marker_not_appended(self) -> None:
        entry = _enqueue(marker="the-marker", parts=("plain",)).entry

        assert _body_file(entry.id).read_text(encoding="utf-8") == "plain"
        assert "the-marker" not in _body_file(entry.id).read_text(encoding="utf-8")
        assert entry.marker == "the-marker"

    def test_spans_contiguous_and_cover_body(self) -> None:
        parts = ["héllo", "wörld ✓", "tail"]
        entry = _enqueue(parts=parts).entry

        sizes = [len(p.encode("utf-8")) for p in parts]
        expected = []
        start = 0
        for size in sizes:
            expected.append((start, start + size))
            start += size
        assert entry.parts == expected
        assert entry.body_bytes == start
        assert _body_file(entry.id).stat().st_size == start

    def test_read_parts_roundtrip(self) -> None:
        parts = ["héllo", "wörld ✓", "line\nbreak"]
        entry = _enqueue(parts=parts, redacted=True).entry

        assert entry.redacted is True
        assert read_parts(entry) == parts

    def test_read_parts_missing_body_raises_record_error(self) -> None:
        entry = _enqueue().entry
        _body_file(entry.id).unlink()

        with pytest.raises(OutboxRecordError):
            read_parts(entry)

    def test_read_parts_mismatched_body_raises_record_error(self) -> None:
        entry = _enqueue().entry
        _body_file(entry.id).write_text("HELLO WORLD", encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            read_parts(entry)

    def test_read_parts_span_inside_a_character_raises_record_error(self) -> None:
        entry = _enqueue(parts=("\u00e9",)).entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))
        torn = OutboxEntry.model_validate({**data, "parts": [[0, 1], [1, 2]]})

        with pytest.raises(OutboxRecordError):
            read_parts(torn)

    def test_duplicate_same_body_returns_existing_created_false_body_matches_true(
        self,
    ) -> None:
        first = _enqueue()
        second = _enqueue()

        assert second.created is False
        assert second.body_matches is True
        assert second.entry == first.entry
        assert len(list_entries(_CLIENT)) == 1

    def test_duplicate_different_body_does_not_overwrite_body_matches_false(
        self,
    ) -> None:
        first = _enqueue()
        second = _enqueue(parts=("a different body",))

        assert second.created is False
        assert second.body_matches is False
        assert second.entry == first.entry
        assert _body_file(first.entry.id).read_text(encoding="utf-8") == _BODY

    def test_id_is_hash_of_key_and_client_scoped_by_directory(self) -> None:
        a = _enqueue("acme").entry
        b = _enqueue("other").entry

        expected = hashlib.sha256(b"tickets\x00T-1\x00m1").hexdigest()[:32]
        assert a.id == b.id == expected
        assert make_entry_id(OutboxRole.TICKETS, "T-1", "m1") == expected
        assert _record_file(a.id, "acme").is_file()
        assert _record_file(b.id, "other").is_file()
        assert len(list_entries("acme")) == len(list_entries("other")) == 1

    def test_role_ticket_marker_each_change_the_id(self) -> None:
        base = _enqueue().entry.id
        other_role = _enqueue(role=OutboxRole.CHAT).entry.id
        other_ticket = _enqueue(ticket="T-2").entry.id
        other_marker = _enqueue(marker="m2").entry.id

        assert len({base, other_role, other_ticket, other_marker}) == 4

    def test_write_order_body_before_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        real_body = _layout.write_body
        real_record = _layout.write_record

        def spy_body(*args: Any, **kwargs: Any) -> None:
            order.append("body")
            real_body(*args, **kwargs)

        def spy_record(*args: Any, **kwargs: Any) -> None:
            order.append("record")
            real_record(*args, **kwargs)

        monkeypatch.setattr("cw.outbox._layout.write_body", spy_body)
        monkeypatch.setattr("cw.outbox._layout.write_record", spy_record)

        _enqueue()

        assert order == ["body", "record"]

    def test_orphan_body_skipped_by_list_and_claim(self) -> None:
        entry = _enqueue().entry
        _record_file(entry.id).unlink()
        assert _body_file(entry.id).exists()

        assert list_entries(_CLIENT) == []
        assert claim(_CLIENT) == []
        # An orphan is harmless: the next enqueue rewrites it and commits.
        again = _enqueue()
        assert again.created is True
        assert len(list_entries(_CLIENT)) == 1

    def test_torn_tmp_file_ignored(self) -> None:
        entry = _enqueue().entry
        torn = _client_dir() / f"{entry.id}.json.abc123.tmp"
        torn.write_text("{not json", encoding="utf-8")

        assert [e.id for e in list_entries(_CLIENT)] == [entry.id]
        assert [e.id for e in claim(_CLIENT)] == [entry.id]

    def test_oserror_on_write_raises_outbox_record_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = _layout.atomic_write_text

        def fail_on_record(path: Path, text: str) -> None:
            if path.suffix == ".json":
                raise OSError(errno.ENOSPC, "disk full")
            real(path, text)

        monkeypatch.setattr("cw.outbox._layout.atomic_write_text", fail_on_record)

        with pytest.raises(OutboxRecordError):
            _enqueue()

        entry_id = make_entry_id(OutboxRole.TICKETS, "T-1", "m1")
        assert _body_file(entry_id).exists()
        assert not _record_file(entry_id).exists()
        assert list_entries(_CLIENT) == []

    def test_corrupt_existing_record_raises_on_duplicate(self) -> None:
        entry = _enqueue().entry
        _record_file(entry.id).write_text("{broken", encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            _enqueue()


class TestPersistence:
    def test_files_are_0600_dirs_0700(self) -> None:
        entry = _enqueue().entry

        for path in (
            _body_file(entry.id),
            _record_file(entry.id),
            _client_dir() / ".outbox.lock",
        ):
            assert path.stat().st_mode & _MODE_MASK == _FILE_MODE, path
        for directory in (state_dir() / "outbox", _client_dir()):
            assert directory.stat().st_mode & _MODE_MASK == _DIR_MODE, directory

    def test_record_extra_field_rejected(self) -> None:
        entry = _enqueue().entry
        path = _record_file(entry.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["surprise"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            _enqueue()

    def test_too_new_schema_version_raises_and_is_not_rewritten(self) -> None:
        entry = _enqueue().entry
        path = _record_file(entry.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = _UNKNOWN_SCHEMA
        data["future_field"] = "kept"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(OutboxSchemaError):
            _enqueue()
        with pytest.raises(OutboxSchemaError):
            settle(
                _CLIENT,
                entry.id,
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

        assert path.read_bytes() == before

    def test_corrupt_record_raises_on_targeted_read(self) -> None:
        entry = _enqueue().entry
        _record_file(entry.id).write_text("not json at all", encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            settle(
                _CLIENT,
                entry.id,
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

    def test_non_object_record_is_corrupt(self) -> None:
        entry = _enqueue().entry
        _record_file(entry.id).write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            _enqueue()

    def test_record_id_must_match_file_name(self) -> None:
        entry = _enqueue().entry
        other = _enqueue(marker="m2").entry
        _record_file(entry.id).write_text(
            _record_file(other.id).read_text(encoding="utf-8"), encoding="utf-8"
        )

        with pytest.raises(OutboxRecordError):
            _enqueue()

    def test_scan_skips_corrupt_and_too_new_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        good = _enqueue(marker="good").entry
        corrupt = _enqueue(marker="corrupt").entry
        too_new = _enqueue(marker="toonew").entry
        _record_file(corrupt.id).write_text("{broken", encoding="utf-8")
        path = _record_file(too_new.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = _UNKNOWN_SCHEMA
        path.write_text(json.dumps(data), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="cw.outbox"):
            listed = list_entries(_CLIENT)
            claimed = claim(_CLIENT, limit=10)

        assert [e.id for e in listed] == [good.id]
        assert [e.id for e in claimed] == [good.id]
        skips = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(skips) >= 2
        assert any(corrupt.id in r.getMessage() for r in skips)
        assert any(too_new.id in r.getMessage() for r in skips)

    def test_spans_not_covering_body_rejected(self) -> None:
        entry = _enqueue(parts=("abc", "def")).entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        for bad in ([[0, 3], [4, 6]], [[0, 3], [3, 5]], [[1, 3], [3, 6]], []):
            with pytest.raises(ValidationError):
                OutboxEntry.model_validate({**data, "parts": bad})

    def test_too_many_spans_rejected(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))
        spans = [[i, i + 1] for i in range(MAX_PARTS + 1)]

        with pytest.raises(ValidationError):
            OutboxEntry.model_validate(
                {**data, "parts": spans, "body_bytes": MAX_PARTS + 1}
            )

    def test_claim_fields_set_iff_claimed(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        with pytest.raises(ValidationError):
            OutboxEntry.model_validate({**data, "claim_token": "tok"})
        with pytest.raises(ValidationError):
            OutboxEntry.model_validate({**data, "state": "claimed"})

    def test_claimant_requires_claimed_state(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        with pytest.raises(ValidationError):
            OutboxEntry.model_validate(
                {**data, "claimant": {"pid": 1, "start_time_ns": 1}}
            )

    def test_claimed_from_must_be_pending_or_unverified(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))
        claimed = {
            **data,
            "state": "claimed",
            "claim_token": "tok",
            "claimed_at": "2026-11-01T10:00:00Z",
        }

        assert OutboxEntry.model_validate({**claimed, "claimed_from": "pending"})
        with pytest.raises(ValidationError):
            OutboxEntry.model_validate({**claimed, "claimed_from": "failed"})

    def test_body_sha256_must_be_a_digest(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        for digest in ("abc", "g" * 64):
            with pytest.raises(ValidationError):
                OutboxEntry.model_validate({**data, "body_sha256": digest})

    def test_record_client_must_match_directory(self) -> None:
        entry = _enqueue("acme").entry
        _enqueue("other")
        _record_file(entry.id, "other").write_bytes(
            _record_file(entry.id, "acme").read_bytes()
        )

        with pytest.raises(OutboxRecordError):
            _layout.read_record("other", entry.id)

    def test_unwritable_store_root_raises_record_error(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "outbox").write_text("a file, not a directory", encoding="utf-8")

        with pytest.raises(OutboxRecordError):
            _layout.ensure_client_dir(_CLIENT)
        with pytest.raises(OutboxRecordError):
            _enqueue()

    def test_unopenable_lock_file_raises_record_error(self) -> None:
        _enqueue()
        lock_file = _client_dir() / ".outbox.lock"
        lock_file.unlink()
        lock_file.mkdir()

        with pytest.raises(OutboxRecordError):
            _enqueue(marker="m2")

    def test_reason_must_belong_to_state(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        with pytest.raises(ValidationError):
            OutboxEntry.model_validate({**data, "reason": "posted"})

    def test_detail_over_cap_rejected(self) -> None:
        entry = _enqueue().entry
        data = json.loads(_record_file(entry.id).read_text(encoding="utf-8"))

        with pytest.raises(ValidationError):
            OutboxEntry.model_validate({**data, "detail": "d" * (MAX_DETAIL_CHARS + 1)})


class TestListEntries:
    def test_newest_first_by_created_at_then_id(self) -> None:
        with freeze_time("2026-11-01T10:00:00Z"):
            oldest = _enqueue(marker="a").entry
        with freeze_time("2026-11-01T11:00:00Z"):
            tie_a = _enqueue(marker="b").entry
            tie_b = _enqueue(marker="c").entry
        newest = sorted([tie_a, tie_b], key=lambda e: e.id, reverse=True)

        listed = list_entries(_CLIENT)

        assert [e.id for e in listed] == [*(e.id for e in newest), oldest.id]

    def test_state_filter_applies_before_limit(self) -> None:
        with freeze_time("2026-11-01T10:00:00Z"):
            old_pending = _enqueue(marker="old").entry
        with freeze_time("2026-11-01T11:00:00Z"):
            newer = _enqueue(marker="new").entry
        got = claim(_CLIENT, limit=1)
        assert [e.id for e in got] == [old_pending.id]

        only_pending = list_entries(_CLIENT, state=OutboxState.PENDING, limit=1)

        assert [e.id for e in only_pending] == [newer.id]

    def test_limit_returns_at_most_limit(self) -> None:
        for i in range(4):
            _enqueue(marker=f"m{i}")

        assert len(list_entries(_CLIENT, limit=2)) == 2
        assert len(list_entries(_CLIENT, limit=10)) == 4
        assert len(list_entries(_CLIENT)) == 4

    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_below_one_rejected(self, limit: int) -> None:
        with pytest.raises(OutboxValidationError):
            list_entries(_CLIENT, limit=limit)

    def test_missing_client_dir_returns_empty_and_creates_nothing(self) -> None:
        assert list_entries("nobody") == []
        assert not (state_dir() / "outbox").exists()

    def test_bad_client_rejected(self) -> None:
        with pytest.raises(OutboxValidationError):
            list_entries("../escape")

    def test_list_clients_sorted_and_only_client_dirs(self) -> None:
        assert list_clients() == []
        _enqueue("zeta")
        _enqueue("alpha")
        (state_dir() / "outbox" / "stray-file").write_text("x", encoding="utf-8")
        (state_dir() / "outbox" / "-bad").mkdir()

        assert list_clients() == ["alpha", "zeta"]


_ALL_PAIRS = list(itertools.product(OutboxState, OutboxState))
_LEGAL = {
    (OutboxState.PENDING, OutboxState.CLAIMED),
    (OutboxState.UNVERIFIED, OutboxState.CLAIMED),
    (OutboxState.CLAIMED, OutboxState.DELIVERED),
    (OutboxState.CLAIMED, OutboxState.UNVERIFIED),
    (OutboxState.CLAIMED, OutboxState.FAILED),
}


class TestTransitionMatrix:
    @pytest.mark.parametrize(("frm", "to"), _ALL_PAIRS)
    def test_5x5(self, frm: OutboxState, to: OutboxState) -> None:
        assert transition_allowed(frm, to) is ((frm, to) in _LEGAL)

    @pytest.mark.parametrize(("frm", "to"), _ALL_PAIRS)
    def test_5x5_with_recovery_adds_only_claimed_to_pending(
        self, frm: OutboxState, to: OutboxState
    ) -> None:
        recovery_edge = (frm, to) == (OutboxState.CLAIMED, OutboxState.PENDING)
        expected = (frm, to) in _LEGAL or recovery_edge
        assert transition_allowed(frm, to, recovery=True) is expected

    def test_allowed_transitions_is_exactly_the_legal_set(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == _LEGAL

    def test_terminal_states_have_no_outgoing_edge(self) -> None:
        for terminal in (OutboxState.DELIVERED, OutboxState.FAILED):
            assert not any(
                transition_allowed(terminal, to, recovery=True) for to in OutboxState
            )

    @pytest.mark.parametrize("target", [OutboxState.PENDING, OutboxState.CLAIMED])
    def test_settle_only_targets_settle_states(self, target: OutboxState) -> None:
        claimed = _claim_one()

        with pytest.raises(OutboxTransitionError):
            settle(
                _CLIENT,
                claimed.id,
                claim_token=claimed.claim_token or "",
                state=target,
                reason=OutboxReason.CLAIM_RECOVERED,
            )

        assert _states() == {claimed.id: OutboxState.CLAIMED}

    def test_reason_sets_closed(self) -> None:
        assert dict(REASONS_BY_STATE) == {
            OutboxState.DELIVERED: frozenset(
                {OutboxReason.POSTED, OutboxReason.MARKER_ALREADY_PRESENT}
            ),
            OutboxState.UNVERIFIED: frozenset(
                {OutboxReason.READBACK_UNVERIFIED, OutboxReason.READBACK_MISMATCH}
            ),
            OutboxState.FAILED: frozenset(
                {
                    OutboxReason.TOOL_UNAVAILABLE,
                    OutboxReason.TURN_FAILED,
                    OutboxReason.BODY_UNREADABLE,
                }
            ),
            OutboxState.PENDING: frozenset({OutboxReason.CLAIM_RECOVERED}),
            OutboxState.CLAIMED: frozenset(
                {
                    OutboxReason.CLAIM_RECOVERED,
                    OutboxReason.READBACK_UNVERIFIED,
                    OutboxReason.READBACK_MISMATCH,
                }
            ),
        }
        assert {r.value for r in OutboxReason} == {
            "posted",
            "marker_already_present",
            "readback_unverified",
            "readback_mismatch",
            "tool_unavailable",
            "turn_failed",
            "body_unreadable",
            "claim_recovered",
        }
        assert {s.value for s in OutboxState} == {
            "pending",
            "claimed",
            "delivered",
            "unverified",
            "failed",
        }
        assert {r.value for r in OutboxRole} == {
            "tickets",
            "knowledge",
            "code_host",
            "chat",
        }


class TestClaim:
    def test_claim_oldest_first_with_limit(self) -> None:
        ids = []
        for i, stamp in enumerate(
            ("2026-11-01T10:00:00Z", "2026-11-01T11:00:00Z", "2026-11-01T12:00:00Z")
        ):
            with freeze_time(stamp):
                ids.append(_enqueue(marker=f"m{i}").entry.id)

        first = claim(_CLIENT, limit=2)
        rest = claim(_CLIENT, limit=2)

        assert [e.id for e in first] == ids[:2]
        assert [e.id for e in rest] == ids[2:]
        assert all(e.state is OutboxState.CLAIMED for e in first)

    def test_default_claim_limit_is_one(self) -> None:
        _enqueue(marker="a")
        _enqueue(marker="b")

        assert DEFAULT_CLAIM_LIMIT == 1
        assert len(claim(_CLIENT)) == 1

    def test_claim_on_empty_store(self) -> None:
        assert claim(_CLIENT) == []
        assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

    def test_claim_unverified_state(self) -> None:
        claimed = _claim_one()
        settle(
            _CLIENT,
            claimed.id,
            claim_token=claimed.claim_token or "",
            state=OutboxState.UNVERIFIED,
            reason=OutboxReason.READBACK_UNVERIFIED,
            detail="could not read back",
        )

        assert claim(_CLIENT) == []
        got = claim(_CLIENT, state=OutboxState.UNVERIFIED)

        assert [e.id for e in got] == [claimed.id]
        assert got[0].claimed_from is OutboxState.UNVERIFIED
        assert got[0].reason is OutboxReason.READBACK_UNVERIFIED
        assert got[0].detail == "could not read back"

    @pytest.mark.parametrize(
        "state",
        [OutboxState.CLAIMED, OutboxState.DELIVERED, OutboxState.FAILED],
    )
    def test_claim_rejects_other_states(self, state: OutboxState) -> None:
        with pytest.raises(OutboxTransitionError):
            claim(_CLIENT, state=state)

    @pytest.mark.parametrize("limit", [0, -3])
    def test_claim_rejects_bad_limit(self, limit: int) -> None:
        with pytest.raises(OutboxValidationError):
            claim(_CLIENT, limit=limit)

    def test_claim_records_token_claimant_claimed_from(self) -> None:
        entry = _enqueue().entry

        (claimed,) = claim(_CLIENT)

        assert claimed.id == entry.id
        assert claimed.state is OutboxState.CLAIMED
        assert claimed.claim_token is not None
        assert len(claimed.claim_token) == 32
        assert claimed.claimant == current_launch_marker()
        assert claimed.claimed_at is not None
        assert claimed.claimed_from is OutboxState.PENDING
        assert list_entries(_CLIENT) == [claimed]

    def test_each_claim_gets_a_fresh_token(self) -> None:
        _enqueue(marker="a")
        _enqueue(marker="b")

        tokens = {e.claim_token for e in claim(_CLIENT, limit=2)}

        assert len(tokens) == 2

    def test_claim_write_failure_rolls_back_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _enqueue(marker="a").entry
        second = _enqueue(marker="b").entry
        before = {
            entry.id: _record_file(entry.id).read_bytes() for entry in (first, second)
        }
        real_write = _layout.write_record
        calls = 0

        def fail_second(client: str, entry: OutboxEntry) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                msg = "injected claim write failure"
                raise OutboxRecordError(msg)
            real_write(client, entry)

        monkeypatch.setattr("cw.outbox._layout.write_record", fail_second)
        with pytest.raises(OutboxRecordError, match="injected"):
            claim(_CLIENT, limit=2)

        assert calls == 2
        assert {entry.id: entry.state for entry in list_entries(_CLIENT)} == {
            first.id: OutboxState.PENDING,
            second.id: OutboxState.PENDING,
        }
        after = {
            entry.id: _record_file(entry.id).read_bytes() for entry in (first, second)
        }
        assert after == before

    def test_claim_missing_body_settles_failed_body_unreadable(self) -> None:
        entry = _enqueue().entry
        _body_file(entry.id).unlink()

        assert claim(_CLIENT) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.FAILED
        assert stored.reason is OutboxReason.BODY_UNREADABLE
        assert stored.claim_token is None
        assert stored.claimant is None
        assert claim(_CLIENT) == []

    @pytest.mark.parametrize("replacement", ["XXXXX world", "short"])
    def test_claim_sha_mismatch_settles_failed_body_unreadable(
        self, replacement: str
    ) -> None:
        entry = _enqueue().entry
        _body_file(entry.id).write_text(replacement, encoding="utf-8")

        assert claim(_CLIENT) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.FAILED
        assert stored.reason is OutboxReason.BODY_UNREADABLE

    def test_unreadable_body_not_counted_toward_limit(self) -> None:
        with freeze_time("2026-11-01T10:00:00Z"):
            broken = _enqueue(marker="broken").entry
        with freeze_time("2026-11-01T11:00:00Z"):
            good_a = _enqueue(marker="good-a").entry
        with freeze_time("2026-11-01T12:00:00Z"):
            good_b = _enqueue(marker="good-b").entry
        _body_file(broken.id).unlink()

        got = claim(_CLIENT, limit=2)

        assert [e.id for e in got] == [good_a.id, good_b.id]
        assert _states()[broken.id] is OutboxState.FAILED

    def test_claim_transient_oserror_on_body_read_does_not_settle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = _enqueue().entry
        before = _record_file(entry.id).read_bytes()
        with monkeypatch.context() as patch:

            def boom(*_args: Any, **_kwargs: Any) -> bytes:
                raise OSError(errno.EIO, "input/output error")

            patch.setattr("cw.outbox._layout.read_body_bytes", boom)
            with pytest.raises(OutboxRecordError):
                claim(_CLIENT)

            (stored,) = list_entries(_CLIENT)
            assert stored.state is OutboxState.PENDING
            assert stored.claim_token is None
            assert stored.reason is None
            assert _record_file(entry.id).read_bytes() == before

        (retry,) = claim(_CLIENT)
        assert retry.id == entry.id
        assert retry.state is OutboxState.CLAIMED

    def test_transient_error_raises_before_any_claim_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with freeze_time("2026-11-01T10:00:00Z"):
            first = _enqueue(marker="first").entry
        with freeze_time("2026-11-01T11:00:00Z"):
            second = _enqueue(marker="second").entry
        real = _layout.read_body_bytes

        def flaky(client: str, entry_id: str) -> bytes:
            if entry_id == second.id:
                raise OSError(errno.EIO, "input/output error")
            return real(client, entry_id)

        with monkeypatch.context() as patch:
            patch.setattr("cw.outbox._layout.read_body_bytes", flaky)
            with pytest.raises(OutboxRecordError):
                claim(_CLIENT, limit=2)

        assert _states() == {
            first.id: OutboxState.PENDING,
            second.id: OutboxState.PENDING,
        }


class TestSettle:
    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (OutboxState.DELIVERED, OutboxReason.POSTED),
            (OutboxState.DELIVERED, OutboxReason.MARKER_ALREADY_PRESENT),
            (OutboxState.UNVERIFIED, OutboxReason.READBACK_UNVERIFIED),
            (OutboxState.UNVERIFIED, OutboxReason.READBACK_MISMATCH),
            (OutboxState.FAILED, OutboxReason.TOOL_UNAVAILABLE),
            (OutboxState.FAILED, OutboxReason.TURN_FAILED),
            (OutboxState.FAILED, OutboxReason.BODY_UNREADABLE),
        ],
    )
    def test_each_legal_settle_with_each_reason(
        self, state: OutboxState, reason: OutboxReason
    ) -> None:
        claimed = _claim_one()

        settled = settle(
            _CLIENT,
            claimed.id,
            claim_token=claimed.claim_token or "",
            state=state,
            reason=reason,
            detail="why",
        )

        assert settled.state is state
        assert settled.reason is reason
        assert settled.detail == "why"
        assert settled.claim_token is None
        assert settled.claimant is None
        assert settled.claimed_at is None
        assert settled.claimed_from is None
        assert settled.updated_at >= claimed.updated_at
        assert list_entries(_CLIENT) == [settled]

    def test_wrong_token_raises_outbox_claim_error(self) -> None:
        claimed = _claim_one()
        before = _record_file(claimed.id).read_bytes()

        with pytest.raises(OutboxClaimError):
            settle(
                _CLIENT,
                claimed.id,
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

        assert _record_file(claimed.id).read_bytes() == before

    def test_settle_from_non_claimed_state_raises(self) -> None:
        entry = _enqueue().entry

        with pytest.raises(OutboxClaimError):
            settle(
                _CLIENT,
                entry.id,
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

        assert _states() == {entry.id: OutboxState.PENDING}

    def test_settle_unknown_entry_raises_claim_error(self) -> None:
        with pytest.raises(OutboxClaimError):
            settle(
                _CLIENT,
                "a" * 32,
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

    def test_settle_rejects_malformed_entry_id(self) -> None:
        with pytest.raises(OutboxValidationError):
            settle(
                _CLIENT,
                "../../etc/passwd",
                claim_token=_BAD_TOKEN,
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (OutboxState.DELIVERED, OutboxReason.TURN_FAILED),
            (OutboxState.UNVERIFIED, OutboxReason.POSTED),
            (OutboxState.FAILED, OutboxReason.READBACK_MISMATCH),
            (OutboxState.FAILED, OutboxReason.CLAIM_RECOVERED),
        ],
    )
    def test_wrong_reason_for_state_raises_transition_error(
        self, state: OutboxState, reason: OutboxReason
    ) -> None:
        claimed = _claim_one()

        with pytest.raises(OutboxTransitionError):
            settle(
                _CLIENT,
                claimed.id,
                claim_token=claimed.claim_token or "",
                state=state,
                reason=reason,
            )

        assert _states() == {claimed.id: OutboxState.CLAIMED}

    def test_detail_over_cap_rejected(self) -> None:
        claimed = _claim_one()

        with pytest.raises(OutboxValidationError):
            settle(
                _CLIENT,
                claimed.id,
                claim_token=claimed.claim_token or "",
                state=OutboxState.FAILED,
                reason=OutboxReason.TURN_FAILED,
                detail="d" * (MAX_DETAIL_CHARS + 1),
            )

    def test_late_settle_after_recovery_raises_and_does_not_overwrite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enqueue()
        (stale,) = _claim_as(monkeypatch, dead_launch_marker())
        # Any claim runs recovery first; nothing is unverified, so it returns [].
        assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []
        before = _record_file(stale.id).read_bytes()

        with pytest.raises(OutboxClaimError):
            settle(
                _CLIENT,
                stale.id,
                claim_token=stale.claim_token or "",
                state=OutboxState.DELIVERED,
                reason=OutboxReason.POSTED,
            )

        assert _record_file(stale.id).read_bytes() == before
        assert _states() == {stale.id: OutboxState.PENDING}

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (OutboxState.DELIVERED, OutboxReason.POSTED),
            (OutboxState.FAILED, OutboxReason.TURN_FAILED),
        ],
    )
    def test_terminal_states_cannot_be_settled(
        self, state: OutboxState, reason: OutboxReason
    ) -> None:
        claimed = _claim_one()
        token = claimed.claim_token or ""
        settle(_CLIENT, claimed.id, claim_token=token, state=state, reason=reason)

        with pytest.raises(OutboxClaimError):
            settle(
                _CLIENT,
                claimed.id,
                claim_token=token,
                state=OutboxState.UNVERIFIED,
                reason=OutboxReason.READBACK_UNVERIFIED,
            )

        assert _states() == {claimed.id: state}


def _patch_probe_for(
    monkeypatch: pytest.MonkeyPatch, marker: LaunchMarker, *, present: bool
) -> None:
    """Restate ``tests/test_launch_marker.py::_patch_probe`` for one marker."""
    probe = (
        ProcessProbe(
            ProbeState.PRESENT, marker.start_time_ns, marker.start_since_boot_ns
        )
        if present
        else ProcessProbe(ProbeState.ABSENT, None)
    )
    monkeypatch.setattr(
        "cw.launch_marker.probe_process_start_time_ns", lambda _pid: probe
    )
    monkeypatch.setattr("cw.launch_marker.read_boot_id", lambda: marker.boot_id)


class TestRecovery:
    def test_live_claimant_with_ancient_claimed_at_not_recovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = live_launch_marker()
        _patch_probe_for(monkeypatch, marker, present=True)
        with freeze_time(_ANCIENT) as frozen:
            _enqueue()
            (claimed,) = _claim_as(monkeypatch, marker)
            frozen.move_to(_RECENT)

            assert claim(_CLIENT) == []
            assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.CLAIMED
        assert stored.claim_token == claimed.claim_token
        assert claimant_state(stored) is LaunchOwnerState.IN_FLIGHT

    def test_dead_claimant_with_fresh_claimed_at_recovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = dead_launch_marker()
        _patch_probe_for(monkeypatch, marker, present=False)
        with freeze_time(_RECENT):
            _enqueue()
            (claimed,) = _claim_as(monkeypatch, marker)

            # Same instant: age plays no part, only the owner's absence does.
            assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.id == claimed.id
        assert stored.state is OutboxState.PENDING
        assert stored.reason is OutboxReason.CLAIM_RECOVERED

    def test_claimant_none_not_recovered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with freeze_time(_ANCIENT) as frozen:
            _enqueue()
            (claimed,) = _claim_as(monkeypatch, None)
            assert claimed.claimant is None
            frozen.move_to(_RECENT)
            monkeypatch.setattr("cw.launch_marker.read_boot_id", lambda: None)

            assert claim(_CLIENT) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.CLAIMED
        assert claimant_state(stored) is LaunchOwnerState.UNKNOWN

    @pytest.mark.parametrize(
        ("marker_factory", "verdict", "recovered"),
        [
            (dead_launch_marker, LaunchOwnerState.OWNER_GONE, True),
            (live_launch_marker, LaunchOwnerState.IN_FLIGHT, False),
            (lambda: None, LaunchOwnerState.UNKNOWN, False),
        ],
        ids=["owner_gone", "in_flight", "unknown"],
    )
    def test_only_owner_gone_is_recovered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        marker_factory: Callable[[], LaunchMarker | None],
        verdict: LaunchOwnerState,
        recovered: bool,
    ) -> None:
        _enqueue()
        (claimed,) = _claim_as(monkeypatch, marker_factory())
        assert claimant_state(claimed) is verdict

        assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

        (stored,) = list_entries(_CLIENT)
        expected = OutboxState.PENDING if recovered else OutboxState.CLAIMED
        assert stored.state is expected

    def test_recovered_pending_gets_claim_recovered_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enqueue()
        _claim_as(monkeypatch, dead_launch_marker())

        assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.PENDING
        assert stored.reason is OutboxReason.CLAIM_RECOVERED
        assert stored.claim_token is None
        assert stored.claimant is None
        assert stored.claimed_at is None
        assert stored.claimed_from is None
        # And it is claimable again by a live process.
        assert [e.id for e in claim(_CLIENT)] == [stored.id]

    def test_recovered_unverified_returns_to_unverified_not_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _claim_one()
        settle(
            _CLIENT,
            first.id,
            claim_token=first.claim_token or "",
            state=OutboxState.UNVERIFIED,
            reason=OutboxReason.READBACK_MISMATCH,
            detail="seen different",
        )
        (second,) = _claim_as(
            monkeypatch, dead_launch_marker(), state=OutboxState.UNVERIFIED
        )
        assert second.claimed_from is OutboxState.UNVERIFIED

        assert claim(_CLIENT) == []

        (stored,) = list_entries(_CLIENT)
        assert stored.state is OutboxState.UNVERIFIED
        assert stored.reason is OutboxReason.READBACK_MISMATCH
        assert stored.detail == "seen different"
        assert stored.claim_token is None

    def test_recycled_pid_is_recovered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enqueue()
        _claim_as(monkeypatch, dead_launch_marker(recycled=True))

        assert claim(_CLIENT, state=OutboxState.UNVERIFIED) == []

        assert _states_list() == [OutboxState.PENDING]

    def test_list_is_pure_read_no_recovery_no_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enqueue(marker="dead")
        _enqueue(marker="plain")
        _claim_as(monkeypatch, dead_launch_marker())

        def snapshot() -> dict[str, tuple[int, bytes]]:
            files = {
                str(p): (p.stat().st_mtime_ns, p.read_bytes())
                for p in _client_dir().iterdir()
                if p.is_file()
            }
            dirs = {
                str(d): (d.stat().st_mtime_ns, b"")
                for d in (state_dir() / "outbox", _client_dir())
            }
            return {**files, **dirs}

        before = snapshot()
        entries = list_entries(_CLIENT)
        for entry in entries:
            claimant_state(entry)
        list_clients()

        assert snapshot() == before
        assert {e.state for e in entries} == {
            OutboxState.CLAIMED,
            OutboxState.PENDING,
        }

    def test_claimant_state_verdicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending = _enqueue(marker="p").entry
        assert claimant_state(pending) is None
        for marker, verdict in (
            (dead_launch_marker(), LaunchOwnerState.OWNER_GONE),
            (live_launch_marker(), LaunchOwnerState.IN_FLIGHT),
            (None, LaunchOwnerState.UNKNOWN),
        ):
            _enqueue(marker=f"k{verdict.value}")
            (claimed,) = _claim_as(monkeypatch, marker)
            assert claimant_state(claimed) is verdict


def _states_list() -> list[OutboxState]:
    return [e.state for e in list_entries(_CLIENT)]


class TestLock:
    def test_outbox_lock_is_leaf_ranked_named_outbox(self) -> None:
        with _outbox_lock(_CLIENT):
            held = held_locks()

        assert len(held) == 1
        (lock,) = held
        assert isinstance(lock, HeldLock)
        assert lock.name == "outbox"
        assert lock.rank is LockRank.LEAF
        assert lock.path == str(_client_dir() / ".outbox.lock")
        assert held_locks() == ()

    def test_lock_is_keyed_per_client(self) -> None:
        paths = []
        for client in ("a", "b"):
            with _outbox_lock(client):
                paths.append(held_locks()[0].path)
        assert paths == [
            str(_client_dir("a") / ".outbox.lock"),
            str(_client_dir("b") / ".outbox.lock"),
        ]

    def test_lock_rejects_bad_client_before_touching_disk(self) -> None:
        with pytest.raises(OutboxValidationError), _outbox_lock("../x"):
            pytest.fail("must not reach body")
        assert not (state_dir() / "outbox").exists()

    def test_enqueue_under_sessions_lock(self) -> None:
        with sessions_lock():
            assert _enqueue().created

    def test_enqueue_under_dev_queue_lock(self) -> None:
        with dev_queue_lock():
            assert _enqueue().created

    def test_enqueue_blocks_while_outbox_lock_file_held_on_another_fd(self) -> None:
        _enqueue(marker="first")
        done = threading.Event()
        results: list[EnqueueResult] = []

        def second() -> None:
            results.append(_enqueue(marker="second"))
            done.set()

        with _hold_flock(_client_dir() / ".outbox.lock") as release:
            worker = threading.Thread(target=second, daemon=True)
            worker.start()
            worker.join(timeout=_BLOCKED_JOIN_S)
            assert not done.is_set()
            assert worker.is_alive()
            release()
            worker.join(timeout=_JOIN_TIMEOUT_S)

        assert done.is_set()
        assert results[0].created is True

    @pytest.mark.lock_violations_expected("order")
    def test_acquiring_another_lock_under_outbox_lock_is_a_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_LOCK_DEBUG", "1")

        with (
            _outbox_lock(_CLIENT),
            pytest.raises(CwLockOrderError, match="lock order violation"),
            _history_lock("c"),
        ):
            pytest.fail("must not reach body")

    def test_no_subprocess_under_outbox_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        violations = _install_launch_hook(monkeypatch)

        _enqueue(marker="a")
        _enqueue(marker="b")
        claimed = claim(_CLIENT, limit=2)
        settle(
            _CLIENT,
            claimed[0].id,
            claim_token=claimed[0].claim_token or "",
            state=OutboxState.DELIVERED,
            reason=OutboxReason.POSTED,
        )
        list_entries(_CLIENT)
        list_clients()
        read_parts(claimed[1])
        claimant_state(claimed[1])
        # Recovery runs inside ``claim`` under the lock.
        _enqueue(marker="c")
        _claim_as(monkeypatch, dead_launch_marker())
        claim(_CLIENT, state=OutboxState.UNVERIFIED)

        assert violations == []

    def test_launch_hook_positive_control(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        violations = _install_launch_hook(monkeypatch)

        with _outbox_lock(_CLIENT):
            subprocess.run([sys.executable, "-c", "pass"], check=True)

        assert violations == ["Popen"]

    def test_outbox_package_imports_no_forbidden_modules(self) -> None:
        forbidden = (
            "subprocess",
            "cw.gh",
            "cw.native_daemon",
            "cw.tracker_io",
            "cw.tracker_post",
        )
        package_dir = Path(outbox_pkg.__file__).parent
        sources = sorted(package_dir.glob("*.py"))
        assert len(sources) >= 5
        imported: set[str] = set()
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                    imported.update(f"{node.module}.{a.name}" for a in node.names)
        for name in imported:
            for bad in forbidden:
                assert name != bad
                assert not name.startswith(bad + ".")

    def test_concurrent_enqueue_same_key_yields_one_entry(self) -> None:
        barrier = threading.Barrier(_THREADS)

        def run() -> EnqueueResult:
            barrier.wait(timeout=_JOIN_TIMEOUT_S)
            return _enqueue()

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(run) for _ in range(_THREADS)]
            results = [f.result(timeout=_JOIN_TIMEOUT_S) for f in futures]

        assert sorted(r.created for r in results) == [False] * (_THREADS - 1) + [True]
        assert len({r.entry.id for r in results}) == 1
        assert len(list_entries(_CLIENT)) == 1

    def test_concurrent_claims_yield_disjoint_entries(self) -> None:
        for i in range(_THREADS * 2):
            _enqueue(marker=f"m{i}")
        barrier = threading.Barrier(_THREADS)

        def run() -> list[str]:
            barrier.wait(timeout=_JOIN_TIMEOUT_S)
            return [e.id for e in claim(_CLIENT, limit=2)]

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(run) for _ in range(_THREADS)]
            claimed = [f.result(timeout=_JOIN_TIMEOUT_S) for f in futures]

        flat = [entry_id for ids in claimed for entry_id in ids]
        assert len(flat) == len(set(flat)) == _THREADS * 2
        assert all(len(ids) == 2 for ids in claimed)


_EXEC_NAMES = (
    "system",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
)


def _install_launch_hook(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every launch made while the outbox lock is held.

    Test-local on purpose: the shared lock-invariants harness records a launch
    only under a SESSIONS-rank lock. ``Popen.__init__`` is captured at install
    time and always delegated to, so the ceiling, sandbox, host-probe and
    lock-trace wrappers keep chaining underneath this one.
    """
    violations: list[str] = []
    real_init = subprocess.Popen.__init__

    def outbox_held() -> bool:
        return any(lock.name == "outbox" for lock in held_locks())

    def hooked_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        if outbox_held():
            violations.append("Popen")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", hooked_init)

    def make_spy(name: str) -> Callable[..., int]:
        def spy(*_args: Any, **_kwargs: Any) -> int:
            if outbox_held():
                violations.append(name)
            msg = f"os.{name} is blocked in this test"
            raise RuntimeError(msg)

        return spy

    for name in _EXEC_NAMES:
        monkeypatch.setattr(os, name, make_spy(name))
    return violations
