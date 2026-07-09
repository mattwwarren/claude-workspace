"""Tests for cw.reconcile.gate_recipes (RFC 0009 P1+P2, GitHub #1065)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cw.config import save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.gh import _PLAN_MARKER
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
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
    _act_auto_adopt_plan,
    _act_auto_approve_review,
    _detect_auto_adopt_plan,
    _detect_auto_approve_review,
    _find_blocked_task,
    _marker_version,
    _stamp_gate_recipe_failure,
    resolve_gate_recipe_enabled,
    run_gate_recipes,
)

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


# YAML lanes block enabling both gate recipes on both the 'default' and
# 'fastlane' lanes — appended to each test client so run_gate_recipes resolves
# the recipes enabled under the 3-tier per-lane precedence (RFC 0009 P4).
_LANES_YAML = (
    "    lanes:\n"
    "      - name: default\n"
    "        gate_recipes:\n"
    "          auto_approve_clean_review: true\n"
    "          auto_adopt_clean_plan: true\n"
    "      - name: fastlane\n"
    "        gate_recipes:\n"
    "          auto_approve_clean_review: true\n"
    "          auto_adopt_clean_plan: true\n"
)


def _write_acme_clients_yaml(tmp_config_dir: Path, workspace: Path) -> None:
    """Write a minimal clients.yaml for 'acme' pointing at *workspace*."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {workspace}\n"
        "    default_branch: main\n" + _LANES_YAML
    )


def _write_two_client_yaml(
    tmp_config_dir: Path, acme_workspace: Path, beta_workspace: Path
) -> None:
    """Write a minimal clients.yaml for both 'acme' and 'beta'."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        f"  acme:\n    workspace_path: {acme_workspace}\n    default_branch: main\n"
        + _LANES_YAML
        + f"  beta:\n    workspace_path: {beta_workspace}\n    default_branch: main\n"
        + _LANES_YAML
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


def _seam1_clients() -> dict[str, ClientConfig]:
    """Seam-1 per-lane clients dict for the direct ``_detect_*`` call sites.

    Client ``acme`` declares BOTH the ``default`` and ``fastlane`` lanes, each
    enabling both gate recipes, so that a task on either lane resolves enabled
    under the 3-tier ``resolve_gate_recipe_enabled`` precedence (several detect
    tests run tasks on ``fastlane``).
    """
    both_on = {RECIPE_AUTO_APPROVE_REVIEW: True, RECIPE_AUTO_ADOPT_PLAN: True}
    return {
        "acme": ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/ws"),
            default_branch="main",
            lanes=[
                LaneConfig(name="default", gate_recipes=dict(both_on)),
                LaneConfig(name="fastlane", gate_recipes=dict(both_on)),
            ],
        )
    }


_SEAM1_CLIENTS = _seam1_clients()


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

        candidates = _detect_auto_approve_review(
            state, [task], clients=_SEAM1_CLIENTS, config=_config()
        )

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

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_latched_failure_yields_none_even_when_predicate_holds(self) -> None:
        """A row with a non-None gate_recipe_failed_at is excluded from
        detection regardless of how clean the current predicate looks —
        the whole point of the latch is to suppress re-detection of a
        persisting failure until something about the episode changes."""
        task = _make_task(gate_recipe_failed_at=_NOW)
        session = _make_session(last_result=_clean_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_wrong_last_result_status_yields_none(self) -> None:
        task = _make_task()
        session = _make_session(
            last_result=_clean_result(status="plan_pending_approval")
        )
        state = CwState(sessions=[session])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_no_session_id_yields_none(self) -> None:
        task = _make_task(session_id=None)
        state = CwState(sessions=[])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_missing_session_yields_none(self) -> None:
        task = _make_task(session_id="ghost")
        state = CwState(sessions=[])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_null_last_result_yields_none(self) -> None:
        task = _make_task()
        session = _make_session(last_result=None)
        state = CwState(sessions=[session])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    @pytest.mark.parametrize("section", ["review", "health", "scope"])
    def test_malformed_last_result_section_yields_none(self, section: str) -> None:
        """A review/health/scope section that is not a dict is not fireable,
        independent of which of the three sections is malformed."""
        task = _make_task()
        bad = _clean_result()
        bad[section] = "not-a-dict"
        session = _make_session(last_result=bad)
        state = CwState(sessions=[session])

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

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

        assert (
            _detect_auto_approve_review(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )


class TestMasterSwitch:
    def test_disabled_is_full_noop(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        from cw.events import read_events

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
        events = read_events(
            consumer="test-gate-disabled-noop",
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert events == []


class TestPerLaneYamlDisablement:
    """Per-lane disablement resolved through the real load_effective_clients()
    -> YAML-parse path, not just hand-built ClientConfig objects.

    TestMasterSwitchVsLane covers the same tier-2 behavior by calling
    _detect_* directly against an in-memory clients dict, which never
    exercises LaneConfig(gate_recipes=...) deserialization from YAML. These
    tests close that gap by driving the real run_gate_recipes() entry point."""

    def test_run_gate_recipes_skips_lane_with_recipe_disabled_in_yaml(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  acme:\n    workspace_path: {tmp_path}\n"
            "    default_branch: main\n"
            "    lanes:\n"
            "      - name: default\n"
            "        gate_recipes:\n"
            "          auto_approve_clean_review: false\n"
            "          auto_adopt_clean_plan: false\n"
        )
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_run_gate_recipes_fires_only_yaml_enabled_lane(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  acme:\n    workspace_path: {tmp_path}\n"
            "    default_branch: main\n"
            "    lanes:\n"
            "      - name: default\n"
            "        gate_recipes:\n"
            "          auto_approve_clean_review: false\n"
            "      - name: fastlane\n"
            "        gate_recipes:\n"
            "          auto_approve_clean_review: true\n"
        )
        task_off = _make_task(ticket_id="GEN-A", lane="default", session_id="sess-a")
        task_on = _make_task(ticket_id="GEN-B", lane="fastlane", session_id="sess-b")
        save_dev_queue(DevQueueStore(tasks=[task_off, task_on]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        ticket_id="GEN-A",
                        session_id="sess-a",
                        last_result=_clean_result(),
                    ),
                    _make_session(
                        ticket_id="GEN-B",
                        session_id="sess-b",
                        last_result=_clean_result(),
                    ),
                ]
            )
        )

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-B"]


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

    def test_approves_both_when_two_clients_share_a_ticket_id(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """ticket_id is a per-repo GitHub issue number, not globally unique —
        two different clients can legitimately have a clean candidate that
        shares the same ticket_id. Both must fire independently; neither
        should silently collide/drop the other (regression test for keying
        the act-phase dedup on (ticket_id, client), not ticket_id alone)."""
        acme_ws = tmp_path / "acme"
        beta_ws = tmp_path / "beta"
        acme_ws.mkdir()
        beta_ws.mkdir()
        _write_two_client_yaml(tmp_config_dir, acme_ws, beta_ws)
        acme_task = _make_task(ticket_id="GEN-1", client="acme", session_id="sess-a")
        beta_task = _make_task(ticket_id="GEN-1", client="beta", session_id="sess-b")
        save_dev_queue(DevQueueStore(tasks=[acme_task, beta_task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        ticket_id="GEN-1",
                        client="acme",
                        session_id="sess-a",
                        last_result=_clean_result(),
                    ),
                    _make_session(
                        ticket_id="GEN-1",
                        client="beta",
                        session_id="sess-b",
                        last_result=_clean_result(),
                    ),
                ]
            )
        )

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert sorted(recovered) == ["GEN-1", "GEN-1"]
        store = load_dev_queue()
        by_client = {t.client: t for t in store.tasks}
        assert by_client["acme"].stage == Stage.FINALIZE
        assert by_client["beta"].stage == Stage.FINALIZE

    def test_event_payload_sources_from_reloaded_state(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Checks the emitted event's payload content and that exactly one
        fires. For the event-survives-a-failed-mutation ordering guarantee
        itself, see TestActApproveFailure — this test alone doesn't prove
        emit-before-mutation ordering since both already succeeded here."""
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


