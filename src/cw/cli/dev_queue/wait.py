"""``dev-queue wait`` — block until a ticket reaches terminal status.

Sentinel-aware wait loop plus its terminal/attention/timeout emit helpers.
Per-ticket state, the ``WAITING`` progress lines and the exit-5 stuck output
live in ``_wait_state`` (#2693).
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from typing import NoReturn

import click

from cw.auto_dev_result import (
    INTERMEDIATE_ADVANCE_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    AutoDevResult,
    BlockedResult,
)
from cw.cli._base import _complete_client, handle_errors
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.config import load_orchestrator_config, load_state
from cw.dev_queue import (
    _find_ticket,
    load_dev_queue,
    resolve_client,
    wait_for_terminal,
)
from cw.dev_queue.next_commands import is_human_kept_gate
from cw.exceptions import CwError
from cw.models import (
    QueueItemStatus,
    Session,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    _csid_from_transcript,
    _transcript_age_seconds,
)
from cw.reconcile._shared import (
    resolve_session_for_task,
    session_daemon_liveness,
)
from cw.result import reconstruct_staged_sentinel
from cw.stuck_pending import (
    HANDOFF_STUCK_SECONDS,
    SECONDS_PER_MINUTE,
    STUCK_PENDING_MINUTES,
    PendingBlock,
    PendingTracker,
    handoff_unrouted_block,
    is_internal_handoff,
    probe_pending,
)
from cw.ticket_view import ViewSources

from ._group import (
    _WAIT_EXIT_ATTENTION,
    _WAIT_EXIT_BLOCKED,
    _WAIT_EXIT_FAILED,
    _WAIT_EXIT_HUMAN_GATE,
    _WAIT_EXIT_STUCK,
    _WAIT_EXIT_TIMEOUT,
    dev_queue,
)
from ._wait_state import (
    TicketWait,
    emit_wait_stuck,
    format_handoff_line,
    format_waiting_line,
)

# Six hours: a long wait is the normal case now that internal hand-offs and
# self-clearing PENDING states keep waiting (#2693). 0 means no limit.
_WAIT_DEFAULT_TIMEOUT: int = 21600

# Poll interval for the sentinel-aware wait loop (seconds).
_WAIT_SENTINEL_POLL_INTERVAL: float = 5.0

# Transcript-staleness threshold for the roster-absence ATTENTION check
# (_check_stale_attention). Purely a reporting threshold for this CLI's exit
# code — it never dispositions the session. Local since the idle-watchdog
# budget resolver was removed with the process-kill timeouts; the check's
# real evidence is roster absence, staleness just debounces it.
_WAIT_STALE_ATTENTION_SECONDS: int = 900

# Exit-code mapping from AutoDevResult.status to wait exit codes. Keys must
# cover every schema.Status value except INTERMEDIATE_ADVANCE_STATUSES (those
# never reach _handle_sentinel_terminal — the ticket advances to its next
# stage, so the wait loop keeps polling). Guarded by
# test_wait_status_exit_covers_status_vocabulary; a new Status value must be
# mapped here (or added to INTERMEDIATE_ADVANCE_STATUSES) before it ships.
# "validation_failed"/"failed" were removed as dead keys: they are
# Blocker.reason values, not Status values, and sentinel.status is typed to
# the Status Literal so they could never match.
_WAIT_STATUS_EXIT: dict[str, int] = {
    "shipped": 0,
    "no_op": 0,
    "blocked": _WAIT_EXIT_BLOCKED,
    "ambiguities_pending_resolution": _WAIT_EXIT_BLOCKED,
    "premises_pending_verification": _WAIT_EXIT_BLOCKED,
    "merge_gate_blocked": _WAIT_EXIT_BLOCKED,
    # PR created, awaiting CI/merge — dispatch routes the task to
    # BLOCKED_ON_USER (#899), so the sentinel path must agree with the
    # queue-status path instead of misreporting FAILED.
    "merge_pending": _WAIT_EXIT_BLOCKED,
    "scope_exceeded": _WAIT_EXIT_FAILED,
    "forbidden_area": _WAIT_EXIT_FAILED,
    # #1870: an empty branch needs the orchestrator to push commits or close the
    # ticket, so it maps to BLOCKED (matching the dispatch gate's BLOCKED_ON_USER park)
    # rather than FAILED — nothing errored, there is simply nothing there.
    "empty_diff_blocked": _WAIT_EXIT_BLOCKED,
    # #1862: this ticket already has an open, unmerged PR from an earlier
    # dispatch. The orchestrator lands or closes it before the ticket can move,
    # so it maps to BLOCKED (matching the dispatch gate's BLOCKED_ON_USER
    # park) rather than FAILED — nothing errored, the work is already in
    # review.
    "stale_dispatch": _WAIT_EXIT_BLOCKED,
}

# Statuses that are a dispatch hand-off, never a wait exit (#2693): the wait
# keeps polling a RUNNING row that carries one until dispatch routes it (the
# row then settles BLOCKED_ON_USER, or advances), and exits 5 only if that has
# not happened after HANDOFF_STUCK_SECONDS. The drift test unions this set
# with _WAIT_STATUS_EXIT and INTERMEDIATE_ADVANCE_STATUSES.
_WAIT_HANDOFF_STATUSES: frozenset[str] = SCOPE_GATED_APPROVAL_STATUSES

_TERMINAL_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    }
)
_MULTI_TICKET_NEEDS_ANY = "Pass --any to wait on more than one ticket."


def _blocked_on_user_exit_code(task: TicketTask) -> int:
    """Map a BLOCKED_ON_USER task to its ``dev-queue wait`` exit code.

    Returns ``_WAIT_EXIT_ATTENTION`` when the block originated from a reap
    proposal (the owning session has ``reap_proposed_at`` set, #542), else
    ``_WAIT_EXIT_BLOCKED``.
    """
    session = resolve_session_for_task(task, load_state())
    if session is not None and session.reap_proposed_at is not None:
        return _WAIT_EXIT_ATTENTION
    return _WAIT_EXIT_BLOCKED


def _session_has_reap_evidence(session_id: str) -> bool:
    """True iff *session_id* exists in state with ``reap_proposed_at`` set.

    Mirrors ``_blocked_on_user_exit_code``'s check-else-fall-through: absence
    of evidence (session pruned from state, or found with reap_proposed_at
    still None) is a deliberate fail-toward-non-alarming default (#1557) —
    the caller falls through to spawn-window grace rather than firing
    ATTENTION on a bare row sample that a normal stage handoff also produces.
    """
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    return session is not None and session.reap_proposed_at is not None


def _handle_terminal_task(
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    output_json: bool,
) -> None:
    """Emit terminal-status output and raise the mapped ``Exit`` for *task*.

    Only call when ``task.status`` is one of COMPLETED / FAILED / CANCELLED /
    BLOCKED_ON_USER / AWAITING_OPERATOR_SIGNOFF. COMPLETED returns normally;
    every other terminal status raises ``click.exceptions.Exit``.
    """
    status_str = task.status.value
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": status_str,
                    "session_id": task.session_id,
                    "state": "terminal",
                    "sentinel_status": None,
                    "pr_url": None,
                }
            )
        )
    else:
        click.echo(f"Status: {status_str.upper()}")

    if task.status == QueueItemStatus.COMPLETED:
        return
    if is_human_kept_gate(task):
        raise click.exceptions.Exit(_WAIT_EXIT_HUMAN_GATE)
    if task.status == QueueItemStatus.BLOCKED_ON_USER:
        raise click.exceptions.Exit(_blocked_on_user_exit_code(task))
    raise click.exceptions.Exit(_WAIT_EXIT_FAILED)


def _handle_sentinel_terminal(
    sentinel: AutoDevResult,
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    output_json: bool,
    *,
    last_result_source: str | None,
) -> NoReturn:
    """Emit sentinel-terminal output and raise the mapped ``Exit`` for *sentinel*."""
    exit_code = _WAIT_STATUS_EXIT.get(sentinel.status, _WAIT_EXIT_FAILED)
    pr_url = sentinel.pr.url if sentinel.pr is not None else None
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": task.session_id,
                    "state": "terminal",
                    "sentinel_status": sentinel.status,
                    "pr_url": pr_url,
                    "last_result_source": last_result_source,
                }
            )
        )
    else:
        click.echo(
            f"Sentinel: {sentinel.status.upper()}" + (f" ({pr_url})" if pr_url else "")
        )
    raise click.exceptions.Exit(exit_code)


def _handle_attention(
    task: TicketTask,
    session: Session,
    ticket_id: str,
    resolved: str,
    output_json: bool,
    *,
    now: datetime,
    transcript_age: float | None,
) -> None:
    """Emit ATTENTION output and raise ``Exit(_WAIT_EXIT_ATTENTION)``.

    Only called when the attention predicate holds, which guarantees
    ``transcript_age`` is non-None; the ``or 0.0`` is a defensive default.
    """
    elapsed_seconds = (now - session.started_at).total_seconds()
    age = transcript_age if transcript_age is not None else 0.0
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": task.session_id,
                    "state": "attention",
                    "sentinel_status": None,
                    "pr_url": None,
                    "elapsed_seconds": elapsed_seconds,
                    "transcript_age_seconds": transcript_age,
                }
            )
        )
    else:
        click.echo(
            f"ATTENTION: {ticket_id} stalled (transcript {age:.0f}s old, not in roster)"
        )
    raise click.exceptions.Exit(_WAIT_EXIT_ATTENTION)


def _check_stale_attention(
    task: TicketTask,
    session: Session,
    sentinel: AutoDevResult | BlockedResult | None,
    ticket_id: str,
    resolved: str,
    output_json: bool,
) -> None:
    """Fire ATTENTION when the session is stale and absent from the daemon roster.

    No-op when the transcript is fresh, the session is in roster, or a
    BlockedResult sentinel is present (partial-write guard).  Raises
    ``Exit(_WAIT_EXIT_ATTENTION)`` when the attention predicate holds.
    """
    now = datetime.now(UTC)
    transcript_age = _transcript_age_seconds(session, now)
    is_stale = (
        transcript_age is not None and transcript_age > _WAIT_STALE_ATTENTION_SECONDS
    )
    # BlockedResult → keep polling (partial write guard), so exclude from
    # ATTENTION. Checked before the roster query (both are cheap local reads)
    # so a polling wait loop does not shell out to the daemon every 5s on a
    # session that is plainly still working.
    if not is_stale or isinstance(sentinel, BlockedResult):
        return
    # #1762: one shared definition of "is this session's surface still live",
    # rather than a second hand-rolled surface_ref/roster comparison.
    liveness = session_daemon_liveness(
        session, get_native_daemon_client().list_live_session_short_ids()
    )
    if liveness.native_surface and not liveness.in_roster:
        _handle_attention(
            task,
            session,
            ticket_id,
            resolved,
            output_json,
            now=now,
            transcript_age=transcript_age,
        )


def _handle_reaped_mid_wait(
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    last_session_id: str | None,
    output_json: bool,
) -> None:
    """Emit ATTENTION output when a session is reaped during the wait loop.

    Called only when session_id has transitioned from non-None to None
    mid-wait AND the prior session carries ``reap_proposed_at`` evidence
    (checked by the caller via ``_session_has_reap_evidence``, #1557) —
    together confirming reconcile reaped the owning session and reverted the
    task to PENDING.  A bare non-None→None transition alone is not sufficient
    evidence: a normal inter-stage handoff clears session_id the same way.
    The orchestrator session decides whether to re-dispatch (#542).
    """
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": last_session_id,
                    "state": "attention",
                    "reason": "reaped_awaiting_redispatch",
                    "sentinel_status": None,
                    "pr_url": None,
                    "elapsed_seconds": None,
                    "transcript_age_seconds": None,
                }
            )
        )
    else:
        click.echo(f"ATTENTION: {ticket_id} reaped mid-wait (task reverted to PENDING)")
    raise click.exceptions.Exit(_WAIT_EXIT_ATTENTION)


def _emit_wait_timeout(
    ticket_ids: list[str],
    clients: list[str],
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Emit timeout output for ``dev-queue wait`` (one or several tickets)."""
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ",".join(ticket_ids),
                    "client": ",".join(dict.fromkeys(clients)),
                    "status": "timeout",
                    "session_id": None,
                    "state": "timeout",
                    "sentinel_status": None,
                    "pr_url": None,
                }
            )
        )
    else:
        click.echo(
            f"Timeout waiting for {', '.join(ticket_ids)} (>{timeout_seconds:.0f}s)"
        )


def _raise_stuck(
    wait: TicketWait,
    task: TicketTask,
    block: PendingBlock,
    age_seconds: float,
    output_json: bool,
    *,
    sentinel_status: str | None = None,
) -> None:
    """Emit the exit-5 result for *wait*'s ticket and raise ``Exit(5)``."""
    emit_wait_stuck(
        ticket_id=wait.ticket_id,
        client=wait.client,
        status=task.status.value,
        session_id=task.session_id,
        block=block,
        age_seconds=age_seconds,
        sentinel_status=sentinel_status,
        output_json=output_json,
    )
    raise click.exceptions.Exit(_WAIT_EXIT_STUCK)


def _probe_pending_row(wait: TicketWait, task: TicketTask, output_json: bool) -> None:
    """Throttled ticket-view probe of a PENDING row: progress line, maybe exit 5.

    At most once per ``WAIT_PROBE_SECONDS``; a ``WAITING`` line goes to stderr
    on first sight and whenever the skip code or its source changes. A
    needs-action block held for ``--stuck-after-minutes`` raises ``Exit(5)``.
    """
    at = time.monotonic()
    if not wait.probe_due(at):
        return
    now = datetime.now(UTC)
    probe = probe_pending(wait.ticket_id, wait.client, now=now, sources=ViewSources())
    if probe is None:
        return
    line, key = format_waiting_line(
        wait.ticket_id, probe.view.dispatch, probe.block, now=now
    )
    if wait.progress_changed(key):
        click.echo(line, err=True)
    age = wait.tracker.observe(wait.ticket_id, probe.block, at)
    if age is not None and probe.block is not None:
        _raise_stuck(wait, task, probe.block, age, output_json)


def _note_handoff(
    wait: TicketWait, task: TicketTask, sentinel: AutoDevResult, output_json: bool
) -> None:
    """Keep waiting on a RUNNING row mid dispatch hand-off; exit 5 if unrouted.

    The fix-loop hand-off has no grace (dispatch leaves the row RUNNING by
    design). A plan/review approval result dispatch has not routed after
    ``HANDOFF_STUCK_SECONDS`` raises ``Exit(5)`` with ``handoff_unrouted``.
    """
    at = time.monotonic()
    if wait.handoff_since is None:
        wait.handoff_since = at
    elapsed = at - wait.handoff_since
    scope_gated = sentinel.status in _WAIT_HANDOFF_STATUSES
    label = sentinel.status if scope_gated else str(_handoff_reason(sentinel))
    if wait.progress_changed(f"handoff|{label}"):
        click.echo(format_handoff_line(wait.ticket_id, label, elapsed), err=True)
    if scope_gated and elapsed >= HANDOFF_STUCK_SECONDS:
        block = handoff_unrouted_block(sentinel.status, int(elapsed))
        _raise_stuck(
            wait, task, block, elapsed, output_json, sentinel_status=sentinel.status
        )


def _handoff_reason(sentinel: AutoDevResult) -> str | None:
    return sentinel.blocker.reason if sentinel.blocker is not None else None


def _check_reaped_mid_wait(
    wait: TicketWait, task: TicketTask, output_json: bool
) -> None:
    """ATTENTION when a session id seen earlier was cleared by a reap (#542).

    A bare non-None -> None transition is not enough: a normal inter-stage
    hand-off clears ``session_id`` the same way, so the prior session must
    carry ``reap_proposed_at`` (#1557); otherwise this is spawn-window grace.
    """
    if wait.observed_session_id is not None and _session_has_reap_evidence(
        wait.observed_session_id
    ):
        _handle_reaped_mid_wait(
            task, wait.ticket_id, wait.client, wait.observed_session_id, output_json
        )


def _poll_ticket(wait: TicketWait, timeout_seconds: float, output_json: bool) -> bool:
    """One poll of one ticket; True when it completed (exit 0).

    Every other exit state raises ``click.exceptions.Exit``; False means keep
    waiting.
    """
    # --- Step 1: fast path — task already terminal in the queue ---
    task = _find_task_or_timeout(wait, timeout_seconds, output_json)
    if task.status in _TERMINAL_STATUSES:
        # COMPLETED returns; every other terminal status raises Exit.
        _handle_terminal_task(task, wait.ticket_id, wait.client, output_json)
        return True
    if task.status == QueueItemStatus.PENDING:
        _probe_pending_row(wait, task, output_json)
    else:
        # A row that left PENDING restarts its stuck clock if it comes back.
        wait.tracker.retain(set())

    # --- Step 2: resolve the session ---
    session_id = task.session_id
    if session_id is None:
        _check_reaped_mid_wait(wait, task, output_json)
        # Spawn-window grace: no session registered yet, or a session_id
        # clear with no reap evidence (#1557) — keep polling.
        return False
    wait.observed_session_id = session_id
    session = next((s for s in load_state().sessions if s.id == session_id), None)
    if session is None:
        # Session not in state yet — spawn-window grace, keep polling.
        return False
    if _check_last_result(wait, task, session, output_json):
        return False

    # --- Step 3: resolve claude session id ---
    csid = session.claude_session_id or _csid_from_transcript(session)

    # --- Step 4: parse sentinel from transcript ---
    sentinel: AutoDevResult | BlockedResult | None = None
    if session.worktree_path is not None and csid is not None:
        sentinel = _parse_sentinel_from_transcript(
            str(session.worktree_path), csid, warned_blocks=wait.warned_blocks
        )

    # Why: session.last_result (Step 2a) is the sole terminal-decision source
    # (RFC 0012 harvest authority) -- a transcript-parsed terminal-shaped
    # sentinel is no longer trusted for the terminal exit, since a stale
    # sentinel from a requeued session's PRIOR run can still be sitting in the
    # transcript (#2397). `sentinel` survives past this point only to feed
    # _check_stale_attention's BlockedResult partial-write guard below.

    # --- Step 5: HEARTBEAT / ATTENTION ---
    # ATTENTION: stale AND worker not native OR not in daemon roster.
    # BlockedResult → keep polling (partial write guard), so exclude from ATTENTION.
    _check_stale_attention(
        task, session, sentinel, wait.ticket_id, wait.client, output_json
    )
    return False


def _find_task_or_timeout(
    wait: TicketWait, timeout_seconds: float, output_json: bool
) -> TicketTask:
    """:func:`_find_task`, emitting the timeout document in the caller's mode."""
    try:
        return _find_ticket(load_dev_queue(), wait.ticket_id, wait.client)
    except CwError:
        pass
    timeout = timeout_seconds if timeout_seconds > 0 else math.inf
    try:
        return wait_for_terminal(wait.ticket_id, wait.client, timeout=timeout)
    except TimeoutError:
        _emit_wait_timeout(
            [wait.ticket_id], [wait.client], timeout_seconds, output_json
        )
        raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT) from None


