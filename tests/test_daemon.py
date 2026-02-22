"""Tests for cw.daemon - background daemon management."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from cw.daemon import (
    _DAEMON_TASK_TIMEOUT_S,
    _async_process_item,
    _async_wait_for_completion,
    _ensure_not_running,
    _get_injectable_session,
    _inject_into_session,
    _inject_via_trigger,
    _is_process_alive,
    _pid_path,
    _reap_done_tasks,
    _rebackground_session,
    _spawn_new_tasks,
    _wait_for_completion,
    _wait_for_idle_event,
    daemon_status,
    stop_daemon,
)
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CwState,
    QueueItem,
    Session,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)


@pytest.fixture
def tmp_daemons_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DAEMONS_DIR to an isolated tmp_path location."""
    daemons_dir = tmp_path / "daemons"
    daemons_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.DAEMONS_DIR", daemons_dir)
    monkeypatch.setattr("cw.daemon.DAEMONS_DIR", daemons_dir)
    return daemons_dir


class TestPidPath:
    def test_returns_correct_path(self, tmp_daemons_dir: Path) -> None:
        result = _pid_path("acme", "debt")
        assert result == tmp_daemons_dir / "acme__debt.pid"

    def test_includes_client_and_purpose_in_name(self, tmp_daemons_dir: Path) -> None:
        result = _pid_path("my-client", "idea")
        assert result.name == "my-client__idea.pid"
        assert result.parent == tmp_daemons_dir

    def test_different_clients_produce_different_paths(
        self, tmp_daemons_dir: Path
    ) -> None:
        path_a = _pid_path("client-a", "debt")
        path_b = _pid_path("client-b", "debt")
        assert path_a != path_b

    def test_different_purposes_produce_different_paths(
        self, tmp_daemons_dir: Path
    ) -> None:
        path_debt = _pid_path("acme", "debt")
        path_idea = _pid_path("acme", "idea")
        assert path_debt != path_idea


class TestIsProcessAlive:
    def test_returns_true_for_current_process(self, tmp_daemons_dir: Path) -> None:
        assert _is_process_alive(os.getpid()) is True

    def test_returns_false_for_nonexistent_pid(self, tmp_daemons_dir: Path) -> None:
        # PID 0 is the kernel scheduler and can't be signaled by userspace;
        # use a very large PID that is almost certainly unallocated.
        # os.kill raises ProcessLookupError for truly nonexistent PIDs.
        nonexistent_pid = 999_999_999
        assert _is_process_alive(nonexistent_pid) is False

    def test_returns_true_on_permission_error(self, tmp_daemons_dir: Path) -> None:
        with patch("os.kill", side_effect=PermissionError):
            assert _is_process_alive(1) is True

    def test_returns_false_on_process_lookup_error(
        self, tmp_daemons_dir: Path
    ) -> None:
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive(99999) is False


