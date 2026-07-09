"""Tests for cw.models - Pydantic models and state queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.models import (
    DEFAULT_AUTO_PURPOSES,
    DEFAULT_LANE,
    DEV_QUEUE_SCHEMA_VERSION,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
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

    def test_feature_branch_prefix_default(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.feature_branch_prefix == "dev"

    def test_feature_branch_prefix_custom(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            feature_branch_prefix="feat",
        )
        assert c.feature_branch_prefix == "feat"

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
        # PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, BLOCKED_ON_USER,
        # AWAITING_OPERATOR_SIGNOFF (#990)
        assert len(QueueItemStatus) == 7

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


class TestFalseParkRecoveryBackoffFields:
    """GitHub #1030: new TicketTask backoff-state fields for concierge recipe 1."""

    def test_defaults(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c")
        assert task.false_park_recovery_count == 0
        assert task.false_park_recovery_next_eligible_at is None

    def test_round_trip(self) -> None:
        next_eligible = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
        task = TicketTask(
            ticket_id="T-1",
            client="c",
            false_park_recovery_count=2,
            false_park_recovery_next_eligible_at=next_eligible,
        )
        dumped = task.model_dump(mode="json")
        restored = TicketTask.model_validate(dumped)
        assert restored.false_park_recovery_count == 2
        assert restored.false_park_recovery_next_eligible_at == next_eligible


class TestComputedScopeTierField:
    """GitHub #1050: pipeline-computed scope tier stamped by dispatch."""

    def test_ticket_task_computed_scope_tier_defaults_none(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c")
        assert task.computed_scope_tier is None


class TestConciergeRecoveryBackoffArmedEventType:
    def test_value(self) -> None:
        assert (
            OrchestratorEventType.CONCIERGE_RECOVERY_BACKOFF_ARMED.value
            == "concierge.recovery_backoff_armed"
        )


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


class TestPrStateAndSchemaV8:
    """PR-state hydration model + schema/config surface (#929)."""

    def test_dev_queue_schema_version_is_13(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 13

    def test_pr_state_defaults(self) -> None:
        state = PrState()
        assert state.state == "OPEN"
        assert state.ci_ok is True
        assert state.review_decision == ""
        assert state.attention_state is None
        assert state.merge_state_status == "UNKNOWN"
        assert state.failing_checks == []
        assert isinstance(state.hydrated_at, datetime)

    def test_ticket_task_pr_state_defaults_none(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme")
        assert task.pr_state is None

    def test_ticket_task_carries_pr_state(self) -> None:
        task = TicketTask(
            ticket_id="GEN-1",
            client="acme",
            pr_state=PrState(state="MERGED", attention_state="ready_to_approve"),
        )
        assert task.pr_state is not None
        assert task.pr_state.state == "MERGED"

    def test_config_pr_hydration_interval_default(self) -> None:
        assert OrchestratorConfig().pr_hydration_interval_seconds == 150


class TestOperatorSignoffGates:
    """RFC 0007 Phase 3 (W3) operator-signoff data model surface (#990)."""

    def test_queue_item_status_has_awaiting_operator_signoff(self) -> None:
        assert QueueItemStatus.AWAITING_OPERATOR_SIGNOFF == "awaiting_operator_signoff"

    def test_occupied_lane_statuses_includes_signoff(self) -> None:
        from cw.models import OCCUPIED_LANE_STATUSES

        assert (
            frozenset(
                [
                    QueueItemStatus.RUNNING,
                    QueueItemStatus.BLOCKED_ON_USER,
                    QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
                ]
            )
            == OCCUPIED_LANE_STATUSES
        )

    def test_ticket_task_signoff_defaults_none(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme")
        assert task.signoff is None

    def test_ticket_task_carries_signoff_operator(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme", signoff="operator")
        assert task.signoff == "operator"

    def test_lane_config_signoff_defaults_none(self) -> None:
        from cw.models import LaneConfig

        assert LaneConfig(name="default").signoff is None

    def test_lane_config_carries_signoff_operator(self) -> None:
        from cw.models import LaneConfig

        lane = LaneConfig(name="default", signoff="operator")
        assert lane.signoff == "operator"

    def test_orchestrator_config_default_signoff_is_none(self) -> None:
        assert OrchestratorConfig().default_signoff == "none"

    def test_orchestrator_config_accepts_default_signoff_operator(self) -> None:
        assert OrchestratorConfig(default_signoff="operator").default_signoff == (
            "operator"
        )

    def test_orchestrator_config_rejects_invalid_default_signoff(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            OrchestratorConfig(default_signoff="bogus")  # type: ignore[arg-type]


class TestConsecutiveSkipLatches:
    """RFC 0007 Phase 4 (W2) + #974: consecutive-skip attention latches."""

    def test_orchestrator_config_freshness_block_threshold_default(self) -> None:
        assert OrchestratorConfig().freshness_block_attention_threshold == 5

    def test_orchestrator_config_salvage_skip_threshold_default(self) -> None:
        assert OrchestratorConfig().salvage_skip_attention_threshold == 5

    def test_client_concurrency_override_freshness_blocks_defaults_zero(self) -> None:
        from cw.models import ClientConcurrencyOverride

        assert ClientConcurrencyOverride().consecutive_freshness_blocks == 0

    def test_session_consecutive_salvage_skips_defaults_zero(self) -> None:
        session = Session(
            name="acme/impl",
            client="acme",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/tmp/acme"),
        )
        assert session.consecutive_salvage_skips == 0


class TestInboxPruneThresholds:
    """Issue #856: OrchestratorConfig defaults for the inbox-size doctor check."""

    def test_orchestrator_config_default_inbox_size_warn_bytes(self) -> None:
        assert OrchestratorConfig().inbox_size_warn_bytes == 5_000_000

    def test_orchestrator_config_default_inbox_line_count_warn(self) -> None:
        assert OrchestratorConfig().inbox_line_count_warn == 15_000


class TestOperatorChannelForward:
    """RFC 0008 W3 (#1002): operator-attention forward-set config surface."""

    def test_default_event_types_match_rfc_defaults(self) -> None:
        from cw.models import OperatorChannelForward

        forward = OperatorChannelForward()
        assert forward.event_types == frozenset(
            {
                OrchestratorEventType.TASK_TRANSITION,
                OrchestratorEventType.TASK_DELETED,
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                OrchestratorEventType.PR_REGISTERED,
                OrchestratorEventType.PR_CI_FAILED,
                OrchestratorEventType.PR_REVIEW_RECEIVED,
                OrchestratorEventType.PR_MERGEABLE,
                OrchestratorEventType.PR_MERGED,
                OrchestratorEventType.SESSION_LIVENESS_CHANGED,
                OrchestratorEventType.OPERATOR_ESCALATION,
                OrchestratorEventType.GATE_AUTO_APPROVED,
                OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
            }
        )

    def test_default_task_transition_statuses(self) -> None:
        from cw.models import OperatorChannelForward

        forward = OperatorChannelForward()
        assert forward.task_transition_statuses == frozenset(
            {
                QueueItemStatus.BLOCKED_ON_USER,
                QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
                QueueItemStatus.COMPLETED,
                QueueItemStatus.FAILED,
                QueueItemStatus.CANCELLED,
            }
        )

    def test_default_liveness_min_bucket_is_stale_30m(self) -> None:
        from cw.models import LivenessBucket, OperatorChannelForward

        assert OperatorChannelForward().liveness_min_bucket == LivenessBucket.STALE_30M

    def test_rejects_invalid_event_type(self) -> None:
        import pydantic

        from cw.models import OperatorChannelForward

        with pytest.raises(pydantic.ValidationError):
            OperatorChannelForward(event_types=frozenset({"bogus.event"}))  # type: ignore[arg-type]

    def test_rejects_invalid_task_transition_status(self) -> None:
        import pydantic

        from cw.models import OperatorChannelForward

        with pytest.raises(pydantic.ValidationError):
            OperatorChannelForward(
                task_transition_statuses=frozenset({"bogus_status"})  # type: ignore[arg-type]
            )

    def test_rejects_invalid_liveness_min_bucket(self) -> None:
        import pydantic

        from cw.models import OperatorChannelForward

        with pytest.raises(pydantic.ValidationError):
            OperatorChannelForward(liveness_min_bucket="bogus_bucket")  # type: ignore[arg-type]

    def test_override_narrows_event_types(self) -> None:
        from cw.models import OperatorChannelForward

        forward = OperatorChannelForward(
            event_types=frozenset({OrchestratorEventType.TASK_DELETED})
        )
        assert forward.event_types == frozenset({OrchestratorEventType.TASK_DELETED})

    def test_override_widens_task_transition_statuses(self) -> None:
        from cw.models import OperatorChannelForward

        forward = OperatorChannelForward(
            task_transition_statuses=frozenset(
                {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
            )
        )
        assert QueueItemStatus.PENDING in forward.task_transition_statuses
        assert QueueItemStatus.RUNNING in forward.task_transition_statuses

    def test_orchestrator_config_operator_channel_forward_default(self) -> None:
        from cw.models import OperatorChannelForward

        assert OrchestratorConfig().operator_channel_forward == OperatorChannelForward()

    def test_orchestrator_config_accepts_operator_channel_forward_override(
        self,
    ) -> None:
        from cw.models import OperatorChannelForward

        cfg = OrchestratorConfig(
            operator_channel_forward=OperatorChannelForward(
                event_types=frozenset({OrchestratorEventType.TASK_DELETED})
            )
        )
        assert cfg.operator_channel_forward.event_types == frozenset(
            {OrchestratorEventType.TASK_DELETED}
        )


class TestConciergeAndEscalationModelSurface:
    """RFC 0008 capstone (#1015): concierge + durable-escalation model surface."""

    def test_ticket_task_escalation_fields_default_none(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme")
        assert task.escalation_parked_at is None
        assert task.escalation_fired_at is None

    def test_ticket_task_carries_escalation_fields(self) -> None:
        parked_at = datetime.now(UTC)
        fired_at = datetime.now(UTC)
        task = TicketTask(
            ticket_id="GEN-1",
            client="acme",
            escalation_parked_at=parked_at,
            escalation_fired_at=fired_at,
        )
        assert task.escalation_parked_at == parked_at
        assert task.escalation_fired_at == fired_at

    def test_orchestrator_event_type_includes_concierge_recovered(self) -> None:
        assert OrchestratorEventType.CONCIERGE_RECOVERED == "concierge.recovered"

    def test_orchestrator_event_type_includes_operator_escalation(self) -> None:
        assert OrchestratorEventType.OPERATOR_ESCALATION == "operator.escalation"

    def test_operator_escalation_in_default_forward_set(self) -> None:
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert (
            OrchestratorEventType.OPERATOR_ESCALATION in _DEFAULT_OPERATOR_EVENT_TYPES
        )

    def test_concierge_recovered_not_in_default_forward_set(self) -> None:
        """CONCIERGE_RECOVERED is audit-trail only — deliberately NOT forwarded
        to the operator channel by default (Q3)."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert (
            OrchestratorEventType.CONCIERGE_RECOVERED
            not in _DEFAULT_OPERATOR_EVENT_TYPES
        )

    def test_orchestrator_config_concierge_enabled_defaults_false(self) -> None:
        assert OrchestratorConfig().concierge_enabled is False

    def test_orchestrator_config_concierge_enabled_accepts_true(self) -> None:
        assert OrchestratorConfig(concierge_enabled=True).concierge_enabled is True

    def test_orchestrator_config_concierge_recoveries_defaults_empty(self) -> None:
        assert OrchestratorConfig().concierge_recoveries == {}

    def test_orchestrator_config_concierge_recoveries_accepts_overrides(self) -> None:
        cfg = OrchestratorConfig(concierge_recoveries={"false_park_requeue": False})
        assert cfg.concierge_recoveries == {"false_park_requeue": False}

    def test_orchestrator_config_concierge_recoveries_rejects_unknown_key(
        self,
    ) -> None:
        """A typo'd recipe key must fail loud, not silently no-op (Q7)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="unrecognized recipe key"):
            OrchestratorConfig(concierge_recoveries={"flase_park_requeue": True})

    # -- RFC 0009 gate-recipe automation surface (#1065) ---------------------

    def test_orchestrator_event_type_includes_gate_auto_approved(self) -> None:
        assert OrchestratorEventType.GATE_AUTO_APPROVED == "gate.auto_approved"

    def test_gate_auto_approved_in_default_forward_set(self) -> None:
        """GATE_AUTO_APPROVED IS forwarded by default: an auto-approve with no
        human review is operator-attention-worthy (contrast CONCIERGE_RECOVERED,
        which is audit-only)."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert OrchestratorEventType.GATE_AUTO_APPROVED in _DEFAULT_OPERATOR_EVENT_TYPES

    def test_orchestrator_event_type_includes_gate_auto_approve_failed(self) -> None:
        assert (
            OrchestratorEventType.GATE_AUTO_APPROVE_FAILED == "gate.auto_approve_failed"
        )

    def test_gate_auto_approve_failed_in_default_forward_set(self) -> None:
        """GATE_AUTO_APPROVE_FAILED IS forwarded by default: it corrects a
        false-positive GATE_AUTO_APPROVED already on the operator channel."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert (
            OrchestratorEventType.GATE_AUTO_APPROVE_FAILED
            in _DEFAULT_OPERATOR_EVENT_TYPES
        )

    def test_orchestrator_config_gate_recipes_enabled_defaults_false(self) -> None:
        assert OrchestratorConfig().gate_recipes_enabled is False

    def test_orchestrator_config_gate_recipes_enabled_accepts_true(self) -> None:
        cfg = OrchestratorConfig(gate_recipes_enabled=True)
        assert cfg.gate_recipes_enabled is True
