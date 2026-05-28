"""Tests for cw.wrapper - Claude wrapper and IDLE signaling."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import events_dir, load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.history import EventType, load_history
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.wrapper import (
    _detect_claude_session_id,
    _idle_signal_path,
    _is_headless,
    _is_paused_for_user_input,
    _run_claude_streaming,
    run_claude_wrapper,
    signal_completed,
    signal_idle,
    signal_needs_attention,
)


def _make_result(
    *,
    status: str = "shipped",
    ticket_id: str = "T-1",
    next_actions: list[str] | None = None,
) -> AutoDevResult:
    """Build a minimal AutoDevResult that satisfies §3-§5 invariants."""
    _base_health: dict[str, Any] = {
        "lowest_agent_confidence": "HIGH",
        "any_incomplete_risk": False,
        "recommendation": "PROCEED",
    }
    _base_review: dict[str, Any] = {
        "must_fix_initial": 0,
        "should_fix": 0,
        "fix_cycles_used": 0,
    }

    # Statuses that exit before branch creation
    pre_branch = status in {
        "no_op",
        "plan_pending_approval",
        "scope_exceeded",
        "forbidden_area",
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    }

    payload: dict[str, Any] = {
        "schema_version": 2,
        "ticket_id": ticket_id,
        "status": status,
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "commits": [],
        "pr": None,
        "branch": None,
        "review": _base_review,
        "health": _base_health,
        "next_actions": next_actions if next_actions is not None else [],
    }

    if pre_branch:
        payload["stage_reached"] = "stage1_plan"
        payload["scope"]["lines_actual"] = None
    else:
        payload["stage_reached"] = "stage4b_pr_create"
        payload["scope"]["lines_actual"] = 8
        payload["branch"] = f"auto-dev/{ticket_id}"
        payload["fork_point_sha"] = "abc123"
        payload["commits"] = ["c1"]

    if status == "shipped":
        payload["pr"] = {
            "number": 42,
            "url": "https://example.com/pr/42",
            "auto_merge": True,
            "base": "main",
        }
        payload["next_actions"] = (
            next_actions if next_actions is not None else ["wait_for_ci"]
        )

    if status == "ambiguities_pending_resolution":
        payload["ambiguities"] = [{"question": "Which approach?"}]
        payload["next_actions"] = (
            next_actions if next_actions is not None else ["user_resolve_ambiguities"]
        )

    if status == "premises_pending_verification":
        payload["premises"] = [{"premise": "API endpoint exists"}]
        payload["next_actions"] = (
            next_actions if next_actions is not None else ["user_verify_premises"]
        )

    return AutoDevResult.model_validate(payload)


def _sentinel_stdout(result: AutoDevResult, *, prefix: str = "log line\n") -> str:
    """Wrap a result payload in the AUTO_DEV_RESULT sentinel block."""
    body = result.model_dump_json()
    return f"{prefix}<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"


class TestIdleSignalPath:
    def test_format(self) -> None:
        path = _idle_signal_path("my-client", "impl")
        assert path == events_dir() / "my-client__impl.idle"

    def test_different_purposes(self) -> None:
        assert _idle_signal_path("c", "impl") != _idle_signal_path("c", "debt")


class TestSignalIdle:
    def test_transitions_active_to_idle(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """Active session transitions to IDLE with idle_at set."""
        state = CwState(
            sessions=[
                Session(
                    id="s1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle("c", "impl", exit_code=0)

        updated = load_state()
        session = updated.sessions[0]
        assert session.status == SessionStatus.IDLE
        assert session.idle_at is not None

    def test_writes_signal_file(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """Signal file is written with correct payload."""
        state = CwState(
            sessions=[
                Session(
                    id="sig1",
                    name="c/debt",
                    client="c",
                    purpose=SessionPurpose.DEBT,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle("c", "debt", exit_code=42)

        signal_file = _idle_signal_path("c", "debt")
        assert signal_file.exists()
        payload = json.loads(signal_file.read_text())
        assert payload["session_id"] == "sig1"
        assert payload["client"] == "c"
        assert payload["purpose"] == "debt"
        assert payload["exit_code"] == 42

    def test_records_history_event(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """SESSION_IDLED event is recorded in history."""
        state = CwState(
            sessions=[
                Session(
                    id="h1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle("c", "impl", exit_code=0)

        events = load_history("c")
        assert len(events) >= 1
        idled_events = [e for e in events if e.event_type == EventType.SESSION_IDLED]
        assert len(idled_events) == 1
        assert idled_events[0].session_id == "h1"
        assert idled_events[0].metadata["exit_code"] == "0"

    def test_no_session_found_is_noop(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_idle does nothing if session doesn't exist."""
        state = CwState(sessions=[])
        save_state(state)

        # Should not raise
        signal_idle("nonexistent", "impl", exit_code=0)

    def test_skips_non_active_session(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_idle does nothing if session is not ACTIVE."""
        state = CwState(
            sessions=[
                Session(
                    id="bg1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle("c", "impl", exit_code=0)

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_stores_claude_session_id(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_idle stores claude_session_id on session and in payload."""
        state = CwState(
            sessions=[
                Session(
                    id="csid1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle(
            "c",
            "impl",
            exit_code=0,
            claude_session_id="550e8400-e29b-41d4-a716-446655440000",
        )

        updated = load_state()
        assert updated.sessions[0].claude_session_id == (
            "550e8400-e29b-41d4-a716-446655440000"
        )
        signal_file = _idle_signal_path("c", "impl")
        payload = json.loads(signal_file.read_text())
        assert payload["claude_session_id"] == ("550e8400-e29b-41d4-a716-446655440000")

    def test_no_claude_session_id_omits_from_payload(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_idle without claude_session_id omits it from payload."""
        state = CwState(
            sessions=[
                Session(
                    id="noid1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        signal_idle("c", "impl", exit_code=0)

        signal_file = _idle_signal_path("c", "impl")
        payload = json.loads(signal_file.read_text())
        assert "claude_session_id" not in payload


class TestRunClaudeWrapper:
    def test_no_env_runs_claude_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without CW_CLIENT/CW_PURPOSE, runs claude and exits."""
        monkeypatch.delenv("CW_CLIENT", raising=False)
        monkeypatch.delenv("CW_PURPOSE", raising=False)

        with patch("cw.wrapper.subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"returncode": 0})()
            with pytest.raises(SystemExit, match="0"):
                run_claude_wrapper(("--resume",))

        mock_run.assert_called_once_with(["claude", "--resume"], check=False)

    def test_with_env_signals_idle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """With CW_CLIENT/CW_PURPOSE set, signals IDLE after Claude exits."""
        monkeypatch.setenv("CW_CLIENT", "test")
        monkeypatch.setenv("CW_PURPOSE", "impl")

        state = CwState(
            sessions=[
                Session(
                    id="w1",
                    name="test/impl",
                    client="test",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        save_state(state)

        with patch("cw.wrapper.subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"returncode": 0})()
            run_claude_wrapper(("--resume",))

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.IDLE


class TestDetectClaudeSessionId:
    def test_finds_most_recent_session(self, tmp_path: Path) -> None:
        """Detects UUID from most recently modified .jsonl file."""
        workspace = str(tmp_path / "workspace")
        encoded = workspace.replace("/", "-")
        project_dir = tmp_path / "home" / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)

        older = project_dir / "aaaa-bbbb-cccc.jsonl"
        older.write_text("{}")
        # Ensure different mtime
        time.sleep(0.05)
        newer = project_dir / "1111-2222-3333.jsonl"
        newer.write_text("{}")

        with patch("cw.wrapper.Path.home", return_value=tmp_path / "home"):
            result = _detect_claude_session_id(workspace)

        assert result == "1111-2222-3333"

    def test_returns_none_for_missing_dir(self, tmp_path: Path) -> None:
        """Returns None when project dir doesn't exist."""
        with patch("cw.wrapper.Path.home", return_value=tmp_path / "home"):
            result = _detect_claude_session_id("/nonexistent/path")

        assert result is None

    def test_returns_none_for_empty_dir(self, tmp_path: Path) -> None:
        """Returns None when project dir has no .jsonl files."""
        workspace = str(tmp_path / "workspace")
        encoded = workspace.replace("/", "-")
        project_dir = tmp_path / "home" / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)

        with patch("cw.wrapper.Path.home", return_value=tmp_path / "home"):
            result = _detect_claude_session_id(workspace)

        assert result is None


class TestIsHeadless:
    def test_print_flag_long(self) -> None:
        assert _is_headless(("--print", "hi"))

    def test_print_flag_short(self) -> None:
        assert _is_headless(("-p", "hi"))

    def test_no_print_flag(self) -> None:
        assert not _is_headless(("--resume",))

    def test_empty_args(self) -> None:
        assert not _is_headless(())


class TestSignalCompleted:
    def _seed_active_session(self, sid: str = "w1") -> Session:
        sess = Session(
            id=sid,
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))
        return sess

    def test_flips_active_to_completed(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_completed transitions ACTIVE → COMPLETED with NORMAL reason."""
        self._seed_active_session("w1")
        result = _make_result(status="shipped", ticket_id="T-1")

        signal_completed("c", "impl", result=result, session_id="w1")

        updated = load_state()
        sess = updated.sessions[0]
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL
        assert sess.completed_at is not None
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"
        assert sess.last_result["ticket_id"] == "T-1"

    def test_emits_session_completed_event(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_completed records SESSION_COMPLETED with crashed=False and status."""
        self._seed_active_session("w2")
        result = _make_result(status="no_op", ticket_id="T-9")

        signal_completed("c", "impl", result=result, session_id="w2")

        events = read_events(event_types=[OrchestratorEventType.SESSION_COMPLETED])
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["session_id"] == "w2"
        assert ev.payload["client"] == "c"
        assert ev.payload["crashed"] is False
        assert ev.payload["status"] == "no_op"
        assert ev.payload["ticket_id"] == "T-9"

    def test_idempotent_when_already_completed(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_completed no-ops when session already COMPLETED."""
        sess = Session(
            id="done1",
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.COMPLETED,
            completed_reason=CompletionReason.CRASHED,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))
        result = _make_result(status="shipped", ticket_id="T-1")

        signal_completed("c", "impl", result=result, session_id="done1")

        updated = load_state()
        # Original completion reason preserved (no overwrite from idempotency path).
        assert updated.sessions[0].completed_reason == CompletionReason.CRASHED
        assert updated.sessions[0].last_result is None
        # No event emitted.
        events = read_events(event_types=[OrchestratorEventType.SESSION_COMPLETED])
        assert events == []

    def test_no_session_is_noop(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_completed silently no-ops when no session exists."""
        save_state(CwState(sessions=[]))
        result = _make_result(status="shipped", ticket_id="T-1")
        # Should not raise.
        signal_completed("c", "impl", result=result, session_id="missing")

    def test_session_id_lookup_disambiguates(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """When multiple impl sessions for one client exist, CW_SESSION_ID picks one."""
        # Two ACTIVE impl sessions for same client (simulating parallel dispatch).
        a = Session(
            id="aaa",
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        b = Session(
            id="bbb",
            name="c/auto-dev/T-2",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[a, b]))
        result = _make_result(status="shipped", ticket_id="T-2")

        signal_completed("c", "impl", result=result, session_id="bbb")

        updated = load_state()
        sess_a = next(s for s in updated.sessions if s.id == "aaa")
        sess_b = next(s for s in updated.sessions if s.id == "bbb")
        assert sess_a.status == SessionStatus.ACTIVE
        assert sess_b.status == SessionStatus.COMPLETED


class TestHeadlessWrapper:
    def test_print_with_sentinel_signals_completed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """--print + AUTO_DEV_RESULT in stdout → SESSION_COMPLETED, not IDLED."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "h1")

        sess = Session(
            id="h1",
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))

        result = _make_result(status="shipped", ticket_id="T-1")
        captured = _sentinel_stdout(result)

        with patch(
            "cw.wrapper._run_claude_streaming",
            return_value=(0, captured),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-1 --headless"))

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED
        assert updated.sessions[0].completed_reason == CompletionReason.NORMAL
        # No IDLE signal file written for the completed path.
        assert not _idle_signal_path("c", "impl").exists()

    def test_print_without_sentinel_routes_to_needs_attention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """--print but no sentinel block → signal_needs_attention (not idle).

        Headless exit code 0 with no AUTO_DEV_RESULT sentinel means the child
        self-backgrounded a subagent and exited early — operator must review.
        """
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "h2")

        sess = Session(
            id="h2",
            name="c/auto-dev/T-2",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))

        with (
            patch(
                "cw.wrapper._run_claude_streaming",
                return_value=(0, "just some output, no sentinel here\n"),
            ),
            patch("cw.wrapper.fire_push_notification"),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-2"))

        updated = load_state()
        # Needs-attention path: session is COMPLETED, not IDLE.
        assert updated.sessions[0].status == SessionStatus.COMPLETED
        assert updated.sessions[0].last_result == {
            "breadcrumbs": "just some output, no sentinel here",
            "needs_attention": True,
        }

    def test_print_nonzero_exit_skips_completed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """Nonzero exit code skips SESSION_COMPLETED even if sentinel is present.

        A worker that crashed mid-write may still have emitted a partial
        sentinel; we'd rather let reconcile's phantom-pane path handle it.
        """
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "h3")

        sess = Session(
            id="h3",
            name="c/auto-dev/T-3",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))

        result = _make_result(status="shipped", ticket_id="T-3")
        captured = _sentinel_stdout(result)

        with patch(
            "cw.wrapper._run_claude_streaming",
            return_value=(1, captured),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-3"))

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.IDLE

    def test_no_print_uses_subprocess_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """Interactive mode keeps subprocess.run + signal_idle, no streaming."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")

        sess = Session(
            id="i1",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))

        with (
            patch("cw.wrapper.subprocess.run") as mock_run,
            patch("cw.wrapper._run_claude_streaming") as mock_stream,
        ):
            mock_run.return_value = type("R", (), {"returncode": 0})()
            run_claude_wrapper(("--resume",))

        mock_stream.assert_not_called()
        mock_run.assert_called_once_with(["claude", "--resume"], check=False)
        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.IDLE


class _FakeStdout:
    """Drip-feed bytes chunk-by-chunk to mimic Popen.stdout."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakePopen:
    def __init__(self, chunks: list[bytes], returncode: int = 0) -> None:
        self.stdout = _FakeStdout(chunks)
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


class TestRunClaudeStreaming:
    def test_captures_stdout_up_to_cap(self) -> None:
        """Streaming run keeps only the tail when output exceeds the cap."""
        chunks = [b"A" * 50, b"TAIL_MARKER"]

        with (
            patch("cw.wrapper.subprocess.Popen", return_value=_FakePopen(chunks)),
            patch("cw.wrapper._write_passthrough"),  # silence stdout tee
        ):
            rc, captured = _run_claude_streaming([], max_capture_bytes=20)

        assert rc == 0
        # Tail-truncated buffer keeps the last 20 bytes, which must include the
        # marker emitted at the very end.
        assert "TAIL_MARKER" in captured
        assert len(captured) <= 20

    def test_captures_full_stdout_under_cap(self) -> None:
        """Output below the cap is captured in full."""
        chunks = [b"short output\n"]

        with (
            patch("cw.wrapper.subprocess.Popen", return_value=_FakePopen(chunks)),
            patch("cw.wrapper._write_passthrough"),
        ):
            rc, captured = _run_claude_streaming([], max_capture_bytes=1024)

        assert rc == 0
        assert captured == "short output\n"

    def test_propagates_returncode(self) -> None:
        """Non-zero returncode from claude is surfaced unchanged."""
        with (
            patch(
                "cw.wrapper.subprocess.Popen",
                return_value=_FakePopen([b""], returncode=7),
            ),
            patch("cw.wrapper._write_passthrough"),
        ):
            rc, captured = _run_claude_streaming([])

        assert rc == 7
        assert captured == ""


class TestIsPausedForUserInput:
    def test_ambiguities_pending_returns_true(self) -> None:
        result = _make_result(status="ambiguities_pending_resolution")
        assert _is_paused_for_user_input(result) is True

    def test_premises_pending_returns_true(self) -> None:
        result = _make_result(status="premises_pending_verification")
        assert _is_paused_for_user_input(result) is True

    def test_shipped_returns_false(self) -> None:
        result = _make_result(status="shipped")
        assert _is_paused_for_user_input(result) is False

    def test_no_op_returns_false(self) -> None:
        result = _make_result(status="no_op")
        assert _is_paused_for_user_input(result) is False

    def test_blocked_with_user_resolve_returns_true(self) -> None:
        """blocked + user_resolve_ next_action → paused for input.

        AutoDevResult currently rejects blocked + non-empty next_actions for
        non-pre-flight stages (forward-compatible code path). Use a mock to
        exercise the branch directly.
        """
        from unittest.mock import MagicMock

        result = MagicMock()
        result.status = "blocked"
        result.next_actions = ["user_resolve_ambiguities"]
        assert _is_paused_for_user_input(result) is True

    def test_blocked_with_user_decide_returns_true(self) -> None:
        """blocked + user_decide_ next_action → paused for input."""
        from unittest.mock import MagicMock

        result = MagicMock()
        result.status = "blocked"
        result.next_actions = ["user_decide_approach"]
        assert _is_paused_for_user_input(result) is True

    def test_blocked_with_user_verify_returns_true(self) -> None:
        """blocked + user_verify_ next_action → paused for input."""
        from unittest.mock import MagicMock

        result = MagicMock()
        result.status = "blocked"
        result.next_actions = ["user_verify_something"]
        assert _is_paused_for_user_input(result) is True

    def test_blocked_without_user_prefix_returns_false(self) -> None:
        """blocked with no user_ next_action → not paused for input."""
        payload: dict[str, Any] = {
            "schema_version": 2,
            "ticket_id": "T-Z",
            "status": "blocked",
            "stage_reached": "stage2_impl",
            "scope": {
                "tier": "small",
                "files": 1,
                "lines_estimate": 5,
                "lines_actual": 0,
                "forbidden_touched": False,
            },
            "plan_source": "generated",
            "branch": "auto-dev/T-Z",
            "commits": [],
            "pr": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "HIGH",
                "any_incomplete_risk": False,
                "recommendation": "PROCEED",
            },
            "next_actions": [],
            "blocker": {
                "stage": "stage2_impl",
                "reason": "review_blocked",
                "details": "fix loop exhausted",
            },
        }
        result = AutoDevResult.model_validate(payload)
        assert _is_paused_for_user_input(result) is False


class TestSignalNeedsAttention:
    def _seed_active_session(self, sid: str = "s1") -> Session:
        sess = Session(
            id=sid,
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))
        return sess

    def test_transitions_active_to_completed(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """signal_needs_attention transitions ACTIVE → COMPLETED."""
        self._seed_active_session("na1")
        with patch("cw.wrapper.fire_push_notification"):
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="some output",
                session_id="na1",
                claude_session_id=None,
            )
        updated = load_state()
        sess = updated.sessions[0]
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL
        assert sess.completed_at is not None

    def test_stores_breadcrumbs_in_last_result(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """Breadcrumbs stored in session.last_result."""
        self._seed_active_session("na2")
        with patch("cw.wrapper.fire_push_notification"):
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="line1\nline2",
                session_id="na2",
                claude_session_id=None,
            )
        updated = load_state()
        assert updated.sessions[0].last_result == {
            "breadcrumbs": "line1\nline2",
            "needs_attention": True,
        }

    def test_emits_session_needs_attention_event(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """SESSION_NEEDS_ATTENTION event emitted with correct fields."""
        self._seed_active_session("na3")
        with patch("cw.wrapper.fire_push_notification"):
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="tail output",
                session_id="na3",
                claude_session_id="csid-999",
            )
        events = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["session_id"] == "na3"
        assert ev.payload["client"] == "c"
        assert ev.payload["crashed"] is False
        assert ev.payload["breadcrumbs"] == "tail output"
        assert ev.payload["claude_session_id"] == "csid-999"
        assert ev.payload["paused_status"] is None
        assert ev.payload["ticket_id"] == "T-1"

    def test_idempotent_already_completed(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """Calling on COMPLETED session is a no-op."""
        sess = Session(
            id="na4",
            name="c/auto-dev/T-1",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.COMPLETED,
            completed_reason=CompletionReason.CRASHED,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))
        with patch("cw.wrapper.fire_push_notification") as mock_notify:
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="",
                session_id="na4",
                claude_session_id=None,
            )
        # Completed reason preserved, no notification fired
        updated = load_state()
        assert updated.sessions[0].completed_reason == CompletionReason.CRASHED
        mock_notify.assert_not_called()

    def test_no_session_found_is_noop(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """Missing session → no exception."""
        save_state(CwState(sessions=[]))
        with patch("cw.wrapper.fire_push_notification"):
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="",
                session_id="missing",
                claude_session_id=None,
            )

    def test_calls_fire_push_notification(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """fire_push_notification called once per invocation."""
        self._seed_active_session("na5")
        with patch("cw.wrapper.fire_push_notification") as mock_notify:
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="",
                session_id="na5",
                claude_session_id=None,
            )
        mock_notify.assert_called_once_with("c/auto-dev/T-1", "c")

    def test_transitions_queue_task_to_blocked_on_user(
        self, tmp_config_dir: Path, tmp_state_dir: Path
    ) -> None:
        """RUNNING queue task for the session's ticket is set to BLOCKED_ON_USER."""
        self._seed_active_session("na6")
        task = TicketTask(ticket_id="T-1", client="c", status=QueueItemStatus.RUNNING)
        save_dev_queue(DevQueueStore(tasks=[task]))

        with patch("cw.wrapper.fire_push_notification"):
            signal_needs_attention(
                "c",
                "impl",
                breadcrumbs="",
                session_id="na6",
                claude_session_id=None,
            )

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "T-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER


class TestRunClaudeWrapperNeedsAttention:
    def _seed_active_session(self, sid: str, name: str = "c/auto-dev/T-1") -> Session:
        sess = Session(
            id=sid,
            name=name,
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
        )
        save_state(CwState(sessions=[sess]))
        return sess

    def test_paused_status_routes_to_signal_needs_attention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """ambiguities_pending_resolution sentinel → signal_needs_attention."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "rw1")
        self._seed_active_session("rw1")

        result = _make_result(status="ambiguities_pending_resolution")
        captured = _sentinel_stdout(result)

        with (
            patch("cw.wrapper._run_claude_streaming", return_value=(0, captured)),
            patch("cw.wrapper.fire_push_notification"),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-1 --headless"))

        updated = load_state()
        sess = updated.sessions[0]
        assert sess.status == SessionStatus.COMPLETED
        assert sess.last_result is not None
        assert sess.last_result["needs_attention"] is True

        events = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert len(events) == 1
        assert events[0].payload["paused_status"] == "ambiguities_pending_resolution"

    def test_nonzero_exit_routes_to_signal_idle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """Nonzero returncode → signal_idle (not needs_attention)."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "rw2")
        self._seed_active_session("rw2")

        result = _make_result(status="ambiguities_pending_resolution")
        captured = _sentinel_stdout(result)

        with patch("cw.wrapper._run_claude_streaming", return_value=(1, captured)):
            run_claude_wrapper(("--print", "/auto-dev T-1 --headless"))

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.IDLE

    def test_headless_no_sentinel_routes_to_signal_needs_attention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """Headless exit code 0 + no sentinel → signal_needs_attention."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "rw3")
        self._seed_active_session("rw3")

        with (
            patch(
                "cw.wrapper._run_claude_streaming",
                return_value=(0, "output but no sentinel\n"),
            ),
            patch("cw.wrapper.fire_push_notification"),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-1 --headless"))

        updated = load_state()
        sess = updated.sessions[0]
        assert sess.status == SessionStatus.COMPLETED
        assert sess.last_result is not None
        assert sess.last_result["needs_attention"] is True

    def test_premises_pending_routes_to_signal_needs_attention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        tmp_state_dir: Path,
    ) -> None:
        """premises_pending_verification sentinel → signal_needs_attention."""
        monkeypatch.setenv("CW_CLIENT", "c")
        monkeypatch.setenv("CW_PURPOSE", "impl")
        monkeypatch.setenv("CW_SESSION_ID", "rw4")
        self._seed_active_session("rw4")

        result = _make_result(status="premises_pending_verification")
        captured = _sentinel_stdout(result)

        with (
            patch("cw.wrapper._run_claude_streaming", return_value=(0, captured)),
            patch("cw.wrapper.fire_push_notification"),
        ):
            run_claude_wrapper(("--print", "/auto-dev T-1 --headless"))

        updated = load_state()
        sess = updated.sessions[0]
        assert sess.status == SessionStatus.COMPLETED
        assert sess.last_result is not None
        assert sess.last_result["needs_attention"] is True