class TestEnsureNotRunning:
    def test_does_nothing_when_no_pid_file(self, tmp_daemons_dir: Path) -> None:
        # Should not raise
        _ensure_not_running("acme", "debt")

    def test_raises_when_daemon_is_running(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text(str(os.getpid()))

        with pytest.raises(CwError, match="Daemon already running for acme/debt"):
            _ensure_not_running("acme", "debt")

    def test_error_message_includes_pid(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text(str(current_pid))

        with pytest.raises(CwError, match=str(current_pid)):
            _ensure_not_running("acme", "debt")

    def test_cleans_stale_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        # Write a PID for a process that doesn't exist
        with patch("cw.daemon._is_process_alive", return_value=False):
            pid_file.write_text("99999")
            _ensure_not_running("acme", "debt")

        assert not pid_file.exists()

    def test_cleans_corrupt_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text("not-a-number")

        # Should not raise; corrupt file gets cleaned up
        _ensure_not_running("acme", "debt")
        assert not pid_file.exists()

    def test_cleans_empty_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text("")

        _ensure_not_running("acme", "debt")
        assert not pid_file.exists()


class TestStopDaemon:
    def test_raises_when_no_pid_file(self, tmp_daemons_dir: Path) -> None:
        with pytest.raises(CwError, match="No daemon running for acme/debt"):
            stop_daemon("acme", "debt")

    def test_raises_with_correct_client_and_purpose(
        self, tmp_daemons_dir: Path
    ) -> None:
        with pytest.raises(CwError, match="No daemon running for my-client/idea"):
            stop_daemon("my-client", "idea")

    def test_sends_sigterm_to_running_process(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        fake_pid = 12345
        pid_file.write_text(str(fake_pid))

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        with (
            patch("cw.daemon._is_process_alive", return_value=True),
            patch("os.kill", side_effect=fake_kill),
        ):
            stop_daemon("acme", "debt")

        assert len(kill_calls) == 1
        assert kill_calls[0] == (fake_pid, signal.SIGTERM)

    def test_cleans_stale_pid_file_without_killing(
        self, tmp_daemons_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text("99999")

        with patch("cw.daemon._is_process_alive", return_value=False):
            stop_daemon("acme", "debt")

        assert not pid_file.exists()
        output = capsys.readouterr().out
        assert "stale" in output.lower() or "not running" in output.lower()

    def test_default_purpose_is_debt(self, tmp_daemons_dir: Path) -> None:
        # No PID file for debt → raises with "debt" in message
        with pytest.raises(CwError, match="No daemon running for acme/debt"):
            stop_daemon("acme")

    def test_raises_on_invalid_pid_file_content(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text("not-a-number")

        with pytest.raises(CwError, match="Invalid PID file"):
            stop_daemon("acme", "debt")


class TestDaemonStatus:
    def test_returns_empty_when_no_pid_files(self, tmp_daemons_dir: Path) -> None:
        result = daemon_status()
        assert result == []

    def test_returns_empty_when_client_filter_has_no_match(
        self, tmp_daemons_dir: Path
    ) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text(str(os.getpid()))

        result = daemon_status(client="other-client")
        assert result == []

    def test_returns_daemon_info_from_pid_file(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text(str(current_pid))

        result = daemon_status()

        assert len(result) == 1
        entry = result[0]
        assert entry["client"] == "acme"
        assert entry["purpose"] == "debt"
        assert entry["pid"] == current_pid
        assert entry["alive"] is True

    def test_alive_false_for_dead_process(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme__debt.pid"
        pid_file.write_text("99999")

        with patch("cw.daemon._is_process_alive", return_value=False):
            result = daemon_status()

        assert len(result) == 1
        assert result[0]["alive"] is False

    def test_filters_by_client(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme__debt.pid").write_text(str(os.getpid()))
        (tmp_daemons_dir / "beta__debt.pid").write_text(str(os.getpid()))

        result = daemon_status(client="acme")

        assert len(result) == 1
        assert result[0]["client"] == "acme"

    def test_returns_all_clients_when_no_filter(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme__debt.pid").write_text(str(os.getpid()))
        (tmp_daemons_dir / "beta__idea.pid").write_text(str(os.getpid()))

        result = daemon_status()

        assert len(result) == 2
        clients = {entry["client"] for entry in result}
        assert clients == {"acme", "beta"}

    def test_skips_malformed_pid_files(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme__debt.pid").write_text("not-a-pid")

        result = daemon_status()

        assert result == []

    def test_skips_pid_files_with_malformed_stem(
        self, tmp_daemons_dir: Path
    ) -> None:
        # A file whose stem can't be split by __ into exactly two parts
        (tmp_daemons_dir / "noseparator.pid").write_text(str(os.getpid()))

        result = daemon_status()

        assert result == []

    def test_multiple_daemons_same_client(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        (tmp_daemons_dir / "acme__debt.pid").write_text(str(current_pid))
        (tmp_daemons_dir / "acme__idea.pid").write_text(str(current_pid))

        result = daemon_status(client="acme")

        assert len(result) == 2
        purposes = {entry["purpose"] for entry in result}
        assert purposes == {"debt", "idea"}

    def test_creates_daemons_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemons_dir = tmp_path / "new" / "daemons"
        monkeypatch.setattr("cw.daemon.DAEMONS_DIR", daemons_dir)

        result = daemon_status()

        assert daemons_dir.exists()
        assert result == []


def _make_queue_item(client: str = "test-client") -> QueueItem:
    """Create a QueueItem for testing."""
    return QueueItem(
        id="item01",
        client=client,
        task=TaskSpec(
            description="Fix lint issues",
            purpose=SessionPurpose.DEBT,
            prompt="Run ruff and fix violations.",
        ),
    )


def _make_client(tmp_path: Path) -> ClientConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ClientConfig(name="test-client", workspace_path=workspace)


def _make_session(
    workspace_path: Path,
    client: str = "test-client",
    purpose: SessionPurpose = SessionPurpose.DEBT,
    status: SessionStatus = SessionStatus.BACKGROUNDED,
) -> Session:
    return Session(
        id="sess-debt",
        name=f"{client}/{purpose.value}",
        client=client,
        purpose=purpose,
        status=status,
        workspace_path=workspace_path,
        zellij_pane=purpose.value,
        zellij_tab=client,
    )


class TestGetInjectableSession:
    def test_returns_session_and_state(self, tmp_path: Path) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])

        with patch("cw.daemon.load_state", return_value=state):
            found, returned_state = _get_injectable_session(
                "test-client", "debt",
            )
        assert found is session
        assert returned_state is state

    def test_raises_on_active(self, tmp_path: Path) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.ACTIVE,
        )
        state = CwState(sessions=[session])

        with (
            patch("cw.daemon.load_state", return_value=state),
            pytest.raises(CwError, match="not injectable"),
        ):
            _get_injectable_session("test-client", "debt")

    def test_accepts_idle_session(self, tmp_path: Path) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.IDLE,
        )
        state = CwState(sessions=[session])

        with patch("cw.daemon.load_state", return_value=state):
            found, returned_state = _get_injectable_session(
                "test-client", "debt",
            )
        assert found is session
        assert returned_state is state

    def test_raises_on_missing(self, tmp_path: Path) -> None:
        state = CwState(sessions=[])

        with (
            patch("cw.daemon.load_state", return_value=state),
            pytest.raises(CwError, match="No debt session"),
        ):
            _get_injectable_session("test-client", "debt")

    def test_raises_on_completed(self, tmp_path: Path) -> None:
        """find_session filters out COMPLETED, so they appear as missing."""
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.COMPLETED,
        )
        state = CwState(sessions=[session])

        with (
            patch("cw.daemon.load_state", return_value=state),
            pytest.raises(CwError, match="No debt session"),
        ):
            _get_injectable_session("test-client", "debt")

    def test_auto_bootstrap_false_raises_on_missing(
        self, tmp_path: Path,
    ) -> None:
        state = CwState(sessions=[])
        with (
            patch("cw.daemon.load_state", return_value=state),
            pytest.raises(CwError, match="No debt session"),
        ):
            _get_injectable_session(
                "test-client", "debt", auto_bootstrap=False,
            )

    def test_auto_bootstrap_calls_start_session(
        self, tmp_path: Path,
    ) -> None:
        """When auto_bootstrap=True and no session exists,
        start_session is called and we poll for BACKGROUNDED."""
        bg_session = _make_session(
            tmp_path / "workspace",
            status=SessionStatus.BACKGROUNDED,
        )
        state_empty = CwState(sessions=[])
        state_with_bg = CwState(sessions=[bg_session])

        # load_state is called: 1) initial check, 2) poll iteration,
        # 3) final reload after bootstrap returns
        def _load_state_side_effect() -> CwState:
            if _load_state_side_effect.call_count == 0:
                _load_state_side_effect.call_count += 1
                return state_empty
            return state_with_bg

        _load_state_side_effect.call_count = 0  # type: ignore[attr-defined]

        with (
            patch(
                "cw.daemon.load_state",
                side_effect=_load_state_side_effect,
            ),
            patch("cw.daemon.start_session") as mock_start,
            patch("cw.daemon.time.sleep"),
        ):
            session, _state = _get_injectable_session(
                "test-client", "debt", auto_bootstrap=True,
            )
            mock_start.assert_called_once_with("test-client", "debt")
            assert session.status == SessionStatus.BACKGROUNDED

    def test_auto_bootstrap_timeout_raises(
        self, tmp_path: Path,
    ) -> None:
        """When bootstrapped session never becomes BACKGROUNDED, timeout."""
        state_empty = CwState(sessions=[])

        with (
            patch("cw.daemon.load_state", return_value=state_empty),
            patch("cw.daemon.start_session"),
            patch("cw.daemon.time.sleep"),
            pytest.raises(CwError, match="Timed out"),
        ):
            _get_injectable_session(
                "test-client", "debt", auto_bootstrap=True,
            )


class TestInjectIntoSession:
    def test_writes_to_pane_for_backgrounded(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[tuple[object, ...]]],
    ) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.time.sleep"),
        ):
            _inject_into_session(client_config, _make_queue_item(), "debt")

        # Should have written twice: resume command + workflow prompt
        assert len(mock_zellij["write_to_pane"]) == 2
        resume_call = mock_zellij["write_to_pane"][0][0]
        assert "claude --resume" in resume_call
        workflow_call = mock_zellij["write_to_pane"][1][0]
        assert "daemon queue system" in workflow_call

    def test_writes_trigger_for_idle(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.IDLE,
        )
        state = CwState(sessions=[session])

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
        ):
            _inject_into_session(client_config, _make_queue_item(), "debt")

        # Trigger file should exist with workflow prompt
        trigger = events_dir / "test-client__debt.trigger"
        assert trigger.exists()
        payload = json.loads(trigger.read_text())
        assert "--append-system-prompt" in payload["claude_args"]

    def test_sets_active_before_zellij_io(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_zellij: object,
    ) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])

        saved_statuses: list[str] = []

        def capture_save(_s: object) -> None:
            saved_statuses.append(session.status)

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state", side_effect=capture_save),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.time.sleep"),
        ):
            _inject_into_session(client_config, _make_queue_item(), "debt")

        # Status was saved as ACTIVE (before Zellij IO)
        assert saved_statuses[0] == SessionStatus.ACTIVE


