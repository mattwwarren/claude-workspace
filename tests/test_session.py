"""Tests for cw.session - session lifecycle management."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
from freezegun import freeze_time

from cw.config import save_state
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
        assert len(state.sessions) == 1
        assert state.sessions[0].client == "test-client"
        assert state.sessions[0].purpose == SessionPurpose.IMPL
        assert state.sessions[0].status == SessionStatus.ACTIVE

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

    def test_zellij_not_installed_raises(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("cw.zellij.is_installed", lambda: False)

        from cw.session import start_session

        with pytest.raises(click.ClickException, match="not installed"):
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

        with pytest.raises(click.ClickException, match="Multiple active"):
            background_session()

    def test_raises_on_no_active(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

        from cw.session import background_session

        with pytest.raises(click.ClickException, match="No active sessions"):
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

        with pytest.raises(click.ClickException, match="not active"):
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

        with pytest.raises(click.ClickException, match="Session not found"):
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

        with pytest.raises(click.ClickException, match="not backgrounded"):
            resume_session("test-client/impl")

    def test_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

        from cw.session import resume_session

        with pytest.raises(click.ClickException, match="Session not found"):
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


class TestListSessions:
    def test_empty_state(
        self,
        tmp_config_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        save_state(CwState())

        from cw.session import list_sessions

        list_sessions()

        output = capsys.readouterr().out
        assert "No sessions tracked" in output

    def test_filters_completed(
        self,
        tmp_config_dir: Path,
        sample_state: CwState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        save_state(sample_state)

        from cw.session import list_sessions

        list_sessions()

        output = capsys.readouterr().out
        # Active and backgrounded should appear, completed should not
        assert "sess0001" in output
        assert "sess0002" in output
        assert "sess0003" not in output

    @freeze_time("2025-01-15 12:00:00", tz_offset=0)
    def test_formats_table(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="fmt00001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
                )
            ]
        )
        save_state(state)

        from cw.session import list_sessions

        list_sessions()

        output = capsys.readouterr().out
        assert "CLIENT" in output
        assert "PURPOSE" in output
        assert "STATUS" in output
        assert "test-client" in output
        assert "2h ago" in output


class TestShowStatus:
    @freeze_time("2025-01-15 12:00:00", tz_offset=0)
    def test_counts_and_formatting(
        self,
        tmp_config_dir: Path,
        sample_state: CwState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  test-client:\n"
            "    workspace_path: /tmp/ws\n"
            "  other-client:\n"
            "    workspace_path: /tmp/ws2\n"
        )

        save_state(sample_state)

        from cw.session import show_status

        show_status()

        output = capsys.readouterr().out
        assert "Clients configured: 2" in output
        assert "Active sessions:    1" in output
        assert "Backgrounded:       1" in output
