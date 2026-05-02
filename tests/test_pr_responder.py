"""Tests for cw.pr_responder — PR event dispatch decision table."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.events import record_event
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEvent,
    OrchestratorEventType,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.pr_responder import (
    PRDispatchRecord,
    clear_completed_pr_sessions,
    respond_to_pr_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ci_failed_event(
    client_name: str,
    repo: str,
    pr_number: int,
    branch: str = "feat/fix-thing",
) -> OrchestratorEvent:
    """Record a PR_CI_FAILED event and return it."""
    return record_event(
        OrchestratorEventType.PR_CI_FAILED,
        {
            "client": client_name,
            "repo": repo,
            "pr_number": pr_number,
            "branch": branch,
        },
    )


def _make_review_received_event(
    client_name: str,
    repo: str,
    pr_number: int,
    branch: str = "feat/my-feature",
) -> OrchestratorEvent:
    """Record a PR_REVIEW_RECEIVED event and return it."""
    return record_event(
        OrchestratorEventType.PR_REVIEW_RECEIVED,
        {
            "client": client_name,
            "repo": repo,
            "pr_number": pr_number,
            "branch": branch,
        },
    )


def _make_mergeable_event(
    client_name: str,
    repo: str,
    pr_number: int,
) -> OrchestratorEvent:
    """Record a PR_MERGEABLE event and return it."""
    return record_event(
        OrchestratorEventType.PR_MERGEABLE,
        {
            "client": client_name,
            "repo": repo,
            "pr_number": pr_number,
        },
    )


def _write_client_config(tmp_config_dir: Path, name: str, workspace: Path) -> None:
    """Write a minimal clients.yaml entry for the given client."""
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    lines = [
        "clients:",
        f"  {name}:",
        f"    workspace_path: {workspace}",
        "    default_branch: main",
        "",
    ]
    clients_file.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> FakeCmuxAdapter:
    """Fresh FakeCmuxAdapter for each test."""
    return FakeCmuxAdapter()


@pytest.fixture
def client_workspace(make_git_repo: Callable[[str], Path]) -> Path:
    """A real git repo for the default test client.

    pr_responder now materialises the PR branch via ``create_worktree``,
    so the workspace must be a real git repo with at least one commit.
    """
    return make_git_repo("workspace/myproject")


@pytest.fixture
def configured_client(tmp_config_dir: Path, client_workspace: Path) -> ClientConfig:
    """Write clients.yaml and return the matching ClientConfig."""
    _write_client_config(tmp_config_dir, "myproject", client_workspace)
    return ClientConfig(
        name="myproject",
        workspace_path=client_workspace,
        default_branch="main",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCIFailedSpawnsFixCI:
    def test_ci_failed_spawns_fix_ci(
        self,
        configured_client: ClientConfig,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """A pr.ci_failed event spawns a session with /fix-ci <pr> in the command."""
        _make_ci_failed_event("myproject", "owner/repo", 42, "feat/ci-branch")

        spawned = respond_to_pr_events(adapter=adapter)

        assert spawned == 1
        assert len(adapter.calls["spawn"]) == 1
        _workspace, command, _surface = adapter.calls["spawn"][0]
        assert "/fix-ci 42" in command
        assert configured_client.name == "myproject"


class TestReviewReceivedSpawnsAddressReview:
    def test_review_received_spawns_address_review(
        self,
        configured_client: ClientConfig,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """A pr.review_received event spawns a session with /address-review <pr>."""
        _make_review_received_event("myproject", "owner/repo", 17, "feat/my-feature")

        spawned = respond_to_pr_events(adapter=adapter)

        assert spawned == 1
        assert len(adapter.calls["spawn"]) == 1
        _workspace, command, _surface = adapter.calls["spawn"][0]
        assert "/address-review 17" in command
        assert configured_client.name == "myproject"


class TestThrottlePreventsDuplicateSpawn:
    def test_throttle_prevents_double_spawn(
        self,
        configured_client: ClientConfig,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """Second pr.ci_failed for same PR does not spawn a second session."""
        # First event — should spawn
        _make_ci_failed_event("myproject", "owner/repo", 99, "feat/branch")
        spawned_first = respond_to_pr_events(adapter=adapter)
        assert spawned_first == 1
        assert len(adapter.calls["spawn"]) == 1

        # Second event for same PR — session still active, should be throttled
        _make_ci_failed_event("myproject", "owner/repo", 99, "feat/branch")
        spawned_second = respond_to_pr_events(adapter=adapter)
        assert spawned_second == 0
        assert len(adapter.calls["spawn"]) == 1  # no additional spawn

        assert configured_client.name == "myproject"


class TestMergeableNoSpawn:
    def test_mergeable_no_spawn(
        self,
        configured_client: ClientConfig,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """A pr.mergeable event produces no spawn and advances the cursor."""
        _make_mergeable_event("myproject", "owner/repo", 5)

        spawned = respond_to_pr_events(adapter=adapter)

        assert spawned == 0
        assert adapter.calls["spawn"] == []

        # Calling again should produce 0 (cursor was advanced)
        spawned_again = respond_to_pr_events(adapter=adapter)
        assert spawned_again == 0

        assert configured_client.name == "myproject"


class TestMergedNoSpawn:
    def test_merged_no_spawn(
        self,
        configured_client: ClientConfig,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """A pr.merged event produces no spawn and advances the cursor."""
        record_event(
            OrchestratorEventType.PR_MERGED,
            {"client": "myproject", "repo": "owner/repo", "pr_number": 7},
        )

        spawned = respond_to_pr_events(adapter=adapter)
        assert spawned == 0
        assert adapter.calls["spawn"] == []

        assert configured_client.name == "myproject"


class TestClearCompletedRemovesRecord:
    def test_clear_completed_removes_record(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A completed session's dispatch record is removed by clear_completed."""
        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Set up a PRDispatchRecord with an active dispatch key
        dispatch_key = "owner/repo#42|fix-ci"
        session_id = "abc12345"
        initial_record = PRDispatchRecord(active={dispatch_key: session_id})
        dispatch_file = state_dir / "pr_dispatch.json"
        dispatch_file.write_text(initial_record.model_dump_json())

        # Build a CwState with that session marked COMPLETED
        state = CwState(
            sessions=[
                Session(
                    id=session_id,
                    name="myproject/fix-ci-42",
                    client="myproject",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.COMPLETED,
                    workspace_path=workspace,
                )
            ]
        )

        clear_completed_pr_sessions(state)

        # Reload and verify the dispatch key was removed
        updated = PRDispatchRecord.model_validate_json(dispatch_file.read_text())
        assert dispatch_key not in updated.active


class TestUnknownClientSkipped:
    def test_unknown_client_skips_without_crash(
        self,
        tmp_config_dir: Path,
        adapter: FakeCmuxAdapter,
    ) -> None:
        """Event referencing unknown client is skipped gracefully (cursor advanced)."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("clients: {}\n")

        _make_ci_failed_event("unknown-client", "owner/repo", 1, "feat/x")

        spawned = respond_to_pr_events(adapter=adapter)
        assert spawned == 0
        assert adapter.calls["spawn"] == []

        # Calling again should still be 0 (cursor advanced past the event)
        spawned_again = respond_to_pr_events(adapter=adapter)
        assert spawned_again == 0
