"""Tests for cw.reconcile.gate_recipes (RFC 0009 P1+P2, GitHub #1065)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cw.config import save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile.gate_recipes import (
    RECIPE_AUTO_ADOPT_PLAN,
    RECIPE_AUTO_APPROVE_REVIEW,
    GateRecipeCandidate,
    _act_auto_approve_review,
    _detect_auto_approve_review,
    run_gate_recipes,
)

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _write_acme_clients_yaml(tmp_config_dir: Path, workspace: Path) -> None:
    """Write a minimal clients.yaml for 'acme' pointing at *workspace*."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {workspace}\n"
        "    default_branch: main\n"
    )


def _clean_result(
    *,
    must_fix_initial: int = 0,
    deferred: int = 0,
    recommendation: str = "PROCEED",
    forbidden_touched: bool = False,
    status: str = "review_pending_approval",
) -> dict[str, Any]:
    """Build a last_result dict matching a clean-review sentinel snapshot."""
    return {
        "status": status,
        "review": {
            "must_fix_initial": must_fix_initial,
            "should_fix": 0,
            "fix_cycles_used": 0,
            "deferred": deferred,
        },
        "health": {
            "recommendation": recommendation,
            "any_incomplete_risk": False,
        },
        "scope": {
            "files": 1,
            "lines_estimate": 10,
            "forbidden_touched": forbidden_touched,
        },
    }


def _make_task(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    stage: Stage = Stage.REVIEW,
    session_id: str | None = "sess-1",
    **kwargs: Any,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=status,
        stage=stage,
        session_id=session_id,
        **kwargs,
    )


def _make_session(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    session_id: str = "sess-1",
    last_result: dict[str, Any] | None = None,
) -> Session:
    return Session(
        id=session_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        last_result=last_result,
    )


def _config(**kwargs: Any) -> OrchestratorConfig:
    kwargs.setdefault("gate_recipes_enabled", True)
    return OrchestratorConfig(**kwargs)


