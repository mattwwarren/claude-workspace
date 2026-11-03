"""Close operator-named routed-result sessions now (#2708).

``cw doctor --reap --routed-session-id <id>`` names a session whose routed
result is already on record. The 30-minute transcript-staleness bucket only
debounces the reconcile *signal* (ADR-0014 invariant 1); an explicit operator
command is the authority that closes the session (invariant 2), so a named id
is closed now rather than once the bucket is reached.

The close is the one ``cw dev-queue approve`` and ``requeue`` already use
(``cw.cli.routed_orphan_close._close_one`` in ``_CloseMode.NAMED``): it
re-validates through ``split_resolvable_routed_sessions``, stops the worker
and confirms it gone from the daemon roster with no lock held (or corroborates
its absence), and only then flips the session COMPLETED through
``_spawn_close_impl``, which takes ``sessions_lock`` for the flip alone
(ADR-0019). This module adds the id-keyed entry point, the refusals for ids
that are unknown or not routed-marker sessions, and the evidence printed
before the irreversible stop. It never takes a state lock itself.

Every line goes to stderr, so a ``--json`` caller's stdout stays one report
document; the returned ids carry success.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, cast

import click

from cw.cli.routed_orphan_close import (
    _close_one,
    _CloseMode,
    _OrphanCloseCall,
    _ticket_label,
)
from cw.config import load_state
from cw.exceptions import CwError, LockTimeoutError
from cw.native_daemon import get_native_daemon_client
from cw.reconcile._shared import (
    _transcript_age_seconds,
    _unresolved_subagent_spawn_age_seconds,
    ticket_id_for_session,
)
from cw.reconcile.routed_result_sessions import (
    IneligibleNamed,
    MarkerRejection,
    select_named_routed_sessions,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.cli.routed_orphan_close import _Revalidated
    from cw.models import OrchestratorConfig, Session
    from cw.native_daemon import NativeDaemonClient

logger = logging.getLogger(__name__)

# ``command`` of the shared close's audit and log lines.
_COMMAND = "doctor_reap"
_SECONDS_PER_MINUTE = 60
_REASON_UNKNOWN = "unknown_session"
_REFUSED_LOG = "routed_named_close_refused: session_id=%s reason=%s"

# Why a named session that exists is not a routed-marker candidate, one per
# MarkerRejection (a lookup, not a branch per reason). Formatted with the
# session's ``origin`` and ``status`` values.
_REJECTION_REASONS: dict[MarkerRejection, str] = {
    MarkerRejection.NOT_DAEMON: (
        "it is a {origin}-origin session, not a daemon worker"
    ),
    MarkerRejection.NOT_LIVE: "its status is {status}, not active or idle",
    MarkerRejection.ORCHESTRATOR: "it is the orchestrator session",
    MarkerRejection.NO_ROUTED_RESULT: (
        "no routed result is on record (its last result carries no"
        " consumed-route marker, #2458)"
    ),
    MarkerRejection.REFUSAL_LATCHED: (
        "a stage refusal is latched on it; the refusal pages own that shape"
    ),
    MarkerRejection.NO_SURFACE: (
        "it has no daemon surface; doctor class 9 owns that shape"
    ),
}

_WORKER_TO_STOP = (
    "its worker is listed in the daemon roster and is about to be stopped; the"
    " close completes only once the roster confirms it gone"
)
_WORKER_ABSENT = (
    "its worker is absent from a readable, trustworthy daemon roster (re-read"
    " once more before the flip); no stop is issued"
)
_NOT_CHECKED = (
    "Not checked: non-subagent background work (a bash task, a build) is"
    " invisible to the drain check and ends with the worker; transcript age is"
    " not enforced."
)


def _refuse_unknown(session_id: str) -> None:
    """Name an id no session in cw state carries."""
    click.echo(
        f"Cannot close routed-result session {session_id}: no session with that"
        " exact id is recorded in cw state. Nothing was changed. List sessions"
        " with: cw list",
        err=True,
    )
    logger.warning(_REFUSED_LOG, session_id, _REASON_UNKNOWN)


def _refuse_ineligible(item: IneligibleNamed) -> None:
    """Name a session that exists but is not a routed-marker candidate."""
    session = item.session
    reason = _REJECTION_REASONS[item.rejection].format(
        origin=session.origin.value, status=session.status.value
    )
    ticket = ticket_id_for_session(session.name) or "(no ticket)"
    click.echo(
        f"Cannot close routed-result session {session.id} for {ticket}"
        f" ({session.client}): {reason}. Its recorded status was not changed.",
        err=True,
    )
    logger.warning(_REFUSED_LOG, session.id, item.rejection.value)


def _minutes_ago(seconds: float | None, *, absent: str) -> str:
    """*seconds* as whole minutes ago, or *absent* when there is no age."""
    if seconds is None:
        return absent
    return f"{seconds / _SECONDS_PER_MINUTE:.0f} min ago"


def _announce_before_act(checked: _Revalidated, *, call: _OrphanCloseCall) -> None:
    """Print the evidence the close relies on, before the irreversible act.

    Transcript and spawn-stamp ages are shown for information only: neither
    gates the close (ADR-0014 forbids a timer gating a destructive act).
    """
    session = checked.session
    now = datetime.now(UTC)
    deadline = call.config.fix_loop_await_deadline_minutes
    worker = _WORKER_TO_STOP if checked.outcome == "stop" else _WORKER_ABSENT
    transcript = _minutes_ago(
        _transcript_age_seconds(session, now), absent="no transcript found"
    )
    stamp = _minutes_ago(
        _unresolved_subagent_spawn_age_seconds(session.worktree_path, now),
        absent="none outstanding",
    )
    lines = (
        f"Closing routed-result session {session.id} for {_ticket_label(call)}"
        f" ({call.client}) now; the 30-minute staleness bucket is not consulted."
        " Evidence relied on:",
        "  - routed result on record (consumed-route marker on its last result)",
        "  - no occupied queue row binds it",
        f"  - no subagent-spawn stamp younger than {deadline} minutes",
        f"  - {worker}",
        _NOT_CHECKED,
        "For information only (never gating, ADR-0014): last transcript"
        f" activity: {transcript}; newest subagent-spawn stamp: {stamp}.",
    )
    for line in lines:
        click.echo(line, err=True)


def _named_call(
    session: Session,
    *,
    config: OrchestratorConfig,
    daemon: NativeDaemonClient,
    candidates: Sequence[Session],
) -> _OrphanCloseCall:
    """The shared close's context for one named candidate."""
    return _OrphanCloseCall(
        ticket_id=ticket_id_for_session(session.name) or "",
        client=session.client,
        command=_COMMAND,
        mode=_CloseMode.NAMED,
        config=config,
        daemon=daemon,
        candidate_ids=frozenset(c.id for c in candidates),
        candidate_surfaces=frozenset(cast("str", c.surface_ref) for c in candidates),
    )


