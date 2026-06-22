"""Shared constants, dataclasses, and leaf helpers for the reconcile package.

This module holds the cross-cutting pieces used by more than one reconcile
cluster (idle, stalled, phantom, salvage, tasks, core): module-level
constants, the :class:`ReconcileReport` / :class:`ReapCandidate` dataclasses,
the :class:`ProposedAction` enum, and the transcript / worktree / queue leaf
helpers. See the package ``__init__`` docstring for the full architecture.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from cw._util import _iter_sentinel_text_blocks, claude_project_dir
from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    PAUSED_FOR_USER_INPUT_STATUSES,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
    extract_block,
    is_documented_example,
    parse_stdout,
)
from cw.config import (
    get_client,
    save_state,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import USAGE_LIMIT_RE, CwError
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
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
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.worktree import (
    remove_worktree,
    worktree_has_unsaved_work,
    worktree_path_for,
)

if TYPE_CHECKING:
    from pathlib import Path

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
DEFAULT_STALLED_RETRY_CAP = 2  # wall-clock-budget retries before parking (#756)

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
# Paused-status written to SESSION_NEEDS_ATTENTION events when the stalled
# watchdog parks a session after exhausting its wall-clock retry cap (GitHub #756).
_STALLED_CAP_PARKED_REASON = "stalled_retry_cap_parked"
# Paused-status written to SESSION_NEEDS_ATTENTION events when a FINALIZE-stage
# session times out with commits pushed but no PR (GitHub #812). The worktree is
# preserved; rescue_finalize_blocked_sessions opens the PR on the next tick.
_FINALIZE_BLOCKED_REASON = "finalize_blocked"
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
_RESCUE_PR_BODY_TEMPLATE = (
    "Auto-rescued by reconcile after finalize was blocked.\n\n"
    "The worker completed impl+review and pushed the branch but could not open"
    " the PR (permission classifier / usage limit / transient gh failure)."
    " Ticket: #{ticket_id}"
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
    # Session at Stage.FINALIZE timed out with commits pushed but no PR.
    # Worktree is preserved; rescue_finalize_blocked_sessions opens the PR.
    # See GitHub #812.
    PARK_FINALIZE_BLOCKED = "park_finalize_blocked"


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
    # Stamped from task.stage / task.attempts in stalled detect; carried into
    # SESSION_STAGE_TIMED_OUT_RETRIED payload. See GitHub #724.
    stage: Stage = DEFAULT_STAGE
    attempts: int = 0


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
    ``rescued_ticket_ids`` — ticket IDs auto-completed via the finalize-blocked
    rescue path (TIMED_OUT sessions whose PR creation previously failed).
    Populated by :func:`rescue_finalize_blocked_sessions`. See GitHub #812 #816.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)
    completed_ticket_ids: list[str] = field(default_factory=list)
    usage_limited: bool = False
    salvaged_ticket_ids: list[str] = field(default_factory=list)
    rescued_ticket_ids: list[str] = field(default_factory=list)


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


def _parse_sentinel_from_blocks(path: Path) -> AutoDevResult | BlockedResult | None:
    """Parse the LAST transcript block carrying a complete sentinel frame.

    Scans candidate blocks via :func:`_iter_sentinel_text_blocks` — assistant
    text AND ``tool_result`` (Bash stdout) blocks — returning the parse of the
    last block whose framing is complete. Last-match mirrors ``extract_block``'s
    §3.1 "LAST occurrence wins" rule (GitHub #591). Documented-example blocks
    (the illustrative ``pr=42 / PROJ-1234`` placeholder in the skill prompt)
    are skipped; if only an example block is present, returns None.

    A worker may emit the sentinel via ``cat <<EOF`` rather than as assistant
    text, landing the frame in a tool_result block; scanning only assistant
    text misses it and the stage stalls (GitHub #731). Returns ``None`` when no
    non-example block carries a complete frame.
    """
    last_result: AutoDevResult | BlockedResult | None = None
    for text in _iter_sentinel_text_blocks(path):
        if extract_block(text) is not None:
            result = parse_stdout(text)
            if isinstance(result, AutoDevResult) and is_documented_example(result):
                continue
            last_result = result
    return last_result


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
    result = _parse_sentinel_from_blocks(transcript)
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
    :func:`_salvage_terminal_result` would skip.  Returns None when no sentinel
    framing is present (the no-frame case ``parse_stdout`` reports as
    BLOCKER_REASON_NO_RESULT_EMITTED).

    Used by the ROUTE_EMITTED_SENTINEL detection path for sessions where the
    sentinel was emitted but the Stop hook never fired.  See GitHub #578, #731.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return None
    result = _parse_sentinel_from_blocks(transcript)
    if result is None or (
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
            # Delegate to the shared B2 staged advance decision so both the
            # consume path (_apply_events_to_store) and the reconcile
            # ROUTE_EMITTED_SENTINEL path use the same routing table (#698).
            # Why function-level import: cw.dispatch imports reconcile at
            # module level (see reconcile.py module docstring); a module-level
            # import here would create a circular dependency.
            from cw.dispatch import apply_staged_decision

            clients = _deps.load_effective_clients()
            last_result = sentinel.model_dump(mode="json")
            apply_staged_decision(target, sentinel.status, last_result, clients)
        # BlockedResult: sentinel failed to parse or was malformed.
        elif sentinel.blocker.reason in _DETERMINISTIC_PARSE_FAILURES:
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
            # An unparseable/unknown-status sentinel (status_unknown,
            # multiple_result_blocks, any unrecognized reason) carries NO success
            # signal. Never mark it COMPLETED — that silently retires unshipped
            # work as "shipped" (#750, the #728 loss). Surface as FAILED so the
            # operator sees it instead of a phantom completion.
            target.status = QueueItemStatus.FAILED

        save_dev_queue(store)


def _session_project_dir(session: Session) -> Path | None:
    """Return the Claude project dir for *session*, or None if worktree path unset."""
    worktree = session.worktree_path
    if worktree is None:
        return None
    return claude_project_dir(worktree)


def _newest_surface_ref_transcript(project_dir: Path, session: Session) -> Path | None:
    """Return the newest ``<surface_ref>*.jsonl`` newer than session start, else None.

    The ``surface_ref``-prefix glob excludes sibling transcripts from other
    sessions that share the same project dir (reused worktree). Do NOT fall
    back to an unscoped ``*.jsonl`` glob — that would silently read a
    different session's transcript. Caller guarantees ``surface_ref`` is set.
    """
    surface_ref = session.surface_ref
    candidates = sorted(
        project_dir.glob(f"{surface_ref}*.jsonl"),
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


def _locate_session_transcript(session: Session) -> Path | None:
    """Return the session's transcript path, or None if not locatable.

    Resolution order:
    1. ``claude_session_id`` set and ``<project_dir>/<csid>.jsonl`` exists →
       return that path directly (mtime guard not needed; csid is exact).
    2. ``surface_ref`` set → newest ``<project_dir>/<surface_ref>*.jsonl``
       with mtime strictly after ``session.started_at``, else None
       (reused-worktree stale-transcript guard, #358/#372).
    3. No project_dir, or neither identifier set → None.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return None
    try:
        if session.claude_session_id is not None:
            path = project_dir / f"{session.claude_session_id}.jsonl"
            return path if path.is_file() else None
        if session.surface_ref is not None:
            return _newest_surface_ref_transcript(project_dir, session)
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
        branch = _deps.checked_out_branch(worktree_path)
        if not branch:
            return False
        client = get_client(client_name)
        return worktree_has_unsaved_work(client, branch)
    except Exception:  # noqa: BLE001
        return False


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


def resolve_stalled_retry_cap(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the wall-clock-budget stalled-stage auto-retry cap for a ticket.

    Precedence: task.scope_hint per-tier override, else the global default.
    See GitHub issue #756.
    """
    if task is None:
        return DEFAULT_STALLED_RETRY_CAP
    if task.scope_hint is not None:
        tier_cap = config.stalled_retry_cap_by_tier.get(task.scope_hint)
        if tier_cap is not None:
            return tier_cap
    return DEFAULT_STALLED_RETRY_CAP


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


def feature_branch_key(
    client_name: str,
    ticket_id: str,
    clients: dict[str, ClientConfig],
) -> str:
    """Return the git branch key for a ticket, respecting feature_branch_prefix.

    Looks up the client's :attr:`ClientConfig.feature_branch_prefix` (SSOT for
    the branch name the staged pipeline provisions and the auto-dev skills push
    to). Falls back to ``"dev"`` when the client is absent from *clients* so
    behaviour is identical to the old hardcoded ``"dev/" + ticket_id``.

    See GitHub issue #728.
    """
    client = clients.get(client_name)
    prefix = client.feature_branch_prefix if client is not None else "dev"
    return f"{prefix}/{ticket_id}"


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
) -> set[str]:
    """Emit SESSION_REAP_PROPOSED for reap-shaped candidates before act phase.

    Called from _reconcile_locked after each _detect_* and before the
    corresponding _act_on_*. Satisfies ADR-0006 invariant 3 (propose before act).

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    Returns the set of session_ids newly stamped in this call. Callers use this
    to gate edge-triggered events (e.g. SESSION_STAGE_TIMED_OUT_RETRIED) so they
    fire only on first detection, not on every re-detect tick. See GitHub #782.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.
    """
    _now = now or datetime.now(UTC)
    session_by_id = {s.id: s for s in state.sessions}
    newly_stamped: set[str] = set()

    for candidate in candidates:
        if candidate.proposed_action not in _REAP_PROPOSED_ACTIONS:
            continue
        session = session_by_id.get(candidate.session_id)
        if session is None or session.reap_proposed_at is not None:
            continue

        in_roster = (
            session.surface_ref is not None and session.surface_ref in native_live
        )

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
        newly_stamped.add(candidate.session_id)
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id or candidate.session_id,
        )

    if newly_stamped:
        save_state(state)
    return newly_stamped


# Non-underscore aliases for the cross-cutting helpers above, so cluster modules
# can call them as public attributes (``_shared.NAME``) without tripping the
# private-member-access lint. Routing every cluster's call through the single
# ``_shared`` attribute preserves the pre-split property that one test patch at
# ``cw.reconcile._shared.NAME`` intercepts all callers. These helpers are not
# called elsewhere inside this module, so there is no dual-name hazard.
detect_usage_limit = _detect_usage_limit
salvage_terminal_result = _salvage_terminal_result
worktree_dirty_by_path = _worktree_dirty_by_path
