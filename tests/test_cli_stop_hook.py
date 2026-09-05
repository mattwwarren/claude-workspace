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

from cw.config import load_state, save_state
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    CwState,
    SessionStatus,
)
from cw.reconcile._shared import SentinelRouteOutcome
from tests._reconcile_helpers import _stage_complete_payload
from tests.conftest import (
    _invoke_hook_command,
    _make_daemon_session,
    _write_hook_context_file,
    _write_stop_hook_transcript,
)

if TYPE_CHECKING:
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

    Every Stop with empty ``background_tasks`` reaches the clear write, even
    when there was nothing to retire -- this asserts the log only fires on
    an actual nonzero-to-zero transition, not on every clear invocation.
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
        lambda *args, **kwargs: SentinelRouteOutcome(
            rescued=False,
            routed=False,
            landed_terminal=False,
            task_already_terminal=True,
        ),
    )

    resolution = _resolve_and_complete_headless_session(
        state,
        session,
        cwd_value=str(worktree),
        claude_session_id=claude_session_id,
        ticket_id_value="1692-mut",
        is_headless=True,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert resolution.rescued is False
    assert resolution.task_already_terminal is True
    assert session.status == SessionStatus.COMPLETED
