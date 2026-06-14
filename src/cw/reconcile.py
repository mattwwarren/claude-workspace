"""Reconcile cw session state with the native Claude daemon.

A cw session is "live" if its ``surface_ref`` appears in the roster
returned by ``claude agents --json``.  ``reconcile()`` is split into two
phases that run under ``sessions_lock`` (see ADR-0005):

**Detect phase** — pure classification, no state writes.
Three sweeps (stalled, idle/phantom, post-salvage) each call a
``_detect_*`` helper that returns :class:`ReapCandidate` objects.  After
each sweep ``_emit_reap_proposed`` fires
:attr:`OrchestratorEventType.SESSION_REAP_PROPOSED`
for every candidate whose :attr:`Session.reap_proposed_at` is ``None``,
stamping that field to deduplicate across ticks.

Emitting before the act phase keeps the event visible even when the act
phase is suppressed by ``signal_only`` — consumers (the lane's ORCHESTRATE
session or the operator) see the proposal and decide.

**Act phase** — gated by ``reap_policy`` (ADR-0006).
Under the default ``signal_only`` policy the act phase routes the owning
:class:`TicketTask` to ``BLOCKED_ON_USER`` but performs no destructive
mutation (no daemon stop, no worktree removal, no RUNNING→PENDING revert).
Destructive acts require either ``reap_policy: auto`` on the session's lane
*or* an explicit operator command (``cw doctor --reap``).

Lock note: act-phase helpers call ``save_state()`` directly — not
``mutate_state()`` (ADR-0005) — because ``_reconcile_locked()`` already
holds ``sessions_lock`` and re-acquiring the same per-open-fd flock would
self-deadlock (#387).  Serialisation against concurrent Stop-hook writes
is enforced by the held lock.

Transient-outage guard: ``reconcile`` skips the act phase entirely when
``claude agents --json`` fails or returns an empty roster while
ACTIVE/IDLE sessions with surface refs exist — a hiccup must not mass-reap.

``_reconcile_locked()`` runs the above sequence while ``sessions_lock``
is held; helper functions called from within it (``revert_stalled_*``,
``flag_silently_idle_*``) ``save_state`` directly without re-acquiring.

See ADR-0005 (single state lock) and ADR-0006 (reaping is gated by an
authority) for the invariants this module enforces.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from cw._util import claude_project_dir
from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    PAUSED_FOR_USER_INPUT_STATUSES,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
    parse_stdout,
)
from cw.config import (
    get_client,
    load_effective_clients,
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import USAGE_LIMIT_RE, CwError
from cw.gh import (
    TIMED_OUT_MERGED_LOOKBACK_DAYS,
    pr_exists_for_branch,
    pr_is_merged_for_ticket,
)
from cw.models import (
    DEFAULT_LANE,
    ClientConfig,
    CompletionReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client
from cw.notify import fire_push_notification
from cw.worktree import (
    _checked_out_branch,
    _has_commits_beyond_base,
    remove_worktree,
    worktree_has_unsaved_work,
    worktree_path_for,
)

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
# 1800 (30 min) — widened from 900 (#544): a large refactor can run a single tool
# call quietly for 20-30 min; reaping at 15 min false-killed live workers (#543).
# Data-safety: benefit-of-the-doubt is only extended when a *recent* pending
# tool_use is present (strong alive signal). No pending tool_use → reaped normally.
SUBAGENT_LIVENESS_WINDOW_SECONDS = 1800

# Paused-status value written to SESSION_NEEDS_ATTENTION events for sessions
# the watchdog flags (no sentinel ever emitted, daemon surface still live).
_SILENTLY_IDLE_REASON = "silently_idle"
_SALVAGE_SKIP_REASON = "park_marker_blocks_salvage"
# Reason tag written to SESSION_COMPLETED events when a TIMED_OUT session's PR
# was found MERGED via issue-linkage (timed_out-merged auto-complete, #488).
_TIMED_OUT_MERGED_REASON = "timed_out_merged"
# Paused-status written to SESSION_NEEDS_ATTENTION events when a session's
# worktree has unsaved work and the task is routed to BLOCKED_ON_USER instead
# of being retried automatically (GitHub issue #421).
_DIRTY_WORKTREE_REASON = "dirty_worktree"
# Reason tag written to SESSION_COMPLETED events when a phantom/stalled/idle
# session's PR was found MERGED before its task was reverted to PENDING.
# Prevents re-dispatch of already-shipped tickets (GitHub issue #637).
_PHANTOM_REAP_MERGED_REASON = "phantom_reap_merged"
# Paused-status written to SESSION_NEEDS_ATTENTION events when the gh
# availability or PR-merged check returns an inconclusive result and the
# task is routed to BLOCKED_ON_USER rather than being reverted to PENDING
# (fail-closed on ambiguous world state; GitHub issue #637).
_GH_CHECK_BLOCKED_REASON = "gh_check_blocked"
# Git-state salvage constants (GitHub issue #497).
_NEEDS_SALVAGE_REASON = "needs_salvage"
_SALVAGE_KIND_GIT_STATE = "git_state_salvage"
_STAGE_REVIEW_COMPLETE = "s3_review_complete"
_SALVAGE_PR_TITLE_TEMPLATE = "chore: salvage auto-dev branch for #{ticket_id}"
_SALVAGE_PR_BODY_TEMPLATE = (
    "Auto-salvaged by reconcile after the session was reaped post-review.\n\n"
    "The worker reached Stage 3 review (clean) and was reaped before opening a PR. "
    "Review this branch and merge when satisfied.\n\n"
    "Ticket: #{ticket_id}"
)

# Cause tags for SESSION_TIMED_OUT events emitted by the idle watchdog (#486).
# idle_stall_recovered — watchdog fired but no usage-limit message found.
# usage_limit_cutoff   — transcript contains a Claude session/usage-limit message.
# USAGE_LIMIT_RE is imported from cw.exceptions (centralized there for reuse).
_CAUSE_IDLE_STALL = "idle_stall_recovered"
_CAUSE_USAGE_LIMIT = "usage_limit_cutoff"

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


def _queue_status_for_salvaged(result: AutoDevResult) -> QueueItemStatus:
    """Map a salvaged AutoDevResult to the appropriate QueueItemStatus."""
    if result.status in PAUSED_FOR_USER_INPUT_STATUSES:
        return QueueItemStatus.BLOCKED_ON_USER
    return QueueItemStatus.COMPLETED


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
    ``completed_ticket_ids`` — ticket IDs whose PENDING TicketTasks were
    auto-completed because their TIMED_OUT session's PR merged. Populated
    by :func:`reconcile` via :func:`complete_timed_out_merged_tasks`.
    ``usage_limited`` — True when any reaped session had
    cause=usage_limit_cutoff during this reconcile pass. Signals the
    dispatch loop to enter back-off mode.
    ``salvaged_ticket_ids`` — ticket IDs auto-completed via the HIGH-path
    git-state salvage (committed-but-no-PR reaped sessions). Populated by
    :func:`salvage_committed_no_pr_sessions`. See GitHub issue #497.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)
    completed_ticket_ids: list[str] = field(default_factory=list)
    usage_limited: bool = False
    salvaged_ticket_ids: list[str] = field(default_factory=list)


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
        if session.purpose is SessionPurpose.ORCHESTRATE:
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


def _backfill_claude_session_ids(
    state: CwState, surface_to_full: dict[str, str]
) -> int:
    """Backfill claude_session_id from the daemon roster for DAEMON sessions.

    Called once per reconcile tick, after the outage guard. Returns the number
    of sessions updated; saves state when non-zero.
    """
    count = 0
    for session in state.sessions:
        if (
            session.claude_session_id is None
            and session.surface_ref is not None
            and session.status in _LIVE_STATUSES
            and session.origin is SessionOrigin.DAEMON
        ):
            from_agents = surface_to_full.get(session.surface_ref)
            resolved = from_agents or _csid_from_transcript(session)
            if resolved is not None:
                session.claude_session_id = resolved
                count += 1
    if count:
        _log.debug("Backfilled claude_session_id for %d session(s)", count)
        save_state(state)
    return count


def resolve_headless_budget(
    task: TicketTask | None,
    session: Session | None,
    config: OrchestratorConfig,
) -> int:
    """Return the wall-clock budget (seconds) for *session*.

    Precedence (highest first):
    1. task.headless_timeout_override — explicit per-ticket escape hatch.
    2. session.last_result scope.tier — look up per-tier default in config.
    2.5. task.scope_hint — fallback when last_result tier is unavailable (#314).
    3. HEADLESS_TIMEOUT_SECONDS — global fallback (pre-Stage-1 or unknown tier).

    *session* may be None when called from the dispatch path (pre-spawn,
    no session object exists yet). In that case step 2 is skipped and
    step 2.5 fires if task.scope_hint is set.
    """
    if task is not None and task.headless_timeout_override is not None:
        return task.headless_timeout_override
    last_result = session.last_result if session is not None else None
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
    # Step 2.5: last_result is None or had no extractable tier — try scope_hint.
    # Fires for sessions that haven't yet emitted a sentinel (pre-Stage-1) and
    # for sessions whose last result had no scope.tier. Fixes the dogfood reap
    # incident where large-tier sessions fell back to the 60-min global default
    # because their first spawn had no last_result (#314).
    if task is not None and task.scope_hint is not None:
        return config.headless_timeout_by_tier.get(
            task.scope_hint, HEADLESS_TIMEOUT_SECONDS
        )
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


# Alias so _salvage_terminal_result can reference the shared constant by the
# private-looking name used throughout this module. The real definition lives
# in auto_dev_result.py as SALVAGE_TERMINAL_STATUSES — single source of truth
# so reconcile.py and cli.py cannot drift apart. See GitHub issues #372, #431.
_SALVAGE_TERMINAL_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES

# Constants for _apply_sentinel_to_task (moved from cli.py; shared by both
# signal_stop and the ROUTE_EMITTED_SENTINEL reconcile path). See GitHub #578.
_VALIDATION_FAILED_MAX_ATTEMPTS = 3
_DETERMINISTIC_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED}
)
_TRANSIENT_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_NO_RESULT_EMITTED}
)
_TERMINAL_NO_RETRY_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES


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


def _detect_usage_limit(session: Session) -> bool:
    """Return True iff the newest post-start transcript contains a usage-limit message.

    Uses :func:`_locate_session_transcript` for precise per-session lookup
    (surface_ref-prefix glob, #541).  Returns False (never raises) when the
    project dir is absent, no matching .jsonl exists, or the transcript
    predates the session start.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return False
    return bool(USAGE_LIMIT_RE.search(_assistant_text_from_transcript(transcript)))


def _salvage_terminal_result(
    session: Session,
) -> tuple[AutoDevResult, str] | None:
    """Recover a terminal-success AUTO_DEV_RESULT from the session's transcript.

    A headless session that emitted a valid sentinel and then stalled (e.g.
    sitting in ``wait_for_ci``) or crashed before session lifecycle completion
    may have its disposition lost. This recovers it directly from the transcript.

    Returns ``(result, claude_session_id)`` only when the transcript located
    by :func:`_locate_session_transcript` (surface_ref-prefix glob, #541) —
    which enforces mtime > started_at, guarding the reused-worktree
    stale-transcript case (#358) — parses to an :class:`AutoDevResult` whose
    status is in :data:`_SALVAGE_TERMINAL_STATUSES`. Returns ``None``
    otherwise.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return None
    result = parse_stdout(_assistant_text_from_transcript(transcript))
    if (
        isinstance(result, AutoDevResult)
        and result.status in _SALVAGE_TERMINAL_STATUSES
    ):
        return result, transcript.stem
    return None


def _parse_any_sentinel_from_transcript(
    session: Session,
) -> tuple[AutoDevResult | BlockedResult, str] | None:
    """Parse any sentinel from the transcript, regardless of status.

    Like :func:`_salvage_terminal_result` but applies no status filter — returns
    the result for any valid parse including PAUSED_FOR_USER_INPUT statuses that
    :func:`_salvage_terminal_result` would skip.  Returns None only when no
    sentinel framing is present (BLOCKER_REASON_NO_RESULT_EMITTED).

    Used by the ROUTE_EMITTED_SENTINEL detection path for sessions where the
    sentinel was emitted but the Stop hook never fired.  See GitHub #578.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return None
    result = parse_stdout(_assistant_text_from_transcript(transcript))
    if (
        isinstance(result, BlockedResult)
        and result.blocker.reason == BLOCKER_REASON_NO_RESULT_EMITTED
    ):
        return None
    return result, transcript.stem


def _apply_sentinel_to_task(
    ticket_id: str,
    cw_session_id: str,
    sentinel: AutoDevResult | BlockedResult,
) -> None:
    """Update the matching dev-queue task based on the sentinel result.

    Shared by signal_stop (cli.py) and the ROUTE_EMITTED_SENTINEL reconcile
    path so both use the same sentinel→QueueItemStatus mapping.  Called before
    marking the session COMPLETED so the task is in its terminal state when
    revert_completed_silent_tasks runs.  See GitHub issues #251, #578.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        target: TicketTask | None = None
        for task in store.tasks:
            if (
                task.ticket_id == ticket_id
                and task.session_id == cw_session_id
                and task.status == QueueItemStatus.RUNNING
            ):
                target = task
                break
        if target is None:
            return

        if isinstance(sentinel, AutoDevResult):
            if sentinel.status in PAUSED_FOR_USER_INPUT_STATUSES:
                target.status = QueueItemStatus.BLOCKED_ON_USER
            elif sentinel.status in _TERMINAL_NO_RETRY_STATUSES:
                target.status = QueueItemStatus.COMPLETED
            elif sentinel.status == "blocked":
                retry = (
                    sentinel.blocker is not None
                    and sentinel.blocker.retry_eligible is True
                )
                if retry:
                    target.status = QueueItemStatus.PENDING
                    target.session_id = None
                else:
                    target.status = QueueItemStatus.COMPLETED
            else:
                target.status = QueueItemStatus.COMPLETED
        else:
            # BlockedResult: sentinel failed to parse or was malformed.
            if sentinel.blocker.reason in _DETERMINISTIC_PARSE_FAILURES:
                target.status = QueueItemStatus.FAILED
            elif sentinel.blocker.reason == BLOCKER_REASON_VALIDATION_FAILED:
                if target.attempts >= _VALIDATION_FAILED_MAX_ATTEMPTS:
                    target.status = QueueItemStatus.FAILED
                else:
                    target.status = QueueItemStatus.PENDING
                    target.session_id = None
            elif sentinel.blocker.reason in _TRANSIENT_PARSE_FAILURES:
                target.status = QueueItemStatus.PENDING
                target.session_id = None
            else:
                target.status = QueueItemStatus.COMPLETED

        save_dev_queue(store)


def _session_project_dir(session: Session) -> Path | None:
    """Return the Claude project dir for *session*, or None if worktree path unset."""
    worktree = session.worktree_path
    if worktree is None:
        return None
    return claude_project_dir(worktree)


def _locate_session_transcript(session: Session) -> Path | None:
    """Return the session's transcript path, or None if not locatable.

    Resolution order:
    1. ``claude_session_id`` set and ``<project_dir>/<csid>.jsonl`` exists →
       return that path directly (mtime guard not needed; csid is exact).
    2. ``surface_ref`` set → newest ``<project_dir>/<surface_ref>*.jsonl``
       with mtime strictly after ``session.started_at``, else None
       (reused-worktree stale-transcript guard, #358/#372).
    3. No project_dir, or neither identifier set → None.

    The ``surface_ref``-prefix glob in step 2 excludes sibling transcripts
    from other sessions that share the same project dir (reused worktree).
    Do NOT fall back to an unscoped ``*.jsonl`` glob — that would silently
    read a different session's transcript.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return None
    try:
        if session.claude_session_id is not None:
            path = project_dir / f"{session.claude_session_id}.jsonl"
            return path if path.is_file() else None
        if session.surface_ref is not None:
            candidates = sorted(
                project_dir.glob(f"{session.surface_ref}*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                return None
            newest = candidates[0]
            mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC)
            if mtime <= session.started_at:
                return None
            return newest
    except OSError:
        return None
    return None


def _csid_from_transcript(session: Session) -> str | None:
    """Return claude_session_id from the transcript filename, or None.

    Thin wrapper around :func:`_locate_session_transcript`.  The transcript
    is named ``<project_dir>/<full-csid>.jsonl`` where
    ``full-csid[:8] == surface_ref``; the stem is therefore the full csid.
    """
    path = _locate_session_transcript(session)
    if path is None:
        return None
    csid = path.stem
    _log.debug(
        "Resolved claude_session_id=%s for session %s via transcript fallback",
        csid,
        session.id,
    )
    return csid


def _detect_post_review_clean(session: Session) -> bool:
    """Return True iff the event bus has a post-review-clean marker for this session.

    Reads STAGE_ENTERED events from the inbox and checks for an event with
    payload["stage"] == _STAGE_REVIEW_COMPLETE correlated to session.id,
    with a time-window guard (event after session.started_at).

    Returns False on any error — conservative default.
    """
    if session.worktree_path is None:
        return False
    try:
        events = read_events(
            event_types=[OrchestratorEventType.STAGE_ENTERED],
            since_ts=session.started_at,
        )
    except Exception:  # noqa: BLE001
        return False
    for ev in events:
        session_id = ev.payload.get("session_id")
        stage = ev.payload.get("stage")
        if session_id == session.id and stage == _STAGE_REVIEW_COMPLETE:
            return True
    return False


def _transcript_recently_active(
    session: Session,
    now: datetime,
    *,
    window_seconds: int = TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
) -> bool:
    """Return True if the session's transcript was written within *window_seconds* ago.

    Uses :func:`_locate_session_transcript` for precise per-session lookup
    (surface_ref-prefix glob, #541).  Returns False — permitting the watchdog
    to proceed — when no transcript is found (pre-first-write or path
    unavailable).  See GitHub #340.
    """
    try:
        transcript = _locate_session_transcript(session)
        if transcript is None:
            return False
        mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
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
    # Why: _locate_session_transcript applies the mtime > started_at guard
    # uniformly (#541).  A stale transcript (from a prior run in a reused
    # worktree) that previously could cause a false-positive "subagent pending"
    # signal now returns None → this function returns False (fail-open).
    # The behavior change is intentional and conservative.
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return False
    try:
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
    """Mark ``session`` COMPLETED from a salvaged sentinel (like signal_stop)."""
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    session.last_result = result.model_dump(mode="json")
    if result.cost_usd is not None:
        session.cost_usd = result.cost_usd
    session.claude_session_id = claude_session_id


def _cleanup_timed_out_worktree(
    session: Session,
    ticket_id: str | None = None,
) -> None:
    """Remove a timed-out session's worktree so the re-dispatch starts clean.

    A timed-out DAEMON session has its ``TicketTask`` reverted to PENDING for
    re-dispatch. If its worktree is left on disk, ``create_worktree`` would
    reuse it (or, post-#404, refuse and spin) — either way feeding the retry a
    prior run's branch and commits. Removing it here means the next claim builds
    a fresh worktree from the current default branch. See GitHub issue #404.

    Dirty-check guard (#425): if the worktree contains uncommitted changes or
    unpushed commits the removal is SKIPPED.  Instead the owning task is flipped
    from PENDING back to BLOCKED_ON_USER so the operator can inspect the tree
    before it is removed.  *ticket_id* is required for the BLOCKED_ON_USER flip;
    when omitted the skip is logged but the queue is not mutated.

    Best-effort: every failure is logged and swallowed. Worktree cleanup must
    never abort the reconcile sweep — a missing/renamed client, an
    already-gone directory, or a git error is non-fatal.
    """
    if not session.branch:
        return
    try:
        client = get_client(session.client)
        if worktree_has_unsaved_work(client, session.branch):
            wt_path = str(worktree_path_for(client, session.branch))
            _log.warning(
                "worktree_cleanup_skip_dirty: %s/%s has unsaved work"
                " — leaving worktree at %s for operator inspection"
                " (ticket=%s)",
                session.client,
                session.branch,
                wt_path,
                ticket_id,
            )
            if ticket_id:
                with dev_queue_lock():
                    store = load_dev_queue()
                    for task in store.tasks:
                        if (
                            task.ticket_id == ticket_id
                            and task.status == QueueItemStatus.PENDING
                        ):
                            task.status = QueueItemStatus.BLOCKED_ON_USER
                            save_dev_queue(store)
                            break
            return
        remove_worktree(client, session.branch, force=True)
    except (CwError, OSError) as exc:
        _log.warning(
            "worktree_cleanup_skip: %s/%s: %s",
            session.client,
            session.branch,
            exc,
        )
    else:
        # Audit breadcrumb for a destructive (force=True) removal — a skip is
        # logged above, so a successful reap leaves a matching record (#404).
        _log.info("worktree_cleanup_ok: %s/%s", session.client, session.branch)


def _compute_worktree_dirty(client_name: str, branch: str | None) -> bool:
    """Return True when the worktree has unpushed commits or uncommitted changes.

    Fail-safe: returns False when branch is None or empty, the client config is
    absent, or any other error occurs — mirrors _cleanup_timed_out_worktree's
    pattern.
    """
    if not branch:
        return False
    try:
        client = get_client(client_name)
        return worktree_has_unsaved_work(client, branch)
    except Exception:  # noqa: BLE001
        return False


def _worktree_dirty_by_path(client_name: str, worktree_path: Path | None) -> bool:
    """Return True if the worktree at *worktree_path* has unsaved work.

    Uses worktree_path (always set on DAEMON sessions) instead of
    session.branch (always None on DAEMON sessions, making the branch-based
    check a production no-op).  Mirror _compute_worktree_dirty's fail-safe:
    returns False on any error, None path, or missing path.
    """
    if not worktree_path:
        return False
    try:
        branch = _checked_out_branch(worktree_path)
        if not branch:
            return False
        client = get_client(client_name)
        return worktree_has_unsaved_work(client, branch)
    except Exception:  # noqa: BLE001
        return False


def _detect_stalled_candidates(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Pure classification phase for stalled headless DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. See GitHub #552, ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if not _is_headless(session):
            continue
        # Park-marker check: sessions already parked by the idle watchdog.
        # Detect returns SKIP_PARKED candidate; act emits the skip event.
        if isinstance(session.last_result, dict) and session.last_result.get(
            "paused_status"
        ) in (_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON):
            actual_paused_status = session.last_result.get("paused_status")
            ticket_id = ticket_id_for_session(session.name)
            # Stamp lane for SKIP_PARKED too so act phase has a consistent candidate.
            skip_task = task_by_ticket.get(ticket_id) if ticket_id else None
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SKIP_PARKED,
                    ticket_id=ticket_id,
                    paused_status=str(actual_paused_status)
                    if actual_paused_status
                    else None,
                    lane=skip_task.lane if skip_task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_headless_budget(task, session, config)
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget:
            continue
        # Try terminal-sentinel salvage before declaring timeout.
        salvage = _salvage_terminal_result(session)
        if salvage is not None:
            result, claude_session_id = salvage
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    elapsed_seconds=elapsed,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.REVERT_TASK,
                ticket_id=ticket_id,
                elapsed_seconds=elapsed,
                reap_reason=ReapReason.WALL_CLOCK_BUDGET,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
        )
    return candidates


def _apply_queue_mutations(
    mutations: dict[str, QueueItemStatus],
    clear_session_id: set[str],
) -> list[str]:
    """Apply ticket-status mutations to the dev queue under dev_queue_lock.

    *mutations* maps ticket_id → target QueueItemStatus for RUNNING tasks.
    *clear_session_id* is the subset of ticket_ids whose session_id should be
    set to None (only PENDING-routed tasks; BLOCKED_ON_USER tasks keep their
    session_id for operator traceability).

    Returns the list of ticket_ids that were mutated.  Skips tasks that are
    not RUNNING (natural idempotency — a second call is a no-op).
    """
    if not mutations:
        return []
    mutated: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id not in mutations:
                continue
            task.status = mutations[task.ticket_id]
            if task.ticket_id in clear_session_id:
                task.session_id = None
            mutated.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return mutated


def _act_on_stalled_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
) -> list[str]:
    """Act phase for stalled headless sessions: apply all mutations.

    Consumes ReapCandidate objects from _detect_stalled_candidates.
    Mirrors the side-effect logic in revert_stalled_headless_sessions.
    Returns the list of ticket IDs reverted to PENDING.

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), REVERT_TASK candidates are
    routed to BLOCKED_ON_USER instead of triggering stop/remove.  Non-REVERT
    candidates (SALVAGE_*, SKIP_PARKED) are unaffected and pass through.
    Per-lane resolution: each REVERT_TASK candidate's effective policy is
    resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return []

    effective_config = config if config is not None else OrchestratorConfig()
    clients = load_effective_clients()
    # Route each REVERT_TASK candidate individually based on its lane's policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.REVERT_TASK:
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(signal_mutations, clear_session_id=set())
    candidates = auto_candidates
    if not candidates:
        return []

    # Separate by action for batch processing.
    skip_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SKIP_PARKED
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]

    # SKIP_PARKED: emit event only, no state/queue change.
    for candidate in skip_candidates:
        record_event(
            OrchestratorEventType.SESSION_SALVAGE_SKIPPED,
            {
                "session_id": candidate.session_id,
                "ticket_id": candidate.ticket_id,
                "reason": _SALVAGE_SKIP_REASON,
                "paused_status": candidate.paused_status,
            },
            correlation_id=candidate.ticket_id,
        )

    if not salvage_candidates and not revert_candidates:
        return []

    # Apply state mutations for salvage and revert.
    session_by_id = {s.id: s for s in state.sessions}

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )

    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET

    save_state(state)

    timed_out_ticket_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    salvaged_result_by_ticket = {
        c.ticket_id: c.salvage_result
        for c in salvage_candidates
        if c.ticket_id and c.salvage_result
    }
    reverted: list[str] = []
    if timed_out_ticket_ids or salvaged_ticket_ids_set:
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
                elif task.ticket_id in salvaged_ticket_ids_set:
                    result = salvaged_result_by_ticket[task.ticket_id]
                    task.status = _queue_status_for_salvaged(result)
                    changed = True
            if changed:
                save_dev_queue(store)

    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "elapsed_seconds": candidate.elapsed_seconds,
            "last_assistant_message_excerpt": "",
        }
        record_event(OrchestratorEventType.SESSION_TIMED_OUT, payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    return reverted


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
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_stalled_candidates(
        state, now=now, config=config, task_by_ticket=task_by_ticket
    )
    return _act_on_stalled_candidates(state, candidates, now=now, config=config)


def _has_terminal_sentinel(session: Session) -> bool:
    """True when the session has already emitted a terminal sentinel.

    A real AUTO_DEV sentinel dump always carries a ``"status"`` key; the park
    markers (``silently_idle``/``needs_salvage``) carry ``"paused_status"`` and
    no ``"status"``. Key presence — not value — is the structural discriminant,
    so a parked session is correctly NOT treated as terminal and the idle
    watchdog re-checks it for a late terminal sentinel. See #418, #497.
    """
    return isinstance(session.last_result, dict) and "status" in session.last_result


def resolve_idle_watchdog_budget(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the idle-watchdog budget (seconds) for a session's ticket.

    Precedence (highest first):
    1. task.idle_watchdog_override — explicit per-ticket escape hatch.
    2. task.scope_hint — look up per-tier default in config.
    3. config.idle_watchdog_seconds — operator-tunable global default.
    4. IDLE_WATCHDOG_SECONDS — hardcoded fallback.
    """
    global_default = (
        IDLE_WATCHDOG_SECONDS
        if config.idle_watchdog_seconds is None
        else config.idle_watchdog_seconds
    )
    if task is None:
        return global_default
    if task.idle_watchdog_override is not None:
        return task.idle_watchdog_override
    if task.scope_hint is not None:
        tier_budget = config.idle_watchdog_by_tier.get(task.scope_hint)
        if tier_budget is not None:
            return tier_budget
    return global_default


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


def _detect_idle_candidates(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Pure classification phase for silently idle DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. The idle_observation_count increment is computed
    but NOT written; it is carried in new_observation_count on the candidate.
    See GitHub #552, ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if _has_terminal_sentinel(session):
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        elapsed = (now - session.started_at).total_seconds()
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_idle_watchdog_budget(task, config)
        # ROUTE_EMITTED_SENTINEL: fires before the full idle-budget check.
        # An emitted sentinel is positive evidence the worker completed; the
        # 300 s threshold (sentinel_unrouted_check_seconds) is shorter than
        # the watchdog budget to route the task before a reap fires.
        # Guard: last_result is None means signal_stop never ran — prevents
        # double-routing. Exempt from signal_only (constructive, not a reap).
        # See GitHub #578.
        unrouted_check = config.sentinel_unrouted_check_seconds
        if session.last_result is None and elapsed >= unrouted_check:
            routed = _parse_any_sentinel_from_transcript(session)
            if routed is not None:
                _routed_result, _csid = routed
                candidates.append(
                    ReapCandidate(
                        session_id=session.id,
                        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
                        ticket_id=ticket_id,
                        routed_sentinel=_routed_result,
                        salvage_csid=_csid,
                        elapsed_seconds=elapsed,
                        lane=task.lane if task else DEFAULT_LANE,
                        client=session.client,
                    )
                )
                continue
        if elapsed < budget:
            continue
        # Liveness check: if active, check for recovery of observation counter.
        if _transcript_recently_active(session, now) or _awaiting_subagent(
            session, now
        ):
            if session.idle_observation_count > 0:
                candidates.append(
                    ReapCandidate(
                        session_id=session.id,
                        proposed_action=ProposedAction.RECOVER_COUNTER,
                        ticket_id=ticket_id,
                        new_observation_count=0,
                        lane=task.lane if task else DEFAULT_LANE,
                        client=session.client,
                    )
                )
            continue
        # Sentinel salvage: evidence-based completion, not deferred by counter.
        salvage = _salvage_terminal_result(session)
        if salvage is not None:
            result, claude_session_id = salvage
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    elapsed_seconds=elapsed,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        # Confirm-before-reap: accumulate consecutive failed observations.
        new_count = session.idle_observation_count + 1
        if new_count < config.idle_confirm_observations:
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.INCREMENT_COUNTER,
                    ticket_id=ticket_id,
                    new_observation_count=new_count,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        # Threshold reached: classify final disposition.
        # Git-state salvage path.
        if session.worktree_path is not None:
            branch = _checked_out_branch(session.worktree_path)
            if branch is not None:
                post_review_clean = _detect_post_review_clean(session)
                worktree_dirty = _worktree_dirty_by_path(
                    session.client, session.worktree_path
                )
                candidates.append(
                    ReapCandidate(
                        session_id=session.id,
                        proposed_action=ProposedAction.SALVAGE_GIT,
                        ticket_id=ticket_id,
                        branch=branch,
                        worktree_path_str=str(session.worktree_path),
                        post_review_clean=post_review_clean,
                        worktree_dirty=worktree_dirty,
                        new_observation_count=new_count,
                        lane=task.lane if task else DEFAULT_LANE,
                        client=session.client,
                    )
                )
                continue
        cap = resolve_idle_retry_cap(task, config)
        worktree_dirty = _worktree_dirty_by_path(session.client, session.worktree_path)
        if task is not None and task.attempts < cap:
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.REVERT_TASK,
                    ticket_id=ticket_id,
                    elapsed_seconds=elapsed,
                    worktree_dirty=worktree_dirty,
                    new_observation_count=new_count,
                    usage_limit_detected=_detect_usage_limit(session),
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
        else:
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
                    ticket_id=ticket_id,
                    worktree_dirty=worktree_dirty,
                    new_observation_count=new_count,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
    return candidates


def _act_on_idle_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
) -> tuple[list[str], list[_SalvageCandidate]]:
    """Act phase for silently idle sessions: apply all mutations.

    Consumes ReapCandidate objects from _detect_idle_candidates.
    Returns (blocked_ticket_ids, salvage_git_candidates) matching
    flag_silently_idle_daemon_sessions's return type.

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), REVERT_TASK candidates are
    routed to BLOCKED_ON_USER instead of triggering stop/remove.  Non-REVERT
    candidates (SALVAGE_*, INCREMENT_COUNTER, RECOVER_COUNTER,
    PARK_BLOCKED_ON_USER) are unaffected and pass through.
    Per-lane resolution: each REVERT_TASK candidate's effective policy is
    resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return [], []

    effective_config = config if config is not None else OrchestratorConfig()
    clients = load_effective_clients()
    # Route each REVERT_TASK candidate individually based on its lane's policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.REVERT_TASK:
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(signal_mutations, clear_session_id=set())
    candidates = auto_candidates
    if not candidates:
        return [], []

    session_by_id = {s.id: s for s in state.sessions}

    counter_candidates = [
        c
        for c in candidates
        if c.proposed_action
        in (
            ProposedAction.INCREMENT_COUNTER,
            ProposedAction.RECOVER_COUNTER,
            # SALVAGE_GIT reaches the threshold — persist new_observation_count so
            # a process restart between ticks does not replay the observation as fresh.
            ProposedAction.SALVAGE_GIT,
        )
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]
    park_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    ]
    salvage_git_candidates_list = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_GIT
    ]
    routed_sentinel_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]

    # ROUTE_EMITTED_SENTINEL queue routing: _apply_sentinel_to_task acquires its
    # own dev_queue_lock so it runs BEFORE the shared lock block below.  Session
    # state is mutated here; save_state picks it up in the combined flush below.
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue
        if candidate.ticket_id:
            _apply_sentinel_to_task(
                candidate.ticket_id, candidate.session_id, candidate.routed_sentinel
            )
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid

    # Counter-only updates: just update the counter and possibly save_state.
    counters_changed = False
    for candidate in counter_candidates:
        session = session_by_id[candidate.session_id]
        session.idle_observation_count = candidate.new_observation_count
        counters_changed = True

    # Salvage completions.
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )

    # Recover (revert to PENDING for re-dispatch).
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # Park: flag-only (preserves #348 — no daemon stop, session stays ACTIVE).
    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        session.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
        session.reap_reason = ReapReason.RETRY_CAP_PARKED

    has_dispositions = bool(
        salvage_candidates
        or revert_candidates
        or park_candidates
        or salvage_git_candidates_list
        or routed_sentinel_candidates
    )

    if counters_changed or has_dispositions:
        save_state(state)

    if not has_dispositions:
        return [], []

    recovered_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    parked_ids = {c.ticket_id for c in park_candidates if c.ticket_id}
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    salvaged_result_by_ticket = {
        c.ticket_id: c.salvage_result
        for c in salvage_candidates
        if c.ticket_id and c.salvage_result
    }
    blocked: list[str] = []
    if recovered_ids or parked_ids or salvaged_ticket_ids_set:
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
                elif task.ticket_id in salvaged_ticket_ids_set:
                    result = salvaged_result_by_ticket[task.ticket_id]
                    task.status = _queue_status_for_salvaged(result)
                    changed = True
            if changed:
                save_dev_queue(store)

    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)
        cause = (
            _CAUSE_USAGE_LIMIT
            if session.reap_reason is ReapReason.USAGE_LIMIT_CUTOFF
            else _CAUSE_IDLE_STALL
        )
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "elapsed_seconds": candidate.elapsed_seconds,
                "cause": cause,
                "last_assistant_message_excerpt": "",
            },
        )

    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _SILENTLY_IDLE_REASON,
                "breadcrumbs": "",
                "crashed": False,
            },
        )
        fire_push_notification(session.name, session.client)

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        routed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.routed_sentinel.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, routed_payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    salvage_git: list[_SalvageCandidate] = [
        (
            c.session_id,
            c.ticket_id,
            c.branch,
            c.worktree_path_str,
            c.post_review_clean,
        )
        for c in salvage_git_candidates_list
        if c.branch is not None and c.worktree_path_str is not None
    ]

    return blocked, salvage_git


def flag_silently_idle_daemon_sessions(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> tuple[list[str], list[_SalvageCandidate]]:
    """Flag DAEMON RUNNING sessions idle past the watchdog budget with no sentinel.

    These are sessions that stalled without emitting a terminal signal — typically
    because the child process self-backgrounded a subagent and exited before
    the subagent returned (GitHub #105, #121). They appear ACTIVE/IDLE in cw
    state while producing no output.

    Only targets sessions whose ``surface_ref`` is currently in *native_live*
    (the daemon still has them). Sessions with a dead surface ref are handled
    by the phantom sweep → PENDING for retry.

    Confirm-before-reap (#545): a session must fail the liveness check on
    ``config.idle_confirm_observations`` consecutive watchdog ticks before it
    is dispositioned. ``session.idle_observation_count`` is incremented each
    tick a session fails; it is reset to 0 on recovery. This prevents a single
    quiet poll from killing a healthy DAEMON worker.

    Returns a tuple of:
    - list of ticket IDs whose queue task was set to BLOCKED_ON_USER
    - list of git-state salvage candidates for the post-lock pass:
      (session_id, ticket_id, branch, worktree_path_str, post_review_clean)
    """
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=config,
        task_by_ticket=task_by_ticket,
    )
    return _act_on_idle_candidates(state, candidates, now=now, config=config)


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
    with sessions_lock():
        locked_report, salvage_git_candidates = _reconcile_locked()

    # Post-pass: runs AFTER sessions_lock releases so no gh subprocess
    # executes under the session lock (liveness — #485 SHOULD_FIX 4).
    completed_ticket_ids = complete_timed_out_merged_tasks()
    salvaged_ticket_ids = salvage_committed_no_pr_sessions(salvage_git_candidates)

    if not completed_ticket_ids and not salvaged_ticket_ids:
        return locked_report

    return ReconcileReport(
        phantom_session_ids=locked_report.phantom_session_ids,
        phantom_session_names=locked_report.phantom_session_names,
        reverted_ticket_ids=locked_report.reverted_ticket_ids,
        completed_ticket_ids=locked_report.completed_ticket_ids + completed_ticket_ids,
        usage_limited=locked_report.usage_limited,
        salvaged_ticket_ids=salvaged_ticket_ids,
    )


_SalvageCandidate = tuple[str, str | None, str, str, bool]


class ProposedAction(StrEnum):
    """Action the act dispatcher will take for a classified session.

    See GitHub #552, ADR-0006.
    """

    REVERT_TASK = "revert_task"
    CRASH_COMPLETE = "crash_complete"
    SALVAGE_COMPLETION = "salvage_completion"
    PARK_BLOCKED_ON_USER = "park_blocked_on_user"
    SALVAGE_GIT = "salvage_git"
    SKIP_PARKED = "skip_parked"
    INCREMENT_COUNTER = "increment_counter"
    RECOVER_COUNTER = "recover_counter"
    # Emitted sentinel that signal_stop never routed (turn never completed).
    # Fires at sentinel_unrouted_check_seconds; exempt from signal_only.
    # See GitHub #578.
    ROUTE_EMITTED_SENTINEL = "route_emitted_sentinel"


@dataclass(frozen=True)
class ReapCandidate:
    """Classification result from detect phase. Consumed by act dispatcher.

    See GitHub #552, ADR-0006.
    """

    session_id: str
    proposed_action: ProposedAction
    ticket_id: str | None = None
    worktree_dirty: bool = False
    salvage_result: AutoDevResult | None = None
    salvage_csid: str | None = None
    # ROUTE_EMITTED_SENTINEL carries the full parsed result (any status).
    routed_sentinel: AutoDevResult | BlockedResult | None = None
    usage_limit_detected: bool = False
    elapsed_seconds: float = 0.0
    reap_reason: ReapReason | None = None
    branch: str | None = None
    worktree_path_str: str | None = None
    post_review_clean: bool = False
    paused_status: str | None = None
    new_observation_count: int = 0
    # Lane the owning task is assigned to; stamped from task.lane in detect phase.
    # Candidates without an owning task carry DEFAULT_LANE. Used by
    # resolve_reap_policy to select per-lane reap_policy over the global default.
    lane: str = DEFAULT_LANE
    # Phantom sweep: carry client + worktree_path for SESSION_PHANTOM_REVERTED payload.
    # Also stamped in stalled/idle detect from session.client so resolve_reap_policy
    # can look up the lane config for this candidate.
    client: str | None = None
    worktree_path: Path | None = None


def resolve_reap_policy(
    candidate: ReapCandidate,
    clients: dict[str, ClientConfig],
    global_cfg: OrchestratorConfig,
) -> ReapPolicy:
    """Resolve the effective reap_policy for a candidate.

    Precedence (highest to lowest):
      1. Lane-level LaneConfig.reap_policy in the candidate's client config.
      2. Global OrchestratorConfig.reap_policy.
      3. ReapPolicy.SIGNAL_ONLY fail-safe (built into OrchestratorConfig default).

    A candidate whose client is absent from *clients* or whose lane name is not
    declared in that client's lanes falls through to the global config. This
    keeps behaviour identical to the pre-#560 flat read for any candidate that
    predates lane stamping.
    """
    client_cfg = clients.get(candidate.client) if candidate.client else None
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == candidate.lane and lane_cfg.reap_policy is not None:
                return lane_cfg.reap_policy
    return global_cfg.reap_policy


def _detect_phantom_candidates(
    state: CwState,
    phantom_set: set[str],
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for phantom sessions.

    Returns a list of ReapCandidate objects. Makes zero writes.
    The worktree_dirty check for DAEMON sessions is performed here
    so the act phase does not need to repeat it. See GitHub #552, ADR-0006.

    task_by_ticket is used to stamp candidate.lane from the owning task's lane
    (GitHub #560). When None or the ticket has no task, lane defaults to DEFAULT_LANE.
    """
    _task_by_ticket = task_by_ticket or {}
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = _task_by_ticket.get(ticket_id) if ticket_id else None
        lane = task.lane if task else DEFAULT_LANE
        # Try sentinel salvage before declaring crashed (DAEMON only).
        salvage = (
            _salvage_terminal_result(session)
            if session.origin is SessionOrigin.DAEMON
            else None
        )
        if salvage is not None:
            result, claude_session_id = salvage
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    lane=lane,
                    client=session.client,
                    worktree_path=session.worktree_path,
                )
            )
            continue
        # Dirty-check for DAEMON sessions only; USER sessions have no worktree.
        # Why: this check runs inside sessions_lock before the queue mutation, but
        # the orphaned claude --bg process may still be alive and could write to the
        # worktree between here and the BLOCKED_ON_USER routing in
        # _act_on_phantom_candidates (TOCTOU). Accepted tradeoff: block > clobber —
        # narrow the window, accept the race. See _act_on_phantom_candidates.
        worktree_dirty = (
            _worktree_dirty_by_path(session.client, session.worktree_path)
            if session.origin is SessionOrigin.DAEMON
            else False
        )
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.CRASH_COMPLETE,
                ticket_id=ticket_id,
                worktree_dirty=worktree_dirty,
                lane=lane,
                client=session.client,
                worktree_path=session.worktree_path,
            )
        )
    return candidates


def _act_on_phantom_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
) -> tuple[list[str], list[str], bool, list[str], dict[str, AutoDevResult]]:
    """Act phase for phantom sessions: apply all mutations.

    Returns (ticket_ids_to_revert, phantom_names, usage_limited,
             salvaged_ticket_ids, salvaged_result_by_ticket).
    ticket_ids_to_revert contains only PENDING-routed tickets (not dirty/blocked).

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), CRASH_COMPLETE candidates
    (non-dirty only) are routed to BLOCKED_ON_USER instead of triggering
    stop/remove.  Dirty-worktree CRASH_COMPLETE already routes to
    BLOCKED_ON_USER in both policies — the gate only affects clean phantoms.
    SALVAGE_COMPLETION candidates pass through unaffected.
    Per-lane resolution: each clean CRASH_COMPLETE candidate's effective policy
    is resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return [], [], False, [], {}

    effective_config = config if config is not None else OrchestratorConfig()
    clients = load_effective_clients()
    # Route each clean CRASH_COMPLETE candidate individually based on its lane's policy.
    # Dirty phantoms always go to BLOCKED_ON_USER regardless of policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.CRASH_COMPLETE and not c.worktree_dirty:
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(signal_mutations, clear_session_id=set())
    candidates = auto_candidates
    if not candidates:
        return [], [], False, [], {}

    session_by_id = {s.id: s for s in state.sessions}

    crash_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.CRASH_COMPLETE
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]

    phantom_names: list[str] = []
    # ticket_ids to revert (only PENDING-routed, excludes dirty/BLOCKED_ON_USER)
    ticket_ids_to_revert: list[str] = []
    salvaged_ticket_ids: list[str] = []
    salvaged_result_by_ticket: dict[str, AutoDevResult] = {}
    pending_events: list[dict[str, object]] = []

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )
        phantom_names.append(session.name)
        if candidate.ticket_id:
            salvaged_ticket_ids.append(candidate.ticket_id)
            salvaged_result_by_ticket[candidate.ticket_id] = candidate.salvage_result
        salvaged_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        if candidate.ticket_id:
            salvaged_payload["ticket_id"] = candidate.ticket_id
        pending_events.append(salvaged_payload)

    for candidate in crash_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        phantom_names.append(session.name)
        crash_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": True,
        }
        if candidate.ticket_id:
            crash_payload["ticket_id"] = candidate.ticket_id
        pending_events.append(crash_payload)

    save_state(state)

    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    # Emit SESSION_PHANTOM_REVERTED for DAEMON-origin CRASH_COMPLETE candidates.
    dirty_ticket_ids: set[str] = set()
    for candidate in crash_candidates:
        if (
            candidate.ticket_id
            and session_by_id[candidate.session_id].origin is SessionOrigin.DAEMON
        ):
            wt_path_str: str | None = (
                str(candidate.worktree_path) if candidate.worktree_path else None
            )
            if candidate.worktree_dirty:
                dirty_ticket_ids.add(candidate.ticket_id)
            queue_status = (
                QueueItemStatus.BLOCKED_ON_USER
                if candidate.worktree_dirty
                else QueueItemStatus.PENDING
            )
            record_event(
                OrchestratorEventType.SESSION_PHANTOM_REVERTED,
                {
                    "session_id": candidate.session_id,
                    "ticket_id": candidate.ticket_id,
                    "client": candidate.client,
                    "worktree_dirty": candidate.worktree_dirty,
                    "worktree_path": wt_path_str,
                    "queue_status": queue_status,
                },
                correlation_id=candidate.ticket_id,
            )

    # Queue mutations.
    daemon_ticket_ids_to_revert = [
        c.ticket_id
        for c in crash_candidates
        if c.ticket_id and session_by_id[c.session_id].origin is SessionOrigin.DAEMON
    ]
    revert_set = set(daemon_ticket_ids_to_revert)
    salvaged_set = set(salvaged_ticket_ids)
    if revert_set or salvaged_set:
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in revert_set:
                    if task.ticket_id in dirty_ticket_ids:
                        task.status = QueueItemStatus.BLOCKED_ON_USER
                    else:
                        task.status = QueueItemStatus.PENDING
                        ticket_ids_to_revert.append(task.ticket_id)
                    task.session_id = None
                    changed = True
                elif task.ticket_id in salvaged_set:
                    salvaged_result = salvaged_result_by_ticket[task.ticket_id]
                    task.status = _queue_status_for_salvaged(salvaged_result)
                    changed = True
            if changed:
                save_dev_queue(store)

    return (
        ticket_ids_to_revert,
        phantom_names,
        False,
        salvaged_ticket_ids,
        salvaged_result_by_ticket,
    )


_REAP_PROPOSED_ACTIONS: frozenset[ProposedAction] = frozenset(
    {
        ProposedAction.REVERT_TASK,
        ProposedAction.CRASH_COMPLETE,
        ProposedAction.PARK_BLOCKED_ON_USER,
    }
)


def _emit_reap_proposed(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    now: datetime | None = None,
) -> None:
    """Emit SESSION_REAP_PROPOSED for reap-shaped candidates before act phase.

    Called from _reconcile_locked after each _detect_* and before the
    corresponding _act_on_*. Satisfies ADR-0006 invariant 3 (propose before act).

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.
    """
    _now = now or datetime.now(UTC)
    session_by_id = {s.id: s for s in state.sessions}
    any_stamped = False

    for candidate in candidates:
        if candidate.proposed_action not in _REAP_PROPOSED_ACTIONS:
            continue
        session = session_by_id.get(candidate.session_id)
        if session is None or session.reap_proposed_at is not None:
            continue

        # Compute in_roster
        in_roster = (
            session.surface_ref is not None and session.surface_ref in native_live
        )

        # Compute transcript_age_seconds (best-effort, nullable)
        transcript_age_seconds: float | None = None
        transcript_path = _locate_session_transcript(session)
        if transcript_path is not None and transcript_path.exists():
            with contextlib.suppress(OSError):
                mtime = transcript_path.stat().st_mtime
                transcript_age_seconds = _now.timestamp() - mtime

        payload = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "lane": candidate.lane,
            "proposed_action": candidate.proposed_action.value,
            "reason": candidate.reap_reason.value if candidate.reap_reason else None,
            "evidence": {
                "elapsed_seconds": candidate.elapsed_seconds,
                "in_roster": in_roster,
                "transcript_age_seconds": transcript_age_seconds,
            },
        }
        # Stamp before record_event: dedup guard fires on retry if write fails.
        session.reap_proposed_at = _now
        any_stamped = True
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id or candidate.session_id,
        )

    if any_stamped:
        save_state(state)


def _reconcile_locked() -> tuple[ReconcileReport, list[_SalvageCandidate]]:
    """Body of reconcile(), called while sessions_lock is held.

    Separated so reconcile() holds exactly one lock acquisition and the
    helpers (revert_stalled_headless_sessions, flag_silently_idle_daemon_sessions)
    can save_state directly without re-acquiring the lock.

    Returns a tuple of (ReconcileReport, salvage_git_candidates) where
    salvage_git_candidates is the list of git-state salvage candidates for
    the post-lock pass in salvage_committed_no_pr_sessions.
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
    stalled_candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )
    # native_live not yet known — stalled sweep is pre-daemon-query.
    _emit_reap_proposed(state, stalled_candidates, native_live=set(), now=now)
    stalled_reverted = _act_on_stalled_candidates(
        state, stalled_candidates, now=now, config=orchestrator_config
    )

    try:
        # `claude agents --json` returns sessionId as a full UUID
        # (e.g. "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"). cw's surface_ref
        # is the 8-char short id (prefix of the UUID) — same shape
        # `claude --bg` returns at spawn. Normalize to short id for
        # comparison; otherwise reconcile sees every native session as a
        # phantom because UUID != short-id.
        _agents = _claude_agents_json()
        native_live = {
            sid[:8] for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
        surface_to_full = {
            sid[:8]: sid for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
        daemon_errored = False
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        native_live = set()
        surface_to_full = {}
        daemon_errored = True
    if _looks_like_daemon_outage(state, daemon_errored, native_live):
        return ReconcileReport(reverted_ticket_ids=stalled_reverted), []
    _backfill_claude_session_ids(state, surface_to_full)

    # Snapshot sessions that are already TIMED_OUT before the watchdog sweep,
    # so we can detect which sessions were newly reaped by usage_limit_cutoff.
    pre_watchdog_timed_out_ids = {
        s.id for s in state.sessions if s.status == SessionStatus.TIMED_OUT
    }
    idle_candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )
    _emit_reap_proposed(state, idle_candidates, native_live=native_live, now=now)
    silently_idle_ticket_ids, salvage_git_candidates = _act_on_idle_candidates(
        state, idle_candidates, now=now, config=orchestrator_config
    )
    # Check whether any session newly transitioned to TIMED_OUT has a usage-limit
    # transcript. The _detect_usage_limit I/O cost is minimal (OS-cached files).
    watchdog_usage_limited = any(
        s.status == SessionStatus.TIMED_OUT
        and s.id not in pre_watchdog_timed_out_ids
        and _detect_usage_limit(s)
        for s in state.sessions
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
        return ReconcileReport(
            reverted_ticket_ids=all_reverted,
            usage_limited=watchdog_usage_limited,
        ), salvage_git_candidates

    phantom_set = set(drift.phantom_session_ids)
    phantom_candidates = _detect_phantom_candidates(
        state, phantom_set, task_by_ticket=shared_task_by_ticket
    )
    _emit_reap_proposed(state, phantom_candidates, native_live=native_live, now=now)
    (
        reverted,
        phantom_names,
        _watchdog_usage_limited_phantom,
        _salvaged_phantom_ticket_ids,
        _salvaged_phantom_results,
    ) = _act_on_phantom_candidates(
        state, phantom_candidates, now=now, config=orchestrator_config
    )

    # Sweep for TIMED_OUT and DAEMON-COMPLETED sessions whose owning TicketTask
    # was not yet reverted (e.g. signal_stop crashed after setting status but
    # before touching the queue, or a headless session completed without
    # the dispatch consumer processing it). TIMED_OUT/COMPLETED sessions are
    # already terminal; the only state mutation for these sessions is the
    # reap_reason stamp inside revert_timed_out_tasks /
    # revert_completed_silent_tasks (in-place + save_state, serialized by
    # the sessions_lock this function runs under).
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

    return (
        ReconcileReport(
            phantom_session_ids=drift.phantom_session_ids,
            phantom_session_names=phantom_names,
            reverted_ticket_ids=all_reverted,
            usage_limited=watchdog_usage_limited,
        ),
        salvage_git_candidates,
    )


def _revert_running_tasks_for_sessions(
    session_ids: set[str],
    dirty_session_ids: set[str] | None = None,
) -> list[str]:
    """Revert RUNNING TicketTasks whose ``session_id`` is in *session_ids*.

    Shared helper for the per-status revert wrappers. Acquires
    ``dev_queue_lock`` for the read+write window; writes only when at least
    one task was reverted. Returns the list of reverted ticket IDs (PENDING
    only — BLOCKED_ON_USER tickets are excluded from the return value so they
    do not enter ReconcileReport.reverted_ticket_ids).

    When *dirty_session_ids* is provided, tasks whose session_id is in the
    set are routed to BLOCKED_ON_USER instead of PENDING to preserve in-flight
    worktree state for operator inspection (GitHub issue #421).

    # Why: dirtiness is checked before dev_queue_lock is acquired (in the
    # callers revert_timed_out_tasks / revert_completed_silent_tasks), but the
    # orphaned claude --bg process may still be alive and could write to the
    # worktree between that check and the BLOCKED_ON_USER write below (TOCTOU).
    # The accepted tradeoff is block > clobber — narrow the window, accept the race.
    """
    if not session_ids:
        return []

    dirty = dirty_session_ids or set()
    reverted: list[str] = []
    changed = False
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            if task.session_id in dirty:
                task.status = QueueItemStatus.BLOCKED_ON_USER
            else:
                task.status = QueueItemStatus.PENDING
                reverted.append(task.ticket_id)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    return reverted


def complete_timed_out_merged_tasks() -> list[str]:
    """Upgrade PENDING TicketTasks to COMPLETED when their PR merged.

    Targets TIMED_OUT DAEMON sessions in the lookback window whose PR merged.

    Post-pass over TIMED_OUT DAEMON sessions in the lookback window. For each
    whose dev-queue task is still PENDING and whose linked PR is MERGED (via
    issue-linkage), upgrades the task to COMPLETED and emits SESSION_COMPLETED
    with reason="timed_out_merged".

    Called from reconcile() AFTER sessions_lock is released — no gh subprocess
    runs under the session lock (liveness requirement, #485 SHOULD_FIX 4).

    Returns the list of ticket IDs auto-completed.
    """
    state = load_state()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)

    # Build a cheap lookup: ticket_id → task (for PENDING filter before gh call).
    task_by_ticket: dict[str, TicketTask] = {
        t.ticket_id: t for t in load_dev_queue().tasks
    }

    # Phase 1: Cheap filters before any gh subprocess call.
    # session.branch is None for all DAEMON sessions (spawn.py never sets it).
    candidates: list[tuple[Session, str]] = []
    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        # Guard: completed_at may be None in legacy state files.
        if session.completed_at is None:
            continue
        if session.completed_at < cutoff:
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        # Idempotency gate: only PENDING tasks are safe to auto-complete.
        # RUNNING means a new session already picked it up; terminal means done.
        task = task_by_ticket.get(ticket_id)
        if task is None or task.status != QueueItemStatus.PENDING:
            continue
        candidates.append((session, ticket_id))

    if not candidates:
        return []

    # Phase 2: One gh call per surviving candidate (outside any lock).
    to_complete: list[tuple[Session, str]] = []
    for session, ticket_id in candidates:
        merged, gh_available = pr_is_merged_for_ticket(ticket_id)
        if not gh_available:
            # gh binary absent — skip all remaining candidates.
            break
        if merged is None:
            # Transient error — skip this session only.
            continue
        if merged:
            to_complete.append((session, ticket_id))
        # merged is False → leave PENDING.

    if not to_complete:
        return []

    # Phase 3: Acquire only dev_queue_lock for the PENDING→COMPLETED write.
    completed_ids: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for _, ticket_id in to_complete:
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.PENDING
                ):
                    task.status = QueueItemStatus.COMPLETED
                    completed_ids.append(ticket_id)
                    changed = True
                    break
        if changed:
            save_dev_queue(store)

    # Phase 4: Emit decision-trace events after the lock releases.
    for session, ticket_id in to_complete:
        if ticket_id in completed_ids:
            record_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": session.id,
                    "session_name": session.name,
                    "client": session.client,
                    "ticket_id": ticket_id,
                    "claude_session_id": session.claude_session_id,
                    "crashed": False,
                    "salvaged": True,
                    "reason": _TIMED_OUT_MERGED_REASON,
                },
                correlation_id=ticket_id,
            )

    return completed_ids


