"""Candidates, evidence readers and acts of the unowned-running sweep (#3328).

Part B of #2615. :mod:`cw.reconcile.unowned_running` detects; this module
holds what both detect and the acts need, and the two acts that follow an
unbound RUNNING row that cannot be adopted:

- **NO_OWNER** (the only status mutation). The claiming process is gone
  (:func:`cw.launch_marker.observed_launch_owner_state`), the daemon roster is
  readable and homes no worker on the worktree, the claim context ties the
  row to no session, and the roster absence is not unproven. Under
  ``dev_queue_lock`` every liveness source detect consulted is re-read, then
  ``session.reap_proposed`` is recorded (ADR-0006 invariant 3, propose before
  act) and the row is parked BLOCKED_ON_USER (``signal_only`` or an unknown
  policy) or reverted PENDING (``auto``). No ``daemon.stop``, no worktree
  removal, no ``ever_spawned`` write; ``unproductive=False``, because a dead
  dispatcher is an infrastructure fault. ``auto`` additionally needs a
  non-empty in-lock roster (binding R13): ``claude agents --json`` cannot be
  re-read under the lock, so an empty roster proves nothing. At most
  ``UNOWNED_ACTS_MAX_PER_TICK`` acts per tick (rows that wait do not count),
  and a failure on one row is logged and retried next tick.
- **UNCONFIRMABLE** (signal only). One ``session.needs_attention`` page, then
  the ``advisory_note`` latch (page first, latch second, the #2524 rule). No
  status, ``session_id`` or lane-slot change.

Never imports :mod:`cw.reconcile.unowned_running`, which imports this module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from cw.config import load_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.launch_marker import LaunchOwnerState, observed_launch_owner_state
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    HOOK_CONTEXT_RELATIVE_PATH,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
)
from cw.queue_rows import _find_running_row, _is_backstop_exempt
from cw.reconcile._shared import ProposedAction
from cw.reconcile.tasks import _resolve_task_policy
from cw.reconcile.unowned_running_text import (
    CONTEXT_ATTEMPT_MISMATCH,
    CONTEXT_MISSING,
    CONTEXT_NO_ATTEMPT,
    CONTEXT_OTHER_CLAIM,
    CONTEXT_SESSION_ABSENT,
    NO_OWNER_PAGE_REASON,
    UNCONFIRMED_PAGE_REASON,
    claim_age_seconds,
    needs_attention_payload,
    no_owner_blocked_reason,
    no_owner_breadcrumbs,
    no_owner_page_session_id,
    reap_proposed_payload,
    unconfirmed_breadcrumbs,
    unconfirmed_note,
    unconfirmed_page_session_id,
)
from cw.worktree import _normalize_path, live_home_reason

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        LaunchMarker,
        OrchestratorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient, WorkerHome

_log = logging.getLogger(__name__)

# Most NO_OWNER acts per tick, oldest claim first; the rest wait for a later
# tick. It counts acts, not attempts: a candidate that waits or fails does not
# use a slot (#3462). The same figure as ``SESSION_LOST_PARKS_MAX_PER_TICK``,
# kept separate by design. Every candidate carries its own dead-launcher proof,
# so no mass-trip guard is needed.
UNOWNED_ACTS_MAX_PER_TICK = 3

# ``ContextTie.state`` for a context that ties the row to a session id.
CONTEXT_OK = "ok"
# ``ContextTie.state`` for a context file that exists but cannot be read.
CONTEXT_UNREADABLE = "unreadable_context"


class ContextTie(NamedTuple):
    """What a worktree's ``cw-context.json`` says about one row's claim.

    ``state`` is :data:`CONTEXT_OK`, :data:`CONTEXT_UNREADABLE` or one of the
    no-tie ``context_state`` values. ``attempt`` is the context's integer
    attempt, when it has one.
    """

    state: str
    session_id: str | None
    attempt: int | None


@dataclass(frozen=True)
class NoOwnerCandidate:
    """One unbound RUNNING row NO_OWNER may park or revert.

    ``created_at``, ``attempts``, ``claimed_at`` and ``launch_marker`` are the
    row's identity at detect time; ``homes_snapshot`` is the roster detect
    read. The act re-checks all of them under ``dev_queue_lock``.
    """

    ticket_id: str
    client: str
    lane: str
    created_at: datetime
    attempts: int
    claimed_at: datetime | None
    launch_marker: LaunchMarker
    worktree: Path
    context_state: str
    context_session_id: str | None
    context_clause: str
    homes_snapshot: frozenset[WorkerHome]


@dataclass(frozen=True)
class SignalCandidate:
    """One unbound RUNNING row whose ownership cannot be confirmed.

    ``evidence`` is the rendered ``{evidence}`` of the page; it may carry
    live counts, so it is page text only and never stored.
    """

    ticket_id: str
    client: str
    lane: str
    created_at: datetime
    attempts: int
    claimed_at: datetime | None
    cause: str
    evidence: str


def read_claim_context_state(worktree: Path) -> tuple[str, dict[str, object] | None]:
    """Read *worktree*'s ``cw-context.json``, keeping missing and unreadable apart.

    Attempts the read and never asks ``is_file()`` first (binding R11):
    ``FileNotFoundError`` is ``missing``; any other ``OSError`` (a directory,
    a permission error), any ``ValueError`` (malformed JSON, non-UTF-8 bytes)
    or a value that is not an object is ``unreadable_context``.
    """
    path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CONTEXT_MISSING, None
    except (OSError, ValueError):
        return CONTEXT_UNREADABLE, None
    try:
        value = json.loads(raw)
    except ValueError:
        return CONTEXT_UNREADABLE, None
    if not isinstance(value, dict):
        return CONTEXT_UNREADABLE, None
    return CONTEXT_OK, value


def classify_claim_context(context: dict[str, object], row: TicketTask) -> ContextTie:
    """Tie a readable *context* to *row*'s current claim, or say why not.

    ``bool`` is excluded from the attempt check (JSON ``true`` would equal
    attempt 1). A tying context without a string ``session_id`` reads as
    ``no_attempt``, keeping the five-value ``context_state`` contract.
    """
    attempt = context.get("attempt")
    if type(attempt) is not int:
        return ContextTie(CONTEXT_NO_ATTEMPT, None, None)
    if context.get("ticket_id") != row.ticket_id or context.get("client") != row.client:
        return ContextTie(CONTEXT_OTHER_CLAIM, None, attempt)
    if attempt != row.attempts:
        return ContextTie(CONTEXT_ATTEMPT_MISMATCH, None, attempt)
    session_id = context.get("session_id")
    if not isinstance(session_id, str):
        return ContextTie(CONTEXT_NO_ATTEMPT, None, attempt)
    return ContextTie(CONTEXT_OK, session_id, attempt)


def read_context_tie(worktree: Path, row: TicketTask) -> ContextTie:
    """:func:`read_claim_context_state` then :func:`classify_claim_context`."""
    state, context = read_claim_context_state(worktree)
    if context is None:
        return ContextTie(state, None, None)
    return classify_claim_context(context, row)


def non_native_backend_names(client: ClientConfig) -> list[str]:
    """Sorted, deduplicated executor backends of *client* that are not claude-native.

    A pure config check over the client pipeline and every effective lane's
    pipeline (a lane with no pipeline is skipped). ``cw.executor`` cannot be
    imported here (it imports ``cw.reconcile``), so this is a superset test: a
    client configuring any other backend anywhere is never called native.
    """
    executors = list(client.pipeline.executors.values())
    for lane in client.effective_lanes:
        if lane.pipeline is not None:
            executors.extend(lane.pipeline.executors.values())
    return sorted({e.backend for e in executors if e.backend != CLAUDE_NATIVE_BACKEND})


def worker_homed(
    worktree: Path,
    *,
    daemon: NativeDaemonClient,
    homes: Iterable[WorkerHome],
) -> bool:
    """True when a worker may be homed on *worktree*; any ``OSError`` is True.

    :func:`cw.worktree.live_home_reason` counts only workers recorded in a
    non-terminal session, so a raw roster ``cwd`` match is added for the
    unrecorded population (the leaked-worker sweep, #2670, owns stopping it).
    """
    if live_home_reason(worktree, daemon=daemon) is not None:
        return True
    try:
        target = _normalize_path(worktree)
        return any(_normalize_path(home.cwd) == target for home in homes)
    except OSError:
        return True


def _same_identity(
    row: TicketTask, *, attempts: int, claimed_at: datetime | None
) -> bool:
    return (
        row.session_id is None
        and row.attempts == attempts
        and row.claimed_at == claimed_at
        and not _is_backstop_exempt(row)
    )


def running_siblings(store: DevQueueStore, row: TicketTask) -> list[TicketTask]:
    """Every other RUNNING row of *store* for *row*'s (client, ticket_id).

    The one sibling predicate detect and the acts share (#3460).
    """
    return [
        other
        for other in store.tasks
        if other is not row
        and other.status is QueueItemStatus.RUNNING
        and other.client == row.client
        and other.ticket_id == row.ticket_id
    ]


def _context_unchanged(candidate: NoOwnerCandidate, row: TicketTask) -> bool:
    """Re-read the claim context: it must still tie nothing the same way."""
    tie = read_context_tie(candidate.worktree, row)
    if candidate.context_state != CONTEXT_SESSION_ABSENT:
        return tie.state == candidate.context_state
    if tie.state != CONTEXT_OK or tie.session_id != candidate.context_session_id:
        return False
    return all(s.id != tie.session_id for s in load_state().sessions)


def _roster_wait_reason(
    candidate: NoOwnerCandidate, *, daemon: NativeDaemonClient
) -> str | None:
    """Re-read the roster in-lock: it must equal detect's and home no worker."""
    homes = daemon.list_live_worker_homes()
    if homes is None:
        return "the daemon roster is unreadable"
    if homes != candidate.homes_snapshot:
        return "the daemon roster changed since detect"
    if worker_homed(candidate.worktree, daemon=daemon, homes=homes):
        return "a worker may be homed on the worktree"
    return None


def _in_lock_wait_reason(
    store: DevQueueStore,
    row: TicketTask,
    candidate: NoOwnerCandidate,
    *,
    daemon: NativeDaemonClient,
    clients: dict[str, ClientConfig],
) -> str | None:
    """Why the act must wait after the in-lock re-reads, or None to act.

    Re-reads every liveness source detect consulted (binding R13): the
    marker verdict, the roster and the worker-homed check, and the context.
    """
    if (
        row.client not in clients
        or not _same_identity(
            row, attempts=candidate.attempts, claimed_at=candidate.claimed_at
        )
        or row.launch_marker != candidate.launch_marker
        or running_siblings(store, row)
    ):
        return "the row changed since detect"
    if (
        observed_launch_owner_state(row.launch_marker)
        is not LaunchOwnerState.OWNER_GONE
    ):
        return "the claiming process is no longer provably gone"
    roster = _roster_wait_reason(candidate, daemon=daemon)
    if roster is not None:
        return roster
    if not _context_unchanged(candidate, row):
        return "the claim context changed since detect"
    return None


def _record_proposal(candidate: NoOwnerCandidate, *, auto: bool, now: datetime) -> bool:
    """Record ``session.reap_proposed`` in-lock; False (act skipped) on failure."""
    action = ProposedAction.REVERT_TASK if auto else ProposedAction.PARK_BLOCKED_ON_USER
    payload = reap_proposed_payload(
        client=candidate.client,
        ticket_id=candidate.ticket_id,
        lane=candidate.lane,
        proposed_action=action.value,
        worktree_path=str(candidate.worktree),
        context_state=candidate.context_state,
        launch_owner_pid=candidate.launch_marker.pid,
        claim_age_seconds=claim_age_seconds(candidate.claimed_at, now),
    )
    try:
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "unowned_running: %s/%s not acted on: the session.reap_proposed "
            "event was not recorded; retrying next tick",
            candidate.client,
            candidate.ticket_id,
            exc_info=True,
        )
        return False
    return True


def _mutate(row: TicketTask, candidate: NoOwnerCandidate, *, auto: bool) -> None:
    if auto:
        transition_task_status(row, QueueItemStatus.PENDING, unproductive=False)
        return
    transition_task_status(
        row,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=ReapReason.UNOWNED_RUNNING_ROW.value,
        blocked_reason=no_owner_blocked_reason(candidate.launch_marker.pid),
        pr_url=row.pr_url,
        unproductive=False,
    )


def _emit_page(payload: dict[str, object], *, ticket_id: str) -> bool:
    """Record one ``session.needs_attention``; a failed write is only logged."""
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "unowned_running: %s page for %s not written",
            payload.get("paused_status"),
            ticket_id,
            exc_info=True,
        )
        return False
    return True


