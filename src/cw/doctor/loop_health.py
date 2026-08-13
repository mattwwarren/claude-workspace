"""Loop health, liveness, TIMED_OUT-merged detection, and targeted reap.

Split out of ``cw.doctor.core`` (#1314, part 2). Holds the dispatch-loop
forensic checks (:func:`_check_loop_health`, :func:`_check_loop_liveness`),
the TIMED_OUT-with-merged-PR detector (:func:`_check_timed_out_merged`), the
``gh pr list`` helper (:func:`_gh_pr_states`), and the targeted single-session
reap (:func:`_reap_session_by_selector`).

``_reap_session_by_selector`` mutates state directly (``save_state``,
``save_dev_queue``) outside ``mutate_state()`` by design — it is kept colocated
here rather than extracted into a shared mutation helper (#1314 constraint).

The one cross-module need (:func:`_collapse_blocked_on_user_tasks` from
``wedge``) is a function-local deferred import inside
:func:`_reap_session_by_selector` — ``wedge`` imports two symbols from this
module at top level, so this module's reach back into ``wedge`` must be
function-level to break the cycle (see the ``pyproject.toml`` PLC0415 entry).
"""

from __future__ import annotations

import contextlib
import json
import subprocess as _sp
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.config import load_state, save_state, sessions_lock
from cw.dev_queue import dev_queue_lock, save_dev_queue, transition_task_status
from cw.dispatch import TICK_STALE_SECONDS
from cw.dispatch_state import load_executor_blocked_markers
from cw.doctor import _deps
from cw.doctor._shared import CheckResult
from cw.events import read_events, record_event
from cw.gh import TIMED_OUT_MERGED_LOOKBACK_DAYS, pr_is_merged_for_ticket
from cw.models import (
    CompletionReason,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.orchestrate import latest_tick_summary_by_client
from cw.reconcile import feature_branch_key, ticket_id_for_session

if TYPE_CHECKING:
    from typing import Any

    from cw.models import ClientConfig, CwState
    from cw.orchestrate import TickSummary


# Number of consecutive FRESHNESS_GATE ticks required to declare a loop stall.
_LOOP_STALL_CONSECUTIVE_TICKS = 3


def _check_loop_health() -> list[CheckResult]:
    """Detect dispatch stalls: pending>0, running==0 across N consecutive ticks.

    Reads DISPATCH_TICK events from the last hour, groups by client, and checks
    whether the most recent _LOOP_STALL_CONSECUTIVE_TICKS ticks are all
    FRESHNESS_GATE with pending>0 and running==0. When a stall is detected for
    a client, emits a warn=True result suggesting ``cw dev-queue refresh-all``.

    This is the on-demand forensic replay (threshold
    _LOOP_STALL_CONSECUTIVE_TICKS=3, derived from tick events) and coexists
    with the proactive, persisted runtime latch
    ``ClientConcurrencyOverride.consecutive_freshness_blocks`` (threshold 5,
    RFC 0007 §W2) — the two are deliberately not unified.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    events = read_events(
        event_types=[OrchestratorEventType.DISPATCH_TICK],
        since_ts=cutoff,
    )

    per_client: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        client = ev.payload.get("client", "")
        per_client.setdefault(client, []).append(ev.payload)

    results: list[CheckResult] = []
    for client, ticks in per_client.items():
        recent = ticks[-_LOOP_STALL_CONSECUTIVE_TICKS:]
        if len(recent) < _LOOP_STALL_CONSECUTIVE_TICKS:
            continue
        stalled = all(
            t.get("skip_reason") == DispatchSkipReason.FRESHNESS_GATE
            and int(t.get("pending", 0)) > 0
            and int(t.get("running", 0)) == 0
            and int(t.get("claimed", 0)) == 0
            for t in recent
        )
        if stalled:
            results.append(
                CheckResult(
                    f"loop-health/{client}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"dispatch stalled for {client} — main behind origin."
                        " Run `cw dev-queue refresh-all`."
                    ),
                )
            )

    if not results:
        results.append(
            CheckResult("loop-health", ok=True, warn=False, detail="no stall detected")
        )
    return results


def _check_loop_liveness() -> list[CheckResult]:
    """Warn when any client's last dispatch tick is stale and has pending tickets.

    A live executor-blocked marker for the client fully suppresses the warning
    (#1742): a stale tick with a review in flight is expected, not actionable,
    and the warning's own remedy ("Run `cw dev-queue run`") would push the
    operator toward a restart that kills that review. Reads the same marker
    sidecar ``cw dev-queue status`` annotates ``[BLOCKED]`` from; suppression
    rather than annotation because this function's contract is binary
    warn/no-warn with no informational middle state.
    """
    tick_data: dict[str, TickSummary] = latest_tick_summary_by_client()
    if not tick_data:
        return [
            CheckResult("loop-liveness", ok=True, warn=False, detail="no tick history")
        ]

    markers = load_executor_blocked_markers()
    blocked_clients = {marker.client for marker in markers.values()}
    now = datetime.now(UTC)
    results: list[CheckResult] = []
    for client, tick in tick_data.items():
        age = (now - tick.tick_at).total_seconds()
        stale_and_pending = age > TICK_STALE_SECONDS and tick.pending > 0
        if stale_and_pending and client not in blocked_clients:
            results.append(
                CheckResult(
                    f"loop-liveness/{client}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"no dispatch tick for {client} in {int(age)}s"
                        f" ({tick.pending} pending) — loop may have exited."
                        " Run `cw dev-queue run`."
                    ),
                )
            )
    if not results:
        results.append(
            CheckResult(
                "loop-liveness",
                ok=True,
                warn=False,
                detail="no stale+pending condition",
            )
        )
    return results


def _gh_pr_states(branch: str) -> tuple[list[dict[str, Any]], bool]:
    """Return (pr_list, gh_missing) for the given branch.

    Returns ([], False) on empty result or non-zero exit.
    Returns ([], True) if gh binary is not found.
    Swallows OSError, ValueError, TimeoutExpired.
    """
    try:
        pr_result = _sp.run(
            ["gh", "pr", "list", "--head", branch, "--json", "state", "--limit", "1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        prs: list[dict[str, Any]] = (
            json.loads(pr_result.stdout) if pr_result.returncode == 0 else []
        )
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError, _sp.TimeoutExpired):
        return [], False
    else:
        return prs, False


def _check_timed_out_merged(
    state: CwState,
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Detect TIMED_OUT sessions whose linked PR has since merged.

    Scans TIMED_OUT DAEMON sessions whose completed_at falls within
    TIMED_OUT_MERGED_LOOKBACK_DAYS, extracts the ticket id from the
    session name, and uses ``gh issue view`` + ``gh pr view`` to
    determine whether a linked PR is MERGED. Emits a warn=True result
    per session when a merged PR is found.

    *clients* is used to resolve each session's
    :attr:`ClientConfig.feature_branch_prefix` (SSOT for the branch name the
    staged pipeline provisions; GitHub #728).
    """
    cutoff = datetime.now(UTC) - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)
    results: list[CheckResult] = []
    gh_missing = False

    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin != SessionOrigin.DAEMON:
            continue
        if session.completed_at is None or session.completed_at < cutoff:
            continue

        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue

        branch = feature_branch_key(session.client, ticket_id, clients)
        merged, gh_available = pr_is_merged_for_ticket(ticket_id, branch=branch)
        if not gh_available and not gh_missing:
            results.append(
                CheckResult(
                    "timed_out-merged",
                    ok=True,
                    warn=True,
                    detail="gh unavailable; skipping timed_out-merged check",
                )
            )
            gh_missing = True
            continue

        if merged is True:
            results.append(
                CheckResult(
                    f"timed_out-merged/{session.id}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"session {session.id} is TIMED_OUT but linked PR"
                        f" for ticket {ticket_id} is MERGED — see #315."
                    ),
                )
            )

    return results


