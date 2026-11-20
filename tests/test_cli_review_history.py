"""Tests for ``cw review history`` (#2790), through Click's ``CliRunner``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result
from freezegun import freeze_time

from cw.cli import main
from cw.cli.review.history import review_history as review_history_command
from cw.config import clients_file, dev_queue_file, state_file
from cw.events import inbox_path
from cw.models import QueueItemStatus, Stage
from cw.models.review_history import ReviewHistory
from cw.review_convergence import LEDGER_RELATIVE_PATH, record_delta_round
from tests._cli_review_helpers import _enter_operator_machine, _seed_review_row
from tests._review_convergence_helpers import write_ledger
from tests._review_history_helpers import (
    ACME_SESSION,
    DEFER_RATIONALE,
    FIX_CYCLE_STEPS,
    FIX_SESSION_COMPLETED,
    FIX_SESSION_STARTED,
    OTHER,
    OTHER_COMMENT,
    OTHER_FILE,
    OTHER_RATIONALE,
    OTHER_SESSION,
    OTHER_SUMMARY,
    REJECT_RATIONALE,
    REVIEW_START,
    ROW_CREATED,
    STAGE_DOC_PARAGRAPH,
    STAGE_TIE_SENTENCE,
    VOIDED_SUMMARY,
    LedgerFixture,
    Step,
    blocking_dispositions,
    clean_sentinel_session,
    fix_cycle_ledger,
    fix_session_for,
    fixture_awaiting_confirm,
    fixture_blocking_sentinel,
    fixture_converged,
    fixture_stopped,
    foreign_voided_event,
    note_foreign_transitions,
    sentinel_review,
    stage_entered_event,
    status_chain,
    timing_task,
    transition_event,
    write_corrupt_ledger,
    write_voided_ledger,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

_STAGE_COUNT = "1 stage.entered event(s) for ticket id 'ACM-1'"
_VOIDED_COUNT = "1 review.finding_voided event(s) for ticket id 'ACM-1'"


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enter_operator_machine(monkeypatch, tmp_path)
    install_acme_client(tmp_path, monkeypatch)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["review", "history", *args])


def _seed_with_ledger(tmp_path: Path, fixture_fn: object, **row: object) -> Path:
    assert callable(fixture_fn)
    worktree = tmp_path / "acme-wt"
    fixture_fn().write(worktree)
    _seed_review_row(ACME_TICKET, ACME, worktree_path=worktree, **row)
    return worktree


def test_converged_text(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_converged, status=QueueItemStatus.COMPLETED)

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert "review_history: ACM-1 (client acme)" in result.output
    assert "new_accepted_by_round: 5 3 2 0 0" in result.output
    assert "round 5 (confirm):" in result.output


def test_converged_json_matches_the_schema(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_converged)

    result = _invoke(ACME_TICKET, "--json")

    assert result.exit_code == 0, result.output
    history = ReviewHistory.model_validate_json(result.output)
    assert history.summary.rounds_run == 5
    assert set(json.loads(result.output)) == set(
        ReviewHistory.model_json_schema()["properties"]
    )


def _fixture_with_delta() -> LedgerFixture:
    """The converged fixture plus a clean cw-recorded delta round (#2980)."""
    converged = fixture_converged()
    ledger, _ = record_delta_round(
        converged.ledger,
        backend="codex",
        reviewed_sha="d" * 40,
        findings=[],
        roster=["Code Quality Reviewer"],
        now="2026-11-04T12:00:00+00:00",
    )
    return LedgerFixture(ledger=ledger, verdicts=converged.verdicts)


def test_a_delta_round_is_listed_as_delta_in_text(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, _fixture_with_delta)

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert "round 6 (delta):" in result.output


def test_a_delta_round_is_kind_delta_in_json(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, _fixture_with_delta)

    result = _invoke(ACME_TICKET, "--json")

    assert result.exit_code == 0, result.output
    history = ReviewHistory.model_validate_json(result.output)
    assert [(r.round, r.kind) for r in history.rounds][-1] == (6, "delta")


def _blocking_row() -> None:
    session = bound_session_for(
        make_task(session_id=ACME_SESSION), last_result=fixture_blocking_sentinel()
    )
    _seed_review_row(
        ACME_TICKET,
        ACME,
        ledger=blocking_dispositions(),
        session_id=ACME_SESSION,
        sessions=[session],
    )


def test_blocking_text_and_json() -> None:
    _blocking_row()

    text = _invoke(ACME_TICKET, "-c", ACME)
    as_json = _invoke(ACME_TICKET, "-c", ACME, "--json")

    assert text.exit_code == 0, text.output
    assert "mode: blocking" in text.output
    assert "round 1 (initial):" in text.output
    assert "REVERSED" in text.output
    history = ReviewHistory.model_validate_json(as_json.output)
    assert history.summary.convergence_mode == "blocking"
    assert len(history.dispositions) == 2


def test_in_flight_row_prints_rounds_so_far(tmp_path: Path) -> None:
    _seed_with_ledger(
        tmp_path, fixture_awaiting_confirm, status=QueueItemStatus.RUNNING
    )

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert "rounds so far" in result.output


def test_settled_and_deferred_rationale_in_text(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_stopped)

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert REJECT_RATIONALE in result.output
    assert DEFER_RATIONALE in result.output


def test_corrupt_ledger_exits_zero_with_a_note(tmp_path: Path) -> None:
    worktree = tmp_path / "acme-wt"
    path = worktree / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    _seed_review_row(ACME_TICKET, ACME, worktree_path=worktree)

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
    assert "Traceback" not in result.output


def test_no_data_exits_zero_with_a_clear_message() -> None:
    _seed_review_row(ACME_TICKET, ACME)

    result = _invoke(ACME_TICKET)

    assert result.exit_code == 0, result.output
    assert "No review data recorded for ticket 'ACM-1'" in result.output


@pytest.mark.parametrize("args", [(), ("-c", ACME)])
def test_unknown_ticket_exits_one(args: tuple[str, ...]) -> None:
    result = _invoke("NOPE-1", *args)

    assert result.exit_code == 1
    assert "No dev-queue task found" in result.output


def test_duplicate_rows_for_one_client_are_noted() -> None:
    old = make_task(stage=Stage.REVIEW, status=QueueItemStatus.COMPLETED)
    new = make_task(stage=Stage.REVIEW, status=QueueItemStatus.RUNNING)
    seed_queue([old, new, make_task(client=OTHER, stage=Stage.REVIEW)])

    text = _invoke(ACME_TICKET, "-c", ACME)
    as_json = _invoke(ACME_TICKET, "-c", ACME, "--json")

    note = (
        "1 other dev-queue row(s) for this ticket and client; showing the one "
        "`cw dev-queue show` selects"
    )
    assert text.exit_code == 0, text.output
    assert f"  - {note}" in text.output.splitlines()
    assert as_json.exit_code == 0, as_json.output
    history = ReviewHistory.model_validate_json(as_json.output)
    assert history.row_status == "running"
    assert note in history.notes


def test_single_row_has_no_duplicate_row_note() -> None:
    seed_queue([make_task(stage=Stage.REVIEW), make_task(client=OTHER)])

    text = _invoke(ACME_TICKET, "-c", ACME)
    as_json = _invoke(ACME_TICKET, "-c", ACME, "--json")

    assert text.exit_code == 0, text.output
    assert "other dev-queue row" not in text.output
    assert as_json.exit_code == 0, as_json.output
    notes = ReviewHistory.model_validate_json(as_json.output).notes
    assert not [n for n in notes if "other dev-queue row" in n]


def test_ticket_under_two_clients_needs_c() -> None:
    _seed_review_row(ACME_TICKET, ACME)
    _seed_review_row(ACME_TICKET, OTHER)

    result = _invoke(ACME_TICKET)

    assert result.exit_code == 1
    assert "acme" in result.output
    assert "other" in result.output


def test_another_clients_events_never_reach_the_output(tmp_path: Path) -> None:
    worktree = tmp_path / "acme-wt"
    write_voided_ledger(worktree)
    _seed_review_row(ACME_TICKET, ACME, worktree_path=worktree)
    other_session = bound_session_for(
        make_task(client=OTHER, session_id=OTHER_SESSION),
        worktree_path=tmp_path / "other-wt",
    )
    _seed_review_row(
        ACME_TICKET, OTHER, session_id=OTHER_SESSION, sessions=[other_session]
    )
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)
    foreign_voided_event(ACME_TICKET)

    text = _invoke(ACME_TICKET, "-c", ACME)
    as_json = _invoke(ACME_TICKET, "-c", ACME, "--json")

    for result in (text, as_json):
        assert result.exit_code == 0, result.output
        for secret in (OTHER_FILE, OTHER_SUMMARY, OTHER_RATIONALE, OTHER_COMMENT):
            assert secret not in result.output
        assert _STAGE_COUNT in result.output
        assert _VOIDED_COUNT in result.output
    assert VOIDED_SUMMARY in text.output