def salvage_committed_no_pr_sessions(
    candidates: list[_SalvageCandidate],
) -> list[str]:
    """Post-pass: git-state salvage for committed-but-no-PR reaped sessions.

    Called from reconcile() AFTER sessions_lock releases — git and gh subprocesses
    run here, never under the session lock. See GitHub issue #497.

    candidates: list of (session_id, ticket_id, branch, worktree_path_str,
    post_review_clean) collected by flag_silently_idle_daemon_sessions under lock.

    Returns list of ticket_ids that were auto-completed (HIGH path).
    """
    if not candidates:
        return []

    completed_ticket_ids: list[str] = []
    state = load_state()

    for (
        session_id,
        ticket_id,
        branch,
        worktree_path_str,
        post_review_clean,
    ) in candidates:
        session = next((s for s in state.sessions if s.id == session_id), None)
        if session is None:
            continue

        wt_path = Path(worktree_path_str)

        # Confirm git-state trigger: commits beyond base AND no open PR.
        has_commits = _has_commits_beyond_base(wt_path)
        if not has_commits:
            # No commits beyond base — not a salvage candidate; fall through to
            # existing recover/park on the next reconcile tick.
            continue

        pr_result, gh_available = pr_exists_for_branch(branch)
        if not gh_available:
            # gh absent — cannot confirm PR absence; treat as non-candidate.
            continue
        if pr_result is None:
            # Transient error — cannot confirm; treat as non-candidate.
            continue
        if pr_result is True:
            # PR already exists — not our case.
            continue

        # Confirmed: commits beyond base AND no open PR.
        if post_review_clean:
            # HIGH path: automated draft PR.
            _salvage_high_path(
                session, ticket_id, branch, wt_path, completed_ticket_ids
            )
        else:
            # LOW path: flag for human salvage.
            _salvage_low_path(session, ticket_id, branch, worktree_path_str)

    return completed_ticket_ids


