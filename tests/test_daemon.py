"""Tests for cw.daemon — PR event watcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cw.daemon import (
    ThrottleStore,
    WatcherSnapshot,
    _post_to_channel,
    run_watcher_tick,
    watch_prs_for_client,
)
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorConfig,
    OrchestratorEventType,
    Session,
    SessionPurpose,
    SessionStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monitor_file(
    directory: Path,
    repo: str,
    pr_number: int,
    repo_path: str,
    *,
    status: str = "watching",
    role: str = "author",
    thread_status: dict[str, Any] | None = None,
    delta_findings: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a MonitorState JSON file to directory and return its path."""
    pr_key = f"{repo}#{pr_number}"
    pr_data: dict[str, Any] = {
        "role": role,
        "repo": repo,
        "repo_path": repo_path,
        "pr_number": pr_number,
        "last_seen_sha": "abc123",
        "status": status,
        "thread_status": thread_status or {},
        "delta_findings": delta_findings or [],
    }
    state: dict[str, Any] = {
        "active": {pr_key: pr_data},
        "completed": {},
    }
    # filename: owner--repo.json
    filename = repo.replace("/", "--") + ".json"
    file_path = directory / filename
    file_path.write_text(json.dumps(state, indent=2))
    return file_path


@pytest.fixture
def tmp_review_monitor_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.REVIEW_MONITOR_DIR to a temp directory."""
    monitor_dir = tmp_path / "review-monitor"
    monitor_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.REVIEW_MONITOR_DIR", monitor_dir)
    return monitor_dir


@pytest.fixture
def tmp_pr_watcher_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.PR_WATCHER_DIR to a temp directory."""
    watcher_dir = tmp_path / "pr_watcher"
    watcher_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.PR_WATCHER_DIR", watcher_dir)
    return watcher_dir


@pytest.fixture
def tmp_state_dir_for_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.STATE_DIR and related paths to tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    events_dir = state_dir / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    return state_dir


@pytest.fixture
def workspace_path(tmp_path: Path) -> Path:
    """Create and return a fake workspace directory."""
    workspace = tmp_path / "workspace" / "my-project"
    workspace.mkdir(parents=True)
    return workspace


@pytest.fixture
def sample_client(workspace_path: Path) -> ClientConfig:
    """A ClientConfig pointing at workspace_path."""
    return ClientConfig(
        name="test-client",
        workspace_path=workspace_path,
        default_branch="main",
    )


# ---------------------------------------------------------------------------
# Tests: watch_prs_for_client
# ---------------------------------------------------------------------------


class TestNoPRsFound:
    def test_no_monitor_files(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Empty monitor dir → no events emitted."""
        snapshot = WatcherSnapshot()
        events, updated = watch_prs_for_client(sample_client, snapshot)
        assert events == []
        assert updated.pr_states == {}

    def test_monitor_file_different_repo_path(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        tmp_path: Path,
    ) -> None:
        """PRs from unrelated repo paths are ignored."""
        other_workspace = str(tmp_path / "other-workspace" / "other-project")
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/other-repo",
            42,
            other_workspace,
            status="complete",
        )
        snapshot = WatcherSnapshot()
        events, _ = watch_prs_for_client(sample_client, snapshot)
        assert events == []


class TestPRMerged:
    def test_pr_merged_event(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """A PR with status='complete' emits PR_MERGED."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            99,
            str(workspace_path),
            status="complete",
        )
        snapshot = WatcherSnapshot()
        events, updated = watch_prs_for_client(sample_client, snapshot)

        assert len(events) == 1
        assert events[0].type == OrchestratorEventType.PR_MERGED
        assert events[0].payload["pr_number"] == 99
        assert events[0].payload["repo"] == "owner/my-repo"
        assert updated.pr_states.get("owner/my-repo#99") == "complete"

    def test_pr_abandoned_emits_merged(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """A PR with status='abandoned' also emits PR_MERGED."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            55,
            str(workspace_path),
            status="abandoned",
        )
        snapshot = WatcherSnapshot()
        events, updated = watch_prs_for_client(sample_client, snapshot)

        assert len(events) == 1
        assert events[0].type == OrchestratorEventType.PR_MERGED
        assert updated.pr_states.get("owner/my-repo#55") == "abandoned"

    def test_no_duplicate_merged(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """Second call with same merged file emits nothing (snapshot updated)."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            99,
            str(workspace_path),
            status="complete",
        )
        snapshot = WatcherSnapshot()
        events_first, updated_snapshot = watch_prs_for_client(sample_client, snapshot)
        assert len(events_first) == 1

        # Second call with updated snapshot — no new event
        events_second, _ = watch_prs_for_client(sample_client, updated_snapshot)
        assert events_second == []