def _page_no_owner(candidate: NoOwnerCandidate) -> None:
    _emit_page(
        needs_attention_payload(
            session_id=no_owner_page_session_id(
                candidate.client, candidate.ticket_id, candidate.attempts
            ),
            client=candidate.client,
            ticket_id=candidate.ticket_id,
            lane=candidate.lane,
            paused_status=NO_OWNER_PAGE_REASON,
            breadcrumbs=no_owner_breadcrumbs(
                client=candidate.client,
                ticket=candidate.ticket_id,
                wt=str(candidate.worktree),
                pid=candidate.launch_marker.pid,
                context=candidate.context_clause,
            ),
        ),
        ticket_id=candidate.ticket_id,
    )


def _park_or_revert(
    candidate: NoOwnerCandidate,
    *,
    now: datetime,
    daemon: NativeDaemonClient,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> bool | None:
    """The in-lock half of :func:`act_no_owner`.

    Returns ``None`` when the row was not acted on, else whether it was
    reverted (``auto``, True) or parked (False) and saved.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_running_row(
            store,
            candidate.ticket_id,
            candidate.client,
            created_at=candidate.created_at,
        )
        reason = (
            "the row is no longer RUNNING"
            if row is None
            else _in_lock_wait_reason(
                store, row, candidate, daemon=daemon, clients=clients
            )
        )
        if row is None or reason is not None:
            _log.info(
                "unowned_running: %s/%s not acted on: %s",
                candidate.client,
                candidate.ticket_id,
                reason,
            )
            return None
        policy = _resolve_task_policy(row.client, row.lane, clients, config)
        auto = policy is ReapPolicy.AUTO
        # The in-lock roster equals the snapshot here, so an empty snapshot is
        # an empty in-lock roster: unproven, never a revert (R13).
        if auto and not candidate.homes_snapshot:
            _log.info(
                "unowned_running: %s/%s not reverted: the daemon roster is empty, "
                "which cannot prove the worker absent (R13)",
                candidate.client,
                candidate.ticket_id,
            )
            return None
        if not _record_proposal(candidate, auto=auto, now=now):
            return None
        _mutate(row, candidate, auto=auto)
        save_dev_queue(store)
    return auto


def act_no_owner(
    candidate: NoOwnerCandidate,
    *,
    now: datetime,
    daemon: NativeDaemonClient,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> bool:
    """Park or revert the candidate's row; True iff the row was mutated and saved.

    *now* only feeds the informational ``evidence.claim_age_seconds``. A
    failure while parking or reverting one row (#3461) -- ``CwError`` from the
    transition, ``OSError`` from the queue write, ``ValueError`` (pydantic or
    JSON decode) from the queue read -- is logged and leaves the row RUNNING
    for the next tick, so it never aborts the sweep for the other candidates.
    """
    try:
        auto = _park_or_revert(
            candidate, now=now, daemon=daemon, clients=clients, config=config
        )
    except (CwError, OSError, ValueError) as exc:
        _log.warning(
            "unowned_running: %s/%s not acted on: %s; retrying next tick",
            candidate.client,
            candidate.ticket_id,
            exc,
            exc_info=True,
        )
        return False
    if auto is None:
        return False
    _log.info(
        "unowned_running: %s %s/%s (%s): the claiming process %d is gone",
        "reverted" if auto else "parked",
        candidate.client,
        candidate.ticket_id,
        ReapReason.UNOWNED_RUNNING_ROW.value,
        candidate.launch_marker.pid,
    )
    if not auto:
        _page_no_owner(candidate)
    return True


def _claim_sort_key(candidate: NoOwnerCandidate) -> float:
    """Oldest claim first; a missing or naive ``claimed_at`` sorts first."""
    claimed_at = candidate.claimed_at
    if claimed_at is None or claimed_at.utcoffset() is None:
        return float("-inf")
    return claimed_at.timestamp()


def run_no_owner_acts(
    candidates: list[NoOwnerCandidate],
    *,
    now: datetime,
    daemon: NativeDaemonClient,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> int:
    """Act on candidates oldest claim first, stopping after
    ``UNOWNED_ACTS_MAX_PER_TICK`` acts; return the acts done.

    The cap counts acts, not attempts (#3462): a candidate that waits, or
    whose act fails, does not take a slot, so it cannot starve a newer one.
    """
    ordered = sorted(candidates, key=_claim_sort_key)
    acted = 0
    for index, candidate in enumerate(ordered):
        if acted == UNOWNED_ACTS_MAX_PER_TICK:
            _log.info(
                "unowned_running: per-tick act cap reached; %d row(s) wait a tick",
                len(ordered) - index,
            )
            break
        acted += act_no_owner(
            candidate, now=now, daemon=daemon, clients=clients, config=config
        )
    return acted


def act_signal(candidate: SignalCandidate) -> bool:
    """Page once, then latch the note; True iff the page landed and the row latched.

    Page first, latch second: a failed page leaves the row unlatched so the
    next tick retries, and a row whose claim changed since detect is not
    latched. Only ``advisory_note`` is written.
    """
    payload = needs_attention_payload(
        session_id=unconfirmed_page_session_id(
            candidate.client, candidate.ticket_id, candidate.attempts, candidate.cause
        ),
        client=candidate.client,
        ticket_id=candidate.ticket_id,
        lane=candidate.lane,
        paused_status=UNCONFIRMED_PAGE_REASON,
        breadcrumbs=unconfirmed_breadcrumbs(
            client=candidate.client,
            ticket=candidate.ticket_id,
            claimed_at=candidate.claimed_at,
            evidence=candidate.evidence,
        ),
    )
    if not _emit_page(payload, ticket_id=candidate.ticket_id):
        return False
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_running_row(
            store,
            candidate.ticket_id,
            candidate.client,
            created_at=candidate.created_at,
        )
        if row is None or not _same_identity(
            row, attempts=candidate.attempts, claimed_at=candidate.claimed_at
        ):
            _log.info(
                "unowned_running: %s/%s changed after its %s page; not latched",
                candidate.client,
                candidate.ticket_id,
                candidate.cause,
            )
            return False
        row.advisory_note = unconfirmed_note(candidate.cause)
        save_dev_queue(store)
    return True
