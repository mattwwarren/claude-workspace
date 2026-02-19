"""Tests for cw.session - session lifecycle management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cw.config import save_state
from cw.exceptions import CwError
from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus


class TestStartSession:
    def test_new_session_creates_and_saves(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.config import load_state

        # Set up client config
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        from cw.session import start_session

        start_session("test-client", "impl")

        state = load_state()
        # Fresh start creates sessions for all purposes (impl, review, debt)
        assert len(state.sessions) == 3
        purposes = {s.purpose for s in state.sessions}
        assert purposes == {
            SessionPurpose.IMPL,
            SessionPurpose.REVIEW,
            SessionPurpose.DEBT,
        }
        for s in state.sessions:
            assert s.client == "test-client"
            assert s.status == SessionStatus.ACTIVE

    def test_existing_backgrounded_triggers_resume(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        # Pre-create a backgrounded session
        state = CwState(
            sessions=[
                Session(
                    id="bg123456",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import start_session

        start_session("test-client", "impl")

        output = capsys.readouterr().out
        assert (
            "backgrounded session" in output.lower()
            or "Found backgrounded" in output
        )

    def test_existing_active_navigates(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="active12",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        # Mock Zellij session as running so the active check doesn't clean up
        monkeypatch.setattr("cw.zellij.session_exists", lambda _name: True)

        from cw.session import start_session

        start_session("test-client", "impl")

        output = capsys.readouterr().out
        assert "already active" in output.lower()

    def test_crashed_pane_triggers_recovery(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cw.config import load_state

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="crash002",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="impl",
                    zellij_tab="test-client",
                )
            ]
        )
        save_state(state)

        # Zellij session exists but impl pane has crashed
        monkeypatch.setattr("cw.zellij.session_exists", lambda _name: True)
        monkeypatch.setattr(
            "cw.zellij.check_pane_health",
            lambda session=None: {"impl": False},
        )

        from cw.session import start_session

        start_session("test-client", "impl")

        output = capsys.readouterr().out
        assert "crashed" in output.lower() or "Recovering" in output

        # The crashed session should be marked COMPLETED
        updated = load_state()
        completed = [s for s in updated.sessions if s.status == SessionStatus.COMPLETED]
        assert len(completed) >= 1

    def test_zellij_not_installed_raises(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("cw.zellij.is_installed", lambda: False)

        from cw.session import start_session

        with pytest.raises(CwError, match="not installed"):
            start_session("test-client", "impl")


class TestBackgroundSession:
    def test_by_name(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cw.config import load_state

        state = CwState(
            sessions=[
                Session(
                    id="bg000001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import background_session

        background_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_auto_detect_single_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        from cw.config import load_state

        state = CwState(
            sessions=[
                Session(
                    id="single01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import background_session

        background_session()

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_raises_on_multiple_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="multi001",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="multi002",
                    name="c/review",
                    client="c",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        from cw.session import background_session

        with pytest.raises(CwError, match="Multiple active"):
            background_session()

    def test_raises_on_no_active(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

        from cw.session import background_session

        with pytest.raises(CwError, match="No active sessions"):
            background_session()

    def test_raises_if_not_active_status(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="notact01",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import background_session

        with pytest.raises(CwError, match="not active"):
            background_session("c/impl")

    def test_outside_zellij_finds_latest_handoff(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cw.config import load_state

        # Create a handoff file
        handoffs_dir = sample_client.workspace_path / ".handoffs"
        handoffs_dir.mkdir(parents=True)
        handoff = handoffs_dir / "session-test.md"
        handoff.write_text("# Handoff\n")

        state = CwState(
            sessions=[
                Session(
                    id="outside1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import background_session

        background_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].last_handoff_path is not None
        output = capsys.readouterr().out
        assert "Not inside Zellij" in output

    def test_session_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

        from cw.session import background_session

        with pytest.raises(CwError, match="Session not found"):
            background_session("nonexistent")


class TestResumeSession:
    def test_extracts_prompt_and_updates_state(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        sample_handoff_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cw.config import load_state

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="resume01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    last_handoff_path=sample_handoff_file,
                )
            ]
        )
        save_state(state)

        from cw.session import resume_session

        resume_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.ACTIVE
        assert updated.sessions[0].resumed_at is not None

        output = capsys.readouterr().out
        assert "Resumed session" in output

    def test_no_handoff_warns(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="nohndff1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import resume_session

        resume_session("test-client/impl")

        output = capsys.readouterr().out
        assert "No handoff file" in output

    def test_not_backgrounded_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="notbg001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        from cw.session import resume_session

        with pytest.raises(CwError, match="not backgrounded"):
            resume_session("test-client/impl")

    def test_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

        from cw.session import resume_session

        with pytest.raises(CwError, match="Session not found"):
            resume_session("nonexistent")

    def test_outside_zellij_shows_prompt(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        sample_handoff_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="outside2",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    last_handoff_path=sample_handoff_file,
                )
            ]
        )
        save_state(state)

        from cw.session import resume_session

        resume_session("test-client/impl")

        output = capsys.readouterr().out
        assert "Resumption prompt:" in output
        assert "auth feature" in output

    def test_in_zellij_injects_prompt(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        sample_handoff_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Override mock_zellij's in_zellij_session to return True
        monkeypatch.setattr("cw.zellij.in_zellij_session", lambda: True)
        # Mock session_exists to return True (session already running)
        monkeypatch.setattr("cw.zellij.session_exists", lambda name: True)
        # Skip the sleep
        monkeypatch.setattr("cw.session.time.sleep", lambda s: None)

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="inject01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    last_handoff_path=sample_handoff_file,
                    zellij_pane="impl",
                    zellij_tab="test-client",
                )
            ]
        )
        save_state(state)

        from cw.session import resume_session

        resume_session("test-client/impl")

        # Verify write_to_pane was called (for "claude\n" and prompt)
        assert len(mock_zellij["write_to_pane"]) >= 2


