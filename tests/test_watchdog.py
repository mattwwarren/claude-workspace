"""Tests for cw.watchdog (RFC 0008 capstone, GitHub #1015)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cw.config import save_state, state_dir
from cw.dev_queue import save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.watchdog import (
    WatchdogStatus,
    _resolve_cw_executable_path,
    generate_launchd_plist_text,
    generate_systemd_service_text,
    generate_systemd_timer_text,
    install,
    launchd_plist_path,
    run_tick,
    status,
    systemd_service_path,
    systemd_timer_path,
    uninstall,
)

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _make_task(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    status_val: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    disposition: str | None = None,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id, client=client, status=status_val, disposition=disposition
    )


def _make_session(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    session_id: str = "sess-1",
    last_result: dict[str, object] | None = None,
    consecutive_salvage_skips: int = 0,
) -> Session:
    return Session(
        id=session_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        surface_ref="surf-1",
        last_result=last_result,
        consecutive_salvage_skips=consecutive_salvage_skips,
    )


@pytest.fixture(autouse=True)
def mock_desktop_notification(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("cw.watchdog.send_desktop_notification", mock)
    return mock


class TestEscalationCheck:
    def test_newly_escalated_ticket_fires_notification_and_logs(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        from cw.reconcile.escalation import ESCALATION_PARK_MINUTES

        task = _make_task(disposition="plan_pending_approval")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))
        # Prime the latch (tick 1 stamps escalation_parked_at).
        run_tick(now=_NOW)
        mock_desktop_notification.reset_mock()

        result = run_tick(now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES))

        assert result.escalated_ticket_ids == ["GEN-1"]
        mock_desktop_notification.assert_called_once()
        log_path = state_dir() / "watchdog.log"
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert any(json.loads(line)["check"] == "escalation" for line in lines)

    def test_healthy_tick_writes_no_log(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        run_tick(now=_NOW)

        log_path = state_dir() / "watchdog.log"
        assert not log_path.exists()
        mock_desktop_notification.assert_not_called()


class TestDispatchLivenessCheck:
    def test_no_events_at_all_is_not_dead(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        """Zero dispatch.tick events ever is 'no evidence', not 'dead'."""
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        result = run_tick(now=_NOW)

        assert result.dispatch_loop_dead is False
        mock_desktop_notification.assert_not_called()

    def test_recent_tick_is_alive(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        record_event(OrchestratorEventType.DISPATCH_TICK, {})
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        result = run_tick(now=datetime.now(UTC))

        assert result.dispatch_loop_dead is False

    def test_stale_tick_is_dead(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        import freezegun

        with freezegun.freeze_time(_NOW - timedelta(hours=2)):
            record_event(OrchestratorEventType.DISPATCH_TICK, {})
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        result = run_tick(now=_NOW)

        assert result.dispatch_loop_dead is True
        assert mock_desktop_notification.call_count == 1
        log_path = state_dir() / "watchdog.log"
        lines = log_path.read_text().splitlines()
        assert any(json.loads(line)["check"] == "dispatch_liveness" for line in lines)

    def test_threshold_scales_with_tick_interval(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        """A slower configured tick_interval_seconds raises the dead threshold."""
        import freezegun

        with freezegun.freeze_time(_NOW - timedelta(minutes=20)):
            record_event(OrchestratorEventType.DISPATCH_TICK, {})
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        # tick_interval_seconds=600 -> threshold = max(600*4, 600) = 2400s = 40m.
        # 20m stale must NOT be flagged dead under this slower config.
        result = run_tick(
            now=_NOW, config=OrchestratorConfig(tick_interval_seconds=600)
        )

        assert result.dispatch_loop_dead is False


class TestParkMarkerCyclingCheck:
    def test_cycling_ticket_detected_and_notified(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        task = _make_task(disposition=None)
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=5,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        result = run_tick(
            now=_NOW, config=OrchestratorConfig(salvage_skip_attention_threshold=5)
        )

        assert result.cycling_ticket_ids == ["GEN-1"]
        assert mock_desktop_notification.call_count == 1
        log_path = state_dir() / "watchdog.log"
        lines = log_path.read_text().splitlines()
        assert any(json.loads(line)["check"] == "park_marker_cycling" for line in lines)

    def test_below_threshold_not_flagged(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        task = _make_task(disposition=None)
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        result = run_tick(
            now=_NOW, config=OrchestratorConfig(salvage_skip_attention_threshold=5)
        )

        assert result.cycling_ticket_ids == []

    def test_no_park_marker_not_flagged(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        task = _make_task(disposition=None)
        session = _make_session(last_result=None, consecutive_salvage_skips=10)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        result = run_tick(now=_NOW)

        assert result.cycling_ticket_ids == []


class TestLogAppendOnlyOnDetection:
    def test_multiple_ticks_accumulate_only_on_detection(
        self, tmp_config_dir: Path, mock_desktop_notification: MagicMock
    ) -> None:
        from cw.reconcile.escalation import ESCALATION_PARK_MINUTES

        task = _make_task(disposition="plan_pending_approval")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        run_tick(now=_NOW)  # tick 1: parks the latch, no fire yet, no log
        log_path = state_dir() / "watchdog.log"
        assert not log_path.exists()

        run_tick(now=_NOW + timedelta(minutes=1))  # tick 2: still not due, no log
        assert not log_path.exists()

        run_tick(now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES))  # fires
        assert log_path.exists()
        assert len(log_path.read_text().splitlines()) == 1

        run_tick(
            now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES + 10)
        )  # latched, no re-fire
        assert len(log_path.read_text().splitlines()) == 1


class TestInstallUninstallStatusLinux:
    def test_install_writes_service_and_timer(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        xdg = tmp_config_dir / "xdg-config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.setattr("cw.watchdog.sys.argv", ["pytest"])
        monkeypatch.setattr(
            "cw.watchdog.shutil.which", lambda _name: "/usr/local/bin/cw"
        )

        paths = install()

        assert paths == [systemd_service_path(), systemd_timer_path()]
        for path in paths:
            assert path.exists()
        assert (
            "ExecStart=/usr/local/bin/cw watchdog tick"
            in systemd_service_path().read_text()
        )
        assert "OnUnitActiveSec=15min" in systemd_timer_path().read_text()

    def test_respects_xdg_config_home(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        xdg = tmp_config_dir / "custom-xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        expected_service = xdg / "systemd" / "user" / "cw-watchdog.service"
        expected_timer = xdg / "systemd" / "user" / "cw-watchdog.timer"
        assert systemd_service_path() == expected_service
        assert systemd_timer_path() == expected_timer

    def test_falls_back_to_home_config_when_xdg_unset(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr("cw.watchdog.Path.home", lambda: tmp_config_dir)

        assert systemd_service_path() == (
            tmp_config_dir / ".config" / "systemd" / "user" / "cw-watchdog.service"
        )

    def test_status_reflects_installed_state(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_config_dir / "xdg"))

        assert status() == WatchdogStatus(
            platform="linux",
            installed=False,
            paths=[str(systemd_service_path()), str(systemd_timer_path())],
        )

        install()

        assert status().installed is True

    def test_uninstall_removes_files(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_config_dir / "xdg"))
        install()

        removed = uninstall()

        assert removed == [systemd_service_path(), systemd_timer_path()]
        assert not systemd_service_path().exists()
        assert not systemd_timer_path().exists()

    def test_uninstall_when_not_installed_returns_empty(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_config_dir / "xdg"))

        assert uninstall() == []


class TestInstallUninstallStatusMacos:
    def test_install_writes_plist(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Darwin")
        monkeypatch.setattr("cw.watchdog.Path.home", lambda: tmp_config_dir)
        monkeypatch.setattr("cw.watchdog.sys.argv", ["pytest"])
        monkeypatch.setattr(
            "cw.watchdog.shutil.which", lambda _name: "/usr/local/bin/cw"
        )

        paths = install()

        assert paths == [launchd_plist_path()]
        assert launchd_plist_path().exists()
        text = launchd_plist_path().read_text()
        assert "com.cw.watchdog" in text
        assert "<integer>900</integer>" in text
        assert "<string>/usr/local/bin/cw</string>" in text

    def test_plist_path_under_library_launchagents(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.Path.home", lambda: tmp_config_dir)
        assert launchd_plist_path() == (
            tmp_config_dir / "Library" / "LaunchAgents" / "com.cw.watchdog.plist"
        )

    def test_status_and_uninstall(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.platform.system", lambda: "Darwin")
        monkeypatch.setattr("cw.watchdog.Path.home", lambda: tmp_config_dir)

        assert status().installed is False
        install()
        assert status().installed is True

        removed = uninstall()
        assert removed == [launchd_plist_path()]
        assert not launchd_plist_path().exists()


class TestUnitFileTextGeneration:
    def test_systemd_service_text_contains_expected_fields(self) -> None:
        text = generate_systemd_service_text("/usr/local/bin/cw")
        assert "[Service]" in text
        assert "Type=oneshot" in text
        assert "ExecStart=/usr/local/bin/cw watchdog tick" in text

    def test_systemd_timer_text_contains_expected_fields(self) -> None:
        text = generate_systemd_timer_text()
        assert "[Timer]" in text
        assert "OnUnitActiveSec=15min" in text
        assert "Unit=cw-watchdog.service" in text
        assert "WantedBy=timers.target" in text

    def test_launchd_plist_text_is_valid_xml_shape(self) -> None:
        text = generate_launchd_plist_text("/usr/local/bin/cw")
        assert text.startswith('<?xml version="1.0"')
        assert "<key>Label</key>" in text
        assert "<string>com.cw.watchdog</string>" in text
        assert "<key>StartInterval</key>" in text
        assert "        <string>/usr/local/bin/cw</string>" in text


class TestResolveCwExecutablePath:
    def test_prefers_argv0_when_it_names_cw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw_file = tmp_path / "cw"
        cw_file.write_text("#!/bin/sh\n")
        monkeypatch.setattr("cw.watchdog.sys.argv", [str(cw_file)])
        monkeypatch.setattr(
            "cw.watchdog.shutil.which", lambda _name: "/should/not/be/used"
        )

        result = _resolve_cw_executable_path()

        assert result == str(cw_file.resolve())

    def test_falls_back_to_which_when_argv0_not_cw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.sys.argv", ["/usr/bin/python3"])
        monkeypatch.setattr("cw.watchdog.shutil.which", lambda _name: "/opt/bin/cw")

        result = _resolve_cw_executable_path()

        assert result == "/opt/bin/cw"

    def test_falls_back_to_which_on_python_m_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.watchdog.sys.argv", ["/home/u/.venv/lib/cw/cli/__main__.py"]
        )
        monkeypatch.setattr("cw.watchdog.shutil.which", lambda _name: "/opt/bin/cw")

        result = _resolve_cw_executable_path()

        assert result == "/opt/bin/cw"

    def test_raises_when_neither_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.sys.argv", ["/usr/bin/python3"])
        monkeypatch.setattr("cw.watchdog.shutil.which", lambda _name: None)

        with pytest.raises(CwError) as exc_info:
            _resolve_cw_executable_path()

        assert "cw" in str(exc_info.value)
        assert "PATH" in str(exc_info.value)
