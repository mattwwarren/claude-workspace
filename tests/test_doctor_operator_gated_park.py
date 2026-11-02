"""``wedge/operator-gated-park`` doctor class (#2739, class 13).

1:1 test module of ``_check_wedge_operator_gated_park`` in
``cw.doctor.wedge.blocked_on_user``. The class is advisory only: it names a
BLOCKED_ON_USER row that holds a lane slot while waiting on an operator, and
``cw doctor --reap`` must never act on it. The two halves pinned here are the
detection (every disposition ``_is_operator_gated_park`` knows, in step with
the class-5 exclusion) and the no-mutation guarantee against every reap
selector, not only ``_reap_wedge_findings``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.codex_review import _const as codex_const
from cw.config import load_state, state_file
from cw.dev_queue import load_dev_queue
from cw.dev_queue.dispositions import (
    FINALIZE_GATE_HELD_DISPOSITION,
    STALE_DISPATCH_GATE_DISPOSITION,
)
from cw.dev_queue.next_commands import (
    HUMAN_GATE_LABEL,
    HUMAN_GATE_PURPOSE_LABEL,
    is_human_gate_command,
    next_commands,
)
from cw.doctor import DoctorReport, WedgeFinding, run_doctor
from cw.doctor.core import _collect_wedge_findings
from cw.doctor.loop_health import _reap_session_by_selector
from cw.doctor.wedge._constants import (
    _DIRTY_WORKTREE_DISPOSITION,
    _HUMAN_GATED_PARK_DISPOSITIONS,
    _OPERATOR_GATED_PARK_DISPOSITIONS,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_OPERATOR_GATED_PARK,
)
from cw.doctor.wedge.blocked_on_user import (
    _cancel_terminal_sibling_parks,
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_operator_gated_park,
    _collapse_blocked_on_user_tasks,
    _is_operator_gated_park,
)
from cw.doctor.wedge.reap import _reap_wedge_findings
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    CwState,
    DevQueueStore,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionStatus,
    occupies_lane_slot,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.ticket_view import ViewSources, next_command_facts
from tests._ticket_view_helpers import (
    ACME_TICKET,
    bound_session_for,
    make_blocked,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session, TicketTask
    from cw.models.ticket_view import NextCommand

_SESSION_ID = "sess-1"
_TERMINAL_SIBLING = ReapReason.TERMINAL_SIBLING.value
_ATTEMPT_CAP_BLOCKED = DispatchSkipReason.ATTEMPT_CAP_BLOCKED.value
_BREACH_REASONS = (
    codex_const.CODEX_FIX_SCOPE_VIOLATION,
    codex_const.CODEX_FIX_SCOPE_DRIFT,
    codex_const.CODEX_FIX_REVERTED_BRANCH,
    codex_const.CODEX_FIX_DIRTY_START,
    codex_const.CODEX_FIX_HOOK_FAILED,
    codex_const.CODEX_FIX_GROWTH_BUDGET,
    codex_const.CODEX_FIX_CONSTRAINT_VIOLATION,
)
# (disposition, blocked_reason) pairs: the matrix test_doctor.py pins for class 5.
_GATED: list[tuple[str, str | None]] = [
    (FINALIZE_GATE_HELD_DISPOSITION, None),
    (STALE_DISPATCH_GATE_DISPOSITION, None),
    (_ATTEMPT_CAP_BLOCKED, None),
    ("blocked", codex_const.CODEX_FIX_SCOPE_DRIFT),
    ("blocked", codex_const.CODEX_FIX_GROWTH_BUDGET),
]


def _row(
    disposition: str | None, reason: str | None, **overrides: object
) -> TicketTask:
    """A BLOCKED_ON_USER ``ACM-1`` row with no session (a dead-looking park)."""
    kwargs: dict[str, object] = {"session_id": None}
    kwargs.update(overrides)
    return make_blocked(disposition=disposition, blocked_reason=reason, **kwargs)


def _stub_daemon_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    """Point every doctor daemon read at one empty, supervisor-alive roster.

    Mirrors ``tests/test_doctor.py``'s ``_stub_daemon`` plus the readers
    ``run_doctor`` reaches through ``blocked_on_user``, ``orphans`` and
    ``reap``. Module-local on purpose: that helper is a method on a test
    class, so it cannot be imported. Returns the shared fake daemon so a test
    can ``seed_live_worker`` a live roster entry after the stub is wired.
    """
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(
        json.dumps({"supervisorPid": 12345, "workers": {}}), encoding="utf-8"
    )
    monkeypatch.setattr("cw.doctor.wedge.session_liveness._ROSTER_PATH", roster_path)
    monkeypatch.setattr("cw.doctor.versions._ROSTER_PATH", roster_path)
    daemon = FakeNativeDaemonClient()
    for target in (
        "cw.doctor.wedge.session_liveness",
        "cw.doctor.wedge.blocked_on_user",
        "cw.doctor.wedge.orphans",
        "cw.doctor.wedge.reap",
        "cw.doctor.loop_health",
    ):
        monkeypatch.setattr(f"{target}.get_native_daemon_client", lambda: daemon)
    return daemon


def _advisory(findings: list[WedgeFinding]) -> list[WedgeFinding]:
    return [f for f in findings if f.wedge_class == _WEDGE_OPERATOR_GATED_PARK]


def _detect(
    tasks: list[TicketTask], sessions: list[Session] | None = None
) -> list[WedgeFinding]:
    """Run the class-13 detector directly on an in-memory state and queue."""
    state = CwState(sessions=sessions or [])
    return _check_wedge_operator_gated_park(state, DevQueueStore(tasks=tasks))


def _expected_first(task: TicketTask, session: Session | None = None) -> NextCommand:
    """The row's first legal command, resolved as ``cw dev-queue tasks`` does."""
    facts = next_command_facts(task, ViewSources())
    return next_commands(
        task,
        session=session,
        operator_opt_in=facts.operator_opt_in,
        pipeline_position=facts.position,
    )[0]