class TestRebackgroundSession:
    def test_rebackgrounds_active_session(self, tmp_path: Path) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.ACTIVE,
        )
        state = CwState(sessions=[session])

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
        ):
            _rebackground_session("test-client", "debt")

        assert session.status == SessionStatus.BACKGROUNDED

    def test_noop_if_already_backgrounded(self, tmp_path: Path) -> None:
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])

        save_calls: list[object] = []

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state", side_effect=save_calls.append),
        ):
            _rebackground_session("test-client", "debt")

        assert session.status == SessionStatus.BACKGROUNDED
        assert len(save_calls) == 0

    def test_noop_if_no_session(self, tmp_path: Path) -> None:
        state = CwState(sessions=[])
        with patch("cw.daemon.load_state", return_value=state):
            _rebackground_session("test-client", "debt")  # No error


class TestDaemonTaskTimeout:
    def test_constant_is_1800(self) -> None:
        assert _DAEMON_TASK_TIMEOUT_S == 1800


class TestWaitForCompletion:
    def test_returns_handoff_when_found(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        before = time.time()
        time.sleep(0.05)
        handoff = handoffs_dir / "session-test.md"
        handoff.write_text("# Handoff\n")

        result = _wait_for_completion(
            tmp_path, before, timeout=5, poll_interval=0,
        )
        assert result is not None
        assert result.name == "session-test.md"

    def test_returns_none_on_timeout(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        result = _wait_for_completion(
            tmp_path, time.time() + 9999, timeout=0, poll_interval=0,
        )
        assert result is None

    def test_returns_none_when_shutdown_event_pre_set(
        self, tmp_path: Path,
    ) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        event = threading.Event()
        event.set()

        result = _wait_for_completion(
            tmp_path, 0, timeout=60, poll_interval=1,
            shutdown_event=event,
        )
        assert result is None

    def test_returns_promptly_when_event_set_mid_wait(
        self, tmp_path: Path,
    ) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        event = threading.Event()

        def _set_after_delay() -> None:
            time.sleep(0.1)
            event.set()

        t = threading.Thread(target=_set_after_delay)
        t.start()

        start = time.time()
        result = _wait_for_completion(
            tmp_path, time.time() + 9999, timeout=30, poll_interval=30,
            shutdown_event=event,
        )
        elapsed = time.time() - start
        t.join()

        assert result is None
        assert elapsed < 5, f"Took {elapsed}s — should have returned promptly"


class TestReapDoneTasks:
    def test_removes_completed_tasks(self) -> None:
        async def _run() -> None:
            async def _noop() -> None:
                pass

            task = asyncio.create_task(_noop())
            await task

            active: dict[tuple[str, str], asyncio.Task[None]] = {
                ("client", "debt"): task,
            }
            _reap_done_tasks(active)
            assert ("client", "debt") not in active

        asyncio.run(_run())

    def test_keeps_running_tasks(self) -> None:
        async def _run() -> None:
            pending_future: asyncio.Future[None] = asyncio.Future()

            async def _block() -> None:
                await pending_future

            task = asyncio.create_task(_block())

            active: dict[tuple[str, str], asyncio.Task[None]] = {
                ("client", "debt"): task,
            }
            _reap_done_tasks(active)
            assert ("client", "debt") in active

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_run())


class TestSpawnNewTasks:
    def test_spawns_no_tasks_when_no_work(self) -> None:
        async def _run() -> None:
            event = asyncio.Event()
            active: dict[tuple[str, str], asyncio.Task[None]] = {}
            with (
                patch("cw.daemon.load_clients", return_value={"acme": {}}),
                patch("cw.daemon.claim_next", return_value=None),
            ):
                _spawn_new_tasks(active, event)
            assert len(active) == 0

        asyncio.run(_run())

    def test_spawns_task_for_claimed_item(self, tmp_path: Path) -> None:
        async def _run() -> None:
            event = asyncio.Event()
            active: dict[tuple[str, str], asyncio.Task[None]] = {}
            item = _make_queue_item()
            client_config = _make_client(tmp_path)

            with (
                patch("cw.daemon.load_clients",
                      return_value={"test-client": {}}),
                patch("cw.daemon.claim_next", return_value=item),
                patch("cw.daemon.get_client", return_value=client_config),
            ):
                _spawn_new_tasks(active, event)

            assert ("test-client", "debt") in active
            # Cancel to avoid warnings about unfinished tasks.
            active[("test-client", "debt")].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active[("test-client", "debt")]

        asyncio.run(_run())

    def test_skips_busy_purpose(self, tmp_path: Path) -> None:
        async def _run() -> None:
            event = asyncio.Event()
            # Simulate a running task for (test-client, debt).
            dummy: asyncio.Future[None] = asyncio.Future()

            async def _block() -> None:
                await dummy

            existing_task = asyncio.create_task(_block())
            active: dict[tuple[str, str], asyncio.Task[None]] = {
                ("test-client", "debt"): existing_task,
            }
            item = _make_queue_item()
            client_config = _make_client(tmp_path)

            with (
                patch("cw.daemon.load_clients",
                      return_value={"test-client": {}}),
                patch("cw.daemon.claim_next", return_value=item),
                patch("cw.daemon.get_client", return_value=client_config),
                patch("cw.daemon.fail_item") as mock_fail,
            ):
                _spawn_new_tasks(active, event)

            # Should NOT have replaced the existing task.
            assert active[("test-client", "debt")] is existing_task
            # Should have failed the item so it can be retried.
            mock_fail.assert_called_once_with(
                "test-client", "item01", "Purpose busy, will retry",
            )

            existing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await existing_task

        asyncio.run(_run())

    def test_stops_on_shutdown(self) -> None:
        async def _run() -> None:
            event = asyncio.Event()
            event.set()
            active: dict[tuple[str, str], asyncio.Task[None]] = {}
            with patch("cw.daemon.load_clients",
                       return_value={"acme": {}}) as mock_load:
                _spawn_new_tasks(active, event)
            mock_load.assert_called_once()
            assert len(active) == 0

        asyncio.run(_run())


class TestAsyncProcessItem:
    @pytest.mark.asyncio
    async def test_completes_item(
        self, tmp_path: Path,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[tuple[object, ...]]],
    ) -> None:
        item = _make_queue_item()
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])
        event = asyncio.Event()

        handoffs_dir = tmp_path / "workspace" / ".handoffs"
        handoffs_dir.mkdir(parents=True)
        handoff_file = handoffs_dir / "session-done.md"
        handoff_file.write_text("# Done\n")

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.time.sleep"),
            patch(
                "cw.daemon.find_handoffs_newer_than",
                return_value=[handoff_file],
            ),
            patch("cw.daemon.parse_handoff_reason", return_value=None),
            patch("cw.daemon.complete_item") as mock_complete,
            patch("cw.daemon._rebackground_session"),
        ):
            await _async_process_item(client_config, item, "debt", event)

        mock_complete.assert_called_once_with(
            "test-client", "item01", "Completed by daemon",
        )

    @pytest.mark.asyncio
    async def test_sends_notification_on_injection_failure(
        self, tmp_path: Path,
    ) -> None:
        item = _make_queue_item()
        client_config = _make_client(tmp_path)
        event = asyncio.Event()

        with (
            patch(
                "cw.daemon._inject_into_session",
                side_effect=CwError("No debt session"),
            ),
            patch("cw.daemon._rebackground_session"),
            patch("cw.daemon.fail_item"),
            patch("cw.daemon.send_notification") as mock_notify,
        ):
            await _async_process_item(client_config, item, "debt", event)

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "Daemon Item Failed"
        assert "No debt session" in call_args[0][1]
        assert call_args[1]["urgency"] == "critical"

    @pytest.mark.asyncio
    async def test_sends_notification_on_timeout(
        self, tmp_path: Path,
        tmp_config_dir: Path,
        mock_zellij: dict[str, list[tuple[object, ...]]],
    ) -> None:
        item = _make_queue_item()
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.BACKGROUNDED,
        )
        state = CwState(sessions=[session])
        event = asyncio.Event()

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.time.sleep"),
            patch(
                "cw.daemon._async_wait_for_completion",
                return_value=None,
            ),
            patch("cw.daemon._rebackground_session"),
            patch("cw.daemon.fail_item") as mock_fail,
            patch("cw.daemon.send_notification") as mock_notify,
        ):
            await _async_process_item(client_config, item, "debt", event)

        mock_fail.assert_called_once()
        mock_notify.assert_called_once()
        assert "Timed Out" in mock_notify.call_args[0][0]


