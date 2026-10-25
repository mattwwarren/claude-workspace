"""RFC 0005 B2 — stage advance loop + executor-by-stage spawn tests.

Decision table unit tests for _apply_events_to_store + _stage_advance,
and an E2E acceptance test for the full staged pipeline.

#2767 adds the large-tier review approval path end to end: the codex review
parks a large ticket for approval, ``approve`` records the approval on the row,
and finalize's Step 4.0 fence (executed here against real git repos) is the
gate that lets it ship.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.schema._vocab import FINALIZE_REGRESS_CAP
from cw.codex_review import synthesize_codex_review_result
from cw.config import load_effective_clients, save_state
from cw.context_queue_metadata import REVIEW_APPROVAL_KEY, build_queue_metadata
from cw.dev_queue import (
    approve_ticket,
    dev_queue_lock,
    load_dev_queue,
    requeue_ticket,
    save_dev_queue,
)
from cw.dispatch import (
    _apply_events_to_store,
    apply_staged_decision,
    consume_completed_sessions,
)
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.gate_block import GateBlockBackoff
from cw.native_daemon import FakeNativeDaemonClient
from cw.spawn import CW_CONTEXT_SCHEMA_VERSION
from tests._clients_yaml import write_clients_yaml
from tests.conftest import (
    _bash_fences,
    _clean_git_env,
    _cmd,
    _make_daemon_session,
    _make_diff,
    _make_reviewer_doc,
    _make_ticket_task,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
    substitute_fence_placeholders,
)

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


def _make_session(
    session_id: str,
    ticket_id: str,
    client_name: str,
    workspace_path: Path,
    last_result: dict | None = None,
) -> Session:
    return _make_daemon_session(
        id=session_id,
        name=f"{client_name}/auto-dev/{ticket_id}",
        client=client_name,
        origin=SessionOrigin.USER,
        workspace_path=workspace_path,
        surface_ref=None,
        worktree_path=None,
        started_at=datetime.now(UTC),
        last_result=last_result,
    )


def _running_task(
    ticket_id: str,
    client: str,
    session_id: str,
    stage: Stage = Stage.PLAN,
    scope_hint: str | None = None,
) -> TicketTask:
    return _make_ticket_task(
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
        write_clients_yaml(client_with_pipeline)
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
            "cw.dispatch.claim.claimed_row.git_output",
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
            # reuse guard does not block the next stage spawn. Also stop the
            # fake worker (#2213 round 7): occupancy now reads the injected
            # daemon for real, and a real daemon's roster reflects a worker's
            # process exiting independently of cw's own session status.
            for short_id in mock_native_daemon.list_live_session_short_ids():
                mock_native_daemon.stop(short_id)
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


# ---------------------------------------------------------------------------
# #2767 — finalize Step 4.0: the large-tier review approval check
# ---------------------------------------------------------------------------

_FINALIZE_DOC = "auto-dev-finalize.md"
_GUARD_HEADING = "### Pre-Stage Detector Guard"
_STEP40_HEADING = "### Step 4.0: Large-Tier Review Approval Check (headless only)"
_STEP4A_HEADING = "### Step 4a: Merge Gate Check"
_TIER_PLACEHOLDER = "resolved scope.tier"
_RECORDED = "REVIEW_APPROVAL_RECORDED"
_NOT_REQUIRED = "REVIEW_APPROVAL_NOT_REQUIRED"
_PARK = "REVIEW_APPROVAL_PARK: "
_NUMSTAT = '"<N from git diff --numstat>"'
_REVIEW_RELEASE = "cw dev-queue requeue <ticket-id> -c <client> --stage review"
_LARGE_MARKER = "**Scope tier:** large (17 files, ~1280 lines, forbidden_touched=false)"
_SHORT_SHA_LEN = 7
_ABSENT_SHA = "deadbeef" * 5
_PARK_TICKET = "T-2767"


def _step40_section() -> str:
    content = _cmd(_FINALIZE_DOC)
    start = content.index(_STEP40_HEADING)
    return content[start : content.index(_STEP4A_HEADING, start)]


def _guard_section() -> str:
    content = _cmd(_FINALIZE_DOC)
    start = content.index(_GUARD_HEADING)
    return content[start : content.index(_STEP40_HEADING, start)]


def _guard_bullet(marker: str) -> str:
    bullets = [
        line
        for line in _guard_section().splitlines()
        if line.startswith(f"- **{marker}")
    ]
    assert len(bullets) == 1, f"expected one Guard bullet for {marker!r}: {bullets}"
    return bullets[0]


def _step40_fence() -> str:
    fences = [fence for fence in _bash_fences(_step40_section()) if _RECORDED in fence]
    assert len(fences) == 1, f"expected one Step 4.0 fence, got {len(fences)}"
    return fences[0]


def _park_template() -> str:
    """The park sentinel JSON template (the one ```json fence in Step 4.0)."""
    match = re.search(r"```json\n(.*?)\n\s*```", _step40_section(), re.DOTALL)
    assert match is not None, "Step 4.0 carries no ```json park template"
    return textwrap.dedent(match.group(1))


def _run_step40_fence(
    worktree: Path, tier: str | None, *, env: dict[str, str] | None = None
) -> str:
    """Run the Step 4.0 fence from *worktree*; return its one verdict line.

    ``tier=None`` leaves ``<resolved scope.tier>`` unsubstituted, the shape a
    worker that skipped the substitution would run. The substitution goes
    through ``substitute_fence_placeholders``, so every other byte of the fence
    is the doc's own.
    """
    placeholders = {} if tier is None else {_TIER_PLACEHOLDER: tier}
    fence = substitute_fence_placeholders(_step40_fence(), placeholders)
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, "-c", fence],
        cwd=worktree,
        env=env if env is not None else _clean_git_env(),
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _assert_park(output: str, token: str) -> None:
    assert output.startswith(f"{_PARK}{token} — "), output
    assert _RECORDED not in output
    assert _NOT_REQUIRED not in output


