"""Tests for cw.session - session lifecycle management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.session import (
    _build_pane_args,
    _create_all_purpose_sessions,
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestBuildPaneArgs:
    def test_includes_system_prompt(self, sample_client: ClientConfig) -> None:
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        assert "--append-system-prompt" in panes["impl"]["claude_cmd"]
        assert "IMPLEMENTATION" in panes["impl"]["claude_cmd"]

    def test_fresh_start_uses_session_id(self, sample_client: ClientConfig) -> None:
        """Fresh session (no claude_session_id) uses --session-id <uuid>."""
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        cmd = panes["impl"]["claude_cmd"]
        assert "--session-id" in cmd
        assert "--resume" not in cmd
        # Session should have claude_session_id set after build
        assert session.claude_session_id is not None

    def test_recovery_uses_resume_with_id(self, sample_client: ClientConfig) -> None:
        """Recovery session (claude_session_id set) uses --resume <uuid>."""
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
            claude_session_id="550e8400-e29b-41d4-a716-446655440000",
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        cmd = panes["impl"]["claude_cmd"]
        assert "--resume 550e8400-e29b-41d4-a716-446655440000" in cmd
        assert "--session-id" not in cmd

    def test_client_override_prompt(self, sample_client: ClientConfig) -> None:
        sample_client.purpose_prompts = {"impl": "Custom impl prompt."}
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        assert "Custom impl prompt." in panes["impl"]["claude_cmd"]

    def test_cwd_from_worktree(self, sample_client: ClientConfig) -> None:
        wt = sample_client.workspace_path.parent / "worktree"
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
            worktree_path=wt,
        )
        panes = _build_pane_args({"impl": session})
        assert panes["impl"]["cwd"] == str(wt)

    def test_cwd_falls_back_to_workspace(self, sample_client: ClientConfig) -> None:
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session})
        assert panes["impl"]["cwd"] == str(sample_client.workspace_path)

    def test_no_client_omits_env_vars(self, sample_client: ClientConfig) -> None:
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session})
        cmd = panes["impl"]["claude_cmd"]
        assert "CW_CLIENT" not in cmd
        assert "CW_PURPOSE" not in cmd

    def test_env_var_prefix_in_command(self, sample_client: ClientConfig) -> None:
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        cmd = panes["impl"]["claude_cmd"]
        assert "CW_CLIENT=test-client" in cmd
        assert "CW_PURPOSE=impl" in cmd

    def test_client_identity_in_prompt(self, sample_client: ClientConfig) -> None:
        session = Session(
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=sample_client.workspace_path,
        )
        panes = _build_pane_args({"impl": session}, client=sample_client)
        cmd = panes["impl"]["claude_cmd"]
        assert "[cw identity]" in cmd
        assert "test-client" in cmd


class TestCreateAllPurposeSessions:
    def test_uses_auto_purposes(self, sample_client: ClientConfig) -> None:
        """_create_all_purpose_sessions iterates client.auto_purposes."""
        sample_client.auto_purposes = [SessionPurpose.IMPL, SessionPurpose.IDEA]
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
        )
        assert set(sessions.keys()) == {"impl", "idea"}
        assert len(state.sessions) == 2

    def test_default_purposes(self, sample_client: ClientConfig) -> None:
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
        )
        assert set(sessions.keys()) == {"impl", "idea", "debt"}

    def test_single_purpose(self, sample_client: ClientConfig) -> None:
        sample_client.auto_purposes = [SessionPurpose.IMPL]
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
        )
        assert set(sessions.keys()) == {"impl"}
        assert len(state.sessions) == 1

    def test_surface_ref_set_to_purpose(self, sample_client: ClientConfig) -> None:
        """Sessions get surface_ref set to the purpose name by default."""
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
        )
        for purpose, session in sessions.items():
            assert session.surface_ref == purpose


class TestCreateAllPurposeSessionsWithPrior:
    def test_carries_forward_claude_session_id(
        self,
        sample_client: ClientConfig,
    ) -> None:
        """Prior sessions' claude_session_id is carried forward."""
        prior = {
            "impl": Session(
                name="test-client/impl",
                client="test-client",
                purpose=SessionPurpose.IMPL,
                workspace_path=sample_client.workspace_path,
                claude_session_id="old-uuid-impl",
            ),
        }
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
            prior_sessions=prior,
        )
        assert sessions["impl"].claude_session_id == "old-uuid-impl"
        # idea and debt have no prior, so no claude_session_id
        assert sessions["idea"].claude_session_id is None
        assert sessions["debt"].claude_session_id is None

    def test_no_prior_generates_fresh(
        self,
        sample_client: ClientConfig,
    ) -> None:
        """Without prior_sessions, all sessions have no claude_session_id."""
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name,
            sample_client,
            state,
        )
        for s in sessions.values():
            assert s.claude_session_id is None


