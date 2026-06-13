"""Tests for cw.models - Pydantic models and state queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.models import (
    DEFAULT_AUTO_PURPOSES,
    DEFAULT_LANE,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    Session,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)


class TestSessionPurpose:
    def test_enum_values(self) -> None:
        assert SessionPurpose.IMPL.value == "impl"
        assert SessionPurpose.IDEA.value == "idea"
        assert SessionPurpose.DEBT.value == "debt"

    def test_all_values(self) -> None:
        assert len(SessionPurpose) == 4


class TestSessionStatus:
    def test_enum_values(self) -> None:
        assert SessionStatus.ACTIVE.value == "active"
        assert SessionStatus.IDLE.value == "idle"
        assert SessionStatus.BACKGROUNDED.value == "backgrounded"
        assert SessionStatus.COMPLETED.value == "completed"
        assert SessionStatus.TIMED_OUT.value == "timed_out"

    def test_all_values(self) -> None:
        assert len(SessionStatus) == 5


class TestSession:
    def test_auto_id_generation(self, tmp_path: object) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert len(s.id) == 8
        assert re.match(r"^[0-9a-f]{8}$", s.id)

    def test_auto_id_is_unique(self) -> None:
        ids = {
            Session(
                name="c/impl",
                client="c",
                purpose=SessionPurpose.IMPL,
                workspace_path=Path("/dev/null"),
            ).id
            for _ in range(10)
        }
        assert len(ids) == 10

    def test_default_status_is_active(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.status == SessionStatus.ACTIVE

    def test_started_at_defaults_to_utc(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.started_at.tzinfo is not None
        assert s.started_at.tzinfo == UTC

    def test_optional_fields_default_none(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.worktree_path is None
        assert s.branch is None
        assert s.surface_ref is None
        assert s.claude_session_id is None
        assert s.backgrounded_at is None
        assert s.resumed_at is None
        assert s.completed_reason is None
        assert s.completed_at is None

    def test_legacy_last_handoff_path_ignored_on_load(self) -> None:
        raw = {
            "name": "c/impl",
            "client": "c",
            "purpose": "impl",
            "workspace_path": "/dev/null",
            "last_handoff_path": "/some/old/path/session.md",
        }
        s = Session.model_validate(raw)
        assert not hasattr(s, "last_handoff_path")

    def test_claude_session_id_round_trip(self) -> None:
        s = Session(
            id="abcd1234",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
            claude_session_id="550e8400-e29b-41d4-a716-446655440000",
        )
        json_str = s.model_dump_json()
        restored = Session.model_validate_json(json_str)
        assert restored.claude_session_id == "550e8400-e29b-41d4-a716-446655440000"

    def test_json_round_trip(self) -> None:
        s = Session(
            id="abcd1234",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=Path("/dev/null"),
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
            workspace_path=Path("/dev/null"),
        )
        assert s.id == "custom99"


class TestClientConfig:
    def test_defaults(self, tmp_path: object) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.default_branch == "main"
        assert c.worktree_base is None

    def test_custom_branch(self) -> None:
        c = ClientConfig(
            name="test", workspace_path=Path("/dev/null"), default_branch="develop"
        )
        assert c.default_branch == "develop"

    def test_default_auto_purposes(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.auto_purposes == [
            SessionPurpose.IDEA,
            SessionPurpose.IMPL,
            SessionPurpose.DEBT,
        ]

    def test_custom_auto_purposes(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            auto_purposes=[SessionPurpose.IMPL, SessionPurpose.IDEA],
        )
        assert len(c.auto_purposes) == 2
        assert SessionPurpose.DEBT not in c.auto_purposes

    def test_auto_purposes_instances_are_independent(self) -> None:
        c1 = ClientConfig(name="a", workspace_path=Path("/dev/null"))
        c2 = ClientConfig(name="b", workspace_path=Path("/dev/null"))
        c1.auto_purposes.append(SessionPurpose.IMPL)
        assert len(c2.auto_purposes) == len(DEFAULT_AUTO_PURPOSES)

    def test_default_purpose_prompts(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.purpose_prompts == {}

    def test_custom_purpose_prompts(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            purpose_prompts={"idea": "Focus on HIPAA compliance."},
        )
        assert c.purpose_prompts["idea"] == "Focus on HIPAA compliance."

    def test_worktree_mode_valid(self) -> None:
        c = ClientConfig(
            name="test",
            repo_path=Path("/home/user/repo"),
            branch="client-a",
        )
        assert c.is_worktree_client is True
        # workspace_path auto-set to repo_path as sentinel
        assert c.workspace_path == c.repo_path

    def test_legacy_mode_not_worktree(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.is_worktree_client is False
        assert c.repo_path is None
        assert c.branch is None

    def test_missing_both_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test")

    def test_repo_path_without_branch_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test", repo_path=Path("/home/user/repo"))

    def test_branch_without_repo_path_raises(self) -> None:
        with pytest.raises(ValueError, match="workspace_path or both"):
            ClientConfig(name="test", branch="client-a")

    def test_explicit_workspace_overrides_sentinel(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/explicit/path"),
            repo_path=Path("/home/user/repo"),
            branch="client-a",
        )
        assert c.is_worktree_client is True
        # Explicit workspace_path is preserved
        assert c.workspace_path == Path("/explicit/path")

    def test_worker_model_defaults_to_none(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.worker_model is None

    def test_worker_model_accepts_opaque_string(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            worker_model="claude-sonnet-4-6-20251015",
        )
        assert c.worker_model == "claude-sonnet-4-6-20251015"

    def test_worker_model_round_trip(self) -> None:
        original = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            worker_model="claude-haiku-4-5-20251001",
        )
        data = original.model_dump(mode="json")
        restored = ClientConfig.model_validate(data)
        assert restored.worker_model == "claude-haiku-4-5-20251001"


class TestCwState:
    def test_empty_state(self) -> None:
        state = CwState()
        assert state.sessions == []
        assert state.active_sessions() == []
        assert state.backgrounded_sessions() == []
        assert state.idled_sessions() == []

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
                    workspace_path=Path("/dev/null"),
                ),
                Session(
                    id="new",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=Path("/dev/null"),
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
        assert result.name == "test-client/idea"

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
                    workspace_path=Path("/dev/null"),
                ),
                Session(
                    id="second",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        result = state.find_by_name_or_id("c/impl")
        assert result is not None
        assert result.id == "second"

    def test_idled_sessions_filter(self) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="s1",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.IDLE,
                    workspace_path=Path("/dev/null"),
                ),
                Session(
                    id="s2",
                    name="c/debt",
                    client="c",
                    purpose=SessionPurpose.DEBT,
                    status=SessionStatus.ACTIVE,
                    workspace_path=Path("/dev/null"),
                ),
            ]
        )
        idled = state.idled_sessions()
        assert len(idled) == 1
        assert idled[0].id == "s1"

    def test_idle_at_defaults_to_none(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.idle_at is None


class TestSessionLinkageFields:
    def test_defaults_are_none_and_empty(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.parent_session_id is None
        assert s.worker_session_ids == []

    def test_worker_session_ids_instances_are_independent(self) -> None:
        s1 = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        s2 = Session(
            name="c/idea",
            client="c",
            purpose=SessionPurpose.IDEA,
            workspace_path=Path("/dev/null"),
        )
        s1.worker_session_ids.append("abc123")
        assert s2.worker_session_ids == []

    def test_linkage_fields_populated_round_trip(self) -> None:
        s = Session(
            id="parent01",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
            parent_session_id="root0001",
            worker_session_ids=["abc123", "def456"],
        )
        json_str = s.model_dump_json()
        restored = Session.model_validate_json(json_str)
        assert restored.parent_session_id == "root0001"
        assert restored.worker_session_ids == ["abc123", "def456"]
        assert restored == s

    def test_default_linkage_fields_round_trip(self) -> None:
        s = Session(
            id="solo0001",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        json_str = s.model_dump_json()
        restored = Session.model_validate_json(json_str)
        assert restored.parent_session_id is None
        assert restored.worker_session_ids == []
        assert restored == s


class TestSessionLastResult:
    def test_default_is_none(self) -> None:
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert s.last_result is None

    def test_round_trip_preserves_dict(self) -> None:
        payload = {"schema_version": 1, "status": "shipped", "ticket_id": "X"}
        s = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
            last_result=payload,
        )
        restored = Session.model_validate_json(s.model_dump_json())
        assert restored.last_result == payload


class TestCompletionReason:
    def test_enum_values(self) -> None:
        assert CompletionReason.USER.value == "user"
        assert CompletionReason.HANDOFF.value == "handoff"
        assert CompletionReason.CRASHED.value == "crashed"
        assert CompletionReason.NORMAL.value == "normal"
        assert CompletionReason.TIMED_OUT.value == "timed_out"

    def test_all_values(self) -> None:
        assert len(CompletionReason) == 5


class TestOrchestratorConfigLegacyDefault:
    """Migration of legacy ``per_client_max_parallel.default`` (issue #145)."""

    def test_lifts_legacy_default_into_top_level_field(self) -> None:
        """A stray ``default`` key under per_client_max_parallel is promoted
        to ``default_max_parallel`` and removed from the per-client dict.
        """
        config = OrchestratorConfig.model_validate(
            {
                "per_client_max_parallel": {"default": 5, "real-client": 2},
            }
        )
        assert config.default_max_parallel == 5
        assert config.per_client_max_parallel == {"real-client": 2}
        assert "default" not in config.per_client_max_parallel

    def test_explicit_default_max_parallel_wins_over_legacy(self) -> None:
        """If both new and legacy keys are present, the explicit field wins."""
        config = OrchestratorConfig.model_validate(
            {
                "default_max_parallel": 7,
                "per_client_max_parallel": {"default": 5},
            }
        )
        assert config.default_max_parallel == 7

    def test_no_legacy_key_leaves_default_at_one(self) -> None:
        """Default fallback is 1 when neither field is set."""
        config = OrchestratorConfig()
        assert config.default_max_parallel == 1


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "abc12345",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "prev_stage": "s1_plan_reviewed",
                "started_at": "2026-05-23T13:01:42Z",
            },
        ),
        (
            OrchestratorEventType.STAGE_ERRORED,
            {
                "session_id": "abc12345",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:01:42Z",
                "error_kind": "agent_block",
            },
        ),
    ],
)
def test_stage_event_types_round_trip(
    event_type: OrchestratorEventType,
    payload: dict[str, str],
) -> None:
    """STAGE_ENTERED / STAGE_ERRORED survive a Pydantic model round-trip."""
    event = OrchestratorEvent(type=event_type, payload=payload)
    restored = OrchestratorEvent.model_validate_json(event.model_dump_json())
    assert restored.type is event_type
    assert restored.payload == payload


