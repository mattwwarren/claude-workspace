"""Tests for ``cw.review_history.ledger``: rounds and settled entries (#2790, #2818).

Ledgers come from the real engine (``tests/_review_history_helpers.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    SettleTarget,
    demote_for_rereview,
    operator_settle,
)
from tests._review_convergence_helpers import (
    LOOP_BACKEND,
    NOW,
    ROSTER,
    consolidated_verdict,
    fresh_ledger,
    stamp_dispositions,
    write_ledger,
)
from tests._review_history_helpers import (
    CONFIRM_FAILURE_CAUSES,
    DEFER_RATIONALE,
    OPERATOR_REASON,
    REJECT_RATIONALE,
    derived_worktree,
    fixture_awaiting_confirm,
    fixture_confirm_failed,
    fixture_converged,
    fixture_in_progress,
    fixture_operator_action,
    fixture_stopped,
    fixture_zero_round,
    history_for,
    notes_with,
    record_verdict_round,
    review_task,
    running_task,
    write_round_verdict,
)
from tests._ticket_view_helpers import ACME_TICKET

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import TicketTask


def test_converged_rounds_summary_and_sources(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert [r.round for r in history.rounds] == [1, 2, 3, 4, 5]
    first, last = history.rounds[0], history.rounds[-1]
    assert (first.kind, first.source, first.backend) == ("loop", "ledger", LOOP_BACKEND)
    assert (first.reviewed_sha, first.roster, first.clean) == (
        "sha-0001",
        ROSTER,
        False,
    )
    assert (first.new_accepted, first.fix_dispatched, first.new_operator_action) == (
        5,
        5,
        0,
    )
    assert (last.kind, last.backend, last.clean) == ("confirm", "codex", True)
    summary = history.summary
    assert summary.convergence_mode == "clean"
    assert (summary.rounds_run, summary.converged) == (5, True)
    assert summary.final_clean_sha == "sha-0004"
    assert summary.new_accepted_series == [5, 3, 2, 0, 0]
    assert (summary.confirm_failures, summary.ledger_status) == (0, "converged")
    assert history.in_flight is False
    assert history.sources == ["ledger", "verdict_files"]
    assert (history.row_status, history.row_stage) == ("completed", "review")


def test_final_clean_sha_is_reported_only_while_converged(tmp_path: Path) -> None:
    """A converged ledger demoted for re-review keeps its sha but is not final."""
    fixture = fixture_converged()
    worktree = derived_worktree(tmp_path)
    write_ledger(worktree, fixture.ledger)
    converged = history_for(review_task(), tmp_path).summary
    assert (converged.converged, converged.final_clean_sha) == (True, "sha-0004")

    demoted = demote_for_rereview(fixture.ledger)
    assert demoted.last_clean_sha == "sha-0004"
    write_ledger(worktree, demoted)
    reopened = history_for(review_task(), tmp_path).summary

    assert reopened.converged is False
    assert reopened.ledger_status == "in_progress"
    assert reopened.final_clean_sha is None


def test_severity_split_from_verdict_files(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    splits = [
        None if r.new_by_severity is None else r.new_by_severity.model_dump()
        for r in history.rounds
    ]
    assert splits[0] == {"must_fix": 3, "should_fix": 2}
    assert splits[1] == {"must_fix": 1, "should_fix": 2}
    assert splits[3] == {"must_fix": 0, "should_fix": 0}


def test_disagreeing_verdict_file_gives_null_split_and_note(tmp_path: Path) -> None:
    fixture = fixture_converged()
    worktree = derived_worktree(tmp_path)
    fixture.write(worktree)
    write_round_verdict(worktree, 2, fixture.verdicts[1])

    history = history_for(review_task(), tmp_path)

    assert history.rounds[1].new_by_severity is None
    assert notes_with(history, "round 2: verdict file")


def test_missing_verdict_files_give_null_split_and_one_note(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path), with_verdicts=False)

    history = history_for(review_task(), tmp_path)

    assert all(r.new_by_severity is None for r in history.rounds)
    assert len(notes_with(history, "no per-round verdict file")) == 1
    assert history.sources == ["ledger"]


def test_corrupt_verdict_file_gives_null_split_and_note(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    path = write_round_verdict(worktree, 3, consolidated_verdict())
    path.write_text("{not json", encoding="utf-8")

    history = history_for(review_task(), tmp_path)

    assert history.rounds[2].new_by_severity is None
    assert notes_with(history, "round 3 verdict file")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the directory mode")
def test_unreadable_verdict_directory_is_a_note_not_a_traceback(
    tmp_path: Path,
) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    verdict_dir = (worktree / LEDGER_RELATIVE_PATH).with_suffix("")
    verdict_dir.chmod(0o000)
    try:
        history = history_for(review_task(), tmp_path)
    finally:
        verdict_dir.chmod(0o700)

    assert len(history.rounds) == 5
    assert all(r.new_by_severity is None for r in history.rounds)
    assert len(notes_with(history, "verdict file")) == 5
    assert notes_with(history, "unreadable (PermissionError)")
    assert not notes_with(history, "no per-round verdict file")


def test_undecided_verdict_file_gives_null_split_and_note(tmp_path: Path) -> None:
    fixture = fixture_converged()
    worktree = derived_worktree(tmp_path)
    fixture.write(worktree)
    dropped = stamp_dispositions(fixture.verdicts[1], {"r1 must 0": ("dropped", "")})
    write_round_verdict(worktree, 1, dropped)

    history = history_for(review_task(), tmp_path)

    assert history.rounds[0].new_by_severity is None
    assert notes_with(history, "undecided")


def test_findings_form_rounds_have_no_split(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert all(r.new_by_severity is None for r in history.rounds)
    assert notes_with(history, "no per-round verdict file")


def test_resurfaced_settled_finding_is_not_new(tmp_path: Path) -> None:
    ledger = fresh_ledger(ticket_id=ACME_TICKET)
    first = stamp_dispositions(
        consolidated_verdict(accepted=[("MUST_FIX", "a"), ("SHOULD_FIX", "b")]),
        {"b": ("rejected", "not a bug")},
    )
    ledger, _ = record_verdict_round(ledger, first, "sha-0001")
    second = consolidated_verdict(
        accepted=[("MUST_FIX", "c"), ("SHOULD_FIX", "b")], sha="sha-0002"
    )
    ledger, _ = record_verdict_round(ledger, second, "sha-0002")
    worktree = derived_worktree(tmp_path)
    write_ledger(worktree, ledger)
    write_round_verdict(worktree, 1, first)
    write_round_verdict(worktree, 2, second)

    history = history_for(review_task(), tmp_path)

    split = history.rounds[1].new_by_severity
    assert split is not None
    assert split.model_dump() == {"must_fix": 1, "should_fix": 0}
    assert history.rounds[1].resurfaced_settled == 1
    # round 1 settles ``b`` anew; round 2 only sees it again, settled
    assert (history.rounds[0].new_settled, history.rounds[0].resurfaced_settled) == (
        1,
        0,
    )
    assert history.rounds[0].settled_total == 1
    assert (history.rounds[1].new_settled, history.rounds[1].resurfaced_settled) == (
        0,
        1,
    )
    assert history.rounds[1].settled_total == 1


def test_fix_dispatched_excludes_operator_action_findings(tmp_path: Path) -> None:
    ledger = fixture_operator_action().ledger
    write_ledger(derived_worktree(tmp_path), ledger)

    history = history_for(review_task(), tmp_path)

    (round_one,) = history.rounds
    assert round_one.new_accepted == 3
    assert round_one.new_operator_action == 1
    assert round_one.fix_dispatched == ledger.rounds[0].new_fix == 2
    assert round_one.fix_dispatched != round_one.new_accepted


def test_confirm_failures_count_the_whole_life_of_the_ledger(tmp_path: Path) -> None:
    ledger = fixture_confirm_failed().ledger
    write_ledger(derived_worktree(tmp_path), ledger)

    history = history_for(running_task(), tmp_path)

    assert history.summary.ledger_status == "awaiting_confirm"
    assert history.summary.confirm_failures == len(CONFIRM_FAILURE_CAUSES) == 4
    assert len(history.rounds) == 2


def test_later_operator_settle_does_not_hide_the_rounds_own_finding(
    tmp_path: Path,
) -> None:
    """An operator settle is stamped ``round == len(rounds)``: the round's own fix
    finding, settled afterwards, must still count as new in that round's split."""
    verdict = consolidated_verdict(accepted=[("MUST_FIX", "a")])
    ledger, _ = record_verdict_round(
        fresh_ledger(ticket_id=ACME_TICKET), verdict, "sha-0001"
    )
    ledger, report = operator_settle(
        ledger,
        [SettleTarget(severity="MUST_FIX", file="src/cw/foo.py", summary="a")],
        actor="mattwwarren",
        reason=OPERATOR_REASON,
        now=NOW,
    )
    assert [e.round for e in report.settled] == [len(ledger.rounds)]
    worktree = derived_worktree(tmp_path)
    write_ledger(worktree, ledger)
    write_round_verdict(worktree, 1, verdict)

    history = history_for(review_task(), tmp_path)

    split = history.rounds[0].new_by_severity
    assert split is not None
    assert split.model_dump() == {"must_fix": 1, "should_fix": 0}
    assert not notes_with(history, "verdict file has")