def _head(worktree: Path) -> str:
    return git_in(worktree, "rev-parse", "HEAD")


def _approval_task(sha: str | None, kind: str = "operator") -> TicketTask:
    return _make_ticket_task(
        stage=Stage.FINALIZE,
        review_approved_at=datetime.now(UTC),
        review_approver_kind=kind,
        review_approved_sha=sha,
    )


def _context(
    worktree: Path, task: TicketTask, **overrides: object
) -> dict[str, object]:
    """A cw-context payload as spawn writes it, with *task*'s queue metadata."""
    payload: dict[str, object] = {
        "schema_version": CW_CONTEXT_SCHEMA_VERSION,
        "worktree_path": str(worktree),
        "queue_metadata": build_queue_metadata(task),
        "world_state_snapshot": {"origin_main_branch": "main"},
    }
    payload.update(overrides)
    return payload


def _write_context(worktree: Path, payload: dict[str, object] | str) -> None:
    path = worktree / ".claude" / "cw-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def _approve_at(worktree: Path, sha: str | None, kind: str = "operator") -> None:
    _write_context(worktree, _context(worktree, _approval_task(sha, kind)))


def _merge_commit(worktree: Path, *parents: str) -> str:
    """Commit a merge of *parents* (first parent's tree) and move HEAD to it."""
    parent_args = [arg for parent in parents for arg in ("-p", parent)]
    sha = git_in(
        worktree, "commit-tree", f"{parents[0]}^{{tree}}", *parent_args, "-m", "merge"
    )
    git_in(worktree, "reset", "--hard", sha)
    return sha


def _advance_main(origin: Path, worktree: Path, side: Path, filename: str) -> str:
    """Land one commit on ``origin/main`` and fetch it into *worktree*."""
    sha = push_commit_to_origin(origin, "main", side, filename)
    git_in(worktree, "fetch", "origin")
    return sha


def _sync_merge(worktree: Path) -> None:
    """Finalize's own Step 4c sync merge of the default branch."""
    git_in(worktree, "merge", "--no-ff", "--no-edit", "origin/main")


