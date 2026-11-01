"""Tests for ``cw.review_history.collect``: the only I/O (#2790, #2818).

Ledgers come from the real engine (``tests/_review_history_helpers.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.models import CwState
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
)
from cw.review_history import (
    build_review_history,
    collect_history_inputs,
)
from tests._review_history_helpers import (
    ACME_SESSION,
    OTHER,
    clean_sentinel_session,
    client_config,
    derived_worktree,
    fixture_converged,
    fixture_in_progress,
    history_for,
    notes_with,
    review_task,
    sentinel_review,
    session_for,
)

if TYPE_CHECKING:
    from cw.models import OrchestratorEvent, Session


def test_unreadable_event_inbox_is_a_note(tmp_path: Path) -> None:
    fixture_converged().write(derived_worktree(tmp_path))

    def _boom(**_kwargs: object) -> list[OrchestratorEvent]:
        msg = "inbox locked"
        raise CwError(msg)

    task = review_task()
    inputs = collect_history_inputs(
        task, client_config(tmp_path), CwState(), read_events_fn=_boom
    )
    history = build_review_history(inputs)

    assert notes_with(history, "event inbox unreadable")
    assert len(history.rounds) == 5


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00"])
def test_corrupt_ledger_falls_back_to_the_sentinel(
    tmp_path: Path, content: bytes
) -> None:
    path = derived_worktree(tmp_path) / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    task = review_task()

    history = history_for(
        task,
        tmp_path,
        sessions=[clean_sentinel_session(task, derived_worktree(tmp_path))],
    )

    assert notes_with(history, "unreadable")
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

    monkeypatch.setattr("cw.review_history.collect.load_ledger", _denied)

    history = history_for(review_task(), tmp_path)

    assert notes_with(history, "unreadable")
    assert history.rounds == []


def test_corrupt_ledger_without_sentinel_is_unknown_mode(tmp_path: Path) -> None:
    path = derived_worktree(tmp_path) / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    history = history_for(review_task(), tmp_path)

    assert history.summary.convergence_mode == "unknown"
    assert history.sources == []


def test_foreign_ticket_ledger_is_skipped(tmp_path: Path) -> None:
    fixture_converged(ticket="ACM-9").write(derived_worktree(tmp_path))

    history = history_for(review_task(), tmp_path)

    assert history.rounds == []
    assert notes_with(history, "foreign ticket 'ACM-9'")


def test_override_beats_the_row_worktree(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "override")
    fixture_in_progress().write(tmp_path / "row")

    history = history_for(
        review_task(worktree_path=tmp_path / "row"),
        tmp_path,
        worktree=tmp_path / "override",
    )

    assert len(history.rounds) == 5


def test_row_worktree_beats_the_bound_session(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "row")
    fixture_in_progress().write(tmp_path / "sess")
    task = review_task(worktree_path=tmp_path / "row", session_id=ACME_SESSION)

    history = history_for(
        task,
        tmp_path,
        sessions=[session_for(task, ACME_SESSION, worktree_path=tmp_path / "sess")],
    )

    assert len(history.rounds) == 5


def test_bound_session_beats_the_derived_path(tmp_path: Path) -> None:
    fixture_converged().write(tmp_path / "sess")
    fixture_in_progress().write(derived_worktree(tmp_path))
    task = review_task(session_id=ACME_SESSION)

    history = history_for(
        task,
        tmp_path,
        sessions=[session_for(task, ACME_SESSION, worktree_path=tmp_path / "sess")],
    )

    assert len(history.rounds) == 5


def test_first_ticket_matching_ledger_wins(tmp_path: Path) -> None:
    fixture_converged(ticket="ACM-9").write(tmp_path / "row")
    fixture_in_progress().write(derived_worktree(tmp_path))

    history = history_for(review_task(worktree_path=tmp_path / "row"), tmp_path)

    assert len(history.rounds) == 1
    assert notes_with(history, "foreign ticket")


def test_unconfigured_client_with_no_worktree_is_noted(tmp_path: Path) -> None:
    history = history_for(review_task(), tmp_path, configured=False)

    assert notes_with(history, "not configured or its config is unreadable")
    assert notes_with(history, "no worktree")


def test_newest_review_session_of_this_client_is_the_sentinel(tmp_path: Path) -> None:
    worktree = derived_worktree(tmp_path)
    task = review_task()

    def _with(
        sid: str, day: int, review: dict[str, object] | None, **kw: object
    ) -> Session:
        return session_for(
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

    history = history_for(task, tmp_path, sessions=sessions)

    assert history.summary.rounds_run == 2


def test_missing_everything_gives_an_empty_history(tmp_path: Path) -> None:
    history = history_for(review_task(), tmp_path)

    assert (history.rounds, history.settled, history.dispositions) == ([], [], [])
    assert (history.voided, history.sources) == ([], [])
    assert history.summary.convergence_mode == "unknown"
    assert notes_with(history, "no converge ledger")
    assert notes_with(history, "no session")


def test_unreadable_state_is_a_note(tmp_path: Path) -> None:
    inputs = collect_history_inputs(review_task(), client_config(tmp_path), None)

    assert notes_with(build_review_history(inputs), "session state unreadable")


def test_other_rows_are_noted_when_above_zero(tmp_path: Path) -> None:
    cfg = client_config(tmp_path)
    state = CwState(sessions=[])

    many = collect_history_inputs(review_task(), cfg, state, other_rows=2)
    none = collect_history_inputs(review_task(), cfg, state, other_rows=0)

    note = "2 other dev-queue row(s) for this ticket and client"
    assert [n for n in many.notes if n.startswith(note)]
    assert "showing the one `cw dev-queue show` selects" in many.notes[0]
    assert not [n for n in none.notes if "other dev-queue row(s)" in n]
