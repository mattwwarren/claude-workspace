"""Tests for ``cw.review_history.attribution``: wall time and attribution (#2790).

Voided events come from the real ``apply_voided_suppression`` and stage
events from the real ``record_event``; nothing here hand-types a payload
the producer would emit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.models.enums import StageIdentifier
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from tests._review_convergence_helpers import (
    write_ledger,
)
from tests._review_history_helpers import (
    ACME_SESSION,
    COMMENT_ID,
    ORIGINAL_RATIONALE,
    OTHER,
    OTHER_COMMENT,
    OTHER_FILE,
    OTHER_RATIONALE,
    OTHER_SESSION,
    OTHER_SUMMARY,
    REVIEW_START,
    STAGE_NOTE,
    STAMPS,
    VOIDED_AT,
    VOIDED_NOTE,
    VOIDED_SUMMARY,
    blocking_dispositions,
    derived_worktree,
    fixture_converged,
    fixture_in_progress,
    foreign_voided_event,
    history_for,
    notes_with,
    review_task,
    running_task,
    session_for,
    stage_entered_event,
    timed,
    write_voided_ledger,
)
from tests._ticket_view_helpers import ACME_TICKET

if TYPE_CHECKING:
    pass


def test_wall_time_is_the_delta_between_rounds(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    walls = [r.wall_seconds for r in history.rounds]
    assert walls == [None, 724.0, 476.0, 600.0, 300.0]
    assert notes_with(history, "round 1 wall time")


def test_naive_recorded_at_reads_as_utc(tmp_path: Path) -> None:
    ledger = timed(
        fixture_converged().ledger, "2026-10-27T12:00:00", "2026-10-27T12:12:04+00:00"
    )
    write_ledger(derived_worktree(tmp_path), ledger)

    history = history_for(review_task(), tmp_path)

    assert history.rounds[1].wall_seconds == 724.0


def test_unparseable_recorded_at_gives_null_wall_time(tmp_path: Path) -> None:
    fixture = fixture_in_progress()
    write_ledger(derived_worktree(tmp_path), timed(fixture.ledger, "not a time"))

    history = history_for(review_task(), tmp_path)

    assert history.rounds[0].wall_seconds is None


def test_stage_event_of_the_bound_session_sets_round_one(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0
    assert "events" in history.sources
    assert not notes_with(history, "stage.entered")


def test_stage_event_of_a_worktree_session_of_this_client_is_used(
    tmp_path: Path,
) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    stage_entered_event(ACME_TICKET, "sess-wt", REVIEW_START)
    task = review_task()

    history = history_for(
        task, tmp_path, sessions=[session_for(task, "sess-wt", worktree_path=worktree)]
    )

    assert history.rounds[0].wall_seconds == 600.0


def test_stage_event_of_another_clients_session_is_not_used(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    fixture_converged().write(worktree)
    stage_entered_event(ACME_TICKET, "sess-y", REVIEW_START)
    task = review_task()
    stranger = session_for(task, "sess-y", client=OTHER, worktree_path=worktree)

    history = history_for(task, tmp_path, sessions=[stranger])

    assert history.rounds[0].wall_seconds is None
    assert STAGE_NOTE in history.notes


@pytest.mark.parametrize("session_id", ["", None])
def test_stage_event_without_a_session_is_never_used(
    tmp_path: Path, session_id: str | None
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, session_id, REVIEW_START)

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None
    assert STAGE_NOTE in history.notes


def test_foreign_stage_event_is_counted(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None
    assert STAGE_NOTE in history.notes
    assert "events" not in history.sources


def test_nearer_foreign_event_does_not_beat_an_attributable_one(
    tmp_path: Path,
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)
    stage_entered_event(ACME_TICKET, OTHER_SESSION, "2026-10-27T11:58:00+00:00")

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0
    assert STAGE_NOTE in history.notes


def test_latest_attributable_start_before_round_one_wins(tmp_path: Path) -> None:
    """A re-run review records a second start; round 1 is timed from the latest."""
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, "2026-10-27T11:30:00+00:00")
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0


def test_stage_event_after_round_one_is_not_a_start(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, STAMPS[2])

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds is None


def test_a_different_stage_event_is_not_a_review_start(tmp_path: Path) -> None:
    """Only ``s3_review_started`` times round 1; other stages are not counted."""
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, ACME_SESSION, REVIEW_START)
    stage_entered_event(
        ACME_TICKET,
        ACME_SESSION,
        "2026-10-27T11:55:00+00:00",
        stage=StageIdentifier.S2_IMPL_COMPLETE.value,
    )

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert history.rounds[0].wall_seconds == 600.0
    assert not notes_with(history, "stage.entered")


def test_another_tickets_stage_event_is_ignored(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    stage_entered_event("ACM-2", OTHER_SESSION, REVIEW_START)

    history = history_for(running_task(session_id=ACME_SESSION), tmp_path)

    assert not notes_with(history, "stage.entered")


def test_voided_event_matched_through_the_ledger(tmp_path: Path) -> None:
    write_voided_ledger(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert len(history.voided) == 1
    voided = history.voided[0]
    assert (voided.severity, voided.file) == ("SHOULD_FIX", "src/cw/foo.py")
    assert voided.summary == VOIDED_SUMMARY
    assert (voided.operator_comment_id, voided.voided_at) == (COMMENT_ID, VOIDED_AT)
    assert voided.original_rationale == ORIGINAL_RATIONALE
    assert datetime.fromisoformat(voided.event_at).tzinfo is not None
    assert "events" in history.sources
    assert not notes_with(history, "review.finding_voided")


def test_voided_event_matched_through_a_row_disposition(tmp_path: Path) -> None:
    key = _disposition_key("src/cw/foo.py", "Some finding") or ""
    task = review_task(
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

    history = history_for(task, tmp_path)

    assert len(history.voided) == 1
    voided = history.voided[0]
    assert (voided.file, voided.summary, voided.severity) == (
        "src/cw/foo.py",
        "Some finding",
        "MUST_FIX",
    )
    assert voided.operator_comment_id == COMMENT_ID


def test_newline_wrapped_disposition_summary_still_attributes(tmp_path: Path) -> None:
    wrapped = "Some   finding\n  wrapped over\tlines"
    key = _disposition_key("src/cw/foo.py", wrapped) or ""
    task = review_task(
        finding_dispositions={
            key: FindingDisposition(
                outcome="REJECTED", rationale="nope", summary=wrapped
            )
        }
    )
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary="Some finding wrapped over lines",
        severity="MUST_FIX",
        operator_comment_id=COMMENT_ID,
    )

    history = history_for(task, tmp_path)

    assert [v.operator_comment_id for v in history.voided] == [COMMENT_ID]


def test_same_content_but_foreign_comment_id_is_not_shown(tmp_path: Path) -> None:
    write_voided_ledger(derived_worktree(tmp_path))
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary=VOIDED_SUMMARY,
        operator_comment_id=OTHER_COMMENT,
    )

    history = history_for(review_task(), tmp_path)

    assert [v.operator_comment_id for v in history.voided] == [COMMENT_ID]
    assert VOIDED_NOTE in history.notes


def test_same_content_and_comment_at_another_severity_is_not_shown(
    tmp_path: Path,
) -> None:
    write_voided_ledger(derived_worktree(tmp_path))
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary=VOIDED_SUMMARY,
        severity="MUST_FIX",
        operator_comment_id=COMMENT_ID,
    )

    history = history_for(review_task(), tmp_path)

    assert [(v.operator_comment_id, v.severity) for v in history.voided] == [
        (COMMENT_ID, "SHOULD_FIX")
    ]
    assert VOIDED_NOTE in history.notes


def test_same_content_and_comment_in_another_file_is_not_shown(
    tmp_path: Path,
) -> None:
    write_voided_ledger(derived_worktree(tmp_path))
    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/other.py",
        summary=VOIDED_SUMMARY,
        operator_comment_id=COMMENT_ID,
    )

    history = history_for(review_task(), tmp_path)

    assert [(v.operator_comment_id, v.file) for v in history.voided] == [
        (COMMENT_ID, "src/cw/foo.py")
    ]
    assert VOIDED_NOTE in history.notes


def test_voided_event_for_no_row_record_is_counted(tmp_path: Path) -> None:
    write_voided_ledger(derived_worktree(tmp_path))
    foreign_voided_event(ACME_TICKET)

    history = history_for(review_task(), tmp_path)

    assert len(history.voided) == 1
    assert VOIDED_NOTE in history.notes


def test_malformed_voided_payload_is_counted(tmp_path: Path) -> None:
    record_event(
        OrchestratorEventType.REVIEW_FINDING_VOIDED,
        {"file": 3},
        correlation_id=ACME_TICKET,
    )

    history = history_for(review_task(), tmp_path)

    assert history.voided == []
    assert VOIDED_NOTE in history.notes


def test_blocking_row_without_a_matching_disposition_counts_it(
    tmp_path: Path,
) -> None:
    foreign_voided_event(ACME_TICKET)

    history = history_for(
        review_task(finding_dispositions=blocking_dispositions()), tmp_path
    )

    assert history.voided == []
    assert VOIDED_NOTE in history.notes


@pytest.mark.parametrize(
    ("file", "summary"),
    [
        pytest.param("src/cw/other.py", "Some finding", id="foreign-file"),
        pytest.param("src/cw/foo.py", "Some other finding", id="foreign-summary"),
    ],
)
def test_disposition_attribution_needs_both_file_and_summary(
    tmp_path: Path, file: str, summary: str
) -> None:
    key = _disposition_key("src/cw/foo.py", "Some finding") or ""
    task = review_task(
        finding_dispositions={
            key: FindingDisposition(
                outcome="REJECTED", rationale="nope", summary="Some finding"
            )
        }
    )
    foreign_voided_event(
        ACME_TICKET,
        file=file,
        summary=summary,
        severity="MUST_FIX",
        operator_comment_id=OTHER_COMMENT,
    )

    foreign_only = history_for(task, tmp_path)

    assert foreign_only.voided == []
    assert VOIDED_NOTE in foreign_only.notes

    foreign_voided_event(
        ACME_TICKET,
        file="src/cw/foo.py",
        summary="Some finding",
        severity="MUST_FIX",
        operator_comment_id=COMMENT_ID,
    )

    with_control = history_for(task, tmp_path)

    assert [v.operator_comment_id for v in with_control.voided] == [COMMENT_ID]
    assert VOIDED_NOTE in with_control.notes


def test_another_tickets_voided_event_is_ignored(tmp_path: Path) -> None:
    foreign_voided_event("ACM-2")

    history = history_for(review_task(), tmp_path)

    assert not notes_with(history, "review.finding_voided")


def test_another_clients_events_never_leak(tmp_path: Path) -> None:
    write_voided_ledger(derived_worktree(tmp_path))
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)
    foreign_voided_event(ACME_TICKET)
    task = running_task(session_id=ACME_SESSION)
    other = session_for(
        task, OTHER_SESSION, client=OTHER, worktree_path=tmp_path / "other-wt"
    )

    history = history_for(task, tmp_path, sessions=[other])

    dumped = history.model_dump_json()
    for secret in (OTHER_FILE, OTHER_SUMMARY, OTHER_RATIONALE, OTHER_COMMENT):
        assert secret not in dumped
    assert history.rounds[0].wall_seconds is None
    assert STAGE_NOTE in history.notes
    assert VOIDED_NOTE in history.notes