def _assert_recipe_names(
    finding: WedgeFinding, task: TicketTask, session: Session | None = None
) -> None:
    """The recipe carries the row's own first command and purpose, never --reap."""
    first = _expected_first(task, session)
    assert first.command in finding.recipe
    assert first.purpose.removeprefix(HUMAN_GATE_PURPOSE_LABEL) in finding.recipe
    assert "--reap" not in finding.recipe
    assert (HUMAN_GATE_LABEL in finding.recipe) == is_human_gate_command(task, first)


def _row_state(ticket_id: str = ACME_TICKET) -> list[tuple[object, ...]]:
    """Every persisted queue row of *ticket_id* as a comparable tuple."""
    return [
        (t.status, t.disposition, t.blocked_reason, t.session_id, t.pr_url)
        for t in load_dev_queue().tasks
        if t.ticket_id == ticket_id
    ]


class TestDetection:
    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_each_gated_disposition_yields_one_finding(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        reason: str | None,
    ) -> None:
        _stub_daemon_roster(tmp_path, monkeypatch)
        task = _row(disposition, reason)
        seed_queue([task])

        report = run_doctor()

        (finding,) = _advisory(report.wedge_findings)
        assert finding.ticket_id == ACME_TICKET
        assert finding.session_id is None
        assert finding.state_file == str(state_file())
        assert disposition in finding.recipe
        assert "holds a lane slot (lane default)" in finding.recipe
        if reason is not None:
            assert reason in finding.recipe
        _assert_recipe_names(finding, task)
        assert not [
            f for f in report.wedge_findings if f.wedge_class != finding.wedge_class
        ]

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_bound_session_is_threaded_and_still_reported(
        self, tmp_config_dir: Path, disposition: str, reason: str | None
    ) -> None:
        task = _row(disposition, reason, session_id=_SESSION_ID)
        session = bound_session_for(task)

        (finding,) = _detect([task], [session])

        assert finding.session_id == _SESSION_ID
        _assert_recipe_names(finding, task, session)

    def test_human_kept_gate_recipe_carries_the_operator_only_label(
        self, tmp_config_dir: Path
    ) -> None:
        task = _row(FINALIZE_GATE_HELD_DISPOSITION, None)
        assert is_human_gate_command(task, _expected_first(task))

        (finding,) = _detect([task])

        assert HUMAN_GATE_LABEL in finding.recipe
        assert "Run:" not in finding.recipe

    def test_attempt_cap_recipe_is_a_plain_command(self, tmp_config_dir: Path) -> None:
        task = _row(_ATTEMPT_CAP_BLOCKED, None)
        assert not is_human_gate_command(task, _expected_first(task))

        (finding,) = _detect([task])

        assert HUMAN_GATE_LABEL not in finding.recipe
        assert "requeue" in finding.recipe
        assert "--reset-attempts" in finding.recipe

    def test_two_rows_yield_two_findings(self, tmp_config_dir: Path) -> None:
        rows = [
            _row(STALE_DISPATCH_GATE_DISPOSITION, None, ticket_id="ACM-2"),
            _row(_ATTEMPT_CAP_BLOCKED, None, ticket_id="ACM-3"),
        ]

        findings = _detect(rows)

        assert [f.ticket_id for f in findings] == ["ACM-2", "ACM-3"]

    def test_degraded_config_still_reports_with_a_recipe(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(name: str) -> object:
            msg = f"no config for {name}"
            raise CwError(msg)

        monkeypatch.setattr("cw.ticket_view.get_effective_client", _boom)
        task = _row(STALE_DISPATCH_GATE_DISPOSITION, None)

        (finding,) = _detect([task])

        assert finding.recipe
        _assert_recipe_names(finding, task)

    def test_no_candidates_returns_before_touching_config(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(name: str) -> object:
            msg = "config must not be read without a candidate"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.ticket_view.get_effective_client", _fail)

        assert _detect([_row("impl_failed", None), make_task()]) == []
        assert _detect([]) == []


class TestNotReported:
    def test_rows_that_hold_no_slot_yield_nothing(self, tmp_config_dir: Path) -> None:
        pending = make_task(disposition=STALE_DISPATCH_GATE_DISPOSITION)
        running = make_task(
            ticket_id="ACM-2",
            status=QueueItemStatus.RUNNING,
            session_id="sess-2",
            blocked_reason=codex_const.CODEX_FIX_SCOPE_DRIFT,
        )
        # The only row where occupies_lane_slot is not redundant with the
        # status test: a terminal_sibling park holds no slot (class 7's turf).
        sibling = _row(
            _TERMINAL_SIBLING, codex_const.CODEX_FIX_SCOPE_DRIFT, ticket_id="ACM-3"
        )
        assert occupies_lane_slot(sibling) is False

        assert _detect([pending, running, sibling]) == []

    @pytest.mark.parametrize(
        ("disposition", "reason"),
        [
            ("blocked", "impl_failed"),
            (None, None),
            (_DIRTY_WORKTREE_DISPOSITION, None),
            *[(d, None) for d in sorted(_HUMAN_GATED_PARK_DISPOSITIONS)],
        ],
    )
    def test_non_gated_rows_yield_nothing(
        self, tmp_config_dir: Path, disposition: str | None, reason: str | None
    ) -> None:
        assert _detect([_row(disposition, reason)]) == []


def _agreement_cases() -> list[tuple[str | None, str | None, bool]]:
    """(disposition, reason, expected-gated) for every shape the predicate knows."""
    gated: list[tuple[str | None, str | None, bool]] = [
        (d, None, True) for d in sorted(_OPERATOR_GATED_PARK_DISPOSITIONS)
    ]
    gated += [("blocked", r, True) for r in _BREACH_REASONS]
    controls: list[tuple[str | None, str | None, bool]] = [
        (d, None, False) for d in sorted(_HUMAN_GATED_PARK_DISPOSITIONS)
    ]
    controls += [
        (_DIRTY_WORKTREE_DISPOSITION, None, False),
        (_TERMINAL_SIBLING, None, False),
        ("blocked", "impl_failed", False),
        (None, None, False),
    ]
    return gated + controls


class TestClassFiveAgreement:
    """Class 5 and the advisory are two consumers of one predicate."""

    @pytest.mark.parametrize(
        ("disposition", "reason", "expect_gated"), _agreement_cases()
    )
    def test_never_both_and_advisory_iff_gated(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str | None,
        reason: str | None,
        expect_gated: bool,
    ) -> None:
        monkeypatch.setattr(
            "cw.doctor.wedge.blocked_on_user.get_native_daemon_client",
            FakeNativeDaemonClient,
        )
        task = _row(disposition, reason)
        state = CwState()
        queue = DevQueueStore(tasks=[task])

        dead = _check_wedge_dead_session_blocked_on_user(state, queue)
        advisory = _check_wedge_operator_gated_park(state, queue)

        assert _is_operator_gated_park(task) is expect_gated
        assert not (dead and advisory)
        assert bool(advisory) is (expect_gated and occupies_lane_slot(task))
        if expect_gated:
            assert dead == []
            assert len(advisory) == 1
            assert advisory[0].wedge_class == _WEDGE_OPERATOR_GATED_PARK
        else:
            assert advisory == []

    def test_every_known_operator_gated_disposition_fires(
        self, tmp_config_dir: Path
    ) -> None:
        for disposition in sorted(_OPERATOR_GATED_PARK_DISPOSITIONS):
            assert len(_detect([_row(disposition, None)])) == 1, disposition

    def test_overlap_row_is_the_advisorys_and_not_class_fives(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.doctor.wedge.blocked_on_user.get_native_daemon_client",
            FakeNativeDaemonClient,
        )
        human = sorted(_HUMAN_GATED_PARK_DISPOSITIONS)[0]
        task = _row(human, codex_const.CODEX_FIX_SCOPE_DRIFT)
        state = CwState()
        queue = DevQueueStore(tasks=[task])

        assert len(_check_wedge_operator_gated_park(state, queue)) == 1
        assert _check_wedge_dead_session_blocked_on_user(state, queue) == []

    def test_dead_session_row_stays_class_fives(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_daemon_roster(tmp_path, monkeypatch)
        seed_queue([_row(None, None)])

        report = run_doctor()

        (dead,) = [
            f
            for f in report.wedge_findings
            if f.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
        ]
        assert "cw doctor --reap" in dead.recipe
        assert _advisory(report.wedge_findings) == []


class TestReapNeverActs:
    """``--reap`` must leave a gated row alone through every mutation path."""

    @staticmethod
    def _seed_bound(
        disposition: str, reason: str | None, **session_overrides: object
    ) -> TicketTask:
        task = _row(disposition, reason, session_id=_SESSION_ID)
        seed_queue([task], [bound_session_for(task, **session_overrides)])
        return task

    @staticmethod
    def _reap_audits() -> list[object]:
        """Every ``SESSION_REAP_AUTHORIZED`` event on record (the reap's audit row)."""
        return list(
            read_events(
                consumer="test-operator-gated-park",
                event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED],
            )
        )

    @staticmethod
    def _bound_status() -> SessionStatus:
        (session,) = [s for s in load_state().sessions if s.id == _SESSION_ID]
        return session.status

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_run_doctor_reap_end_to_end(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        reason: str | None,
    ) -> None:
        # The bound ACTIVE session's surface is absent from the empty roster,
        # so class 6 reaps the SESSION; the gated queue row must still come
        # out unchanged. The session assertions prove the reap really ran, so
        # the row assertion covers ``_reap_session_by_selector``.
        _stub_daemon_roster(tmp_path, monkeypatch)
        self._seed_bound(disposition, reason)
        before = _row_state()

        report = run_doctor(reap=True)

        assert self._bound_status() == SessionStatus.COMPLETED
        assert len(self._reap_audits()) == 1
        assert _row_state() == before
        assert len(_advisory(report.wedge_findings)) == 1

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_run_doctor_reap_leaves_a_live_bound_session_and_its_row(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        reason: str | None,
    ) -> None:
        # Unpatched run with the bound session LIVE in the roster: class 6 does
        # not fire, so nothing may reap the session, write a reap audit, or
        # touch the row. A class-13 leak into any reap selector would.
        daemon = _stub_daemon_roster(tmp_path, monkeypatch)
        live_ref = daemon.seed_live_worker(tmp_path)
        self._seed_bound(disposition, reason, surface_ref=live_ref)
        before = _row_state()

        report = run_doctor(reap=True)

        assert len(_advisory(report.wedge_findings)) == 1
        assert self._bound_status() == SessionStatus.ACTIVE
        assert self._reap_audits() == []
        assert daemon.stop_calls == []
        assert _row_state() == before

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_reap_wedge_findings_ignores_the_advisory(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        reason: str | None,
    ) -> None:
        # A RUNNING sibling of the same ticket id would be reverted if the
        # class leaked into running_ticket_ids (the set is default-inclusive).
        gated = _row(disposition, reason)
        sibling = make_task(status=QueueItemStatus.RUNNING, session_id="sess-9")
        seed_queue([gated, sibling])
        (finding,) = _detect([gated])

        def _no_lock() -> object:
            msg = "dev_queue_lock must not be entered for an advisory-only class"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.doctor.wedge.reap.dev_queue_lock", _no_lock)
        monkeypatch.setattr(
            "cw.doctor.wedge.reap.has_pending_routed_result_audits", lambda: False
        )
        before = _row_state()

        assert _reap_wedge_findings([finding]) is None

        assert _row_state() == before

    def test_no_tail_selector_is_called(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bound to a session so ``finding.session_id`` is truthy: the daemon
        # and routed-result selectors both key off it, so a class-13 leak into
        # either would actually reach the patched callable below.
        task = self._seed_bound(STALE_DISPATCH_GATE_DISPOSITION, None)
        (finding,) = _detect([task], [bound_session_for(task)])
        assert finding.session_id == _SESSION_ID
        calls: list[str] = []
        for name in (
            "_reap_session_by_selector",
            "reap_routed_result_findings",
            "sweep_leaked_daemon_workers",
        ):
            monkeypatch.setattr(
                f"cw.doctor.wedge.reap.{name}",
                lambda *_a, _name=name, **_kw: calls.append(_name),
            )
        monkeypatch.setattr(
            "cw.doctor.wedge.reap.has_pending_routed_result_audits", lambda: False
        )

        assert _reap_wedge_findings([finding]) is None

        assert calls == []

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_collapse_and_sibling_cancel_skip_the_row(
        self, tmp_config_dir: Path, disposition: str, reason: str | None
    ) -> None:
        task = _row(disposition, reason, session_id=_SESSION_ID)
        queue = DevQueueStore(tasks=[task])

        assert _collapse_blocked_on_user_tasks(queue, {ACME_TICKET}) is False
        assert _cancel_terminal_sibling_parks(queue, {ACME_TICKET}) is False

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.session_id == _SESSION_ID

    @pytest.mark.parametrize(("disposition", "reason"), _GATED)
    def test_targeted_session_reap_leaves_the_row(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        disposition: str,
        reason: str | None,
    ) -> None:
        monkeypatch.setattr(
            "cw.doctor.loop_health.get_native_daemon_client", FakeNativeDaemonClient
        )
        self._seed_bound(disposition, reason)
        before = _row_state()

        assert _reap_session_by_selector(_SESSION_ID) is True

        # The session itself becomes COMPLETED (the operator named it); the
        # queue row is not.
        assert _row_state() == before

    def test_targeted_session_reap_does_revert_a_non_gated_row(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the same path reverts a plain dead park, so the test has teeth."""
        monkeypatch.setattr(
            "cw.doctor.loop_health.get_native_daemon_client", FakeNativeDaemonClient
        )
        task = _row("blocked", "impl_failed", session_id=_SESSION_ID)
        seed_queue([task], [bound_session_for(task)])

        assert _reap_session_by_selector(_SESSION_ID) is True

        (row,) = load_dev_queue().tasks
        assert row.status == QueueItemStatus.PENDING

    def test_cli_reap_leaves_the_row_and_json_lists_the_class(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_daemon_roster(tmp_path, monkeypatch)
        self._seed_bound(STALE_DISPATCH_GATE_DISPOSITION, None)
        before = _row_state()

        reap = CliRunner().invoke(main, ["doctor", "--reap"])
        as_json = CliRunner().invoke(main, ["doctor", "--json"])

        assert _row_state() == before
        assert _WEDGE_OPERATOR_GATED_PARK in reap.output
        payload = json.loads(as_json.stdout)
        assert [f["wedge_class"] for f in payload["wedge_findings"]].count(
            _WEDGE_OPERATOR_GATED_PARK
        ) == 1


class TestWiring:
    def test_collect_wedge_findings_includes_the_class(
        self, tmp_config_dir: Path
    ) -> None:
        task = _row(_ATTEMPT_CAP_BLOCKED, None)

        findings = _collect_wedge_findings(CwState(), DevQueueStore(tasks=[task]))

        assert [f.wedge_class for f in findings] == [_WEDGE_OPERATOR_GATED_PARK]

    def test_the_class_does_not_fail_the_report(self) -> None:
        finding = WedgeFinding(
            wedge_class=_WEDGE_OPERATOR_GATED_PARK,
            session_id=None,
            ticket_id=ACME_TICKET,
            recipe="r",
            state_file="s",
        )

        assert DoctorReport(version="x", wedge_findings=[finding]).ok is True