def test_worktree_override(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "elsewhere")
    _seed_review_row(ACME_TICKET, ACME)

    result = _invoke(
        ACME_TICKET, "-c", ACME, "--worktree", str(tmp_path / "elsewhere"), "--json"
    )

    assert result.exit_code == 0, result.output
    assert ReviewHistory.model_validate_json(result.output).summary.rounds_run == 5


def test_worktree_override_without_a_ledger_does_not_fall_back(
    tmp_path: Path,
) -> None:
    _seed_with_ledger(tmp_path, fixture_converged)
    empty = tmp_path / "empty"
    empty.mkdir()

    result = _invoke(ACME_TICKET, "-c", ACME, "--worktree", str(empty), "--json")

    assert result.exit_code == 0, result.output
    history = ReviewHistory.model_validate_json(result.output)
    assert history.rounds == []
    assert history.summary.rounds_run is None
    assert (
        f"no converge ledger found for this ticket (looked in: {empty})"
        in history.notes
    )


def test_worktree_override_does_not_bind_another_tickets_session(
    tmp_path: Path,
) -> None:
    fixture_converged().write(tmp_path / "elsewhere")
    foreign_review = {"convergence_mode": "clean", "rounds_run": 9}
    foreign = bound_session_for(
        make_task(ticket_id="ACM-2", session_id="sess-acm-2"),
        worktree_path=tmp_path / "elsewhere",
        last_result=sentinel_review(foreign_review),
    )
    _seed_review_row(ACME_TICKET, ACME, sessions=[foreign])
    stage_entered_event(ACME_TICKET, "sess-acm-2", REVIEW_START)

    result = _invoke(
        ACME_TICKET, "-c", ACME, "--worktree", str(tmp_path / "elsewhere"), "--json"
    )

    assert result.exit_code == 0, result.output
    history = ReviewHistory.model_validate_json(result.output)
    assert len(history.rounds) == 5
    assert history.rounds[0].wall_seconds is None
    assert "events" not in history.sources
    assert not any("rounds_run=9" in note for note in history.notes)
    assert any(_STAGE_COUNT in note for note in history.notes)