@pytest.fixture(autouse=True)
def stub_gh_comment(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture ``gh issue comment`` argv without spawning a subprocess.

    Returns the list of captured argv lists so tests can assert on the body.
    """
    import subprocess

    calls: list[list[str]] = []

    def _fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _fake_run)
    return calls


class TestDetect:
    def test_detects_clean_review_pending_row(self) -> None:
        task = _make_task(lane="fastlane")
        session = _make_session(last_result=_clean_result())
        state = CwState(sessions=[session])

        candidates = _detect_auto_approve_review(state, [task])

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.ticket_id == "GEN-1"
        assert cand.client == "acme"
        assert cand.lane == "fastlane"
        assert cand.recipe == RECIPE_AUTO_APPROVE_REVIEW
        assert cand.session_id == "sess-1"
        assert cand.evidence == {
            "must_fix_initial": 0,
            "deferred": 0,
            "recommendation": "PROCEED",
            "forbidden_touched": False,
        }

    def test_non_blocked_status_yields_none(self) -> None:
        task = _make_task(status=QueueItemStatus.PENDING)
        session = _make_session(last_result=_clean_result())
        state = CwState(sessions=[session])

        assert _detect_auto_approve_review(state, [task]) == []

    def test_wrong_last_result_status_yields_none(self) -> None:
        task = _make_task()
        session = _make_session(
            last_result=_clean_result(status="plan_pending_approval")
        )
        state = CwState(sessions=[session])

        assert _detect_auto_approve_review(state, [task]) == []

    def test_no_session_id_yields_none(self) -> None:
        task = _make_task(session_id=None)
        state = CwState(sessions=[])

        assert _detect_auto_approve_review(state, [task]) == []

    def test_missing_session_yields_none(self) -> None:
        task = _make_task(session_id="ghost")
        state = CwState(sessions=[])

        assert _detect_auto_approve_review(state, [task]) == []

    def test_null_last_result_yields_none(self) -> None:
        task = _make_task()
        session = _make_session(last_result=None)
        state = CwState(sessions=[session])

        assert _detect_auto_approve_review(state, [task]) == []

    def test_malformed_last_result_section_yields_none(self) -> None:
        """A review/health/scope section that is not a dict is not fireable."""
        task = _make_task()
        bad = _clean_result()
        bad["review"] = "not-a-dict"
        session = _make_session(last_result=bad)
        state = CwState(sessions=[session])

        assert _detect_auto_approve_review(state, [task]) == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"must_fix_initial": 1},
            {"deferred": 1},
            {"recommendation": "EXIT_FOR_HUMAN_REVIEW"},
            {"forbidden_touched": True},
        ],
    )
    def test_predicate_boundary_each_field_blocks(self, kwargs: dict[str, Any]) -> None:
        task = _make_task()
        session = _make_session(last_result=_clean_result(**kwargs))
        state = CwState(sessions=[session])

        assert _detect_auto_approve_review(state, [task]) == []


class TestMasterSwitch:
    def test_disabled_is_full_noop(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        recovered = run_gate_recipes(
            now=_NOW, config=_config(gate_recipes_enabled=False)
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW


class TestRunApprove:
    def test_approves_clean_review_like_human(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The recipe advances the ticket exactly as a human approve would:
        REVIEW/BLOCKED_ON_USER -> FINALIZE/PENDING, session_id cleared."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        approved = store.tasks[0]
        assert approved.status == QueueItemStatus.PENDING
        assert approved.stage == Stage.FINALIZE
        assert approved.session_id is None

    def test_event_emitted_before_transition_with_reloaded_sources(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.events import read_events

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(lane="fastlane")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        run_gate_recipes(now=_NOW, config=_config())

        events = read_events(
            consumer="test-gate-approve-event",
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["ticket_id"] == "GEN-1"
        assert payload["client"] == "acme"
        assert payload["lane"] == "fastlane"
        assert payload["session_id"] == "sess-1"
        assert payload["recipe"] == RECIPE_AUTO_APPROVE_REVIEW
        assert payload["predicate_snapshot"] == {
            "must_fix_initial": 0,
            "deferred": 0,
            "recommendation": "PROCEED",
            "forbidden_touched": False,
        }
        assert events[0].correlation_id == "GEN-1"

    def test_comment_written_on_success(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        stub_gh_comment: list[list[str]],
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        run_gate_recipes(now=_NOW, config=_config())

        assert len(stub_gh_comment) == 1
        argv = stub_gh_comment[0]
        assert argv[:4] == ["gh", "issue", "comment", "GEN-1"]
        body = argv[-1]
        assert "auto_approve_clean_review" in body
        assert "must_fix_initial: 0" in body
        assert "recommendation: PROCEED" in body
        assert "forbidden_touched: False" in body

    def test_comment_failure_swallowed_and_logged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        boom_msg = "gh exploded"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _boom)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        # Approve still stands despite the comment write failing.
        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.FINALIZE
        assert any("GEN-1" in rec.message for rec in caplog.records)


class TestActRecheckRace:
    def test_stale_candidate_predicate_fails_at_act(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A candidate produced at detect time but whose last_result no longer
        satisfies the predicate at act time is skipped: no event, no mutation."""
        from cw.events import read_events

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        # Persist a state where the predicate NO LONGER holds (must_fix_initial=1),
        # simulating a concurrent mutation between detect and act.
        save_state(
            CwState(
                sessions=[_make_session(last_result=_clean_result(must_fix_initial=1))]
            )
        )
        stale_candidate = GateRecipeCandidate(
            ticket_id="GEN-1",
            client="acme",
            lane="default",
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
            evidence={
                "must_fix_initial": 0,
                "deferred": 0,
                "recommendation": "PROCEED",
                "forbidden_touched": False,
            },
            session_id="sess-1",
        )

        recovered = _act_auto_approve_review([stale_candidate], now=_NOW)

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW
        events = read_events(
            consumer="test-gate-race",
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert events == []

    def test_empty_candidates_is_noop(self) -> None:
        assert _act_auto_approve_review([], now=_NOW) == []


def test_recipe_constants_are_distinct() -> None:
    """Both recipe keys are defined (P3 wires the second one, #1066)."""
    assert RECIPE_AUTO_APPROVE_REVIEW == "auto_approve_clean_review"
    assert RECIPE_AUTO_ADOPT_PLAN == "auto_adopt_clean_plan"
    assert RECIPE_AUTO_APPROVE_REVIEW != RECIPE_AUTO_ADOPT_PLAN