def _salvage_high_path(
    session: Session,
    ticket_id: str | None,
    branch: str,
    wt_path: Path,
    completed_ticket_ids: list[str],
) -> None:
    """Execute the HIGH-confidence automated draft PR path."""
    # Idempotency re-check immediately before creating the PR.
    pr_result, gh_available = pr_exists_for_branch(branch)
    if not gh_available or pr_result is True or pr_result is None:
        # Cannot confirm or PR now exists — downgrade to LOW.
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Push branch to origin (no-op if already pushed).
    try:
        subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=wt_path,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Create draft PR.
    title = _SALVAGE_PR_TITLE_TEMPLATE.format(ticket_id=ticket_id or "unknown")
    body = _SALVAGE_PR_BODY_TEMPLATE.format(ticket_id=ticket_id or "unknown")
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        pr_url = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Mark session completed — under sessions_lock to prevent concurrent clobber.
    now = datetime.now(UTC)
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session.id:
                if s.status not in (SessionStatus.COMPLETED, SessionStatus.TIMED_OUT):
                    s.status = SessionStatus.COMPLETED
                    s.completed_at = now
                    s.completed_reason = CompletionReason.NORMAL
                    s.reap_reason = ReapReason.SALVAGE_COMPLETED
                break
        save_state(fresh_state)

    # Update queue task to COMPLETED.
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.COMPLETED
                    save_dev_queue(store)
                    completed_ticket_ids.append(ticket_id)
                    break

    # Emit SESSION_COMPLETED event.
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "crashed": False,
            "salvaged": True,
            "salvage_kind": _SALVAGE_KIND_GIT_STATE,
            "draft": True,
            "pr": pr_url,
        },
    )

    # Stop the surface if still running.
    if session.surface_ref is not None:
        with contextlib.suppress(Exception):
            get_native_daemon_client().stop(session.surface_ref)


