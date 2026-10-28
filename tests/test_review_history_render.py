"""Tests for ``cw.review_history_render``: the ``cw review history`` text (#2790).

Each history is built from the real fixtures through the real builder, so a
renderer test cannot drift onto a shape the builder never produces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.models import CwState, QueueItemStatus
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_history import build_review_history, collect_history_inputs
from cw.review_history_render import render_review_history
from tests._review_convergence_helpers import (
    write_ledger,
)
from tests._review_history_helpers import (
    ACME_SESSION,
    COMMENT_ID,
    DEFER_RATIONALE,
    OPERATOR_REASON,
    ORIGINAL_RATIONALE,
    REJECT_RATIONALE,
    VOIDED_AT,
    VOIDED_SUMMARY,
    blocking_dispositions,
    clean_sentinel_session,
    client_config,
    derived_worktree,
    fixture_awaiting_confirm,
    fixture_blocking_sentinel,
    fixture_converged,
    fixture_stopped,
    review_task,
    sentinel_review,
    write_corrupt_ledger,
    write_voided_ledger,
)
from tests._ticket_view_helpers import bound_session_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session, TicketTask
    from cw.models.review_history import ReviewHistory


def _render(
    tmp_path: Path, task: TicketTask, sessions: tuple[Session, ...] = ()
) -> tuple[ReviewHistory, str]:
    inputs = collect_history_inputs(
        task, client_config(tmp_path), CwState(sessions=list(sessions))
    )
    history = build_review_history(inputs)
    return history, render_review_history(history)


def _blocking(tmp_path: Path) -> tuple[ReviewHistory, str]:
    task = review_task(
        session_id=ACME_SESSION, finding_dispositions=blocking_dispositions()
    )
    session = bound_session_for(task, last_result=fixture_blocking_sentinel())
    return _render(tmp_path, task, (session,))


def _assert_invariants(text: str) -> None:
    assert "|" not in text
    assert "---" not in text
    assert "None" not in text
    assert "\nnotes:" in text or text.startswith("notes:")


def test_converged_text(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    _, text = _render(tmp_path, review_task())

    lines = text.splitlines()
    assert lines[0] == "review_history: ACM-1 (client acme)"
    assert "mode: clean" in lines
    assert "converged: yes" in lines
    assert "final_clean_sha: sha-0004" in lines
    assert "new_accepted_by_round: 5 3 2 0 0" in lines
    assert "deferred: 0" in lines
    assert "confirm_failures: 0" in lines
    assert "round 1 (loop):" in lines
    assert "  new_findings: 5 (MUST_FIX 3, SHOULD_FIX 2)" in lines
    assert "  wall_time: 12m04s" in lines
    assert "  wall_time: not recorded" in lines
    assert "round 5 (confirm):" in lines
    _assert_invariants(text)


def test_converged_header_and_round_block_keys_in_fixed_order(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    _, text = _render(tmp_path, review_task())

    lines = text.splitlines()
    assert lines[:13] == [
        "review_history: ACM-1 (client acme)",
        "state: row completed at review; ledger converged; finished",
        "mode: clean",
        "rounds: 5",
        "converged: yes",
        "final_clean_sha: sha-0004",
        "stop: -",
        "new_accepted_by_round: 5 3 2 0 0",
        "fix_cycles: 3 of 8",
        "confirm_backend: codex",
        "deferred: 0",
        "confirm_failures: 0",
        "sources: ledger, verdict_files",
    ]
    assert lines[13:23] == [
        "round 1 (loop):",
        "  source: ledger",
        "  backend: claude-native",
        "  sha: sha-0001",
        "  reviewers: Code Quality Reviewer, Test Reviewer",
        "  new_findings: 5 (MUST_FIX 3, SHOULD_FIX 2)",
        "  fix_dispatched: 5",
        "  settled: 0 new, 0 resurfaced",
        "  wall_time: not recorded",
        "  recorded_at: 2026-10-27T12:00:00+00:00",
    ]
    _assert_invariants(text)


def _voided_ledger_with_dispositions(tmp_path: Path) -> dict[str, FindingDisposition]:
    """A voided ledger finding plus two live row dispositions and a legacy one."""
    write_voided_ledger(derived_worktree(tmp_path))
    dispositions = blocking_dispositions()
    legacy_key = _disposition_key("src/cw/legacy.py", "no summary") or ""
    dispositions[legacy_key] = FindingDisposition(outcome="REJECTED", rationale="old")
    return dispositions


def test_voided_and_disposition_lines_carry_every_field(tmp_path: Path) -> None:
    dispositions = _voided_ledger_with_dispositions(tmp_path)

    history, text = _render(tmp_path, review_task(finding_dispositions=dispositions))

    lines = text.splitlines()
    event_at = history.voided[0].event_at
    voided = lines[lines.index("voided:") + 1]
    assert voided == (
        f"  - SHOULD_FIX src/cw/foo.py: {VOIDED_SUMMARY} -- original rejection: "
        f"{ORIGINAL_RATIONALE} (comment {COMMENT_ID}, voided_at {VOIDED_AT}, "
        f"event_at {event_at})"
    )
    assert event_at.endswith("+00:00")
    shown = lines[lines.index("dispositions:") + 1 : lines.index("voided:")]
    assert shown == [
        "  - REVERSED src/cw/bar.py: Settled, then withdrawn -- withdrawn: it was "
        "a real bug (actor mattwwarren, recorded_at 2026-10-26T11:00:00Z, sha "
        "abc1234)",
        "  - REJECTED src/cw/foo.py: Rejected by the operator -- false positive: "
        "covered by the caller (actor mattwwarren, recorded_at "
        "2026-10-26T10:00:00Z, sha abc1234)",
        "  - REJECTED src/cw/legacy.py: - -- old (actor -, recorded_at -, sha -)",
    ]
    _assert_invariants(text)


def test_stopped_text_prints_each_rationale_once(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history, text = _render(tmp_path, review_task())

    lines = text.splitlines()
    assert "stop: oscillation (MUST_FIX::src/cw/foo.py::Other bug)" in lines
    assert f"deferred: {len(history.summary.deferred)}" in lines
    assert "  new_findings: 2 (severity split not recorded)" in lines
    for rationale in (REJECT_RATIONALE, DEFER_RATIONALE, OPERATOR_REASON):
        assert text.count(rationale) == 1, rationale
    assert (
        f"    - reject SHOULD_FIX src/cw/foo.py: False alarm -- {REJECT_RATIONALE}"
        in lines
    )
    other = lines[lines.index("settled_other:") + 1]
    assert other.startswith("  - reject MUST_FIX src/cw/foo.py: Recurring defect")
    assert "operator" in other
    assert "mattwwarren" in other
    releases = lines[lines.index("releases:") + 1]
    assert "identities_settled" in releases
    _assert_invariants(text)


def test_settled_entries_with_no_round_block_render_under_settled_other(
    tmp_path: Path,
) -> None:
    ledger = fixture_stopped().ledger
    write_ledger(
        derived_worktree(tmp_path),
        ledger.model_copy(update={"rounds": ledger.rounds[1:]}),
    )

    _, text = _render(tmp_path, review_task())

    lines = text.splitlines()
    other = lines[lines.index("settled_other:") + 1 : lines.index("dispositions: -")]
    for summary, rationale in (
        ("False alarm", REJECT_RATIONALE),
        ("Later thing", DEFER_RATIONALE),
    ):
        assert text.count(rationale) == 1, rationale
        (line,) = (x for x in other if summary in x)
        assert rationale in line
        assert "adjudication" in line


def test_sentinel_friction_round_renders_the_unsplit_settled_total(
    tmp_path: Path,
) -> None:
    write_corrupt_ledger(derived_worktree(tmp_path))
    task = review_task()

    session = clean_sentinel_session(task, derived_worktree(tmp_path))
    _, text = _render(tmp_path, task, (session,))

    lines = text.splitlines()
    assert "  source: sentinel_friction" in lines
    assert "  sha: abc1234" in lines
    assert "  settled: 1 (new/resurfaced split not recorded)" in lines
    assert "  wall_time: not recorded" in lines
    _assert_invariants(text)


def test_in_flight_text_says_rounds_so_far(tmp_path: Path) -> None:
    fixture_awaiting_confirm().write(derived_worktree(tmp_path))

    _, text = _render(tmp_path, review_task(status=QueueItemStatus.RUNNING))

    assert "rounds so far" in text
    assert "converged: no" in text
    _assert_invariants(text)


def test_blocking_degraded_text(tmp_path: Path) -> None:
    _, text = _blocking(tmp_path)

    lines = text.splitlines()
    assert "mode: blocking" in lines
    assert "round 1 (initial):" in lines
    assert "  new_findings: 5 (MUST_FIX 3, SHOULD_FIX 2)" in lines
    assert "confirm_failures: not recorded" in lines
    assert "deferred: 1 (text not recorded)" in lines
    assert "converged: unknown" in lines
    dispositions = lines[lines.index("dispositions:") + 1 :]
    assert dispositions[0].startswith("  - REVERSED src/cw/bar.py:")
    assert dispositions[1].startswith("  - REJECTED src/cw/foo.py:")
    assert any("rounds_unrecorded" in line for line in lines)
    _assert_invariants(text)


def test_deferred_header_says_where_the_count_came_from(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))
    ledger_history, ledger_text = _render(tmp_path, review_task())
    _, blocking_text = _blocking(tmp_path / "blocking")
    task = review_task(session_id=ACME_SESSION)
    bare = bound_session_for(task, last_result=sentinel_review({"fix_cycles_used": 1}))
    _, bare_text = _render(tmp_path / "bare", task, (bare,))

    count = ledger_history.summary.deferred_count
    assert count
    assert f"deferred: {count}" in ledger_text.splitlines()
    assert "deferred: 1 (text not recorded)" in blocking_text.splitlines()
    assert "deferred: not recorded" in bare_text.splitlines()


def test_empty_history_prints_one_clear_line_then_notes(tmp_path: Path) -> None:
    _, text = _render(tmp_path, review_task())

    lines = text.splitlines()
    assert lines[0].startswith("No review data recorded for ticket 'ACM-1'")
    assert lines[1] == "notes:"
    assert all(line.startswith("  - ") for line in lines[2:])
    _assert_invariants(text)


def test_text_is_single_line_and_never_truncated(tmp_path: Path) -> None:
    long_rationale = "because " + "very " * 80 + "long"
    key = _disposition_key("src/cw/foo.py", "multi line") or ""
    task = review_task(
        finding_dispositions={
            key: FindingDisposition(
                outcome="REJECTED",
                rationale=f"first\n{long_rationale}",
                summary="multi\nline",
            )
        }
    )

    _, text = _render(tmp_path, task)

    line = next(x for x in text.splitlines() if "REJECTED" in x)
    assert "multi line" in line
    assert f"first {long_rationale}" in line
    _assert_invariants(text)


def test_unrecorded_values_and_long_walls_render(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))
    history, _ = _render(tmp_path, review_task())
    bare = history.rounds[0].model_copy(
        update={
            "new_accepted": None,
            "new_settled": None,
            "resurfaced_settled": None,
            "settled_total": None,
            "fix_dispatched": None,
            "wall_seconds": 3725.0,
        }
    )
    history = history.model_copy(
        update={
            "rounds": [
                bare,
                history.rounds[1].model_copy(update={"wall_seconds": -5.0}),
            ],
            "summary": history.summary.model_copy(update={"stop_identities": []}),
        }
    )

    lines = render_review_history(history).splitlines()

    assert "stop: oscillation" in lines
    assert "  new_findings: not recorded" in lines
    assert "  settled: not recorded" in lines
    assert "  fix_dispatched: not recorded" in lines
    assert "  wall_time: 1h02m05s" in lines
    assert "  wall_time: -0m05s" in lines


@pytest.mark.parametrize("notes_present", [True, False])
def test_notes_block_is_always_printed(tmp_path: Path, notes_present: bool) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    history, _ = _render(tmp_path, review_task())
    if not notes_present:
        history = history.model_copy(update={"notes": []})

    text = render_review_history(history)

    lines = text.splitlines()
    assert ("notes:" in lines) is notes_present
    if notes_present:
        assert lines[-1].startswith("  - ")
    else:
        assert lines[-1] == "notes: -"