class TestActApproveFailure:
    def test_event_survives_a_failed_mutation_and_no_comment_is_posted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stub_gh_comment: list[list[str]],
    ) -> None:
        """Proves the event-before-mutation ordering actually matters: even
        when the mutation itself raises, the already-recorded GATE_AUTO_APPROVED
        event is not rolled back (durable audit trail) — but the ticket is NOT
        approved, and no audit comment is posted for a mutation that never
        landed. This is the coverage a passing "both happened" assertion alone
        cannot provide. Also asserts GATE_AUTO_APPROVE_FAILED is emitted as a
        durable correction, so GATE_AUTO_APPROVED never stands alone on the
        operator channel as an uncorrected false-positive "approved" signal."""
        from cw.events import read_events
        from cw.exceptions import ApproveGateError

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        boom_msg = "boom"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ApproveGateError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._approve_ticket_locked", _boom)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW
        assert store.tasks[0].gate_recipe_failed_at == _NOW
        events = read_events(
            consumer="test-gate-approve-failure",
            event_types=[
                OrchestratorEventType.GATE_AUTO_APPROVED,
                OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
            ],
        )
        approved_events = [
            e for e in events if e.type == OrchestratorEventType.GATE_AUTO_APPROVED
        ]
        failed_events = [
            e
            for e in events
            if e.type == OrchestratorEventType.GATE_AUTO_APPROVE_FAILED
        ]
        assert len(approved_events) == 1
        assert len(failed_events) == 1
        failed_payload = failed_events[0].payload
        assert failed_payload["ticket_id"] == "GEN-1"
        assert failed_payload["client"] == "acme"
        assert boom_msg in failed_payload["error"]
        assert failed_events[0].correlation_id == "GEN-1"
        assert approved_events[0].payload["ticket_id"] == "GEN-1"
        assert stub_gh_comment == []
        assert any("GEN-1" in rec.message for rec in caplog.records)

    def test_latched_failure_does_not_refire_on_the_next_tick(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression test for the repeat-forever bug: without the
        gate_recipe_failed_at latch, a persisting failure (task stays
        BLOCKED_ON_USER, predicate still holds, nothing about the episode
        changes) would re-detect and re-emit both GATE_AUTO_APPROVED and
        GATE_AUTO_APPROVE_FAILED on every single reconcile tick forever. The
        latch stamped by the first failing tick must suppress every
        subsequent tick until something about the episode actually changes."""
        from cw.events import read_events
        from cw.exceptions import ApproveGateError

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        boom_msg = "boom"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ApproveGateError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._approve_ticket_locked", _boom)

        first_tick = run_gate_recipes(now=_NOW, config=_config())
        second_tick = run_gate_recipes(now=_NOW, config=_config())

        assert first_tick == []
        assert second_tick == []
        events = read_events(
            consumer="test-gate-latch-no-refire",
            event_types=[
                OrchestratorEventType.GATE_AUTO_APPROVED,
                OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
            ],
        )
        # Exactly one of each — the second tick's detect phase excluded the
        # latched row entirely, so neither event fired a second time. Split
        # by type (not just a combined count) so a regression that fired 2x
        # one type and 0x the other would still be caught.
        approved = [
            e for e in events if e.type == OrchestratorEventType.GATE_AUTO_APPROVED
        ]
        failed = [
            e
            for e in events
            if e.type == OrchestratorEventType.GATE_AUTO_APPROVE_FAILED
        ]
        assert len(approved) == 1
        assert len(failed) == 1

    def test_latch_clears_on_next_status_transition(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Once a human (or any status-transition call site) acts on the
        ticket, transition_task_status unconditionally clears the latch — a
        fresh episode always starts clean, matching the escalation-latch
        precedent."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(gate_recipe_failed_at=_NOW)
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.dev_queue import cancel_ticket

        cancel_ticket("GEN-1", "acme")

        store = load_dev_queue()
        assert store.tasks[0].gate_recipe_failed_at is None

    def test_duplicate_blocked_rows_stamp_the_newest_not_the_stale_one(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression test for the duplicate-row resolution-
        consistency fix: two BLOCKED_ON_USER rows share (ticket_id, client)
        (a legitimate scenario per add_ticket's dedup guard, which only blocks
        re-insertion for PENDING/RUNNING/terminal-matching rows, not
        BLOCKED_ON_USER). Both the act loop's own lookup and the failure-path
        stamp helper must resolve to the SAME (newest) physical row, not
        silently diverge and latch the wrong one."""
        from cw.exceptions import ApproveGateError

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        older = _make_task(
            session_id="sess-old", created_at=datetime(2026, 7, 1, tzinfo=UTC)
        )
        newer = _make_task(
            session_id="sess-new", created_at=datetime(2026, 7, 8, tzinfo=UTC)
        )
        save_dev_queue(DevQueueStore(tasks=[older, newer]))
        save_state(
            CwState(
                sessions=[
                    _make_session(session_id="sess-old", last_result=_clean_result()),
                    _make_session(session_id="sess-new", last_result=_clean_result()),
                ]
            )
        )

        boom_msg = "boom"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ApproveGateError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._approve_ticket_locked", _boom)

        run_gate_recipes(now=_NOW, config=_config())

        store = load_dev_queue()
        by_session = {t.session_id: t for t in store.tasks}
        assert by_session["sess-new"].gate_recipe_failed_at == _NOW
        assert by_session["sess-old"].gate_recipe_failed_at is None

    def test_recipe_does_not_clear_a_newer_awaiting_signoff_duplicate(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """RFC 0009 / #1083 regression: when a resolved BLOCKED_ON_USER row A
        coexists with a strictly-newer AWAITING_OPERATOR_SIGNOFF duplicate B for
        the same (ticket_id, client), the recipe must approve the exact row A it
        validated — NOT let _approve_ticket_locked re-resolve to row B (which
        _find_ticket would pick, since _APPROVABLE_STATUSES pools both statuses
        newest-wins) and blindly clear a signoff gate the recipe never checked.
        Exercises the real _approve_ticket_locked (no monkeypatch)."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        row_a = _make_task(
            session_id="sess-old",
            stage=Stage.REVIEW,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        row_b = _make_task(
            session_id="sess-new",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            stage=Stage.REVIEW,
            created_at=datetime(2026, 7, 8, tzinfo=UTC),
        )
        save_dev_queue(DevQueueStore(tasks=[row_a, row_b]))
        save_state(
            CwState(
                sessions=[
                    _make_session(session_id="sess-old", last_result=_clean_result())
                ]
            )
        )

        approved = run_gate_recipes(now=_NOW, config=_config())

        store = load_dev_queue()
        # Key on created_at (stable identity): the approved row's session_id is
        # cleared to None by _advance_task_pointer, so session_id can't identify
        # both rows post-approve.
        by_created = {t.created_at: t for t in store.tasks}
        row_b = by_created[datetime(2026, 7, 8, tzinfo=UTC)]
        row_a = by_created[datetime(2026, 7, 1, tzinfo=UTC)]
        # Row B's signoff gate is untouched — NOT advanced/completed.
        assert row_b.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert row_b.stage == Stage.REVIEW
        assert row_b.session_id == "sess-new"
        # Row A is the one approved: review -> finalize PENDING.
        assert row_a.stage == Stage.FINALIZE
        assert row_a.status == QueueItemStatus.PENDING
        assert approved == ["GEN-1"]

    def test_mixed_outcome_batch_does_not_revert_the_successful_candidate(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for the stale-outer-snapshot clobber risk flagged
        against _stamp_gate_recipe_failure: when one candidate in a batch
        succeeds and a LATER candidate in the same _act_auto_approve_review
        call fails, the failure-path stamp (a fresh load/save round-trip)
        must not silently revert the earlier candidate's already-persisted
        approve by writing through the loop's stale pre-loop-hoisted
        snapshot."""
        from cw.exceptions import ApproveGateError

        acme_ws = tmp_path / "acme"
        beta_ws = tmp_path / "beta"
        acme_ws.mkdir()
        beta_ws.mkdir()
        _write_two_client_yaml(tmp_config_dir, acme_ws, beta_ws)
        acme_task = _make_task(ticket_id="GEN-1", client="acme", session_id="sess-a")
        beta_task = _make_task(ticket_id="GEN-1", client="beta", session_id="sess-b")
        save_dev_queue(DevQueueStore(tasks=[acme_task, beta_task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        ticket_id="GEN-1",
                        client="acme",
                        session_id="sess-a",
                        last_result=_clean_result(),
                    ),
                    _make_session(
                        ticket_id="GEN-1",
                        client="beta",
                        session_id="sess-b",
                        last_result=_clean_result(),
                    ),
                ]
            )
        )

        from cw.dev_queue import _approve_ticket_locked as real_approve_locked

        boom_msg = "boom"

        def _fail_beta_only(
            ticket_id: str, client: str, *, resolved_task: TicketTask | None = None
        ) -> dict[str, str | bool]:
            if client == "beta":
                raise ApproveGateError(boom_msg)
            return real_approve_locked(ticket_id, client, resolved_task=resolved_task)

        monkeypatch.setattr(
            "cw.reconcile.gate_recipes._approve_ticket_locked", _fail_beta_only
        )

        run_gate_recipes(now=_NOW, config=_config())

        store = load_dev_queue()
        by_client = {t.client: t for t in store.tasks}
        # acme's successful approve must survive beta's later failure/stamp.
        assert by_client["acme"].stage == Stage.FINALIZE
        assert by_client["acme"].status == QueueItemStatus.PENDING
        assert by_client["beta"].status == QueueItemStatus.BLOCKED_ON_USER
        assert by_client["beta"].gate_recipe_failed_at == _NOW

    def test_stamp_on_missing_row_is_a_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The row can vanish between the failed mutation and the stamp call
        (e.g. a concurrent delete) — _stamp_gate_recipe_failure must not raise,
        just skip silently, mirroring the act-loop's own task-is-None guard."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        save_dev_queue(DevQueueStore(tasks=[]))

        _stamp_gate_recipe_failure("GEN-1", "acme", now=_NOW)

        assert load_dev_queue().tasks == []


class TestFindBlockedTask:
    def test_resolves_the_only_match(self) -> None:
        task = _make_task()
        store = DevQueueStore(tasks=[task])

        found = _find_blocked_task(store, "GEN-1", "acme")

        assert found is task

    def test_returns_none_when_no_match(self) -> None:
        store = DevQueueStore(tasks=[])

        assert _find_blocked_task(store, "GEN-1", "acme") is None

    def test_ignores_non_blocked_status_rows(self) -> None:
        task = _make_task(status=QueueItemStatus.PENDING)
        store = DevQueueStore(tasks=[task])

        assert _find_blocked_task(store, "GEN-1", "acme") is None

    def test_duplicate_rows_resolve_to_newest_blocked_on_user(self) -> None:
        """Regression test (Data Safety cycle-3 finding): a naive first-match
        lookup could resolve a duplicate (ticket_id, client) row differently
        than dev_queue._find_ticket's tie-break, silently latching or
        re-validating the wrong physical row. _find_blocked_task must mirror
        that tie-break (newest created_at wins) for the BLOCKED_ON_USER tier
        it operates on."""
        older = _make_task(
            session_id="sess-old", created_at=datetime(2026, 7, 1, tzinfo=UTC)
        )
        newer = _make_task(
            session_id="sess-new", created_at=datetime(2026, 7, 8, tzinfo=UTC)
        )
        store = DevQueueStore(tasks=[older, newer])

        found = _find_blocked_task(store, "GEN-1", "acme")

        assert found is newer
        assert found.session_id == "sess-new"


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

    def _stale_candidate(self) -> GateRecipeCandidate:
        return GateRecipeCandidate(
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

    def test_not_blocked_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Row no longer BLOCKED_ON_USER at act time (e.g. concurrent advance)."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(status=QueueItemStatus.PENDING)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        assert _act_auto_approve_review([self._stale_candidate()], now=_NOW) == []

    def test_row_deleted_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Row removed from the dev-queue store entirely between detect and act
        (the ``task is None`` half of the lookup's skip condition — distinct
        from the ``test_not_blocked_at_act_skips`` case above, which only
        flips ``status`` and never removes the row from ``store.tasks``)."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        assert _act_auto_approve_review([self._stale_candidate()], now=_NOW) == []

    def test_session_id_cleared_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Row's session_id cleared between detect and act."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(session_id=None)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        assert _act_auto_approve_review([self._stale_candidate()], now=_NOW) == []

    def test_session_gone_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Session record pruned between detect and act."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(session_id="sess-1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        assert _act_auto_approve_review([self._stale_candidate()], now=_NOW) == []


class TestCommentNonZeroReturn:
    def test_nonzero_gh_return_is_logged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import subprocess

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task()
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        def _fail(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"nope")

        monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _fail)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-1"]  # approve stands despite comment rc!=0
        assert any(
            "rc=1" in rec.message and "GEN-1" in rec.message for rec in caplog.records
        )


def test_recipe_constants_are_distinct() -> None:
    """Both recipe keys are defined (P3 wires the second one, #1066)."""
    assert RECIPE_AUTO_APPROVE_REVIEW == "auto_approve_clean_review"
    assert RECIPE_AUTO_ADOPT_PLAN == "auto_adopt_clean_plan"
    assert RECIPE_AUTO_APPROVE_REVIEW != RECIPE_AUTO_ADOPT_PLAN


# --------------------------------------------------------------------------- #
# RFC 0009 P3 — auto_adopt_clean_plan (#1066)
# --------------------------------------------------------------------------- #

# The predicate_snapshot the two markers in _plan_body() extract to (R3):
# snake_case keys, "<date> <vN>" string values, no other keys.
_PLAN_SNAPSHOT: dict[str, object] = {
    "plan_spec_reviewed": "2026-07-08 v2",
    "plan_soundness_reviewed": "2026-07-08 v1",
}


def _plan_result(status: str = "plan_pending_approval") -> dict[str, Any]:
    """Build a last_result dict for a plan-gate sentinel snapshot.

    The plan recipe only reads ``status`` off ``last_result`` (the review/
    health/scope blocks are hardcoded zeros at plan-stage exit and are
    intentionally NOT read), so this is deliberately minimal.
    """
    return {"status": status}


def _plan_body(*, spec: bool = True, soundness: bool = True) -> str:
    """Build a plan-of-record body with optional signoff markers.

    Markers match the verbatim shape auto-dev-plan.md appends:
    ``<!-- plan-spec-reviewed: YYYY-MM-DD vN -->`` /
    ``<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->``.
    """
    lines = ["# Plan — some ticket", ""]
    if spec:
        lines.append("<!-- plan-spec-reviewed: 2026-07-08 v2 -->")
    if soundness:
        lines.append("<!-- plan-soundness-reviewed: 2026-07-08 v1 -->")
    lines.extend(["", "body text"])
    return "\n".join(lines)


def _stub_fetch_plan(monkeypatch: pytest.MonkeyPatch, body: str | None) -> None:
    """Stub gate_recipes.fetch_approved_plan_comment to return *body*."""
    monkeypatch.setattr(
        "cw.reconcile.gate_recipes.fetch_approved_plan_comment",
        lambda _ticket_id, **_k: body,
    )


class TestDetectAdoptPlan:
    def test_detects_clean_plan_pending_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, lane="fastlane")
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        candidates = _detect_auto_adopt_plan(
            state, [task], clients=_SEAM1_CLIENTS, config=_config()
        )

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.ticket_id == "GEN-1"
        assert cand.client == "acme"
        assert cand.lane == "fastlane"
        assert cand.recipe == RECIPE_AUTO_ADOPT_PLAN
        assert cand.session_id == "sess-1"
        assert cand.evidence == _PLAN_SNAPSHOT

    def test_missing_soundness_marker_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body(soundness=False))
        task = _make_task(stage=Stage.PLAN)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_missing_spec_marker_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body(spec=False))
        task = _make_task(stage=Stage.PLAN)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_non_plan_pending_status_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        session = _make_session(
            last_result=_plan_result(status="review_pending_approval")
        )
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_non_blocked_status_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, status=QueueItemStatus.PENDING)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_latched_failure_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, gate_recipe_failed_at=_NOW)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_no_session_id_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, session_id=None)
        state = CwState(sessions=[])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_missing_session_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, session_id="ghost")
        state = CwState(sessions=[])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_null_last_result_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        session = _make_session(last_result=None)
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_falls_back_to_cw_plan_md_when_tracker_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """R1: tracker-first, `.cw/plan.md` fallback. When the tracker read
        returns None, the recipe falls back to the worktree's plan.md."""
        _stub_fetch_plan(monkeypatch, None)
        ws = tmp_path / "ws"
        (ws / ".cw").mkdir(parents=True)
        (ws / ".cw" / "plan.md").write_text(_plan_body(), encoding="utf-8")
        task = _make_task(stage=Stage.PLAN, worktree_path=ws)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        candidates = _detect_auto_adopt_plan(
            state, [task], clients=_SEAM1_CLIENTS, config=_config()
        )

        assert len(candidates) == 1
        assert candidates[0].evidence == _PLAN_SNAPSHOT

    def test_no_worktree_path_when_tracker_returns_none_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None tracker read AND a None worktree_path leaves no fallback —
        the recipe must return None rather than raise on Path(None)."""
        _stub_fetch_plan(monkeypatch, None)
        task = _make_task(stage=Stage.PLAN, worktree_path=None)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_missing_cw_plan_md_when_tracker_returns_none_yields_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tracker returns None and the worktree exists but has no
        `.cw/plan.md` on disk — the fallback is unavailable, so the recipe
        returns None (the ``plan_path.exists()`` False branch)."""
        _stub_fetch_plan(monkeypatch, None)
        ws = tmp_path / "ws"
        ws.mkdir()
        task = _make_task(stage=Stage.PLAN, worktree_path=ws)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_tracker_spec_only_body_not_completed_by_cw_plan_md(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """R2: same-source only, no cross-source union. The tracker returns a
        spec-only body (non-None, so the `.cw/plan.md` fallback is never
        consulted); even though plan.md on disk carries BOTH markers, the
        soundness marker is absent from the single body in scope -> NOT
        detected. Proves markers are never unioned across tracker + file."""
        _stub_fetch_plan(monkeypatch, _plan_body(soundness=False))
        ws = tmp_path / "ws"
        (ws / ".cw").mkdir(parents=True)
        (ws / ".cw" / "plan.md").write_text(_plan_body(), encoding="utf-8")
        task = _make_task(stage=Stage.PLAN, worktree_path=ws)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_unclosed_marker_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed on a malformed marker: the soundness marker's prefix is
        present but the comment is never closed with ``-->``. Without a
        closure check, str.split would silently return the rest of the body
        as the "version" — leaking raw plan text into the snapshot. Proves
        _marker_version's fail-closed branch, not just the prefix-presence
        check in _clean_plan_snapshot."""
        unclosed_body = (
            "# Plan\n\n"
            "<!-- plan-spec-reviewed: 2026-07-08 v2 -->\n"
            "<!-- plan-soundness-reviewed: 2026-07-08 v1 unterminated, no close"
        )
        _stub_fetch_plan(monkeypatch, unclosed_body)
        task = _make_task(stage=Stage.PLAN)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_plan_md_read_error_yields_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A read failure between .exists() and read_text() (permission
        error, file removed mid-read, etc.) degrades to "no plan body"
        rather than propagating — an unhandled exception here would abort
        the entire reconcile tick, including the unrelated
        auto_approve_clean_review recipe processed in the same
        run_gate_recipes() call."""
        _stub_fetch_plan(monkeypatch, None)
        ws = tmp_path / "ws"
        (ws / ".cw").mkdir(parents=True)
        (ws / ".cw" / "plan.md").write_text(_plan_body(), encoding="utf-8")
        task = _make_task(stage=Stage.PLAN, worktree_path=ws)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        read_err_msg = "permission denied"

        def _boom_read_text(_self: Path, encoding: str = "utf-8") -> str:
            raise OSError(read_err_msg)

        monkeypatch.setattr(Path, "read_text", _boom_read_text)

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )

    def test_plan_md_non_utf8_content_yields_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """UnicodeDecodeError is not an OSError subclass, so it needs its
        own dedicated coverage distinct from test_plan_md_read_error_yields_none
        (which only proves the OSError arm). Writes genuinely invalid UTF-8
        bytes to disk rather than monkeypatching, so this exercises the real
        decode failure read_text(encoding="utf-8") raises, not a simulated
        stand-in exception type."""
        _stub_fetch_plan(monkeypatch, None)
        ws = tmp_path / "ws"
        (ws / ".cw").mkdir(parents=True)
        (ws / ".cw" / "plan.md").write_bytes(b"\xff\xfe not valid utf-8")
        task = _make_task(stage=Stage.PLAN, worktree_path=ws)
        session = _make_session(last_result=_plan_result())
        state = CwState(sessions=[session])

        assert (
            _detect_auto_adopt_plan(
                state, [task], clients=_SEAM1_CLIENTS, config=_config()
            )
            == []
        )


