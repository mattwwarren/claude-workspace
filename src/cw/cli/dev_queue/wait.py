"""``dev-queue wait`` — block until a ticket reaches terminal status.

Sentinel-aware wait loop plus its terminal/attention/timeout emit helpers.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import click

from cw.auto_dev_result import (
    INTERMEDIATE_ADVANCE_STATUSES,
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
from cw.session import _is_native_surface_ref

from ._group import (
    _WAIT_EXIT_ATTENTION,
    _WAIT_EXIT_BLOCKED,
    _WAIT_EXIT_FAILED,
    _WAIT_EXIT_SIGNOFF,
    _WAIT_EXIT_TIMEOUT,
    dev_queue,
)

_WAIT_DEFAULT_TIMEOUT: int = 300

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
    "plan_pending_approval": _WAIT_EXIT_BLOCKED,
    "review_pending_approval": _WAIT_EXIT_BLOCKED,
    "merge_gate_blocked": _WAIT_EXIT_BLOCKED,
    # PR created, awaiting CI/merge — dispatch routes the task to
    # BLOCKED_ON_USER (#899), so the sentinel path must agree with the
    # queue-status path instead of misreporting FAILED.
    "merge_pending": _WAIT_EXIT_BLOCKED,
    "scope_exceeded": _WAIT_EXIT_FAILED,
    "forbidden_area": _WAIT_EXIT_FAILED,
    # #1870: an empty branch needs a human to push commits or close the ticket,
    # so it maps to BLOCKED (matching the dispatch gate's BLOCKED_ON_USER park)
    # rather than FAILED — nothing errored, there is simply nothing there.
    "empty_diff_blocked": _WAIT_EXIT_BLOCKED,
    # #1862: this ticket already has an open, unmerged PR from an earlier
    # dispatch. A human has to land or close it before the ticket can move,
    # so it maps to BLOCKED (matching the dispatch gate's BLOCKED_ON_USER
    # park) rather than FAILED — nothing errored, the work is already in
    # review.
    "stale_dispatch": _WAIT_EXIT_BLOCKED,
}


def _blocked_on_user_exit_code(task: TicketTask) -> int:
    """Map a BLOCKED_ON_USER task to its ``dev-queue wait`` exit code.

    Returns ``_WAIT_EXIT_ATTENTION`` when the block originated from a reap
    proposal (the owning session has ``reap_proposed_at`` set, #542), else
    ``_WAIT_EXIT_BLOCKED``.
    """
    if task.session_id is not None:
        state = load_state()
        session = next(
            (s for s in state.sessions if s.id == task.session_id),
            None,
        )
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
    if task.status == QueueItemStatus.BLOCKED_ON_USER:
        raise click.exceptions.Exit(_blocked_on_user_exit_code(task))
    if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        raise click.exceptions.Exit(_WAIT_EXIT_SIGNOFF)
    raise click.exceptions.Exit(_WAIT_EXIT_FAILED)


def _handle_sentinel_terminal(
    sentinel: AutoDevResult,
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    output_json: bool,
) -> None:
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
                }
            )
        )
    else:
        click.echo(
            f"Sentinel: {sentinel.status.upper()}" + (f" ({pr_url})" if pr_url else "")
        )
    raise click.exceptions.Exit(exit_code)


def _raise_if_deadline_exceeded(
    deadline: float,
    ticket_id: str,
    resolved: str,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Emit timeout output and raise ``Exit`` when the hard ceiling has passed.

    No-op (returns) while time remains, so callers can fall through to a
    poll-sleep. Used by every poll-and-retry branch in ``dev-queue wait``.
    """
    if time.monotonic() >= deadline:
        _emit_wait_timeout(ticket_id, resolved, timeout_seconds, output_json)
        raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT)


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
    in_roster = False
    surface_ref = session.surface_ref
    if surface_ref is not None and _is_native_surface_ref(surface_ref):
        daemon = get_native_daemon_client()
        in_roster = surface_ref in daemon.list_live_session_short_ids()

    # BlockedResult → keep polling (partial write guard), so exclude from ATTENTION.
    no_sentinel_at_all = sentinel is None
    is_attention = (
        is_stale
        and no_sentinel_at_all
        and surface_ref is not None
        and _is_native_surface_ref(surface_ref)
        and not in_roster
    )
    if is_attention:
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
    The operator must decide whether to re-dispatch (#542).
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
    ticket_id: str,
    resolved: str,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Emit timeout output for ``dev-queue wait``."""
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": "timeout",
                    "session_id": None,
                    "state": "timeout",
                    "sentinel_status": None,
                    "pr_url": None,
                }
            )
        )
    else:
        click.echo(f"Timeout waiting for {ticket_id} (>{timeout_seconds:.0f}s)")


@dev_queue.command(name="wait")
@click.argument("ticket_id")
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
    type=float,
    default=_WAIT_DEFAULT_TIMEOUT,
    help="Seconds to wait (default: 300).",
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
    ticket_id: str,
    client: str | None,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Block until a dev-queue ticket reaches terminal status.

    Sentinel-aware: detects AUTO_DEV_RESULT sentinels in the transcript
    directly rather than relying solely on task-status polling.  This
    eliminates false-timeout (exit 124) for long-running healthy workers
    whose reconcile cycle hasn't fired yet.

    A ``stage_complete`` sentinel is a successful intermediate hand-off, not
    a terminal outcome — the wait keeps polling while dispatch advances the
    ticket to its next stage.

    Exit codes:
      0   shipped / no_op (or COMPLETED queue status)
      1   scope_exceeded / forbidden_area / FAILED / CANCELLED
      2   blocked / merge_pending / *_pending_* family / BLOCKED_ON_USER
          (no reap proposal)
      3   ATTENTION — transcript stale past idle budget, worker not in roster;
          or BLOCKED_ON_USER caused by a reap proposal (reap_proposed_at set);
          or a mid-wait session_id clear confirmed by reap_proposed_at on the
          prior session (#1557 — a bare clear during a normal stage handoff
          does NOT fire this; it falls through to spawn-window grace/timeout)
      4   AWAITING_OPERATOR_SIGNOFF — ticket parked for an explicit operator
          signoff before it ships (RFC 0007 Phase 3, #990)
      124 hard timeout ceiling (--timeout) with no terminal or attention signal
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)

    deadline = time.monotonic() + timeout_seconds
    # Track the first non-None session_id seen so we can distinguish a
    # legitimate spawn window (session_id never set yet) from a post-reap
    # revert (session_id was set, then cleared by reconcile — #542).
    observed_session_id: str | None = None
    # Caller-owned dedup set (issue #1247): created once, outside the poll
    # loop, so an unresolved malformed sentinel's WARNING re-logs at most
    # once across the entire wait's lifetime rather than every 5s poll.
    warned_blocks: set[str] = set()

    while True:
        # --- Step 1: fast path — task already terminal in the queue ---
        store = load_dev_queue()
        try:
            task = _find_ticket(store, ticket_id, resolved)
        except CwError:
            task = None
        if task is None:
            # Fallback: delegate to wait_for_terminal so it can surface
            # "not found" errors (CwError) and handle TimeoutError.
            try:
                task = wait_for_terminal(ticket_id, resolved, timeout=timeout_seconds)
            except TimeoutError:
                _emit_wait_timeout(ticket_id, resolved, timeout_seconds, output_json)
                raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT) from None

        if task.status in {
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        }:
            # COMPLETED returns; every other terminal status raises Exit.
            _handle_terminal_task(task, ticket_id, resolved, output_json)
            return

        # --- Step 2: resolve the session ---
        session_id = task.session_id
        if session_id is None:
            if observed_session_id is not None and _session_has_reap_evidence(
                observed_session_id
            ):
                # session_id was non-None on a prior poll and is now None, AND
                # the prior session carries reap_proposed_at — reconcile
                # reaped the session and reverted the task to PENDING; surface
                # ATTENTION (#542).
                _handle_reaped_mid_wait(
                    task, ticket_id, resolved, observed_session_id, output_json
                )
            # Spawn-window grace: session hasn't registered yet, OR the
            # non-None→None transition has no corresponding reap evidence
            # (#1557 — a normal inter-stage handoff clears session_id the
            # same way a reap does) — keep polling.
            _raise_if_deadline_exceeded(
                deadline, ticket_id, resolved, timeout_seconds, output_json
            )
            time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
            continue

        observed_session_id = session_id

        cw_state = load_state()
        session = next((s for s in cw_state.sessions if s.id == session_id), None)
        if session is None:
            # Session not in state yet — spawn-window grace, keep polling.
            _raise_if_deadline_exceeded(
                deadline, ticket_id, resolved, timeout_seconds, output_json
            )
            time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
            continue

        # --- Step 3: resolve claude session id ---
        csid = session.claude_session_id or _csid_from_transcript(session)

        # --- Step 4: parse sentinel from transcript ---
        sentinel: AutoDevResult | BlockedResult | None = None
        if session.worktree_path is not None and csid is not None:
            sentinel = _parse_sentinel_from_transcript(
                str(session.worktree_path), csid, warned_blocks=warned_blocks
            )

        # BlockedResult means framing present but payload unusable — treat as
        # not-yet-terminal (could be a partial write); keep polling.
        # INTERMEDIATE_ADVANCE_STATUSES (stage_complete) is a successful
        # stage hand-off, not a ticket-terminal outcome: dispatch advances the
        # ticket to its next stage, so keep polling instead of exiting FAILED
        # (previously stage_complete fell through .get()'s FAILED default).
        if (
            isinstance(sentinel, AutoDevResult)
            and sentinel.status not in INTERMEDIATE_ADVANCE_STATUSES
        ):
            # TERMINAL: emit and raise the mapped exit code.
            _handle_sentinel_terminal(sentinel, task, ticket_id, resolved, output_json)

        # --- Step 5: HEARTBEAT / ATTENTION ---
        # ATTENTION: stale AND worker not native OR not in daemon roster.
        # Must guard with _is_native_surface_ref to avoid false-attention on
        # non-daemon surface refs (e.g. tmux window names).
        # BlockedResult → keep polling (partial write guard), so exclude from ATTENTION.
        _check_stale_attention(
            task, session, sentinel, ticket_id, resolved, output_json
        )

        # HEARTBEAT: no terminal sentinel but transcript advancing (or session
        # hasn't hit the budget yet) — keep polling within the hard ceiling.
        _raise_if_deadline_exceeded(
            deadline, ticket_id, resolved, timeout_seconds, output_json
        )

        time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