def _salvage_low_path(
    session: Session,
    ticket_id: str | None,
    branch: str,
    worktree_path_str: str,
) -> None:
    """Execute the LOW-confidence flag-only path."""
    breadcrumbs = f"branch={branch} worktree={worktree_path_str}"
    already_flagged = False

    # Update session last_result under sessions_lock. Capture already_flagged
    # before the conditional write so the early-return below can suppress
    # duplicate queue mutation, event, and push notification (#418).
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session.id:
                already_flagged = (
                    isinstance(s.last_result, dict)
                    and s.last_result.get("paused_status") == _NEEDS_SALVAGE_REASON
                )
                if not already_flagged:
                    s.last_result = {"paused_status": _NEEDS_SALVAGE_REASON}
                    s.reap_reason = ReapReason.SALVAGE_PARKED
                break
        save_state(fresh_state)

    # Already dispositioned on a prior tick — suppress duplicate queue mutation,
    # event, and push notification so the idle watchdog re-collecting this parked
    # session does not re-fire every reconcile tick (#418 removed the upstream
    # _has_terminal_sentinel skip this relied on).
    if already_flagged:
        return

    # Route queue task to BLOCKED_ON_USER.
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.BLOCKED_ON_USER
                    save_dev_queue(store)
                    break

    # Emit SESSION_NEEDS_ATTENTION with breadcrumbs for human salvage.
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _NEEDS_SALVAGE_REASON,
            "breadcrumbs": breadcrumbs,
            "crashed": False,
        },
    )
    fire_push_notification(session.name, session.client)