def test_history_is_read_only(tmp_path: Path) -> None:
    worktree = _seed_with_ledger(tmp_path, fixture_converged)
    stage_entered_event(ACME_TICKET, OTHER_SESSION, REVIEW_START)
    ledger = worktree / LEDGER_RELATIVE_PATH
    before = (
        ledger.read_bytes(),
        dev_queue_file().read_bytes(),
        inbox_path().read_bytes(),
    )

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert before == (
        ledger.read_bytes(),
        dev_queue_file().read_bytes(),
        inbox_path().read_bytes(),
    )


def test_review_help_lists_history() -> None:
    result = CliRunner().invoke(main, ["review", "--help"])

    assert result.exit_code == 0
    assert "history" in result.output


def test_unreadable_state_and_config_are_notes(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_converged)
    state_file().write_text("{not json", encoding="utf-8")
    clients_file().write_text("clients: [unclosed", encoding="utf-8")

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "session state unreadable" in result.output
    assert "is not configured or its config is unreadable" in result.output
    assert "round 5 (confirm):" in result.output


def test_sentinel_fallback_round_renders_the_unsplit_settled_total(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "acme-wt"
    write_corrupt_ledger(worktree)
    session = clean_sentinel_session(make_task(), worktree)
    _seed_review_row(ACME_TICKET, ACME, worktree_path=worktree, sessions=[session])

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "  source: sentinel_friction" in lines
    assert "  sha: abc1234" in lines
    assert "  settled: 1 (new/resurfaced split not recorded)" in lines
    assert "  wall_time: not recorded" in lines


_PRE_EXISTING_ROUND_KEYS = {
    "round",
    "kind",
    "source",
    "backend",
    "reviewed_sha",
    "roster",
    "new_accepted",
    "new_fix",
    "new_operator_action",
    "new_settled",
    "resurfaced_settled",
    "settled_total",
    "new_by_severity",
    "fix_dispatched",
    "clean",
    "recorded_at",
    "wall_seconds",
    "settled",
}
_PRE_EXISTING_SUMMARY_KEYS = {
    "convergence_mode",
    "rounds_run",
    "converged",
    "final_clean_sha",
    "ledger_status",
    "stop_reason",
    "stop_identities",
    "fix_cycles_used",
    "max_cycles",
    "confirm_backend",
    "new_accepted_series",
    "deferred",
    "deferred_count",
    "releases",
    "confirm_failures",
}
_STAGE_KEYS = {"queued", "reviewing", "repairing", "gate"}


def _seed_fix_cycle_row(tmp_path: Path) -> None:
    """The fix cycle's ledger, transition chain and FIX session, behind a real row."""
    worktree = tmp_path / "acme-wt"
    write_ledger(worktree, fix_cycle_ledger())
    stage_entered_event(ACME_TICKET, ACME_SESSION, "2026-10-27T11:50:00+00:00")
    status_chain(timing_task(), *FIX_CYCLE_STEPS)
    fix = fix_session_for(
        timing_task(),
        "sess-fix",
        FIX_SESSION_STARTED,
        FIX_SESSION_COMPLETED,
        worktree=worktree,
    )
    with freeze_time(ROW_CREATED):
        _seed_review_row(
            ACME_TICKET,
            ACME,
            worktree_path=worktree,
            status=QueueItemStatus.COMPLETED,
            session_id=ACME_SESSION,
            sessions=[fix],
        )


def test_stage_seconds_is_additive_in_json(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_converged)

    result = _invoke(ACME_TICKET, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "schema_version",
        "ticket_id",
        "client",
        "row_status",
        "row_stage",
        "in_flight",
        "rounds",
        "summary",
        "settled",
        "dispositions",
        "voided",
        "sources",
        "notes",
    }
    assert payload["schema_version"] == 1
    for view in payload["rounds"]:
        assert set(view) - {"stage_seconds"} == _PRE_EXISTING_ROUND_KEYS
        assert set(view["stage_seconds"]) == _STAGE_KEYS
    summary = payload["summary"]
    assert set(summary) - {"stage_seconds", "stage_seconds_as_of"} == (
        _PRE_EXISTING_SUMMARY_KEYS
    )
    assert set(summary["stage_seconds"]) == _STAGE_KEYS
    assert summary["stage_seconds_as_of"] is None


def test_stage_time_is_in_the_text_output(tmp_path: Path) -> None:
    _seed_with_ledger(tmp_path, fixture_converged)

    result = _invoke(ACME_TICKET, "-c", ACME)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("stage_time: ") for line in lines)
    assert sum(line.startswith("  stage_time: ") for line in lines) == 5