def test_stopped_ledger_summary_and_releases(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    summary = history.summary
    assert (summary.ledger_status, summary.stop_reason) == ("stopped", "oscillation")
    assert summary.converged is False
    assert summary.stop_identities == ["MUST_FIX::src/cw/foo.py::Other bug"]
    assert len(summary.releases) == 1
    release = summary.releases[0]
    assert (release.cause, release.round, release.sha) == (
        "identities_settled",
        2,
        "sha-0002",
    )
    assert release.reason == "oscillation"


@pytest.mark.parametrize(
    "task_factory", [running_task, review_task], ids=["running_row", "completed_row"]
)
def test_stopped_ledger_is_not_in_flight(
    tmp_path: Path, task_factory: Callable[[], TicketTask]
) -> None:
    """``review_not_converging`` is a finished review: a stop is not a live loop."""
    fixture_stopped().write(derived_worktree(tmp_path))

    history = history_for(task_factory(), tmp_path)

    assert history.summary.ledger_status == "stopped"
    assert history.in_flight is False


def test_settled_entries_are_placed_once(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert [(s.summary, s.origin) for s in history.settled] == [
        ("False alarm", "adjudication"),
        ("Later thing", "adjudication"),
        ("Recurring defect", "operator"),
    ]
    round_one = history.rounds[0].settled
    assert [(s.outcome, s.rationale) for s in round_one] == [
        ("reject", REJECT_RATIONALE),
        ("defer", DEFER_RATIONALE),
    ]
    assert all(s.origin == "adjudication" for r in history.rounds for s in r.settled)
    assert history.summary.deferred == [
        s for s in history.settled if s.outcome == "defer"
    ]
    operator = history.settled[2]
    assert (operator.actor, operator.round) == ("mattwwarren", 2)
    assert operator.settled_at is not None


def test_settled_entry_with_no_round_view_gets_a_note(tmp_path: Path) -> None:
    ledger = fixture_stopped().ledger
    write_ledger(
        derived_worktree(tmp_path),
        ledger.model_copy(update={"rounds": ledger.rounds[1:]}),
    )

    history = history_for(review_task(), tmp_path)

    assert len(history.settled) == 3
    assert notes_with(history, "has no recorded round")


@pytest.mark.parametrize(
    ("fixture", "rounds"),
    [(fixture_awaiting_confirm, 2), (fixture_in_progress, 1), (fixture_zero_round, 0)],
)
def test_in_flight_ledgers_show_rounds_so_far(
    tmp_path: Path, fixture: object, rounds: int
) -> None:
    assert callable(fixture)
    fixture().write(derived_worktree(tmp_path))

    history = history_for(running_task(), tmp_path)

    assert history.in_flight is True
    assert len(history.rounds) == rounds
    assert bool(notes_with(history, "no recorded round")) is (rounds == 0)


def test_in_flight_without_a_ledger_reads_the_row(tmp_path: Path) -> None:
    assert history_for(running_task(), tmp_path).in_flight is True
    assert history_for(review_task(), tmp_path).in_flight is False


def test_in_flight_ledger_beats_a_running_row(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = history_for(running_task(), tmp_path)

    assert history.row_status == "running"
    assert history.in_flight is False


def test_in_flight_ledger_beats_a_completed_row(tmp_path: Path) -> None:
    fixture_in_progress().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert history.row_status == "completed"
    assert history.in_flight is True
