"""Tests for cw.models - Pydantic models and state queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from cw.models import (
    ClientConfig,
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
