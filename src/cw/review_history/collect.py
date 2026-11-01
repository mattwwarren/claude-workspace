"""Collection for :mod:`cw.review_history`: the only I/O (#2790).

Reads the converge ledger and its per-round verdict files, picks the session
sentinel from the state the caller passes in, and reads the event inbox. Every
data problem becomes a ``notes`` entry; nothing here raises on bad data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import read_events
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.models.enums import StageIdentifier
from cw.reconcile._shared import resolve_session_for_task
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    load_ledger,
    load_round_verdict,
    round_verdict_path,
)
from cw.review_history.inputs import HistoryInputs, _review_block
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorEvent,
        Session,
        TicketTask,
    )
    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import ReviewVerdict

_REVIEW_STARTED = StageIdentifier.S3_REVIEW_STARTED.value


def collect_history_inputs(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    state: CwState | None,
    *,
    worktree_override: Path | None = None,
    other_rows: int = 0,
    read_events_fn: Callable[..., list[OrchestratorEvent]] = read_events,
) -> HistoryInputs:
    """Read the ledger, verdict files, sentinel and events for *task*'s row.

    *client_cfg* is ``None`` for an unconfigured client (its derived worktree
    is then not tried); *state* is ``None`` when the session state could not
    be read. *worktree_override* replaces the automatic ledger lookup.
    *other_rows* counts the row's re-enqueued siblings, noted when above zero.
    Never raises on a data problem: each one becomes a note.
    """
    notes: list[str] = []
    if other_rows:
        notes.append(
            f"{other_rows} other dev-queue row(s) for this ticket and client; "
            "showing the one `cw dev-queue show` selects"
        )
    sessions: Sequence[Session] = [] if state is None else state.sessions
    if state is None:
        notes.append("session state unreadable; no session sentinel or event tie used")
    automatic = _automatic_candidates(task, client_cfg, state, notes)
    lookup = automatic if worktree_override is None else [worktree_override]
    ledger, ledger_path = _find_ledger(task, lookup, notes)
    verdicts: dict[int, ReviewVerdict] = {}
    missing: tuple[int, ...] = ()
    if ledger is not None and ledger_path is not None:
        verdicts, missing = _read_verdicts(ledger, ledger_path, notes)
    # The override only moves the ledger lookup; sessions bind to the row's own
    # worktrees, or an override at another ticket's checkout would pull that
    # ticket's sessions (and their review results and events) into this row.
    worktrees = frozenset(automatic)
    row_sessions = [
        s
        for s in sessions
        if s.client == task.client
        and s.worktree_path is not None
        and s.worktree_path in worktrees
    ]
    bound = {s.id for s in row_sessions}
    if task.session_id:
        bound.add(task.session_id)
    stage_events, voided_events = _read_row_events(task, read_events_fn, notes)
    return HistoryInputs(
        task=task,
        ledger=ledger,
        verdicts=verdicts,
        missing_verdict_rounds=missing,
        sentinel=_sentinel_of(task, sessions, row_sessions),
        bound_session_ids=frozenset(bound),
        stage_events=stage_events,
        voided_events=voided_events,
        notes=tuple(notes),
    )


def _automatic_candidates(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    state: CwState | None,
    notes: list[str],
) -> list[Path]:
    """Row worktree, bound session's worktree, then the derived path (deduped)."""
    candidates: list[Path] = []
    if task.worktree_path is not None:
        candidates.append(task.worktree_path)
    session = None if state is None else resolve_session_for_task(task, state)
    if session is not None and session.worktree_path is not None:
        candidates.append(session.worktree_path)
    if client_cfg is None:
        notes.append(
            f"client '{task.client}' is not configured or its config is "
            "unreadable; its derived worktree path was not tried"
        )
    else:
        branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
        candidates.append(worktree_path_for(client_cfg, branch))
    return list(dict.fromkeys(candidates))


def _find_ledger(
    task: TicketTask, candidates: Sequence[Path], notes: list[str]
) -> tuple[ConvergenceLedger | None, Path | None]:
    """The first readable ledger owned by *task*'s ticket, and its path."""
    if not candidates:
        notes.append("no worktree to look for a converge ledger in")
        return None, None
    for candidate in candidates:
        path = candidate / LEDGER_RELATIVE_PATH
        try:
            ledger = load_ledger(path)
        except (OSError, ValueError) as exc:
            notes.append(
                f"converge ledger at {path} unreadable ({type(exc).__name__}); skipped"
            )
            continue
        if ledger is None:
            continue
        if ledger.ticket_id != task.ticket_id:
            notes.append(
                f"converge ledger at {path} belongs to foreign ticket "
                f"'{ledger.ticket_id}'; skipped"
            )
            continue
        return ledger, path
    looked = ", ".join(str(c) for c in candidates)
    notes.append(f"no converge ledger found for this ticket (looked in: {looked})")
    return None, None


def _read_verdicts(
    ledger: ConvergenceLedger, ledger_path: Path, notes: list[str]
) -> tuple[dict[int, ReviewVerdict], tuple[int, ...]]:
    """Each recorded round's verdict file, plus the rounds that have none."""
    verdicts: dict[int, ReviewVerdict] = {}
    missing: list[int] = []
    for record in ledger.rounds:
        try:
            verdicts[record.round] = load_round_verdict(ledger_path, record.round)
        except FileNotFoundError:
            missing.append(record.round)
        except (OSError, ValueError) as exc:
            path = round_verdict_path(ledger_path, record.round)
            notes.append(
                f"round {record.round} verdict file {path} unreadable "
                f"({type(exc).__name__}); severity split not shown"
            )
    return verdicts, tuple(missing)


def _sentinel_of(
    task: TicketTask, sessions: Sequence[Session], row_sessions: Sequence[Session]
) -> Mapping[str, object] | None:
    """The bound session's result, else the newest row session's with a review."""
    if task.session_id:
        bound = next((s for s in sessions if s.id == task.session_id), None)
        if bound is not None and _review_block(bound.last_result) is not None:
            return bound.last_result
    reviewed = [s for s in row_sessions if _review_block(s.last_result) is not None]
    if not reviewed:
        return None
    return max(reviewed, key=lambda s: s.started_at).last_result


def _read_row_events(
    task: TicketTask,
    read_events_fn: Callable[..., list[OrchestratorEvent]],
    notes: list[str],
) -> tuple[tuple[OrchestratorEvent, ...], tuple[OrchestratorEvent, ...]]:
    """This ticket id's review-start and voided events, not yet attributed."""
    try:
        stage = read_events_fn(event_types=[OrchestratorEventType.STAGE_ENTERED])
        voided = read_events_fn(
            event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED]
        )
    except (CwError, OSError, ValueError) as exc:
        notes.append(
            f"event inbox unreadable ({type(exc).__name__}); round-1 wall time "
            "and voided findings not shown"
        )
        return (), ()
    return (
        tuple(
            e
            for e in stage
            if e.payload.get("stage") == _REVIEW_STARTED
            and e.payload.get("ticket_id") == task.ticket_id
        ),
        tuple(e for e in voided if e.correlation_id == task.ticket_id),
    )
