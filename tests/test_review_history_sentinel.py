"""Tests for ``cw.review_history.sentinel``: the sentinel path (#2790)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import (
    friction_lines,
    record_delta_round,
)
from cw.review_history import (
    _parse_friction_lines,
)
from tests._review_history_helpers import (
    ACME_SESSION,
    blocking_dispositions,
    derived_worktree,
    fixture_blocking_sentinel,
    fixture_converged,
    fixture_stopped,
    history_for,
    notes_with,
    review_task,
    sentinel_review,
    session_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests._review_history_helpers import LedgerFixture


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


@pytest.mark.parametrize(
    "build", [fixture_converged, fixture_stopped], ids=["converged", "stopped"]
)
def test_friction_lines_round_trip_through_the_parser(
    build: Callable[[], LedgerFixture],
) -> None:
    ledger = build().ledger

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


def test_friction_round_trip_parses_the_confirm_round_as_confirm() -> None:
    ledger = fixture_converged().ledger
    confirm = ledger.rounds[-1]
    assert confirm.kind == "confirm"

    views = _parse_friction_lines(friction_lines(ledger))

    assert [(v.round, v.kind, v.backend) for v in views if v.kind == "confirm"] == [
        (confirm.round, "confirm", "codex")
    ]
    assert [v.kind for v in views[:-1]] == ["loop"] * (len(views) - 1)


@pytest.mark.parametrize("kind", ["loop", "confirm", "delta"])
def test_friction_kinds_map_to_themselves(kind: str) -> None:
    """``delta`` (#2918) parses as itself; the mapping is a strict identity."""
    line = f"review_round_3: kind={kind} backend=codex sha=abc new_accepted=0 settled=0"

    views = _parse_friction_lines([line])

    assert [(v.round, v.kind) for v in views] == [(3, kind)]


def test_friction_round_trip_parses_a_delta_round_as_delta() -> None:
    ledger, _ = record_delta_round(
        fixture_converged().ledger,
        backend="codex",
        reviewed_sha="m" * 40,
        findings=[],
        roster=["Code Quality Reviewer"],
        now="2026-11-04T12:00:00+00:00",
    )

    views = _parse_friction_lines(friction_lines(ledger))

    assert views[-1].kind == "delta"
    assert views[-1].reviewed_sha == "m" * 7


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
    task = review_task(session_id=ACME_SESSION)
    session = session_for(task, ACME_SESSION, last_result=sentinel_review(review))

    history = history_for(task, tmp_path, sessions=[session])

    summary = history.summary
    assert summary.convergence_mode == "clean"
    assert (summary.rounds_run, summary.converged) == (3, False)
    assert (summary.final_clean_sha, summary.fix_cycles_used) == ("abc1234", 2)
    assert summary.confirm_backend == "codex"
    assert summary.confirm_failures is None
    assert history.sources == ["sentinel"]
    assert notes_with(history, "carries no review_round_<n> friction lines")


def test_sentinel_rounds_run_mismatch_keeps_the_ledger(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    task = review_task(session_id=ACME_SESSION)
    session = session_for(
        task,
        ACME_SESSION,
        last_result=sentinel_review({"convergence_mode": "clean", "rounds_run": 4}),
    )

    history = history_for(task, tmp_path, sessions=[session])

    assert history.summary.rounds_run == 5
    assert notes_with(history, "rounds_run=4")


def test_sentinel_rounds_run_agreeing_with_the_ledger_adds_no_note(
    tmp_path: Path,
) -> None:
    fixture_converged().write(derived_worktree(tmp_path))
    task = review_task(session_id=ACME_SESSION)
    session = session_for(
        task,
        ACME_SESSION,
        last_result=sentinel_review({"convergence_mode": "clean", "rounds_run": 5}),
    )

    history = history_for(task, tmp_path, sessions=[session])

    assert history.summary.rounds_run == 5
    assert not notes_with(history, "disagrees")


_ROUND_LINE = "review_round_1: kind=loop backend=b sha=a new_accepted=1 settled=0"


@pytest.mark.parametrize(
    "last_result",
    [
        pytest.param(
            {"review": "oops", "friction_highlights": "nope"}, id="non-container"
        ),
        pytest.param(
            {
                "review": {
                    "rounds_run": True,
                    "converged": "yes",
                    "final_round_clean_sha": 7,
                    "confirm_backend": 3,
                },
                "friction_highlights": [None, 3, _ROUND_LINE],
            },
            id="mistyped-fields",
        ),
    ],
)
def test_malformed_sentinel_degrades_to_a_message(
    tmp_path: Path, last_result: dict[str, object]
) -> None:
    task = review_task(session_id=ACME_SESSION)
    session = session_for(task, ACME_SESSION, last_result=last_result)

    history = history_for(task, tmp_path, sessions=[session])

    summary = history.summary
    assert summary.rounds_run is None
    assert summary.converged is None
    assert summary.final_clean_sha is None
    assert summary.confirm_backend is None
    if isinstance(last_result["review"], dict):
        assert [(r.round, r.source) for r in history.rounds] == [
            (1, "sentinel_friction")
        ]
    else:
        assert history.rounds == []
        assert notes_with(history, "no session AUTO_DEV_RESULT with a review block")


def test_blocking_sentinel_without_counts_has_no_rounds(tmp_path: Path) -> None:
    task = review_task(session_id=ACME_SESSION)
    session = session_for(
        task, ACME_SESSION, last_result=sentinel_review({"fix_cycles_used": 1})
    )

    history = history_for(task, tmp_path, sessions=[session])

    assert history.rounds == []
    assert history.summary.convergence_mode == "blocking"
    assert notes_with(history, "no must_fix_initial/should_fix")


def test_deferred_count_comes_from_the_ledger_when_one_was_read(
    tmp_path: Path,
) -> None:
    fixture_stopped().write(derived_worktree(tmp_path))

    summary = history_for(review_task(), tmp_path).summary

    assert summary.deferred
    assert summary.deferred_count == len(summary.deferred)


def test_deferred_count_comes_from_the_sentinel_without_a_ledger(
    tmp_path: Path,
) -> None:
    task = review_task(session_id=ACME_SESSION)
    session = session_for(task, ACME_SESSION, last_result=fixture_blocking_sentinel())

    summary = history_for(task, tmp_path, sessions=[session]).summary

    assert summary.deferred == []
    assert summary.deferred_count == 1


def test_deferred_count_is_null_when_nothing_records_it(tmp_path: Path) -> None:
    task = review_task(session_id=ACME_SESSION)
    session = session_for(
        task, ACME_SESSION, last_result=sentinel_review({"fix_cycles_used": 1})
    )

    assert (
        history_for(task, tmp_path, sessions=[session]).summary.deferred_count is None
    )
    assert history_for(review_task(), tmp_path).summary.deferred_count is None


def test_blocking_mode_degraded_view(tmp_path: Path) -> None:
    task = review_task(
        session_id=ACME_SESSION, finding_dispositions=blocking_dispositions()
    )
    session = session_for(task, ACME_SESSION, last_result=fixture_blocking_sentinel())

    history = history_for(task, tmp_path, sessions=[session])

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
    assert notes_with(history, "rounds_unrecorded")
    assert notes_with(history, "sentinel reports 1 deferred finding(s)")
    assert [(d.file, d.outcome) for d in history.dispositions] == [
        ("src/cw/bar.py", "REVERSED"),
        ("src/cw/foo.py", "REJECTED"),
    ]
    assert history.dispositions[1].rationale.startswith("false positive")
    assert history.dispositions[1].actor == "mattwwarren"
    assert history.sources == ["sentinel", "row_dispositions"]


def test_friction_regex_matches_every_friction_kind() -> None:
    from cw.review_history.sentinel import _FRICTION_KINDS, _FRICTION_RE

    assert _FRICTION_KINDS
    for kind in _FRICTION_KINDS:
        line = f"review_round_1: kind={kind} backend=b sha=s new_accepted=1 settled=0"
        match = _FRICTION_RE.match(line)
        assert match is not None, kind
        assert match["kind"] == kind


def test_friction_regex_rejects_a_kind_outside_the_mapping() -> None:
    from cw.review_history.sentinel import _FRICTION_RE

    line = "review_round_1: kind=initial backend=b sha=s new_accepted=1 settled=0"
    assert _FRICTION_RE.match(line) is None