@pytest.fixture
def approval_repo(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> tuple[Path, Path]:
    """A feature branch pushed to a bare origin, approved at its HEAD."""
    worktree, origin = make_git_repo_with_origin("wt-step40")
    _approve_at(worktree, _head(worktree))
    return worktree, origin


class TestStep40TierBranch:
    """R12: only an exact ``small`` or ``large`` tier gets past the tier branch.

    Every non-exact case runs on a repo whose large approval is valid, so a
    fail-open fence would print ``RECORDED`` or ``NOT_REQUIRED`` here.
    """

    def test_valid_large_approval_records(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        assert _run_step40_fence(worktree, "large") == _RECORDED

    @pytest.mark.parametrize(
        "tier",
        [
            None,
            "",
            "Large",
            "SMALL",
            "Small",
            "scope_tier_unresolvable",
            "null",
            "x; echo REVIEW_APPROVAL_NOT_REQUIRED",
        ],
        ids=[
            "unsubstituted",
            "empty",
            "Large",
            "SMALL",
            "Small",
            "scope_tier_unresolvable",
            "null",
            "garbage",
        ],
    )
    def test_non_exact_tier_parks_unresolved_tier(
        self, approval_repo: tuple[Path, Path], tier: str | None
    ) -> None:
        worktree, _ = approval_repo
        _assert_park(_run_step40_fence(worktree, tier), "unresolved_tier")

    def test_exact_small_needs_no_approval_or_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-step40-small")
        assert not (worktree / ".claude" / "cw-context.json").exists()
        assert _run_step40_fence(worktree, "small") == _NOT_REQUIRED


class TestStep40ApprovalRule:
    """R11 on real git history: the approval holds for the head about to ship
    only if nothing but merges of the default branch landed after it."""

    @pytest.mark.parametrize(
        ("sha_len", "kind"),
        [(None, "operator"), (_SHORT_SHA_LEN, "operator"), (None, "recipe")],
        ids=["full", "abbreviated", "recipe"],
    )
    def test_approval_at_head_records(
        self, approval_repo: tuple[Path, Path], sha_len: int | None, kind: str
    ) -> None:
        worktree, _ = approval_repo
        _approve_at(worktree, _head(worktree)[:sha_len], kind)
        assert _run_step40_fence(worktree, "large") == _RECORDED

    @pytest.mark.parametrize(
        "sha_len", [None, _SHORT_SHA_LEN], ids=["full", "abbreviated"]
    )
    def test_successive_sync_merges_of_default_branch_record(
        self, approval_repo: tuple[Path, Path], tmp_path: Path, sha_len: int | None
    ) -> None:
        worktree, origin = approval_repo
        _approve_at(worktree, _head(worktree)[:sha_len])
        for filename in ("main-1.txt", "main-2.txt"):
            _advance_main(origin, worktree, tmp_path / "side", filename)
            _sync_merge(worktree)
            assert _run_step40_fence(worktree, "large") == _RECORDED

    def test_rerun_after_own_pushed_sync_merge_records(
        self, approval_repo: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A1: a crashed run already pushed finalize's sync merge (and maybe
        opened the PR). Both Guard ship bullets route the re-run through Step
        4.0, which sees exactly this state and lets it continue."""
        worktree, origin = approval_repo
        _advance_main(origin, worktree, tmp_path / "side", "main-1.txt")
        _sync_merge(worktree)
        git_in(worktree, "push", "origin", "HEAD:refs/heads/feature")

        for marker in ("PR exists and is OPEN", "No PR exists"):
            assert "Step 4.0" in _guard_bullet(marker)
        assert _run_step40_fence(worktree, "large") == _RECORDED

    def test_fix_commit_after_approval_is_stale(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        commit_tracked_file(worktree, "fix.py")
        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert "non-merge commit(s) landed after the approval" in output

    def test_fix_commit_after_sync_merge_is_stale(
        self, approval_repo: tuple[Path, Path], tmp_path: Path
    ) -> None:
        worktree, origin = approval_repo
        _advance_main(origin, worktree, tmp_path / "side", "main-1.txt")
        _sync_merge(worktree)
        commit_tracked_file(worktree, "fix.py")
        _assert_park(_run_step40_fence(worktree, "large"), "stale")

    def test_merge_of_non_default_branch_with_its_own_commit_is_stale(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        git_in(worktree, "checkout", "-b", "side", "origin/main")
        commit_tracked_file(worktree, "side.py")
        git_in(worktree, "checkout", "feature")
        git_in(worktree, "merge", "--no-ff", "--no-edit", "side")
        _assert_park(_run_step40_fence(worktree, "large"), "stale")

    def test_merge_of_merge_only_foreign_ref_is_stale(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        """No non-merge commit lands, so only the merge-parent rule sees it."""
        worktree, _ = approval_repo
        approved = _head(worktree)
        main = git_in(worktree, "rev-parse", "origin/main")
        foreign = git_in(
            worktree,
            "commit-tree",
            f"{approved}^{{tree}}",
            "-p",
            main,
            "-p",
            approved,
            "-m",
            "foreign",
        )
        _merge_commit(worktree, approved, foreign)
        assert not git_in(worktree, "rev-list", "--no-merges", f"{approved}..HEAD")

        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert foreign in output

    def test_octopus_with_foreign_parent_last_is_stale(
        self, approval_repo: tuple[Path, Path], tmp_path: Path
    ) -> None:
        worktree, origin = approval_repo
        approved = _head(worktree)
        main = _advance_main(origin, worktree, tmp_path / "side", "main-1.txt")
        foreign = git_in(
            worktree,
            "commit-tree",
            f"{approved}^{{tree}}",
            "-p",
            main,
            "-p",
            approved,
            "-m",
            "foreign",
        )
        _merge_commit(worktree, approved, main, foreign)

        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert foreign in output

    def test_rewritten_history_is_stale(self, approval_repo: tuple[Path, Path]) -> None:
        """The approved head survives in the object store but left history."""
        worktree, _ = approval_repo
        approved = _head(worktree)
        git_in(worktree, "reset", "--hard", "HEAD~1")
        commit_tracked_file(worktree, "rewritten.py")
        assert git_in(worktree, "cat-file", "-t", approved) == "commit"

        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert "is not an ancestor of head" in output

    def test_reviewed_head_ahead_of_head_is_stale(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        commit_tracked_file(worktree, "ahead.py")
        _approve_at(worktree, _head(worktree))
        git_in(worktree, "reset", "--hard", "HEAD~1")

        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert "is not an ancestor of head" in output

    def test_reviewed_sha_not_in_repository_is_stale(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        _approve_at(worktree, _ABSENT_SHA)
        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert "is not a commit in this repository" in output

    def test_open_pr_without_approval_parks_missing(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        """The branch is pushed (the state an open PR leaves) and no approval is
        recorded: an existing PR is never a reason to ship a large ticket."""
        worktree, _ = approval_repo
        assert git_in(worktree, "rev-parse", "origin/feature") == _head(worktree)
        payload = _context(worktree, _make_ticket_task(stage=Stage.FINALIZE))
        _write_context(worktree, payload)

        _assert_park(_run_step40_fence(worktree, "large"), "missing")


class TestStep40ChannelAndTooling:
    """R5: a missing or unreadable approval channel, or missing tooling, parks."""

    def test_no_approval_recorded_parks_missing(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        _write_context(worktree, _context(worktree, _make_ticket_task()))
        _assert_park(_run_step40_fence(worktree, "large"), "missing")

    def test_unknown_approver_kind_parks_missing(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        metadata = build_queue_metadata(_approval_task(_head(worktree)))
        metadata[REVIEW_APPROVAL_KEY] = {
            "approved_at": datetime.now(UTC).isoformat(),
            "approver_kind": "robot",
            "reviewed_sha": _head(worktree),
        }
        _write_context(
            worktree, _context(worktree, _make_ticket_task(), queue_metadata=metadata)
        )
        _assert_park(_run_step40_fence(worktree, "large"), "missing")

    @pytest.mark.parametrize("sha", [None, "not-a-sha"], ids=["null", "malformed"])
    def test_unusable_reviewed_sha_parks_stale(
        self, approval_repo: tuple[Path, Path], sha: str | None
    ) -> None:
        worktree, _ = approval_repo
        _approve_at(worktree, sha)
        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "stale")
        assert "carries no usable reviewed head" in output

    @pytest.mark.parametrize(
        "schema_version",
        [CW_CONTEXT_SCHEMA_VERSION - 1, str(CW_CONTEXT_SCHEMA_VERSION)],
        ids=["v13", "string"],
    )
    def test_old_or_malformed_schema_parks_channel_absent(
        self, approval_repo: tuple[Path, Path], schema_version: object
    ) -> None:
        worktree, _ = approval_repo
        task = _approval_task(_head(worktree))
        _write_context(
            worktree, _context(worktree, task, schema_version=schema_version)
        )
        _assert_park(_run_step40_fence(worktree, "large"), "channel_absent")

    def test_missing_context_parks_unreadable(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        (worktree / ".claude" / "cw-context.json").unlink()
        _assert_park(_run_step40_fence(worktree, "large"), "unreadable")

    def test_invalid_json_context_parks_unreadable(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        _write_context(worktree, "{not json")
        _assert_park(_run_step40_fence(worktree, "large"), "unreadable")

    def test_missing_jq_parks_unreadable(
        self, approval_repo: tuple[Path, Path], tmp_path: Path
    ) -> None:
        worktree, _ = approval_repo
        bin_dir = tmp_path / "bin-without-jq"
        bin_dir.mkdir()
        for tool in ("git", "grep", "tr", "cut"):
            found = shutil.which(tool)
            assert found is not None
            (bin_dir / tool).symlink_to(found)
        env = {**_clean_git_env(), "PATH": str(bin_dir)}

        output = _run_step40_fence(worktree, "large", env=env)
        _assert_park(output, "unreadable")
        assert "jq is not installed" in output

    def test_unreadable_head_parks_unreadable(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt-step40-unborn"
        worktree.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(worktree)],
            capture_output=True,
            check=True,
            env=_clean_git_env(),
        )
        _approve_at(worktree, _ABSENT_SHA)

        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "unreadable")
        assert "HEAD is not readable" in output

    @pytest.mark.parametrize(
        "snapshot",
        [{}, {"origin_main_branch": ""}, {"origin_main_branch": "bad..ref"}],
        ids=["absent", "empty", "invalid-ref"],
    )
    def test_bad_default_branch_parks_unreadable(
        self, approval_repo: tuple[Path, Path], snapshot: dict[str, str]
    ) -> None:
        worktree, _ = approval_repo
        task = _approval_task(_head(worktree))
        _write_context(
            worktree, _context(worktree, task, world_state_snapshot=snapshot)
        )
        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "unreadable")
        assert "origin_main_branch is missing or invalid" in output

    def test_unresolvable_default_branch_parks_unreadable(
        self, approval_repo: tuple[Path, Path]
    ) -> None:
        worktree, _ = approval_repo
        task = _approval_task(_head(worktree))
        snapshot = {"origin_main_branch": "develop"}
        _write_context(
            worktree, _context(worktree, task, world_state_snapshot=snapshot)
        )
        output = _run_step40_fence(worktree, "large")
        _assert_park(output, "unreadable")
        assert "origin/develop is not resolvable" in output


class TestStep40DocPins:
    """Prose pins on ``auto-dev-finalize.md`` around the Step 4.0 fence."""

    def test_step40_sits_after_the_guard_and_before_step4a(self) -> None:
        content = _cmd(_FINALIZE_DOC)
        guard = content.index(_GUARD_HEADING)
        step40 = content.index(_STEP40_HEADING)
        assert guard < step40 < content.index(_STEP4A_HEADING)

    def test_guard_ship_bullets_route_headless_runs_through_step40(self) -> None:
        """R11, A13: both ship outcomes pass Step 4.0 on a headless run."""
        open_pr = _guard_bullet("PR exists and is OPEN")
        assert open_pr.index("Step 4.0") < open_pr.index("Step 4d")
        for bullet in (open_pr, _guard_bullet("No PR exists")):
            headless, _, interactive = bullet.partition("**Interactive:**")
            assert "**Headless:**" in headless
            assert "Step 4.0" in headless
            assert "Step 4.0 does not run" in interactive or (
                "skip Step 4.0" in interactive
            )

    def test_guard_terminal_bullets_ship_nothing(self) -> None:
        for marker in ("PR exists and is MERGED", "PR exists and is CLOSED"):
            bullet = _guard_bullet(marker)
            assert "Step 4.0 does not run: nothing ships." in bullet
            assert "**Headless:**" not in bullet

    def test_step40_is_headless_only(self) -> None:
        section = _step40_section()
        assert "An interactive run skips this step" in section
        assert '`--headless` in "$ARGUMENTS"' in section

    def test_step40_prose_states_the_exact_tier_rule(self) -> None:
        section = _step40_section()
        assert "unresolved_tier" in section
        assert "exactly the lowercase `small` or `large`" in section
        assert "Never normalize or guess a tier" in section

    def test_fence_branches_on_the_exact_tier(self) -> None:
        fence = _step40_fence()
        assert 'case "$SCOPE_TIER" in' in fence
        lines = fence.splitlines()
        assert any(line.startswith("  small) ") for line in lines)
        assert "  large)" in lines
        assert any(
            line.startswith("  *) ") and "unresolved_tier" in line for line in lines
        )

    def test_fence_names_fail_closed_ancestor_and_merge_parent_rules(self) -> None:
        section = _step40_section()
        fence = _step40_fence()
        assert "This check fails closed" in section
        assert "merge-base --is-ancestor" in fence
        assert '"refs/remotes/origin/$DEFAULT_BRANCH^{commit}"' in fence
        assert '"^$MAIN_SHA"' in fence
        assert "rev-list --merges" in fence

    def test_park_is_review_pending_approval_at_stage4a(self) -> None:
        template = _park_template()
        assert _NUMSTAT in template
        for absent in ('"files": 0', '"lines_actual": 0', '"reviewed_sha"'):
            assert absent not in template
        assert "agent_block" not in template
        assert "(never `agent_block`)" in _step40_section()
        park = json.loads(template.replace(_NUMSTAT, "1"))
        assert park["status"] == "review_pending_approval"
        assert park["stage_reached"] == "stage4a_merge_gate"
        assert park["scope"]["tier"] == "large"
        assert park["pr"] is None
        assert park["blocker"] is None
        assert park["next_actions"] == ["requeue_stage_review"]

    def test_park_hint_names_the_review_requeue_for_every_token(self) -> None:
        """A11, A16: one labelled release; environment faults are fixed first."""
        park = json.loads(_park_template().replace(_NUMSTAT, "1"))
        hint = park["friction_highlights"][0]
        assert _REVIEW_RELEASE in hint
        assert "first fix the tier source" in hint
        for fault in ("jq", "HEAD", "origin_main_branch", "origin/<default-branch>"):
            assert fault in hint
        assert "first fix that environment fault" in hint
        section = _step40_section()
        assert "--stage finalize" not in section
        assert "to record approval for the current head" not in section

    def test_step4a_headless_line_admits_a_recorded_large_ticket(self) -> None:
        content = _cmd(_FINALIZE_DOC)
        assert (
            "**Headless:** Step 4a runs for small tickets and for large tickets"
            " whose Step 4.0 check printed REVIEW_APPROVAL_RECORDED." in content
        )
        assert "large already exited at S3" not in content

    def test_step40_is_rechecked_after_branch_mutations_before_automerge(self) -> None:
        content = _cmd(_FINALIZE_DOC)
        step40 = content.index(_STEP40_HEADING)
        recheck = content.index("Headless re-check before auto-merge", step40)
        arm = content.index("3. **Enable auto-merge:**", recheck)
        assert "after `/prep-pr` returns" in content[recheck:arm]
        assert "Step 4c.5" in content[recheck:arm]
        assert "review_pending_approval" in content[recheck:arm]

    def test_completion_template_lists_the_step40_status(self) -> None:
        content = _cmd(_FINALIZE_DOC)
        completion = content[content.index("## Stage 4+5 Completion (headless only)") :]
        assert (
            '"status": "<shipped | merge_gate_blocked | blocked'
            ' | review_pending_approval>"' in completion
        )
        assert "emitted only by Step 4.0" in completion

    def test_resolve_scope_tier_feeds_step40_verbatim(self) -> None:
        content = _cmd(_FINALIZE_DOC)
        start = content.index("**Resolve `scope.tier`:**")
        section = content[start : content.index("## Stage 4: PR Creation", start)]
        assert "Step 4.0" in section
        assert "exact lowercase" in section
        assert "unresolved_tier" in section

    def test_auto_dev_stage_reached_names_the_step40_park(self) -> None:
        """Round 5: the canonical ``stage_reached`` bullet agrees with the park."""
        content = _cmd("auto-dev.md")
        start = content.index("**`stage_reached`** — canonical values")
        bullet = next(
            line
            for line in content[start:].splitlines()
            if line.startswith("- `review_pending_approval` →")
        )
        assert '"stage3_review"' in bullet
        assert "stage4a_merge_gate" in bullet
        assert "Step 4.0" in bullet


# ---------------------------------------------------------------------------
# #2767 — end to end: codex review park, approve, finalize Step 4.0
# ---------------------------------------------------------------------------


def _write_plan_marker(worktree: Path, marker: str) -> None:
    plan = worktree / ".cw" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"# Plan\n\n{marker}\n", encoding="utf-8")


def _codex_review_sentinel(task: TicketTask, worktree: Path) -> dict[str, object]:
    """Run the real codex synthesis on a clean verdict at the worktree's HEAD."""
    assert task.session_id is not None
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=[_make_reviewer_doc()],
        failures=[],
        diff=_make_diff(),
        reviewed_sha=_head(worktree),
        session_id=task.session_id,
        default_branch="main",
        fix_loop_enabled=False,
    )
    assert verdict is not None
    return result.model_dump(mode="json")


def _ticket_row(
    worktree: Path, client: str, stage: Stage, **overrides: object
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=_PARK_TICKET,
        client=client,
        status=QueueItemStatus.RUNNING,
        stage=stage,
        worktree_path=worktree,
        **overrides,
    )


def _persist(task: TicketTask, worktree: Path, last_result: dict[str, object]) -> None:
    """Save *task* and its bound session, as dispatch leaves a parked row."""
    assert task.session_id is not None
    save_dev_queue(DevQueueStore(tasks=[task]))
    session = _make_session(
        task.session_id, task.ticket_id, task.client, worktree, last_result
    )
    save_state(CwState(sessions=[session]))


def _queued_row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _PARK_TICKET)


