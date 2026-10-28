"""Tests for ``cw.review_history_render``: the ``cw review history`` text (#2790).

Each history is built from the real fixtures through the real builder, so a
renderer test cannot drift onto a shape the builder never produces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.models import CwState, QueueItemStatus, Stage
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_history import build_review_history, collect_history_inputs
from cw.review_history_render import render_review_history
from tests._review_history_helpers import (
    ACME_SESSION,
    DEFER_RATIONALE,
    OPERATOR_REASON,
    REJECT_RATIONALE,
    blocking_dispositions,
    client_config,
    derived_worktree,
    fixture_awaiting_confirm,
    fixture_blocking_sentinel,
    fixture_converged,
    fixture_stopped,
)
from tests._ticket_view_helpers import bound_session_for, make_task

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


def _task(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.COMPLETED,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def _blocking(tmp_path: Path) -> tuple[ReviewHistory, str]:
    task = _task(session_id=ACME_SESSION, finding_dispositions=blocking_dispositions())
    session = bound_session_for(task, last_result=fixture_blocking_sentinel())
    return _render(tmp_path, task, (session,))


def _assert_invariants(text: str) -> None:
    assert "|" not in text
    assert "---" not in text
    assert "None" not in text
    assert "\nnotes:" in text or text.startswith("notes:")


def test_converged_text(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    _, text = _render(tmp_path, _task())

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


def test_stopped_text_prints_each_rationale_once(tmp_path: Path) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    history, text = _render(tmp_path, _task())

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


def test_in_flight_text_says_rounds_so_far(tmp_path: Path) -> None:
    fixture_awaiting_confirm().write(derived_worktree(tmp_path))

    _, text = _render(tmp_path, _task(status=QueueItemStatus.RUNNING))

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
    assert "converged: unknown" in lines
    dispositions = lines[lines.index("dispositions:") + 1 :]
    assert dispositions[0].startswith("  - REVERSED src/cw/bar.py:")
    assert dispositions[1].startswith("  - REJECTED src/cw/foo.py:")
    assert any("rounds_unrecorded" in line for line in lines)
    _assert_invariants(text)


def test_empty_history_prints_one_clear_line_then_notes(tmp_path: Path) -> None:
    _, text = _render(tmp_path, _task())

    lines = text.splitlines()
    assert lines[0].startswith("No review data recorded for ticket 'ACM-1'")
    assert lines[1] == "notes:"
    assert all(line.startswith("  - ") for line in lines[2:])
    _assert_invariants(text)


def test_text_is_single_line_and_never_truncated(tmp_path: Path) -> None:
    long_rationale = "because " + "very " * 80 + "long"
    key = _disposition_key("src/cw/foo.py", "multi line") or ""
    task = _task(
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


@pytest.mark.parametrize("notes_present", [True, False])
def test_notes_block_is_always_printed(tmp_path: Path, notes_present: bool) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    history, _ = _render(tmp_path, _task())
    if not notes_present:
        history = history.model_copy(update={"notes": []})

    text = render_review_history(history)

    lines = text.splitlines()
    assert lines[-1] == ("notes: -" if not notes_present else lines[-1])
    assert ("notes:" in lines) is notes_present