def _check_last_result(
    wait: TicketWait, task: TicketTask, session: Session, output_json: bool
) -> bool:
    """Step 2a: the published result decides; True while a hand-off is pending.

    session.last_result is the one-door published result -- a fresh Session
    (e.g. after requeue's _reset_for_same_stage_requeue, #2397) is a new
    object with last_result=None, so this check is structurally immune to a
    stale transcript-parsed sentinel surviving from a requeued session's
    prior run. Checked BEFORE any transcript parse. INTERMEDIATE_ADVANCE_STATUSES
    (stage_complete) is excluded: dispatch advances the ticket to its next
    stage rather than this wait exiting on it. An internal hand-off on a
    RUNNING row keeps waiting (and skips the stale-transcript ATTENTION check,
    since the reporting session has legitimately exited); on a row dispatch
    already moved off RUNNING it is ignored.
    """
    sentinel = reconstruct_staged_sentinel(session.last_result)
    if (
        not isinstance(sentinel, AutoDevResult)
        or sentinel.status in INTERMEDIATE_ADVANCE_STATUSES
    ):
        wait.handoff_since = None
        return False
    if is_internal_handoff(sentinel):
        if task.status != QueueItemStatus.RUNNING:
            wait.handoff_since = None
            return False
        _note_handoff(wait, task, sentinel, output_json)
        return True
    _handle_sentinel_terminal(
        sentinel,
        task,
        wait.ticket_id,
        wait.client,
        output_json,
        last_result_source=(
            session.last_result_source.value if session.last_result_source else None
        ),
    )


