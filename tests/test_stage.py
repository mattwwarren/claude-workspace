"""Tests for the stage-advance decision table in _apply_events_to_store.

RFC 0005 B2 — drive the 6-rule decision table via direct calls to
_apply_events_to_store rather than going through consume_completed_sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import save_dev_queue
from cw.dispatch import _apply_events_to_store
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    ticket_id: str,
    session_id: str,
    *,
    crashed: bool = False,
) -> object:
    from cw.models import OrchestratorEvent

    payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "session_id": session_id,
    }
    if crashed:
        payload["crashed"] = True
    return OrchestratorEvent(
        type=OrchestratorEventType.SESSION_COMPLETED,
        payload=payload,
    )


def _seed_session(
    session_id: str,
    *,
    last_result: dict[str, object] | None,
    tmp_config_dir: Path,
) -> None:
    """Write a session with the given last_result into state."""
    sess = Session(
        id=session_id,
        name=f"testclient/auto-dev/{session_id}",
        client="testclient",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=tmp_config_dir,
        origin=SessionOrigin.DAEMON,
        last_result=last_result,
    )
    state = load_state()
    state.sessions.append(sess)
    save_state(state)


def _make_task(
    ticket_id: str,
    *,
    session_id: str,
    stage: Stage = Stage.PLAN,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client="testclient",
        status=status,
        session_id=session_id,
        stage=stage,
    )


def _make_clients(tmp_config_dir: Path) -> dict[str, ClientConfig]:
    return {
        "testclient": ClientConfig(
            name="testclient",
            workspace_path=tmp_config_dir,
        )
    }


# ---------------------------------------------------------------------------
# Rule 1: pure pause statuses → BLOCKED_ON_USER
# ---------------------------------------------------------------------------


class TestRule1PurePageStatuses:
    def test_ambiguities_pending_resolution(self, tmp_config_dir: Path) -> None:
        """status=ambiguities_pending_resolution → BLOCKED_ON_USER (stage stays)."""
        _seed_session(
            "sess-r1a",
            last_result={"status": "ambiguities_pending_resolution"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-1", session_id="sess-r1a", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-1", "sess-r1a")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.PLAN  # stage unchanged

    def test_premises_pending_verification(self, tmp_config_dir: Path) -> None:
        """status=premises_pending_verification → BLOCKED_ON_USER."""
        _seed_session(
            "sess-r1b",
            last_result={"status": "premises_pending_verification"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-2", session_id="sess-r1b", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-2", "sess-r1b")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# Rule 2: scope-gated approval
# ---------------------------------------------------------------------------


class TestRule2ScopeGated:
    def test_plan_pending_small(self, tmp_config_dir: Path) -> None:
        """plan_pending_approval + scope.tier=small → PENDING, stage advances."""
        _seed_session(
            "sess-r2a",
            last_result={
                "status": "plan_pending_approval",
                "scope": {"tier": "small"},
            },
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-3", session_id="sess-r2a", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-3", "sess-r2a")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.PENDING
        assert store.tasks[0].stage == Stage.IMPL  # advanced from PLAN
        assert store.tasks[0].session_id is None

    def test_plan_pending_large(self, tmp_config_dir: Path) -> None:
        """plan_pending_approval + scope.tier=large → BLOCKED_ON_USER."""
        _seed_session(
            "sess-r2b",
            last_result={
                "status": "plan_pending_approval",
                "scope": {"tier": "large"},
            },
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-4", session_id="sess-r2b", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-4", "sess-r2b")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_plan_pending_missing_scope_defaults_large(
        self, tmp_config_dir: Path
    ) -> None:
        """plan_pending_approval with no scope key → BLOCKED_ON_USER (default large)."""
        _seed_session(
            "sess-r2c",
            last_result={"status": "plan_pending_approval"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-5", session_id="sess-r2c", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-5", "sess-r2c")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_review_pending_small(self, tmp_config_dir: Path) -> None:
        """review_pending_approval + scope.tier=small → PENDING, stage advances."""
        _seed_session(
            "sess-r2d",
            last_result={
                "status": "review_pending_approval",
                "scope": {"tier": "small"},
            },
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-6", session_id="sess-r2d", stage=Stage.REVIEW)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-6", "sess-r2d")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.PENDING
        assert store.tasks[0].stage == Stage.FINALIZE


# ---------------------------------------------------------------------------
# Rule 3: shipped → stage success
# ---------------------------------------------------------------------------


class TestRule3Shipped:
    def test_shipped_non_terminal_stage(self, tmp_config_dir: Path) -> None:
        """shipped at PLAN stage → PENDING, stage=IMPL."""
        _seed_session(
            "sess-r3a",
            last_result={"status": "shipped"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-7", session_id="sess-r3a", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-7", "sess-r3a")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.PENDING
        assert store.tasks[0].stage == Stage.IMPL

    def test_shipped_terminal_stage(self, tmp_config_dir: Path) -> None:
        """shipped at FINALIZE (last stage) → COMPLETED."""
        _seed_session(
            "sess-r3b",
            last_result={"status": "shipped"},
            tmp_config_dir=tmp_config_dir,
        )
        # Default pipeline: [PLAN, IMPL, REVIEW, FINALIZE]
        task = _make_task("T-8", session_id="sess-r3b", stage=Stage.FINALIZE)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-8", "sess-r3b")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.COMPLETED


# ---------------------------------------------------------------------------
# Rule 4: no_op → always terminal
# ---------------------------------------------------------------------------


class TestRule4NoOp:
    def test_no_op_is_completed(self, tmp_config_dir: Path) -> None:
        """no_op → COMPLETED regardless of stage."""
        _seed_session(
            "sess-r4",
            last_result={"status": "no_op"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-9", session_id="sess-r4", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-9", "sess-r4")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.COMPLETED


# ---------------------------------------------------------------------------
# Rule 5: failure statuses → BLOCKED_ON_USER
# ---------------------------------------------------------------------------


class TestRule5FailureStatuses:
    @pytest.mark.parametrize(
        "fail_status",
        ["blocked", "merge_gate_blocked", "scope_exceeded", "forbidden_area"],
    )
    def test_failure_status_routes_blocked(
        self, fail_status: str, tmp_config_dir: Path
    ) -> None:
        """Failure statuses → BLOCKED_ON_USER."""
        sid = f"sess-r5-{fail_status}"
        _seed_session(
            sid,
            last_result={"status": fail_status},
            tmp_config_dir=tmp_config_dir,
        )
        tid = f"T-fail-{fail_status}"
        task = _make_task(tid, session_id=sid, stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event(tid, sid)],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# Rule 6: unparseable / None last_result → BLOCKED_ON_USER
# ---------------------------------------------------------------------------


class TestRule6Unparseable:
    def test_none_last_result(self, tmp_config_dir: Path) -> None:
        """last_result=None → BLOCKED_ON_USER."""
        _seed_session(
            "sess-r6a",
            last_result=None,
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-10", session_id="sess-r6a", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-10", "sess-r6a")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_unparseable_no_status_key(self, tmp_config_dir: Path) -> None:
        """last_result dict with no 'status' key → BLOCKED_ON_USER."""
        _seed_session(
            "sess-r6b",
            last_result={"other_key": "value"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-11", session_id="sess-r6b", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)

        result = _apply_events_to_store(
            store,
            [_make_event("T-11", "sess-r6b")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_idempotency_second_event_no_change(self, tmp_config_dir: Path) -> None:
        """Same SESSION_COMPLETED event processed twice has no effect second time."""
        _seed_session(
            "sess-idem",
            last_result={"status": "shipped"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-idem", session_id="sess-idem", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = _make_clients(tmp_config_dir)
        event = _make_event("T-idem", "sess-idem")

        # First pass
        _apply_events_to_store(store, [event], clients)  # type: ignore[list-item]
        assert store.tasks[0].status == QueueItemStatus.PENDING

        # Second pass — task is PENDING (not RUNNING), so event is skipped
        result2 = _apply_events_to_store(store, [event], clients)  # type: ignore[list-item]
        assert result2 == 0
        assert store.tasks[0].status == QueueItemStatus.PENDING

    def test_missing_client_routes_blocked(self, tmp_config_dir: Path) -> None:
        """Unknown client in task.client → BLOCKED_ON_USER, no raise."""
        _seed_session(
            "sess-nocl",
            last_result={"status": "shipped"},
            tmp_config_dir=tmp_config_dir,
        )
        task = _make_task("T-nocl", session_id="sess-nocl", stage=Stage.PLAN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients: dict[str, ClientConfig] = {}  # empty — "testclient" not present

        result = _apply_events_to_store(
            store,
            [_make_event("T-nocl", "sess-nocl")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_stage_not_in_pipeline_routes_blocked(self, tmp_config_dir: Path) -> None:
        """Task stage not in client.pipeline.stages → BLOCKED_ON_USER."""
        from cw.models import StagePipelineConfig

        _seed_session(
            "sess-nopipe",
            last_result={"status": "shipped"},
            tmp_config_dir=tmp_config_dir,
        )
        # HARDEN is not in a [PLAN, IMPL] pipeline
        task = _make_task("T-nopipe", session_id="sess-nopipe", stage=Stage.HARDEN)
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)
        clients = {
            "testclient": ClientConfig(
                name="testclient",
                workspace_path=tmp_config_dir,
                pipeline=StagePipelineConfig(stages=[Stage.PLAN, Stage.IMPL]),
            )
        }

        result = _apply_events_to_store(
            store,
            [_make_event("T-nopipe", "sess-nopipe")],
            clients,  # type: ignore[list-item]
        )

        assert result == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