class TestCIFailed:
    def test_delta_findings_ci_failed(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """delta_findings with CI keyword → PR_CI_FAILED emitted."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            77,
            str(workspace_path),
            status="watching",
            delta_findings=[{"message": "CI check failed on tests"}],
        )
        snapshot = WatcherSnapshot()
        events, updated = watch_prs_for_client(sample_client, snapshot)

        ci_events = [e for e in events if e.type == OrchestratorEventType.PR_CI_FAILED]
        assert len(ci_events) == 1
        assert ci_events[0].payload["pr_number"] == 77
        assert "owner/my-repo#77" in updated.ci_fail_prs

    def test_no_duplicate_ci_failed(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """Second call with same CI failure → no duplicate event."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            77,
            str(workspace_path),
            status="watching",
            delta_findings=[{"message": "CI check failed"}],
        )
        snapshot = WatcherSnapshot()
        _, updated_snapshot = watch_prs_for_client(sample_client, snapshot)
        events_second, _ = watch_prs_for_client(sample_client, updated_snapshot)

        ci_events = [
            e for e in events_second if e.type == OrchestratorEventType.PR_CI_FAILED
        ]
        assert ci_events == []

    def test_non_ci_findings_no_event(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """delta_findings without CI keywords → no PR_CI_FAILED."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            33,
            str(workspace_path),
            status="watching",
            delta_findings=[{"message": "style nit: trailing whitespace"}],
        )
        snapshot = WatcherSnapshot()
        events, _ = watch_prs_for_client(sample_client, snapshot)

        ci_events = [e for e in events if e.type == OrchestratorEventType.PR_CI_FAILED]
        assert ci_events == []


class TestReviewReceived:
    def test_review_received_on_new_threads(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """New unresolved threads → PR_REVIEW_RECEIVED emitted."""
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            10,
            str(workspace_path),
            status="watching",
            thread_status={"t1": {"resolved": False}},
        )
        snapshot = WatcherSnapshot()
        events, updated = watch_prs_for_client(sample_client, snapshot)

        review_events = [
            e for e in events if e.type == OrchestratorEventType.PR_REVIEW_RECEIVED
        ]
        assert len(review_events) == 1
        assert "owner/my-repo#10" in updated.review_prs


class TestMergeable:
    def test_mergeable_after_threads_resolved(
        self,
        tmp_review_monitor_dir: Path,
        tmp_pr_watcher_dir: Path,
        tmp_state_dir_for_daemon: Path,
        sample_client: ClientConfig,
        workspace_path: Path,
    ) -> None:
        """All threads resolved after prior review → PR_MERGEABLE emitted."""
        # First tick: unresolved thread → PR_REVIEW_RECEIVED
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            20,
            str(workspace_path),
            status="watching",
            thread_status={"t1": {"resolved": False}},
        )
        snapshot = WatcherSnapshot()
        _, snapshot_after_review = watch_prs_for_client(sample_client, snapshot)
        assert "owner/my-repo#20" in snapshot_after_review.review_prs

        # Second tick: thread now resolved → PR_MERGEABLE
        _make_monitor_file(
            tmp_review_monitor_dir,
            "owner/my-repo",
            20,
            str(workspace_path),
            status="watching",
            thread_status={"t1": {"resolved": True}},
        )
        events_second, _ = watch_prs_for_client(sample_client, snapshot_after_review)

        mergeable_events = [
            e for e in events_second if e.type == OrchestratorEventType.PR_MERGEABLE
        ]
        assert len(mergeable_events) == 1


# ---------------------------------------------------------------------------
# Tests: ThrottleStore
# ---------------------------------------------------------------------------


class TestThrottleStore:
    def _make_active_session(self, session_id: str, workspace: Path) -> Session:
        return Session(
            id=session_id,
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
        )

    def _make_completed_session(self, session_id: str, workspace: Path) -> Session:
        return Session(
            id=session_id,
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.COMPLETED,
            workspace_path=workspace,
        )

    def test_not_throttled_when_empty(self) -> None:
        """No dispatches → is_throttled returns False."""
        store = ThrottleStore()
        state = CwState()
        assert not store.is_throttled("client", "owner/repo#1", "author", state)

    def test_throttled_when_active_session_exists(self, tmp_path: Path) -> None:
        """Marking dispatch and having active session → is_throttled True."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        store = ThrottleStore()
        state = CwState(sessions=[self._make_active_session("sess001", workspace)])
        store.mark_dispatched("client", "owner/repo#1", "author", "sess001")
        assert store.is_throttled("client", "owner/repo#1", "author", state)

    def test_not_throttled_after_clear_completed(self, tmp_path: Path) -> None:
        """After clear_completed removes completed session → is_throttled False."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        store = ThrottleStore()
        state = CwState(sessions=[self._make_completed_session("sess002", workspace)])
        store.mark_dispatched("client", "owner/repo#2", "author", "sess002")

        # Before clearing: session is completed but still in active_dispatches
        store.clear_completed(state)

        # After clearing, throttle entry is removed
        assert not store.is_throttled("client", "owner/repo#2", "author", state)

    def test_throttle_different_role_not_throttled(self, tmp_path: Path) -> None:
        """Different role → not throttled even if same repo+pr."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        store = ThrottleStore()
        state = CwState(sessions=[self._make_active_session("sess003", workspace)])
        store.mark_dispatched("client", "owner/repo#3", "author", "sess003")
        # "reviewer" role is different from "author"
        assert not store.is_throttled("client", "owner/repo#3", "reviewer", state)


# ---------------------------------------------------------------------------
# Tests: _post_to_channel
# ---------------------------------------------------------------------------


class TestChannelPosting:
    """Tests for the best-effort HTTP channel posting helper."""

    def test_merged_posts_correct_payload(self) -> None:
        """merged event_type sends correct JSON body to channel server."""
        captured: list[dict[str, Any]] = []
        mock_conn = MagicMock()
        mock_conn.request.side_effect = lambda _m, _p, body, _h: captured.append(
            json.loads(body)
        )

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            _post_to_channel(
                "merged",
                "owner/my-repo",
                42,
                {"status": "complete", "role": "author"},
            )

        assert len(captured) == 1
        assert captured[0]["event_type"] == "merged"
        assert captured[0]["repo"] == "owner/my-repo"
        assert captured[0]["pr_number"] == 42
        assert captured[0]["payload"] == {"status": "complete", "role": "author"}

    def test_ci_failed_posts_correct_payload(self) -> None:
        """ci_failed event_type sends correct JSON body to channel server."""
        captured: list[dict[str, Any]] = []
        mock_conn = MagicMock()
        mock_conn.request.side_effect = lambda _m, _p, body, _h: captured.append(
            json.loads(body)
        )

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            _post_to_channel(
                "ci_failed", "owner/repo", 7, {"findings_count": 3, "role": "author"}
            )

        assert captured[0]["event_type"] == "ci_failed"
        assert captured[0]["pr_number"] == 7

    def test_review_received_posts_correct_payload(self) -> None:
        """review_received event_type sends correct JSON body."""
        captured: list[dict[str, Any]] = []
        mock_conn = MagicMock()
        mock_conn.request.side_effect = lambda _m, _p, body, _h: captured.append(
            json.loads(body)
        )

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            _post_to_channel(
                "review_received",
                "owner/repo",
                5,
                {"unresolved_threads": 2, "role": "author"},
            )

        assert captured[0]["event_type"] == "review_received"
        assert captured[0]["payload"]["unresolved_threads"] == 2

    def test_mergeable_posts_correct_payload(self) -> None:
        """mergeable event_type sends correct JSON body."""
        captured: list[dict[str, Any]] = []
        mock_conn = MagicMock()
        mock_conn.request.side_effect = lambda _m, _p, body, _h: captured.append(
            json.loads(body)
        )

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            _post_to_channel("mergeable", "owner/repo", 3, {"role": "author"})

        assert captured[0]["event_type"] == "mergeable"

    def test_channel_server_down_does_not_raise(self) -> None:
        """OSError from unreachable server is silently swallowed."""
        mock_conn = MagicMock()
        mock_conn.request.side_effect = OSError("Connection refused")

        # Must not raise — best-effort design
        with patch("http.client.HTTPConnection", return_value=mock_conn):
            _post_to_channel("merged", "owner/repo", 1, {})

    def test_cw_pr_events_url_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CW_PR_EVENTS_URL env var controls the POST destination."""
        custom_url = "http://custom-host:9999/pr-event"
        monkeypatch.setenv("CW_PR_EVENTS_URL", custom_url)
        captured_netlocs: list[str] = []

        def fake_connection(netloc: str, timeout: int = 0) -> MagicMock:
            captured_netlocs.append(netloc)
            return MagicMock()

        with patch("http.client.HTTPConnection", fake_connection):
            _post_to_channel("merged", "owner/repo", 1, {})

        assert captured_netlocs == ["custom-host:9999"]


# ---------------------------------------------------------------------------
# run_watcher_tick regression guard
# ---------------------------------------------------------------------------


def test_run_watcher_tick_does_not_call_respond_to_pr_events() -> None:
    """respond_to_pr_events must NOT be imported or called in run_watcher_tick.

    The orchestrator skill (cw-orchestrator.md) replaced the in-daemon
    dispatch role of pr_responder.respond_to_pr_events(). This regression
    guard ensures the import stays removed.
    """
    import importlib

    import cw.daemon

    importlib.reload(cw.daemon)
    assert not hasattr(cw.daemon, "respond_to_pr_events"), (
        "respond_to_pr_events must not be imported in cw.daemon — "
        "the orchestrator skill replaced its dispatch role"
    )


# ---------------------------------------------------------------------------
# run_watcher_tick per-client guard tests (#390)
# ---------------------------------------------------------------------------


class TestRunWatcherTickPerClientGuard:
    """Per-client exception isolation and whole-tick guard in run_watcher_tick."""

    def _base_patches(
        self,
        tmp_path: Path,
        clients: dict[str, ClientConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Patch config accessors so run_watcher_tick(once=True) can run."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "events"
        events_dir.mkdir()
        watcher_dir = tmp_path / "pr_watcher"
        watcher_dir.mkdir()
        monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
        monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
        monkeypatch.setattr("cw.config.PR_WATCHER_DIR", watcher_dir)
        monitor_dir = tmp_path / "review-monitor"
        monitor_dir.mkdir()
        monkeypatch.setattr("cw.config.REVIEW_MONITOR_DIR", monitor_dir)

    def test_one_bad_client_others_still_watched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """watch_prs_for_client raising for one client must not skip others.

        Acceptance: a client whose watch raises does NOT stop the tick;
        the other clients still get watched.
        """
        workspace_a = tmp_path / "ws_a"
        workspace_a.mkdir()
        workspace_b = tmp_path / "ws_b"
        workspace_b.mkdir()
        client_a = ClientConfig(
            name="client-a", workspace_path=workspace_a, default_branch="main"
        )
        client_b = ClientConfig(
            name="client-b", workspace_path=workspace_b, default_branch="main"
        )
        clients = {"client-a": client_a, "client-b": client_b}
        self._base_patches(tmp_path, clients, monkeypatch)

        watched: list[str] = []

        def fake_watch(
            client: ClientConfig, snapshot: WatcherSnapshot
        ) -> tuple[list[Any], WatcherSnapshot]:
            watched.append(client.name)
            if client.name == "client-a":
                msg = "boom from client-a"
                raise RuntimeError(msg)
            return [], snapshot

        with (
            patch("cw.daemon.load_clients", return_value=clients),
            patch("cw.daemon.load_state", return_value=CwState()),
            patch(
                "cw.daemon.load_orchestrator_config", return_value=OrchestratorConfig()
            ),
            patch("cw.daemon.watch_prs_for_client", side_effect=fake_watch),
            patch("cw.daemon.clear_completed_pr_sessions"),
            patch("cw.daemon.retire_merged_prs"),
            patch("cw.daemon._load_snapshot", return_value=WatcherSnapshot()),
            patch("cw.daemon._save_snapshot"),
            patch("cw.daemon._load_throttle", return_value=ThrottleStore()),
            patch("cw.daemon._save_throttle"),
        ):
            run_watcher_tick(once=True)

        assert "client-a" in watched
        assert "client-b" in watched

    def test_bad_client_error_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Exception from watch_prs_for_client must be logged with the client name.

        Acceptance: the error is logged (not silently dropped).
        """
        import logging

        workspace = tmp_path / "ws"
        workspace.mkdir()
        client = ClientConfig(
            name="bad-client", workspace_path=workspace, default_branch="main"
        )
        clients = {"bad-client": client}
        self._base_patches(tmp_path, clients, monkeypatch)

        def bad_watch(
            _client: ClientConfig, _snapshot: WatcherSnapshot
        ) -> tuple[list[Any], WatcherSnapshot]:
            msg = "simulated watch failure"
            raise ValueError(msg)

        with (
            patch("cw.daemon.load_clients", return_value=clients),
            patch("cw.daemon.load_state", return_value=CwState()),
            patch(
                "cw.daemon.load_orchestrator_config", return_value=OrchestratorConfig()
            ),
            patch("cw.daemon.watch_prs_for_client", side_effect=bad_watch),
            patch("cw.daemon.clear_completed_pr_sessions"),
            patch("cw.daemon.retire_merged_prs"),
            patch("cw.daemon._load_snapshot", return_value=WatcherSnapshot()),
            patch("cw.daemon._save_snapshot"),
            patch("cw.daemon._load_throttle", return_value=ThrottleStore()),
            patch("cw.daemon._save_throttle"),
            caplog.at_level(logging.ERROR, logger="cw.daemon"),
        ):
            run_watcher_tick(once=True)

        # Error must be logged and include the client name
        assert any("bad-client" in r.getMessage() for r in caplog.records), (
            "Expected a log record mentioning 'bad-client'"
        )

    def test_whole_tick_guard_sleeps_and_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected outer-tick error must sleep and retry, not exit the loop.

        Acceptance: the whole-tick guard sleeps + retries rather than letting
        an unhandled exception propagate out of the while True loop.
        The loop must still be running (i.e. reach a second tick) after the error.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        client = ClientConfig(
            name="client-x", workspace_path=workspace, default_branch="main"
        )
        clients = {"client-x": client}
        self._base_patches(tmp_path, clients, monkeypatch)

        call_count = 0

        def load_clients_side_effect() -> dict[str, ClientConfig]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First tick: simulate an unexpected error in the tick body.
                msg = "unexpected outer tick error"
                raise RuntimeError(msg)
            if call_count >= 3:
                # Stop the loop after the second successful tick by raising
                # KeyboardInterrupt (exits while True without being caught by the
                # whole-tick guard, which only catches Exception).
                raise KeyboardInterrupt
            return clients

        sleep_calls: list[float] = []

        with (
            patch("cw.daemon.load_clients", side_effect=load_clients_side_effect),
            patch("cw.daemon.load_state", return_value=CwState()),
            patch(
                "cw.daemon.load_orchestrator_config", return_value=OrchestratorConfig()
            ),
            patch(
                "cw.daemon.watch_prs_for_client", return_value=([], WatcherSnapshot())
            ),
            patch("cw.daemon.clear_completed_pr_sessions"),
            patch("cw.daemon.retire_merged_prs"),
            patch("cw.daemon._load_snapshot", return_value=WatcherSnapshot()),
            patch("cw.daemon._save_snapshot"),
            patch("cw.daemon._load_throttle", return_value=ThrottleStore()),
            patch("cw.daemon._save_throttle"),
            patch("cw.daemon.time.sleep", side_effect=sleep_calls.append),
            pytest.raises(KeyboardInterrupt),
        ):
            run_watcher_tick()

        # The guard slept at least once (the error recovery sleep after call_count==1)
        assert len(sleep_calls) >= 1, "Expected at least one sleep call from tick guard"
        # The loop reached a second tick (call_count progressed to at least 2)
        assert call_count >= 2, (
            "Loop must retry after the whole-tick guard catches error"
        )
