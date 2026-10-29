"""Tests for the pure converge-to-clean engine ``cw.review_convergence`` (#2787).

No CLI and no git except ``TestStrictTipWithRealGit``. Every ledger is built
through the real engine and every verdict through the real
``consolidate_verdict`` path (see ``tests/_review_convergence_helpers.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from cw.exceptions import CwError
from cw.review_adjudication import merge_deferred_adjudications
from cw.review_convergence import (
    CONFIRM_UNAVAILABLE_CAP,
    LEDGER_RELATIVE_PATH,
    ROUND_VERDICT_DIR_RELATIVE_PATH,
    ConvergenceError,
    ConvergenceLedger,
    FindingIdentity,
    FrozenCounts,
    RoundInput,
    RoundRecord,
    SettledEntry,
    SettleTarget,
    UnreadFindings,
    clean_mode_supported,
    confirm_blocker_fragment,
    consecutive_confirm_failures,
    demote_for_rereview,
    detect_no_progress,
    detect_oscillation,
    evaluate_tip,
    finalize_ready,
    finding_identity,
    friction_lines,
    from_verdict,
    frozen_counts_from_verdict,
    history_rewritten,
    load_ledger,
    max_cycles_exhausted,
    next_action,
    operator_settle,
    operator_settle_event_payload,
    reconcile_for_init,
    reconcile_ledger_for_init,
    record_confirm_failure,
    record_round,
    release_cause,
    render_history_comment,
    resolve_confirm_backend,
    round_input_findings,
    round_verdict_path,
    save_ledger,
    save_round_verdict,
    sentinel_review_fragment,
    settle_payload,
    status_summary,
    stop_summary,
    suppress_settled,
    unread_from_verdict,
    worktree_state,
)
from cw.review_convergence._backend import _LOOP_FAMILY
from cw.review_findings import ReviewVerdict, consolidate_verdict
from tests._codex_review_helpers import _task
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    FORK_POINT,
    LOOP_BACKEND,
    NOW,
    ROSTER,
    TICKET_ID,
    codex_audit_fixture,
    confirm_verdict_payload,
    confirmed_ledger,
    consolidated_verdict,
    fresh_ledger,
    ledger_with_rounds,
    operator_finding,
    record,
    round_finding,
    round_payload,
    stamp_dispositions,
    stopped_ledger,
    structurally_invalid_doc_json,
    warm_codex_capability,
    write_ledger,
)
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    commit_tracked_file,
    git_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.review_convergence import (
        ConfirmBackend,
        Reconciled,
        RoundFinding,
        RoundKind,
    )

LATER = "2026-10-27T13:00:00+00:00"


def _distinct(index: int, count: int) -> list[RoundFinding]:
    """*count* fix findings for round *index*, each with its own identity."""
    return [round_finding("SHOULD_FIX", f"r{index} f{k}") for k in range(count)]


def _identity_of(finding: RoundFinding) -> FindingIdentity:
    return finding_identity(finding.severity, finding.file, finding.summary)


def _awaiting(sha: str = DEFAULT_SHA) -> ConvergenceLedger:
    """A codex-confirm ledger parked at ``awaiting_confirm`` for *sha*."""
    ledger, report = record(fresh_ledger(confirm="codex"), sha=sha)
    assert report.decision == "confirm_required"
    return ledger


def _reconcile(
    existing: ConvergenceLedger | None,
    *,
    head: str,
    max_cycles: int = 8,
    history_rewritten: bool = False,
    ticket_id: str = TICKET_ID,
    fork_point: str = FORK_POINT,
    confirm: ConfirmBackend | None = "none",
) -> Reconciled:
    return reconcile_ledger_for_init(
        existing,
        fresh=fresh_ledger(ticket_id=ticket_id, fork_point=fork_point, confirm=confirm),
        ticket_id=ticket_id,
        fork_point=fork_point,
        history_rewritten=history_rewritten,
        head_sha=head,
        max_cycles=max_cycles,
        now=LATER,
    )


def _targets(*identities: FindingIdentity) -> list[SettleTarget]:
    return [
        SettleTarget(
            severity="MUST_FIX" if i.severity == "MUST_FIX" else "SHOULD_FIX",
            file=i.file,
            summary=i.summary_normalized,
        )
        for i in identities
    ]


class TestIdentity:
    def test_normalizes_whitespace_like_the_voided_matcher(self) -> None:
        a = finding_identity("MUST_FIX", "src/a.py", "Bug   here\n now")
        b = finding_identity("MUST_FIX", "src/a.py", "Bug here now")
        assert a == b
        assert a.summary_normalized == "Bug here now"

    def test_evidence_and_line_do_not_change_identity(self) -> None:
        first = _make_finding(evidence="def broken():", line_start=10, line_end=10)
        second = _make_finding(evidence="def fixed():", line_start=12, line_end=12)
        assert finding_identity(
            first.severity, first.file, first.summary
        ) == finding_identity(second.severity, second.file, second.summary)

    def test_severity_and_file_are_part_of_identity(self) -> None:
        base = finding_identity("MUST_FIX", "src/a.py", "Bug")
        assert base != finding_identity("SHOULD_FIX", "src/a.py", "Bug")
        assert base != finding_identity("MUST_FIX", "src/b.py", "Bug")

    def test_audit_key_is_severity_file_summary_and_handles_na(self) -> None:
        identity = finding_identity("MUST_FIX", "N/A", "Do   a thing")
        assert identity.audit_key() == "MUST_FIX::N/A::Do a thing"

    def test_identity_is_hashable(self) -> None:
        identity = finding_identity("MUST_FIX", "a", "b")
        assert {identity: 1}[finding_identity("MUST_FIX", "a", "b")] == 1


class TestFromVerdict:
    def test_maps_dispositions_to_outcomes_and_skips_non_gating(self) -> None:
        verdict = stamp_dispositions(
            consolidated_verdict(
                accepted=[
                    ("MUST_FIX", "a"),
                    ("SHOULD_FIX", "b"),
                    ("MUST_FIX", "c"),
                    ("SHOULD_FIX", "d"),
                    ("NIT", "e"),
                ]
            ),
            {
                "b": ("rejected", "why b"),
                "c": ("deferred", "why c"),
                "d": ("operator_actionable", "do d"),
            },
        )
        findings, unread = from_verdict(verdict)
        assert {f.summary: (f.outcome, f.rationale) for f in findings} == {
            "a": ("fix", ""),
            "b": ("reject", "why b"),
            "c": ("defer", "why c"),
            "d": ("operator_action", "do d"),
        }
        assert not unread.any

    @pytest.mark.parametrize(
        "build",
        [
            lambda: consolidated_verdict(accepted=[("MUST_FIX", "a")]),
            lambda: consolidated_verdict(rejected_must_fix=True),
            lambda: consolidated_verdict(discarded={"SHOULD_FIX": 1}),
            lambda: consolidated_verdict(failed_role="Security Reviewer"),
        ],
        ids=["clean", "rejected_must_fix", "discarded", "failed_role"],
    )
    def test_unread_from_verdict_matches_from_verdict(
        self, build: Callable[[], ReviewVerdict]
    ) -> None:
        """#2791: one derivation, shared by ``from_verdict`` and the codex gate."""
        verdict = build()
        _findings, unread = from_verdict(verdict)
        assert unread_from_verdict(verdict) == unread

    @pytest.mark.parametrize("disposition", ["dropped", "unresolved"])
    def test_undecided_finding_raises(self, disposition: str) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        undecided = verdict.model_copy(
            update={
                "accepted": [
                    verdict.accepted[0].model_copy(update={"disposition": disposition})
                ]
            }
        )
        with pytest.raises(ConvergenceError, match="undecided finding"):
            from_verdict(undecided)

    def test_unread_reports_all_three_losses(self) -> None:
        verdict = consolidated_verdict(
            rejected_must_fix=True,
            discarded={"SHOULD_FIX": 1},
            failed_role="Code Quality Reviewer",
        )
        _, unread = from_verdict(verdict)
        assert unread.rejected_must_fix == 1
        assert unread.discarded_run_failures == 1
        assert set(unread.failed_roles) == {
            "Performance Reviewer",
            "Code Quality Reviewer",
        }
        assert unread.any

    def test_nit_only_discard_is_not_a_gating_discard_but_the_role_failed(
        self,
    ) -> None:
        verdict = consolidated_verdict(discarded={"NIT": 1})
        assert verdict.run_failures_with_should_fix_discards == []
        _, unread = from_verdict(verdict)
        assert unread.discarded_run_failures == 0
        assert unread.failed_roles == ("Performance Reviewer",)

    def test_degraded_role_is_not_a_failed_role(self) -> None:
        verdict = consolidate_verdict(
            [
                _make_reviewer_doc(
                    reviewer_role="Perf", status="degraded", detail="ran partially"
                )
            ],
            _make_diff(),
            DEFAULT_SHA,
        )
        _, unread = from_verdict(verdict)
        assert unread.failed_roles == ()
        assert not unread.any

    def test_frozen_counts_come_from_the_verdict_review(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "a"), ("SHOULD_FIX", "b")], rejected_must_fix=True
        )
        frozen = frozen_counts_from_verdict(verdict)
        assert frozen == FrozenCounts(
            must_fix_initial=verdict.review.must_fix_initial,
            should_fix=verdict.review.should_fix,
            agents_run=verdict.review.agents_run,
            rejected_count=verdict.rejected_count,
            rejected_count_by_severity=dict(verdict.rejected_count_by_severity),
        )
        assert frozen.must_fix_initial == 1
        assert frozen.rejected_count == 1

    def test_round_input_requires_exactly_one_source(self) -> None:
        base = {"kind": "loop", "backend": "claude-native", "reviewed_sha": "s"}
        with pytest.raises(ValidationError, match="exactly one"):
            RoundInput.model_validate(base)
        verdict = consolidated_verdict()
        with pytest.raises(ValidationError, match="exactly one"):
            RoundInput.model_validate(
                {
                    **base,
                    "findings": [],
                    "verdict": json.loads(verdict.model_dump_json()),
                }
            )

    def test_round_input_findings_for_both_forms(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "a")], failed_role="Code Quality Reviewer"
        )
        verdict_input = RoundInput(
            kind="loop", backend="claude-native", reviewed_sha="s", verdict=verdict
        )
        findings, unread = round_input_findings(verdict_input)
        assert [f.summary for f in findings] == ["a"]
        assert unread is not None
        assert unread.failed_roles == ("Code Quality Reviewer",)

        payload = round_payload("loop", "claude-native", "s", round_finding())
        findings_input = RoundInput.model_validate(payload)
        findings, unread = round_input_findings(findings_input)
        assert len(findings) == 1
        assert unread is None


