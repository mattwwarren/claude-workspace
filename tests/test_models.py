"""Tests for cw.models - Pydantic models and state queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)


class TestSessionPurpose:
    def test_enum_values(self) -> None:
        assert SessionPurpose.IMPL == "impl"
        assert SessionPurpose.REVIEW == "review"
        assert SessionPurpose.DEBT == "debt"
        assert SessionPurpose.EXPLORE == "explore"

    def test_all_values(self) -> None:
        assert len(SessionPurpose) == 4


class TestSessionStatus:
    def test_enum_values(self) -> None:
        assert SessionStatus.ACTIVE == "active"
        assert SessionStatus.BACKGROUNDED == "backgrounded"
        assert SessionStatus.COMPLETED == "completed"

    def test_all_values(self) -> None:
        assert len(SessionStatus) == 3


class TestSession:
    def test_auto_id_generation(self, tmp_path: object) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path="/dev/null",
        )
        assert len(s.id) == 8
        assert re.match(r"^[0-9a-f]{8}$", s.id)

    def test_auto_id_is_unique(self) -> None:
        ids = {
            Session(
                name="c/impl",
                client="c",
                purpose=SessionPurpose.IMPL,
                workspace_path="/dev/null",
            ).id
            for _ in range(10)
        }
        assert len(ids) == 10

    def test_default_status_is_active(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path="/dev/null",
        )
        assert s.status == SessionStatus.ACTIVE

    def test_started_at_defaults_to_utc(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path="/dev/null",
        )
        assert s.started_at.tzinfo is not None
        assert s.started_at.tzinfo == UTC

    def test_optional_fields_default_none(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path="/dev/null",
        )
        assert s.worktree_path is None
        assert s.branch is None
        assert s.zellij_pane is None
        assert s.zellij_tab is None
        assert s.last_handoff_path is None
        assert s.backgrounded_at is None
        assert s.resumed_at is None
        assert s.completed_reason is None
        assert s.completed_at is None

    def test_json_round_trip(self) -> None:
        s = Session(
            id="abcd1234",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.BACKGROUNDED,
            workspace_path="/dev/null",
            started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            backgrounded_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        json_str = s.model_dump_json()
        restored = Session.model_validate_json(json_str)
        assert restored.id == s.id
        assert restored.name == s.name
        assert restored.status == s.status
        assert restored.started_at == s.started_at
        assert restored.backgrounded_at == s.backgrounded_at

    def test_explicit_id_preserved(self) -> None:
        s = Session(
            id="custom99",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path="/dev/null",
        )
        assert s.id == "custom99"


class TestClientConfig:
    def test_defaults(self, tmp_path: object) -> None:
        c = ClientConfig(name="test", workspace_path="/dev/null")
        assert c.default_branch == "main"
        assert c.worktree_base is None

    def test_custom_branch(self) -> None:
        c = ClientConfig(
            name="test", workspace_path="/dev/null", default_branch="develop"
        )
        assert c.default_branch == "develop"

    def test_default_auto_purposes(self) -> None:
        c = ClientConfig(name="test", workspace_path="/dev/null")
        assert c.auto_purposes == [
            SessionPurpose.IMPL,
            SessionPurpose.REVIEW,
            SessionPurpose.DEBT,
        ]

    def test_custom_auto_purposes(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path="/dev/null",
            auto_purposes=[SessionPurpose.IMPL, SessionPurpose.REVIEW],
        )
        assert len(c.auto_purposes) == 2
        assert SessionPurpose.DEBT not in c.auto_purposes

    def test_auto_purposes_instances_are_independent(self) -> None:
        c1 = ClientConfig(name="a", workspace_path="/dev/null")
        c2 = ClientConfig(name="b", workspace_path="/dev/null")
        c1.auto_purposes.append(SessionPurpose.EXPLORE)
        assert SessionPurpose.EXPLORE not in c2.auto_purposes

    def test_default_purpose_prompts(self) -> None:
        c = ClientConfig(name="test", workspace_path="/dev/null")
        assert c.purpose_prompts == {}

    def test_custom_purpose_prompts(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path="/dev/null",
            purpose_prompts={"review": "Focus on HIPAA compliance."},
        )
        assert c.purpose_prompts["review"] == "Focus on HIPAA compliance."

    def test_worktree_mode_valid(self) -> None:
        c = ClientConfig(
            name="test",
            repo_path="/home/user/repo",
            branch="client-a",
        )
        assert c.is_worktree_client is True
        # workspace_path auto-set to repo_path as sentinel
        assert c.workspace_path == c.repo_path

    def test_legacy_mode_not_worktree(self) -> None:
        c = ClientConfig(name="test", workspace_path="/dev/null")
        assert c.is_worktree_client is False
        assert c.repo_path is None
        assert c.branch is None

    def test_missing_both_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test")

    def test_repo_path_without_branch_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test", repo_path="/home/user/repo")

    def test_branch_without_repo_path_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test", branch="client-a")

    def test_explicit_workspace_overrides_sentinel(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path="/explicit/path",
            repo_path="/home/user/repo",
            branch="client-a",
        )
        assert c.is_worktree_client is True
        # Explicit workspace_path is preserved
        assert c.workspace_path == Path("/explicit/path")


class TestCwState:
    def test_empty_state(self) -> None:
        state = CwState()
        assert state.sessions == []
        assert state.active_sessions() == []
        assert state.backgrounded_sessions() == []

    def test_active_sessions_filter(self, sample_state: CwState) -> None:
        active = sample_state.active_sessions()
        assert len(active) == 1
        assert active[0].id == "sess0001"

    def test_backgrounded_sessions_filter(self, sample_state: CwState) -> None:
        bg = sample_state.backgrounded_sessions()
        assert len(bg) == 1
        assert bg[0].id == "sess0002"

    def test_find_session_returns_match(self, sample_state: CwState) -> None:
        result = sample_state.find_session("test-client", "impl")
        assert result is not None
        assert result.id == "sess0001"

    def test_find_session_returns_none_on_miss(self, sample_state: CwState) -> None:
        result = sample_state.find_session("nonexistent", "impl")
        assert result is None

    def test_find_session_skips_completed(self, sample_state: CwState) -> None:
        result = sample_state.find_session("other-client", "impl")
        assert result is None

    def test_find_session_returns_most_recent(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="old",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path="/dev/null",
                ),
                Session(
                    id="new",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path="/dev/null",
                ),
            ]
        )
        result = state.find_session("c", "impl")
        assert result is not None
        assert result.id == "new"

    def test_find_by_name(self, sample_state: CwState) -> None:
        result = sample_state.find_by_name_or_id("test-client/impl")
        assert result is not None
        assert result.id == "sess0001"

    def test_find_by_id(self, sample_state: CwState) -> None:
        result = sample_state.find_by_name_or_id("sess0002")
        assert result is not None
        assert result.name == "test-client/review"

    def test_find_by_name_or_id_returns_none(self, sample_state: CwState) -> None:
        result = sample_state.find_by_name_or_id("nonexistent")
        assert result is None

    def test_find_by_name_or_id_reverse_order(self) -> None:
        """Most recent session with matching name should be returned."""
        state = CwState(
            sessions=[
                Session(
                    id="first",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    workspace_path="/dev/null",
                ),
                Session(
                    id="second",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    workspace_path="/dev/null",
                ),
            ]
        )
        result = state.find_by_name_or_id("c/impl")
        assert result is not None
        assert result.id == "second"

    def test_client_sessions(self, sample_state: CwState) -> None:
        result = sample_state.client_sessions("test-client")
        assert len(result) == 2
        assert all(s.client == "test-client" for s in result)

    def test_client_sessions_includes_all_statuses(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="a1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path="/dev/null",
                ),
                Session(
                    id="a2",
                    name="c/review",
                    client="c",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.COMPLETED,
                    workspace_path="/dev/null",
                ),
            ]
        )
        result = state.client_sessions("c")
        assert len(result) == 2

    def test_client_sessions_empty(self, sample_state: CwState) -> None:
        result = sample_state.client_sessions("nonexistent")
        assert result == []

    def test_active_for_client(self, sample_state: CwState) -> None:
        result = sample_state.active_for_client("test-client")
        assert len(result) == 2  # 1 active + 1 backgrounded
        statuses = {s.status for s in result}
        assert statuses == {SessionStatus.ACTIVE, SessionStatus.BACKGROUNDED}

    def test_active_for_client_excludes_completed(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="b1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.COMPLETED,
                    workspace_path="/dev/null",
                ),
            ]
        )
        result = state.active_for_client("c")
        assert result == []

    def test_sibling_sessions(self, sample_state: CwState) -> None:
        source = sample_state.sessions[0]  # test-client/impl ACTIVE
        siblings = sample_state.sibling_sessions(source)
        assert len(siblings) == 1
        assert siblings[0].id == "sess0002"  # test-client/review BACKGROUNDED

    def test_sibling_sessions_excludes_completed(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="s1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path="/dev/null",
                ),
                Session(
                    id="s2",
                    name="c/review",
                    client="c",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.COMPLETED,
                    workspace_path="/dev/null",
                ),
            ]
        )
        siblings = state.sibling_sessions(state.sessions[0])
        assert siblings == []

    def test_sibling_sessions_excludes_other_clients(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="s1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path="/dev/null",
                ),
                Session(
                    id="s2",
                    name="d/impl",
                    client="d",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path="/dev/null",
                ),
            ]
        )
        siblings = state.sibling_sessions(state.sessions[0])
        assert siblings == []


class TestCompletionReason:
    def test_enum_values(self) -> None:
        assert CompletionReason.USER == "user"
        assert CompletionReason.HANDOFF == "handoff"
        assert CompletionReason.CRASHED == "crashed"

    def test_all_values(self) -> None:
        assert len(CompletionReason) == 3