class TestStartSession:
    def test_new_session_creates_and_saves(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        # Set up client config
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        start_session("test-client", "impl", adapter=mock_cmux_adapter)

        state = load_state()
        # Fresh start creates sessions for all purposes (impl, idea, debt)
        assert len(state.sessions) == 3
        purposes = {s.purpose for s in state.sessions}
        assert purposes == {
            SessionPurpose.IMPL,
            SessionPurpose.IDEA,
            SessionPurpose.DEBT,
        }
        for s in state.sessions:
            assert s.client == "test-client"
            assert s.status == SessionStatus.ACTIVE

    def test_new_session_spawns_cmux_surfaces(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """start_session calls adapter.spawn for each purpose."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        start_session("test-client", "impl", adapter=mock_cmux_adapter)

        # Spawned one surface per purpose (3 default purposes)
        assert len(mock_cmux_adapter.calls["spawn"]) == 3

    def test_existing_backgrounded_triggers_resume(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        start_session("test-client", "impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert (
            "backgrounded session" in output.lower() or "Found backgrounded" in output
        )

    def test_existing_active_noop(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        start_session("test-client", "impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "already active" in output.lower()

    def test_start_with_worktree(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        # Mock create_worktree to return a path
        wt_path = sample_client.workspace_path.parent / ".worktrees" / "feat-search"
        wt_path.mkdir(parents=True)
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: wt_path,
        )

        start_session(
            "test-client", "impl", worktree="feat/search", adapter=mock_cmux_adapter
        )

        state = load_state()
        # impl and idea should have worktree_path set
        impl_sessions = [s for s in state.sessions if s.purpose == SessionPurpose.IMPL]
        idea_sessions = [s for s in state.sessions if s.purpose == SessionPurpose.IDEA]
        debt_sessions = [s for s in state.sessions if s.purpose == SessionPurpose.DEBT]
        assert impl_sessions[0].worktree_path == wt_path
        assert impl_sessions[0].branch == "feat/search"
        assert idea_sessions[0].worktree_path == wt_path
        assert debt_sessions[0].worktree_path is None


class TestStartWorktreeClient:
    def test_auto_creates_worktree(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Worktree-mode client auto-creates worktree at start."""
        repo = tmp_path / "repo"
        repo.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  client-a:\n    repo_path: {repo}\n    branch: client-a\n"
        )

        wt_path = tmp_path / "wt" / "client-a"
        wt_path.mkdir(parents=True)
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: wt_path,
        )

        start_session("client-a", "impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "Creating worktree for branch 'client-a'" in output
        assert str(wt_path) in output

        state = load_state()
        # All sessions should exist
        assert len(state.sessions) == 3
        # impl and idea should have worktree_path
        impl = next(s for s in state.sessions if s.purpose == SessionPurpose.IMPL)
        assert impl.worktree_path == wt_path
        assert impl.branch == "client-a"

    def test_second_worktree_client_creates_surfaces(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second client creates surfaces via cmux adapter."""
        repo = tmp_path / "repo"
        repo.mkdir()
        ws = tmp_path / "personal"
        ws.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  personal:\n"
            f"    workspace_path: {ws}\n"
            "  client-a:\n"
            f"    repo_path: {repo}\n"
            "    branch: client-a\n"
        )

        wt_path = tmp_path / "wt" / "client-a"
        wt_path.mkdir(parents=True)
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: wt_path,
        )

        start_session("client-a", "impl", adapter=mock_cmux_adapter)

        # Should have spawned surfaces for all purposes
        assert len(mock_cmux_adapter.calls["spawn"]) == 3


class TestBackgroundSession:
    def test_by_name(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
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

        background_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_auto_detect_single_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
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

        background_session()

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_raises_on_multiple_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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
                    name="c/idea",
                    client="c",
                    purpose=SessionPurpose.IDEA,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        with pytest.raises(CwError, match="Multiple active"):
            background_session()

    def test_raises_on_no_active(
        self,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="No active sessions"):
            background_session()

    def test_raises_if_not_active_status(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        with pytest.raises(CwError, match="not active or idle"):
            background_session("c/impl")

    def test_finds_latest_handoff(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
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

        background_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].last_handoff_path is not None
        output = capsys.readouterr().out
        assert "Not inside cmux" in output

    def test_session_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="Session not found"):
            background_session("nonexistent")


class TestResumeSession:
    def test_extracts_prompt_and_updates_state(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.ACTIVE
        assert updated.sessions[0].resumed_at is not None

        output = capsys.readouterr().out
        assert "Resumed session" in output

    def test_resume_spawns_cmux_surface(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        sample_handoff_file: Path,
    ) -> None:
        """resume_session calls adapter.spawn to create a new surface."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="spawn001",
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

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        assert len(mock_cmux_adapter.calls["spawn"]) == 1
        workspace, cmd, _surface = mock_cmux_adapter.calls["spawn"][0]
        assert "test-client" in str(workspace)
        assert "claude --resume" in str(cmd)

    def test_no_handoff_warns(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "No handoff file" in output

    def test_not_backgrounded_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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

        with pytest.raises(CwError, match="not backgrounded or idle"):
            resume_session("test-client/impl", adapter=mock_cmux_adapter)

    def test_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="Session not found"):
            resume_session("nonexistent", adapter=mock_cmux_adapter)

    def test_resume_shows_prompt(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        sample_handoff_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

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

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "Resumption prompt:" in output
        assert "auth feature" in output

    def test_resume_no_handoff_injects_context_only(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without a handoff, resumed session still gets [cw identity] context."""
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="nohnd001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="impl",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "[cw identity]" in output
        assert "Client: 'test-client'" in output

    def test_resume_command_includes_env_vars(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The claude --resume command has CW_CLIENT/CW_PURPOSE env vars."""
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="envres01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="impl",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        # The spawn command should include CW_CLIENT/CW_PURPOSE env vars
        assert len(mock_cmux_adapter.calls["spawn"]) == 1
        _workspace, cmd, _surface = mock_cmux_adapter.calls["spawn"][0]
        assert "CW_CLIENT=test-client" in str(cmd)
        assert "CW_PURPOSE=impl" in str(cmd)

    def test_resume_injects_client_context(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        sample_handoff_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Resumed session gets [cw identity] prepended to handoff prompt."""
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="ctx00001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    last_handoff_path=sample_handoff_file,
                    surface_ref="impl",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "[cw identity]" in output
        assert "Client: 'test-client'" in output
        # Original handoff content still present
        assert "auth feature" in output

    def test_regular_handoff_not_cleaned_up(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        sample_handoff_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="cleanup2",
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

        resume_session("test-client/impl", adapter=mock_cmux_adapter)

        # Regular session-*.md handoffs should be preserved
        assert sample_handoff_file.exists()
        updated = load_state()
        assert updated.sessions[0].last_handoff_path is not None


class TestDoneSession:
    def test_marks_completed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="done0001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        done_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED

    def test_already_completed_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="done0002",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.COMPLETED,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        with pytest.raises(CwError, match="already completed"):
            done_session("test-client/impl")

    def test_cleanup_calls_remove_worktree(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        wt_path = sample_client.workspace_path.parent / ".worktrees" / "feat-done"
        state = CwState(
            sessions=[
                Session(
                    id="done0003",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=wt_path,
                    branch="feat/done",
                )
            ]
        )
        save_state(state)

        remove_calls: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            "cw.session.remove_worktree",
            lambda client, branch, force=False: remove_calls.append(
                (client, branch, force),
            ),
        )

        done_session("test-client/impl", cleanup=True)

        assert len(remove_calls) == 1
        assert remove_calls[0][1] == "feat/done"

    def test_force_passed_through(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        wt_path = sample_client.workspace_path.parent / ".worktrees" / "feat-force"
        state = CwState(
            sessions=[
                Session(
                    id="done0004",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=wt_path,
                    branch="feat/force",
                )
            ]
        )
        save_state(state)

        remove_calls: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            "cw.session.remove_worktree",
            lambda client, branch, force=False: remove_calls.append(
                (client, branch, force),
            ),
        )

        done_session("test-client/impl", cleanup=True, force=True)

        assert remove_calls[0][2] is True

    def test_sets_completed_reason_user(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="done0005",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        done_session("test-client/impl")

        updated = load_state()
        assert updated.sessions[0].completed_reason == CompletionReason.USER
        assert updated.sessions[0].completed_at is not None


class TestBackgroundNotify:
    def test_notify_calls_adapter_spawn(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
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
                    id="bn_src01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="bn_tgt01",
                    name="test-client/idea",
                    client="test-client",
                    purpose=SessionPurpose.IDEA,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="idea",
                ),
            ]
        )
        save_state(state)

        background_session("test-client/impl", notify="idea", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "Notified" in output

    def test_notify_no_active_target_warns(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="bn_src02",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        background_session("test-client/impl", notify="idea", adapter=mock_cmux_adapter)

        output = capsys.readouterr().out
        assert "No active idea session" in output


def test_start_session_reaps_phantom_before_existing_check(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
) -> None:
    """Phantom ACTIVE session is reaped; start_session then spawns fresh sessions.

    Reconciliation runs before the existing-session check so a dead "active"
    row doesn't cause start_session to short-circuit.
    """
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  {sample_client.name}:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )

    save_state(
        CwState(
            sessions=[
                Session(
                    id="phantom",
                    name=f"{sample_client.name}/impl",
                    client=sample_client.name,
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="gone-ref",
                ),
            ]
        )
    )

    adapter = FakeCmuxAdapter()
    # Non-empty live set bypasses reconcile's outage guard; "gone-ref" still
    # isn't live so the phantom is still reaped.
    adapter.spawn("decoy-ws", "echo")
    start_session(sample_client.name, "impl", adapter=adapter)

    reloaded = load_state()
    # Phantom got reaped
    phantom = reloaded.find_by_name_or_id("phantom")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED
    # New sessions spawned: at least one for impl beyond the decoy
    assert len(adapter.calls["spawn"]) >= 2


def test_start_session_launch_message_names_backend(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'Launching X surfaces...' message names the backend in use."""
    from cw.models import BackendName

    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  test-client:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )
    monkeypatch.setattr("cw.session._resolve_backend_name", lambda: BackendName.TMUX)

    start_session(sample_client.name, "impl", adapter=FakeCmuxAdapter())
    out = capsys.readouterr().out
    assert "Launching tmux surfaces" in out


class TestBackgroundAllSessions:
    def test_backgrounds_all_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="a001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="a002",
                    name="test-client/idea",
                    client="test-client",
                    purpose=SessionPurpose.IDEA,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        background_all_sessions()

        updated = load_state()
        for s in updated.sessions:
            assert s.status == SessionStatus.BACKGROUNDED

    def test_no_active_sessions_is_noop(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        state = CwState(sessions=[])
        save_state(state)

        background_all_sessions()  # Should not raise