class TestAsyncWaitForCompletion:
    @pytest.mark.asyncio
    async def test_returns_handoff_when_found(self, tmp_path: Path) -> None:
        handoff_file = tmp_path / "handoff.md"
        handoff_file.write_text("# Done\n")

        with patch(
            "cw.daemon.find_handoffs_newer_than",
            return_value=[handoff_file],
        ):
            result = await _async_wait_for_completion(
                tmp_path, 0.0, timeout=5, poll_interval=1,
            )
        assert result == handoff_file

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self, tmp_path: Path) -> None:
        with patch(
            "cw.daemon.find_handoffs_newer_than",
            return_value=[],
        ):
            result = await _async_wait_for_completion(
                tmp_path, 0.0, timeout=0, poll_interval=1,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_shutdown_set(
        self, tmp_path: Path,
    ) -> None:
        event = asyncio.Event()
        event.set()
        with patch(
            "cw.daemon.find_handoffs_newer_than",
            return_value=[],
        ):
            result = await _async_wait_for_completion(
                tmp_path, 0.0, timeout=60, poll_interval=1,
                shutdown_event=event,
            )
        assert result is None


class TestInjectViaTrigger:
    def test_writes_trigger_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        _inject_via_trigger("my-client", "debt", "Do the work.")

        trigger = events_dir / "my-client__debt.trigger"
        assert trigger.exists()

        payload = json.loads(trigger.read_text())
        assert payload["claude_args"] == [
            "--append-system-prompt", "Do the work.",
        ]

    def test_creates_events_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "new" / "events"
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        _inject_via_trigger("c", "impl", "prompt")

        assert events_dir.exists()


class TestWaitForIdleEvent:
    @pytest.mark.asyncio
    async def test_returns_payload_when_signal_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        signal_file = events_dir / "c__debt.idle"
        signal_file.write_text(json.dumps({
            "session_id": "s1", "exit_code": 0,
        }))

        result = await _wait_for_idle_event(
            "c", "debt", timeout=5, poll_interval=0.1,
        )
        assert result is not None
        assert result["exit_code"] == 0
        assert not signal_file.exists()  # Consumed

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        result = await _wait_for_idle_event(
            "c", "debt", timeout=0.2, poll_interval=0.1,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_shutdown_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        event = asyncio.Event()
        event.set()

        result = await _wait_for_idle_event(
            "c", "debt", timeout=60, poll_interval=0.1,
            shutdown_event=event,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_malformed_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        signal_file = events_dir / "c__debt.idle"
        signal_file.write_text("not json{{{")

        result = await _wait_for_idle_event(
            "c", "debt", timeout=5, poll_interval=0.1,
        )
        assert result == {}
        assert not signal_file.exists()


class TestAsyncProcessItemIdlePath:
    """Tests for _async_process_item when session is IDLE (event-driven)."""

    @pytest.mark.asyncio
    async def test_completes_via_idle_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        item = _make_queue_item()
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.IDLE,
        )
        state = CwState(sessions=[session])
        event = asyncio.Event()

        idle_payload: dict[str, object] = {
            "session_id": "sess-debt", "exit_code": 0,
        }

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.build_daemon_workflow_prompt", return_value="work"),
            patch(
                "cw.daemon._wait_for_idle_event",
                return_value=idle_payload,
            ),
            patch("cw.daemon.complete_item") as mock_complete,
        ):
            await _async_process_item(client_config, item, "debt", event)

        mock_complete.assert_called_once_with(
            "test-client", "item01", "Completed by daemon",
        )

    @pytest.mark.asyncio
    async def test_fails_on_nonzero_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setattr("cw.daemon.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.wrapper.EVENTS_DIR", events_dir)

        item = _make_queue_item()
        client_config = _make_client(tmp_path)
        session = _make_session(
            client_config.workspace_path,
            status=SessionStatus.IDLE,
        )
        state = CwState(sessions=[session])
        event = asyncio.Event()

        idle_payload: dict[str, object] = {
            "session_id": "sess-debt", "exit_code": 1,
        }

        with (
            patch("cw.daemon.load_state", return_value=state),
            patch("cw.daemon.save_state"),
            patch("cw.daemon.record_event"),
            patch("cw.daemon.build_daemon_workflow_prompt", return_value="work"),
            patch(
                "cw.daemon._wait_for_idle_event",
                return_value=idle_payload,
            ),
            patch("cw.daemon.fail_item") as mock_fail,
        ):
            await _async_process_item(client_config, item, "debt", event)

        mock_fail.assert_called_once()
        assert "exited with code 1" in mock_fail.call_args[0][2]
