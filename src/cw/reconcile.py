"""Reconcile cw session state with the native Claude daemon.

A cw session is "live" if its ``surface_ref`` appears in the roster
returned by ``claude agents --json``. :func:`compute_drift` checks the
live set and returns a :class:`ReconcileReport` naming sessions whose
``surface_ref`` is absent. :func:`reconcile` applies the report.

The split is deliberate: ``compute_drift`` is pure and testable in
isolation; ``reconcile`` does the side-effecting work (state mutation,
event emission, dev-queue revert).

Transient-outage safety: ``reconcile`` refuses to mutate state when the
daemon cannot be reached (``_claude_agents_json`` raises
``CalledProcessError``) or returns an empty roster while the persisted
state still contains ACTIVE/IDLE sessions with surface refs. A transient
daemon hiccup would otherwise irreversibly mark every session as CRASHED.
``compute_drift`` stays pure and does not apply this guard.

Race note: ``reconcile`` does ``load_state → mutate → save_state`` without
a dedicated ``sessions.json`` file lock. This matches every other
``save_state`` call site in the codebase (``cw.session``, ``cw.cli``, …);
a unified state lock is a larger refactor tracked separately. In
practice the race window is the in-memory mutation between load and save,
and concurrent writers are rare in the single-user model this tool
targets.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult, parse_stdout
from cw.config import get_client, load_orchestrator_config, load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client
from cw.notify import fire_push_notification
from cw.worktree import remove_worktree

if TYPE_CHECKING:
    from cw.models import CwState, Session

_log = logging.getLogger(__name__)


# Session-name prefix for DAEMON sessions spawned by the dispatch loop. The
# full name is ``<client>/<AUTO_DEV_LABEL_PREFIX><ticket_id>``; reconciliation
# uses it to recover the ticket id when reverting phantom tickets. Defined
# here (not in ``cw.dispatch``) to avoid a circular import — ``cw.dispatch``
# imports :func:`reconcile` from this module.
AUTO_DEV_LABEL_PREFIX = "auto-dev/"

# Wall-clock budget for headless daemon sessions. Mirrors the constant in
# cli.py signal_stop; cli.py imports this value so there is a single source
# of truth. See GitHub issue #185.
#
# Bumped 30 → 60 min on 2026-05-25 after ticket #215 (tier=large, 11 files,
# 626 lines) hit the 30-min cap mid-implementation. Per-ticket / per-tier
# override mechanism tracked in #265.
HEADLESS_TIMEOUT_SECONDS = 3600  # 60 minutes

# Watchdog budget for DAEMON RUNNING sessions that have not yet emitted any
# AUTO_DEV_RESULT sentinel. Per-tier overrides in
# OrchestratorConfig.idle_watchdog_by_tier take precedence; per-ticket
# TicketTask.idle_watchdog_override beats both. After this window, reconcile
# flags the session as BLOCKED_ON_USER and fires a push notification.
IDLE_WATCHDOG_SECONDS = 900  # 15 minutes

DEFAULT_IDLE_RETRY_CAP = 2  # idle-stall auto-retries before parking (#384)

# How recently a session's transcript must have been modified to be considered
# actively making progress. If the newest .jsonl under the session's project
# dir was written within this window, the watchdog skips the session (GitHub
# #340). Conservative default: 2 min = well below the 15-min budget.
# 5 min — widened from 2 min (#384): covers short inter-turn gaps;
# subagent gaps handled by _awaiting_subagent below.
TRANSCRIPT_LIVENESS_WINDOW_SECONDS = 300

# A worker awaiting a subagent leaves the parent transcript quiet (subagent output
# only lands on return). Treat a pending tool_use at the transcript tail as alive
# for up to this long before concluding the subagent itself is hung. See #384.
SUBAGENT_LIVENESS_WINDOW_SECONDS = 900

# Paused-status value written to SESSION_NEEDS_ATTENTION events for sessions
# the watchdog flags (no sentinel ever emitted, daemon surface still live).
_SILENTLY_IDLE_REASON = "silently_idle"


# Grace window for a newly-spawned session to register with the daemon
# (`claude agents --json`). `claude --bg` spawn → daemon roster registration
# is async; reconciliation that runs in the same dispatch tick as the spawn
# would otherwise see the session as a phantom and reap it within 1 second.
# 30 seconds is comfortably above observed registration latency (~0.3-1.5s
# in dogfooding 2026-05-26) while still bounding how long a genuinely dead
# session can hide. See GitHub issue #271.
SPAWN_GRACE_SECONDS = 30


# Only these two statuses imply "the daemon should have a live session".
# BACKGROUNDED sessions intentionally have no surface (that's the whole point);
# COMPLETED is terminal. Both are ignored by reconciliation.
_LIVE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
    }
)


def _claude_agents_json() -> list[dict[str, object]]:
    """Call ``claude agents --json`` and return the parsed list.

    Raises ``subprocess.CalledProcessError`` when the daemon is not running.
    """
    proc = subprocess.run(
        ["claude", "agents", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    return data if isinstance(data, list) else []


@dataclass(frozen=True)
class ReconcileReport:
    """What reconciliation would do / did.

    ``phantom_session_ids`` — sessions whose ``surface_ref`` is not in the
    live set. Ordered by the original order in ``state.sessions``.
    ``phantom_session_names`` — session names in the same order as
    ``phantom_session_ids``. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)


