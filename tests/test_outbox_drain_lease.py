"""The per-client drain lease and its two sidecars (#3706, RFC 0019 O2).

``drain_lease`` is a long-held, unguarded, non-blocking flock on
``outbox/<client>/drain.lock``; ``drain_lease_held`` is the tick's probe. The
unreachable marker (``drain.unreachable.<role>``) and the held list
(``drain.held``) carry no timestamp and are never parsed as entries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw._lock_guard import held_locks
from cw.config import state_dir
from cw.outbox import (
    DrainLeaseHeldError,
    HeldList,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    UnreachableMarker,
    _layout,
    append_held,
    clear_all_markers,
    clear_held,
    clear_marker,
    drain_lease,
    drain_lease_held,
    marked_roles,
    read_held,
    read_marker,
    write_marker,
)
from tests._shared.lock_helpers import _hold_flock
from tests._shared.outbox_builders import CLIENT, enqueue

# The real flock tests below exercise OS behaviour that differs on macOS.
pytestmark = pytest.mark.platform_sensitive

_INVALID_UTF8 = b"\xff\xfe{}"
_ID_A = "a" * 32
_ID_B = "b" * 32


class TestDrainLease:
    def test_exclusive_against_another_open_file_description(self) -> None:
        with (
            _hold_flock(_layout.drain_lock_path(CLIENT)),
            pytest.raises(DrainLeaseHeldError),
            drain_lease(CLIENT),
        ):
            pytest.fail("the lease must not be granted while held")

    def test_released_on_normal_exit(self) -> None:
        with drain_lease(CLIENT):
            assert drain_lease_held(CLIENT) is True
        assert drain_lease_held(CLIENT) is False
        with drain_lease(CLIENT):
            pass

    def test_released_on_exceptional_exit(self) -> None:
        msg = "boom"
        with pytest.raises(RuntimeError), drain_lease(CLIENT):
            raise RuntimeError(msg)
        assert drain_lease_held(CLIENT) is False

    def test_nested_second_lease_in_process_is_refused(self) -> None:
        with (
            drain_lease(CLIENT),
            pytest.raises(DrainLeaseHeldError),
            drain_lease(CLIENT),
        ):
            pytest.fail("a second lease must fail fast")

    def test_lease_is_invisible_to_the_lock_guard(self) -> None:
        with drain_lease(CLIENT):
            assert held_locks() == ()

    def test_bad_client_refused(self) -> None:
        with pytest.raises(ValueError, match="client"), drain_lease("../x"):
            pass

    def test_unopenable_lock_file_is_a_record_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(*_args: object, **_kwargs: object) -> None:
            msg = "denied"
            raise PermissionError(msg)

        monkeypatch.setattr(Path, "open", _denied)

        with pytest.raises(OutboxRecordError), drain_lease(CLIENT):
            pytest.fail("the lease must not be granted")


class TestDrainLeaseHeld:
    def test_false_without_a_lock_file_and_creates_nothing(self) -> None:
        assert drain_lease_held(CLIENT) is False
        assert not (state_dir() / "outbox").exists()

    def test_true_while_a_contender_holds_it(self) -> None:
        with _hold_flock(_layout.drain_lock_path(CLIENT)):
            assert drain_lease_held(CLIENT) is True
        assert drain_lease_held(CLIENT) is False


class TestUnreachableMarker:
    def test_write_read_clear(self) -> None:
        marker = UnreachableMarker(
            role=OutboxRole.TICKETS,
            reason=OutboxReason.BINDING_UNREACHABLE,
            announced=[_ID_A],
        )

        write_marker(CLIENT, marker)

        assert read_marker(CLIENT, OutboxRole.TICKETS) == marker
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})
        clear_marker(CLIENT, OutboxRole.TICKETS)
        assert read_marker(CLIENT, OutboxRole.TICKETS) is None
        assert marked_roles(CLIENT) == frozenset()

    def test_clear_all_roles(self) -> None:
        for role in (OutboxRole.TICKETS, OutboxRole.KNOWLEDGE):
            write_marker(
                CLIENT,
                UnreachableMarker(role=role, reason=OutboxReason.TRANSIENT_FAILURE),
            )

        clear_all_markers(CLIENT)

        assert marked_roles(CLIENT) == frozenset()

    def test_clear_without_a_directory_is_a_no_op(self) -> None:
        clear_all_markers(CLIENT)
        clear_marker(CLIENT, OutboxRole.TICKETS)
        assert not (state_dir() / "outbox" / CLIENT).exists()

    def test_corrupt_marker_reads_as_set(self) -> None:
        path = _layout.unreachable_path(CLIENT, OutboxRole.TICKETS)
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")

        marker = read_marker(CLIENT, OutboxRole.TICKETS)

        assert marker is not None
        assert marker.role is OutboxRole.TICKETS
        assert marker.announced == []
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})

    def test_invalid_utf8_marker_reads_as_set(self) -> None:
        path = _layout.unreachable_path(CLIENT, OutboxRole.TICKETS)
        path.parent.mkdir(parents=True)
        path.write_bytes(_INVALID_UTF8)

        marker = read_marker(CLIENT, OutboxRole.TICKETS)

        assert marker is not None
        assert marker.reason is OutboxReason.TRANSIENT_FAILURE
        assert marker.announced == []
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})

    def test_announced_round_trips_and_no_timestamp(self) -> None:
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.KNOWLEDGE,
                reason=OutboxReason.TRANSIENT_FAILURE,
                announced=[_ID_A, _ID_B],
            ),
        )
        path = _layout.unreachable_path(CLIENT, OutboxRole.KNOWLEDGE)
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data == {
            "schema_version": 1,
            "role": "knowledge",
            "reason": "transient_failure",
            "announced": [_ID_A, _ID_B],
        }
        assert path.name == "drain.unreachable.knowledge"

    def test_marker_filed_under_another_role_keeps_this_role(self) -> None:
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.KNOWLEDGE,
                reason=OutboxReason.BINDING_UNREACHABLE,
                announced=[_ID_A],
            ),
        )
        source = _layout.unreachable_path(CLIENT, OutboxRole.KNOWLEDGE)
        target = _layout.unreachable_path(CLIENT, OutboxRole.TICKETS)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

        marker = read_marker(CLIENT, OutboxRole.TICKETS)

        assert marker == UnreachableMarker(
            role=OutboxRole.TICKETS, reason=OutboxReason.BINDING_UNREACHABLE
        )

    def test_unremovable_marker_is_a_record_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.TICKETS, reason=OutboxReason.TRANSIENT_FAILURE
            ),
        )

        def _busy(*_args: object, **_kwargs: object) -> None:
            msg = "busy"
            raise PermissionError(msg)

        monkeypatch.setattr(Path, "unlink", _busy)

        with pytest.raises(OutboxRecordError):
            clear_marker(CLIENT, OutboxRole.TICKETS)

    def test_non_release_reason_refused(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            UnreachableMarker(role=OutboxRole.TICKETS, reason=OutboxReason.POSTED)

    def test_marker_is_not_parsed_as_an_entry(self) -> None:
        entry = enqueue().entry
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.TICKETS, reason=OutboxReason.BINDING_UNREACHABLE
            ),
        )

        assert [e.id for e in _layout.scan_entries(CLIENT)] == [entry.id]


class TestHeldList:
    def test_append_is_idempotent_and_read_returns_the_ids(self) -> None:
        append_held(CLIENT, _ID_A)
        append_held(CLIENT, _ID_B)
        append_held(CLIENT, _ID_A)

        assert read_held(CLIENT) == frozenset({_ID_A, _ID_B})
        data = json.loads(_layout.held_path(CLIENT).read_text(encoding="utf-8"))
        assert data == {"schema_version": 1, "entry_ids": [_ID_A, _ID_B]}
        assert set(HeldList.model_fields) == {"schema_version", "entry_ids"}

    def test_missing_file_reads_empty(self) -> None:
        assert read_held(CLIENT) == frozenset()
        assert not (state_dir() / "outbox").exists()

    def test_corrupt_file_reads_empty_and_the_next_append_rewrites(self) -> None:
        path = _layout.held_path(CLIENT)
        path.parent.mkdir(parents=True)
        path.write_text("[not an object", encoding="utf-8")

        assert read_held(CLIENT) == frozenset()
        append_held(CLIENT, _ID_A)
        assert read_held(CLIENT) == frozenset({_ID_A})

    def test_invalid_utf8_file_reads_empty_and_the_next_append_rewrites(self) -> None:
        path = _layout.held_path(CLIENT)
        path.parent.mkdir(parents=True)
        path.write_bytes(_INVALID_UTF8)

        assert read_held(CLIENT) == frozenset()
        append_held(CLIENT, _ID_A)
        assert read_held(CLIENT) == frozenset({_ID_A})

    def test_clear_removes_the_file(self) -> None:
        append_held(CLIENT, _ID_A)

        clear_held(CLIENT)

        assert not _layout.held_path(CLIENT).exists()
        assert read_held(CLIENT) == frozenset()
        clear_held(CLIENT)

    def test_append_writes_atomically(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        real = _layout._write

        def _spy(path: Path, text: str) -> None:
            calls.append(text)
            real(path, text)

        monkeypatch.setattr("cw.outbox._lease._write", _spy)

        append_held(CLIENT, _ID_A)

        assert len(calls) == 1

    def test_held_file_is_not_parsed_as_an_entry(self) -> None:
        entry = enqueue().entry
        append_held(CLIENT, _ID_A)

        assert [e.id for e in _layout.scan_entries(CLIENT)] == [entry.id]