class TestSuppressSettled:
    def _settled_ledger(self) -> ConvergenceLedger:
        """A ledger whose round 1 rejected "Bug A" and deferred "Bug B"."""
        ledger, report = record(
            fresh_ledger(confirm="codex"),
            round_finding(
                "MUST_FIX", "Bug A", outcome="reject", rationale="A is a false positive"
            ),
            round_finding(
                "SHOULD_FIX", "Bug B", outcome="defer", rationale="B is out of scope"
            ),
            sha="s1",
        )
        assert report.new_settled == 2
        return ledger

    def test_stamps_findings_with_stored_outcome_and_verbatim_rationale(self) -> None:
        ledger = self._settled_ledger()
        resurfaced = [
            round_finding("MUST_FIX", "Bug A"),
            round_finding("SHOULD_FIX", "Bug   B"),
            round_finding("SHOULD_FIX", "Fresh"),
        ]
        stamped, adjudications = suppress_settled(ledger, resurfaced)
        assert [(f.summary, f.outcome) for f in stamped] == [
            ("Bug A", "reject"),
            ("Bug   B", "defer"),
            ("Fresh", "fix"),
        ]
        assert stamped[0].rationale == "A is a false positive"
        assert [(a.outcome, a.rationale) for a in adjudications] == [
            ("reject", "A is a false positive"),
            ("defer", "B is out of scope"),
        ]
        assert stamped[2] is resurfaced[2]

    def test_nothing_resurfaced_returns_the_input_untouched(self) -> None:
        ledger = self._settled_ledger()
        findings = [round_finding("SHOULD_FIX", "Fresh")]
        stamped, adjudications = suppress_settled(ledger, findings)
        assert stamped == findings
        assert adjudications == []

    def test_adjudications_dedupe_through_merge_deferred_adjudications(self) -> None:
        ledger = self._settled_ledger()
        _, first = suppress_settled(ledger, [round_finding("MUST_FIX", "Bug A")])
        _, second = suppress_settled(ledger, [round_finding("MUST_FIX", "Bug A")])
        assert merge_deferred_adjudications(first, second) == first

    def test_verdict_form_stamps_and_recomputes_only_gate_fields(self) -> None:
        ledger = self._settled_ledger()
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "Bug A"), ("MUST_FIX", "Other"), ("NIT", "nit")],
            rejected_must_fix=True,
            failed_role="Code Quality Reviewer",
        )
        suppressed, adjudications = suppress_settled(ledger, verdict)
        assert isinstance(suppressed, ReviewVerdict)
        first, second, third = suppressed.accepted
        assert (first.disposition, first.disposition_detail) == (
            "rejected",
            "A is a false positive",
        )
        assert second == verdict.accepted[1]
        assert third == verdict.accepted[2]
        assert [f.summary for f in suppressed.must_fix] == ["Other"]
        assert suppressed.blocking is True
        assert adjudications[0].rationale == "A is a false positive"
        assert adjudications[0].line_start == verdict.accepted[0].finding.line_start
        assert adjudications[0].evidence == verdict.accepted[0].finding.evidence
        # Everything outside accepted/must_fix/blocking is preserved verbatim.
        for field in (
            "review",
            "rejected_must_fix",
            "run_failures_with_should_fix_discards",
            "agents_run",
            "rejected_count",
        ):
            assert getattr(suppressed, field) == getattr(verdict, field)

    def test_verdict_form_settled_only_must_fix_stops_blocking(self) -> None:
        ledger = self._settled_ledger()
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "Bug A")])
        suppressed, _ = suppress_settled(ledger, verdict)
        assert isinstance(suppressed, ReviewVerdict)
        assert suppressed.must_fix == []
        assert suppressed.blocking is False

    def test_verdict_form_defer_stamps_deferred(self) -> None:
        ledger = self._settled_ledger()
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Bug B")])
        suppressed, adjudications = suppress_settled(ledger, verdict)
        assert isinstance(suppressed, ReviewVerdict)
        assert suppressed.accepted[0].disposition == "deferred"
        assert adjudications[0].outcome == "defer"

    def test_verdict_form_nothing_resurfaced_is_the_same_object(self) -> None:
        ledger = self._settled_ledger()
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "Fresh"), ("NIT", "Bug A")]
        )
        suppressed, adjudications = suppress_settled(ledger, verdict)
        assert suppressed is verdict
        assert adjudications == []

    def test_blank_stored_rationale_still_yields_a_valid_adjudication(self) -> None:
        ledger = ledger_with_rounds(
            [round_finding("SHOULD_FIX", "Quiet", outcome="defer", rationale="")],
        )
        # A lone defer is clean (converged); the entry is what matters here.
        assert ledger.settled[0].rationale == ""
        _, adjudications = suppress_settled(
            ledger, [round_finding("SHOULD_FIX", "Quiet")]
        )
        assert adjudications[0].rationale


class TestRecordRoundDecisions:
    def test_fixture_a_must_loop(self) -> None:
        ledger = fresh_ledger()
        ledger, first = record(ledger, round_finding("MUST_FIX", "A"), sha="s1")
        assert first.decision == "continue"
        ledger, second = record(ledger, round_finding("SHOULD_FIX", "B"), sha="s2")
        assert second.decision == "continue"
        assert second.new_accepted == 1
        assert ledger.rounds[-1].clean is False
        assert ledger.status == "in_progress"
        assert ledger.fix_cycles_used == 2
        assert (second.fix_cycles_used, second.next_fix_cycle) == (2, 2)
        assert second.history == [1, 1]
        assert second.reason is None

    def test_clean_round_without_confirm_converges(self) -> None:
        ledger, report = record(fresh_ledger(confirm="none"), sha="s1")
        assert report.decision == "clean"
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == "s1"
        assert finalize_ready(ledger)
        assert report.next_fix_cycle == 1
        assert report.confirm_not_independent is False

    def test_clean_loop_round_with_codex_confirm_awaits_it(self) -> None:
        ledger, report = record(fresh_ledger(confirm="codex"), sha="s1")
        assert report.decision == "confirm_required"
        assert ledger.status == "awaiting_confirm"
        assert ledger.awaiting_confirm_sha == "s1"
        assert ledger.last_clean_sha is None
        assert not finalize_ready(ledger)

    def test_clean_confirm_at_the_same_sha_converges(self) -> None:
        ledger, report = record(_awaiting("s1"), sha="s1", kind="confirm")
        assert report.decision == "clean"
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == "s1"
        assert ledger.awaiting_confirm_sha is None
        assert ledger.rounds[-1].kind == "confirm"
        assert finalize_ready(ledger)

    def test_confirm_at_a_different_sha_raises(self) -> None:
        with pytest.raises(ConvergenceError, match="awaiting-confirm candidate"):
            record(_awaiting("s1"), sha="s2", kind="confirm")

    def test_confirm_with_a_different_backend_raises(self) -> None:
        with pytest.raises(ConvergenceError, match="confirm backend"):
            record(_awaiting("s1"), sha="s1", kind="confirm", backend="claude")

    def test_confirm_round_requires_an_awaiting_ledger(self) -> None:
        with pytest.raises(ConvergenceError, match="requires an awaiting_confirm"):
            record(fresh_ledger(confirm="codex"), sha="s1", kind="confirm")

    @pytest.mark.parametrize("terminal", ["converged", "stopped"])
    def test_a_terminal_ledger_refuses_rounds(self, terminal: str) -> None:
        ledger = fresh_ledger().model_copy(update={"status": terminal})
        with pytest.raises(
            ConvergenceError, match=f"cannot record a round on a {terminal}"
        ):
            record(ledger, sha="s1")

    def test_confirm_finding_continues_clears_the_wait_and_applies_settled(
        self,
    ) -> None:
        ledger, _ = record(
            fresh_ledger(confirm="codex"),
            round_finding("SHOULD_FIX", "X", outcome="reject", rationale="no"),
            sha="s1",
        )
        assert ledger.status == "awaiting_confirm"
        ledger, report = record(
            ledger,
            round_finding("SHOULD_FIX", "X"),
            round_finding("MUST_FIX", "Y"),
            sha="s1",
            kind="confirm",
        )
        assert report.decision == "continue"
        assert report.new_accepted == 1
        assert report.resurfaced_settled == 1
        assert ledger.awaiting_confirm_sha is None
        assert ledger.status == "in_progress"
        assert ledger.last_clean_sha is None

    def test_loop_round_at_a_new_sha_clears_the_awaited_sha(self) -> None:
        ledger, report = record(_awaiting("s1"), round_finding(), sha="s2")
        assert report.decision == "continue"
        assert ledger.awaiting_confirm_sha is None
        assert ledger.status == "in_progress"

    def test_loop_round_at_the_same_sha_is_allowed(self) -> None:
        ledger, report = record(_awaiting("s1"), sha="s1")
        assert report.decision == "confirm_required"
        assert ledger.awaiting_confirm_sha == "s1"
        assert len(ledger.rounds) == 2

    def test_explicit_claude_equals_loop_backend_is_clean_but_not_independent(
        self,
    ) -> None:
        ledger, report = record(fresh_ledger(confirm="claude"), sha="s1")
        assert report.decision == "clean"
        assert report.confirm_not_independent is True
        assert ledger.confirm_not_independent is True
        assert ledger.status == "converged"

    def test_frozen_counts_are_stored_once(self) -> None:
        first = FrozenCounts(must_fix_initial=2, should_fix=1, agents_run=3)
        second = FrozenCounts(must_fix_initial=9, should_fix=9, agents_run=9)
        ledger = fresh_ledger()
        ledger, _ = record_round(
            ledger,
            kind="loop",
            backend="claude-native",
            reviewed_sha="s1",
            findings=[round_finding()],
            roster=ROSTER,
            now=NOW,
            frozen=first,
        )
        ledger, _ = record_round(
            ledger,
            kind="loop",
            backend="claude-native",
            reviewed_sha="s2",
            findings=[round_finding(summary="other")],
            roster=ROSTER,
            now=NOW,
            frozen=second,
        )
        assert ledger.frozen == first

    def test_record_does_not_mutate_its_input(self) -> None:
        ledger = fresh_ledger()
        before = ledger.model_dump()
        record(ledger, round_finding(), sha="s1")
        assert ledger.model_dump() == before

    def test_round_record_carries_counts_roster_and_time(self) -> None:
        ledger, _ = record(
            fresh_ledger(),
            round_finding("MUST_FIX", "a"),
            round_finding("SHOULD_FIX", "b", outcome="reject", rationale="r"),
            sha="s1",
        )
        assert ledger.rounds[0] == RoundRecord(
            round=1,
            kind="loop",
            backend="claude-native",
            reviewed_sha="s1",
            new_accepted=1,
            new_fix=1,
            new_operator_action=0,
            new_settled=1,
            resurfaced_settled=0,
            clean=False,
            roster=ROSTER,
            recorded_at=NOW,
        )

    def test_dropped_finding_raises_before_record_round(self) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        dropped = stamp_dispositions(verdict, {"x": ("dropped", "")})
        with pytest.raises(ConvergenceError):
            from_verdict(dropped)