def compute_drift(
    state: CwState,
    native_live: set[str],
    *,
    now: datetime | None = None,
) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in *native_live*, AND
    - its ``started_at`` is older than :data:`SPAWN_GRACE_SECONDS` ago
      (newly-spawned sessions are still registering with the daemon).

    *native_live* is the set of short session IDs reported by
    ``claude agents --json``; callers obtain it via :func:`_claude_agents_json`.

    *now* is injected for testability; defaults to ``datetime.now(UTC)``.

    This function does not mutate state. It also does not distinguish
    "backend reports zero live entries" from "backend is unreachable";
    that guard lives in :func:`reconcile`.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref in native_live:
            continue
        if session.started_at > cutoff:
            continue
        phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)


def ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(AUTO_DEV_LABEL_PREFIX):
        return tail[len(AUTO_DEV_LABEL_PREFIX) :]
    return None


def _looks_like_daemon_outage(
    state: CwState,
    daemon_errored: bool,
    native_live: set[str],
) -> bool:
    """True when the daemon appears unreachable and the state still has live refs.

    Fires when:
    - the daemon subprocess raised ``CalledProcessError`` (*daemon_errored*), OR
    - the daemon returned an empty roster while the persisted state has at
      least one ACTIVE/IDLE session with a ``surface_ref``.

    In either case, assume the daemon is transiently unreachable rather than
    "somehow every session died at once". Aborting here is the difference
    between a 5-second restart and permanent data loss.

    When *native_live* is non-empty the daemon is clearly reachable, so
    this returns False regardless of *daemon_errored*.
    """
    if not daemon_errored and native_live:
        return False
    return any(
        s.surface_ref is not None and s.status in _LIVE_STATUSES for s in state.sessions
    )


def resolve_headless_budget(
    task: TicketTask | None,
    session: Session,
    config: OrchestratorConfig,
) -> int:
    """Return the wall-clock budget (seconds) for *session*.

    Precedence (highest first):
    1. task.headless_timeout_override — explicit per-ticket escape hatch.
    2. session.last_result scope.tier — look up per-tier default in config.
    3. HEADLESS_TIMEOUT_SECONDS — global fallback (pre-Stage-1 or unknown tier).
    """
    if task is not None and task.headless_timeout_override is not None:
        return task.headless_timeout_override
    last_result = session.last_result
    if last_result is not None:
        tier: str | None = None
        try:
            scope = last_result.get("scope")
            if isinstance(scope, dict):
                tier = scope.get("tier")
        except (AttributeError, TypeError):
            pass
        if isinstance(tier, str):
            return config.headless_timeout_by_tier.get(tier, HEADLESS_TIMEOUT_SECONDS)
    return HEADLESS_TIMEOUT_SECONDS