@dev_queue.command(name="wait")
@click.argument("ticket_ids", nargs=-1, required=True, metavar="TICKET_ID...")
@click.option(
    "--client",
    "-c",
    default=None,
    shell_complete=_complete_client,
    help="Client name.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.FloatRange(min=0),
    default=_WAIT_DEFAULT_TIMEOUT,
    help="Seconds to wait (default: 21600 = 6 hours; 0 = no limit).",
)
@click.option(
    "--any",
    "any_ticket",
    is_flag=True,
    default=False,
    help=(
        "With several TICKET_IDs, return as soon as the first one reaches "
        "an exit state. Without it, exactly one ticket is allowed."
    ),
)
@click.option(
    "--stuck-after-minutes",
    type=click.IntRange(min=1),
    default=STUCK_PENDING_MINUTES,
    help=(
        "Exit 5 when a pending ticket's blocker will not clear on its own "
        "after this many minutes (default: 15)."
    ),
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON output.",
)
@handle_errors
def dev_queue_wait(
    ticket_ids: tuple[str, ...],
    client: str | None,
    timeout_seconds: float,
    any_ticket: bool,
    stuck_after_minutes: int,
    output_json: bool,
) -> None:
    """Block until a dev-queue ticket reaches terminal status.

    Sentinel-aware: the terminal decision is sourced from ``session.last_result``
    (RFC 0012 harvest authority, checked first every poll) rather than relying
    solely on task-status polling. A transcript-parsed AUTO_DEV_RESULT sentinel
    is demoted to a heartbeat/``BlockedResult``-guard signal only -- it is no
    longer trusted for the terminal exit, since a stale sentinel from a
    requeued session's prior run can still be sitting in the transcript
    (#2397). Together this eliminates both false-timeout (exit 124) for
    long-running healthy workers whose reconcile cycle hasn't fired yet, and
    false-terminal reporting of a leftover sentinel from before a requeue.

    A ``stage_complete`` sentinel is a successful intermediate hand-off, not
    a terminal outcome — the wait keeps polling while dispatch advances the
    ticket to its next stage.

    Exit codes:
      0   shipped / no_op (or COMPLETED queue status)
      1   scope_exceeded / forbidden_area / FAILED / CANCELLED
      2   blocked / merge_pending / ambiguities_pending_resolution /
          premises_pending_verification / BLOCKED_ON_USER (no reap
          proposal), once dispatch has parked the row
      3   ATTENTION — transcript stale past idle budget, worker not in roster;
          or BLOCKED_ON_USER caused by a reap proposal (reap_proposed_at set);
          or a mid-wait session_id clear confirmed by reap_proposed_at on the
          prior session (#1557 — a bare clear during a normal stage handoff
          does NOT fire this; it falls through to spawn-window grace/timeout)
      4   human gate — the row is held for an action only a human can give:
          AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3, #990), a finalize hold
          (hold_finalize / finalize_gate: manual), or a scope_hint: large
          approval gate
      5   stuck — a PENDING ticket whose blocker will not clear on its own
          (tripped lane breaker with no auto-resume, paused lane,
          ssh key / disk pressure gate) for --stuck-after-minutes,
          or a recorded plan/review approval result dispatch has not routed
          for 300s. The reason and a next command are printed.
      124 hard timeout ceiling (--timeout) with no terminal or attention signal

    Internal hand-offs never exit: a blocked result with blocker.reason
    fix_loop_pending_dispatch, or a plan_pending_approval /
    review_pending_approval result on a row that is still RUNNING, is
    dispatch's own routine routing. The wait keeps polling until the row
    settles, then the codes above apply.

    While a ticket waits PENDING (lane cap, paused lane, ...), one
    "WAITING | ..." progress line per change goes to stderr; stdout stays
    one result document.
    """
    if len(ticket_ids) > 1 and not any_ticket:
        raise click.UsageError(_MULTI_TICKET_NEEDS_ANY)
    config = load_orchestrator_config()
    threshold = stuck_after_minutes * SECONDS_PER_MINUTE
    waits = [
        TicketWait(
            ticket_id=ticket_id,
            client=resolve_client(ticket_id, config, client),
            tracker=PendingTracker(threshold),
        )
        for ticket_id in dict.fromkeys(ticket_ids)
    ]
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else math.inf
    while True:
        # The first ticket to reach an exit state ends the wait: every exit
        # but COMPLETED raises click.exceptions.Exit out of _poll_ticket.
        for wait in waits:
            if _poll_ticket(wait, timeout_seconds, output_json):
                return
        if time.monotonic() >= deadline:
            _emit_wait_timeout(
                [w.ticket_id for w in waits],
                [w.client for w in waits],
                timeout_seconds,
                output_json,
            )
            raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT)
        time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