class TestRunAdoptPlan:
    def test_adopts_clean_plan_like_human(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recipe advances the ticket exactly as a human approve would:
        PLAN/BLOCKED_ON_USER -> IMPL/PENDING, session_id cleared."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        approved = store.tasks[0]
        assert approved.status == QueueItemStatus.PENDING
        assert approved.stage == Stage.IMPL
        assert approved.session_id is None

    def test_event_payload_and_recipe_name(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.events import read_events

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN, lane="fastlane")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        run_gate_recipes(now=_NOW, config=_config())

        events = read_events(
            consumer="test-gate-adopt-event",
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["ticket_id"] == "GEN-1"
        assert payload["client"] == "acme"
        assert payload["lane"] == "fastlane"
        assert payload["session_id"] == "sess-1"
        assert payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN
        assert payload["predicate_snapshot"] == _PLAN_SNAPSHOT
        assert events[0].correlation_id == "GEN-1"

    def test_comment_written_best_effort(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_gh_comment: list[list[str]],
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        run_gate_recipes(now=_NOW, config=_config())

        assert len(stub_gh_comment) == 1
        argv = stub_gh_comment[0]
        assert argv[:4] == ["gh", "issue", "comment", "GEN-1"]
        body = argv[-1]
        assert "auto_adopt_clean_plan" in body
        assert "plan_spec_reviewed: 2026-07-08 v2" in body
        assert "plan_soundness_reviewed: 2026-07-08 v1" in body

    def test_review_then_plan_order(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6: recipe order is review-then-plan, matching constant declaration
        order. One clean-review candidate and one clean-plan candidate both
        fire in one tick; the returned approvals appear in declared order."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        review_task = _make_task(
            ticket_id="GEN-R", session_id="sess-r", stage=Stage.REVIEW
        )
        plan_task = _make_task(ticket_id="GEN-P", session_id="sess-p", stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[review_task, plan_task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        ticket_id="GEN-R",
                        session_id="sess-r",
                        last_result=_clean_result(),
                    ),
                    _make_session(
                        ticket_id="GEN-P",
                        session_id="sess-p",
                        last_result=_plan_result(),
                    ),
                ]
            )
        )

        recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-R", "GEN-P"]

    def test_comment_failure_swallowed_and_logged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A comment-write OSError is logged best-effort; the approve stands."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        boom_msg = "gh exploded"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _boom)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        assert any("GEN-1" in rec.message for rec in caplog.records)

    def test_comment_nonzero_return_is_logged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A nonzero gh return code is logged best-effort; the approve stands."""
        import subprocess

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        def _fail(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"nope")

        monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _fail)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == ["GEN-1"]
        assert any(
            "rc=1" in rec.message and "GEN-1" in rec.message for rec in caplog.records
        )


class TestActAdoptPlanFailure:
    def test_event_survives_failed_mutation_and_no_comment(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stub_gh_comment: list[list[str]],
    ) -> None:
        """Even when the mutation raises, the durable GATE_AUTO_APPROVED event
        is not rolled back, GATE_AUTO_APPROVE_FAILED is emitted as a correction,
        the failure latch is stamped, and no audit comment is posted."""
        from cw.events import read_events
        from cw.exceptions import ApproveGateError

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        boom_msg = "boom"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ApproveGateError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._approve_ticket_locked", _boom)

        with caplog.at_level("WARNING"):
            recovered = run_gate_recipes(now=_NOW, config=_config())

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.PLAN
        assert store.tasks[0].gate_recipe_failed_at == _NOW
        events = read_events(
            consumer="test-gate-adopt-failure",
            event_types=[
                OrchestratorEventType.GATE_AUTO_APPROVED,
                OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
            ],
        )
        approved = [
            e for e in events if e.type == OrchestratorEventType.GATE_AUTO_APPROVED
        ]
        failed = [
            e
            for e in events
            if e.type == OrchestratorEventType.GATE_AUTO_APPROVE_FAILED
        ]
        assert len(approved) == 1
        assert len(failed) == 1
        assert approved[0].payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN
        failed_payload = failed[0].payload
        assert failed_payload["ticket_id"] == "GEN-1"
        assert failed_payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN
        assert boom_msg in failed_payload["error"]
        assert stub_gh_comment == []
        assert any("GEN-1" in rec.message for rec in caplog.records)

    def test_latched_failure_does_not_refire(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.events import read_events
        from cw.exceptions import ApproveGateError

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        boom_msg = "boom"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise ApproveGateError(boom_msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._approve_ticket_locked", _boom)

        first_tick = run_gate_recipes(now=_NOW, config=_config())
        second_tick = run_gate_recipes(now=_NOW, config=_config())

        assert first_tick == []
        assert second_tick == []
        events = read_events(
            consumer="test-gate-adopt-latch",
            event_types=[
                OrchestratorEventType.GATE_AUTO_APPROVED,
                OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
            ],
        )
        approved = [
            e for e in events if e.type == OrchestratorEventType.GATE_AUTO_APPROVED
        ]
        failed = [
            e
            for e in events
            if e.type == OrchestratorEventType.GATE_AUTO_APPROVE_FAILED
        ]
        assert len(approved) == 1
        assert len(failed) == 1


class TestActAdoptRecheckRace:
    def _plan_candidate(self) -> GateRecipeCandidate:
        return GateRecipeCandidate(
            ticket_id="GEN-1",
            client="acme",
            lane="default",
            recipe=RECIPE_AUTO_ADOPT_PLAN,
            evidence=dict(_PLAN_SNAPSHOT),
            session_id="sess-1",
        )

    def test_status_changed_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Row no longer BLOCKED_ON_USER at act time (concurrent advance)."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(stage=Stage.PLAN, status=QueueItemStatus.PENDING)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        assert _act_auto_adopt_plan([self._plan_candidate()], now=_NOW) == []

    def test_last_result_status_changed_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """In-memory recheck (R5): last_result no longer at the plan gate."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        last_result=_plan_result(status="review_pending_approval")
                    )
                ]
            )
        )

        assert _act_auto_adopt_plan([self._plan_candidate()], now=_NOW) == []

    def test_row_deleted_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        assert _act_auto_adopt_plan([self._plan_candidate()], now=_NOW) == []

    def test_session_gone_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        assert _act_auto_adopt_plan([self._plan_candidate()], now=_NOW) == []

    def test_session_id_cleared_at_act_skips(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Row still BLOCKED_ON_USER but its session_id was cleared between
        detect and act (the ``task.session_id is None`` half of the skip)."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        task = _make_task(stage=Stage.PLAN, session_id=None)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        assert _act_auto_adopt_plan([self._plan_candidate()], now=_NOW) == []

    def test_fetch_not_recalled_during_act(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R5: the plan-of-record network read is NOT held under the lock. Act
        must reuse candidate.evidence and never re-call
        fetch_approved_plan_comment. Asserts the approve still lands purely
        from in-memory state + the detect-time snapshot."""
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        calls: list[str] = []
        no_refetch_msg = "fetch must not be re-called during act"

        def _boom_fetch(ticket_id: str, **_k: Any) -> str | None:
            calls.append(ticket_id)
            raise AssertionError(no_refetch_msg)

        monkeypatch.setattr(
            "cw.reconcile.gate_recipes.fetch_approved_plan_comment", _boom_fetch
        )
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        recovered = _act_auto_adopt_plan([self._plan_candidate()], now=_NOW)

        assert recovered == ["GEN-1"]
        assert calls == []
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL

    def test_comment_posted_after_lock_release(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R5's lock-scope guarantee: the gh issue comment subprocess call
        must fire AFTER dev_queue_lock() releases, never while held. Prior
        tests only proved *what* gets called (stub_gh_comment); this proves
        *when* — a regression here would silently re-widen the lock hold
        time to include a ~30s network call, defeating R5's whole point."""
        import subprocess

        from cw.dev_queue import dev_queue_lock as real_lock

        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _stub_fetch_plan(monkeypatch, _plan_body())
        task = _make_task(stage=Stage.PLAN)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        events: list[str] = []

        @contextmanager
        def _tracking_lock() -> Iterator[None]:
            with real_lock():
                events.append("locked")
                yield
            events.append("unlocked")

        monkeypatch.setattr("cw.reconcile.gate_recipes.dev_queue_lock", _tracking_lock)

        def _tracking_run(
            argv: list[str], **_kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            events.append("comment_posted")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr("cw.reconcile.gate_recipes.subprocess.run", _tracking_run)

        run_gate_recipes(now=_NOW, config=_config())

        assert events == ["locked", "unlocked", "comment_posted"]


def test_marker_version_fails_closed_when_marker_absent() -> None:
    """_marker_version defends its own fail-closed contract rather than
    depending on callers to pre-check marker presence — direct unit test
    for the branch _clean_plan_snapshot's presence guard never exercises
    (that guard always short-circuits before calling this function)."""
    assert _marker_version("no markers here", marker="<!-- plan-spec-reviewed") is None


def test_plan_spec_marker_matches_gh_marker() -> None:
    """Drift guard for the intentionally-duplicated marker constant: the
    module docstring for _PLAN_SPEC_MARKER says "keep the two in sync" but
    that was previously enforced by comment only. If gh._PLAN_MARKER ever
    changes, this recipe's predicate would silently stop matching (fails
    closed, but silently) — this test converts the comment-only invariant
    into a real assertion."""
    from cw.reconcile.gate_recipes import _PLAN_SPEC_MARKER

    assert _PLAN_SPEC_MARKER == _PLAN_MARKER


def _client_with_lanes(*lanes: LaneConfig) -> ClientConfig:
    return ClientConfig(
        name="acme",
        workspace_path=Path("/tmp/ws"),
        default_branch="main",
        lanes=list(lanes),
    )


class TestResolveGateRecipeEnabled:
    """3-tier precedence for resolve_gate_recipe_enabled (RFC 0009 P4)."""

    def test_tier1_task_override_wins_over_lane_and_default(self) -> None:
        """A ticket-level override beats an enabling lane map (True) and a
        disabling lane map (False), in both directions."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True},
                )
            )
        }
        task_off = _make_task(gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: False})
        assert (
            resolve_gate_recipe_enabled(task_off, clients, RECIPE_AUTO_APPROVE_REVIEW)
            is False
        )
        clients_off = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: False},
                )
            )
        }
        task_on = _make_task(gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True})
        assert (
            resolve_gate_recipe_enabled(
                task_on, clients_off, RECIPE_AUTO_APPROVE_REVIEW
            )
            is True
        )

    def test_tier2_lane_map_wins_over_default(self) -> None:
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True},
                )
            )
        }
        task = _make_task()
        assert (
            resolve_gate_recipe_enabled(task, clients, RECIPE_AUTO_APPROVE_REVIEW)
            is True
        )

    def test_tier2_lane_map_recipe_miss_falls_through_to_default(self) -> None:
        """A lane map that sets one recipe but not the other leaves the
        unset recipe on its hardcoded default-off, not implicitly enabled."""
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True},
                )
            )
        }
        task = _make_task()
        assert (
            resolve_gate_recipe_enabled(task, clients, RECIPE_AUTO_ADOPT_PLAN) is False
        )

    def test_tier3_default_off_when_nothing_configured(self) -> None:
        clients = {"acme": _client_with_lanes(LaneConfig(name="default"))}
        task = _make_task()
        assert (
            resolve_gate_recipe_enabled(task, clients, RECIPE_AUTO_APPROVE_REVIEW)
            is False
        )
        assert (
            resolve_gate_recipe_enabled(task, clients, RECIPE_AUTO_ADOPT_PLAN) is False
        )

    def test_client_absent_falls_through_to_default(self) -> None:
        task = _make_task(client="ghost")
        assert (
            resolve_gate_recipe_enabled(task, {}, RECIPE_AUTO_APPROVE_REVIEW) is False
        )

    def test_lane_absent_from_client_falls_through_to_default(self) -> None:
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True},
                )
            )
        }
        task = _make_task(lane="nonexistent")
        assert (
            resolve_gate_recipe_enabled(task, clients, RECIPE_AUTO_APPROVE_REVIEW)
            is False
        )


class TestMasterSwitchVsLane:
    """Master switch on; per-lane enablement decides which rows fire."""

    def test_only_enabled_lane_row_is_a_candidate(self) -> None:
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: False},
                ),
                LaneConfig(
                    name="fastlane",
                    gate_recipes={RECIPE_AUTO_APPROVE_REVIEW: True},
                ),
            )
        }
        task_off = _make_task(ticket_id="GEN-A", lane="default", session_id="sess-a")
        task_on = _make_task(ticket_id="GEN-B", lane="fastlane", session_id="sess-b")
        state = CwState(
            sessions=[
                _make_session(
                    ticket_id="GEN-A",
                    session_id="sess-a",
                    last_result=_clean_result(),
                ),
                _make_session(
                    ticket_id="GEN-B",
                    session_id="sess-b",
                    last_result=_clean_result(),
                ),
            ]
        )

        candidates = _detect_auto_approve_review(
            state, [task_off, task_on], clients=clients, config=_config()
        )

        assert [c.ticket_id for c in candidates] == ["GEN-B"]

    def test_adopt_plan_lane_disabled_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean-plan row on a lane that disables auto_adopt_clean_plan is
        gated out even though the plan predicate holds."""
        _stub_fetch_plan(monkeypatch, _plan_body())
        clients = {
            "acme": _client_with_lanes(
                LaneConfig(
                    name="default",
                    gate_recipes={RECIPE_AUTO_ADOPT_PLAN: False},
                )
            )
        }
        task = _make_task(stage=Stage.PLAN)
        state = CwState(sessions=[_make_session(last_result=_plan_result())])

        candidates = _detect_auto_adopt_plan(
            state, [task], clients=clients, config=_config()
        )

        assert candidates == []


class TestGateRecipesValidator:
    """Both LaneConfig.gate_recipes and TicketTask.gate_recipes reject
    unrecognized recipe keys at construction (fail-loud)."""

    def test_lane_config_rejects_unrecognized_key(self) -> None:
        with pytest.raises(ValidationError):
            LaneConfig(name="default", gate_recipes={"bogus_recipe": True})

    def test_ticket_task_rejects_unrecognized_key(self) -> None:
        with pytest.raises(ValidationError):
            _make_task(gate_recipes={"bogus_recipe": True})

    def test_lane_config_accepts_recognized_keys(self) -> None:
        lane = LaneConfig(
            name="default",
            gate_recipes={
                RECIPE_AUTO_APPROVE_REVIEW: True,
                RECIPE_AUTO_ADOPT_PLAN: False,
            },
        )
        assert lane.gate_recipes == {
            RECIPE_AUTO_APPROVE_REVIEW: True,
            RECIPE_AUTO_ADOPT_PLAN: False,
        }

    def test_ticket_task_accepts_recognized_keys(self) -> None:
        task = _make_task(
            gate_recipes={
                RECIPE_AUTO_APPROVE_REVIEW: False,
                RECIPE_AUTO_ADOPT_PLAN: True,
            }
        )
        assert task.gate_recipes == {
            RECIPE_AUTO_APPROVE_REVIEW: False,
            RECIPE_AUTO_ADOPT_PLAN: True,
        }

    def test_explicit_none_passes_validator_on_both_models(self) -> None:
        """An explicit None (not just the default) short-circuits both
        gate_recipes field validators without raising."""
        assert LaneConfig(name="default", gate_recipes=None).gate_recipes is None
        assert _make_task(gate_recipes=None).gate_recipes is None