def _is_headless(session: Session) -> bool:
    """Return True if session's worktree has a headless cw-context.json.

    Fail-open: returns False when worktree_path is None, or when the context
    file is missing or unreadable — a deleted worktree must not be falsely
    flagged as headless. Mirrors cli.py signal_stop at line 1003-1005.
    """
    if session.worktree_path is None:
        return False
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    try:
        context = json.loads(context_path.read_text())
        return bool(context.get("headless")) if isinstance(context, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


# AUTO_DEV_RESULT statuses for which a stalled/crashed session must NOT be
# re-dispatched: the work either shipped (a PR exists) or no work was needed.
# Salvaging these recovers the real disposition instead of mislabeling the
# session timed_out/crashed and re-running already-finished work. Non-success
# statuses (blocked, *_pending_*) keep the existing retry-on-timeout behavior.
# See GitHub issue #372.
_SALVAGE_TERMINAL_STATUSES: frozenset[str] = frozenset({"shipped", "no_op"})


def _assistant_text_from_transcript(path: Path) -> str:
    """Concatenate the text of every assistant message in a jsonl transcript.

    The AUTO_DEV_RESULT sentinel block is emitted inside an assistant text
    message, so joining assistant text reconstructs the input ``parse_stdout``
    would have seen on the worker's stdout. Returns "" on any read error.
    """
    parts: list[str] = []
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                parts.extend(
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
    except OSError:
        return ""
    return "\n".join(parts)


def _salvage_terminal_result(
    session: Session, *, after: datetime
) -> tuple[AutoDevResult, str] | None:
    """Recover a terminal-success AUTO_DEV_RESULT from the session's transcript.

    A headless session that emitted a valid sentinel and then stalled (e.g.
    sitting in ``wait_for_ci``) or crashed never reaches the wrapper's
    post-exit parse, so its disposition is lost. This recovers it directly
    from the transcript.

    Returns ``(result, claude_session_id)`` only when the newest transcript
    in the session's worktree — modified strictly after ``after`` (the session
    start, guarding the reused-worktree stale-transcript case, #358) — parses
    to an :class:`AutoDevResult` whose status is in
    :data:`_SALVAGE_TERMINAL_STATUSES`. Returns ``None`` otherwise.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return None
    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    newest = candidates[0]
    # Stale-transcript guard (#358): a reused worktree may retain a prior
    # run's transcript. Only trust output written since this session began.
    if datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC) <= after:
        return None
    result = parse_stdout(_assistant_text_from_transcript(newest))
    if (
        isinstance(result, AutoDevResult)
        and result.status in _SALVAGE_TERMINAL_STATUSES
    ):
        return result, newest.stem
    return None


def _session_project_dir(session: Session) -> Path | None:
    """Return the Claude project dir for *session*, or None if worktree path unset."""
    worktree = session.worktree_path
    if worktree is None:
        return None
    return Path.home() / ".claude" / "projects" / str(worktree).replace("/", "-")


def _transcript_recently_active(
    session: Session,
    now: datetime,
    *,
    window_seconds: int = TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
) -> bool:
    """Return True if the session's transcript was written within *window_seconds* ago.

    Reuses the project-dir layout from :func:`_salvage_terminal_result`.
    Returns False — permitting the watchdog to proceed — when no transcript
    is found (either the session is pre-first-write or path unavailable).
    See GitHub #340.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return False

    try:
        if session.claude_session_id is not None:
            transcript = project_dir / f"{session.claude_session_id}.jsonl"
            if not transcript.is_file():
                return False
            mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
            return (now - mtime).total_seconds() < window_seconds

        # claude_session_id not yet recorded — scan for the newest post-spawn .jsonl
        candidates = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return False
        newest = candidates[0]
        mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC)
        if mtime <= session.started_at:
            return False
        return (now - mtime).total_seconds() < window_seconds
    except OSError:
        return False


def _awaiting_subagent(session: Session, now: datetime) -> bool:
    """Return True if the worker is mid-tool/subagent (parent tail pending).

    A subagent's output only lands in the parent transcript when it returns,
    so the parent goes quiet during execution and mtime-based liveness
    false-positives. Detect the in-flight case: the last assistant turn is a
    ``tool_use`` with no following ``tool_result``, and that turn is within
    ``SUBAGENT_LIVENESS_WINDOW_SECONDS`` of *now* (a pending tool_use older than
    that is a hung subagent — not alive). See GitHub #384.

    Fail-open to False (permit the watchdog to proceed) on any read/parse error.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return False
    try:
        if session.claude_session_id is not None:
            transcript = project_dir / f"{session.claude_session_id}.jsonl"
        else:
            candidates = sorted(
                project_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                return False
            transcript = candidates[0]
        if not transcript.is_file():
            return False

        last_tool_use_ts: datetime | None = None
        saw_result_after_tool_use = True
        for raw in transcript.read_text().splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            if etype == "assistant" and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            ):
                ts = entry.get("timestamp")
                if isinstance(ts, str):
                    try:
                        last_tool_use_ts = datetime.fromisoformat(ts)
                    except ValueError:
                        last_tool_use_ts = None
                saw_result_after_tool_use = False
            elif etype == "user" and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                saw_result_after_tool_use = True

        if saw_result_after_tool_use or last_tool_use_ts is None:
            return False
        return (
            now - last_tool_use_ts
        ).total_seconds() < SUBAGENT_LIVENESS_WINDOW_SECONDS
    except OSError:
        return False


def _apply_salvaged_completion(
    session: Session,
    result: AutoDevResult,
    claude_session_id: str,
    *,
    now: datetime,
) -> None:
    """Mark ``session`` COMPLETED from a salvaged sentinel (like signal_completed)."""
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    session.last_result = result.model_dump(mode="json")
    if result.cost_usd is not None:
        session.cost_usd = result.cost_usd
    session.claude_session_id = claude_session_id


def _cleanup_timed_out_worktree(session: Session) -> None:
    """Remove a timed-out session's worktree so the re-dispatch starts clean.

    A timed-out DAEMON session has its ``TicketTask`` reverted to PENDING for
    re-dispatch. If its worktree is left on disk, ``create_worktree`` would
    reuse it (or, post-#404, refuse and spin) — either way feeding the retry a
    prior run's branch and commits. Removing it here means the next claim builds
    a fresh worktree from the current default branch. See GitHub issue #404.

    Best-effort: every failure is logged and swallowed. Worktree cleanup must
    never abort the reconcile sweep — a missing/renamed client, an
    already-gone directory, or a git error is non-fatal.
    """
    if not session.branch:
        return
    try:
        client = get_client(session.client)
        remove_worktree(client, session.branch, force=True)
    except (CwError, OSError) as exc:
        _log.warning(
            "worktree_cleanup_skip: %s/%s: %s",
            session.client,
            session.branch,
            exc,
        )


def revert_stalled_headless_sessions(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Transition stalled headless DAEMON sessions past budget to TIMED_OUT.

    Passive backstop complementing signal_stop's Stop-hook-driven check.
    signal_stop can only fire at Claude turn boundaries; a session whose agent
    stalled mid-turn (classifier denial, OOM, long subagent chain) produces no
    further Stop firings and would sit ACTIVE forever without this sweep.

    Runs unconditionally before the outage guard so a transient backend hiccup
    does not delay enforcement of the wall-clock budget. The sweep is purely
    time-based; surface liveness is irrelevant.

    Loads the dev queue once (read-only, no lock) for per-ticket budget lookups.
    The existing dev_queue_lock block for the revert step (below) still guards
    the read-write window.

    Calls save_state(state) when any sessions are transitioned — callers must
    not assume state is unchanged on return. On the phantom-handling path in
    reconcile(), save_state is called again later; this double-save is benign
    because save_state is idempotent over identical content.

    Returns the list of ticket IDs whose TicketTask was reverted to PENDING.
    See GitHub issue #185, #265.
    """
    # Read-only dev-queue load for budget lookups — no lock needed here.
    # Use the caller-supplied index when available (avoids a second filesystem
    # read when reconcile() shares one load across the stalled + idle sweeps).
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    pending: list[tuple[Session, str | None]] = []
    salvaged: list[tuple[Session, str | None, AutoDevResult]] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if not _is_headless(session):
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_headless_budget(task, session, config)
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget:
            continue
        # Before declaring a timeout, try to recover a terminal-success
        # sentinel the worker emitted before stalling (e.g. waiting on CI).
        # If found, the session is dispositioned by that sentinel and its
        # ticket is NOT reverted for re-dispatch. See GitHub issue #372.
        salvage = _salvage_terminal_result(session, after=session.started_at)
        if salvage is not None:
            result, claude_session_id = salvage
            _apply_salvaged_completion(session, result, claude_session_id, now=now)
            salvaged.append((session, ticket_id, result))
            continue
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        pending.append((session, ticket_id))

    if not pending and not salvaged:
        return []

    save_state(state)

    timed_out_ticket_ids = {tid for _, tid in pending if tid}
    salvaged_ticket_ids = {tid for _, tid, _ in salvaged if tid}
    reverted: list[str] = []
    if timed_out_ticket_ids or salvaged_ticket_ids:
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in timed_out_ticket_ids:
                    task.status = QueueItemStatus.PENDING
                    task.session_id = None
                    reverted.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in salvaged_ticket_ids:
                    # Terminal-success salvage: retire the task so the
                    # COMPLETED-silent backstop does not revert it to PENDING
                    # and re-dispatch already-finished work (#372).
                    task.status = QueueItemStatus.COMPLETED
                    changed = True
            if changed:
                save_dev_queue(store)

    for session, ticket_id in pending:
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "elapsed_seconds": (now - session.started_at).total_seconds(),
            "last_assistant_message_excerpt": "",
        }
        record_event(OrchestratorEventType.SESSION_TIMED_OUT, payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)
        # Stale-worktree cleanup: the task was reverted to PENDING above, so
        # the retry must not inherit this run's worktree state (#404).
        _cleanup_timed_out_worktree(session)

    for session, ticket_id, result in salvaged:
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    return reverted


def _has_terminal_sentinel(session: Session) -> bool:
    """True when the session has already emitted a terminal sentinel."""
    return session.last_result is not None


def resolve_idle_watchdog_budget(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the idle-watchdog budget (seconds) for a session's ticket.

    Precedence (highest first):
    1. task.idle_watchdog_override — explicit per-ticket escape hatch.
    2. task.scope_hint — look up per-tier default in config.
    3. IDLE_WATCHDOG_SECONDS — global fallback.
    """
    if task is None:
        return IDLE_WATCHDOG_SECONDS
    if task.idle_watchdog_override is not None:
        return task.idle_watchdog_override
    if task.scope_hint is not None:
        tier_budget = config.idle_watchdog_by_tier.get(task.scope_hint)
        if tier_budget is not None:
            return tier_budget
    return IDLE_WATCHDOG_SECONDS


def resolve_idle_retry_cap(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the idle-stall auto-retry cap for a session's ticket.

    Precedence: task.scope_hint per-tier override, else the global default.
    See GitHub issue #384.
    """
    if task is None:
        return DEFAULT_IDLE_RETRY_CAP
    if task.scope_hint is not None:
        tier_cap = config.idle_retry_cap_by_tier.get(task.scope_hint)
        if tier_cap is not None:
            return tier_cap
    return DEFAULT_IDLE_RETRY_CAP


def flag_silently_idle_daemon_sessions(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Flag DAEMON RUNNING sessions idle past the watchdog budget with no sentinel.

    These are sessions the wrapper never got a chance to signal — typically
    because the child process self-backgrounded a subagent and exited before
    the subagent returned (GitHub #105, #121). They appear ACTIVE/IDLE in cw
    state while producing no output.

    Only targets sessions whose ``surface_ref`` is currently in *native_live*
    (the daemon still has them). Sessions with a dead surface ref are handled
    by the phantom sweep → PENDING for retry.

    Returns list of ticket IDs whose queue task was set to BLOCKED_ON_USER.
    """
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    recover: list[tuple[Session, str | None]] = []
    park: list[tuple[Session, str | None]] = []
    salvaged: list[tuple[Session, str | None, AutoDevResult]] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if _has_terminal_sentinel(session):
            continue
        # Only target sessions whose daemon surface is still live — phantom
        # sessions (dead surface) are handled by the crashed-phantom sweep.
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        elapsed = (now - session.started_at).total_seconds()
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_idle_watchdog_budget(task, config)
        if elapsed < budget:
            continue
        # Liveness check: skip workers that are still making progress. A recent
        # transcript write (#340) OR an in-flight subagent (#384 — parent
        # transcript goes quiet while a subagent runs) both count as alive.
        if _transcript_recently_active(session, now) or _awaiting_subagent(
            session, now
        ):
            continue
        # Before parking or recovering, try to find a terminal-success sentinel
        # the worker emitted while waiting on CI (e.g. shipped-then-wait_for_ci).
        # If found, the session is dispositioned by that sentinel. (#398)
        salvage = _salvage_terminal_result(session, after=session.started_at)
        if salvage is not None:
            result, claude_session_id = salvage
            _apply_salvaged_completion(session, result, claude_session_id, now=now)
            salvaged.append((session, ticket_id, result))
            continue
        cap = resolve_idle_retry_cap(task, config)
        if task is not None and task.attempts < cap:
            recover.append((session, ticket_id))
        else:
            park.append((session, ticket_id))

    if not recover and not park and not salvaged:
        return []

    # Auto-recover: retire the session and revert its task for re-dispatch.
    for session, _ in recover:
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
    # Park: flag-only (preserves #348 — no daemon stop, session stays ACTIVE).
    for session, _ in park:
        session.last_result = {"paused_status": _SILENTLY_IDLE_REASON}

    # Write session to disk BEFORE queue mutation so a crash between the two
    # leaves state set on disk — watchdog skips on subsequent ticks. (#324, #348)
    save_state(state)

    recovered_ids = {tid for _, tid in recover if tid}
    parked_ids = {tid for _, tid in park if tid}
    salvaged_ticket_ids = {tid for _, tid, _ in salvaged if tid}
    blocked: list[str] = []
    if recovered_ids or parked_ids or salvaged_ticket_ids:
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in recovered_ids:
                    task.status = QueueItemStatus.PENDING
                    task.session_id = None
                    changed = True
                elif task.ticket_id in parked_ids:
                    task.status = QueueItemStatus.BLOCKED_ON_USER
                    blocked.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in salvaged_ticket_ids:
                    task.status = QueueItemStatus.COMPLETED
                    changed = True
            if changed:
                save_dev_queue(store)

    # Recovery: stop the dead surface + emit a distinguishable timeout event.
    # Payload mirrors the wall-clock timeout path; cause distinguishes the source.
    for session, ticket_id in recover:
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)
        # Stale-worktree cleanup: this task was reverted to PENDING above for
        # re-dispatch, so the retry must start from a fresh worktree (#404).
        _cleanup_timed_out_worktree(session)
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": session.claude_session_id,
                "elapsed_seconds": (now - session.started_at).total_seconds(),
                "cause": "idle_stall_recovered",
                "last_assistant_message_excerpt": "",
            },
        )

    # Park: needs-attention for operator disposition (unchanged from #348).
    for session, ticket_id in park:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _SILENTLY_IDLE_REASON,
                "breadcrumbs": "",
                "crashed": False,
            },
        )
        fire_push_notification(session.name, session.client)

    for session, ticket_id, result in salvaged:
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    return blocked


def reconcile() -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_daemon_outage` matches — a transient daemon hiccup
    must not trigger mass-reaping.

    Partial-failure note: state and the dev queue are separate files. If
    ``save_state`` succeeds but the subsequent dev-queue update raises,
    the session will be COMPLETED while its TicketTask stays RUNNING.
    The next ``reconcile()`` call will not pick this up because the
    session is no longer ACTIVE/IDLE — so a stranded RUNNING task can
    only be recovered by explicit operator action. This is an acceptable
    tradeoff for a file-based, single-user tool.
    """
    state = load_state()
    now = datetime.now(UTC)

    # Passive budget sweep: catches headless DAEMON sessions whose agent
    # stalled mid-turn and produced no further Stop hook firings. Runs before
    # the outage guard so a daemon hiccup does not delay budget enforcement.
    # See GitHub issue #185.
    orchestrator_config = load_orchestrator_config()
    # Load dev queue once here; pass to both sweeps to avoid a duplicate
    # filesystem read within the same reconcile tick. See GitHub issue #326.
    shared_task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    stalled_reverted = revert_stalled_headless_sessions(
        state, now=now, config=orchestrator_config, task_by_ticket=shared_task_by_ticket
    )

    try:
        # `claude agents --json` returns sessionId as a full UUID
        # (e.g. "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"). cw's surface_ref
        # is the 8-char short id (prefix of the UUID) — same shape
        # `claude --bg` returns at spawn. Normalize to short id for
        # comparison; otherwise reconcile sees every native session as a
        # phantom because UUID != short-id.
        native_live = {
            sid[:8]
            for a in _claude_agents_json()
            if isinstance(sid := a.get("sessionId"), str)
        }
        daemon_errored = False
    except subprocess.CalledProcessError:
        native_live = set()
        daemon_errored = True
    if _looks_like_daemon_outage(state, daemon_errored, native_live):
        return ReconcileReport(reverted_ticket_ids=stalled_reverted)

    silently_idle_ticket_ids = flag_silently_idle_daemon_sessions(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )

    drift = compute_drift(state, native_live, now=now)
    if not drift.phantom_session_ids:
        # No phantom sessions to reap, but still run the TIMED_OUT and
        # COMPLETED-silent sweeps so any tasks whose sessions completed or
        # timed out without reverting their queue task are recovered.
        timed_out_ticket_ids = revert_timed_out_tasks()
        completed_silent_ticket_ids = revert_completed_silent_tasks()
        all_reverted = list(
            dict.fromkeys(
                stalled_reverted
                + silently_idle_ticket_ids
                + timed_out_ticket_ids
                + completed_silent_ticket_ids
            )
        )
        return ReconcileReport(reverted_ticket_ids=all_reverted)

    phantom_set = set(drift.phantom_session_ids)
    ticket_ids_to_revert: list[str] = []
    salvaged_ticket_ids: list[str] = []
    pending_events: list[dict[str, object]] = []
    phantom_names: list[str] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        ticket_id = ticket_id_for_session(session.name)
        # Recover a terminal-success sentinel before declaring the phantom
        # crashed, so already-shipped work is not re-dispatched (#372).
        salvage = (
            _salvage_terminal_result(session, after=session.started_at)
            if session.origin is SessionOrigin.DAEMON
            else None
        )
        if salvage is not None:
            result, claude_session_id = salvage
            _apply_salvaged_completion(session, result, claude_session_id, now=now)
            phantom_names.append(session.name)
            if ticket_id:
                salvaged_ticket_ids.append(ticket_id)
            salvaged_payload: dict[str, object] = {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "crashed": False,
                "salvaged": True,
                "status": result.status,
            }
            if ticket_id:
                salvaged_payload["ticket_id"] = ticket_id
            pending_events.append(salvaged_payload)
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        phantom_names.append(session.name)
        if ticket_id and session.origin is SessionOrigin.DAEMON:
            ticket_ids_to_revert.append(ticket_id)
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": True,
        }
        if ticket_id:
            payload["ticket_id"] = ticket_id
        pending_events.append(payload)

    save_state(state)
    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    reverted: list[str] = []
    if ticket_ids_to_revert or salvaged_ticket_ids:
        revert_set = set(ticket_ids_to_revert)
        salvaged_set = set(salvaged_ticket_ids)
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in revert_set:
                    task.status = QueueItemStatus.PENDING
                    # Drop the stamp from the prior (now-crashed) session so
                    # the next dispatch_tick can re-stamp with the freshly
                    # spawned session_id without a window where the task
                    # carries a stale id. See GitHub issue #97.
                    task.session_id = None
                    reverted.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in salvaged_set:
                    # Terminal-success salvage: retire the task instead of
                    # reverting, so shipped/no_op work is not re-dispatched (#372).
                    task.status = QueueItemStatus.COMPLETED
                    changed = True
            if changed:
                save_dev_queue(store)

    # Sweep for TIMED_OUT and DAEMON-COMPLETED sessions whose owning TicketTask
    # was not yet reverted (e.g. signal_stop crashed after setting status but
    # before touching the queue, or a headless session completed without
    # the dispatch consumer processing it). TIMED_OUT/COMPLETED sessions are
    # already terminal so no state mutation is needed — queue revert only.
    timed_out_ticket_ids = revert_timed_out_tasks()
    completed_silent_ticket_ids = revert_completed_silent_tasks()
    all_reverted = list(
        dict.fromkeys(
            stalled_reverted
            + silently_idle_ticket_ids
            + reverted
            + timed_out_ticket_ids
            + completed_silent_ticket_ids
        )
    )

    return ReconcileReport(
        phantom_session_ids=drift.phantom_session_ids,
        phantom_session_names=phantom_names,
        reverted_ticket_ids=all_reverted,
    )


def _revert_running_tasks_for_sessions(session_ids: set[str]) -> list[str]:
    """Revert RUNNING TicketTasks whose ``session_id`` is in *session_ids*.

    Shared helper for the per-status revert wrappers. Acquires
    ``dev_queue_lock`` for the read+write window; writes only when at least
    one task was reverted. Returns the list of reverted ticket IDs.
    """
    if not session_ids:
        return []

    reverted: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            task.status = QueueItemStatus.PENDING
            task.session_id = None
            reverted.append(task.ticket_id)
        if reverted:
            save_dev_queue(store)
    return reverted


def revert_timed_out_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is TIMED_OUT.

    Called during :func:`reconcile` as a backstop for the case where
    ``signal_stop`` crashed after writing TIMED_OUT status but before
    reverting the dev-queue task. Returns the list of ticket IDs reverted.
    """
    state = load_state()
    session_ids = {
        s.id
        for s in state.sessions
        if s.status == SessionStatus.TIMED_OUT and s.origin is SessionOrigin.DAEMON
    }
    return _revert_running_tasks_for_sessions(session_ids)


def revert_completed_silent_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is DAEMON COMPLETED.

    Called during :func:`reconcile` as a backstop for sessions that completed
    without reverting their dev-queue task (e.g. the session wrote COMPLETED
    status but the dispatch consumer had not yet processed it). Returns the
    list of ticket IDs reverted.
    """
    state = load_state()
    session_ids = {
        s.id
        for s in state.sessions
        if s.status == SessionStatus.COMPLETED and s.origin is SessionOrigin.DAEMON
    }
    return _revert_running_tasks_for_sessions(session_ids)
