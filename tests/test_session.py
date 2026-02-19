"""Tests for cw.session - session lifecycle management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

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
    background_session,
    done_session,
    handoff_session,
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


class TestCreateAllPurposeSessions:
    def test_uses_auto_purposes(self, sample_client: ClientConfig) -> None:
        """_create_all_purpose_sessions iterates client.auto_purposes."""
        sample_client.auto_purposes = [SessionPurpose.IMPL, SessionPurpose.REVIEW]
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name, sample_client, state,
        )
        assert set(sessions.keys()) == {"impl", "review"}
        assert len(state.sessions) == 2

    def test_default_purposes(self, sample_client: ClientConfig) -> None:
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name, sample_client, state,
        )
        assert set(sessions.keys()) == {"impl", "review", "debt"}

    def test_single_purpose(self, sample_client: ClientConfig) -> None:
        sample_client.auto_purposes = [SessionPurpose.IMPL]
        state = CwState()
        sessions = _create_all_purpose_sessions(
            sample_client.name, sample_client, state,
        )
        assert set(sessions.keys()) == {"impl"}
        assert len(state.sessions) == 1


class TestStartSession:
    def test_new_session_creates_and_saves(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Set up client config
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

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

        def _mock_check_pane_health(
            *, session: str | None = None
        ) -> dict[str, bool]:
            return {"impl": False}

        monkeypatch.setattr(
            "cw.zellij.check_pane_health",
            _mock_check_pane_health,
        )

        start_session("test-client", "impl")

        output = capsys.readouterr().out
        assert "crashed" in output.lower() or "Recovering" in output

        # The crashed session should be marked COMPLETED with CRASHED reason
        updated = load_state()
        completed = [
            s for s in updated.sessions if s.status == SessionStatus.COMPLETED
        ]
        assert len(completed) >= 1
        crashed = [
            s for s in completed
            if s.completed_reason == CompletionReason.CRASHED
        ]
        assert len(crashed) >= 1
        assert crashed[0].completed_at is not None

    def test_start_with_worktree(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
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

        start_session("test-client", "impl", worktree="feat/search")

        state = load_state()
        # impl and review should have worktree_path set
        impl_sessions = [
            s for s in state.sessions if s.purpose == SessionPurpose.IMPL
        ]
        review_sessions = [
            s for s in state.sessions if s.purpose == SessionPurpose.REVIEW
        ]
        debt_sessions = [
            s for s in state.sessions if s.purpose == SessionPurpose.DEBT
        ]
        assert impl_sessions[0].worktree_path == wt_path
        assert impl_sessions[0].branch == "feat/search"
        assert review_sessions[0].worktree_path == wt_path
        assert debt_sessions[0].worktree_path is None

    def test_zellij_not_installed_raises(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("cw.zellij.is_installed", lambda: False)

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
        mock_zellij: dict[str, list[Any]],
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

        with pytest.raises(CwError, match="Multiple active"):
            background_session()

    def test_raises_on_no_active(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

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

        with pytest.raises(CwError, match="not active"):
            background_session("c/impl")

    def test_outside_zellij_finds_latest_handoff(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
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
        assert "Not inside Zellij" in output

    def test_session_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

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

        with pytest.raises(CwError, match="not backgrounded"):
            resume_session("test-client/impl")

    def test_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        save_state(CwState())

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
        monkeypatch.setattr(
            "cw.zellij.session_exists", lambda _name: True
        )
        # Skip the sleep
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

        resume_session("test-client/impl")

        # Verify write_to_pane was called (for "claude\n" and prompt)
        assert len(mock_zellij["write_to_pane"]) >= 2


class TestDoneSession:
    def test_marks_completed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
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
        mock_zellij: dict[str, list[Any]],
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
        mock_zellij: dict[str, list[Any]],
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
        mock_zellij: dict[str, list[Any]],
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


class TestHandoffSession:
    def test_backgrounds_source_and_delivers_to_active_target(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="ho_src01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="ho_tgt01",
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="review",
                    zellij_tab="test-client",
                ),
            ]
        )
        save_state(state)

        handoff_session("impl", "review", client_name="test-client")

        updated = load_state()
        src = updated.find_by_name_or_id("ho_src01")
        assert src is not None
        assert src.status == SessionStatus.COMPLETED
        assert src.completed_reason == CompletionReason.HANDOFF

        output = capsys.readouterr().out
        assert "Handoff complete" in output

    def test_delivers_to_backgrounded_target(
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
                    id="ho_src02",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="ho_tgt02",
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="review",
                    zellij_tab="test-client",
                ),
            ]
        )
        save_state(state)

        # Skip sleep in resume_session
        monkeypatch.setattr("cw.session.time.sleep", lambda _s: None)

        handoff_session("impl", "review", client_name="test-client")

        output = capsys.readouterr().out
        assert "Resuming" in output or "Handoff complete" in output

    def test_raises_if_source_not_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="ho_src03",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.COMPLETED,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="ho_tgt03",
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        with pytest.raises(CwError, match="No active/backgrounded impl"):
            handoff_session("impl", "review", client_name="test-client")

    def test_raises_if_target_completed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="ho_src04",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="ho_tgt04",
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.COMPLETED,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        with pytest.raises(CwError, match="No active/backgrounded review"):
            handoff_session("impl", "review", client_name="test-client")

    def test_raises_if_source_equals_target(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        with pytest.raises(CwError, match="Source and target cannot be the same"):
            handoff_session("impl", "impl", client_name="test-client")

    def test_auto_detects_client(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="ho_src05",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="ho_tgt05",
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="review",
                    zellij_tab="test-client",
                ),
            ]
        )
        save_state(state)

        # No client_name — should auto-detect
        handoff_session("impl", "review")

        output = capsys.readouterr().out
        assert "Handoff complete" in output


class TestBackgroundNotify:
    def test_notify_calls_write_to_pane(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
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
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="review",
                    zellij_tab="test-client",
                ),
            ]
        )
        save_state(state)

        background_session("test-client/impl", notify="review")

        output = capsys.readouterr().out
        assert "Notified" in output
        # write_to_pane should have been called for the notification
        assert len(mock_zellij["write_to_pane"]) >= 1

    def test_notify_no_active_target_warns(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
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

        background_session("test-client/impl", notify="review")

        output = capsys.readouterr().out
        assert "No active review session" in output