def test_orchestrator_event_type_includes_needs_sync() -> None:
    """TICKET_NEEDS_SYNC event type has correct string value."""
    assert OrchestratorEventType.TICKET_NEEDS_SYNC.value == "ticket.needs_sync"


def test_orchestrator_event_type_includes_reap_authorized() -> None:
    """SESSION_REAP_AUTHORIZED event type has correct string value."""
    assert (
        OrchestratorEventType.SESSION_REAP_AUTHORIZED.value == "session.reap_authorized"
    )


class TestQueueItemStatusBlockedOnUser:
    def test_blocked_on_user_value(self) -> None:
        assert QueueItemStatus.BLOCKED_ON_USER.value == "blocked_on_user"

    def test_all_queue_statuses(self) -> None:
        # PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, BLOCKED_ON_USER
        assert len(QueueItemStatus) == 6

    def test_blocked_on_user_not_in_running(self) -> None:
        store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-1",
                    client="c",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                ),
                TicketTask(
                    ticket_id="T-2",
                    client="c",
                    status=QueueItemStatus.RUNNING,
                ),
            ]
        )
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "T-2"
        assert all(t.ticket_id != "T-1" for t in running)


class TestOrchestratorEventTypeSessionNeedsAttention:
    def test_session_needs_attention_value(self) -> None:
        assert (
            OrchestratorEventType.SESSION_NEEDS_ATTENTION.value
            == "session.needs_attention"
        )


