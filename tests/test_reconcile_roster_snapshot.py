"""Contract tests for ``cw.reconcile.roster_snapshot`` (#2550).

``reconcile()`` reads the daemon roster (``claude agents --json``) into one
tick-scoped :class:`RosterSnapshot` before it takes ``sessions_lock``; the
in-lock code only looks it up through :func:`lookup_roster`. These tests pin
the snapshot's mechanics with a fake reader (never a real ``claude``) and
``freezegun`` for the clock:

1. A capture keeps the latest attempt, success or failure; a failure carries
   the reader's exception text.
2. ``lookup`` fails closed: missing, stale (strict ``<`` age bound, a negative
   age too) and failed readings all raise :class:`RosterUnavailableError`
   with a ``reason``; an empty roster is a hit.
3. ``refresh`` reuses a success younger than ``ROSTER_REUSE_MAX_AGE_SECONDS``
   and re-reads anything else.
4. ``shared()`` is the dirty-check pre-pass's reader over the same snapshot.
5. The constants keep the spawn-grace invariant chain, and the module is a
   leaf.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time

from cw import spawn
from cw.reconcile import roster_snapshot
from cw.reconcile._shared._constants import SPAWN_GRACE_SECONDS
from cw.reconcile._shared._roster import (
    CLAUDE_AGENTS_TIMEOUT_SECONDS,
    ROSTER_REGISTRATION_SLACK_SECONDS,
)
from cw.reconcile.roster_snapshot import (
    ROSTER_READ_ERRORS,
    ROSTER_REUSE_MAX_AGE_SECONDS,
    ROSTER_SNAPSHOT_MAX_AGE_SECONDS,
    RosterSnapshot,
    RosterUnavailableError,
    lookup_roster,
)

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_AGENTS: list[dict[str, object]] = [{"sessionId": "abcd1234-0000-0000-0000-0000"}]
_LOGGER = "cw.reconcile.roster_snapshot"


class _Reader:
    """A roster reader that replays *results* in order and counts its calls."""

    def __init__(self, *results: list[dict[str, object]] | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self) -> list[dict[str, object]]:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return list(result)


_READER_ERRORS: list[Exception] = [
    subprocess.CalledProcessError(1, ["claude", "agents", "--json"]),
    json.JSONDecodeError("Expecting value", "", 0),
    FileNotFoundError(2, "No such file or directory", "claude"),
    subprocess.TimeoutExpired(cmd=["claude", "agents", "--json"], timeout=10),
]


def _unavailable_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _LOGGER and r.getMessage().startswith("roster_unavailable")
    ]


class TestCaptureAndLookup:
    def test_capture_success_is_looked_up(self) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW):
            snap.capture(_Reader(_AGENTS))
            assert snap.lookup() == _AGENTS
        assert snap.captures == 1
        assert snap.captured_at == _NOW

    @pytest.mark.parametrize("error", _READER_ERRORS, ids=lambda e: type(e).__name__)
    def test_capture_error_is_a_failed_reading(self, error: Exception) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW):
            snap.capture(_Reader(error))
            with pytest.raises(RosterUnavailableError) as info:
                snap.lookup()
        assert info.value.reason == "failed"
        assert type(error).__name__ in str(info.value)
        assert snap.captures == 1
        assert snap.captured_at == _NOW

    def test_unlisted_exception_propagates_uncaught(self) -> None:
        snap = RosterSnapshot()
        with pytest.raises(PermissionError):
            snap.capture(_Reader(PermissionError(13, "denied")))

    def test_missing_snapshot_raises(self) -> None:
        snap = RosterSnapshot()
        with pytest.raises(RosterUnavailableError) as info:
            snap.lookup()
        assert info.value.reason == "missing"
        assert snap.captures == 0
        assert snap.captured_at is None

    def test_stale_at_the_max_age(self) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW) as clock:
            snap.capture(_Reader(_AGENTS))
            clock.tick(ROSTER_SNAPSHOT_MAX_AGE_SECONDS - 0.1)
            assert snap.lookup() == _AGENTS
            clock.tick(0.1)
            with pytest.raises(RosterUnavailableError) as info:
                snap.lookup()
        assert info.value.reason == "stale"
        assert f"age={ROSTER_SNAPSHOT_MAX_AGE_SECONDS:.1f}s" in str(info.value)

    def test_negative_age_is_stale(self) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW) as clock:
            snap.capture(_Reader(_AGENTS))
            clock.move_to(_NOW - timedelta(seconds=1))
            with pytest.raises(RosterUnavailableError) as info:
                snap.lookup()
        assert info.value.reason == "stale"

    def test_empty_roster_is_a_hit(self) -> None:
        snap = RosterSnapshot()
        snap.capture(_Reader([]))
        assert snap.lookup() == []

    def test_failed_recapture_overwrites_an_earlier_success(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS, _READER_ERRORS[0])
        snap.capture(reader)
        snap.capture(reader)
        with pytest.raises(RosterUnavailableError) as info:
            snap.lookup()
        assert info.value.reason == "failed"
        assert snap.captures == 2


class TestRefresh:
    def test_reuses_a_young_success(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS)
        with freeze_time(_NOW) as clock:
            snap.capture(reader)
            clock.tick(ROSTER_REUSE_MAX_AGE_SECONDS - 0.1)
            snap.refresh(reader)
        assert reader.calls == 1
        assert snap.captures == 1
        assert snap.captured_at == _NOW

    def test_rereads_a_success_at_the_reuse_window(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS, [{"sessionId": "ffff0000-1"}])
        with freeze_time(_NOW) as clock:
            snap.capture(reader)
            clock.tick(ROSTER_REUSE_MAX_AGE_SECONDS)
            snap.refresh(reader)
            assert snap.lookup() == [{"sessionId": "ffff0000-1"}]
        assert reader.calls == 2
        assert snap.captured_at == _NOW + timedelta(
            seconds=ROSTER_REUSE_MAX_AGE_SECONDS
        )

    def test_always_rereads_a_held_failure(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_READER_ERRORS[3], _AGENTS)
        with freeze_time(_NOW):
            snap.capture(reader)
            snap.refresh(reader)
            assert snap.lookup() == _AGENTS
        assert reader.calls == 2

    def test_reads_when_nothing_is_held(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS)
        snap.refresh(reader)
        assert reader.calls == 1
        assert snap.lookup() == _AGENTS

    def test_reads_the_reuse_window_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(roster_snapshot, "ROSTER_REUSE_MAX_AGE_SECONDS", 0.0)
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS)
        with freeze_time(_NOW):
            snap.refresh(reader)
            snap.refresh(reader)
        assert reader.calls == 2


class TestShared:
    def test_first_call_captures_then_reuses(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS)
        shared = snap.shared(reader)
        with freeze_time(_NOW):
            assert shared() == _AGENTS
            assert shared() == _AGENTS
        assert reader.calls == 1
        assert snap.captures == 1

    def test_failure_raises_a_roster_read_error(self) -> None:
        snap = RosterSnapshot()
        shared = snap.shared(_Reader(_READER_ERRORS[0]))
        with pytest.raises(RosterUnavailableError) as info:
            shared()
        assert info.value.reason == "failed"
        assert isinstance(info.value, ROSTER_READ_ERRORS)

    def test_the_final_refresh_reuses_the_shared_read(self) -> None:
        snap = RosterSnapshot()
        reader = _Reader(_AGENTS)
        with freeze_time(_NOW):
            snap.shared(reader)()
            snap.refresh(reader)
        assert reader.calls == 1


class TestLookupRoster:
    def test_none_is_missing_and_logs_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(RosterUnavailableError) as info,
        ):
            lookup_roster(None)
        assert info.value.reason == "missing"
        (record,) = _unavailable_records(caplog)
        assert "cause=missing" in record.getMessage()

    def test_stale_cause_names_the_age(self, caplog: pytest.LogCaptureFixture) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW) as clock:
            snap.capture(_Reader(_AGENTS))
            clock.tick(21.0)
            with (
                caplog.at_level(logging.WARNING, logger=_LOGGER),
                pytest.raises(RosterUnavailableError),
            ):
                lookup_roster(snap)
        (record,) = _unavailable_records(caplog)
        assert "cause=stale age=21.0s" in record.getMessage()

    def test_failed_cause_names_the_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        snap = RosterSnapshot()
        snap.capture(_Reader(_READER_ERRORS[3]))
        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(RosterUnavailableError) as info,
        ):
            lookup_roster(snap)
        assert info.value.reason == "failed"
        (record,) = _unavailable_records(caplog)
        assert "cause=failed: TimeoutExpired" in record.getMessage()
        assert "daemon outage" in record.getMessage()

    def test_hit_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        snap = RosterSnapshot()
        snap.capture(_Reader(_AGENTS))
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert lookup_roster(snap) == _AGENTS
        assert _unavailable_records(caplog) == []


class TestUnavailableReason:
    @pytest.mark.parametrize(
        ("reason", "skips"), [("failed", False), ("stale", True), ("missing", True)]
    )
    def test_only_stale_and_missing_skip_the_tick(
        self, reason: str, skips: bool
    ) -> None:
        snap = RosterSnapshot()
        with freeze_time(_NOW) as clock:
            if reason == "failed":
                snap.capture(_Reader(_READER_ERRORS[0]))
            elif reason == "stale":
                snap.capture(_Reader(_AGENTS))
                clock.tick(ROSTER_SNAPSHOT_MAX_AGE_SECONDS)
            with pytest.raises(RosterUnavailableError) as info:
                snap.lookup()
        assert info.value.reason == reason
        assert info.value.skips_tick is skips

    def test_error_is_not_a_cw_error(self) -> None:
        from cw.exceptions import CwError

        assert not issubclass(RosterUnavailableError, CwError)


class TestConstants:
    def test_spawn_grace_invariant_chain(self) -> None:
        assert (
            CLAUDE_AGENTS_TIMEOUT_SECONDS
            < ROSTER_SNAPSHOT_MAX_AGE_SECONDS
            < SPAWN_GRACE_SECONDS
        )
        assert ROSTER_REUSE_MAX_AGE_SECONDS < ROSTER_SNAPSHOT_MAX_AGE_SECONDS

    def test_registration_slack_covers_the_spawn_roster_poll(self) -> None:
        """M1: a session's ref may land on the roster up to the spawn/resume
        roster poll (``spawn._ROSTER_POLL_TIMEOUT_SECS``) after its
        ``started_at``/``resumed_at``; the slack is that plus 5 s of skew."""
        assert spawn._ROSTER_POLL_TIMEOUT_SECS == 10.0
        assert ROSTER_REGISTRATION_SLACK_SECONDS == (
            spawn._ROSTER_POLL_TIMEOUT_SECS + 5.0
        )

    def test_read_errors_tuple(self) -> None:
        expected = (
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
            RosterUnavailableError,
        )
        assert expected == ROSTER_READ_ERRORS


class TestLeaf:
    def test_imports_only_probe_store_and_the_standard_library(self) -> None:
        tree = ast.parse(inspect.getsource(roster_snapshot))
        runtime: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                runtime.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                runtime.add(node.module)
        project = {m for m in runtime if m.split(".")[0] == "cw"}
        assert project == {"cw.reconcile.probe_store"}
        others = {m.split(".")[0] for m in runtime - project}
        assert others <= sys.stdlib_module_names | {"__future__"}, others