class TestOscillation:
    def test_fixture_b_recurrence_after_a_fix_commit_stops(self) -> None:
        recurring = round_finding("MUST_FIX", "Recurring")
        ledger = ledger_with_rounds([recurring], _distinct(2, 2), shas=["A", "B"])
        ledger, report = record(ledger, recurring, sha="C")
        assert report.decision == "stop_not_converging"
        assert report.reason == "oscillation"
        assert ledger.status == "stopped"
        assert ledger.stop_reason == "oscillation"
        assert ledger.stop_identities == [_identity_of(recurring)]
        assert len(ledger.rounds) == 3
        assert report.fix_cycles_used == 2

    def test_same_sha_recurrence_is_not_oscillation(self) -> None:
        recurring = round_finding("MUST_FIX", "Recurring")
        ledger = ledger_with_rounds([recurring], shas=["A"])
        _, report = record(ledger, recurring, sha="A")
        assert report.decision == "continue"
        assert detect_oscillation(ledger, [recurring], "A") == []
        assert detect_oscillation(ledger, [recurring], "B") == [_identity_of(recurring)]

    def test_detect_oscillation_dedupes_and_ignores_unseen(self) -> None:
        recurring = round_finding("MUST_FIX", "Recurring")
        ledger = ledger_with_rounds([recurring], shas=["A"])
        found = detect_oscillation(
            ledger, [recurring, recurring, round_finding(summary="new")], "B"
        )
        assert found == [_identity_of(recurring)]

    def test_a_stopped_round_does_not_mark_its_findings_fixed(self) -> None:
        ledger = stopped_ledger("operator_action", sha="s1")
        assert ledger.fixed == []

    def test_oscillation_outranks_no_progress(self) -> None:
        recurring = round_finding("SHOULD_FIX", "Recurring")
        ledger = ledger_with_rounds(
            [recurring, *_distinct(1, 2)], _distinct(2, 3), shas=["A", "B"]
        )
        # counts 3,3 then 3 again would be no_progress; the recurrence wins.
        ledger, report = record(ledger, recurring, *_distinct(3, 2), sha="C")
        assert report.reason == "oscillation"


class TestNoProgress:
    @pytest.mark.parametrize("counts", [[3, 3, 3], [2, 3, 4]])
    def test_stalled_series_stops(self, counts: list[int]) -> None:
        ledger = ledger_with_rounds(
            *[_distinct(i, n) for i, n in enumerate(counts[:-1])]
        )
        ledger, report = record(ledger, *_distinct(len(counts), counts[-1]), sha="last")
        assert report.decision == "stop_not_converging"
        assert report.reason == "no_progress"
        assert report.history == counts
        assert ledger.stop_identities == []

    def test_progress_continues(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 5), _distinct(1, 2))
        _, report = record(ledger, *_distinct(2, 1), sha="last")
        assert report.decision == "continue"

    def test_a_clean_round_in_the_window_breaks_the_series(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 5), _distinct(1, 2), confirm="codex")
        ledger, report = record(ledger, sha="s3")
        assert report.decision == "confirm_required"
        ledger, report = record(ledger, *_distinct(3, 3), sha="s3", kind="confirm")
        assert report.decision == "continue"
        assert report.history == [5, 2, 0, 3]

    def test_window_starts_at_the_later_of_epoch_and_window_start(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 3), _distinct(1, 3))
        reset = ledger.model_copy(update={"window_start_round": len(ledger.rounds)})
        _, report = record(reset, *_distinct(2, 3), sha="x")
        assert report.decision == "continue"
        _, control = record(ledger, *_distinct(2, 3), sha="x")
        assert control.reason == "no_progress"
        epoch = ledger.model_copy(update={"epoch_start_round": len(ledger.rounds)})
        _, report = record(epoch, *_distinct(2, 3), sha="x")
        assert report.decision == "continue"

    def test_detect_no_progress_unit_cases(self) -> None:
        def records(*counts: int) -> list[RoundRecord]:
            return [
                RoundRecord(
                    round=i + 1,
                    kind="loop",
                    backend="b",
                    reviewed_sha="s",
                    new_accepted=n,
                    new_fix=n,
                    new_operator_action=0,
                    new_settled=0,
                    resurfaced_settled=0,
                    clean=n == 0,
                    roster=[],
                    recorded_at=NOW,
                )
                for i, n in enumerate(counts)
            ]

        assert detect_no_progress(records(3, 3)) is False
        assert detect_no_progress(records(3, 3, 3)) is True
        assert detect_no_progress(records(5, 4, 3)) is False
        assert detect_no_progress(records(9, 3, 3, 3)) is True
        assert detect_no_progress(records(3, 3, 3), start=1) is False
        assert detect_no_progress(records(3, 3), window=1) is True
        assert detect_no_progress(records(3, 0, 3)) is False


class TestMaxCycles:
    def test_dirty_round_at_the_cap_stops_without_counting_a_cycle(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 2), _distinct(1, 1), max_cycles=2)
        assert ledger.fix_cycles_used == 2
        assert max_cycles_exhausted(ledger)
        ledger, report = record(ledger, *_distinct(2, 2), sha="last")
        assert report.decision == "stop_max_cycles"
        assert report.reason == "max_cycles"
        assert ledger.fix_cycles_used == 2
        assert len(ledger.rounds) == 3
        assert ledger.stop_identities == []

    def test_cap_is_not_exhausted_below_it(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 1), max_cycles=2)
        assert not max_cycles_exhausted(ledger)

    def test_no_progress_outranks_max_cycles(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 3), _distinct(1, 3), max_cycles=2)
        _, report = record(ledger, *_distinct(2, 3), sha="x")
        assert report.reason == "no_progress"


class TestOperatorAction:
    def test_new_operator_action_finding_stops_and_records_its_identity(self) -> None:
        finding = operator_finding("Rotate the key")
        ledger, report = record(fresh_ledger(), finding, sha="s1")
        assert report.decision == "stop_operator_action"
        assert report.reason == "operator_action"
        assert ledger.stop_identities == [
            finding_identity("MUST_FIX", "N/A", "Rotate the key")
        ]
        assert ledger.fix_cycles_used == 0

    def test_duplicate_operator_findings_record_one_identity(self) -> None:
        ledger, _ = record(
            fresh_ledger(), operator_finding("a"), operator_finding("a"), sha="s1"
        )
        assert len(ledger.stop_identities) == 1

    def test_operator_action_outranks_no_progress(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 3), _distinct(1, 3))
        _, report = record(ledger, operator_finding("z"), *_distinct(2, 2), sha="x")
        assert report.reason == "operator_action"


class TestSettledMemory:
    def test_reject_and_defer_resurfacing_never_reopen_the_loop(self) -> None:
        ledger = ledger_with_rounds(
            [
                round_finding("MUST_FIX", "Fix me"),
                round_finding("SHOULD_FIX", "R", outcome="reject", rationale="no"),
                round_finding("SHOULD_FIX", "D", outcome="defer", rationale="later"),
            ]
        )
        assert [e.origin for e in ledger.settled] == ["adjudication", "adjudication"]
        ledger, report = record(
            ledger,
            round_finding("SHOULD_FIX", "R"),
            round_finding("SHOULD_FIX", "D", outcome="defer", rationale="later"),
            sha="s2",
        )
        assert report.decision == "clean"
        assert report.new_accepted == 0
        assert report.resurfaced_settled == 2
        assert report.new_settled == 0

    def test_a_duplicate_new_reject_in_one_round_is_settled_once(self) -> None:
        ledger, report = record(
            fresh_ledger(confirm="codex"),
            round_finding("SHOULD_FIX", "R", outcome="reject", rationale="a"),
            round_finding("SHOULD_FIX", "R", outcome="reject", rationale="b"),
            sha="s1",
        )
        assert report.new_settled == 1
        assert [e.rationale for e in ledger.settled] == ["a"]

    def test_operator_settled_identity_does_not_restop_an_unsettled_one_does(
        self,
    ) -> None:
        recurring = round_finding("MUST_FIX", "Recurring")
        base = ledger_with_rounds([recurring], _distinct(2, 2), shas=["A", "B"])

        _, unsettled = record(base, recurring, sha="C")
        assert unsettled.reason == "oscillation"

        settled, settle_report = operator_settle(
            base,
            _targets(_identity_of(recurring)),
            actor="ops",
            reason="reviewer is wrong about this",
            now=LATER,
        )
        assert settle_report.unmatched == []
        # Reaches record_round stamped `fix`, never through suppress_settled.
        after, report = record(settled, recurring, sha="C")
        assert report.decision == "clean"
        assert report.new_accepted == 0
        assert report.resurfaced_settled == 1
        assert after.rounds[-1].resurfaced_settled == 1

        _, dirty = record(settled, recurring, round_finding(summary="W"), sha="C")
        assert dirty.decision == "continue"
        assert dirty.new_accepted == 1

        _, adjudications = suppress_settled(settled, [recurring])
        assert adjudications[0].rationale == "reviewer is wrong about this"

    def test_operator_action_stop_settle_release_reraise_chain(self) -> None:
        finding = operator_finding("Rotate the key")
        stopped, report = record(fresh_ledger(), finding, sha="s1")
        assert report.decision == "stop_operator_action"
        identity = stopped.stop_identities[0]

        settled, settle_report = operator_settle(
            stopped, _targets(identity), actor="ops", reason="done", now=LATER
        )
        assert settle_report.unmatched == []
        assert settle_report.releasable is True
        assert [e.origin for e in settled.settled] == ["operator"]

        released = _reconcile(settled, head="s1")
        assert released.released is not None
        assert released.released.cause == "operator_action_done"
        assert [e.origin for e in released.ledger.settled] == ["operator"]

        for stamped in (finding, finding.model_copy(update={"outcome": "fix"})):
            _, again = record(released.ledger, stamped, sha="s1")
            assert again.decision != "stop_operator_action"
            assert again.new_accepted == 0

        # Control: without the settle, the same re-raise stops again.
        control = _reconcile(stopped, head="s1")
        _, restop = record(control.ledger, finding, sha="s1")
        assert restop.decision == "stop_operator_action"


