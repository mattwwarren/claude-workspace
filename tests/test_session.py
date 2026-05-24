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

    def test_cleanup_failure_prevents_completed_transition(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.exceptions import WorktreeError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        wt_path = (
            sample_client.workspace_path.parent / ".worktrees" / "feat-cleanup-fail"
        )
        state = CwState(
            sessions=[
                Session(
                    id="done0006",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=wt_path,
                    branch="feat/cleanup-fail",
                )
            ]
        )
        save_state(state)

        def mock_remove_worktree(
            client: object, branch: str, force: bool = False
        ) -> None:
            msg = "worktree has uncommitted changes"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.session.remove_worktree", mock_remove_worktree)

        with pytest.raises(WorktreeError):
            done_session("test-client/impl", cleanup=True)

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.ACTIVE


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


def test_start_session_spawn_failure_leaves_state_unchanged(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
) -> None:
    """If spawn raises mid-loop, on-disk state is unchanged from pre-call.

    Pins the invariant from issue #63: state persists once at the end after
    every surface has been spawned. Partial-success window (sessions linked
    on disk without surface_ref) must not exist.
    """
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  test-client:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )

    # Snapshot pre-call state file (empty initially).
    pre_state = load_state()
    pre_session_count = len(pre_state.sessions)

    # Adapter that fails on the second spawn — first one succeeds, then boom.
    class FlakyAdapter(FakeCmuxAdapter):
        def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
            if len(self.calls["spawn"]) >= 1:
                msg = "simulated spawn failure"
                raise CwError(msg)
            return super().spawn(workspace, command, surface)

    adapter = FlakyAdapter()

    with pytest.raises(CwError, match="simulated spawn failure"):
        start_session("test-client", "impl", adapter=adapter)

    # Post-call: state file must be unchanged (no partial sessions persisted).
    post_state = load_state()
    assert len(post_state.sessions) == pre_session_count


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