class TestCostFields:
    def test_cost_fields_default_none(self) -> None:
        sess = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
        )
        assert sess.cost_usd is None
        assert sess.cost_breakdown is None

    def test_cost_fields_round_trip(self) -> None:
        sess = Session(
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/dev/null"),
            cost_usd=1.5,
            cost_breakdown={"model-a": 1.5},
        )
        dumped = sess.model_dump(mode="json")
        restored = Session.model_validate(dumped)
        assert restored.cost_usd == 1.5
        assert restored.cost_breakdown == {"model-a": 1.5}

    def test_ticket_task_total_cost_usd_defaults_none(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c")
        assert task.total_cost_usd is None

    def test_ticket_task_total_cost_round_trip(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c", total_cost_usd=3.14)
        dumped = task.model_dump(mode="json")
        restored = TicketTask.model_validate(dumped)
        assert restored.total_cost_usd == pytest.approx(3.14)


class TestLaneConfig:
    def test_default_fields(self) -> None:
        """LaneConfig has correct defaults when instantiated with only name."""
        lane = LaneConfig(name="default")
        assert lane.max_parallel == 1
        assert lane.priority == 0
        assert lane.paused is False
        assert lane.description == ""
        assert lane.reap_policy is None

    def test_reap_policy_uses_enum(self) -> None:
        """reap_policy is a ReapPolicy instance when explicitly set."""
        lane = LaneConfig(name="fast", reap_policy=ReapPolicy.SIGNAL_ONLY)
        assert isinstance(lane.reap_policy, ReapPolicy)
        assert lane.reap_policy == ReapPolicy.SIGNAL_ONLY

    def test_name_required(self) -> None:
        """LaneConfig without name raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LaneConfig()  # type: ignore[call-arg]

    def test_empty_name_raises(self) -> None:
        """Empty lane name raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LaneConfig(name="")


class TestClientConfigEffectiveLanes:
    def test_effective_lanes_empty_synthesizes_default(self, tmp_path: object) -> None:
        """ClientConfig with no lanes returns synthesized default lane."""
        config = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        lanes = config.effective_lanes
        assert len(lanes) == 1
        assert lanes[0].name == DEFAULT_LANE

    def test_effective_lanes_explicit_list_passed_through(
        self, tmp_path: object
    ) -> None:
        """ClientConfig with explicit lanes returns them unchanged."""
        explicit = [LaneConfig(name="fast"), LaneConfig(name="slow")]
        config = ClientConfig(
            name="test", workspace_path=Path("/dev/null"), lanes=explicit
        )
        lanes = config.effective_lanes
        assert len(lanes) == 2
        assert lanes[0].name == "fast"
        assert lanes[1].name == "slow"


class TestOrchestratorConfigCeilingMigration:
    """Migration of legacy per_client_max_parallel / default_max_parallel into
    per_client_ceiling / default_ceiling fields (#558)."""

    def test_legacy_per_client_max_parallel_lifted(self) -> None:
        """Legacy per_client_max_parallel is migrated into per_client_ceiling."""
        config = OrchestratorConfig(
            per_client_max_parallel={"my-client": 3},
            default_max_parallel=2,
        )
        assert config.per_client_ceiling == {"my-client": 3}
        assert config.default_ceiling == 2

    def test_new_fields_win_over_legacy(self) -> None:
        """When both new and legacy fields are present, new fields win."""
        config = OrchestratorConfig(
            per_client_ceiling={"my-client": 5},
            default_ceiling=4,
            per_client_max_parallel={"my-client": 3},
            default_max_parallel=2,
        )
        assert config.per_client_ceiling == {"my-client": 5}
        assert config.default_ceiling == 4

    def test_absent_legacy_keys_use_defaults(self) -> None:
        """Without legacy keys, new fields default to empty dict and 1."""
        config = OrchestratorConfig()
        assert config.per_client_ceiling == {}
        assert config.default_ceiling == 1


# --- SessionPurpose.ORCHESTRATE and WORKER_PURPOSES ---


def test_session_purpose_orchestrate_exists() -> None:
    """SessionPurpose.ORCHESTRATE has value 'orchestrate'."""
    assert SessionPurpose.ORCHESTRATE == "orchestrate"
    assert SessionPurpose.ORCHESTRATE.value == "orchestrate"


def test_worker_purposes_excludes_orchestrate() -> None:
    """WORKER_PURPOSES tuple contains IMPL/IDEA/DEBT but not ORCHESTRATE."""
    from cw.models import WORKER_PURPOSES

    assert SessionPurpose.ORCHESTRATE not in WORKER_PURPOSES
    assert SessionPurpose.IMPL in WORKER_PURPOSES
    assert SessionPurpose.IDEA in WORKER_PURPOSES
    assert SessionPurpose.DEBT in WORKER_PURPOSES


def test_session_lane_defaults_to_none() -> None:
    """Session.lane defaults to None when not provided."""
    sess = Session(
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp"),
    )
    assert sess.lane is None


def test_session_lane_round_trips() -> None:
    """Session(lane='x') stores and returns the lane value."""
    sess = Session(
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp"),
        lane="my-lane",
    )
    assert sess.lane == "my-lane"