class TestConfirmUnavailable:
    def test_failure_appends_an_entry_and_changes_nothing_else(self) -> None:
        ledger = _awaiting("s1")
        failed, report = record_confirm_failure(
            ledger, cause="binary_missing", detail="no codex", now=LATER
        )
        assert report.decision == "stop_confirm_unavailable"
        assert report.reason == "binary_missing"
        (failure,) = failed.confirm_failures
        assert failure.candidate_sha == "s1"
        assert failure.round == len(ledger.rounds)
        assert failure.backend == "codex"
        assert failure.at == LATER
        assert failed.model_copy(update={"confirm_failures": []}) == ledger
        again, _ = record_confirm_failure(
            failed, cause="binary_missing", detail="still", now=LATER
        )
        assert len(again.confirm_failures) == 2
        recovered, ok = record(again, sha="s1", kind="confirm")
        assert ok.decision == "clean"
        assert len(recovered.confirm_failures) == 2

    def test_requires_an_awaiting_confirm_ledger(self) -> None:
        with pytest.raises(ConvergenceError, match="requires an awaiting_confirm"):
            record_confirm_failure(
                fresh_ledger(confirm="codex"), cause="crash", detail="x", now=NOW
            )

    def test_fourth_same_cause_failure_escalates_and_stays_escalated(self) -> None:
        ledger = _awaiting("s1")
        decisions: list[str] = []
        for _ in range(CONFIRM_UNAVAILABLE_CAP + 2):
            ledger, report = record_confirm_failure(
                ledger, cause="crash", detail="boom", now=NOW
            )
            decisions.append(report.decision)
        assert (
            decisions
            == ["stop_confirm_unavailable"] * CONFIRM_UNAVAILABLE_CAP
            + ["stop_confirm_escalated"] * 2
        )
        assert consecutive_confirm_failures(ledger, cause="crash") == 5
        assert consecutive_confirm_failures(ledger, cause="other") == 0
        assert ledger.status == "awaiting_confirm"
        assert len(ledger.rounds) == 1

    def test_a_different_cause_resets_the_run(self) -> None:
        ledger = _awaiting("s1")
        for cause in ("a", "a", "b", "a"):
            ledger, report = record_confirm_failure(
                ledger, cause=cause, detail="d", now=NOW
            )
            assert report.decision == "stop_confirm_unavailable"
        assert consecutive_confirm_failures(ledger, cause="a") == 1

    def test_a_recorded_round_resets_the_run(self) -> None:
        ledger = _awaiting("s1")
        for _ in range(CONFIRM_UNAVAILABLE_CAP):
            ledger, _ = record_confirm_failure(ledger, cause="c", detail="d", now=NOW)
        ledger, _ = record(ledger, sha="s1")  # a clean loop round, same sha
        assert consecutive_confirm_failures(ledger, cause="c") == 0
        _, report = record_confirm_failure(ledger, cause="c", detail="d", now=NOW)
        assert report.decision == "stop_confirm_unavailable"

    def test_a_new_candidate_sha_resets_the_run(self) -> None:
        ledger = _awaiting("s1")
        for _ in range(CONFIRM_UNAVAILABLE_CAP):
            ledger, _ = record_confirm_failure(ledger, cause="c", detail="d", now=NOW)
        ledger, _ = record(ledger, sha="s2")
        assert ledger.awaiting_confirm_sha == "s2"
        _, report = record_confirm_failure(ledger, cause="c", detail="d", now=NOW)
        assert report.decision == "stop_confirm_unavailable"

    def test_the_count_survives_a_save_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / ".cw" / "review-convergence.json"
        ledger = _awaiting("s1")
        decisions: list[str] = []
        for _ in range(CONFIRM_UNAVAILABLE_CAP + 1):
            save_ledger(path, ledger)
            loaded = load_ledger(path)
            assert loaded is not None
            ledger, report = record_confirm_failure(
                loaded, cause="c", detail="d", now=NOW
            )
            decisions.append(report.decision)
        assert decisions[-1] == "stop_confirm_escalated"
        assert decisions[:-1] == ["stop_confirm_unavailable"] * CONFIRM_UNAVAILABLE_CAP

    def test_blocker_fragments_for_park_and_escalation(self) -> None:
        ledger = _awaiting("abcdef123456")
        for _ in range(CONFIRM_UNAVAILABLE_CAP + 1):
            ledger, _ = record_confirm_failure(
                ledger, cause="backend_error", detail="exit 7", now=NOW
            )
        park = confirm_blocker_fragment(ledger, escalated=False, client="acme")
        assert park == {
            "stage": "stage3_review",
            "reason": "confirm_backend_unavailable",
            "details": "backend_error: exit 7",
            "recovery_hint": (
                "restore the confirm backend, then cw dev-queue requeue T-1 -c acme"
            ),
            "retry_eligible": True,
            "retry_delay_seconds": None,
        }
        escalated = confirm_blocker_fragment(ledger, escalated=True)
        assert escalated["reason"] == "agent_block"
        assert escalated["retry_eligible"] is False
        assert str(escalated["details"]).startswith(
            "confirm backend unavailable 4 times in a row with cause backend_error "
            "at abcdef1: exit 7"
        )
        assert "-c <C>" in str(escalated["recovery_hint"])


class TestNoSilentSkip:
    def test_codex_confirm_needs_the_independent_round(self) -> None:
        loop_only, _ = record(fresh_ledger(confirm="codex"), sha="s1")
        assert not finalize_ready(loop_only)

    def test_none_backend_is_ready_after_the_loop_round(self) -> None:
        ledger, _ = record(fresh_ledger(confirm="none"), sha="s1")
        assert finalize_ready(ledger)

    def test_confirmed_ledger_is_ready(self) -> None:
        assert finalize_ready(confirmed_ledger(sha="s1"))


class TestUnreadFindingsGuard:
    """R29: the engine refuses to record unread findings as a clean round."""

    @pytest.mark.parametrize("kind", ["loop", "confirm"])
    @pytest.mark.parametrize(
        ("field", "make_verdict"),
        [
            ("rejected_must_fix", lambda: consolidated_verdict(rejected_must_fix=True)),
            (
                "run_failures_with_should_fix_discards",
                lambda: consolidated_verdict(discarded={"SHOULD_FIX": 1}),
            ),
        ],
    )
    def test_each_field_blocks_a_would_be_clean_round(
        self,
        kind: RoundKind,
        field: str,
        make_verdict: Callable[[], ReviewVerdict],
    ) -> None:
        verdict = make_verdict()
        assert getattr(verdict, field)
        findings, unread = from_verdict(verdict)
        ledger = _awaiting("s1") if kind == "confirm" else fresh_ledger(confirm="none")
        before = ledger.model_dump()
        with pytest.raises(ConvergenceError, match=r"^unread_findings:"):
            record_round(
                ledger,
                kind=kind,
                backend=ledger.confirm_backend
                if kind == "confirm"
                else "claude-native",
                reviewed_sha="s1",
                findings=findings,
                roster=ROSTER,
                now=NOW,
                unread=unread,
            )
        assert ledger.model_dump() == before
        assert ledger.status != "converged"
        assert ledger.last_clean_sha is None
        assert not finalize_ready(ledger)

    def test_control_without_the_field_is_clean(self) -> None:
        findings, unread = from_verdict(consolidated_verdict())
        assert not unread.any
        _, report = record(fresh_ledger(), *findings, sha="s1", unread=unread)
        assert report.decision == "clean"

    def test_findings_form_passes_no_unread_and_is_unchanged(self) -> None:
        _, report = record(fresh_ledger(), sha="s1", unread=None)
        assert report.decision == "clean"

    def test_unread_with_a_new_accepted_finding_is_a_dirty_round(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("SHOULD_FIX", "x")], rejected_must_fix=True
        )
        findings, unread = from_verdict(verdict)
        _, report = record(fresh_ledger(), *findings, sha="s1", unread=unread)
        assert report.decision == "continue"
        assert report.new_accepted == 1

    def test_unread_with_only_settled_accepted_findings_still_raises(self) -> None:
        ledger, _ = record(
            fresh_ledger(confirm="codex"),
            round_finding("SHOULD_FIX", "x", outcome="reject", rationale="no"),
            sha="s1",
        )
        verdict = consolidated_verdict(
            accepted=[("SHOULD_FIX", "x")], rejected_must_fix=True
        )
        findings, unread = from_verdict(verdict)
        with pytest.raises(ConvergenceError, match=r"^unread_findings:"):
            record(ledger, *findings, sha="s2", unread=unread)

    def test_message_states_both_counts(self) -> None:
        verdict = consolidated_verdict(
            rejected_must_fix=True, discarded={"MUST_FIX": 2}
        )
        findings, unread = from_verdict(verdict)
        with pytest.raises(ConvergenceError, match=r"1 MUST_FIX.*1 reviewer failure"):
            record(fresh_ledger(), *findings, sha="s1", unread=unread)


class TestFailedRoleGuard:
    """Obs 2: a role that never reviewed makes a round unrecordable as clean."""

    ROLE = "Code Quality Reviewer"

    @pytest.mark.parametrize("reason", ["unparseable_response", "truncated_response"])
    @pytest.mark.parametrize("confirm", ["none", "claude", "codex"])
    def test_loop_round_with_a_failed_role_raises_for_every_backend(
        self, confirm: ConfirmBackend, reason: str
    ) -> None:
        verdict = consolidated_verdict(failed_role=self.ROLE, failed_reason=reason)
        findings, unread = from_verdict(verdict)
        ledger = fresh_ledger(confirm=confirm)
        before = ledger.model_dump()
        with pytest.raises(ConvergenceError, match=rf"^failed_role:.*{self.ROLE}"):
            record(ledger, *findings, sha="s1", unread=unread)
        assert ledger.model_dump() == before
        assert ledger.awaiting_confirm_sha is None
        assert ledger.last_clean_sha is None
        assert not finalize_ready(ledger)

    def test_confirm_round_with_a_failed_role_raises(self) -> None:
        findings, unread = from_verdict(consolidated_verdict(failed_role=self.ROLE))
        with pytest.raises(ConvergenceError, match=r"^failed_role:"):
            record(_awaiting("s1"), *findings, sha="s1", kind="confirm", unread=unread)

    def test_failed_role_with_a_new_accepted_finding_continues(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "x")], failed_role=self.ROLE
        )
        findings, unread = from_verdict(verdict)
        _, report = record(fresh_ledger(), *findings, sha="s1", unread=unread)
        assert report.decision == "continue"

    def test_failed_role_with_only_settled_accepted_finding_raises(self) -> None:
        ledger, _ = record(
            fresh_ledger(confirm="codex"),
            round_finding("SHOULD_FIX", "x", outcome="reject", rationale="no"),
            sha="s1",
        )
        verdict = consolidated_verdict(
            accepted=[("SHOULD_FIX", "x")], failed_role=self.ROLE
        )
        findings, unread = from_verdict(verdict)
        with pytest.raises(ConvergenceError, match=r"^failed_role:"):
            record(ledger, *findings, sha="s2", unread=unread)

    def test_discard_and_failed_role_reports_unread_findings_first(self) -> None:
        verdict = consolidated_verdict(
            discarded={"SHOULD_FIX": 1}, failed_role=self.ROLE
        )
        findings, unread = from_verdict(verdict)
        with pytest.raises(ConvergenceError, match=r"^unread_findings:"):
            record(fresh_ledger(), *findings, sha="s1", unread=unread)

    def test_nit_only_discard_is_refused_as_a_failed_role(self) -> None:
        findings, unread = from_verdict(consolidated_verdict(discarded={"NIT": 1}))
        with pytest.raises(ConvergenceError, match=r"^failed_role:"):
            record(fresh_ledger(), *findings, sha="s1", unread=unread)

    def test_degraded_role_does_not_raise(self) -> None:
        verdict = consolidate_verdict(
            [_make_reviewer_doc(status="degraded", detail="ran partially")],
            _make_diff(),
            DEFAULT_SHA,
        )
        findings, unread = from_verdict(verdict)
        _, report = record(fresh_ledger(), *findings, sha="s1", unread=unread)
        assert report.decision == "clean"

    def test_unread_findings_model_any(self) -> None:
        assert not UnreadFindings().any
        assert UnreadFindings(rejected_must_fix=1).any
        assert UnreadFindings(discarded_run_failures=1).any
        assert UnreadFindings(failed_roles=("r",)).any


