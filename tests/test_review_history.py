"""Tests for ``cw.review_history``: collecting and building a review's history (#2790).

Ledgers come from the real engine (``tests/_review_history_helpers.py``),
voided events from the real ``apply_voided_suppression``, and stage events
from the real ``record_event``; nothing here hand-types a ledger or a payload
the producer would emit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.events import record_event
from cw.exceptions import CwError
from cw.models import CwState, OrchestratorEventType, QueueItemStatus, Stage
from cw.models.review_history import ReviewHistory
from cw.review_adjudication import VoidedFinding
from cw.review_adjudication._voided import _voided_rationale
from cw.review_convergence import LEDGER_RELATIVE_PATH, friction_lines
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_history import (
    _VOIDED_RATIONALE_PREFIX,
    _parse_friction_lines,
    build_review_history,
    collect_history_inputs,
)
from tests._review_convergence_helpers import (
    LOOP_BACKEND,
    ROSTER,
    consolidated_verdict,
    fresh_ledger,
    stamp_dispositions,
    write_ledger,
)
from tests._review_history_helpers import (
    ACME_SESSION,
    COMMENT_ID,
    DEFER_RATIONALE,
    ORIGINAL_RATIONALE,
    OTHER,
    OTHER_COMMENT,
    OTHER_FILE,
    OTHER_RATIONALE,
    OTHER_SESSION,
    OTHER_SUMMARY,
    REJECT_RATIONALE,
    REVIEW_START,
    STAMPS,
    VOIDED_AT,
    VOIDED_SUMMARY,
    blocking_dispositions,
    clean_sentinel_session,
    client_config,
    derived_worktree,
    fixture_awaiting_confirm,
    fixture_blocking_sentinel,
    fixture_converged,
    fixture_in_progress,
    fixture_stopped,
    fixture_zero_round,
    foreign_voided_event,
    record_verdict_round,
    sentinel_review,
    stage_entered_event,
    timed,
    voided_suppression,
    write_round_verdict,
)
from tests._ticket_view_helpers import ACME_TICKET, bound_session_for, make_task

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import ClientConfig, OrchestratorEvent, Session, TicketTask

_RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "dispatch-runbook.md"
_STAGE_NOTE = (
    "1 stage.entered event(s) for ticket id 'ACM-1' could not be tied to this "
    "row's sessions; not used"
)
_VOIDED_NOTE = (
    "1 review.finding_voided event(s) for ticket id 'ACM-1' could not be tied "
    "to this row; not shown"
)


def _task(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.COMPLETED,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def _history(
    task: TicketTask,
    tmp_path: Path,
    *,
    sessions: Sequence[Session] = (),
    configured: bool = True,
    worktree: Path | None = None,
    client_cfg: ClientConfig | None = None,
) -> ReviewHistory:
    cfg = client_cfg or (client_config(tmp_path) if configured else None)
    inputs = collect_history_inputs(
        task, cfg, CwState(sessions=list(sessions)), worktree_override=worktree
    )
    return build_review_history(inputs)


def _session(task: TicketTask, sid: str, **overrides: object) -> Session:
    return bound_session_for(task, id=sid, **overrides)


def _notes_with(history: ReviewHistory, needle: str) -> list[str]:
    return [n for n in history.notes if needle in n]


# --- converged ledger --------------------------------------------------------


def test_converged_rounds_summary_and_sources(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

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


def test_severity_split_from_verdict_files(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

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

    history = _history(_task(), tmp_path)

    assert history.rounds[1].new_by_severity is None
    assert _notes_with(history, "round 2: verdict file")


def test_missing_verdict_files_give_null_split_and_one_note(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path), with_verdicts=False)

    history = _history(_task(), tmp_path)

    assert all(r.new_by_severity is None for r in history.rounds)
    assert len(_notes_with(history, "no per-round verdict file")) == 1
    assert history.sources == ["ledger"]


def test_corrupt_verdict_file_gives_null_split_and_note(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    path = write_round_verdict(worktree, 3, consolidated_verdict())
    path.write_text("{not json", encoding="utf-8")

    history = _history(_task(), tmp_path)

    assert history.rounds[2].new_by_severity is None
    assert _notes_with(history, "round 3 verdict file")


def test_undecided_verdict_file_gives_null_split_and_note(tmp_path: Path) -> None:
    fixture = fixture_converged()
    worktree = derived_worktree(tmp_path)
    fixture.write(worktree)
    dropped = stamp_dispositions(fixture.verdicts[1], {"r1 must 0": ("dropped", "")})
    write_round_verdict(worktree, 1, dropped)

    history = _history(_task(), tmp_path)

    assert history.rounds[0].new_by_severity is None
    assert _notes_with(history, "undecided")


def test_findings_form_rounds_have_no_split(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

    assert all(r.new_by_severity is None for r in history.rounds)
    assert _notes_with(history, "no per-round verdict file")


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

    history = _history(_task(), tmp_path)

    split = history.rounds[1].new_by_severity
    assert split is not None
    assert split.model_dump() == {"must_fix": 1, "should_fix": 0}
    assert history.rounds[1].resurfaced_settled == 1


# --- wall time ----------------------------------------------------------------


def test_wall_time_is_the_delta_between_rounds(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

    walls = [r.wall_seconds for r in history.rounds]
    assert walls == [None, 724.0, 476.0, 600.0, 300.0]
    assert _notes_with(history, "round 1 wall time")


def test_naive_recorded_at_reads_as_utc(tmp_path: Path) -> None:
    ledger = timed(
        fixture_converged().ledger, "2026-10-27T12:00:00", "2026-10-27T12:12:04+00:00"
    )
    write_ledger(derived_worktree(tmp_path), ledger)

    history = _history(_task(), tmp_path)

    assert history.rounds[1].wall_seconds == 724.0


def test_unparseable_recorded_at_gives_null_wall_time(tmp_path: Path) -> None:
    fixture = fixture_in_progress()
    write_ledger(derived_worktree(tmp_path), timed(fixture.ledger, "not a time"))

    history = _history(_task(), tmp_path)

    assert history.rounds[0].wall_seconds is None


def test_unreadable_event_inbox_is_a_note(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    def _boom(**_kwargs: object) -> list[OrchestratorEvent]:
        msg = "inbox locked"
        raise CwError(msg)

    task = _task()
    inputs = collect_history_inputs(
        task, client_config(tmp_path), CwState(), read_events_fn=_boom
    )
    history = build_review_history(inputs)

    assert _notes_with(history, "event inbox unreadable")
    assert len(history.rounds) == 5


# --- stage.entered attribution ------------------------------------------------


def _running(**overrides: object) -> TicketTask:
    return _task(status=QueueItemStatus.RUNNING, **overrides)


def test_stage_event_of_the_bound_session_sets_round_one(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0
    assert "events" in history.sources
    assert not _notes_with(history, "stage.entered")


def test_stage_event_of_a_worktree_session_of_this_client_is_used(
    tmp_path: Path,
) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    stage_entered_event(ACME_TICKET, "sess-wt", REVIEW_START)
    task = _task()

    history = _history(
        task, tmp_path, sessions=[_session(task, "sess-wt", worktree_path=worktree)]
    )

    assert history.rounds[0].wall_seconds == 600.0


def test_stage_event_of_another_clients_session_is_not_used(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    stage_entered_event(ACME_TICKET, "sess-y", REVIEW_START)
    task = _task()
    stranger = _session(task, "sess-y", client=OTHER, worktree_path=worktree)

    history = _history(task, tmp_path, sessions=[stranger])

    assert history.rounds[0].wall_seconds is None
    assert _STAGE_NOTE in history.notes


@pytest.mark.parametrize("session_id", ["", None])
def test_stage_event_without_a_session_is_never_used(
    tmp_path: Path, session_id: str | None
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, session_id, REVIEW_START)

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None
    assert _STAGE_NOTE in history.notes


def test_foreign_stage_event_is_counted(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None
    assert _STAGE_NOTE in history.notes
    assert "events" not in history.sources


def test_nearer_foreign_event_does_not_beat_an_attributable_one(
    tmp_path: Path,
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)
    stage_entered_event(ACME_TICKET, OTHER_SESSION, "2026-10-27T11:58:00+00:00")

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0
    assert _STAGE_NOTE in history.notes


def test_stage_event_after_round_one_is_not_a_start(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, STAMPS[2])

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None


def test_another_tickets_stage_event_is_ignored(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event("ACM-2", OTHER_SESSION, REVIEW_START)

    history = _history(_running(session_id=ACME_SESSION), tmp_path)

    assert not _notes_with(history, "stage.entered")


# --- review.finding_voided attribution ----------------------------------------


def _voided_ledger_worktree(tmp_path: Path) -> Path:
    """A one-round ledger whose SHOULD_FIX finding was voided by the operator."""
    verdict = consolidated_verdict(
        accepted=[("MUST_FIX", "Real bug"), ("SHOULD_FIX", VOIDED_SUMMARY)]
    )
    stamped = voided_suppression(ACME_TICKET, verdict)
    ledger, _ = record_verdict_round(
        fresh_ledger(ticket_id=ACME_TICKET), stamped, "sha-0001"
    )
    worktree = derived_worktree(tmp_path)
    write_ledger(worktree, ledger)
    return worktree


def test_voided_event_matched_through_the_ledger(tmp_path: Path) -> None:
    _voided_ledger_worktree(tmp_path)

    history = _history(_task(), tmp_path)

    assert len(history.voided) == 1
    voided = history.voided[0]
    assert (voided.severity, voided.file) == ("SHOULD_FIX", "src/cw/foo.py")
    assert voided.summary == VOIDED_SUMMARY
    assert (voided.operator_comment_id, voided.voided_at) == (COMMENT_ID, VOIDED_AT)
    assert voided.original_rationale == ORIGINAL_RATIONALE
    assert datetime.fromisoformat(voided.event_at).tzinfo is not None
    assert "events" in history.sources
    assert not _notes_with(history, "review.finding_voided")


def test_voided_event_matched_through_a_row_disposition(tmp_path: Path) -> None:
    key = _disposition_key("src/cw/foo.py", "Some finding") or ""
    task = _task(
        finding_dispositions={
            key: FindingDisposition(
                outcome="REJECTED", rationale="nope", summary="Some finding"
            )
        }
    )
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary="Some   finding",
        severity="MUST_FIX",
        operator_comment_id=COMMENT_ID,
    )

    history = _history(task, tmp_path)

    assert len(history.voided) == 1
    voided = history.voided[0]
    assert (voided.file, voided.summary, voided.severity) == (
        "src/cw/foo.py",
        "Some finding",
        "MUST_FIX",
    )
    assert voided.operator_comment_id == COMMENT_ID


def test_same_content_but_foreign_comment_id_is_not_shown(tmp_path: Path) -> None:
    _voided_ledger_worktree(tmp_path)
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary=VOIDED_SUMMARY,
        operator_comment_id=OTHER_COMMENT,
    )

    history = _history(_task(), tmp_path)

    assert [v.operator_comment_id for v in history.voided] == [COMMENT_ID]
    assert _VOIDED_NOTE in history.notes


def test_voided_event_for_no_row_record_is_counted(tmp_path: Path) -> None:
    _voided_ledger_worktree(tmp_path)
    foreign_voided_event(ACME_TICKET)

    history = _history(_task(), tmp_path)

    assert len(history.voided) == 1
    assert _VOIDED_NOTE in history.notes


def test_malformed_voided_payload_is_counted(tmp_path: Path) -> None:
    record_event(
        OrchestratorEventType.REVIEW_FINDING_VOIDED,
        {"file": 3},
        correlation_id=ACME_TICKET,
    )

    history = _history(_task(), tmp_path)

    assert history.voided == []
    assert _VOIDED_NOTE in history.notes


def test_blocking_row_without_a_matching_disposition_counts_it(
    tmp_path: Path,
) -> None:
    foreign_voided_event(ACME_TICKET)

    history = _history(_task(finding_dispositions=blocking_dispositions()), tmp_path)

    assert history.voided == []
    assert _VOIDED_NOTE in history.notes


def test_another_tickets_voided_event_is_ignored(tmp_path: Path) -> None:
    foreign_voided_event("ACM-2")

    history = _history(_task(), tmp_path)

    assert not _notes_with(history, "review.finding_voided")


def test_voided_rationale_prefix_pins_the_producer() -> None:
    void = VoidedFinding(
        severity="MUST_FIX",
        file="f.py",
        summary="s",
        evidence="e",
        operator_comment_id=COMMENT_ID,
    )

    assert _voided_rationale(void).startswith(_VOIDED_RATIONALE_PREFIX)


def test_another_clients_events_never_leak(tmp_path: Path) -> None:
    _voided_ledger_worktree(tmp_path)
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)
    foreign_voided_event(ACME_TICKET)
    task = _running(session_id=ACME_SESSION)
    other = _session(
        task, OTHER_SESSION, client=OTHER, worktree_path=tmp_path / "other-wt"
    )

    history = _history(task, tmp_path, sessions=[other])

    dumped = history.model_dump_json()
    for secret in (OTHER_FILE, OTHER_SUMMARY, OTHER_RATIONALE, OTHER_COMMENT):
        assert secret not in dumped
    assert history.rounds[0].wall_seconds is None
    assert _STAGE_NOTE in history.notes
    assert _VOIDED_NOTE in history.notes


# --- stopped ledger and settled placement --------------------------------------


def test_stopped_ledger_summary_and_releases(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

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


def test_settled_entries_are_placed_once(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

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

    history = _history(_task(), tmp_path)

    assert len(history.settled) == 3
    assert _notes_with(history, "has no recorded round")


# --- in flight ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "rounds"),
    [(fixture_awaiting_confirm, 2), (fixture_in_progress, 1), (fixture_zero_round, 0)],
)
def test_in_flight_ledgers_show_rounds_so_far(
    tmp_path: Path, fixture: object, rounds: int
) -> None:
    assert callable(fixture)
    fixture().write(derived_worktree(tmp_path))

    history = _history(_running(), tmp_path)

    assert history.in_flight is True
    assert len(history.rounds) == rounds
    assert bool(_notes_with(history, "no recorded round")) is (rounds == 0)


def test_in_flight_without_a_ledger_reads_the_row(tmp_path: Path) -> None:
    assert _history(_running(), tmp_path).in_flight is True
    assert _history(_task(), tmp_path).in_flight is False


def test_in_flight_ledger_beats_a_running_row(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = _history(_running(), tmp_path)

    assert history.row_status == "running"
    assert history.in_flight is False


def test_in_flight_ledger_beats_a_completed_row(tmp_path: Path) -> None:
    fixture_in_progress().write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

    assert history.row_status == "completed"
    assert history.in_flight is True


# --- ledger degradation ----------------------------------------------------------


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00"])
def test_corrupt_ledger_falls_back_to_the_sentinel(
    tmp_path: Path, content: bytes
) -> None:
    path = derived_worktree(tmp_path) / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    task = _task()

    history = _history(
        task,
        tmp_path,
        sessions=[clean_sentinel_session(task, derived_worktree(tmp_path))],
    )

    assert _notes_with(history, "unreadable")
    assert history.sources == ["sentinel"]
    assert [(r.round, r.source) for r in history.rounds] == [
        (1, "sentinel_friction"),
        (2, "sentinel_friction"),
    ]
    assert history.summary.convergence_mode == "clean"


def test_ledger_oserror_is_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    def _denied(_path: Path) -> None:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr("cw.review_history.load_ledger", _denied)

    history = _history(_task(), tmp_path)

    assert _notes_with(history, "unreadable")
    assert history.rounds == []


def test_corrupt_ledger_without_sentinel_is_unknown_mode(tmp_path: Path) -> None:
    path = derived_worktree(tmp_path) / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    history = _history(_task(), tmp_path)

    assert history.summary.convergence_mode == "unknown"
    assert history.sources == []


def test_foreign_ticket_ledger_is_skipped(tmp_path: Path) -> None:
    fixture_converged(ticket="ACM-9").write(derived_worktree(tmp_path))

    history = _history(_task(), tmp_path)

    assert history.rounds == []
    assert _notes_with(history, "foreign ticket 'ACM-9'")


# --- sentinel fallback ------------------------------------------------------------


def test_friction_line_parser() -> None:
    views = _parse_friction_lines(
        [
            "review_round_1: kind=loop backend=codex sha= new_accepted=2 settled=1",
            "garbage line",
            "review_round_2: kind=bogus backend=codex sha=a new_accepted=0 settled=0",
            "deferred_audited: 1 deferred/rejected finding(s)",
            "review_round_1: kind=loop backend=codex sha=dup new_accepted=9 settled=9",
        ]
    )

    assert len(views) == 1
    view = views[0]
    assert (view.backend, view.reviewed_sha, view.new_accepted) == ("codex", None, 2)
    assert (view.settled_total, view.clean, view.source) == (
        1,
        False,
        "sentinel_friction",
    )


def test_friction_lines_round_trip_through_the_parser() -> None:
    ledger = fixture_stopped().ledger

    views = _parse_friction_lines(friction_lines(ledger))

    assert [
        (v.round, v.kind, v.backend, v.reviewed_sha, v.new_accepted, v.settled_total)
        for v in views
    ] == [
        (
            r.round,
            r.kind,
            r.backend,
            r.reviewed_sha[:7],
            r.new_accepted,
            r.new_settled + r.resurfaced_settled,
        )
        for r in ledger.rounds
    ]


def test_sentinel_review_block_maps_to_the_summary(tmp_path: Path) -> None:
    review = {
        "convergence_mode": "clean",
        "rounds_run": 3,
        "converged": False,
        "final_round_clean_sha": "abc1234",
        "fix_cycles_used": 2,
        "confirm_backend": "codex",
        "must_fix_initial": 1,
        "should_fix": 0,
    }
    task = _task(session_id=ACME_SESSION)
    session = _session(task, ACME_SESSION, last_result=sentinel_review(review))

    history = _history(task, tmp_path, sessions=[session])

    summary = history.summary
    assert summary.convergence_mode == "clean"
    assert (summary.rounds_run, summary.converged) == (3, False)
    assert (summary.final_clean_sha, summary.fix_cycles_used) == ("abc1234", 2)
    assert summary.confirm_backend == "codex"
    assert summary.confirm_failures is None
    assert history.sources == ["sentinel"]


def test_sentinel_rounds_run_mismatch_keeps_the_ledger(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    task = _task(session_id=ACME_SESSION)
    session = _session(
        task,
        ACME_SESSION,
        last_result=sentinel_review({"convergence_mode": "clean", "rounds_run": 4}),
    )

    history = _history(task, tmp_path, sessions=[session])

    assert history.summary.rounds_run == 5
    assert _notes_with(history, "rounds_run=4")


# --- blocking mode ---------------------------------------------------------------


def test_blocking_sentinel_without_counts_has_no_rounds(tmp_path: Path) -> None:
    task = _task(session_id=ACME_SESSION)
    session = _session(
        task, ACME_SESSION, last_result=sentinel_review({"fix_cycles_used": 1})
    )

    history = _history(task, tmp_path, sessions=[session])

    assert history.rounds == []
    assert history.summary.convergence_mode == "blocking"
    assert _notes_with(history, "no must_fix_initial/should_fix")


def test_blocking_mode_degraded_view(tmp_path: Path) -> None:
    task = _task(session_id=ACME_SESSION, finding_dispositions=blocking_dispositions())
    session = _session(task, ACME_SESSION, last_result=fixture_blocking_sentinel())

    history = _history(task, tmp_path, sessions=[session])

    assert len(history.rounds) == 1
    initial = history.rounds[0]
    assert (initial.kind, initial.source, initial.new_accepted) == (
        "initial",
        "sentinel_cycle0",
        5,
    )
    assert initial.new_by_severity is not None
    assert initial.new_by_severity.model_dump() == {"must_fix": 3, "should_fix": 2}
    summary = history.summary
    assert (summary.convergence_mode, summary.fix_cycles_used) == ("blocking", 2)
    assert summary.confirm_failures is None
    assert _notes_with(history, "rounds_unrecorded")
    assert [(d.file, d.outcome) for d in history.dispositions] == [
        ("src/cw/bar.py", "REVERSED"),
        ("src/cw/foo.py", "REJECTED"),
    ]
    assert history.dispositions[1].rationale.startswith("false positive")
    assert history.dispositions[1].actor == "mattwwarren"
    assert history.sources == ["sentinel", "row_dispositions"]


# --- worktree and session resolution ----------------------------------------------


def test_override_beats_the_row_worktree(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "override")
    fixture_in_progress().write(tmp_path / "row")

    history = _history(
        _task(worktree_path=tmp_path / "row"), tmp_path, worktree=tmp_path / "override"
    )

    assert len(history.rounds) == 5


def test_row_worktree_beats_the_bound_session(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "row")
    fixture_in_progress().write(tmp_path / "sess")
    task = _task(worktree_path=tmp_path / "row", session_id=ACME_SESSION)

    history = _history(
        task,
        tmp_path,
        sessions=[_session(task, ACME_SESSION, worktree_path=tmp_path / "sess")],
    )

    assert len(history.rounds) == 5


def test_bound_session_beats_the_derived_path(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "sess")
    fixture_in_progress().write(derived_worktree(tmp_path))
    task = _task(session_id=ACME_SESSION)

    history = _history(
        task,
        tmp_path,
        sessions=[_session(task, ACME_SESSION, worktree_path=tmp_path / "sess")],
    )

    assert len(history.rounds) == 5


def test_first_ticket_matching_ledger_wins(tmp_path: Path) -> None:
    fixture_converged(ticket="ACM-9").write(tmp_path / "row")
    fixture_in_progress().write(derived_worktree(tmp_path))

    history = _history(_task(worktree_path=tmp_path / "row"), tmp_path)

    assert len(history.rounds) == 1
    assert _notes_with(history, "foreign ticket")


def test_unconfigured_client_with_no_worktree_is_noted(tmp_path: Path) -> None:
    history = _history(_task(), tmp_path, configured=False)

    assert _notes_with(history, "not configured")
    assert _notes_with(history, "no worktree")


def test_newest_review_session_of_this_client_is_the_sentinel(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    task = _task()

    def _with(
        sid: str, day: int, review: dict[str, object] | None, **kw: object
    ) -> Session:
        return _session(
            task,
            sid,
            worktree_path=worktree,
            started_at=datetime(2026, 10, day, tzinfo=UTC),
            last_result=sentinel_review(review),
            **kw,
        )

    sessions = [
        _with("old", 1, {"convergence_mode": "clean", "rounds_run": 1}),
        _with("new", 2, {"convergence_mode": "clean", "rounds_run": 2}),
        _with("no-review", 3, None),
        _with(
            "stranger", 4, {"convergence_mode": "clean", "rounds_run": 9}, client=OTHER
        ),
    ]

    history = _history(task, tmp_path, sessions=sessions)

    assert history.summary.rounds_run == 2


def test_missing_everything_gives_an_empty_history(tmp_path: Path) -> None:
    history = _history(_task(), tmp_path)

    assert (history.rounds, history.settled, history.dispositions) == ([], [], [])
    assert (history.voided, history.sources) == ([], [])
    assert history.summary.convergence_mode == "unknown"
    assert _notes_with(history, "no converge ledger")
    assert _notes_with(history, "no session")


def test_unreadable_state_is_a_note(tmp_path: Path) -> None:
    inputs = collect_history_inputs(_task(), client_config(tmp_path), None)

    assert _notes_with(build_review_history(inputs), "session state unreadable")


# --- docs guard -------------------------------------------------------------------


def test_runbook_documents_every_top_level_field() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index("### Reading a review's history")
    section = text[start : text.index("\n### ", start + 1)]

    for name in ReviewHistory.model_fields:
        assert f"`{name}`" in section, name
