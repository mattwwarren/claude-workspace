"""Tests for cw.daemon — PR event watcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from cw.daemon import (
    ThrottleStore,
    WatcherSnapshot,
    _post_to_channel,
    watch_prs_for_client,
)
from cw.models import (
    ClientConfig,
    CwState,
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

        def fake_urlopen(req: Any, timeout: int = 0) -> MagicMock:
            captured.append(json.loads(req.data))
            return MagicMock()

        with patch("urllib.request.urlopen", fake_urlopen):
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

        def fake_urlopen(req: Any, timeout: int = 0) -> MagicMock:
            captured.append(json.loads(req.data))
            return MagicMock()

        with patch("urllib.request.urlopen", fake_urlopen):
            _post_to_channel(
                "ci_failed", "owner/repo", 7, {"findings_count": 3, "role": "author"}
            )

        assert captured[0]["event_type"] == "ci_failed"
        assert captured[0]["pr_number"] == 7

    def test_review_received_posts_correct_payload(self) -> None:
        """review_received event_type sends correct JSON body."""
        captured: list[dict[str, Any]] = []

        def fake_urlopen(req: Any, timeout: int = 0) -> MagicMock:
            captured.append(json.loads(req.data))
            return MagicMock()

        with patch("urllib.request.urlopen", fake_urlopen):
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

        def fake_urlopen(req: Any, timeout: int = 0) -> MagicMock:
            captured.append(json.loads(req.data))
            return MagicMock()

        with patch("urllib.request.urlopen", fake_urlopen):
            _post_to_channel("mergeable", "owner/repo", 3, {"role": "author"})

        assert captured[0]["event_type"] == "mergeable"

    def test_channel_server_down_does_not_raise(self) -> None:
        """URLError from unreachable server is silently swallowed."""
        err_msg = "Connection refused"

        def raise_url_error(req: Any, timeout: int = 0) -> None:
            raise URLError(err_msg)

        # Must not raise — best-effort design
        with patch("urllib.request.urlopen", raise_url_error):
            _post_to_channel("merged", "owner/repo", 1, {})

    def test_cw_pr_events_url_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CW_PR_EVENTS_URL env var controls the POST destination."""
        custom_url = "http://custom-host:9999/pr-event"
        monkeypatch.setenv("CW_PR_EVENTS_URL", custom_url)
        captured_urls: list[str] = []

        def fake_urlopen(req: Any, timeout: int = 0) -> MagicMock:
            captured_urls.append(req.full_url)
            return MagicMock()

        with patch("urllib.request.urlopen", fake_urlopen):
            _post_to_channel("merged", "owner/repo", 1, {})

        assert captured_urls == [custom_url]