class TestBackendResolver:
    @pytest.mark.parametrize(
        ("configured", "loop", "name", "not_independent"),
        [
            (None, "claude-native", "codex", False),
            (None, "codex", "claude", False),
            (None, "opencode", "codex", False),
            ("codex", "claude-native", "codex", False),
            ("codex", "codex", "codex", True),
            ("none", "claude-native", "none", False),
            ("none", "codex", "none", False),
            ("claude", "claude-native", "claude", True),
            ("claude", "codex", "claude", False),
            ("claude", "opencode", "claude", False),
        ],
    )
    def test_resolution(
        self,
        configured: ConfirmBackend | None,
        loop: str,
        name: str,
        not_independent: bool,
    ) -> None:
        resolved = resolve_confirm_backend(configured, loop)
        assert (resolved.name, resolved.not_independent) == (name, not_independent)


class TestCleanModeSupported:
    """#2797: the one predicate the executor guard and the doctor check share."""

    @pytest.mark.parametrize(
        ("backend", "supported"),
        [
            ("claude-native", True),
            ("codex", True),
            ("opencode", False),
            ("local", False),
            ("", False),
        ],
    )
    def test_supported_backends(self, backend: str, supported: bool) -> None:
        assert clean_mode_supported(backend) is supported

    def test_derived_from_the_loop_family_table(self) -> None:
        for backend in _LOOP_FAMILY:
            assert clean_mode_supported(backend) is True


class TestLedgerStore:
    def test_round_trip_is_atomic_and_leaves_no_temp_files(
        self, tmp_path: Path
    ) -> None:
        ledger = ledger_with_rounds(_distinct(0, 2), _distinct(1, 1))
        path = tmp_path / LEDGER_RELATIVE_PATH
        save_ledger(path, ledger)
        assert load_ledger(path) == ledger
        assert [p.name for p in path.parent.iterdir()] == ["review-convergence.json"]
        save_ledger(path, ledger.model_copy(update={"max_cycles": 3}))
        loaded = load_ledger(path)
        assert loaded is not None
        assert loaded.max_cycles == 3

    def test_missing_file_loads_as_none(self, tmp_path: Path) -> None:
        assert load_ledger(tmp_path / "nope.json") is None

    @pytest.mark.parametrize(
        "body", [b"not json", b"{}", b'{"schema_version": 2}', b"\xff\xfe"]
    )
    def test_corrupt_file_raises_a_value_error(
        self, tmp_path: Path, body: bytes
    ) -> None:
        path = tmp_path / "ledger.json"
        path.write_bytes(body)
        with pytest.raises(ValueError, match=r"\S"):
            load_ledger(path)

    def test_convergence_error_is_a_cw_error(self) -> None:
        assert issubclass(ConvergenceError, CwError)

    def test_path_constants_agree(self, tmp_path: Path) -> None:
        assert Path(".cw/review-convergence.json") == LEDGER_RELATIVE_PATH
        assert round_verdict_path(tmp_path / LEDGER_RELATIVE_PATH, 2) == (
            tmp_path / ROUND_VERDICT_DIR_RELATIVE_PATH / "round-2-verdict.json"
        )

    def test_save_round_verdict_writes_the_verdict(self, tmp_path: Path) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        ledger_path = tmp_path / "custom" / "ledger.json"
        written = save_round_verdict(ledger_path, 3, verdict)
        assert written == tmp_path / "custom" / "ledger" / "round-3-verdict.json"
        assert ReviewVerdict.model_validate_json(written.read_text("utf-8")) == verdict


class TestReconcileForInit:
    def test_no_ledger_starts_fresh(self) -> None:
        result = _reconcile(None, head="s1")
        assert result.ledger == fresh_ledger()
        assert (result.reset_reason, result.released) == (None, None)

    @pytest.mark.parametrize(
        ("ticket_id", "fork_point", "rewritten", "reason"),
        [
            ("T-2", FORK_POINT, False, "ticket_mismatch"),
            (TICKET_ID, "other", False, "fork_point_mismatch"),
            (TICKET_ID, FORK_POINT, True, "history_rewritten"),
        ],
    )
    def test_stale_ledger_is_replaced(
        self, ticket_id: str, fork_point: str, rewritten: bool, reason: str
    ) -> None:
        existing = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        result = _reconcile(
            existing,
            head="s1",
            ticket_id=ticket_id,
            fork_point=fork_point,
            history_rewritten=rewritten,
        )
        assert result.reset_reason == reason
        assert result.ledger.rounds == []
        assert result.released is None

    def test_stale_reset_keeps_a_recoverable_snapshot(self) -> None:
        existing = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        result = _reconcile(existing, ticket_id="T-2", head="s1")
        (reset,) = result.ledger.reset_history
        assert reset.reason == "ticket_mismatch"
        assert reset.previous_ledger == existing.model_dump(mode="json")

    def test_matching_in_progress_ledger_is_resumed(self) -> None:
        existing = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        result = _reconcile(existing, head="s1")
        assert result.ledger == existing
        assert (result.reset_reason, result.released) == (None, None)

    def test_cap_refresh_applies_on_every_non_fresh_path(self) -> None:
        in_progress = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        converged = confirmed_ledger(sha="s1")
        stopped = stopped_ledger("no_progress", sha="s1")
        operator = stopped_ledger("operator_action", sha="s1")
        cases = [
            (in_progress, "s1"),  # resumed
            (converged, "s1"),  # converged, same tip
            (converged, "s2"),  # converged, reopened
            (stopped, "s1"),  # still stopped
            (stopped, "s2"),  # reopened on a new tip
            (operator, "s1"),  # released
        ]
        for existing, head in cases:
            for cap in (1, 20):
                assert (
                    _reconcile(existing, head=head, max_cycles=cap).ledger.max_cycles
                    == cap
                )

    def test_stopped_ledger_on_a_new_tip_is_reopened_as_a_new_epoch(self) -> None:
        stopped = stopped_ledger("oscillation", sha="osc", stop_identities=("A",))
        result = _reconcile(stopped, head="newtip")
        ledger = result.ledger
        assert ledger.status == "in_progress"
        assert ledger.epoch_start_round == len(stopped.rounds)
        assert ledger.fix_cycles_used == 0
        assert ledger.stop_reason is None
        assert ledger.stop_identities == []
        assert (ledger.settled, ledger.fixed) == (stopped.settled, stopped.fixed)
        release = result.released
        assert release is not None
        assert (release.cause, release.reason, release.sha, release.at) == (
            "new_commits",
            "oscillation",
            "newtip",
            LATER,
        )
        assert release.round == len(stopped.rounds)
        assert ledger.releases == [release]
        assert next_action(ledger, head_sha="newtip") == "loop_round"

    def test_max_cycles_stop_same_tip_needs_a_raised_cap(self) -> None:
        stopped = stopped_ledger("max_cycles", sha="stop", max_cycles=2)
        for cap in (2, 1):
            result = _reconcile(stopped, head="stop", max_cycles=cap)
            assert result.ledger.status == "stopped"
            assert result.released is None
            assert result.ledger.stop_reason == "max_cycles"

    def test_max_cycles_stop_released_by_a_raised_cap_buys_exactly_one_cycle(
        self,
    ) -> None:
        stopped = stopped_ledger("max_cycles", sha="stop", max_cycles=2)
        result = _reconcile(stopped, head="stop", max_cycles=3)
        ledger = result.ledger
        assert ledger.status == "in_progress"
        assert ledger.stop_reason is None
        assert ledger.epoch_start_round == stopped.epoch_start_round
        assert ledger.fix_cycles_used == stopped.fix_cycles_used == 2
        assert ledger.window_start_round == len(stopped.rounds)
        assert result.released is not None
        assert result.released.cause == "max_cycles_raised"
        assert next_action(ledger, head_sha="stop") == "loop_round"

        ledger, first = record(ledger, *_distinct(90, 2), sha="stop")
        assert first.decision == "continue"
        assert first.next_fix_cycle == stopped.fix_cycles_used + 1
        _, second = record(ledger, *_distinct(91, 1), sha="later")
        assert second.decision == "stop_max_cycles"

    def test_operator_action_stop_is_released_on_a_same_tip_requeue(self) -> None:
        stopped = stopped_ledger("operator_action", sha="s1")
        result = _reconcile(stopped, head="s1")
        assert result.released is not None
        assert result.released.cause == "operator_action_done"
        ledger = result.ledger
        assert ledger.status == "in_progress"
        assert ledger.stop_identities == []
        assert ledger.window_start_round == len(stopped.rounds)
        assert ledger.epoch_start_round == stopped.epoch_start_round
        assert ledger.fix_cycles_used == stopped.fix_cycles_used

    def test_oscillation_stop_is_terminal_until_every_identity_is_settled(self) -> None:
        stopped = stopped_ledger("oscillation", sha="osc", stop_identities=("A", "B"))
        a, b = stopped.stop_identities
        assert _reconcile(stopped, head="osc").released is None
        partial, _ = operator_settle(
            stopped, _targets(a), actor="ops", reason="fp", now=NOW
        )
        terminal = _reconcile(partial, head="osc")
        assert terminal.released is None
        assert terminal.ledger.status == "stopped"
        full, _ = operator_settle(
            partial, _targets(b), actor="ops", reason="fp", now=NOW
        )
        result = _reconcile(full, head="osc")
        assert result.released is not None
        assert result.released.cause == "identities_settled"
        assert result.ledger.status == "in_progress"

    def test_no_progress_and_unknown_stops_are_always_terminal(self) -> None:
        no_progress = stopped_ledger("no_progress", sha="np")
        result = _reconcile(no_progress, head="np", max_cycles=99)
        assert result.released is None
        assert result.ledger == no_progress.model_copy(update={"max_cycles": 99})
        unknown = no_progress.model_copy(update={"stop_reason": "mystery"})
        assert _reconcile(unknown, head="np").released is None
        assert release_cause(unknown) is None

    def test_an_open_ledger_follows_the_current_confirm_choice(self) -> None:
        awaiting = _awaiting()
        assert awaiting.confirm_backend == "codex"
        result = _reconcile(awaiting, head=DEFAULT_SHA, confirm="none")
        assert result.ledger.confirm_backend == "none"
        assert result.ledger.status == "awaiting_confirm"
        assert result.ledger.awaiting_confirm_sha == awaiting.awaiting_confirm_sha

    def test_the_not_independent_flag_is_refreshed_with_the_backend(self) -> None:
        existing = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        assert existing.confirm_not_independent is False
        refreshed = _reconcile(existing, head="s1", confirm="claude").ledger
        assert refreshed.confirm_backend == "claude"
        # The helper loop backend is claude-native, so claude is not independent.
        assert refreshed.confirm_not_independent is True

    def test_a_converged_ledger_clean_at_head_keeps_its_stamped_choice(self) -> None:
        converged = confirmed_ledger(sha="S", confirm="codex")
        result = _reconcile(converged, head="S", confirm="none")
        assert result.ledger.confirm_backend == "codex"
        assert result.ledger.status == "converged"

    def test_a_demoted_converged_ledger_follows_the_current_choice(self) -> None:
        converged = confirmed_ledger(sha="S", confirm="codex")
        result = _reconcile(converged, head="S+1", confirm="none")
        assert result.ledger.status == "in_progress"
        assert result.ledger.confirm_backend == "none"

    def test_fixture_d_converged_ledger_on_a_moved_tip_is_reopened(self) -> None:
        converged = confirmed_ledger(sha="S")
        settled, _ = record(fresh_ledger(), round_finding(summary="Keep me"), sha="a")
        result = _reconcile(converged, head="S+1")
        reopened = result.ledger
        assert reopened.status == "in_progress"
        assert result.released is None
        assert reopened.releases == []
        assert not finalize_ready(reopened)
        assert reopened.settled == converged.settled
        assert reopened.fixed == converged.fixed
        assert reopened.epoch_start_round == converged.epoch_start_round
        assert reopened.fix_cycles_used == converged.fix_cycles_used
        assert next_action(reopened, head_sha="S+1") == "loop_round"
        same = _reconcile(converged, head="S").ledger
        assert same.status == "converged"
        assert next_action(same, head_sha="S") == "done"
        assert settled.fixed  # the helper really recorded fixed memory

    def test_confirm_failures_are_kept_except_on_a_fresh_ledger(self) -> None:
        awaiting, _ = record_confirm_failure(
            _awaiting("s1"), cause="crash", detail="x", now=NOW
        )
        failures = awaiting.confirm_failures
        assert _reconcile(awaiting, head="s1").ledger.confirm_failures == failures
        stopped = stopped_ledger("operator_action", sha="s1").model_copy(
            update={"confirm_failures": failures}
        )
        assert _reconcile(stopped, head="s1").ledger.confirm_failures == failures
        assert _reconcile(stopped, head="new").ledger.confirm_failures == failures
        assert (
            _reconcile(awaiting, head="s1", ticket_id="T-9").ledger.confirm_failures
            == []
        )