class TestStartSessionParentLinkage:
    """Tests for bidirectional parent/worker linkage via start_session --parent."""

    def _write_clients_file(
        self, tmp_config_dir: Path, sample_client: ClientConfig
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

    def test_parent_linkage_bidirectional(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Worker sessions get parent_session_id; parent gains all worker IDs.

        The parent has purpose=DEBT so start_session("impl") doesn't see it
        as an existing ACTIVE impl session and short-circuit to noop.
        """
        self._write_clients_file(tmp_config_dir, sample_client)

        # Use debt purpose for parent so it doesn't collide with new impl sessions
        parent_session = Session(
            id="parent01",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session]))

        start_session(
            "test-client", "impl", parent="parent01", adapter=mock_cmux_adapter
        )

        state = load_state()
        # Original parent session + 3 new sessions (impl, idea, debt)
        assert len(state.sessions) == 4
        new_sessions = [s for s in state.sessions if s.id != "parent01"]
        assert len(new_sessions) == 3

        # Only the impl session links back to the parent
        impl_session = next(s for s in new_sessions if s.purpose == SessionPurpose.IMPL)
        non_impl_sessions = [
            s for s in new_sessions if s.purpose != SessionPurpose.IMPL
        ]
        assert impl_session.parent_session_id == "parent01"
        for non_impl in non_impl_sessions:
            assert non_impl.parent_session_id is None

        # Parent accumulates only the impl session's ID (1 entry per start call)
        updated_parent = state.find_by_name_or_id("parent01")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == [impl_session.id]

    def test_two_workers_from_same_parent(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Calling start_session twice with the same parent accumulates worker IDs.

        The parent has purpose=DEBT to avoid colliding with the new impl sessions
        created in each call.  After the first call, the three new sessions are
        marked COMPLETED so the second call creates three more.
        """
        self._write_clients_file(tmp_config_dir, sample_client)

        parent_session = Session(
            id="parent02",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session]))

        # First call: start_session creates 3 worker sessions
        start_session(
            "test-client", "impl", parent="parent02", adapter=mock_cmux_adapter
        )

        # Mark new sessions completed so the second call isn't blocked by ACTIVE check
        state = load_state()
        for s in state.sessions:
            if s.id != "parent02":
                s.status = SessionStatus.COMPLETED
        save_state(state)

        # Second call: creates another 3 worker sessions
        start_session(
            "test-client", "impl", parent="parent02", adapter=mock_cmux_adapter
        )

        state = load_state()
        updated_parent = state.find_by_name_or_id("parent02")
        assert updated_parent is not None
        # Parent accumulates 1 impl session ID per start call: 2 total
        assert len(updated_parent.worker_session_ids) == 2

    def test_nonexistent_parent_raises_cw_error(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Unknown parent ID raises CwError before any state is written."""
        self._write_clients_file(tmp_config_dir, sample_client)
        save_state(CwState())

        with pytest.raises(CwError, match="Parent session not found: no-such-id"):
            start_session(
                "test-client", "impl", parent="no-such-id", adapter=mock_cmux_adapter
            )

        # No new sessions written to state
        state = load_state()
        assert state.sessions == []

    def test_crash_mid_write_leaves_state_unchanged(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If save_state raises after mutations, state file is unchanged.

        The atomic write path (write temp + os.replace) means either the old
        file or the new file is visible — never a partial write. We simulate
        the save raising to verify the original state is preserved on disk.
        """
        self._write_clients_file(tmp_config_dir, sample_client)

        # debt parent avoids colliding with start_session("impl") active-check
        parent_session = Session(
            id="parent03",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        initial_state = CwState(sessions=[parent_session])
        save_state(initial_state)

        # Patch save_state to raise on the first call after session creation
        call_count = 0

        def failing_save(state: CwState) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Simulated disk failure"
                raise OSError(msg)

        monkeypatch.setattr("cw.session.save_state", failing_save)

        with pytest.raises(OSError, match="Simulated disk failure"):
            start_session(
                "test-client", "impl", parent="parent03", adapter=mock_cmux_adapter
            )

        # State on disk is unchanged — still just the parent session
        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].id == "parent03"
        assert state.sessions[0].worker_session_ids == []

    def test_start_without_parent_unchanged(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """start_session without --parent works as before (regression guard)."""
        self._write_clients_file(tmp_config_dir, sample_client)

        start_session("test-client", "impl", adapter=mock_cmux_adapter)

        state = load_state()
        assert len(state.sessions) == 3
        for s in state.sessions:
            assert s.parent_session_id is None
            assert s.worker_session_ids == []

    def test_parent_with_existing_active_session_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """CwError raised when --parent collides with an existing ACTIVE session."""
        self._write_clients_file(tmp_config_dir, sample_client)

        parent_session = Session(
            id="parent04",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        # Pre-create an active impl session to trigger the short-circuit path
        existing_impl = Session(
            id="existing-impl",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session, existing_impl]))

        with pytest.raises(CwError, match="already active"):
            start_session(
                "test-client", "impl", parent="parent04", adapter=mock_cmux_adapter
            )

        # No new sessions and parent's worker_session_ids unchanged
        state = load_state()
        assert len(state.sessions) == 2
        updated_parent = state.find_by_name_or_id("parent04")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == []

    def test_parent_with_existing_backgrounded_session_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """CwError raised when --parent collides with existing BACKGROUNDED session."""
        self._write_clients_file(tmp_config_dir, sample_client)

        parent_session = Session(
            id="parent05",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        # Pre-create a backgrounded impl session to trigger the short-circuit path
        existing_impl = Session(
            id="existing-impl-bg",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session, existing_impl]))

        err_match = "Cannot apply --parent to existing backgrounded session"
        with pytest.raises(CwError, match=err_match):
            start_session(
                "test-client", "impl", parent="parent05", adapter=mock_cmux_adapter
            )

        # No new sessions and parent's worker_session_ids unchanged
        state = load_state()
        assert len(state.sessions) == 2
        updated_parent = state.find_by_name_or_id("parent05")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == []

    def test_parent_validation_runs_before_short_circuit(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Bad parent ID raises CwError even when an active session would
        short-circuit."""
        self._write_clients_file(tmp_config_dir, sample_client)

        # Pre-create an active impl session that would normally short-circuit
        existing_impl = Session(
            id="existing-impl-sc",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[existing_impl]))

        # The bad parent ID error must fire BEFORE the active-session short-circuit
        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", adapter=mock_cmux_adapter
            )

    def test_parent_validation_runs_before_backgrounded_short_circuit(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Bad parent ID raises CwError even when a backgrounded session would
        short-circuit (resume path)."""
        self._write_clients_file(tmp_config_dir, sample_client)

        # Pre-create a backgrounded impl session that would normally trigger
        # the resume_session short-circuit path.
        existing_impl = Session(
            id="existing-impl-bg-sc",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[existing_impl]))

        # Bad parent ID must error BEFORE resume_session is invoked.
        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", adapter=mock_cmux_adapter
            )

        # Existing session must remain BACKGROUNDED — short-circuit never ran.
        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_parent_requires_impl_in_auto_purposes(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """--parent raises CwError when client.auto_purposes excludes 'impl'."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
            f"    auto_purposes: [idea, debt]\n"
        )

        parent_session = Session(
            id="parent06",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session]))

        err_match = "--parent requires the client config to include 'impl'"
        with pytest.raises(CwError, match=err_match):
            start_session(
                "test-client", "impl", parent="parent06", adapter=mock_cmux_adapter
            )

        # No new sessions created and parent unchanged.
        state = load_state()
        assert len(state.sessions) == 1
        updated_parent = state.find_by_name_or_id("parent06")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == []

    def test_parent_validation_runs_before_worktree_creation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """Bad --parent on a worktree-mode client must error BEFORE
        create_worktree runs, so no on-disk worktree is orphaned.

        Pins Item 2's stated motivation: a regression that re-orders parent
        validation back below worktree creation would call create_worktree
        and leave an orphaned worktree on disk before the validation error
        fires. This spy catches that regression.
        """
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
            f"    branch: main\n"
        )

        create_worktree_calls: list[tuple[object, ...]] = []

        def spy_create_worktree(*args: object, **kwargs: object) -> Path:
            create_worktree_calls.append(args)
            msg = "create_worktree should not have been called"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.session.create_worktree", spy_create_worktree)

        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", adapter=mock_cmux_adapter
            )

        assert create_worktree_calls == []
