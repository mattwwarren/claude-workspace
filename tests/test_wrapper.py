"""Tests for cw.wrapper - Claude wrapper and IDLE signaling."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cw.config import EVENTS_DIR, load_state, save_state
from cw.history import EventType, load_history
from cw.models import CwState, Session, SessionPurpose, SessionStatus
from cw.wrapper import (
    _detect_claude_session_id,
    _idle_signal_path,
    run_claude_wrapper,
    signal_idle,
)


class TestIdleSignalPath:
    def test_format(self) -> None:
        path = _idle_signal_path("my-client", "impl")
        assert path == EVENTS_DIR / "my-client__impl.idle"

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
