"""RFC 0005 B2 — stage advance loop + executor-by-stage spawn tests.

Decision table unit tests for _apply_events_to_store + _stage_advance,
and an E2E acceptance test for the full staged pipeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cw.config import save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.dispatch import _apply_events_to_store, consume_completed_sessions
from cw.events import record_event
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    Stage,
    StagePipelineConfig,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_stage_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; autouse fixture handles state isolation."""
    return tmp_config_dir


@pytest.fixture
def workspace_dir(make_git_repo: Callable[[str], Path]) -> Path:
    """Real git repo for client workspace."""
    return make_git_repo("workspace/stage-test-project")


@pytest.fixture
def client_with_pipeline(workspace_dir: Path, tmp_path: Path) -> ClientConfig:
    """A ClientConfig with the default 4-stage pipeline."""
    return ClientConfig(
        name="stage-client",
        workspace_path=workspace_dir,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        pipeline=StagePipelineConfig(
            stages=[Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        ),
    )


def _make_clients_yaml(tmp_path: Path, client: ClientConfig) -> None:
    """Write a minimal clients.yaml."""
    config_dir = tmp_path / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    clients_file = config_dir / "clients.yaml"
    lines = [
        "clients:\n",
        f"  {client.name}:\n",
        f"    workspace_path: {client.workspace_path}\n",
        f"    default_branch: {client.default_branch}\n",
    ]
    if client.worktree_base is not None:
        lines.append(f"    worktree_base: {client.worktree_base}\n")
    lines += [
        "    pipeline:\n",
        "      stages: [plan, impl, review, finalize]\n",
    ]
    clients_file.write_text("".join(lines))


def _make_session(
    session_id: str,
    ticket_id: str,
    client_name: str,
    workspace_path: Path,
    last_result: dict | None = None,
) -> Session:
    return Session(
        id=session_id,
        name=f"{client_name}/auto-dev/{ticket_id}",
        client=client_name,
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=workspace_path,
        last_result=last_result,
    )


def _running_task(
    ticket_id: str,
    client: str,
    session_id: str,
    stage: Stage = Stage.PLAN,
    scope_hint: str | None = None,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        stage=stage,
        scope_hint=scope_hint,
    )


def _apply_and_check(
    *,
    ticket_id: str,
    session_id: str,
    client_name: str,
    workspace_path: Path,
    last_result: dict | None,
    clients: dict,
    initial_stage: Stage = Stage.PLAN,
    scope_hint: str | None = None,
) -> tuple[QueueItemStatus, Stage | None]:
    """Set up store + state, call _apply_events_to_store, return (status, stage)."""
    task = _running_task(
        ticket_id, client_name, session_id, stage=initial_stage, scope_hint=scope_hint
    )
    store = DevQueueStore(tasks=[task])

    sess = _make_session(
        session_id, ticket_id, client_name, workspace_path, last_result
    )
    save_state(CwState(sessions=[sess]))

    event = OrchestratorEvent(
        id=f"evt-{ticket_id}",
        type=OrchestratorEventType.SESSION_COMPLETED,
        payload={"ticket_id": ticket_id, "session_id": session_id},
    )

    with dev_queue_lock():
        _apply_events_to_store(store, [event], clients)

    updated = store.tasks[0]
    return updated.status, updated.stage


# ---------------------------------------------------------------------------
# StagePipelineConfig model validation
# ---------------------------------------------------------------------------


class TestStagePipelineConfigValidation:
    """Validator tests for StagePipelineConfig (RFC 0005 B2)."""

    def test_duplicate_stages_rejected(self) -> None:
        """Duplicate stages in pipeline config raise ValidationError."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="unique"):
            StagePipelineConfig(stages=[Stage.PLAN, Stage.PLAN, Stage.IMPL])

    def test_unique_stages_accepted(self) -> None:
        cfg = StagePipelineConfig(
            stages=[Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        )
        assert len(cfg.stages) == 4


# ---------------------------------------------------------------------------
# Decision table unit tests (6 rules)
# ---------------------------------------------------------------------------


class TestDecisionTable:
    """Unit tests for the 6-rule decision table in _apply_events_to_store."""

    def test_rule1_ambiguities_pending_resolution(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 1: ambiguities_pending_resolution -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R1A",
            session_id="sess-r1a",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "ambiguities_pending_resolution",
                "schema_version": 4,
            },
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule1_premises_pending_verification(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 1: premises_pending_verification -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R1B",
            session_id="sess-r1b",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "premises_pending_verification",
                "schema_version": 4,
            },
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule2a_plan_pending_approval_small_tier_advances(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2a: plan_pending_approval + scope.tier=small -> advances to IMPL."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, stage = _apply_and_check(
            ticket_id="T-R2A",
            session_id="sess-r2a",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "plan_pending_approval",
                "schema_version": 4,
                "scope": {"tier": "small"},
            },
            clients=clients,
            initial_stage=Stage.PLAN,
        )
        assert status == QueueItemStatus.PENDING
        assert stage == Stage.IMPL

    def test_rule2b_plan_pending_approval_large_tier_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2b: plan_pending_approval + scope.tier=large -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _stage = _apply_and_check(
            ticket_id="T-R2B",
            session_id="sess-r2b",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "plan_pending_approval",
                "schema_version": 4,
                "scope": {"tier": "large"},
            },
            clients=clients,
            initial_stage=Stage.PLAN,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule2c_review_pending_approval_small_tier_advances(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2c: review_pending_approval + scope.tier=small -> advances."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, stage = _apply_and_check(
            ticket_id="T-R2C",
            session_id="sess-r2c",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "review_pending_approval",
                "schema_version": 4,
                "scope": {"tier": "small"},
            },
            clients=clients,
            initial_stage=Stage.REVIEW,
        )
        assert status == QueueItemStatus.PENDING
        assert stage == Stage.FINALIZE

    def test_rule2d_scope_tier_none_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2d: missing scope.tier + no scope_hint -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R2D",
            session_id="sess-r2d",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "plan_pending_approval",
                "schema_version": 4,
                # no scope key
            },
            clients=clients,
            initial_stage=Stage.PLAN,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule2e_null_tier_scope_hint_small_advances(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2e (#696): real PLAN sentinel has scope.tier=null; task.scope_hint
        ='small' resolves the tier and the stage advances PLAN->IMPL.

        Reproduces the #663 dogfood sentinel verbatim: a small ticket whose plan
        stage emits scope.tier=null (lines_actual unknown pre-impl) must still
        auto-advance via the scope_hint fallback, mirroring reconcile's
        tier-unavailable resolution. Fails before the fix (null != "small" ->
        BLOCKED_ON_USER).
        """
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, stage = _apply_and_check(
            ticket_id="T-R2E",
            session_id="sess-r2e",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "plan_pending_approval",
                "schema_version": 4,
                "scope": {
                    "tier": None,
                    "files": 4,
                    "lines_estimate": 258,
                    "lines_actual": None,
                    "forbidden_touched": False,
                },
            },
            clients=clients,
            initial_stage=Stage.PLAN,
            scope_hint="small",
        )
        assert status == QueueItemStatus.PENDING
        assert stage == Stage.IMPL

    def test_rule2f_null_tier_scope_hint_large_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 2f (#696): scope.tier=null + scope_hint='large' -> BLOCKED_ON_USER.

        The scope_hint fallback resolves the tier as large, so the gate parks
        for operator approval rather than auto-advancing.
        """
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R2F",
            session_id="sess-r2f",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={
                "status": "plan_pending_approval",
                "schema_version": 4,
                "scope": {"tier": None},
            },
            clients=clients,
            initial_stage=Stage.PLAN,
            scope_hint="large",
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule3_shipped_advances(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 3: shipped -> advances to next stage."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, stage = _apply_and_check(
            ticket_id="T-R3A",
            session_id="sess-r3a",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "shipped", "schema_version": 4},
            clients=clients,
            initial_stage=Stage.PLAN,
        )
        assert status == QueueItemStatus.PENDING
        assert stage == Stage.IMPL

    def test_rule3_shipped_at_terminal_stage_completed(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 3: shipped at terminal stage (FINALIZE) -> COMPLETED."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R3B",
            session_id="sess-r3b",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "shipped", "schema_version": 4},
            clients=clients,
            initial_stage=Stage.FINALIZE,
        )
        assert status == QueueItemStatus.COMPLETED

    def test_rule3_stage_complete_advances_impl_to_review(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 3: stage_complete at IMPL advances to REVIEW (RFC 0005 B2, #699).

        IMPL does not create a PR — it emits stage_complete (not shipped) so
        the schema validators don't require a non-null pr or wait_for_ci.
        The B2 advance machine must still route stage_complete via _stage_advance.
        """
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, stage = _apply_and_check(
            ticket_id="T-R3C",
            session_id="sess-r3c",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "stage_complete", "schema_version": 4},
            clients=clients,
            initial_stage=Stage.IMPL,
        )
        assert status == QueueItemStatus.PENDING
        assert stage == Stage.REVIEW

    def test_rule3_stage_complete_at_terminal_stage_completed(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 3: stage_complete at terminal stage (FINALIZE) -> COMPLETED."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R3D",
            session_id="sess-r3d",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "stage_complete", "schema_version": 4},
            clients=clients,
            initial_stage=Stage.FINALIZE,
        )
        assert status == QueueItemStatus.COMPLETED

    def test_rule4_no_op_completed(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 4: no_op -> COMPLETED (terminal, regardless of remaining stages)."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        # Even at PLAN stage (not terminal), no_op is terminal
        status, _ = _apply_and_check(
            ticket_id="T-R4",
            session_id="sess-r4",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "no_op", "schema_version": 2},
            clients=clients,
            initial_stage=Stage.PLAN,
        )
        assert status == QueueItemStatus.COMPLETED

    def test_rule5_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 5: blocked -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R5A",
            session_id="sess-r5a",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "blocked", "schema_version": 4},
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule5_merge_gate_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 5: merge_gate_blocked -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R5B",
            session_id="sess-r5b",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "merge_gate_blocked", "schema_version": 4},
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule5_scope_exceeded(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 5: scope_exceeded -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R5C",
            session_id="sess-r5c",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "scope_exceeded", "schema_version": 4},
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule5_forbidden_area(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 5: forbidden_area -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R5D",
            session_id="sess-r5d",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"status": "forbidden_area", "schema_version": 4},
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule6_last_result_none_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 6: last_result=None -> BLOCKED_ON_USER (conservative fallback)."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R6A",
            session_id="sess-r6a",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result=None,
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_rule6_last_result_no_status_key_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Rule 6: last_result dict missing status key -> BLOCKED_ON_USER."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        status, _ = _apply_and_check(
            ticket_id="T-R6B",
            session_id="sess-r6b",
            client_name=client_with_pipeline.name,
            workspace_path=client_with_pipeline.workspace_path,
            last_result={"schema_version": 4},  # no status key
            clients=clients,
        )
        assert status == QueueItemStatus.BLOCKED_ON_USER

    def test_idempotency_second_pass_noop(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """Idempotency: same event processed twice is a no-op (already transitioned)."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        task = _running_task("T-IDEM", client_with_pipeline.name, "sess-idem")
        store = DevQueueStore(tasks=[task])
        sess = _make_session(
            "sess-idem",
            "T-IDEM",
            client_with_pipeline.name,
            client_with_pipeline.workspace_path,
            {"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        event = OrchestratorEvent(
            id="evt-idem",
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={"ticket_id": "T-IDEM", "session_id": "sess-idem"},
        )

        with dev_queue_lock():
            count1 = _apply_events_to_store(store, [event], clients)
        # First pass: advances from PLAN to PENDING/IMPL
        assert count1 == 1
        assert store.tasks[0].status == QueueItemStatus.PENDING

        # Second pass: task is now PENDING, not RUNNING -- skip
        with dev_queue_lock():
            count2 = _apply_events_to_store(store, [event], clients)
        assert count2 == 0

    def test_missing_client_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """R1 guard: client not found in clients dict -> BLOCKED_ON_USER."""
        # Use empty clients dict to simulate missing client
        clients: dict = {}
        task = _running_task("T-MC", client_with_pipeline.name, "sess-mc")
        store = DevQueueStore(tasks=[task])
        sess = _make_session(
            "sess-mc",
            "T-MC",
            client_with_pipeline.name,
            client_with_pipeline.workspace_path,
            {"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        event = OrchestratorEvent(
            id="evt-mc",
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={"ticket_id": "T-MC", "session_id": "sess-mc"},
        )

        with dev_queue_lock():
            _apply_events_to_store(store, [event], clients)

        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_stage_not_in_pipeline_blocked(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """R1 guard: task stage not in pipeline -> BLOCKED_ON_USER."""
        # Single-stage pipeline (only PLAN), task is at FINALIZE
        single_stage_client = ClientConfig(
            name="stage-client",
            workspace_path=client_with_pipeline.workspace_path,
            pipeline=StagePipelineConfig(stages=[Stage.PLAN]),
        )
        overridden_clients = {single_stage_client.name: single_stage_client}

        task = _running_task(
            "T-SIP",
            single_stage_client.name,
            "sess-sip",
            stage=Stage.FINALIZE,  # not in single-stage pipeline
        )
        store = DevQueueStore(tasks=[task])
        sess = _make_session(
            "sess-sip",
            "T-SIP",
            single_stage_client.name,
            client_with_pipeline.workspace_path,
            {"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        event = OrchestratorEvent(
            id="evt-sip",
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={"ticket_id": "T-SIP", "session_id": "sess-sip"},
        )

        with dev_queue_lock():
            _apply_events_to_store(store, [event], overridden_clients)

        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_session_id_cleared_on_advance(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """R6: session_id is cleared to None when task advances stage."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        task = _running_task(
            "T-SID", client_with_pipeline.name, "sess-sid", stage=Stage.PLAN
        )
        assert task.session_id == "sess-sid"
        store = DevQueueStore(tasks=[task])

        sess = _make_session(
            "sess-sid",
            "T-SID",
            client_with_pipeline.name,
            client_with_pipeline.workspace_path,
            {"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        event = OrchestratorEvent(
            id="evt-sid",
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={"ticket_id": "T-SID", "session_id": "sess-sid"},
        )

        with dev_queue_lock():
            _apply_events_to_store(store, [event], clients)

        updated = store.tasks[0]
        assert updated.status == QueueItemStatus.PENDING
        assert updated.stage == Stage.IMPL
        assert updated.session_id is None  # R6: cleared on advance

    def test_stage_base_ref_cleared_on_advance(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
    ) -> None:
        """stage_base_ref is cleared to None on stage advance (RFC idempotency)."""
        _make_clients_yaml(tmp_stage_dirs, client_with_pipeline)
        from cw.config import load_effective_clients

        clients = load_effective_clients()
        task = _running_task(
            "T-SBR", client_with_pipeline.name, "sess-sbr", stage=Stage.PLAN
        )
        task.stage_base_ref = "abc123def456"  # pre-stamped from prior spawn
        store = DevQueueStore(tasks=[task])

        sess = _make_session(
            "sess-sbr",
            "T-SBR",
            client_with_pipeline.name,
            client_with_pipeline.workspace_path,
            {"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        event = OrchestratorEvent(
            id="evt-sbr",
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={"ticket_id": "T-SBR", "session_id": "sess-sbr"},
        )

        with dev_queue_lock():
            _apply_events_to_store(store, [event], clients)

        updated = store.tasks[0]
        assert updated.status == QueueItemStatus.PENDING
        assert updated.stage == Stage.IMPL
        assert updated.stage_base_ref is None  # RFC: cleared so next spawn re-stamps


# ---------------------------------------------------------------------------
# stage_base_ref stamping error path
# ---------------------------------------------------------------------------


class TestStageBaseRefStamping:
    """Tests for the stage_base_ref git-rev-parse block in dispatch_tick."""

    def test_stage_base_ref_subprocess_error_is_nonfatal(
        self,
        tmp_stage_dirs: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """SubprocessError in git rev-parse: stage_base_ref stays None, spawn ok."""
        import unittest.mock

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.dispatch import dispatch_tick
        from cw.models import DevQueueStore, TicketTask

        worktree = make_git_repo("wt-sbr-error")
        client_cfg = ClientConfig(
            name="sbr-client",
            workspace_path=worktree,
            default_branch="main",
            worktree_base=tmp_stage_dirs / "worktrees",
            pipeline=StagePipelineConfig(
                stages=[Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
            ),
        )

        config_dir = tmp_stage_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n"
            f"  {client_cfg.name}:\n"
            f"    workspace_path: {client_cfg.workspace_path}\n"
            f"    default_branch: {client_cfg.default_branch}\n"
            f"    worktree_base: {client_cfg.worktree_base}\n"
            f"    pipeline:\n"
            f"      stages: [plan, impl, review, finalize]\n"
        )

        task = TicketTask(
            ticket_id="T-SBR-ERR",
            client=client_cfg.name,
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        cfg = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"sbr-client": 1},
        )
        with unittest.mock.patch(
            "cw.dispatch.subprocess.check_output",
            side_effect=subprocess.SubprocessError("git failure"),
        ):
            dispatch_tick(cfg, native_daemon=mock_native_daemon)

        updated_store = load_dev_queue()
        updated_task = next(
            (t for t in updated_store.tasks if t.ticket_id == "T-SBR-ERR"), None
        )
        assert updated_task is not None
        assert updated_task.status == QueueItemStatus.RUNNING
        assert updated_task.stage_base_ref is None  # not stamped due to error
        assert len(mock_native_daemon.spawn_calls) == 1  # spawn still succeeded


# ---------------------------------------------------------------------------
# E2E acceptance test
# ---------------------------------------------------------------------------


class TestFullStagedPipelineE2E:
    """Full staged pipeline acceptance test (B2 correctness requirement)."""

    def test_full_staged_pipeline_e2e(
        self,
        tmp_stage_dirs: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Full staged pipeline: PLAN->IMPL->REVIEW->FINALIZE->COMPLETED.

        Guards against the regression where consumer-half-only B2 advanced a
        shipped task to PENDING (re-spawning the monolith on an already-merged ticket).
        """
        from cw.dispatch import dispatch_tick
        from cw.models import OrchestratorConfig

        worktree = make_git_repo("wt-e2e")

        # Write clients.yaml with 4-stage pipeline
        config_dir = tmp_stage_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        clients_file = config_dir / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  e2e-client:\n"
            f"    workspace_path: {worktree}\n"
            "    default_branch: main\n"
            f"    worktree_base: {tmp_stage_dirs / 'worktrees'}\n"
            "    pipeline:\n"
            "      stages: [plan, impl, review, finalize]\n"
        )

        orchestrator_config_dir = tmp_stage_dirs / ".claude-workspace"
        orchestrator_config_dir.mkdir(parents=True, exist_ok=True)
        orchestrator_config_file = orchestrator_config_dir / "orchestrator.yaml"
        orchestrator_config_file.write_text(
            yaml.dump(
                {
                    "tick_interval_seconds": 30,
                    "per_client_max_parallel": {"e2e-client": 1},
                }
            )
        )

        # Create PLAN-stage PENDING task
        task = TicketTask(
            ticket_id="E2E-1",
            client="e2e-client",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        stages = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        expected_prompts = [
            "/auto-dev-plan E2E-1 --headless",
            "/auto-dev-impl E2E-1 --headless",
            "/auto-dev-review E2E-1 --headless",
            "/auto-dev-finalize E2E-1 --headless",
        ]
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"e2e-client": 1},
        )

        for i, stage in enumerate(stages):
            # Tick should spawn the stage
            result = dispatch_tick(
                config=config,
                native_daemon=mock_native_daemon,
            )
            assert result.spawned == 1, (
                f"stage {stage}: expected 1 spawn, got {result.spawned}"
            )

            # Verify prompt for this stage
            assert len(mock_native_daemon.spawn_calls) == i + 1
            actual_prompt = mock_native_daemon.spawn_calls[i][1]
            assert actual_prompt == expected_prompts[i], (
                f"stage {stage}: expected {expected_prompts[i]!r},"
                f" got {actual_prompt!r}"
            )

            # Get the session_id that was assigned
            running_tasks = [
                t for t in load_dev_queue().tasks if t.status == QueueItemStatus.RUNNING
            ]
            assert len(running_tasks) == 1
            session_id = running_tasks[0].session_id
            assert session_id is not None

            # Simulate shipped result -- mark session COMPLETED so worktree
            # reuse guard does not block the next stage spawn.
            sess = Session(
                id=session_id,
                name="e2e-client/auto-dev/E2E-1",
                client="e2e-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.COMPLETED,
                workspace_path=worktree,
                last_result={"status": "shipped", "schema_version": 4},
            )
            save_state(CwState(sessions=[sess]))

            record_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {"ticket_id": "E2E-1", "session_id": session_id},
            )

            completed = consume_completed_sessions()
            assert completed == 1

            tasks = load_dev_queue().tasks
            assert len(tasks) == 1
            current_task = tasks[0]

            if stage == Stage.FINALIZE:
                # Last stage: should be COMPLETED
                assert current_task.status == QueueItemStatus.COMPLETED, (
                    f"After FINALIZE shipped: expected COMPLETED,"
                    f" got {current_task.status}"
                )
            else:
                # Intermediate stage: should advance to PENDING with next stage
                assert current_task.status == QueueItemStatus.PENDING, (
                    f"After {stage} shipped: expected PENDING,"
                    f" got {current_task.status}"
                )
                assert current_task.stage == stages[i + 1], (
                    f"After {stage} shipped: expected stage {stages[i + 1]},"
                    f" got {current_task.stage}"
                )
                assert current_task.session_id is None, (
                    "R6: session_id must be cleared on advance"
                )

        # Assert exactly 4 spawns total (no extra re-spawns)
        assert len(mock_native_daemon.spawn_calls) == 4, (
            f"Expected 4 total spawns, got {len(mock_native_daemon.spawn_calls)}"
        )
