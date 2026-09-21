"""Tests for the ``cw signal-park`` park-comment stamp command (#2135).

``cw signal-park`` is the PRODUCER half of the abandoned-exit park: a headless
worker runs it once, from its session worktree root, immediately after its park
comment has posted and just before it emits its exit sentinel. It writes a
``park_comment_marker`` into ``.claude/cw-context.json`` — the worker's own
recorded claim that it posted that comment and is taking that exit, never an
observation by cw that a tracker comment exists.

Everything here pins the fail-open contract. The command takes no arguments,
exits 0 on every foreseeable failure, and writes nothing when it cannot build a
trustworthy marker: a caller must be able to ignore it entirely and still emit
its sentinel unchanged. The one thing it must never do is record a marker that
does not match the RUNNING dev-queue row, because the Stop hook parks a row on
exactly that evidence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.dev_queue import save_dev_queue
from cw.events import read_events
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    PARK_COMMENT_MARKER_KEY,
    DevQueueStore,
    QueueItemStatus,
    SessionOrigin,
    Stage,
)
from tests.conftest import (
    _hold_context_lock,
    _invoke_hook_command,
    _make_ticket_task,
    _write_hook_context_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def _context_path(worktree: Path) -> Path:
    return worktree / HOOK_CONTEXT_RELATIVE_PATH


def _read_context(worktree: Path) -> dict[str, object]:
    raw = json.loads(_context_path(worktree).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _seeded_worktree(tmp_path: Path, name: str = "wt") -> Path:
    """A worktree carrying a freshly-written cw-context.json."""
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree)
    return worktree


def _seed_running_row(
    worktree: Path,
    *,
    stage: Stage = Stage.IMPL,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
) -> None:
    """Seed the dev queue with the row the seeded context file describes.

    The ids are read back out of the written context rather than restated, so
    this can never drift from ``_write_hook_context_file``'s own literals.
    """
    context = _read_context(worktree)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=context["ticket_id"],
                    client=context["client"],
                    session_id=context["session_id"],
                    status=status,
                    stage=stage,
                )
            ]
        )
    )


def _invoke_signal_park(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> object:  # click.testing.Result
    monkeypatch.chdir(worktree)
    return _invoke_hook_command("signal-park", {})


class TestSignalParkRegistration:
    def test_signal_park_is_registered_and_help_states_the_contract(self) -> None:
        """The --help text is the worker-facing contract; pin its load-bearing
        phrases, whitespace-normalized because Click re-wraps at ~80 columns."""
        from click.testing import CliRunner

        from cw.cli import main

        assert "signal-park" in main.commands

        result = CliRunner().invoke(main, ["signal-park", "--help"])

        assert result.exit_code == 0
        normalized = " ".join(result.output.split())
        assert "recorded claim" in normalized
        assert "exits 0 on every failure it can foresee" in normalized
        assert "park marker NOT recorded" in normalized


class TestSignalParkStamps:
    def test_stamps_the_marker_for_the_running_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.models import read_park_comment_marker

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        # Sibling hook-written keys the real file carries; both must survive.
        context = _read_context(worktree)
        context["agent_spawn_stamp"] = {"unresolved_count": 1}
        context["queue_metadata"] = {"attempt": 2}
        _context_path(worktree).write_text(json.dumps(context), encoding="utf-8")

        with freeze_time("2026-01-01T00:03:00Z"):
            result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        after = _read_context(worktree)
        marker = read_park_comment_marker(after)
        assert marker is not None
        assert marker.ticket_id == "940"
        assert marker.stage is Stage.IMPL
        assert marker.session_id == "sess940g"
        assert marker.posted_at.utcoffset() is not None
        assert after["agent_spawn_stamp"] == {"unresolved_count": 1}
        assert after["queue_metadata"] == {"attempt": 2}
        assert after["session_id"] == "sess940g"
        assert "park marker recorded (ticket 940, stage impl)" in result.output

    def test_latest_stamp_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.models import read_park_comment_marker

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)

        with freeze_time("2026-01-01T00:03:00Z"):
            _invoke_signal_park(worktree, monkeypatch)
        first = read_park_comment_marker(_read_context(worktree))
        with freeze_time("2026-01-01T00:09:00Z"):
            _invoke_signal_park(worktree, monkeypatch)
        second = read_park_comment_marker(_read_context(worktree))

        assert first is not None
        assert second is not None
        assert second.posted_at > first.posted_at

    def test_stage_comes_from_the_row_not_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command takes no arguments: a doc typo cannot fake a stage."""
        from cw.models import read_park_comment_marker

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree, stage=Stage.REVIEW)

        result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        marker = read_park_comment_marker(_read_context(worktree))
        assert marker is not None
        assert marker.stage is Stage.REVIEW

    def test_the_stop_hooks_own_context_write_keeps_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown keys round-trip through _write_cw_context_locked."""
        from cw.cli._hook_io import _write_cw_context_locked
        from cw.cli.stop_hook import _clear_agent_spawn_stamp
        from cw.models import read_park_comment_marker

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        _invoke_signal_park(worktree, monkeypatch)

        assert _write_cw_context_locked(str(worktree), _clear_agent_spawn_stamp)

        assert read_park_comment_marker(_read_context(worktree)) is not None

    def test_a_respawn_removes_an_earlier_legs_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn's wholesale context rewrite is the real staleness guard (D4)."""
        from cw.spawn import _write_hook_context

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        _invoke_signal_park(worktree, monkeypatch)
        assert PARK_COMMENT_MARKER_KEY in _read_context(worktree)

        _write_hook_context(
            worktree,
            session_id="sessB",
            session_name="client-a/impl",
            client="client-a",
            purpose="impl",
            ticket_id="940",
            origin=SessionOrigin.DAEMON,
        )

        assert PARK_COMMENT_MARKER_KEY not in _read_context(worktree)

    def test_signal_park_emits_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stamp is fallback evidence for one Stop decision, not an audit
        surface: no event, no durable row of its own (A17)."""
        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        before = len(read_events())

        result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        assert len(read_events()) == before


class TestSignalParkFailsOpen:
    """Every foreseeable failure exits 0, says why, and writes nothing."""

    def test_no_context_file_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()

        result = _invoke_signal_park(bare, monkeypatch)

        assert result.exit_code == 0
        assert (
            "park marker NOT recorded: no readable .claude/cw-context.json in the"
            " current directory" in result.output
        )
        assert not _context_path(bare).exists()

    def test_context_without_a_string_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _seeded_worktree(tmp_path)
        context = _read_context(worktree)
        context["session_id"] = 42
        _context_path(worktree).write_text(json.dumps(context), encoding="utf-8")
        before = _context_path(worktree).read_bytes()

        result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        assert (
            "park marker NOT recorded: cw-context.json carries no string session_id"
            " and ticket_id" in result.output
        )
        assert _context_path(worktree).read_bytes() == before

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(None, id="no-row"),
            pytest.param(QueueItemStatus.BLOCKED_ON_USER, id="not-running"),
        ],
    )
    def test_no_running_row(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: QueueItemStatus | None,
    ) -> None:
        worktree = _seeded_worktree(tmp_path)
        if status is None:
            save_dev_queue(DevQueueStore(tasks=[]))
        else:
            _seed_running_row(worktree, status=status)
        before = _context_path(worktree).read_bytes()

        result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        assert (
            "park marker NOT recorded: no RUNNING dev-queue row for this session"
            " (or the dev queue is unreadable)" in result.output
        )
        assert _context_path(worktree).read_bytes() == before

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{ not json", id="invalid-json"),
            pytest.param('{"schema_version": 1, "tasks": "nope"}', id="schema-invalid"),
            pytest.param("[]", id="top-level-list"),
        ],
    )
    def test_corrupt_dev_queue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        payload: str,
    ) -> None:
        """A corrupt queue is "no row": exit 0, one WARNING, nothing written."""
        from cw.config import dev_queue_file

        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        queue_path = dev_queue_file()
        queue_path.write_text(payload, encoding="utf-8")
        queue_bytes = queue_path.read_bytes()
        before = _context_path(worktree).read_bytes()

        with caplog.at_level("WARNING"):
            result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        assert (
            "park marker NOT recorded: no RUNNING dev-queue row for this session"
            " (or the dev queue is unreadable)" in result.output
        )
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "find_running_task_for_session" in warnings[0].getMessage()
        assert _context_path(worktree).read_bytes() == before
        assert queue_path.read_bytes() == queue_bytes

    def test_contended_context_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
        )
        worktree = _seeded_worktree(tmp_path)
        _seed_running_row(worktree)
        before = _context_path(worktree).read_bytes()

        with _hold_context_lock(worktree):
            result = _invoke_signal_park(worktree, monkeypatch)

        assert result.exit_code == 0
        assert (
            "park marker NOT recorded: could not write cw-context.json (locked,"
            " missing or malformed)" in result.output
        )
        assert _context_path(worktree).read_bytes() == before


class TestPostedAtIsAuditOnly:
    def test_posted_at_is_never_compared_to_the_wall_clock(self) -> None:
        """posted_at is audit-only (ARCHITECTURE.md §7.13): no age, no expiry,
        no threshold. Its one non-audit use is as an ordering pivot, which can
        only ever SUPPRESS a park. Only the writer reads a clock."""
        import inspect

        import cw.models.park_comment_marker

        import cw.cli._sentinels
        import cw.cli.stop_hook

        sources = [
            inspect.getsource(cw.models.park_comment_marker),
            inspect.getsource(cw.cli.stop_hook._park_if_abandoned),
            inspect.getsource(cw.cli.stop_hook._sentinel_frame_follows_marker),
            inspect.getsource(cw.cli._sentinels._sentinel_frame_after),
            inspect.getsource(cw.cli._sentinels._at_or_after),
        ]
        forbidden = (
            "now(",
            "utcnow",
            "time.time",
            "monotonic",
            "timedelta",
            "total_seconds",
        )
        for source in sources:
            for token in forbidden:
                assert token not in source