def _reap_session_by_selector(
    selector: str,
    *,
    authority: str = "operator",
    lane: str | None = None,
    proposed_action: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Reap a single session by exact short id or exact session name.

    Bypasses ``reap_policy`` — targeted reap is always authorized by the operator.

    Called directly from the CLI ``doctor --reap <SESSION>`` targeted path.
    Does NOT go through ``run_doctor`` to avoid changing its return type.
    Uses the same write primitives as the normal reconcile act phase.

    Returns True when the session was found (even if already terminal).
    Returns False when no session matches *selector*.
    """
    with sessions_lock():
        state = load_state()
        target = next(
            (s for s in state.sessions if selector in (s.id, s.name)),
            None,
        )
        if target is None:
            return False
        if target.status not in (
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
            SessionStatus.BACKGROUNDED,
        ):
            # Already terminal — idempotent.
            return True
        now = datetime.now(UTC)
        target.status = SessionStatus.COMPLETED
        target.completed_at = now
        target.completed_reason = CompletionReason.USER
        save_state(state)

    # Stop daemon surface after releasing sessions_lock.
    if target.surface_ref is not None:
        with contextlib.suppress(Exception):
            get_native_daemon_client().stop(target.surface_ref)

    # Revert owning TicketTask to PENDING — separate lock per established pattern.
    # When no RUNNING task exists, also try to collapse BLOCKED_ON_USER duplicates.
    ticket_id = ticket_id_for_session(target.name)
    mutations: list[str] = ["session_status_completed", "daemon_stopped"]
    if ticket_id:
        with dev_queue_lock():
            store = _deps.load_dev_queue()
            running_reverted = False
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    transition_task_status(task, QueueItemStatus.PENDING)
                    task.session_id = None
                    running_reverted = True
                    break
            if running_reverted:
                mutations.append("task_reverted_to_pending")
                save_dev_queue(store)
            else:
                # No RUNNING task — try to collapse dead-session BLOCKED_ON_USER rows.
                # Why: liveness is not re-checked here because _reap_session_by_selector
                # targets a specific session; BLOCKED_ON_USER tasks for the same ticket
                # are crash artifacts of that session. A live BLOCKED_ON_USER from a
                # concurrent session is an unusual race; _reap_wedge_findings checks
                # liveness before routing ticket_ids to this helper.
                # Deferred import: breaks the wedge ↔ loop_health circular dependency
                # (#1314) — wedge imports _gh_pr_states/_reap_session_by_selector from
                # this module at top level, so this reach back into wedge is
                # function-level (see pyproject.toml PLC0415 per-file-ignore).
                from cw.doctor.wedge import _collapse_blocked_on_user_tasks

                blocked_changed = _collapse_blocked_on_user_tasks(store, {ticket_id})
                if blocked_changed:
                    mutations.append("blocked_task_reverted_to_pending")
                    save_dev_queue(store)

    # Emit audit event after all locks released. record_event uses _inbox_lock
    # (separate file lock — no deadlock risk). Covers both automated 4c consumer
    # and manual cw doctor --reap so propose→authorize→act is fully traceable.
    record_event(
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
        payload={
            "session_id": target.id,
            "session_name": target.name,
            "client": target.client,
            "ticket_id": ticket_id,
            "lane": lane,
            "authority": authority,
            "proposed_action": proposed_action,
            "mutations": mutations,
        },
        correlation_id=correlation_id,
    )
    return True