class TestOperatorSettle:
    def _oscillating(self) -> ConvergenceLedger:
        return stopped_ledger(
            "oscillation", sha="osc", stop_identities=("Alpha", "Beta")
        )

    def test_match_via_stop_identities_writes_an_audited_operator_entry(self) -> None:
        stopped = self._oscillating()
        alpha = stopped.stop_identities[0]
        updated, report = operator_settle(
            stopped, _targets(alpha), actor="matt", reason="false positive", now=LATER
        )
        (entry,) = report.settled
        assert entry == SettledEntry(
            identity=alpha,
            outcome="reject",
            rationale="false positive",
            round=len(stopped.rounds),
            origin="operator",
            actor="matt",
            settled_at=LATER,
            reviewed_sha="osc",
        )
        assert updated.settled == [entry]
        assert report.unmatched == []
        assert report.releasable is False  # Beta is still unsettled

    def test_match_via_fixed_and_via_adjudication_settled(self) -> None:
        ledger = ledger_with_rounds(
            [
                round_finding("MUST_FIX", "Fixed one"),
                round_finding(
                    "SHOULD_FIX", "Rejected one", outcome="reject", rationale="orig"
                ),
            ]
        )
        fixed_id = ledger.fixed[0].identity
        rejected_id = ledger.settled[0].identity
        updated, report = operator_settle(
            ledger, _targets(fixed_id, rejected_id), actor="a", reason="r", now=LATER
        )
        assert len(report.settled) == 2
        assert [e.origin for e in updated.settled] == ["operator", "operator"]
        assert {e.identity for e in updated.settled} == {fixed_id, rejected_id}
        assert len(updated.settled) == 2  # the adjudication entry was replaced

    def test_unmatched_targets_name_the_nearest_recorded_identity(self) -> None:
        stopped = self._oscillating()
        alpha = stopped.stop_identities[0]
        near_summary = SettleTarget(
            severity="MUST_FIX", file="src/cw/foo.py", summary="Alphx"
        )
        near_file = SettleTarget(
            severity="MUST_FIX", file="src/cw/fo.py", summary="Alpha"
        )
        unrelated = SettleTarget(
            severity="SHOULD_FIX",
            file="docs/z.md",
            summary="completely unrelated words",
        )
        _, report = operator_settle(
            stopped,
            [near_summary, near_file, unrelated],
            actor="a",
            reason="r",
            now=LATER,
        )
        assert [(u.target, u.nearest) for u in report.unmatched] == [
            (near_summary, alpha),
            (near_file, alpha),
            (unrelated, None),
        ]

    def test_all_unmatched_leaves_the_ledger_untouched(self) -> None:
        stopped = self._oscillating()
        before = stopped.model_dump_json()
        target = SettleTarget(severity="MUST_FIX", file="nowhere", summary="never seen")
        updated, report = operator_settle(
            stopped, [target], actor="a", reason="r", now=LATER
        )
        assert updated is stopped
        assert updated.model_dump_json() == before
        assert report.settled == []
        assert len(report.unmatched) == 1

    def test_partial_match_records_only_the_matched(self) -> None:
        stopped = self._oscillating()
        alpha = stopped.stop_identities[0]
        miss = SettleTarget(severity="MUST_FIX", file="nowhere", summary="never seen")
        updated, report = operator_settle(
            stopped, [*_targets(alpha), miss], actor="a", reason="r", now=LATER
        )
        assert [e.identity for e in updated.settled] == [alpha]
        assert len(report.unmatched) == 1

    def test_whitespace_matches_but_severity_does_not(self) -> None:
        stopped = self._oscillating()
        spaced = SettleTarget(
            severity="MUST_FIX", file="src/cw/foo.py", summary="  Alpha \n"
        )
        other_severity = SettleTarget(
            severity="SHOULD_FIX", file="src/cw/foo.py", summary="Alpha"
        )
        _, report = operator_settle(
            stopped, [spaced, other_severity], actor="a", reason="r", now=LATER
        )
        assert len(report.settled) == 1
        assert [u.target for u in report.unmatched] == [other_severity]

    def test_targets_that_differ_only_by_severity_stay_distinct(self) -> None:
        ledger = ledger_with_rounds(
            [round_finding("MUST_FIX", "same"), round_finding("SHOULD_FIX", "same")]
        )
        targets = [
            SettleTarget(severity="MUST_FIX", file="src/cw/foo.py", summary="same"),
            SettleTarget(severity="SHOULD_FIX", file="src/cw/foo.py", summary="same"),
        ]
        updated, report = operator_settle(
            ledger, targets, actor="a", reason="r", now=LATER
        )
        assert len(report.settled) == len(updated.settled) == 2
        assert len({e.identity.audit_key() for e in updated.settled}) == 2

    def test_settling_twice_is_idempotent_and_the_newest_time_wins(self) -> None:
        stopped = self._oscillating()
        target = _targets(stopped.stop_identities[0])
        once, _ = operator_settle(stopped, target, actor="a", reason="r", now=NOW)
        twice, _ = operator_settle(
            once, [*target, *target], actor="a", reason="r2", now=LATER
        )
        (entry,) = twice.settled
        assert (entry.settled_at, entry.rationale) == (LATER, "r2")

    def test_na_target_matches_like_any_other(self) -> None:
        stopped = stopped_ledger(
            "operator_action", sha="s1", stop_identities=("Do it",)
        )
        identity = stopped.stop_identities[0]
        assert identity.file == "N/A"
        _, report = operator_settle(
            stopped, _targets(identity), actor="a", reason="r", now=NOW
        )
        assert report.unmatched == []
        assert report.releasable is True

    def test_a_ledger_with_no_rounds_cannot_be_settled(self) -> None:
        with pytest.raises(ConvergenceError, match="no recorded rounds"):
            operator_settle(fresh_ledger(), [], actor="a", reason="r", now=NOW)

    def test_releasable_by_stop_kind(self) -> None:
        oscillating = self._oscillating()
        both = oscillating.stop_identities
        _, all_settled = operator_settle(
            oscillating, _targets(*both), actor="a", reason="r", now=NOW
        )
        assert all_settled.releasable is True

        no_progress = stopped_ledger("no_progress", sha="np")
        recorded = no_progress.fixed[0].identity
        _, np_report = operator_settle(
            no_progress, _targets(recorded), actor="a", reason="r", now=NOW
        )
        assert np_report.releasable is False

        capped = stopped_ledger("max_cycles", sha="mc", max_cycles=2)
        _, mc_report = operator_settle(
            capped, _targets(capped.fixed[0].identity), actor="a", reason="r", now=NOW
        )
        assert mc_report.releasable is False

    def test_event_payload_has_the_eight_disposition_keys(self) -> None:
        stopped = stopped_ledger(
            "operator_action", sha="s1", stop_identities=("Do   it",)
        )
        updated, _ = operator_settle(
            stopped,
            _targets(stopped.stop_identities[0]),
            actor="matt",
            reason="done",
            now=LATER,
        )
        assert operator_settle_event_payload(updated.settled[0]) == {
            "key": "MUST_FIX::N/A::Do it",
            "file": "N/A",
            "summary": "Do it",
            "outcome": "REJECTED",
            "reason": "done",
            "actor": "matt",
            "recorded_at": LATER,
            "reviewed_sha": "s1",
        }


class TestEvaluateTip:
    def test_accepted_only_when_head_is_origin_and_the_clean_sha(self) -> None:
        verdict = evaluate_tip(
            confirmed_ledger(sha="s1"), head_sha="s1", origin_tip_sha="s1"
        )
        assert (verdict.decision, verdict.reason) == ("accepted", None)

    @pytest.mark.parametrize("ledger_name", ["fresh", "awaiting", "stopped"])
    def test_no_clean_round(self, ledger_name: str) -> None:
        ledgers = {
            "fresh": fresh_ledger(),
            "awaiting": _awaiting("s1"),
            "stopped": stopped_ledger("no_progress", sha="s1"),
        }
        verdict = evaluate_tip(ledgers[ledger_name], head_sha="s1", origin_tip_sha="s1")
        assert (verdict.decision, verdict.reason) == (
            "rereview_required",
            "no_clean_round",
        )

    def test_head_not_origin_tip_is_checked_before_the_commit_rule(self) -> None:
        ledger = confirmed_ledger(sha="s1")
        for head, origin in (("s1", "s0"), ("s2", "s3")):
            verdict = evaluate_tip(ledger, head_sha=head, origin_tip_sha=origin)
            assert verdict.reason == "head_not_origin_tip"

    def test_commits_after_the_clean_round(self) -> None:
        verdict = evaluate_tip(
            confirmed_ledger(sha="s1"), head_sha="s2", origin_tip_sha="s2"
        )
        assert (verdict.decision, verdict.reason) == (
            "rereview_required",
            "commits_after_clean_round",
        )

    def test_demote_reopens_a_converged_ledger_and_keeps_memory(self) -> None:
        converged = ledger_with_rounds(
            [round_finding("MUST_FIX", "x")], [], confirm="none", shas=["a", "b"]
        )
        assert converged.status == "converged"
        demoted = demote_for_rereview(converged)
        assert demoted.status == "in_progress"
        assert not finalize_ready(demoted)
        assert demoted.fixed == converged.fixed
        assert demoted.rounds == converged.rounds
        assert demoted.releases == []
        assert demote_for_rereview(demoted) is demoted


