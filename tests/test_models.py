"""Tests for cw.models - Pydantic models and state queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cw.models import (
    DEFAULT_AUTO_PURPOSES,
    DEFAULT_LANE,
    DEV_QUEUE_SCHEMA_VERSION,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LaneConfig,
    LastResultSource,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    Session,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
    WatchedPr,
)
from cw.review_finding_dispositions import FindingDisposition


class TestSessionPurpose:
    def test_enum_values(self) -> None:
        assert SessionPurpose.IMPL.value == "impl"
        assert SessionPurpose.IDEA.value == "idea"
        assert SessionPurpose.DEBT.value == "debt"

    def test_all_values(self) -> None:
        assert len(SessionPurpose) == 5


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

    def test_quality_gate_commands_defaults_to_none(self) -> None:
        c = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert c.quality_gate_commands is None

    def test_quality_gate_commands_accepts_opaque_string(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            quality_gate_commands="npm run lint",
        )
        assert c.quality_gate_commands == "npm run lint"

    def test_quality_gate_commands_accepts_empty_string(self) -> None:
        c = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            quality_gate_commands="",
        )
        assert c.quality_gate_commands == ""

    def test_unknown_key_raises(self) -> None:
        """extra='forbid' rejects an unrecognized top-level key (#1200)."""
        with pytest.raises(ValidationError):
            ClientConfig(name="test", workspace_path=Path("/dev/null"), bogus_field="x")


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

    def test_unknown_key_raises(self) -> None:
        """extra='forbid' rejects an unrecognized top-level key (#1200)."""
        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate({"bogus_field": "x"})


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

    def test_ticket_task_last_blocked_result_defaults_to_none(self) -> None:
        """GitHub #1266: new diagnostic field defaults to None (schema v19)."""
        task = TicketTask(ticket_id="T-1", client="c")
        assert task.last_blocked_result is None

    def test_ticket_task_last_blocked_result_round_trip(self) -> None:
        task = TicketTask(
            ticket_id="T-1",
            client="c",
            last_blocked_result={
                "status": "blocked",
                "blocker": {"reason": "status_unknown"},
            },
        )
        dumped = task.model_dump(mode="json")
        restored = TicketTask.model_validate(dumped)
        assert restored.last_blocked_result == {
            "status": "blocked",
            "blocker": {"reason": "status_unknown"},
        }


class TestTicketTaskFindingDispositions:
    """GitHub #1838: the cross-round adjudication ledger field (schema v31)."""

    def test_defaults_to_an_empty_dict(self) -> None:
        assert TicketTask(ticket_id="T-1", client="c").finding_dispositions == {}

    def test_round_trips_through_json(self) -> None:
        task = TicketTask(
            ticket_id="T-1",
            client="c",
            finding_dispositions={
                "src/cw/foo.py::bug here": FindingDisposition(
                    outcome="REJECTED",
                    rationale="settled by the operator",
                    recorded_at="2026-08-16T00:00:00Z",
                )
            },
        )
        restored = TicketTask.model_validate(task.model_dump(mode="json"))
        entry = restored.finding_dispositions["src/cw/foo.py::bug here"]
        assert entry.outcome == "REJECTED"
        assert entry.rationale == "settled by the operator"
        assert entry.recorded_at == "2026-08-16T00:00:00Z"

    def test_legacy_pre_v31_row_loads_with_an_empty_ledger(self) -> None:
        """A persisted row from before the field existed must still load."""
        legacy = {
            "ticket_id": "T-1",
            "client": "c",
            "priority": 0,
            "status": "pending",
        }
        assert TicketTask.model_validate(legacy).finding_dispositions == {}


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

    def test_unknown_key_raises(self) -> None:
        """extra='forbid' rejects an unrecognized top-level key (#1200)."""
        with pytest.raises(ValidationError):
            LaneConfig(name="x", bogus_field="x")

    def test_typo_review_recipies_raises(self) -> None:
        """The ticket's own motivating typo (review_recipies) is caught at the
        model level via extra='forbid', not just the field validator's
        recognized-key check on the correctly-spelled field (#1200)."""
        with pytest.raises(ValidationError):
            LaneConfig(name="x", review_recipies={"address_review": True})  # type: ignore[call-arg]


class TestBusyWaitGuardConfigBounds:
    """#1946 review gate: repeat_threshold <= 1 makes _repeat_threshold_tripped's
    ``matching >= repeat_threshold - 1`` condition always true, blocking every
    non-bare-noop Bash call on its first occurrence. Field(ge=...) must reject
    that at both the global and lane-override level."""

    def test_global_repeat_threshold_rejects_one(self) -> None:
        with pytest.raises(ValidationError):
            OrchestratorConfig(busy_wait_guard_repeat_threshold=1)

    def test_global_repeat_threshold_accepts_two(self) -> None:
        config = OrchestratorConfig(busy_wait_guard_repeat_threshold=2)
        assert config.busy_wait_guard_repeat_threshold == 2

    def test_global_window_seconds_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            OrchestratorConfig(busy_wait_guard_window_seconds=0)

    def test_global_window_seconds_accepts_one(self) -> None:
        config = OrchestratorConfig(busy_wait_guard_window_seconds=1)
        assert config.busy_wait_guard_window_seconds == 1

    def test_lane_repeat_threshold_rejects_one(self) -> None:
        with pytest.raises(ValidationError):
            LaneConfig(name="x", busy_wait_guard_repeat_threshold=1)

    def test_lane_repeat_threshold_accepts_none(self) -> None:
        lane = LaneConfig(name="x")
        assert lane.busy_wait_guard_repeat_threshold is None

    def test_lane_window_seconds_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            LaneConfig(name="x", busy_wait_guard_window_seconds=0)


class TestStageExecutorConfigExtraForbid:
    """RFC 0005 A1 (dormant); extra='forbid' coverage added by #1200."""

    def test_defaults(self) -> None:
        executor = StageExecutorConfig()
        assert executor.backend == "claude-native"
        assert executor.model is None
        assert executor.endpoint is None

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            StageExecutorConfig(bogus_field="x")


class TestStagePipelineConfigExtraForbid:
    """RFC 0005 A1 (dormant); extra='forbid' coverage added by #1200."""

    def test_defaults(self) -> None:
        pipeline = StagePipelineConfig()
        assert pipeline.stages == [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        assert pipeline.executors == {}

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            StagePipelineConfig(bogus_field="x")


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


def test_session_purpose_fix_exists() -> None:
    """SessionPurpose.FIX has value 'fix'."""
    assert SessionPurpose.FIX == "fix"
    assert SessionPurpose.FIX.value == "fix"


def test_worker_purposes_excludes_fix() -> None:
    """WORKER_PURPOSES tuple contains IMPL/IDEA/DEBT but not FIX (#2017)."""
    from cw.models import WORKER_PURPOSES

    assert SessionPurpose.FIX not in WORKER_PURPOSES
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


def test_session_last_result_source_defaults_none() -> None:
    """Session.last_result_source defaults to None (RFC 0012 S2, #1456)."""
    sess = Session(
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp"),
    )
    assert sess.last_result_source is None


def test_session_last_result_source_round_trips() -> None:
    """Session(last_result_source=...) survives a model_dump/model_validate
    round trip (mirrors TicketTask.stage_high_water's round-trip precedent)."""
    sess = Session(
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp"),
        last_result_source=LastResultSource.EMIT_CLI,
    )
    dumped = sess.model_dump(mode="json")
    restored = Session.model_validate(dumped)
    assert restored.last_result_source == LastResultSource.EMIT_CLI


class TestPrStateAndSchemaV8:
    """PR-state hydration model + schema/config surface (#929)."""

    def test_dev_queue_schema_version_is_current(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 33

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


class TestStageHighWaterField:
    """TicketTask.stage_high_water (schema v21, GitHub #1361)."""

    def test_defaults_to_none(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c")
        assert task.stage_high_water is None

    def test_round_trip(self) -> None:
        task = TicketTask(ticket_id="T-1", client="c", stage_high_water=Stage.REVIEW)
        dumped = task.model_dump(mode="json")
        restored = TicketTask.model_validate(dumped)
        assert restored.stage_high_water == Stage.REVIEW

    def test_rejects_invalid_stage_string(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="T-1", client="c", stage_high_water="not-a-stage")


class TestWatchedPrModel:
    """WatchedPr model + DevQueueStore.watched_prs field (GitHub #1154, RFC 0011 S2)."""

    def test_watched_pr_construct_all_fields(self) -> None:
        watched = WatchedPr(
            pr_url="https://github.com/acme/widgets/pull/42",
            repo="acme/widgets",
            pr_number=42,
            requester_login="alice",
            source="cli",
            status="active",
            pr_state=PrState(state="OPEN"),
        )
        assert watched.pr_url == "https://github.com/acme/widgets/pull/42"
        assert watched.repo == "acme/widgets"
        assert watched.pr_number == 42
        assert watched.requester_login == "alice"
        assert watched.source == "cli"
        assert watched.status == "active"
        assert watched.pr_state is not None
        assert watched.pr_state.state == "OPEN"

    def test_watched_pr_defaults(self) -> None:
        watched = WatchedPr(
            pr_url="https://github.com/acme/widgets/pull/7",
            repo="acme/widgets",
            pr_number=7,
            source="webhook",
        )
        assert watched.status == "active"
        assert watched.requester_login is None
        assert watched.pr_state is None
        assert isinstance(watched.requested_at, datetime)

    def test_watched_pr_client_defaults_none_for_preexisting_blob(self) -> None:
        """GitHub #1927: a persisted pre-#1927 blob has no ``client`` key.

        No DEV_QUEUE_SCHEMA_VERSION bump accompanies the field (settled A1),
        so backward parse compatibility is the load-bearing guarantee.
        """
        blob = {
            "pr_url": "https://github.com/acme/widgets/pull/9",
            "repo": "acme/widgets",
            "pr_number": 9,
            "source": "webhook",
            "status": "active",
        }
        watched = WatchedPr.model_validate(blob)
        assert watched.client is None

    def test_watched_pr_stale_dispatch_park_source_round_trips(self) -> None:
        watched = WatchedPr(
            pr_url="https://github.com/acme/widgets/pull/9",
            repo="acme/widgets",
            pr_number=9,
            client="acme",
            source="stale_dispatch_park",
        )
        restored = WatchedPr.model_validate(watched.model_dump(mode="json"))
        assert restored.source == "stale_dispatch_park"
        assert restored.client == "acme"

    def test_watched_pr_rejects_bad_source(self) -> None:
        with pytest.raises(ValidationError):
            WatchedPr(
                pr_url="https://github.com/acme/widgets/pull/7",
                repo="acme/widgets",
                pr_number=7,
                source="carrier-pigeon",  # type: ignore[arg-type]
            )

    def test_watched_pr_rejects_bad_status(self) -> None:
        with pytest.raises(ValidationError):
            WatchedPr(
                pr_url="https://github.com/acme/widgets/pull/7",
                repo="acme/widgets",
                pr_number=7,
                source="cli",
                status="paused",  # type: ignore[arg-type]
            )

    def test_dev_queue_store_watched_prs_default_empty(self) -> None:
        assert DevQueueStore().watched_prs == []


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


class TestCodexFixLoopEnabledGate:
    """Lane-scoped codex_fix_loop_enabled with client/global fallback (#1553)."""

    def test_lane_config_codex_fix_loop_enabled_defaults_none(self) -> None:
        assert LaneConfig(name="default").codex_fix_loop_enabled is None

    def test_lane_config_carries_codex_fix_loop_enabled_true(self) -> None:
        lane = LaneConfig(name="default", codex_fix_loop_enabled=True)
        assert lane.codex_fix_loop_enabled is True

    def test_lane_config_rejects_codex_fix_loop_enabled_false(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            LaneConfig(name="default", codex_fix_loop_enabled=False)  # type: ignore[arg-type]

    def test_orchestrator_config_default_codex_fix_loop_enabled_is_false(self) -> None:
        assert OrchestratorConfig().default_codex_fix_loop_enabled is False

    def test_orchestrator_config_accepts_default_codex_fix_loop_enabled_true(
        self,
    ) -> None:
        assert (
            OrchestratorConfig(
                default_codex_fix_loop_enabled=True
            ).default_codex_fix_loop_enabled
            is True
        )


class TestLaneAttemptCeiling:
    """Lane-scoped attempt_ceiling with global fallback (#1751).

    Tri-state field: ``None`` = lane sets no override (defer to
    ``OrchestratorConfig.global_attempt_ceiling``), ``False`` = lane explicitly
    disables the ceiling, positive ``int`` = lane override value.
    """

    def test_lane_config_attempt_ceiling_defaults_none(self) -> None:
        assert LaneConfig(name="default").attempt_ceiling is None

    def test_lane_config_accepts_positive_int_override(self) -> None:
        assert LaneConfig(name="x", attempt_ceiling=25).attempt_ceiling == 25

    def test_lane_config_accepts_false_to_disable(self) -> None:
        assert LaneConfig(name="x", attempt_ceiling=False).attempt_ceiling is False

    def test_lane_config_rejects_zero_ceiling(self) -> None:
        """Raw ``0`` would silently smart-union-collapse to ``False`` (disable)."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            LaneConfig(name="x", attempt_ceiling=0)

    def test_lane_config_rejects_negative_ceiling(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            LaneConfig(name="x", attempt_ceiling=-1)

    def test_lane_config_rejects_true_ceiling(self) -> None:
        """Raw ``True`` would silently smart-union-collapse to a ceiling of 1."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            LaneConfig(name="x", attempt_ceiling=True)


class TestConsecutiveSkipLatches:
    """RFC 0007 Phase 4 (W2) + #974: consecutive-skip attention latches."""

    def test_orchestrator_config_freshness_block_threshold_default(self) -> None:
        assert OrchestratorConfig().freshness_block_attention_threshold == 5

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

    def test_session_consecutive_park_vetoes_defaults_zero(self) -> None:
        session = Session(
            name="acme/impl",
            client="acme",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/tmp/acme"),
        )
        assert session.consecutive_park_vetoes == 0


class TestDispatchStaleNotifyFields:
    """#1875: the dispatch-loop staleness watchdog's debounce + interval fields.

    Mirrors the ``lane_starved_notify_*`` pair (#1630) they are modelled on:
    a per-client persisted ``next_eligible_at`` stamp plus a fixed
    interval-minutes knob on ``OrchestratorConfig``.
    """

    def test_client_override_stale_notify_stamp_defaults_none(self) -> None:
        from cw.models import ClientConcurrencyOverride

        assert (
            ClientConcurrencyOverride().dispatch_stale_notify_next_eligible_at is None
        )

    def test_client_override_stale_notify_stamp_round_trips(self) -> None:
        from cw.models import ClientConcurrencyOverride

        stamp = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        override = ClientConcurrencyOverride(
            dispatch_stale_notify_next_eligible_at=stamp
        )
        restored = ClientConcurrencyOverride.model_validate_json(
            override.model_dump_json()
        )
        assert restored.dispatch_stale_notify_next_eligible_at == stamp

    def test_orchestrator_config_stale_notify_interval_default(self) -> None:
        assert OrchestratorConfig().dispatch_stale_notify_interval_minutes == 15

    def test_orchestrator_config_stale_notify_interval_round_trips(self) -> None:
        config = OrchestratorConfig(dispatch_stale_notify_interval_minutes=45)
        restored = OrchestratorConfig.model_validate_json(config.model_dump_json())
        assert restored.dispatch_stale_notify_interval_minutes == 45


class TestInboxPruneThresholds:
    """Issue #856: OrchestratorConfig defaults for the inbox-size doctor check."""

    def test_orchestrator_config_default_inbox_size_warn_bytes(self) -> None:
        assert OrchestratorConfig().inbox_size_warn_bytes == 5_000_000

    def test_orchestrator_config_default_inbox_line_count_warn(self) -> None:
        assert OrchestratorConfig().inbox_line_count_warn == 15_000


class TestEventInboxAutoPruneDefaults:
    """#1980: OrchestratorConfig defaults for the event-inbox auto-prune trigger."""

    def test_orchestrator_config_default_event_inbox_auto_prune_enabled(self) -> None:
        assert OrchestratorConfig().event_inbox_auto_prune_enabled is True

    def test_orchestrator_config_default_event_inbox_retention_bytes(self) -> None:
        assert OrchestratorConfig().event_inbox_retention_bytes == 5_000_000

    def test_orchestrator_config_default_event_inbox_retention_count(self) -> None:
        assert OrchestratorConfig().event_inbox_retention_count == 2000


class TestEventInboxRetentionRatioValidator:
    """#1980 review: warn (never fail) when retention_bytes can't hold
    retention_count events, so auto-prune can't thrash on every append."""

    def test_warns_when_bytes_below_plausible_footprint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            OrchestratorConfig(
                event_inbox_retention_bytes=50, event_inbox_retention_count=3
            )
        assert any("event_inbox_retention_bytes" in r.message for r in caplog.records)

    def test_no_warning_for_default_config(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            OrchestratorConfig()
        assert not [
            r for r in caplog.records if "event_inbox_retention_bytes" in r.message
        ]

    def test_no_warning_when_auto_prune_disabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            OrchestratorConfig(
                event_inbox_auto_prune_enabled=False,
                event_inbox_retention_bytes=50,
                event_inbox_retention_count=3,
            )
        assert not [
            r for r in caplog.records if "event_inbox_retention_bytes" in r.message
        ]


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
                OrchestratorEventType.GATE_AUTO_APPROVE_HELD,
                OrchestratorEventType.PR_ACTION_TAKEN,
                OrchestratorEventType.PR_ACTION_FAILED,
                OrchestratorEventType.SSH_KEY_GATE_BYPASSED,
                OrchestratorEventType.DISK_PRESSURE_GATE_BYPASSED,
                OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
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

    def test_unknown_key_raises(self) -> None:
        """extra='forbid' rejects an unrecognized top-level key (#1200)."""
        from cw.models import OperatorChannelForward

        with pytest.raises(ValidationError):
            OperatorChannelForward(bogus_field="x")

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

    def test_config_reference_operator_channel_forward_event_types_match_default(
        self,
    ) -> None:
        """Drift guard (#1597 Item C): CONFIG_REFERENCE.md's documented
        operator_channel_forward.event_types example must equal
        _DEFAULT_OPERATOR_EVENT_TYPES. The doc is prose and cannot import/derive
        from the constant, so this asserts equality instead of relying on eyeball
        review to catch drift (mirrors
        test_salvage_terminal_statuses_constant_is_single_source_of_truth,
        tests/test_reconcile_shared_sentinels.py:1341).
        """
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        doc = (
            Path(__file__).resolve().parent.parent / "config" / "CONFIG_REFERENCE.md"
        ).read_text(encoding="utf-8")
        fences = re.findall(r"```yaml\n(.*?)\n```", doc, re.DOTALL)
        matching = [f for f in fences if "operator_channel_forward" in f]
        assert len(matching) == 1, (
            f"expected exactly one fenced yaml block containing "
            f"'operator_channel_forward', found {len(matching)}"
        )
        parsed = yaml.safe_load(matching[0])
        documented = set(parsed["operator_channel_forward"]["event_types"])
        canonical = {e.value for e in _DEFAULT_OPERATOR_EVENT_TYPES}
        assert documented == canonical, (
            "CONFIG_REFERENCE.md's operator_channel_forward.event_types example "
            f"drifted from _DEFAULT_OPERATOR_EVENT_TYPES. doc has "
            f"{sorted(documented)}, constant has {sorted(canonical)}"
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

    def test_ticket_task_escalate_merge_block_fired_at_defaults_none(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme")
        assert task.escalate_merge_block_fired_at is None

    def test_ticket_task_carries_escalate_merge_block_fired_at(self) -> None:
        stamp = datetime.now(UTC)
        task = TicketTask(
            ticket_id="GEN-1", client="acme", escalate_merge_block_fired_at=stamp
        )
        assert task.escalate_merge_block_fired_at == stamp

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

    # -- RFC 0011 A3 proactive finalize hold (#1160) -------------------------

    def test_orchestrator_event_type_includes_gate_auto_approve_held(self) -> None:
        assert OrchestratorEventType.GATE_AUTO_APPROVE_HELD == "gate.auto_approve_held"

    def test_gate_auto_approve_held_in_default_forward_set(self) -> None:
        """GATE_AUTO_APPROVE_HELD IS forwarded by default: it corrects a
        GATE_AUTO_APPROVED that the A3 force hold then declined to act on."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert (
            OrchestratorEventType.GATE_AUTO_APPROVE_HELD
            in _DEFAULT_OPERATOR_EVENT_TYPES
        )

    # -- RFC 0010 P2 review-recipe act-phase surface (#1097) -----------------

    def test_orchestrator_event_type_includes_pr_action_taken(self) -> None:
        assert OrchestratorEventType.PR_ACTION_TAKEN == "pr.action_taken"

    def test_orchestrator_event_type_includes_pr_action_failed(self) -> None:
        assert OrchestratorEventType.PR_ACTION_FAILED == "pr.action_failed"

    def test_pr_action_taken_in_default_forward_set(self) -> None:
        """PR_ACTION_TAKEN IS forwarded by default: a review recipe dispatching
        an /address-review action with no human in the loop is
        operator-attention-worthy (mirrors GATE_AUTO_APPROVED)."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert OrchestratorEventType.PR_ACTION_TAKEN in _DEFAULT_OPERATOR_EVENT_TYPES

    def test_pr_action_failed_in_default_forward_set(self) -> None:
        """PR_ACTION_FAILED IS forwarded by default: it corrects a
        PR_ACTION_TAKEN whose subsequent dispatch failed, so it forwards
        alongside (same rationale as the gate pair)."""
        from cw.models import _DEFAULT_OPERATOR_EVENT_TYPES

        assert OrchestratorEventType.PR_ACTION_FAILED in _DEFAULT_OPERATOR_EVENT_TYPES

    def test_orchestrator_config_gate_recipes_enabled_defaults_false(self) -> None:
        assert OrchestratorConfig().gate_recipes_enabled is False

    def test_orchestrator_config_gate_recipes_enabled_accepts_true(self) -> None:
        cfg = OrchestratorConfig(gate_recipes_enabled=True)
        assert cfg.gate_recipes_enabled is True

    # -- GitHub #1437 ssh_key_gate operator escape hatch ---------------------

    def test_orchestrator_config_ssh_key_gate_enabled_defaults_true(self) -> None:
        assert OrchestratorConfig().ssh_key_gate_enabled is True

    def test_orchestrator_config_ssh_key_gate_enabled_accepts_false(self) -> None:
        cfg = OrchestratorConfig(ssh_key_gate_enabled=False)
        assert cfg.ssh_key_gate_enabled is False


class TestReviewRecipeKeyValidation:
    """RFC 0010 P4 (#1099): the review_recipes recognized-key set gains three
    new recipe names; an unrecognized key still fails loud on both models."""

    def test_ticket_task_accepts_all_review_recipe_names(self) -> None:
        recipes = {
            "address_review": True,
            "auto_fix_ci": True,
            "request_reviewer": False,
            "escalate_merge_block": True,
        }
        task = TicketTask(ticket_id="X", client="acme", review_recipes=recipes)
        assert task.review_recipes == recipes

    def test_lane_config_accepts_all_review_recipe_names(self) -> None:
        recipes = {
            "auto_fix_ci": True,
            "request_reviewer": True,
            "escalate_merge_block": False,
        }
        lane = LaneConfig(name="default", review_recipes=recipes)
        assert lane.review_recipes == recipes

    def test_unrecognized_review_recipe_key_still_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TicketTask(ticket_id="X", client="acme", review_recipes={"bogus": True})
        with pytest.raises(ValidationError):
            LaneConfig(name="default", review_recipes={"bogus": True})


class TestTicketIdValidation:
    """GitHub issue #1129: TicketTask.ticket_id is validated at construction
    to prevent argv/URL-injection via a malformed ticket id (e.g. a leading
    dash, an embedded '/', or a '..' path-confusion sequence)."""

    @pytest.mark.parametrize(
        "ticket_id", ["GEN-100", "1129", "ABC-1", "my.ticket_id-2"]
    )
    def test_ticket_task_accepts_typical_ticket_ids(self, ticket_id: str) -> None:
        task = TicketTask(ticket_id=ticket_id, client="acme")
        assert task.ticket_id == ticket_id

    def test_ticket_task_accepts_empty_ticket_id(self) -> None:
        """Regression guard: reconcile/local.py's LOCAL DAEMON harvest path
        constructs TicketTask(ticket_id="", ...) as a "no associated ticket"
        sentinel -- the empty string must remain exempt from format checks."""
        task = TicketTask(ticket_id="", client="acme")
        assert task.ticket_id == ""

    @pytest.mark.parametrize("ticket_id", ["redact-api#1", "dev-workspace#20"])
    def test_ticket_task_accepts_cross_repo_hash_ids(self, ticket_id: str) -> None:
        """`repo#N` is a real tracker id shape in production use.

        #1129 outlawed it, which bricked load_dev_queue() for every client the
        moment one such row was on disk. '#' is legal in a git ref name and is
        inert in argv (subprocess takes a list, no shell); its one genuine
        hazard is as a URL fragment in a `gh api` path segment, which is now
        handled by percent-encoding at that sink (cw.gh) rather than by
        outlawing the id here.
        """
        task = TicketTask(ticket_id=ticket_id, client="acme")
        assert task.ticket_id == ticket_id

    def test_ticket_task_rejects_slash(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="foo/bar", client="acme")

    def test_ticket_task_rejects_leading_dash(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="-rm-rf", client="acme")

    def test_ticket_task_rejects_leading_whitespace(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id=" GEN-1", client="acme")

    def test_ticket_task_rejects_embedded_newline(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id="GEN-1\n--flag", client="acme")

    @pytest.mark.parametrize("ticket_id", ["1..2", "../../etc"])
    def test_ticket_task_rejects_dot_dot_sequence(self, ticket_id: str) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id=ticket_id, client="acme")

    def test_ticket_task_rejects_ticket_id_starting_with_dot(self) -> None:
        with pytest.raises(ValidationError):
            TicketTask(ticket_id=".hidden", client="acme")

    def test_ticket_task_ticket_id_error_message_is_clear(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TicketTask(ticket_id="foo/bar", client="acme")
        message = str(exc_info.value)
        assert "foo/bar" in message


class TestOrchestratorConfigDisallowedMcpTools:
    """Config-driven --disallowed-tools mechanism (replaces the #726
    hard-coded, tracker-gated Linear MCP disallow) — cw.spawn.
    build_disallowed_tools_arg reads this field."""

    def test_default_is_empty_list(self) -> None:
        assert OrchestratorConfig().disallowed_mcp_tools == []

    def test_round_trips_configured_patterns(self) -> None:
        config = OrchestratorConfig(
            disallowed_mcp_tools=["mcp__plugin_linear_linear__*"]
        )
        assert config.disallowed_mcp_tools == ["mcp__plugin_linear_linear__*"]

    def test_rejects_blank_entry(self) -> None:
        with pytest.raises(ValidationError):
            OrchestratorConfig(disallowed_mcp_tools=["  "])

    def test_rejects_comma_bearing_entry(self) -> None:
        # A comma would split into two patterns at build_disallowed_tools_arg's
        # comma-join — reject it fail-loud rather than silently reinterpret.
        with pytest.raises(ValidationError):
            OrchestratorConfig(disallowed_mcp_tools=["mcp__a__*,mcp__b__*"])

    def test_model_validate_from_dict(self) -> None:
        config = OrchestratorConfig.model_validate(
            {"disallowed_mcp_tools": ["mcp__plugin_linear_linear__*", "mcp__foo__*"]}
        )
        assert config.disallowed_mcp_tools == [
            "mcp__plugin_linear_linear__*",
            "mcp__foo__*",
        ]


class TestPackageExportCompleteness:
    """Guards that the cw.models package re-exports its full public surface.

    The models.py -> cw/models/ package split (#1320) must preserve every
    ``from cw.models import X`` call site unchanged. This asserts ``__all__``
    equals the exhaustive top-level surface (the historical 49 names, plus
    #1730's ``HOOK_CONTEXT_RELATIVE_PATH`` = 50, plus #1646's three
    ``AGENT_SPAWN_*`` stamp keys = 53, plus #1646's own review-fix-loop
    addition of ``extract_unresolved_spawn_count`` = 54) — hardcoded here, NOT
    re-derived from the package, so a dropped or renamed export is a
    falsifiable failure rather than a tautology. A deliberate addition updates
    this set in the same commit.
    """

    def test_all_matches_full_surface(self) -> None:
        from cw import models

        expected = {
            "AGENT_SPAWN_LAST_STAMPED_AT_KEY",
            "AGENT_SPAWN_STAMP_KEY",
            "AGENT_SPAWN_UNRESOLVED_COUNT_KEY",
            "CLAUDE_NATIVE_BACKEND",
            "CODEX_BACKEND",
            "CONTEXT_JSON_RELATIVE_PATH",
            "CW_STATE_SCHEMA_VERSION",
            "ClientConcurrencyOverride",
            "ClientConfig",
            "CompletionReason",
            "ConcurrencyOverrides",
            "CwState",
            "DEFAULT_AUTO_PURPOSES",
            "DEFAULT_DISK_PRESSURE_MIN_FREE_GB",
            "DEFAULT_GLOBAL_ATTEMPT_CEILING",
            "DEFAULT_LANE",
            "DEFAULT_STAGE",
            "DEV_QUEUE_SCHEMA_VERSION",
            "DevQueueStore",
            "DispatchPlan",
            "DispatchSkipReason",
            "EventHookRegistry",
            "FocusEntry",
            "HOOK_CONTEXT_RELATIVE_PATH",
            "HookRule",
            "LOCAL_BACKEND",
            "LaneConcurrencyOverride",
            "LaneConfig",
            "LastResultSource",
            "LivenessBucket",
            "LocalLivenessHandle",
            "OCCUPIED_LANE_STATUSES",
            "OPENCODE_BACKEND",
            "OperatorChannelForward",
            "OrchestratorConfig",
            "OrchestratorEvent",
            "OrchestratorEventType",
            "PrState",
            "QueueItemStatus",
            "ReapPolicy",
            "ReapReason",
            "Session",
            "SessionOrigin",
            "SessionPurpose",
            "SessionStatus",
            "Stage",
            "StageExecutorConfig",
            "StagePipelineConfig",
            "TERMINAL_SESSION_STATUSES",
            "TicketTask",
            "WORKER_PURPOSES",
            "WatchedPr",
            "_DEFAULT_OPERATOR_EVENT_TYPES",
            "_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES",
            "_SAFE_TICKET_ID",
            "_USAGE_LIMIT_BACKOFF_SECONDS",
            "_validate_gate_recipe_keys",
            "_validate_review_recipe_keys",
            "extract_unresolved_spawn_count",
        }
        assert set(models.__all__) == expected


class TestExtractUnresolvedSpawnCount:
    """#1646 review fix: the one shared count-extraction rule both the
    write-side hook reader (``cw.cli.agent_spawn_stamp._unresolved_count``)
    and the read-side phantom-sweep reader
    (``cw.reconcile._shared._read_unresolved_subagent_spawn``) now delegate
    to, so the two layers cannot independently drift onto different
    validation rules for the same on-disk ``agent_spawn_stamp`` shape.
    """

    @pytest.mark.parametrize(
        ("context", "expected"),
        [
            ({"agent_spawn_stamp": {"unresolved_count": 2}}, 2),
            ({"agent_spawn_stamp": {"unresolved_count": 0}}, 0),
            ({}, 0),
            ({"agent_spawn_stamp": "not-a-dict"}, 0),
            ({"agent_spawn_stamp": {}}, 0),
            ({"agent_spawn_stamp": {"unresolved_count": True}}, 0),
            ({"agent_spawn_stamp": {"unresolved_count": "1"}}, 0),
            ({"agent_spawn_stamp": {"unresolved_count": None}}, 0),
        ],
    )
    def test_extract_unresolved_spawn_count(
        self, context: dict[str, object], expected: int
    ) -> None:
        from cw.models import extract_unresolved_spawn_count

        assert extract_unresolved_spawn_count(context) == expected