def test_the_stage_split_end_to_end_with_real_events_and_a_fix_session(
    tmp_path: Path,
) -> None:
    _seed_fix_cycle_row(tmp_path)

    as_json = _invoke(ACME_TICKET, "-c", ACME, "--json")
    text = _invoke(ACME_TICKET, "-c", ACME)

    assert as_json.exit_code == 0, as_json.output
    history = ReviewHistory.model_validate_json(as_json.output)
    assert history.rounds[1].stage_seconds.model_dump() == {
        "queued": 270.0,
        "reviewing": 1530.0,
        "repairing": 1200.0,
        "gate": 0.0,
    }
    assert history.summary.stage_seconds.model_dump() == {
        "queued": 2070.0,
        "reviewing": 4410.0,
        "repairing": 1200.0,
        "gate": 3720.0,
    }
    assert text.exit_code == 0, text.output
    assert (
        "stage_time: queued 34m30s, reviewing 1h13m30s, repairing 20m00s, gate 1h02m00s"
    ) in text.output.splitlines()


def test_a_newer_sibling_rows_events_are_not_this_rows(tmp_path: Path) -> None:
    """The selected live row ends where the next re-enqueued row begins."""
    selected = timing_task(
        status=QueueItemStatus.RUNNING,
        created_at=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
    )
    sibling = timing_task(
        status=QueueItemStatus.COMPLETED,
        created_at=datetime(2026, 10, 27, 14, 30, tzinfo=UTC),
    )
    seed_queue([selected, sibling])
    status_chain(
        selected,
        Step("2026-10-27T11:00:00+00:00", QueueItemStatus.PENDING),
        Step("2026-10-27T11:30:00+00:00", QueueItemStatus.RUNNING),
    )
    transition_event(sibling, QueueItemStatus.COMPLETED, "2026-10-27T14:40:00+00:00")

    result = _invoke(ACME_TICKET, "-c", ACME, "--json")

    assert result.exit_code == 0, result.output
    history = ReviewHistory.model_validate_json(result.output)
    assert history.row_status == "running"
    assert note_foreign_transitions(1) in history.notes
    assert not [n for n in history.notes if "the last task.transition event" in n]
    assert history.summary.stage_seconds.queued == 1800.0
    assert history.summary.stage_seconds.gate == 0.0
    assert history.summary.stage_seconds_as_of is not None


def test_the_help_documents_the_stage_time_and_the_tie() -> None:
    help_text = " ".join((review_history_command.help or "").split())

    assert " ".join(STAGE_DOC_PARAGRAPH.split()) in help_text
    assert " ".join(STAGE_TIE_SENTENCE.split()) in help_text