class TestStrictTipWithRealGit:
    """Fixture C: R22 against a real repo with a bare origin."""

    def _tip(self, repo: Path, ledger: ConvergenceLedger) -> tuple[str, str | None]:
        git_in(repo, "fetch", "origin", "feature")
        verdict = evaluate_tip(
            ledger,
            head_sha=git_in(repo, "rev-parse", "HEAD"),
            origin_tip_sha=git_in(repo, "rev-parse", "origin/feature"),
        )
        return verdict.decision, verdict.reason

    def _converged(self, repo: Path) -> ConvergenceLedger:
        return confirmed_ledger(sha=git_in(repo, "rev-parse", "HEAD"))

    def _advance_main(self, repo: Path) -> None:
        git_in(repo, "checkout", "main")
        commit_tracked_file(repo, "main_only.py")
        git_in(repo, "push", "origin", "main")
        git_in(repo, "checkout", "feature")

    def test_clean_head_equal_to_origin_is_accepted(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _ = make_git_repo_with_origin("tip-ok")
        assert self._tip(repo, self._converged(repo)) == ("accepted", None)

    def test_a_pushed_fix_commit_requires_rereview(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _ = make_git_repo_with_origin("tip-fix")
        ledger = self._converged(repo)
        commit_tracked_file(repo, "fix.py")
        git_in(repo, "push", "origin", "feature")
        assert self._tip(repo, ledger) == (
            "rereview_required",
            "commits_after_clean_round",
        )

    def test_an_unpushed_commit_is_head_not_origin_tip(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _ = make_git_repo_with_origin("tip-unpushed")
        ledger = self._converged(repo)
        commit_tracked_file(repo, "fix.py")
        assert self._tip(repo, ledger) == (
            "rereview_required",
            "head_not_origin_tip",
        )

    def test_a_merge_of_the_default_branch_requires_rereview(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _ = make_git_repo_with_origin("tip-merge")
        ledger = self._converged(repo)
        self._advance_main(repo)
        git_in(repo, "merge", "--no-edit", "main")
        git_in(repo, "push", "origin", "feature")
        assert self._tip(repo, ledger) == (
            "rereview_required",
            "commits_after_clean_round",
        )

    def test_a_force_pushed_rebase_requires_rereview(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _ = make_git_repo_with_origin("tip-rebase")
        ledger = self._converged(repo)
        self._advance_main(repo)
        git_in(repo, "rebase", "main")
        git_in(repo, "push", "--force", "origin", "feature")
        assert self._tip(repo, ledger) == (
            "rereview_required",
            "commits_after_clean_round",
        )


class TestHistoryRewritten:
    """``history_rewritten`` against a real repo (#2791, moved from the CLI)."""

    def _ledger_at(self, sha: str) -> ConvergenceLedger:
        ledger, _ = record(fresh_ledger(), sha=sha)
        return ledger

    def test_no_ledger_or_no_rounds_is_false_without_git(self, tmp_path: Path) -> None:
        missing = tmp_path / "not-a-repo"
        assert history_rewritten(None, missing, "HEAD") is False
        assert history_rewritten(fresh_ledger(), missing, "HEAD") is False

    def test_an_ancestor_is_not_rewritten(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("hist-ancestor")
        base = git_in(repo, "rev-parse", "HEAD")
        commit_tracked_file(repo, "a.py")
        head = git_in(repo, "rev-parse", "HEAD")
        assert history_rewritten(self._ledger_at(base), repo, head) is False

    def test_a_dropped_commit_is_rewritten(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("hist-dropped")
        commit_tracked_file(repo, "a.py")
        dropped = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "reset", "--hard", "HEAD~1")
        head = git_in(repo, "rev-parse", "HEAD")
        assert history_rewritten(self._ledger_at(dropped), repo, head) is True

    def test_an_unknown_sha_raises_cw_error(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("hist-unknown")
        head = git_in(repo, "rev-parse", "HEAD")
        with pytest.raises(CwError, match=r"failed .* with exit 128"):
            history_rewritten(self._ledger_at("f" * 40), repo, head)

    def test_a_long_git_error_is_collapsed_to_one_bounded_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stderr = "fatal:\n" + "x " * 400

        def _failing_git(argv: list[str], **_kwargs: object) -> object:
            return subprocess.CompletedProcess(argv, 128, "", stderr)

        monkeypatch.setattr("cw.review_convergence._release.run_git", _failing_git)
        with pytest.raises(CwError) as raised:
            history_rewritten(self._ledger_at("a" * 40), tmp_path, "HEAD")
        message = str(raised.value)
        assert "\n" not in message
        assert message.endswith("...")
        assert "fatal: x x" in message

    def test_git_that_cannot_run_raises_cw_error(self, tmp_path: Path) -> None:
        with pytest.raises(CwError, match="could not run"):
            history_rewritten(self._ledger_at("a" * 40), tmp_path / "gone", "HEAD")


class TestReconcileForInitEntryPoint:
    """``reconcile_for_init``, the one reconcile both review lanes call (#2791)."""

    @staticmethod
    def _reconcile(
        existing: ConvergenceLedger | None,
        worktree: Path,
        *,
        head: str,
        ticket_id: str = TICKET_ID,
        fork_point: str = FORK_POINT,
        max_cycles: int = 5,
    ) -> Reconciled:
        return reconcile_for_init(
            existing,
            worktree=worktree,
            ticket_id=ticket_id,
            fork_point=fork_point,
            head_sha=head,
            max_cycles=max_cycles,
            loop_backend=LOOP_BACKEND,
            confirm=resolve_confirm_backend("none", LOOP_BACKEND),
            now=LATER,
        )

    def test_no_ledger_starts_fresh_from_the_arguments(self, tmp_path: Path) -> None:
        result = self._reconcile(None, tmp_path, head="s1", max_cycles=5)
        assert result.ledger == fresh_ledger(max_cycles=5, checkpoint_3a_sha="s1")
        assert (result.reset_reason, result.released) == (None, None)

    @pytest.mark.parametrize(
        ("ticket_id", "fork_point", "reason"),
        [
            ("T-2", FORK_POINT, "ticket_mismatch"),
            (TICKET_ID, "other", "fork_point_mismatch"),
        ],
    )
    def test_a_foreign_ledger_is_reset_without_consulting_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ticket_id: str,
        fork_point: str,
        reason: str,
    ) -> None:
        def _unexpected(*_args: object, **_kwargs: object) -> bool:
            pytest.fail("history_rewritten must not run for a foreign ledger")

        monkeypatch.setattr(
            "cw.review_convergence._release.history_rewritten", _unexpected
        )
        existing = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        result = self._reconcile(
            existing,
            tmp_path / "not-a-repo",
            head="s1",
            ticket_id=ticket_id,
            fork_point=fork_point,
        )
        assert result.reset_reason == reason
        assert result.ledger.rounds == []
        assert result.ledger.ticket_id == ticket_id
        assert result.ledger.fork_point == fork_point

    def test_an_owned_ledger_with_rewritten_history_is_reset(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("init-rewritten")
        commit_tracked_file(repo, "a.py")
        dropped = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "reset", "--hard", "HEAD~1")
        head = git_in(repo, "rev-parse", "HEAD")
        existing = ledger_with_rounds(_distinct(0, 1), shas=[dropped])
        result = self._reconcile(existing, repo, head=head)
        assert result.reset_reason == "history_rewritten"
        assert result.ledger.rounds == []
        assert result.released is None

    def test_an_owned_ledger_is_resumed_with_the_refreshed_cap(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("init-resumed")
        base = git_in(repo, "rev-parse", "HEAD")
        existing = ledger_with_rounds(_distinct(0, 1), shas=[base])
        result = self._reconcile(existing, repo, head=base, max_cycles=11)
        assert result.ledger == existing.model_copy(update={"max_cycles": 11})
        assert (result.reset_reason, result.released) == (None, None)


class TestWorktreeState:
    """``worktree_state`` (#2791 G4, moved from ``cw review confirm``)."""

    def test_clean_repo_returns_head_and_empty_status(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("state-clean")
        assert worktree_state(repo) == (git_in(repo, "rev-parse", "HEAD"), "")

    def test_a_file_write_changes_the_status(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("state-write")
        before = worktree_state(repo)
        (repo / "new.txt").write_text("x\n", encoding="utf-8")
        after = worktree_state(repo)
        assert after[0] == before[0]
        assert "?? new.txt" in after[1]

    def test_a_commit_changes_the_head(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("state-commit")
        before = worktree_state(repo)
        commit_tracked_file(repo, "a.py")
        after = worktree_state(repo)
        assert after[0] != before[0]
        assert after[1] == ""

    def test_a_git_failure_raises_cw_error(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with pytest.raises(CwError, match="Cannot read the git state"):
            worktree_state(not_a_repo)


class TestNextActionAndStatus:
    def test_fresh_ledger_starts_a_loop_round(self) -> None:
        assert next_action(fresh_ledger(), head_sha="s0") == "loop_round"

    def test_dirty_round_at_head_is_a_pending_fix_until_head_moves(self) -> None:
        ledger = ledger_with_rounds(_distinct(0, 1), shas=["s1"])
        assert next_action(ledger, head_sha="s1") == "fix"
        assert next_action(ledger, head_sha="s2") == "loop_round"
        summary = status_summary(ledger, head_sha="s1")
        assert summary["next_fix_cycle"] == ledger.fix_cycles_used
        assert status_summary(ledger, head_sha="s2")["next_fix_cycle"] == (
            ledger.fix_cycles_used + 1
        )

    def test_awaiting_confirm_routes_by_head(self) -> None:
        ledger = _awaiting("s1")
        assert next_action(ledger, head_sha="s1") == "confirm"
        assert next_action(ledger, head_sha="s2") == "loop_round"

    def test_done_and_stopped(self) -> None:
        assert next_action(confirmed_ledger(sha="s1"), head_sha="s1") == "done"
        assert next_action(confirmed_ledger(sha="s1"), head_sha="s2") == "loop_round"
        stopped = stopped_ledger("no_progress", sha="s1")
        assert next_action(stopped, head_sha="s1") == "stopped"
        assert next_action(stopped, head_sha="other") == "stopped"

    def test_status_summary_survives_a_session_boundary(self, tmp_path: Path) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        findings, unread = from_verdict(verdict)
        ledger, _ = record_round(
            fresh_ledger(checkpoint_3a_sha="cp3a"),
            kind="loop",
            backend="claude-native",
            reviewed_sha="s1",
            findings=findings,
            roster=["A", "B"],
            now=NOW,
            unread=unread,
            frozen=frozen_counts_from_verdict(verdict),
        )
        path = tmp_path / LEDGER_RELATIVE_PATH
        save_ledger(path, ledger)
        loaded = load_ledger(path)
        assert loaded is not None
        summary = status_summary(loaded, head_sha="s1")
        assert summary["checkpoint_3a_sha"] == "cp3a"
        assert summary["roster"] == ["A", "B"]
        assert summary["frozen"] == frozen_counts_from_verdict(verdict).model_dump()
        assert summary["next_action"] == "fix"
        assert summary["fix_cycles_used"] == 1
        assert summary["stop"] is None
        assert summary["awaiting_confirm_sha"] is None
        assert summary["releases"] == []
        assert summary["max_cycles"] == 8
        assert status_summary(fresh_ledger(), head_sha="s")["roster"] == []
        assert status_summary(fresh_ledger(), head_sha="s")["frozen"] is None

    def test_status_of_a_stopped_ledger_carries_the_stop_summary(self) -> None:
        stopped = stopped_ledger(
            "operator_action", sha="s1", stop_identities=("Do it",)
        )
        summary = status_summary(stopped, head_sha="s1")
        stop = summary["stop"]
        assert isinstance(stop, dict)
        assert stop["reason"] == "operator_action"
        assert stop["releasable"] is True
        assert summary["next_action"] == "stopped"


class TestRenderers:
    def test_stop_summary_is_none_unless_stopped(self) -> None:
        assert stop_summary(fresh_ledger()) is None

    @pytest.mark.parametrize(
        ("reason", "fragment"),
        [
            ("max_cycles", "raises review.max_cycles"),
            ("operator_action", "execute the operator checklist"),
            ("oscillation", "operator-settle' for every recurring finding"),
            ("no_progress", "terminal until a commit lands"),
        ],
    )
    def test_release_hints_are_fixed_per_reason(
        self, reason: str, fragment: str
    ) -> None:
        ledger = stopped_ledger(reason, sha="s1", max_cycles=2)
        summary = stop_summary(ledger)
        assert summary is not None
        assert fragment in str(summary["release_hint"])
        assert "cw dev-queue requeue <T> -c <C>" in str(summary["release_hint"])
        assert summary["reason"] == reason

    def test_unknown_stop_reason_gets_the_terminal_hint(self) -> None:
        ledger = stopped_ledger("no_progress", sha="s1").model_copy(
            update={"stop_reason": "mystery"}
        )
        summary = stop_summary(ledger)
        assert summary is not None
        assert "terminal until a commit lands" in str(summary["release_hint"])
        assert summary["releasable"] is False

    def test_settle_payload_matches_through_operator_settle(self) -> None:
        stopped = stopped_ledger("oscillation", sha="o", stop_identities=("A  b", "C"))
        payload = settle_payload(stopped)
        entries = payload["entries"]
        assert isinstance(entries, list)
        targets = [SettleTarget.model_validate(e) for e in entries]
        _, report = operator_settle(stopped, targets, actor="a", reason="r", now=NOW)
        assert report.unmatched == []
        assert report.releasable is True
        assert settle_payload(stopped_ledger("max_cycles", sha="m", max_cycles=2)) == {}

    def test_history_comment_sections_and_marker(self) -> None:
        stopped = stopped_ledger(
            "oscillation",
            sha="abcdef123456",
            stop_identities=("see <!-- injected --> and <b>bold</b>",),
        )
        settled, _ = operator_settle(
            stopped,
            _targets(stopped.stop_identities[0]),
            actor="matt",
            reason="reviewer <!-- x --> wrong",
            now=LATER,
        )
        released = _reconcile(settled, head="abcdef123456").ledger
        # Render the stop with the release audit trail the requeue left behind.
        with_failure = settled.model_copy(
            update={
                "releases": released.releases,
                "confirm_failures": record_confirm_failure(
                    _awaiting("s1"), cause="crash", detail="boom", now=NOW
                )[0].confirm_failures,
            }
        )
        comment = render_history_comment(with_failure, "oscillation")
        assert comment.startswith("## Review Convergence History")
        assert comment.rstrip().endswith("<!-- cw-agent-authored -->")
        assert "| Round | Kind |" in comment
        assert "### Deferred / rejected (audited)" in comment
        assert "settled by operator matt at 2026-10-27T13:00:00+00:00" in comment
        assert "### Stop releases" in comment
        assert "identities_settled" in comment
        assert "### Confirm backend failures" in comment
        assert "### Stopped: oscillation" in comment
        assert "cw review converge operator-settle" in comment
        # Marker syntax inside finding text is neutralised, in prose and payload.
        assert "<!-- injected" not in comment
        assert "<!-- x" not in comment
        block = comment.split("```json\n", 1)[1].split("\n```", 1)[0]
        assert "<" not in block
        assert json.loads(block) == settle_payload(with_failure)

    def test_history_comment_without_stop_or_extras_is_minimal(self) -> None:
        comment = render_history_comment(ledger_with_rounds(_distinct(0, 1)))
        assert "Deferred / rejected" not in comment
        assert "Stop releases" not in comment
        assert "Stopped:" not in comment
        assert "| 1 | loop |" in comment

    def test_stop_section_without_identities_prints_no_payload(self) -> None:
        stopped = stopped_ledger("no_progress", sha="np")
        comment = render_history_comment(stopped, "no_progress")
        assert "### Stopped: no_progress" in comment
        assert "```json" not in comment

    def test_friction_lines_exact_strings(self) -> None:
        ledger = ledger_with_rounds(
            [
                round_finding("MUST_FIX", "Fix me"),
                round_finding("SHOULD_FIX", "R", outcome="reject", rationale="no"),
            ],
            [],
            shas=["abcdef123456", "fedcba654321"],
        )
        assert friction_lines(ledger) == [
            "review_round_1: kind=loop backend=claude-native sha=abcdef1 "
            "new_accepted=1 settled=1",
            "review_round_2: kind=loop backend=claude-native sha=fedcba6 "
            "new_accepted=0 settled=0",
            "deferred_audited: 1 deferred/rejected finding(s) settled without "
            "re-review; see convergence history comment",
            "confirm_round_opt_out: review.confirm_backend=none",
        ]

    def test_friction_lines_operator_and_not_independent(self) -> None:
        ledger, _ = record(
            fresh_ledger(confirm="claude"),
            round_finding("SHOULD_FIX", "R", outcome="reject", rationale="no"),
            sha="s1",
        )
        settled, _ = operator_settle(
            ledger,
            _targets(ledger.settled[0].identity),
            actor="matt",
            reason="r",
            now=NOW,
        )
        lines = friction_lines(settled)
        assert (
            "operator_settled: 1 finding(s) settled by operator (matt); see "
            "convergence history comment"
        ) in lines
        assert (
            "confirm_not_independent: confirm_backend=claude equals loop backend "
            "claude-native; no independent round ran"
        ) in lines
        assert not any(line.startswith("confirm_round_opt_out") for line in lines)

    def test_friction_not_independent_names_the_codex_backends(self) -> None:
        """#2791: the text is derived from the ledger, not hard-coded to claude."""
        ledger, report = record(
            fresh_ledger(confirm="codex", loop_backend="codex"),
            sha="s1",
            backend="codex",
        )
        assert report.decision == "clean"
        assert (
            "confirm_not_independent: confirm_backend=codex equals loop backend "
            "codex; no independent round ran"
        ) in friction_lines(ledger)

    def test_sentinel_fragment_for_open_and_converged_ledgers(self) -> None:
        open_ledger = ledger_with_rounds(_distinct(0, 1))
        assert sentinel_review_fragment(open_ledger) == {
            "convergence_mode": "clean",
            "rounds_run": 1,
            "converged": False,
            "final_round_clean_sha": None,
            "confirm_backend": "none",
            "fix_cycles_used": 1,
        }
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        frozen = frozen_counts_from_verdict(verdict)
        ledger, _ = record_round(
            fresh_ledger(),
            kind="loop",
            backend="claude-native",
            reviewed_sha="s1",
            findings=[],
            roster=ROSTER,
            now=NOW,
            frozen=frozen,
        )
        fragment = sentinel_review_fragment(ledger)
        assert fragment["converged"] is True
        assert fragment["final_round_clean_sha"] == "s1"
        for key, value in frozen.model_dump().items():
            assert fragment[key] == value

    def test_history_comment_lists_adjudication_settled_entries(self) -> None:
        ledger = ledger_with_rounds(
            [
                round_finding("MUST_FIX", "Fix me"),
                round_finding(
                    "SHOULD_FIX", "Skip me", outcome="defer", rationale="out of scope"
                ),
            ]
        )
        comment = render_history_comment(ledger)
        assert (
            "- SHOULD_FIX `src/cw/foo.py`: Skip me (defer in round 1): out of scope"
            in (comment)
        )


class TestSharedHelpers:
    """Smoke tests for the CLI-facing helpers later test files import."""

    def test_confirm_verdict_payload_round_trips_the_verdict(self) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "x")])
        payload = confirm_verdict_payload(verdict, result_blocker_reason="r")
        assert payload["available"] is True
        assert payload["result_blocker_reason"] == "r"
        assert payload["reviewed_sha"] == verdict.reviewed_sha
        assert ReviewVerdict.model_validate(payload["verdict"]) == verdict

    def test_structurally_invalid_doc_has_no_reviewer_role(self) -> None:
        document = json.loads(structurally_invalid_doc_json("SHOULD_FIX"))
        assert "reviewer_role" not in document
        assert document["findings"][0]["severity"] == "SHOULD_FIX"

    def test_codex_audit_fixture_reads_the_captured_file(self) -> None:
        assert codex_audit_fixture("capacity_turn_failed.jsonl").strip()

    def test_write_ledger_persists_at_the_default_path(self, tmp_path: Path) -> None:
        ledger = ledger_with_rounds(_distinct(0, 1))
        path = write_ledger(tmp_path, ledger)
        assert path == tmp_path / LEDGER_RELATIVE_PATH
        assert load_ledger(path) == ledger

    def test_round_payload_validates_as_a_round_input(self) -> None:
        payload = round_payload("loop", "claude-native", "s1", round_finding())
        parsed = RoundInput.model_validate(payload)
        assert parsed.roster == ROSTER
        assert parsed.findings is not None
        assert len(parsed.findings) == 1

    def test_stopped_ledger_honours_fix_cycles_padding(self) -> None:
        ledger = stopped_ledger("operator_action", sha="s", fix_cycles_used=2)
        assert ledger.fix_cycles_used == 2
        assert stopped_ledger("no_progress", sha="s").stop_reason == "no_progress"

    def test_stopped_ledger_rejects_inconsistent_arguments(self) -> None:
        with pytest.raises(AssertionError):
            stopped_ledger("max_cycles", sha="s", fix_cycles_used=1, max_cycles=2)
        with pytest.raises(AssertionError):
            stopped_ledger("bogus", sha="s")

    def test_ledger_with_rounds_asserts_intermediate_rounds_continue(self) -> None:
        with pytest.raises(AssertionError):
            ledger_with_rounds([], [round_finding()])

    def test_warm_codex_capability_runs_one_pass(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("warm-capability")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "mod.py")
        warm_codex_capability(repo, _task())


class TestEdgeCases:
    @pytest.mark.parametrize("field", ["file", "summary"])
    def test_settle_target_rejects_blank_fields(self, field: str) -> None:
        values = {"severity": "MUST_FIX", "file": "a.py", "summary": "s", field: "  "}
        with pytest.raises(ValidationError, match="non-blank"):
            SettleTarget.model_validate(values)

    def test_settle_target_forbids_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            SettleTarget.model_validate(
                {"severity": "MUST_FIX", "file": "a", "summary": "s", "evidence": "e"}
            )

    def test_oscillation_stop_without_identities_is_never_releasable(self) -> None:
        stopped = stopped_ledger("oscillation", sha="o", stop_identities=("A",))
        bare = stopped.model_copy(update={"stop_identities": []})
        assert release_cause(bare) is None
