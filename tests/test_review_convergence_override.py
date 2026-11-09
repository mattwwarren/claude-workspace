"""Tests for the operator override of a dirty Claude confirm round (#2834).

Pure ledger transitions (no git, no clock): ``confirm_override_applies`` and
``override_confirm_round``. The tip rule, the next-action rule, demotion and a
later round are applied to the result to prove the override converges the
ledger exactly as a clean confirm would, and the renderer is checked for the
audited override.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import (
    ConfirmOverride,
    ConvergenceError,
    ConvergenceLedger,
    FindingIdentity,
    SettleTarget,
    demote_for_rereview,
    detect_no_progress,
    evaluate_tip,
    finalize_ready,
    finding_identity,
    friction_lines,
    next_action,
    operator_settle,
    override_confirm_round,
    record_delta_round,
    render_history_comment,
)
from cw.review_convergence import (
    confirm_override_applies as applies,
)
from tests._review_convergence_helpers import (
    FORK_POINT,
    NOW,
    ROSTER,
    confirmed_ledger,
    consolidated_verdict,
    dirty_confirm_ledger,
    fresh_ledger,
    ledger_with_rounds,
    record,
    round_finding,
    stopped_confirm_ledger,
    stopped_ledger,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import RoundFinding
    from cw.review_findings import ReviewVerdict

SHA = "a" * 40
LATER = "b" * 40
AT = "2026-11-08T12:00:00Z"
ACTOR = "octocat"
REASON = "accepted risk; follow-up #9999"
STOP_REASONS = ("max_cycles", "oscillation", "operator_action")


def _verdict() -> ReviewVerdict:
    return consolidated_verdict(
        accepted=[("MUST_FIX", "Leak one"), ("MUST_FIX", "Leak two")], sha=SHA
    )


def _ids(verdict: ReviewVerdict) -> list[FindingIdentity]:
    return [
        finding_identity("MUST_FIX", f.file, f.summary, fingerprint=f.fingerprint)
        for f in verdict.must_fix
    ]


def _in_progress(tmp_path: Path) -> tuple[ConvergenceLedger, list[FindingIdentity]]:
    verdict = _verdict()
    ledger = dirty_confirm_ledger(
        tmp_path, verdict, loop_sha=SHA, confirm_sha=SHA, fork_point=FORK_POINT
    )
    assert ledger.status == "in_progress"
    return ledger, _ids(verdict)


def _stop_findings(reason: str) -> list[RoundFinding]:
    if reason == "operator_action":
        return [
            round_finding(
                summary=s, outcome="operator_action", rationale="needs an operator"
            )
            for s in ("Leak one", "Leak two")
        ]
    return [round_finding(summary=s) for s in ("Leak one", "Leak two")]


def _stopped(reason: str) -> tuple[ConvergenceLedger, list[FindingIdentity]]:
    findings = _stop_findings(reason)
    ledger = stopped_confirm_ledger(reason, findings, sha=SHA)
    ids = [finding_identity(f.severity, f.file, f.summary) for f in findings]
    return ledger, ids


def _override(
    ledger: ConvergenceLedger,
    ids: list[FindingIdentity],
    *,
    actor: str = ACTOR,
) -> tuple[ConvergenceLedger, ConfirmOverride]:
    return override_confirm_round(
        ledger,
        ids,
        reviewed_sha=SHA,
        round_identities=ids,
        actor=actor,
        reason=REASON,
        now=AT,
    )


# -- applicability -----------------------------------------------------------


def test_applies_to_an_in_progress_dirty_confirm(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    assert applies(ledger, ids, reviewed_sha=SHA, round_identities=ids)


@pytest.mark.parametrize("reason", STOP_REASONS)
def test_applies_to_a_confirm_round_stopped_for(reason: str) -> None:
    ledger, ids = _stopped(reason)
    assert ledger.stop_reason == reason
    assert applies(ledger, ids, reviewed_sha=SHA, round_identities=ids)


def test_a_max_cycles_confirm_stop_records_no_identity_but_still_applies() -> None:
    """The regression the earlier ledger-memory guard would have refused."""
    ledger, ids = _stopped("max_cycles")
    assert ledger.stop_identities == []
    assert not {e.identity for e in ledger.fixed} & set(ids)
    assert applies(ledger, ids, reviewed_sha=SHA, round_identities=ids)


def _delta_last() -> ConvergenceLedger:
    converged = confirmed_ledger(sha="c" * 40, confirm="codex")
    ledger, _ = record_delta_round(
        converged,
        backend="codex",
        reviewed_sha=SHA,
        findings=[round_finding(summary="Leak one")],
        roster=ROSTER,
        now=NOW,
    )
    assert ledger.rounds[-1].kind == "delta"
    assert not ledger.rounds[-1].clean
    return ledger


def _awaiting_confirm() -> ConvergenceLedger:
    ledger, _ = record(
        fresh_ledger(confirm="claude", loop_backend="codex"), sha=SHA, backend="codex"
    )
    assert ledger.status == "awaiting_confirm"
    return ledger


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: confirmed_ledger(sha=SHA, confirm="claude"), id="converged"
        ),
        pytest.param(
            lambda: ledger_with_rounds([round_finding(summary="Leak one")], shas=[SHA]),
            id="loop-last-dirty",
        ),
        pytest.param(lambda: stopped_ledger("no_progress", sha=SHA), id="no-progress"),
        pytest.param(_delta_last, id="delta-last-dirty"),
        pytest.param(fresh_ledger, id="no-rounds"),
        pytest.param(_awaiting_confirm, id="awaiting-confirm"),
    ],
)
def test_does_not_apply_to(build: object) -> None:
    assert callable(build)
    ledger = build()
    assert isinstance(ledger, ConvergenceLedger)
    ids = [finding_identity("MUST_FIX", "src/cw/foo.py", "Leak one")]
    assert not applies(ledger, ids, reviewed_sha=SHA, round_identities=ids)


def test_does_not_apply_to_a_verdict_at_another_sha(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    assert not applies(ledger, ids, reviewed_sha=LATER, round_identities=ids)


def test_does_not_apply_without_identities(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    assert not applies(ledger, [], reviewed_sha=SHA, round_identities=ids)


def test_does_not_apply_to_an_identity_the_round_did_not_raise(
    tmp_path: Path,
) -> None:
    ledger, ids = _in_progress(tmp_path)
    stranger = finding_identity("MUST_FIX", "src/cw/foo.py", "Never raised")
    assert not applies(ledger, [*ids, stranger], reviewed_sha=SHA, round_identities=ids)


def test_does_not_apply_when_the_saved_round_has_an_uncovered_identity(
    tmp_path: Path,
) -> None:
    ledger, ids = _in_progress(tmp_path)
    extra = finding_identity("MUST_FIX", "src/cw/foo.py", "Also raised")
    assert not applies(ledger, ids, reviewed_sha=SHA, round_identities=[*ids, extra])


def test_override_raises_when_not_applicable() -> None:
    ledger = ledger_with_rounds([round_finding(summary="Leak one")], shas=[SHA])
    ids = [finding_identity("MUST_FIX", "src/cw/foo.py", "Leak one")]
    with pytest.raises(ConvergenceError, match="dirty Claude confirm round"):
        _override(ledger, ids)


# -- the transition ----------------------------------------------------------


def test_override_converges_an_in_progress_ledger(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    before = ledger.model_dump(mode="json")
    last = ledger.rounds[-1]

    updated, override = _override(ledger, ids)

    assert ledger.model_dump(mode="json") == before
    assert updated.status == "converged"
    assert updated.last_clean_sha == SHA
    assert updated.awaiting_confirm_sha is None
    assert updated.stop_reason is None
    assert updated.stop_identities == []
    assert updated.rounds[:-1] == ledger.rounds[:-1]
    assert updated.rounds[-1] == last.model_copy(update={"clean": True})
    assert updated.fixed == ledger.fixed
    assert updated.fix_cycles_used == ledger.fix_cycles_used
    assert updated.releases == ledger.releases
    assert updated.epoch_start_round == ledger.epoch_start_round
    assert updated.window_start_round == ledger.window_start_round
    assert updated.confirm_failures == ledger.confirm_failures
    new = updated.settled[len(ledger.settled) :]
    assert [e.identity for e in new] == ids
    for entry in new:
        assert entry.outcome == "defer"
        assert entry.origin == "operator"
        assert entry.round == last.round
        assert entry.actor == ACTOR
        assert entry.settled_at == AT
        assert entry.reviewed_sha == SHA
        assert entry.rationale == REASON
    assert override == ConfirmOverride(
        at=AT,
        round=last.round,
        reviewed_sha=SHA,
        actor=ACTOR,
        reason=REASON,
        prior_status="in_progress",
        prior_stop_reason=None,
        identities=ids,
    )
    assert updated.confirm_overrides == [override]


@pytest.mark.parametrize("reason", STOP_REASONS)
def test_override_converges_a_stopped_ledger_and_audits_the_stop(
    reason: str,
) -> None:
    ledger, ids = _stopped(reason)

    updated, override = _override(ledger, ids)

    assert updated.status == "converged"
    assert updated.stop_reason is None
    assert updated.stop_identities == []
    assert override.prior_status == "stopped"
    assert override.prior_stop_reason == reason


def test_override_keeps_one_settled_entry_per_identity_newest_wins() -> None:
    ledger, ids = _stopped("oscillation")
    target = ids[0]
    ledger, report = operator_settle(
        ledger,
        [
            SettleTarget(
                severity=target.severity,
                file=target.file,
                summary=target.summary_normalized,
            )
        ],
        actor="someone",
        reason="earlier settle",
        now="2026-11-01T00:00:00Z",
    )
    assert report.settled

    updated, _ = _override(ledger, [*ids, ids[0]])

    matching = [e for e in updated.settled if e.identity == target]
    assert len(matching) == 1
    assert matching[0].outcome == "defer"
    assert matching[0].actor == ACTOR
    assert matching[0].rationale == REASON
    assert updated.confirm_overrides[0].identities == ids


def test_an_empty_actor_is_stored_as_empty(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, override = _override(ledger, ids, actor="")
    assert override.actor == ""
    assert {e.actor for e in updated.settled if e.origin == "operator"} == {""}


def test_the_overridden_ledger_passes_the_tip_rules(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)
    assert finalize_ready(updated)
    assert evaluate_tip(updated, head_sha=SHA, origin_tip_sha=SHA).decision == (
        "accepted"
    )
    assert next_action(updated, head_sha=SHA) == "done"
    assert not detect_no_progress(updated.rounds)


# -- carry-over --------------------------------------------------------------


def test_the_override_carries_across_a_tolerated_default_merge(
    tmp_path: Path,
) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)
    verdict = evaluate_tip(
        updated, head_sha=LATER, origin_tip_sha=LATER, default_merge_only=True
    )
    assert verdict.decision == "accepted"
    assert next_action(updated, head_sha=LATER, default_merge_tolerated=True) == (
        "done"
    )
    strict = evaluate_tip(updated, head_sha=LATER, origin_tip_sha=LATER)
    assert strict.reason == "commits_after_clean_round"


def test_demotion_keeps_the_audit_and_settles_the_same_finding_later(
    tmp_path: Path,
) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)

    demoted = demote_for_rereview(updated)

    assert demoted.status == "in_progress"
    assert demoted.last_clean_sha == SHA
    assert demoted.confirm_overrides == updated.confirm_overrides
    assert demoted.settled == updated.settled
    reraised = round_finding(file=ids[0].file, summary=ids[0].summary_normalized)
    _, again = record(demoted, reraised, sha=LATER, backend="codex")
    assert again.new_accepted == 0
    assert again.resurfaced_settled == 1
    _, fresh = record(
        demoted,
        reraised,
        round_finding(summary="Brand new"),
        sha=LATER,
        backend="codex",
    )
    assert fresh.new_accepted == 1


# -- serialization -----------------------------------------------------------


def test_an_unoverridden_ledger_dumps_no_overrides_key(tmp_path: Path) -> None:
    ledger, _ = _in_progress(tmp_path)
    assert "confirm_overrides" not in json.loads(ledger.model_dump_json())


def test_an_overridden_ledger_round_trips(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)
    dumped = updated.model_dump_json()
    assert "confirm_overrides" in json.loads(dumped)
    assert ConvergenceLedger.model_validate_json(dumped) == updated


# -- rendering ---------------------------------------------------------------


def test_the_history_comment_shows_the_audited_override(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)

    comment = render_history_comment(updated)

    last = updated.rounds[-1].round
    assert f"| {last} | confirm |" in comment
    assert "| clean (operator override) |" in comment
    assert "### Operator override of a confirm round (audited)" in comment
    section = comment[comment.index("### Operator override") :]
    assert f"round {last} at {SHA[:7]}" in section
    assert f"operator {ACTOR} at {AT}" in section
    assert "was in_progress" in section
    assert REASON in section
    assert "Leak one" in section
    assert "Leak two" in section
    assert comment.index("### Deferred / rejected") < comment.index(
        "### Operator override"
    )


def test_the_override_section_names_the_stop_it_overrode() -> None:
    ledger, ids = _stopped("max_cycles")
    updated, _ = _override(ledger, ids)
    assert "was stopped, stop max_cycles" in render_history_comment(updated)


def test_override_text_is_neutralised_and_an_empty_actor_is_unknown(
    tmp_path: Path,
) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = override_confirm_round(
        ledger,
        ids,
        reviewed_sha=SHA,
        round_identities=ids,
        actor="",
        reason="ship <!-- cw-marker --> it",
        now=AT,
    )
    comment = render_history_comment(updated)
    section = comment[comment.index("### Operator override") :]
    assert "<!-- cw-marker -->" not in section
    assert "operator unknown at" in section
    lines = friction_lines(updated)
    overridden = [line for line in lines if line.startswith("confirm_overridden:")]
    assert overridden == [
        f"confirm_overridden: round={updated.rounds[-1].round} actor=unknown "
        f"sha={SHA[:7]} prior_status=in_progress"
    ]


def test_friction_lines_name_the_override(tmp_path: Path) -> None:
    ledger, ids = _in_progress(tmp_path)
    updated, _ = _override(ledger, ids)
    assert (
        f"confirm_overridden: round={updated.rounds[-1].round} actor={ACTOR} "
        f"sha={SHA[:7]} prior_status=in_progress"
    ) in friction_lines(updated)


def test_an_unoverridden_ledger_renders_unchanged(tmp_path: Path) -> None:
    ledger, _ = _in_progress(tmp_path)
    comment = render_history_comment(ledger)
    assert "operator override" not in comment
    assert "Operator override" not in comment
    assert not any(
        line.startswith("confirm_overridden") for line in friction_lines(ledger)
    )