def _assert_no_review_approval(task: TicketTask) -> None:
    assert task.review_approved_at is None
    assert task.review_approver_kind is None
    assert task.review_approved_sha is None


def _park_sentinel(worktree: Path, client: str) -> dict[str, object]:
    """Fill the doc's park template the way the finalize worker would."""
    head = _head(worktree)
    text = substitute_fence_placeholders(
        _park_template().replace(_NUMSTAT, "1"),
        {
            "ticket-id": _PARK_TICKET,
            "client": client,
            "resolved plan_source": "github_issue_existing",
            "branch-name": "feature",
            "session worktree path": str(worktree),
            "fork point sha": git_in(worktree, "merge-base", "HEAD", "origin/main"),
            "sha1": head,
        },
    )
    payload: dict[str, object] = json.loads(text)
    return payload


class TestCodexLargeReviewApprovalE2E:
    """R4: a large codex review parks at REVIEW, ``approve`` records the
    approval, and finalize's Step 4.0 (the only executable finalize gate)
    lets the ticket ship. The recipe is never run here, by construction."""

    def test_large_codex_review_parks_approves_and_finalize_records(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        write_clients_yaml(client_with_pipeline)
        clients = load_effective_clients()
        client = client_with_pipeline.name
        worktree, _ = make_git_repo_with_origin("wt-e2e-large")
        _write_plan_marker(worktree, _LARGE_MARKER)
        task = _ticket_row(worktree, client, Stage.REVIEW, session_id="sess-large")

        sentinel = _codex_review_sentinel(task, worktree)
        assert sentinel["status"] == "review_pending_approval"
        assert apply_staged_decision(task, "review_pending_approval", sentinel, clients)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.REVIEW
        assert task.disposition == "review_pending_approval"

        _persist(task, worktree, sentinel)
        result = approve_ticket(_PARK_TICKET, client)

        head = _head(worktree)
        assert result["review_approved_sha"] == head
        row = _queued_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.stage == Stage.FINALIZE
        assert row.review_approver_kind == "operator"
        assert row.review_approved_sha == head

        _write_context(worktree, _context(worktree, row))
        assert _run_step40_fence(worktree, "large") == _RECORDED

    def test_small_codex_review_advances_without_park(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        write_clients_yaml(client_with_pipeline)
        clients = load_effective_clients()
        worktree = make_git_repo("wt-e2e-small")
        _write_plan_marker(worktree, "**Scope tier:** small")
        task = _ticket_row(
            worktree, client_with_pipeline.name, Stage.REVIEW, session_id="sess-sm"
        )

        sentinel = _codex_review_sentinel(task, worktree)
        assert sentinel["status"] == "stage_complete"
        assert apply_staged_decision(task, "stage_complete", sentinel, clients)

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.FINALIZE
        _assert_no_review_approval(task)


class TestFinalizeStep40ParkRelease:
    """R18, R20, A10: a large row at FINALIZE with no approval parks at Step
    4.0, ``approve`` stays refused, and the operator's review requeue is the
    release that records a fresh approval."""

    @pytest.mark.parametrize(
        "seeded_regress", [0, FINALIZE_REGRESS_CAP - 1], ids=["fresh", "one-prior"]
    )
    def test_unapproved_large_parks_and_releases_through_review(
        self,
        tmp_stage_dirs: Path,
        client_with_pipeline: ClientConfig,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        seeded_regress: int,
    ) -> None:
        write_clients_yaml(client_with_pipeline)
        clients = load_effective_clients()
        client = client_with_pipeline.name
        worktree, _ = make_git_repo_with_origin("wt-e2e-park")
        _write_plan_marker(worktree, _LARGE_MARKER)
        task = _ticket_row(
            worktree,
            client,
            Stage.FINALIZE,
            session_id="sess-fin",
            regress_attempts=seeded_regress,
            gate_block=GateBlockBackoff(
                count=2, fingerprint="x", until=datetime.now(UTC) + timedelta(hours=1)
            ),
            next_eligible_at=None,
        )

        # Finalize with no approval recorded: Step 4.0 parks.
        _write_context(worktree, _context(worktree, task))
        _assert_park(_run_step40_fence(worktree, "large"), "missing")
        park = _park_sentinel(worktree, client)
        AutoDevResult.model_validate(park)
        assert park["next_actions"] == ["requeue_stage_review"]
        review = park["review"]
        assert isinstance(review, dict)
        assert "reviewed_sha" not in review

        assert apply_staged_decision(task, "review_pending_approval", park, clients)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.FINALIZE
        assert task.disposition == "review_pending_approval"
        assert park["blocker"] is None
        # #2717 (R20): Rule 1 cleared the gate-block run and stamped no wait.
        assert task.gate_block is None
        assert task.next_eligible_at is None

        # approve stays refused at the terminal stage and names the release.
        _persist(task, worktree, park)
        with pytest.raises(ApproveGateError, match="terminal stage") as refused:
            approve_ticket(_PARK_TICKET, client)
        assert f"requeue {_PARK_TICKET} -c {client} --stage review" in str(
            refused.value
        )
        parked = _queued_row()
        assert parked.status == QueueItemStatus.BLOCKED_ON_USER
        assert parked.stage == Stage.FINALIZE
        _assert_no_review_approval(parked)

        # The operator's labelled release: a regress back to REVIEW.
        requeue_ticket(
            _PARK_TICKET,
            client,
            "review",
            infer_regress=True,
            native_daemon=FakeNativeDaemonClient(),
        )
        released = _queued_row()
        assert released.status == QueueItemStatus.PENDING
        assert released.stage == Stage.REVIEW
        assert released.regress_attempts == seeded_regress + 1
        _assert_no_review_approval(released)

        # Review re-runs on the current head, parks large, approve records it.
        rerun = released.model_copy(
            update={"status": QueueItemStatus.RUNNING, "session_id": "sess-rv2"}
        )
        sentinel = _codex_review_sentinel(rerun, worktree)
        assert apply_staged_decision(
            rerun, "review_pending_approval", sentinel, clients
        )
        assert rerun.status == QueueItemStatus.BLOCKED_ON_USER
        _persist(rerun, worktree, sentinel)
        approve_ticket(_PARK_TICKET, client)

        approved = _queued_row()
        assert approved.stage == Stage.FINALIZE
        assert approved.review_approved_sha == _head(worktree)
        # A10: approve does not reset the regress count, so the second release
        # on one ticket leaves Rule 5a's FINALIZE self-heal exhausted.
        assert approved.regress_attempts == seeded_regress + 1
        assert (approved.regress_attempts < FINALIZE_REGRESS_CAP) is (
            seeded_regress + 1 < FINALIZE_REGRESS_CAP
        )

        _write_context(worktree, _context(worktree, approved))
        assert _run_step40_fence(worktree, "large") == _RECORDED
        commit_tracked_file(worktree, "fix.py")
        _assert_park(_run_step40_fence(worktree, "large"), "stale")
