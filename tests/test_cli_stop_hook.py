"""Tests for the #1947 ``cw signal-stop`` <-> ``agent_spawn_stamp`` wiring.

#1646's ``PostToolUse:Agent`` decrement fired at *launch-return* (the
``Async agent launched successfully.`` tool_result), not at subagent
completion -- confirmed by replaying the ``ea2f3d42``/#1902 transcript
(``13:12:05.585Z`` Agent tool_use -> ``13:12:09.513Z`` PostToolUse:Agent ->
``13:12:13.028Z`` turn_duration still reporting
``pendingBackgroundAgentCount: 1``). The stamp balanced back to 0 while the
harness's own turn accounting still considered the subagent pending, so the
phantom sweep's ``unresolved_subagent_spawn`` signal was silently hollow for
every async ``Agent(isolation="worktree")`` spawn.

This file covers the #1947 replacement: ``cw signal-stop`` (not
``PostToolUse:Agent``, which is removed -- see ``tests/test_spawn.py``) now
owns the ``agent_spawn_stamp`` write, driven off the hook payload's own
``background_tasks`` list, which the harness populates from its own
turn-accounting (the same field ``pendingBackgroundAgentCount`` values fed
into the replay). A turn ending with pending background work snapshots the
live count; a turn ending with none clears it. Both writes share
``cw.cli._hook_io._write_cw_context_locked`` with the pre-existing
``agent-spawn-pre`` writer (tested in ``tests/test_cli_agent_spawn_stamp.py``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.config import load_state, save_state
from cw.events import read_events
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    CwState,
    OrchestratorEventType,
    SessionStatus,
)
from cw.reconcile._shared import SentinelRouteOutcome
from tests._reconcile_helpers import _stage_complete_payload
from tests.conftest import (
    _STAMP_ABSENT,
    _invoke_hook_command,
    _make_daemon_session,
    _write_hook_context_file,
    _write_stop_hook_transcript,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import Session


def _seed_session(tmp_path: Path, sess_id: str = "sess940g") -> Session:
    """Seed a DAEMON session whose id matches ``_write_hook_context_file``'s.

    ``_write_hook_context_file`` hardcodes ``session_id="sess940g"`` (#1646) --
    matching the id here is what lets the whole-object ``state.sessions``
    snapshot assertions below prove the deferral/clear paths never touch
    session state, mirroring ``TestSignalStop._seed_session`` in
    ``tests/test_cli.py`` (not reused directly: that helper is a private
    method of a class in a different module).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    session = _make_daemon_session(
        id=sess_id,
        name="client-a/impl",
        client="client-a",
        workspace_path=workspace,
        worktree_path=worktree,
        surface_ref=None,
        started_at=datetime.now(UTC),
    )
    state = load_state()
    state.sessions.append(session)
    save_state(state)
    return session


def _read_stamp(worktree: Path) -> dict[str, object]:
    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    stamp = context[AGENT_SPAWN_STAMP_KEY]
    assert isinstance(stamp, dict)
    return stamp


def test_signal_stop_defer_snapshots_bg_task_count_to_agent_spawn_stamp(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The deferral branch snapshots ``len(background_tasks)`` into the stamp.

    #1947: this is the replacement evidence source for the removed
    ``PostToolUse:Agent`` decrement, which the ``ea2f3d42`` replay showed
    balances to 0 at launch-return rather than subagent completion. The
    Stop hook's own ``background_tasks`` list reflects the harness's live
    turn-accounting (the same field the replay's
    ``pendingBackgroundAgentCount`` came from), so it survives past
    launch-return for as long as the subagent is genuinely still running.
    """
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    worktree = session.worktree_path
    _write_hook_context_file(worktree, workspace_path=session.workspace_path)

    # Snapshot session state pre-call -- the deferral path must leave it
    # byte-for-byte unchanged, matching
    # test_signal_stop_defers_when_background_tasks_pending's pattern
    # (tests/test_cli.py:665-731).
    pre_state = load_state()
    pre_target = next(s for s in pre_state.sessions if s.id == session.id)
    pre_snapshot = pre_target.model_dump()

    hook_stdin = {
        "session_id": "claude-uuid-bg",
        "cwd": str(worktree),
        "hook_event_name": "Stop",
        "background_tasks": [
            {"id": "task-1", "description": "Fix-loop subagent running"},
            {"id": "task-2", "description": "Second parallel subagent running"},
        ],
    }
    result = _invoke_hook_command("signal-stop", hook_stdin)
    assert result.exit_code == 0, result.output

    post_state = load_state()
    post_target = next(s for s in post_state.sessions if s.id == session.id)
    assert post_target.model_dump() == pre_snapshot

    stamp = _read_stamp(worktree)
    assert stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 2
    assert stamp[AGENT_SPAWN_LAST_STAMPED_AT_KEY] is not None


def test_signal_stop_clears_agent_spawn_stamp_when_bg_tasks_drain(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A Stop with no pending background_tasks clears a stale nonzero stamp.

    Builds the pre-existing nonzero count by driving a real
    ``agent-spawn-pre`` invocation (not a hand-written stamp value), then
    fires ``signal-stop`` with ``background_tasks`` empty and asserts the
    clear-to-zero write happens on the very next Stop -- this is the fast
    path that reaches past the ``background_tasks`` check even when no
    session in ``state.sessions`` matches (session lookup happens later);
    the write must not depend on that lookup succeeding.
    """
    worktree = tmp_path / "wt-clear"
    worktree.mkdir()
    _write_hook_context_file(worktree)

    pre_result = _invoke_hook_command(
        "agent-spawn-pre",
        {"cwd": str(worktree), "hook_event_name": "PreToolUse", "tool_name": "Agent"},
    )
    assert pre_result.exit_code == 0
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 1

    hook_stdin = {
        "session_id": "claude-uuid-drain",
        "cwd": str(worktree),
        "hook_event_name": "Stop",
    }
    result = _invoke_hook_command("signal-stop", hook_stdin)
    assert result.exit_code == 0, result.output

    stamp = _read_stamp(worktree)
    assert stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
    assert stamp[AGENT_SPAWN_LAST_STAMPED_AT_KEY] is not None


def test_signal_stop_clear_logs_when_retiring_a_nonzero_stamp(
    tmp_config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Clearing a nonzero stamp leaves an audit trail (#1947 review finding).

    The clear write is otherwise silent (same fail-open contract as every
    hook write here), but ``agent_spawn_stamp`` is the sole disk evidence
    gating BLOCKED_ON_USER vs a PENDING revert on the phantom sweep, so a
    transition worth an operator's attention gets a log line.
    """
    worktree = tmp_path / "wt-clear-logged"
    worktree.mkdir()
    _write_hook_context_file(worktree)

    pre_result = _invoke_hook_command(
        "agent-spawn-pre",
        {"cwd": str(worktree), "hook_event_name": "PreToolUse", "tool_name": "Agent"},
    )
    assert pre_result.exit_code == 0
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 1

    with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
        result = _invoke_hook_command(
            "signal-stop",
            {
                "session_id": "claude-uuid-drain-logged",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            },
        )
    assert result.exit_code == 0, result.output
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
    assert any(
        "agent_spawn_stamp cleared" in record.message for record in caplog.records
    )


def test_signal_stop_clear_does_not_log_when_stamp_already_zero(
    tmp_config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No spurious log line on the ordinary, already-resolved fast path.

    A Stop with empty ``background_tasks`` whose stamp is already the resolved
    ``{0, ...}`` shape skips the clear write entirely (#2229); a stamp in any
    other shape still takes it. This asserts the log only fires on an actual
    nonzero-to-zero transition, never merely because a Stop ran.
    """
    worktree = tmp_path / "wt-clear-quiet"
    worktree.mkdir()
    _write_hook_context_file(worktree)
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0

    with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
        result = _invoke_hook_command(
            "signal-stop",
            {
                "session_id": "claude-uuid-drain-quiet",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            },
        )
    assert result.exit_code == 0, result.output
    assert not any(
        "agent_spawn_stamp cleared" in record.message for record in caplog.records
    )


def test_signal_stop_stamp_write_fails_open_on_lock_contention(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held per-worktree lock must not crash or block signal-stop.

    Mirrors ``test_pre_hook_fails_open_on_lock_exhaustion``
    (``tests/test_cli_agent_spawn_stamp.py``): the stamp write shares the same
    ``_write_cw_context_locked`` primitive and the same bounded, fail-open
    lock contract. Exercised on the deferral (snapshot) branch, which is the
    new write path #1947 adds -- the clear-write shares the same helper so
    is not separately re-tested for lock exhaustion.
    """
    import fcntl

    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )

    worktree = tmp_path / "wt-locked"
    worktree.mkdir()
    _write_hook_context_file(worktree)
    lock_path = worktree / ".claude" / "cw-context.json.lock"

    hook_stdin = {
        "session_id": "claude-uuid-locked",
        "cwd": str(worktree),
        "hook_event_name": "Stop",
        "background_tasks": [{"id": "task-1", "description": "still running"}],
    }

    with lock_path.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = _invoke_hook_command("signal-stop", hook_stdin)

    assert result.exit_code == 0, result.output
    # The write never landed -- stamp stays at its seeded value (0).
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0


def test_resolve_and_complete_headless_session_completes_on_task_already_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation-proof for #1692: force ``_apply_sentinel_to_task`` to return
    ``routed=False, task_already_terminal=True`` for a valid sentinel and
    assert the session still completes.

    This is the literal "force ``_apply_sentinel_to_task`` to return
    ``routed=False``" mutation the ticket asks for -- RED before the fix
    (``_HeadlessResolution.task_already_terminal`` does not exist and the
    bail returns ``rescued=None`` without completing the session), GREEN
    after. Calls ``_resolve_and_complete_headless_session`` directly rather
    than going through the ``signal-stop`` CLI entrypoint, isolating the
    fix to its exact seam.
    """
    from cw.cli.stop_hook import _resolve_and_complete_headless_session

    home = tmp_path / "fake-home-1692-mut"
    worktree = tmp_path / "worktree-1692-mut"
    worktree.mkdir(parents=True)

    session = _make_daemon_session(
        id="sess-1692-mut",
        worktree_path=worktree,
        surface_ref="sfref-1692-mut",
    )
    state = CwState(sessions=[session])

    claude_session_id = "sfref-1692-mut-uuid"
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1692-mut"
    sentinel_text = (
        "<<<AUTO_DEV_RESULT\n" + json.dumps(payload) + "\nAUTO_DEV_RESULT>>>"
    )
    _write_stop_hook_transcript(home, worktree, claude_session_id, sentinel_text)
    monkeypatch.setattr("cw._util.Path.home", lambda: home)

    monkeypatch.setattr(
        "cw.cli.stop_hook._apply_sentinel_to_task",
        lambda *_args, **_kwargs: SentinelRouteOutcome(
            rescued=False,
            routed=False,
            landed_terminal=False,
            task_already_terminal=True,
        ),
    )

    resolution = _resolve_and_complete_headless_session(
        state,
        session,
        context={},
        cwd_value=str(worktree),
        claude_session_id=claude_session_id,
        ticket_id_value="1692-mut",
        is_headless=True,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert resolution.rescued is False
    assert resolution.task_already_terminal is True
    assert session.status == SessionStatus.COMPLETED


def _context_snapshot(worktree: Path) -> tuple[bytes, int, int]:
    """Return ``(bytes, inode, mtime_ns)`` of the worktree's context file.

    ``atomic_write_text`` renames a temp file into place, so any write changes
    the inode -- this observes "the hook wrote nothing" without mocking.
    """
    path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def _lock_path(worktree: Path) -> Path:
    """The per-worktree lock ``_context_lock`` opens (with ``"w"``) on entry."""
    return worktree / (str(HOOK_CONTEXT_RELATIVE_PATH) + ".lock")


def _stop_payload(worktree: Path, **extra: object) -> dict[str, object]:
    return {
        "session_id": "claude-uuid-2229",
        "cwd": str(worktree),
        "hook_event_name": "Stop",
        **extra,
    }


def test_signal_stop_cheap_exit_on_clear_stamp_touches_nothing_and_completes(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#2229: a Stop whose stamp is already ``{0, None}`` takes no lock and
    rewrites nothing -- and still runs the completion path behind it.

    ADR-0003: "the Stop path still completes". The skip sits in front of the
    session lookup, so a session that is ACTIVE must still land COMPLETED and
    emit exactly one ``SESSION_COMPLETED``; a skip that short-circuited the
    function would show up here as a session left ACTIVE.
    """
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    worktree = session.worktree_path
    _write_hook_context_file(worktree, workspace_path=session.workspace_path)
    before = _context_snapshot(worktree)
    assert not _lock_path(worktree).exists()

    result = _invoke_hook_command("signal-stop", _stop_payload(worktree))

    assert result.exit_code == 0, result.output
    assert _context_snapshot(worktree) == before
    assert not _lock_path(worktree).exists()
    updated = next(s for s in load_state().sessions if s.id == session.id)
    assert updated.status == SessionStatus.COMPLETED
    events = read_events(
        consumer="test-2229-cheap-exit",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert [e.payload.get("session_id") for e in events] == [session.id]


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param(_STAMP_ABSENT, id="absent-key"),
        pytest.param([], id="non-dict-list"),
        pytest.param({}, id="dict-without-count"),
        pytest.param({AGENT_SPAWN_UNRESOLVED_COUNT_KEY: "0"}, id="str-count"),
        pytest.param({AGENT_SPAWN_UNRESOLVED_COUNT_KEY: False}, id="bool-count"),
        pytest.param({AGENT_SPAWN_UNRESOLVED_COUNT_KEY: -1}, id="negative-count"),
        pytest.param({AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 2}, id="positive-count"),
    ],
)
def test_signal_stop_still_writes_when_stamp_is_not_the_resolved_shape(
    tmp_config_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    stamp: object,
) -> None:
    """#2229: only an exact ``{unresolved_count: 0}`` dict skips the clear.

    Every other shape -- absent, non-dict, missing/str/bool/negative count, or
    a live nonzero count -- still takes the locked clear and is normalized to
    ``{0, <timestamp>}``. Pins the edge cases so the skip cannot over-reach.
    """
    worktree = tmp_path / "wt-shape"
    worktree.mkdir()
    _write_hook_context_file(worktree, stamp=stamp)

    with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
        result = _invoke_hook_command("signal-stop", _stop_payload(worktree))

    assert result.exit_code == 0, result.output
    normalized = _read_stamp(worktree)
    assert normalized[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
    assert isinstance(normalized[AGENT_SPAWN_LAST_STAMPED_AT_KEY], str)
    logged = [r.getMessage() for r in caplog.records]
    if stamp == {AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 2}:
        assert any("unresolved_count 2 -> 0" in line for line in logged)


def test_signal_stop_clear_reads_fresh_under_lock_after_concurrent_increment(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2229: the write arm re-reads the file under the lock, never the
    pre-lock ``context``.

    Simulates an ``agent-spawn-pre`` landing between Stop's unlocked read and
    its locked read-modify-write by bumping the count to 2 just before the
    real writer runs. The clear must observe that 2 (log ``2 -> 0``); reusing
    the stale pre-lock context would be a lost-update bug. This is the guard
    against ever memoizing the read-modify-write's read.
    """
    from cw.cli import stop_hook

    worktree = tmp_path / "wt-fresh"
    worktree.mkdir()
    _write_hook_context_file(worktree, stamp={AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 1})
    real_write = stop_hook._write_cw_context_locked
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH

    def _bump_then_write(
        cwd_value: str, mutate_fn: Callable[[dict[str, object]], dict[str, object]]
    ) -> bool:
        raw = json.loads(context_path.read_text(encoding="utf-8"))
        raw[AGENT_SPAWN_STAMP_KEY] = {AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 2}
        context_path.write_text(json.dumps(raw), encoding="utf-8")
        return real_write(cwd_value, mutate_fn)

    monkeypatch.setattr(stop_hook, "_write_cw_context_locked", _bump_then_write)

    with caplog.at_level("INFO", logger="cw.cli.stop_hook"):
        result = _invoke_hook_command("signal-stop", _stop_payload(worktree))

    assert result.exit_code == 0, result.output
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
    assert any("unresolved_count 2 -> 0" in r.getMessage() for r in caplog.records)


def test_signal_stop_skip_arm_does_not_clobber_a_concurrent_increment(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2229: the skip arm writes nothing, so it cannot lose an increment.

    The unlocked pre-lock read is stale (it saw ``{0}``) while an
    ``agent-spawn-pre`` had already raised the count to 1 on disk. Skipping is
    linearizable -- "this Stop cleared first, then the spawn incremented" --
    so the file must be left exactly as it is: same bytes, same inode, no lock
    taken.
    """
    from cw.cli import stop_hook

    worktree = tmp_path / "wt-stale-zero"
    worktree.mkdir()
    _write_hook_context_file(
        worktree,
        stamp={
            AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 1,
            AGENT_SPAWN_LAST_STAMPED_AT_KEY: "2026-01-01T00:00:00+00:00",
        },
    )
    on_disk = _context_snapshot(worktree)
    stale = json.loads(on_disk[0])
    stale[AGENT_SPAWN_STAMP_KEY] = {
        AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 0,
        AGENT_SPAWN_LAST_STAMPED_AT_KEY: None,
    }
    monkeypatch.setattr(stop_hook, "_read_cw_context", lambda _cwd: stale)

    result = _invoke_hook_command("signal-stop", _stop_payload(worktree))

    assert result.exit_code == 0, result.output
    assert _read_stamp(worktree)[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 1
    assert _context_snapshot(worktree) == on_disk
    assert not _lock_path(worktree).exists()


def test_signal_stop_deferral_touches_no_session_state_and_refreshes_stamp(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0003 line 3 (#2229): a deferral turn does no session-state I/O, runs
    before the idempotency check, and keeps refreshing ``last_stamped_at``.

    The session is already COMPLETED -- were idempotency checked first, the
    hook would return before writing the snapshot. ``load_state`` and
    ``sessions_lock`` are replaced by stubs that raise, so any state I/O on the
    deferral path fails the exit-code assertion. Two turns with the same
    ``background_tasks`` at different times pin that the unchanged-count
    snapshot is deliberately NOT skipped: ``last_stamped_at`` bounds the #2012
    distress-suppression deadline while the count is above zero.
    """
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    worktree = session.worktree_path
    state = load_state()
    next(
        s for s in state.sessions if s.id == session.id
    ).status = SessionStatus.COMPLETED
    save_state(state)
    _write_hook_context_file(worktree, workspace_path=session.workspace_path)
    before = next(s for s in load_state().sessions if s.id == session.id).model_dump()

    def _no_state_io(*_args: object, **_kwargs: object) -> object:
        msg = "the deferral path must not touch session state"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.cli.stop_hook.load_state", _no_state_io)
    monkeypatch.setattr("cw.cli.stop_hook.sessions_lock", _no_state_io)
    tasks = [{"id": "task-1", "description": "still running"}]
    stamps = []
    for frozen in ("2026-03-01T12:00:00+00:00", "2026-03-01T12:00:05+00:00"):
        with freeze_time(frozen):
            result = _invoke_hook_command(
                "signal-stop", _stop_payload(worktree, background_tasks=tasks)
            )
        assert result.exit_code == 0, result.output
        stamps.append(_read_stamp(worktree))

    assert [s[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] for s in stamps] == [1, 1]
    assert [s[AGENT_SPAWN_LAST_STAMPED_AT_KEY] for s in stamps] == [
        "2026-03-01T12:00:00+00:00",
        "2026-03-01T12:00:05+00:00",
    ]
    after = next(s for s in load_state().sessions if s.id == session.id).model_dump()
    assert after == before


@pytest.mark.parametrize(
    "terminal",
    [SessionStatus.COMPLETED, SessionStatus.IDLE, SessionStatus.TIMED_OUT],
)
def test_signal_stop_terminal_session_is_noop_and_leaves_clear_stamp_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: SessionStatus,
) -> None:
    """ADR-0003 line 2 (#2229): COMPLETED, IDLE and TIMED_OUT sessions are no-ops.

    A clear-stamp context is left byte-identical and same-inode (the file
    assertion is what the old unconditional clear write violated); session
    state is unchanged, no ``SESSION_COMPLETED`` is emitted, and the native
    daemon is never touched. TIMED_OUT had no test before.
    """
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    worktree = session.worktree_path
    state = load_state()
    next(s for s in state.sessions if s.id == session.id).status = terminal
    save_state(state)
    _write_hook_context_file(worktree, workspace_path=session.workspace_path)
    ctx_before = _context_snapshot(worktree)
    sess_before = next(s for s in load_state().sessions if s.id == session.id)

    def _no_daemon() -> object:
        msg = "a terminal session must never reach the native daemon"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.cli.stop_hook.get_native_daemon_client", _no_daemon)

    result = _invoke_hook_command("signal-stop", _stop_payload(worktree))

    assert result.exit_code == 0, result.output
    sess_after = next(s for s in load_state().sessions if s.id == session.id)
    assert sess_after.model_dump() == sess_before.model_dump()
    assert _context_snapshot(worktree) == ctx_before
    events = read_events(
        consumer="test-2229-terminal-noop",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == session.id for e in events)


@pytest.mark.parametrize(
    ("nested_cwd", "expected_scans"),
    [
        pytest.param(False, 1, id="cwd-equals-worktree-path"),
        pytest.param(True, 2, id="cwd-differs-799-fallback"),
    ],
)
def test_parse_headless_sentinel_scans_transcript_once_when_cwd_is_worktree_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested_cwd: bool,
    expected_scans: int,
) -> None:
    """#2229: the #799 ``worktree_path`` fallback rescans only a different dir.

    When the hook ``cwd`` equals ``session.worktree_path`` the fallback would
    re-read a byte-identical transcript, so it is skipped. When they differ
    (EnterWorktree shifted the cwd) the fallback still runs -- pinned end to
    end by ``tests/test_cli.py`` ``..._finds_sentinel_via_worktree_path_
    fallback``. No transcript exists on disk, so every scan returns ``None``.
    """
    from cw.cli import stop_hook

    worktree = tmp_path / "wt-scan"
    session = _make_daemon_session(worktree_path=worktree)
    cwd_value = str(worktree / "nested") if nested_cwd else str(worktree)
    monkeypatch.setattr("cw._util.Path.home", lambda: tmp_path / "home")
    scanned: list[str] = []
    real_scan = stop_hook._parse_sentinel_from_transcript

    def _counting_scan(cwd: str, claude_session_id: str | None) -> object:
        scanned.append(cwd)
        return real_scan(cwd, claude_session_id)

    monkeypatch.setattr(stop_hook, "_parse_sentinel_from_transcript", _counting_scan)

    parsed = stop_hook._parse_headless_sentinel(session, cwd_value, "uuid-2229")

    assert parsed is None
    assert len(scanned) == expected_scans
    assert scanned[0] == cwd_value
    if nested_cwd:
        assert scanned[1] == str(worktree)