def _report_unattempted(remaining: Sequence[Session]) -> None:
    """Name the ids a sessions-lock timeout left unattempted."""
    if not remaining:
        return
    ids = ", ".join(s.id for s in remaining)
    click.echo(
        f"Not attempted: {ids}. The sessions lock timed out, and each remaining"
        " close would wait it out again; re-run this command for them once the"
        " lock is free.",
        err=True,
    )


def close_named_routed_result_sessions(
    session_ids: Sequence[str],
    *,
    config: OrchestratorConfig,
    native_daemon: NativeDaemonClient | None = None,
) -> list[str]:
    """Close each named routed-result session now; return the closed ids.

    Unknown ids and sessions that are not routed-marker candidates are
    refused with their reason before any daemon client is resolved. Every
    candidate then goes through the shared close, which refuses (on stderr)
    a pinned, draining or roster-untrusted session, an unreadable roster, an
    unconfirmed stop, and a failed flip; nothing is flipped on a refusal.
    Sessions closed earlier stay closed. A flip that times out on
    ``sessions_lock`` stops the loop, because each remaining close would wait
    the bounded lock out again; the unattempted ids are named.
    """
    selection = select_named_routed_sessions(load_state(), session_ids)
    for session_id in selection.unknown:
        _refuse_unknown(session_id)
    for item in selection.ineligible:
        _refuse_ineligible(item)
    candidates = selection.candidates
    if not candidates:
        return []
    daemon = native_daemon or get_native_daemon_client()
    closed: list[str] = []
    for index, session in enumerate(candidates):
        call = _named_call(session, config=config, daemon=daemon, candidates=candidates)
        try:
            if _close_one(
                session, call, before_act=partial(_announce_before_act, call=call)
            ):
                closed.append(session.id)
        except CwError as exc:
            click.echo(str(exc), err=True)
            if isinstance(exc.__cause__, LockTimeoutError):
                _report_unattempted(candidates[index + 1 :])
                break
    return closed