def _build_dirty_session_ids_and_notify(
    sessions: list[Session],
) -> set[str]:
    """Identify sessions with dirty worktrees, emit SESSION_NEEDS_ATTENTION.

    Called before acquiring dev_queue_lock so that dirtiness is assessed
    outside the lock window (see TOCTOU note in _revert_running_tasks_for_sessions).

    Returns the set of session IDs whose worktrees have unsaved work.
    Does NOT write session.last_result — this is a queue-level guard, not a
    park-marker update (to avoid interfering with the existing park-marker logic).
    """
    dirty_session_ids: set[str] = set()
    for session in sessions:
        if not _worktree_dirty_by_path(session.client, session.worktree_path):
            continue
        dirty_session_ids.add(session.id)
        ticket_id = ticket_id_for_session(session.name)
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _DIRTY_WORKTREE_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
            },
        )
        fire_push_notification(session.name, session.client)
    return dirty_session_ids


def revert_timed_out_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is TIMED_OUT.

    Called during :func:`reconcile` as a backstop for the case where
    ``signal_stop`` crashed after writing TIMED_OUT status but before
    reverting the dev-queue task. Returns the list of ticket IDs reverted.

    Caller must hold ``sessions_lock`` (all call sites are inside
    ``_reconcile_locked``); the reap_reason stamp below relies on it.

    Sessions with dirty worktrees are routed to BLOCKED_ON_USER instead of
    PENDING, and a SESSION_NEEDS_ATTENTION event is emitted for operator
    inspection (GitHub issue #421).

    Sets reap_reason=COMPLETED_BACKSTOP only on sessions whose RUNNING
    dev-queue task is actually being reverted, so the queue-events server
    can emit queue.session_reaped (#380) without false events on the happy
    path (sessions whose task already completed normally are not stamped).
    """
    state = load_state()
    target_sessions = [
        s
        for s in state.sessions
        if s.status == SessionStatus.TIMED_OUT and s.origin is SessionOrigin.DAEMON
    ]
    session_ids = {s.id for s in target_sessions}
    # Pre-read the dev queue (no lock) to identify which sessions have a
    # RUNNING task that will actually be reverted.  Only those sessions get
    # the COMPLETED_BACKSTOP stamp so we avoid emitting false reap events for
    # sessions whose task already completed normally via the happy path.
    # Why: this read is outside dev_queue_lock, so a task could flip from
    # RUNNING to another status between here and the locked revert below —
    # TOCTOU accepted (same pattern as the dirty-check in
    # _revert_running_tasks_for_sessions); worst case is a missed or early
    # event, no data loss.
    store = load_dev_queue()
    backstop_session_ids = {
        t.session_id
        for t in store.tasks
        if t.status == QueueItemStatus.RUNNING and t.session_id in session_ids
    }
    # Why: stamp in place + save_state, NOT mutate_state — the caller
    # already holds sessions_lock, and the lock is a per-open-fd flock,
    # so re-acquiring it here self-deadlocks (#387 gate hang).
    state_changed = False
    for s in target_sessions:
        if s.reap_reason is None and s.id in backstop_session_ids:
            s.reap_reason = ReapReason.COMPLETED_BACKSTOP
            state_changed = True
    if state_changed:
        save_state(state)
    # Compute dirtiness BEFORE acquiring dev_queue_lock (see TOCTOU note in
    # _revert_running_tasks_for_sessions docstring).
    dirty_session_ids = _build_dirty_session_ids_and_notify(target_sessions)
    return _revert_running_tasks_for_sessions(session_ids, dirty_session_ids)


def revert_completed_silent_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is DAEMON COMPLETED.

    Called during :func:`reconcile` as a backstop for sessions that completed
    without reverting their dev-queue task (e.g. the session wrote COMPLETED
    status but the dispatch consumer had not yet processed it). Returns the
    list of ticket IDs reverted.

    Caller must hold ``sessions_lock`` (all call sites are inside
    ``_reconcile_locked``); the reap_reason stamp below relies on it.

    Sessions with dirty worktrees are routed to BLOCKED_ON_USER instead of
    PENDING, and a SESSION_NEEDS_ATTENTION event is emitted for operator
    inspection (GitHub issue #421).

    Sets reap_reason=COMPLETED_BACKSTOP only on sessions whose RUNNING
    dev-queue task is actually being reverted, so the queue-events server
    can emit queue.session_reaped (#380) without false events on the happy
    path (sessions whose task already completed normally are not stamped).
    """
    state = load_state()
    target_sessions = [
        s
        for s in state.sessions
        if s.status == SessionStatus.COMPLETED and s.origin is SessionOrigin.DAEMON
    ]
    session_ids = {s.id for s in target_sessions}
    # Pre-read the dev queue (no lock) to identify which sessions have a
    # RUNNING task that will actually be reverted.  Only those sessions get
    # the COMPLETED_BACKSTOP stamp so we avoid emitting false reap events for
    # sessions whose task already completed normally via the happy path.
    # Why: this read is outside dev_queue_lock, so a task could flip from
    # RUNNING to another status between here and the locked revert below —
    # TOCTOU accepted (same pattern as the dirty-check in
    # _revert_running_tasks_for_sessions); worst case is a missed or early
    # event, no data loss.
    store = load_dev_queue()
    backstop_session_ids = {
        t.session_id
        for t in store.tasks
        if t.status == QueueItemStatus.RUNNING and t.session_id in session_ids
    }
    # Why: stamp in place + save_state, NOT mutate_state — the caller
    # already holds sessions_lock, and the lock is a per-open-fd flock,
    # so re-acquiring it here self-deadlocks (#387 gate hang).
    state_changed = False
    for s in target_sessions:
        if s.reap_reason is None and s.id in backstop_session_ids:
            s.reap_reason = ReapReason.COMPLETED_BACKSTOP
            state_changed = True
    if state_changed:
        save_state(state)
    # Compute dirtiness BEFORE acquiring dev_queue_lock (see TOCTOU note in
    # _revert_running_tasks_for_sessions docstring).
    dirty_session_ids = _build_dirty_session_ids_and_notify(target_sessions)
    return _revert_running_tasks_for_sessions(session_ids, dirty_session_ids)
