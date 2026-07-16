"""Tests for cw.session - session lifecycle management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.exceptions import CwError, SpawnUnregisteredError, WorktreeError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.session import (
    _resolve_resume_cwd,
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)
from tests.test_spawn import _write_orchestrator_disallow

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(*_args: object, **_kwargs: object) -> None:
    pass


# ---------------------------------------------------------------------------
# TestIsNativeSurfaceRef
# ---------------------------------------------------------------------------


class TestIsNativeSurfaceRef:
    def test_valid_8_char_hex(self) -> None:
        from cw.session import _is_native_surface_ref

        assert _is_native_surface_ref("abcd1234") is True
        assert _is_native_surface_ref("00000001") is True
        assert _is_native_surface_ref("deadbeef") is True

    def test_invalid_too_short(self) -> None:
        from cw.session import _is_native_surface_ref

        assert _is_native_surface_ref("abc1234") is False

    def test_invalid_too_long(self) -> None:
        from cw.session import _is_native_surface_ref

        assert _is_native_surface_ref("abcd12345") is False

    def test_invalid_non_hex_chars(self) -> None:
        from cw.session import _is_native_surface_ref

        assert _is_native_surface_ref("abcg1234") is False
        assert _is_native_surface_ref("impl-pane") is False

    def test_invalid_uppercase(self) -> None:
        from cw.session import _is_native_surface_ref

        assert _is_native_surface_ref("ABCD1234") is False


# ---------------------------------------------------------------------------
# TestStartSession
# ---------------------------------------------------------------------------


class TestStartSession:
    def _write_clients_file(
        self, tmp_config_dir: Path, sample_client: ClientConfig
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

    def test_new_session_creates_and_saves(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        state = load_state()
        assert len(state.sessions) == 1
        s = state.sessions[0]
        assert s.client == "test-client"
        assert s.purpose == SessionPurpose.IMPL
        assert s.status == SessionStatus.ACTIVE
        assert s.origin == SessionOrigin.USER

    def test_new_session_spawns_via_daemon(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        assert len(mock_native_daemon.spawn_calls) == 1

    def test_new_session_surface_ref_is_short_id(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        state = load_state()
        # FakeNativeDaemonClient returns "00000001" for the first spawn
        assert state.sessions[0].surface_ref == "00000001"

    def test_new_session_calls_attach(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)

        attached: list[str] = []
        monkeypatch.setattr("cw.session._attach_session", attached.append)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        assert attached == ["00000001"]

    def test_new_session_writes_hook_context(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        hook_calls: list[dict[str, object]] = []

        def capture_hook(path: object, **kwargs: object) -> None:
            hook_calls.append({"path": path, **kwargs})

        monkeypatch.setattr("cw.session._write_hook_context", capture_hook)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        assert len(hook_calls) == 1
        assert hook_calls[0]["client"] == "test-client"
        assert hook_calls[0]["purpose"] == "impl"
        assert hook_calls[0]["origin"] == SessionOrigin.USER

    def test_start_debt_purpose_hook_context_omits_workspace_path(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#940 invariant: a non-worktree (debt) start never sets workspace_path
        in the hook context, so ``cw guard-cwd`` no-ops instead of blocking every
        Bash call on a legitimately main-homed interactive session (R2 case iii /
        Plan Soundness Advisory). See
        test_start_worktree_impl_hook_context_sets_workspace_path for the
        contrasting worktree-homed case, where workspace_path IS set."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        hook_calls: list[dict[str, object]] = []

        def capture_hook(path: object, **kwargs: object) -> None:
            hook_calls.append({"path": path, **kwargs})

        monkeypatch.setattr("cw.session._write_hook_context", capture_hook)

        start_session("test-client", "debt", native_daemon=mock_native_daemon)

        assert len(hook_calls) == 1
        # workspace_path absent (defaults to None) → guard-cwd fallback no-ops.
        assert hook_calls[0].get("workspace_path") is None

    def test_start_worktree_impl_hook_context_sets_workspace_path(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#940 R5 coverage: an ``impl`` start that IS worktree-homed (cwd ==
        worktree_path, distinct from the main checkout) must set workspace_path
        to the main checkout, so ``cw guard-cwd`` actually protects it — the
        contrasting case to the debt/non-worktree no-op above. Prior to the
        #940 fix, workspace_path was omitted unconditionally regardless of
        purpose, silently disabling the guard for every USER-origin session."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        worktree_dir = sample_client.workspace_path.parent / "wt-impl"
        worktree_dir.mkdir()
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: worktree_dir,
        )

        hook_calls: list[dict[str, object]] = []

        def capture_hook(path: object, **kwargs: object) -> None:
            hook_calls.append({"path": path, **kwargs})

        monkeypatch.setattr("cw.session._write_hook_context", capture_hook)

        start_session(
            "test-client", "impl", worktree="feat/x", native_daemon=mock_native_daemon
        )

        assert len(hook_calls) == 1
        assert hook_calls[0]["path"] == worktree_dir
        assert hook_calls[0].get("workspace_path") == sample_client.workspace_path

    def test_existing_backgrounded_triggers_resume(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="bg123456",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="abcd1234",
                )
            ]
        )
        # Make the backgrounded session appear live in daemon
        mock_native_daemon._live.add("abcd1234")
        save_state(state)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        output = capsys.readouterr().out
        assert (
            "backgrounded session" in output.lower() or "Found backgrounded" in output
        )

    def test_existing_active_noop(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

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

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        output = capsys.readouterr().out
        assert "already active" in output.lower()

    def test_start_with_worktree(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        wt_path = sample_client.workspace_path.parent / ".worktrees" / "feat-search"
        wt_path.mkdir(parents=True)
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: wt_path,
        )

        start_session(
            "test-client",
            "impl",
            worktree="feat/search",
            native_daemon=mock_native_daemon,
        )

        state = load_state()
        assert len(state.sessions) == 1
        s = state.sessions[0]
        assert s.purpose == SessionPurpose.IMPL
        assert s.worktree_path == wt_path
        assert s.branch == "feat/search"

    def test_start_debt_purpose_no_worktree_path(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """debt purpose is not in WORKTREE_PURPOSES; worktree_path stays None."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        wt_path = sample_client.workspace_path.parent / ".worktrees" / "feat"
        wt_path.mkdir(parents=True)
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: wt_path,
        )

        start_session(
            "test-client", "debt", worktree="feat/x", native_daemon=mock_native_daemon
        )

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].worktree_path is None

    def test_spawn_failure_leaves_state_unchanged(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If daemon.spawn_bg raises, no session is persisted to disk."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        pre_state = load_state()

        class FlakyDaemon(FakeNativeDaemonClient):
            def spawn_bg(
                self,
                *,
                cwd: object,
                prompt: object,
                extra_args: object = None,
                permission_mode: str | None = None,
            ) -> str:
                msg = "simulated daemon failure"
                raise CwError(msg)

        with pytest.raises(CwError, match="simulated daemon failure"):
            start_session("test-client", "impl", native_daemon=FlakyDaemon())

        post_state = load_state()
        assert len(post_state.sessions) == len(pre_state.sessions)

    def test_detach_hint_printed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        out = capsys.readouterr().out
        assert "Ctrl+Z" in out
        assert "Ctrl+D" in out


# ---------------------------------------------------------------------------
# TestStartWorktreeClient
# ---------------------------------------------------------------------------


class TestStartWorktreeClient:
    def test_auto_creates_worktree(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
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
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("client-a", "impl", native_daemon=mock_native_daemon)

        output = capsys.readouterr().out
        assert "Creating worktree for branch 'client-a'" in output
        assert str(wt_path) in output

        state = load_state()
        assert len(state.sessions) == 1
        impl = state.sessions[0]
        assert impl.worktree_path == wt_path
        assert impl.branch == "client-a"


# ---------------------------------------------------------------------------
# TestBackgroundSession
# ---------------------------------------------------------------------------


class TestBackgroundSession:
    def test_by_name(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
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
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="No active sessions"):
            background_session()

    def test_raises_if_not_active_status(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
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

    def test_session_not_found_raises(
        self,
        tmp_config_dir: Path,
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="Session not found"):
            background_session("nonexistent")

    def test_notify_warns_not_supported(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="notifyw1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        background_session("test-client/impl", notify="idea")

        output = capsys.readouterr().out
        assert "Warning" in output
        assert "notify" in output.lower()

    def test_auto_flag_persists(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="autobg01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                )
            ]
        )
        save_state(state)

        background_session("test-client/impl", auto=True)

        updated = load_state()
        assert updated.sessions[0].auto_backgrounded is True


# ---------------------------------------------------------------------------
# TestResumeSession
# ---------------------------------------------------------------------------


class TestResumeSession:
    def _write_clients_file(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        worker_model: str | None = None,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        body = (
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )
        if worker_model is not None:
            body += f"    worker_model: {worker_model}\n"
        clients_file.write_text(body)

    def test_live_session_attaches_directly(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        attached: list[str] = []
        monkeypatch.setattr("cw.session._attach_session", attached.append)

        short_id = "abcd1234"
        mock_native_daemon._live.add(short_id)

        state = CwState(
            sessions=[
                Session(
                    id="resume01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref=short_id,
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert attached == [short_id]
        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.ACTIVE
        assert updated.sessions[0].resumed_at is not None

    def test_dead_session_spawns_new(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        attached: list[str] = []
        monkeypatch.setattr("cw.session._attach_session", attached.append)

        state = CwState(
            sessions=[
                Session(
                    id="resume02",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",  # not in daemon
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        # New session spawned
        assert len(mock_native_daemon.spawn_calls) == 1
        # attach called with the new short_id
        assert len(attached) == 1
        assert attached[0] == "00000001"

        updated = load_state()
        assert updated.sessions[0].surface_ref == "00000001"
        assert updated.sessions[0].status == SessionStatus.ACTIVE

    def test_dead_session_uses_resume_flag_when_has_claude_session_id(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resume03",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert len(mock_native_daemon.spawn_calls) == 1
        # Verify --resume <uuid> was passed as extra_args
        extra = mock_native_daemon.spawn_extra_args[0]
        assert extra == ["--resume", "550e8400-e29b-41d4-a716-446655440000"]

    def test_resume_daemon_session_with_worker_model_pins_model(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON-origin resume of a dead surface forwards --model from client."""
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-sonnet-4-6-20251015",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumewm1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_extra_args[0] == [
            "--resume",
            "550e8400-e29b-41d4-a716-446655440000",
            "--model",
            "claude-sonnet-4-6-20251015",
        ]

    def test_resume_daemon_session_no_worker_model_omits_model_flag(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: DAEMON resume without worker_model only has --resume."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumewm2",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_extra_args[0] == [
            "--resume",
            "550e8400-e29b-41d4-a716-446655440000",
        ]

    def test_resume_user_session_never_pins_model(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin resume ignores client.worker_model (operator default wins)."""
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-sonnet-4-6-20251015",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumewm3",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.USER,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_extra_args[0] == [
            "--resume",
            "550e8400-e29b-41d4-a716-446655440000",
        ]

    def test_resume_daemon_non_auto_model_derives_bypass_permissions(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON resume with a Haiku pin spawns with bypassPermissions (#1111)."""
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-haiku-4-5-20251001",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumepm1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_permission_modes[0] == "bypassPermissions"

    def test_resume_daemon_auto_capable_model_permission_mode_none(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON resume with an auto-capable pin leaves permission_mode None."""
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-sonnet-4-6-20251015",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumepm2",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_permission_modes[0] is None

    def test_resume_daemon_no_worker_model_permission_mode_none(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: DAEMON resume without a pin stays permission_mode None."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumepm3",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_permission_modes[0] is None

    def test_resume_user_session_non_auto_model_permission_mode_none(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin resume never derives bypass — derivation is DAEMON-gated."""
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-haiku-4-5-20251001",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="resumepm4",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.USER,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440000",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_permission_modes[0] is None

    def test_not_backgrounded_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)

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
            resume_session("test-client/impl", native_daemon=mock_native_daemon)

    def test_not_found_raises(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        save_state(CwState())

        with pytest.raises(CwError, match="Session not found"):
            resume_session("nonexistent", native_daemon=mock_native_daemon)

    def test_live_session_prints_detach_hint(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        short_id = "abcd1234"
        mock_native_daemon._live.add(short_id)

        state = CwState(
            sessions=[
                Session(
                    id="hinttest1",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref=short_id,
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        out = capsys.readouterr().out
        assert "Ctrl+Z" in out
        assert "Ctrl+D" in out

    def test_resume_daemon_injects_disallow_when_configured(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON resume injects --disallowed-tools when orchestrator config
        sets ``disallowed_mcp_tools`` — tracker no longer affects this."""
        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="rgh726a",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440001",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        extra = mock_native_daemon.spawn_extra_args[0] or []
        # Single `=`-joined token; the bare two-token form would let the variadic
        # flag swallow the resume's positional prompt (#733).
        assert "--disallowed-tools=mcp__plugin_linear_linear__*" in extra
        assert "--disallowed-tools" not in extra

    def test_resume_daemon_multiple_patterns_comma_joined(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON resume comma-joins multiple patterns into ONE token — parity
        with the spawn_create_impl chokepoint's multi-pattern handling."""
        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*", "mcp__foo__*"])
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="rgh726b",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440002",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        extra = mock_native_daemon.spawn_extra_args[0] or []
        assert "--disallowed-tools=mcp__plugin_linear_linear__*,mcp__foo__*" in extra
        # Both patterns ride ONE token (#733); no bare two-token form.
        assert "--disallowed-tools" not in extra

    def test_resume_daemon_no_disallow_when_unconfigured(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON resume with no orchestrator config → no --disallowed-tools."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="rgh726c",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440003",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        extra = mock_native_daemon.spawn_extra_args[0] or []
        assert "--disallowed-tools" not in extra
        assert not any(tok.startswith("--disallowed-tools=") for tok in extra)

    def test_resume_user_session_never_injects_disallow(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin resume never injects --disallowed-tools (#726), even
        when orchestrator config sets ``disallowed_mcp_tools``."""
        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="rgh726b",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.USER,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440002",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        extra = mock_native_daemon.spawn_extra_args[0] or []
        assert "--disallowed-tools" not in extra
        assert not any(tok.startswith("--disallowed-tools=") for tok in extra)

    def test_resume_session_prompt_is_trailing_positional(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """resume_session (dead surface): prompt is the trailing positional in argv.

        Uses the maximally-loaded extra_args set: ``--resume <uuid>``,
        ``--model``, and ``--disallowed-tools=`` (configured disallow
        patterns). This is the exact argv shape that triggered #733 when the
        disallow flag was in two-token form — ``--disallowed-tools <pattern>``
        would consume the prompt as a second value, leaving the worker
        promptless.
        """
        from cw.native_daemon import _DEFAULT_PERMISSION_MODE, _build_spawn_argv

        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        self._write_clients_file(
            tmp_config_dir,
            sample_client,
            worker_model="claude-sonnet-4-6-20251015",
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="rargv733a",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    worktree_path=sample_client.workspace_path.parent / "wt-resume",
                    # Dead surface — not in mock's live set — forces re-spawn.
                    surface_ref="deadbeef",
                    claude_session_id="550e8400-e29b-41d4-a716-446655440099",
                )
            ]
        )
        save_state(state)

        resume_session("test-client/impl", native_daemon=mock_native_daemon)

        _, received_prompt = mock_native_daemon.spawn_calls[0]
        extra_args = mock_native_daemon.spawn_extra_args[0]
        full_argv = _build_spawn_argv(
            mode=_DEFAULT_PERMISSION_MODE,
            extra_args=extra_args,
            prompt=received_prompt,
        )

        # Prompt must be the final argv token — no variadic flag may consume it.
        assert full_argv[-1] == received_prompt
        # Sanity: all three flag types are present, exercising the full #733 shape.
        assert "--resume" in (extra_args or [])
        assert "--model" in (extra_args or [])
        assert "--disallowed-tools=mcp__plugin_linear_linear__*" in (extra_args or [])

    def test_dead_surface_unregistered_raises_spawn_unregistered_error(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dead-surface re-spawn: worker never registers → SpawnUnregisteredError."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True

        state = CwState(
            sessions=[
                Session(
                    id="rsr520a",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                )
            ]
        )
        save_state(state)

        with pytest.raises(SpawnUnregisteredError, match="spawn_unregistered"):
            resume_session(
                "test-client/impl",
                native_daemon=daemon,
                _roster_poll_timeout=0.0,
            )

    def test_dead_surface_unregistered_does_not_mark_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dead-surface re-spawn: worker never registers → session NOT marked ACTIVE."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True

        state = CwState(
            sessions=[
                Session(
                    id="rsr520b",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="deadbeef",
                )
            ]
        )
        save_state(state)

        with pytest.raises(SpawnUnregisteredError):
            resume_session(
                "test-client/impl",
                native_daemon=daemon,
                _roster_poll_timeout=0.0,
            )

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.BACKGROUNDED


# ---------------------------------------------------------------------------
# TestDoneSession
# ---------------------------------------------------------------------------


class TestDoneSession:
    def test_marks_completed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
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


# ---------------------------------------------------------------------------
# TestBackgroundAllSessions
# ---------------------------------------------------------------------------


class TestBackgroundAllSessions:
    def test_backgrounds_all_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
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
    ) -> None:
        save_state(CwState())
        background_all_sessions()  # Should not raise


# ---------------------------------------------------------------------------
# Standalone tests
# ---------------------------------------------------------------------------


def test_start_session_reaps_phantom_before_existing_check(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom ACTIVE session is reaped; start_session then spawns a fresh session.

    Reconciliation runs before the existing-session check so a dead "active"
    row doesn't cause start_session to short-circuit.
    """
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  {sample_client.name}:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )

    from datetime import UTC
    from datetime import datetime as _dt

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
                    # Older than SPAWN_GRACE_SECONDS — phantom-eligible.
                    started_at=_dt(2026, 4, 19, tzinfo=UTC),
                ),
            ]
        )
    )

    # Non-empty live set bypasses outage guard; "gone-ref" is absent so
    # the phantom session is reaped.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    monkeypatch.setattr("cw.session._write_hook_context", _noop)

    attached: list[str] = []
    monkeypatch.setattr("cw.session._attach_session", attached.append)

    daemon = FakeNativeDaemonClient()
    start_session(sample_client.name, "impl", native_daemon=daemon)

    reloaded = load_state()
    # Phantom got reaped
    phantom = reloaded.find_by_name_or_id("phantom")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED
    # New session spawned and attached
    assert len(attached) == 1


# ---------------------------------------------------------------------------
# TestStartSessionParentLinkage
# ---------------------------------------------------------------------------


class TestStartSessionParentLinkage:
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
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker session gets parent_session_id; parent gains the worker ID."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

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
            "test-client", "impl", parent="parent01", native_daemon=mock_native_daemon
        )

        state = load_state()
        # Original parent + 1 new impl session
        assert len(state.sessions) == 2
        new_session = next(s for s in state.sessions if s.id != "parent01")
        assert new_session.purpose == SessionPurpose.IMPL
        assert new_session.parent_session_id == "parent01"

        updated_parent = state.find_by_name_or_id("parent01")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == [new_session.id]

    def test_two_workers_from_same_parent(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling start_session twice with the same parent accumulates worker IDs."""
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        parent_session = Session(
            id="parent02",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[parent_session]))

        start_session(
            "test-client", "impl", parent="parent02", native_daemon=mock_native_daemon
        )

        # Mark new session completed so the second call isn't blocked
        state = load_state()
        for s in state.sessions:
            if s.id != "parent02":
                s.status = SessionStatus.COMPLETED
        save_state(state)

        start_session(
            "test-client", "impl", parent="parent02", native_daemon=mock_native_daemon
        )

        state = load_state()
        updated_parent = state.find_by_name_or_id("parent02")
        assert updated_parent is not None
        assert len(updated_parent.worker_session_ids) == 2

    def test_nonexistent_parent_raises_cw_error(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)
        save_state(CwState())

        with pytest.raises(CwError, match="Parent session not found: no-such-id"):
            start_session(
                "test-client",
                "impl",
                parent="no-such-id",
                native_daemon=mock_native_daemon,
            )

        state = load_state()
        assert state.sessions == []

    def test_crash_mid_write_leaves_state_unchanged(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

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

        call_count = 0

        def failing_save(state: CwState) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Simulated disk failure"
                raise OSError(msg)

        # mutate_state (in cw.config) calls save_state directly from its own
        # module; patch the config-level name so the failure is intercepted.
        monkeypatch.setattr("cw.config.save_state", failing_save)

        with pytest.raises(OSError, match="Simulated disk failure"):
            start_session(
                "test-client",
                "impl",
                parent="parent03",
                native_daemon=mock_native_daemon,
            )

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].id == "parent03"
        assert state.sessions[0].worker_session_ids == []

    def test_start_without_parent_unchanged(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        start_session("test-client", "impl", native_daemon=mock_native_daemon)

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].parent_session_id is None
        assert state.sessions[0].worker_session_ids == []

    def test_parent_with_existing_active_session_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        parent_session = Session(
            id="parent04",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
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
                "test-client",
                "impl",
                parent="parent04",
                native_daemon=mock_native_daemon,
            )

        state = load_state()
        assert len(state.sessions) == 2
        updated_parent = state.find_by_name_or_id("parent04")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == []

    def test_parent_with_existing_backgrounded_session_raises(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        parent_session = Session(
            id="parent05",
            name="test-client/debt",
            client="test-client",
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
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
                "test-client",
                "impl",
                parent="parent05",
                native_daemon=mock_native_daemon,
            )

        state = load_state()
        assert len(state.sessions) == 2
        updated_parent = state.find_by_name_or_id("parent05")
        assert updated_parent is not None
        assert updated_parent.worker_session_ids == []

    def test_parent_validation_runs_before_short_circuit(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        existing_impl = Session(
            id="existing-impl-sc",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[existing_impl]))

        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", native_daemon=mock_native_daemon
            )

    def test_parent_validation_runs_before_backgrounded_short_circuit(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        existing_impl = Session(
            id="existing-impl-bg-sc",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=sample_client.workspace_path,
        )
        save_state(CwState(sessions=[existing_impl]))

        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", native_daemon=mock_native_daemon
            )

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].status == SessionStatus.BACKGROUNDED

    def test_parent_requires_impl_in_auto_purposes(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
            f"    auto_purposes: [idea, debt]\n"
        )
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

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
                "test-client",
                "impl",
                parent="parent06",
                native_daemon=mock_native_daemon,
            )

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
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """Bad --parent on a worktree-mode client must error BEFORE create_worktree."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
            f"    branch: main\n"
        )
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        create_worktree_calls: list[tuple[object, ...]] = []

        def spy_create_worktree(*args: object, **kwargs: object) -> None:
            create_worktree_calls.append(args)
            msg = "create_worktree should not have been called"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.session.create_worktree", spy_create_worktree)

        with pytest.raises(CwError, match="Parent session not found: bad-id"):
            start_session(
                "test-client", "impl", parent="bad-id", native_daemon=mock_native_daemon
            )

        assert create_worktree_calls == []


# ---------------------------------------------------------------------------
# TestStartSessionIsolationGuard (#428)
# ---------------------------------------------------------------------------


class TestStartSessionIsolationGuard:
    """start_session must call check_not_main_checkout after create_worktree (#428)."""

    def _write_clients_file(
        self, tmp_config_dir: Path, sample_client: ClientConfig
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

    def test_start_with_worktree_raises_when_worktree_is_main_checkout(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cw start --worktree raises WorktreeError when create_worktree returns
        the main checkout path (#428)."""
        from cw.exceptions import WorktreeError

        self._write_clients_file(tmp_config_dir, sample_client)
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        # Simulate degenerate create_worktree returning the workspace itself.
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: sample_client.workspace_path,
        )
        # check_not_main_checkout is NOT mocked — it must fire for real.

        with pytest.raises(WorktreeError, match="main checkout"):
            start_session(
                "test-client",
                "impl",
                worktree="feat/x",
                native_daemon=mock_native_daemon,
            )

    def test_worktree_client_raises_when_worktree_is_main_checkout(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cw start (worktree-mode client) raises WorktreeError when create_worktree
        returns the main checkout path (#428)."""
        from cw.exceptions import WorktreeError

        repo = tmp_path / "repo"
        repo.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  wt-client:\n    repo_path: {repo}\n    branch: wt-client\n"
        )

        # Simulate degenerate create_worktree returning repo itself.
        monkeypatch.setattr(
            "cw.session.create_worktree",
            lambda _client, _branch: repo,
        )
        monkeypatch.setattr("cw.session._write_hook_context", _noop)
        monkeypatch.setattr("cw.session._attach_session", _noop)

        with pytest.raises(WorktreeError, match="main checkout"):
            start_session("wt-client", "impl", native_daemon=mock_native_daemon)


# ---------------------------------------------------------------------------
# TestResolveResumeCwd — R2 respawn-cwd guard (#940)
# ---------------------------------------------------------------------------


class TestResolveResumeCwd:
    """_resolve_resume_cwd enforces the R2 decision table before respawn (#940)."""

    def _client(self, tmp_path: Path) -> ClientConfig:
        workspace = tmp_path / "main-checkout"
        workspace.mkdir(parents=True, exist_ok=True)
        return ClientConfig(
            name="test-client",
            workspace_path=workspace,
            default_branch="main",
        )

    def _session(
        self,
        *,
        origin: SessionOrigin,
        purpose: SessionPurpose,
        workspace_path: Path,
        worktree_path: Path | None,
    ) -> Session:
        return Session(
            id="sess940a",
            name=f"test-client/{purpose.value}",
            client="test-client",
            purpose=purpose,
            origin=origin,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=workspace_path,
            worktree_path=worktree_path,
        )

    def test_daemon_origin_worktree_none_raises(self, tmp_path: Path) -> None:
        """(i) DAEMON-origin with worktree_path=None is corrupted → CwError."""
        client = self._client(tmp_path)
        session = self._session(
            origin=SessionOrigin.DAEMON,
            purpose=SessionPurpose.IMPL,
            workspace_path=client.workspace_path,
            worktree_path=None,
        )
        with pytest.raises(CwError, match="worktree_path"):
            _resolve_resume_cwd(session, client)

    def test_user_worktree_purpose_cwd_is_main_raises(self, tmp_path: Path) -> None:
        """(ii) USER-origin impl whose cwd resolves to main → WorktreeError."""
        client = self._client(tmp_path)
        session = self._session(
            origin=SessionOrigin.USER,
            purpose=SessionPurpose.IMPL,
            workspace_path=client.workspace_path,
            # Degenerate: worktree_path points at the main checkout itself.
            worktree_path=client.workspace_path,
        )
        with pytest.raises(WorktreeError, match="main checkout"):
            _resolve_resume_cwd(session, client)

    def test_user_worktree_purpose_distinct_worktree_proceeds(
        self, tmp_path: Path
    ) -> None:
        """(ii) USER-origin impl with a distinct worktree → returns that path."""
        client = self._client(tmp_path)
        wt = tmp_path / "wt-impl"
        wt.mkdir()
        session = self._session(
            origin=SessionOrigin.USER,
            purpose=SessionPurpose.IMPL,
            workspace_path=client.workspace_path,
            worktree_path=wt,
        )
        assert _resolve_resume_cwd(session, client) == wt

    def test_user_non_worktree_purpose_main_cwd_unguarded(self, tmp_path: Path) -> None:
        """(iii) USER-origin debt is legitimately main-homed → no guard."""
        client = self._client(tmp_path)
        session = self._session(
            origin=SessionOrigin.USER,
            purpose=SessionPurpose.DEBT,
            workspace_path=client.workspace_path,
            worktree_path=None,
        )
        assert _resolve_resume_cwd(session, client) == client.workspace_path

    def test_daemon_origin_worktree_set_proceeds(self, tmp_path: Path) -> None:
        """DAEMON-origin with a worktree set → returns worktree, no guard/raise."""
        client = self._client(tmp_path)
        wt = tmp_path / "wt-daemon"
        wt.mkdir()
        session = self._session(
            origin=SessionOrigin.DAEMON,
            purpose=SessionPurpose.IMPL,
            workspace_path=client.workspace_path,
            worktree_path=wt,
        )
        assert _resolve_resume_cwd(session, client) == wt

    def test_daemon_worktree_none_blocks_spawn_via_resume_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """resume_session on a corrupted DAEMON session raises before spawn_bg."""
        workspace = tmp_path / "main-checkout"
        workspace.mkdir(parents=True, exist_ok=True)
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  test-client:\n    workspace_path: {workspace}\n"
        )
        monkeypatch.setattr("cw.session._attach_session", _noop)

        state = CwState(
            sessions=[
                Session(
                    id="sess940b",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=workspace,
                    worktree_path=None,
                    surface_ref="deadbeef",  # not live → dead-surface respawn path
                )
            ]
        )
        save_state(state)

        with pytest.raises(CwError, match="worktree_path"):
            resume_session("test-client/impl", native_daemon=mock_native_daemon)

        assert mock_native_daemon.spawn_calls == []
