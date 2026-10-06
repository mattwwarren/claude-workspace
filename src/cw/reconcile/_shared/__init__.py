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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from cw._transcript import locate_transcript
from cw._util import _iter_sentinel_text_blocks
from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
    _is_placeholder_sentinel_text,
    extract_block,
    is_documented_example,
    parse_stdout,
    queue_status_for_terminal_sentinel,
)
from cw.config import get_client, save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    OCCUPIED_LANE_STATUSES,
    TERMINAL_QUEUE_STATUSES,
    ClientConfig,
    CompletionReason,
    DevQueueStore,
    LastResultSource,
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
    extract_unresolved_spawn_count,
)
from cw.native_daemon import _is_native_surface_ref
from cw.reconcile import _deps
from cw.reconcile._shared._constants import (
    _CAUSE_IDLE_STALL,
    _CAUSE_USAGE_LIMIT,
    _DANGLING_TOOL_USE_REASON,
    _DIRTY_WORKTREE_REASON,
    _DISPATCH_LOOP_STALE_REASON,
    _EXTERNAL_COUNTERPARTY_IDLE_REASON,
    _FINALIZE_BLOCKED_REASON,
    _FIX_DISPATCH_REF_UNRESOLVED_REASON,
    _FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON,
    _FRESHNESS_BLOCK_ESCALATED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _LIVE_STATUSES,
    _MAIN_CHECKOUT_DRIFT_REASON,
    _NEEDS_SALVAGE_REASON,
    _NEVER_CLAIMED_COMPLETION_REASON,
    _PAUSED_STATUS_KEY,
    _PHANTOM_REAP_MERGED_REASON,
    _QUEUE_OPERATION_ENQUEUE,
    _QUEUE_OPERATION_RECORD_TYPE,
    _REAP_ELIGIBLE_DISPOSITIONS_BASE,
    _RESCUE_PR_BODY_TEMPLATE,
    _RESCUE_PR_CLOSES_TRAILER_TEMPLATE,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_PR_BODY_TEMPLATE,
    _SALVAGE_PR_TITLE_TEMPLATE,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _SESSION_ID_MISMATCH_ADVISORY_NOTE,
    _SESSION_UNRESPONSIVE_REASON,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _STALLED_CAP_PARKED_REASON,
    _STOPPED_WITHOUT_SENTINEL_REASON,
    _TIMED_OUT_MERGED_REASON,
    _UNCONSUMED_QUEUE_NOTIFICATION_REASON,
    _UNRESOLVED_SUBAGENT_SPAWN_REASON,
    _USAGE_LIMITED_MID_TURN_REASON,
    AUTO_DEV_LABEL_PREFIX,
    SPAWN_GRACE_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
    USAGE_LIMIT_SALVAGE_WINDOW_SECONDS,
)
from cw.reconcile._shared._detectors import (
    _SUBAGENT_SPAWNING_TOOL_NAMES,
    _TOOL_USE_COMMAND_SNIPPET_MAX_CHARS,
    DanglingToolUseEvidence,
    UsageLimitDetection,
    _apply_tool_use_block,
    _bash_command_snippet,
    _detect_dangling_tool_use,
    _detect_post_review_clean,
    _detect_provider_overload,
    _detect_unconsumed_queue_notification,
    _detect_usage_limit,
    _redact_and_truncate,
    _usage_limit_is_recent,
)
from cw.reconcile._shared._transcripts import (
    _csid_from_transcript,
    _effective_transcript_timestamp,
    _iter_assistant_records,
    _iter_notification_records,
    _iter_transcript_records,
    _locate_session_transcript,
    _newest_surface_ref_transcript,
    _project_transcripts_latest_timestamp,
    _session_project_dir,
    _transcript_age_seconds,
    _transcript_recently_active,
    _TranscriptRecordIterator,
    _widened_transcript_timestamp,
)
from cw.reconcile._shared._types import (
    ProposedAction,
    ReapCandidate,
    ReconcileReport,
    _apply_correction_signal_fields,
    _resolve_routed_sentinel,
)
from cw.result import (
    EmitOutcome,
    _record_result_emitted_audit,
    emit_result_on_audited,
    has_terminal_result,
    reconstruct_staged_sentinel,
)
from cw.worktree import (
    reconcile_result_scope,
    resolve_scope_guard_default_branch,
    unsaved_work_reason,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.dispatch import _StagePosition
    from cw.models import CwState, Session

_log = logging.getLogger(__name__)


def _claude_agents_json() -> list[dict[str, object]]:
    """Call ``claude agents --json`` and return the parsed list.

    Raises ``subprocess.CalledProcessError`` when the daemon is not running,
    or ``subprocess.TimeoutExpired`` if the call hangs past the timeout (#1230).
    """
    proc = subprocess.run(
        ["claude", "agents", "--json"],
        capture_output=True,
        text=True,
        check=True,
        # Why: bare literal (not a module constant) — single call site, matches
        # the RealNativeDaemonClient.stop timeout=10 precedent (native_daemon.py:352)
        # and keeps this fix minimal per #1230's scope fence (see .cw/plan.md).
        timeout=15,
    )
    data = json.loads(proc.stdout)
    return data if isinstance(data, list) else []


def _queue_status_for_salvaged(result: AutoDevResult) -> QueueItemStatus:
    """Map a salvaged AutoDevResult to the appropriate QueueItemStatus.

    Delegates to the shared dispatch/salvage classifier (#1566) so this path
    cannot drift from live dispatch's Rule 1/2/5/3b hold routing again.
    """
    return queue_status_for_terminal_sentinel(result.status)


def _validate_existing_result_for_routing(
    existing_result: dict[str, Any] | None,
) -> AutoDevResult | BlockedResult | None:
    """Validate a door-refused foreign ``existing_result`` for routing, or None.

    Reuses the door's own discriminated validation
    (:func:`cw.result._validate_harvest_payload`) against a **foreign, untrusted**
    dict written by an unknown authority. A validation failure means the shape is
    unroutable, so the caller falls through to the PENDING-requeue floor.

    RFC 0012 A3 (#1459): this read-side foreign-shape check is the one deliberate
    exception to R6's "no defensive ladder around this ticket's own emit sites"
    rule -- it validates a dict this ticket did NOT construct, so a
    ``EmitValidationError`` here is a genuine "is this even routable" reading, not
    a widening of a known-valid payload.

    Promoted here from ``cw.reconcile.concierge`` (#1470) so stalled.py's
    COMPLETE_FOREIGN_RESULT detect-phase guard can reuse it without a
    cross-module private import; concierge.py's own call site now delegates here.

    #1762 moved the body itself down to ``cw.result.reconstruct_staged_sentinel``
    -- the phantom sweep and the Stop hook need the same reconstruction, and
    ``cw.result`` owns the union it validates against. This name survives as the
    reconcile-side vocabulary for the *foreign-shape* reading (see above), not as
    a second implementation.
    """
    return reconstruct_staged_sentinel(existing_result)


def _foreign_result_target_queue_status(
    validated: AutoDevResult | BlockedResult,
) -> QueueItemStatus:
    """Map a validated foreign result to its target QueueItemStatus.

    Extracted from ``cw.reconcile.concierge``'s
    ``_route_park_marker_poison_task`` isinstance/status ladder (#1470) so both
    concierge.py's park-marker-poison routing and stalled.py's
    COMPLETE_FOREIGN_RESULT routing share one mapping.

    isinstance(BlockedResult) MUST precede the delegated
    ``_queue_status_for_salvaged`` call: ``BlockedResult`` has no
    ``AutoDevResult`` fields, so the isinstance check is what proves the else
    branch's operand is an ``AutoDevResult`` for mypy --strict (RFC 0012 A3
    #1459). The former ``status == "blocked"`` special case (#1470's "round-3
    bug fix") is gone: "blocked" is in ``STAGE_FAILURE_STATUSES``, so
    ``queue_status_for_terminal_sentinel`` now routes it to BLOCKED_ON_USER
    without a special case (#1566).
    """
    if isinstance(validated, BlockedResult):
        return QueueItemStatus.BLOCKED_ON_USER
    return _queue_status_for_salvaged(validated)


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
# Deliberately compared against raw ``target.attempts``, NOT the newer
# ``unproductive_attempts`` counter (GitHub #1750). This is a per-stage cap on
# repeated *sentinel validation failures*: a worker that keeps emitting an
# unparseable sentinel may well have committed real work each time, so it
# would read as productive and never trip a ceiling keyed on productivity.
# #1750 moved only the GLOBAL ceiling (dispatch/claim.py, concierge.py) to the
# new counter; this cap stays on the total claim count by design. See #756.
# GitHub #2401: also gates the deterministic-parse branch below
# (_DETERMINISTIC_PARSE_FAILURES) since the #2077 incident (session
# f1dd7a30) showed a schema_version_unsupported BlockedResult landing FAILED
# on the first occurrence can kill a worker that is, in fact, still working
# -- the same "may have committed real work each time" rationale applies
# identically to a deterministic parse failure as to validation_failed, so a
# same-occurrence-count cap replaces the prior immediate-abandon there too.
_VALIDATION_FAILED_MAX_ATTEMPTS = 3
_DETERMINISTIC_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED}
)
_TRANSIENT_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_NO_RESULT_EMITTED}
)
_TERMINAL_NO_RETRY_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES


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
        block = extract_block(text)
        if block is not None:
            if _is_placeholder_sentinel_text(block):
                continue
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

    Uses the same two-layer transcript search as
    :func:`_parse_any_sentinel_from_transcript` (csid-exact, then surface_ref-
    newest fallback when the csid transcript is absent or has no sentinel) so a
    terminal sentinel written before a resume/backfill is never missed
    (GitHub #1353; mirrors the #892 fix already applied to that sibling).

    Returns ``(result, claude_session_id)`` only when the parsed result is an
    :class:`AutoDevResult` whose status is in :data:`_SALVAGE_TERMINAL_STATUSES`.
    Returns ``None`` otherwise.
    """
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        isinstance(result, AutoDevResult)
        and result.status in _SALVAGE_TERMINAL_STATUSES
    ):
        return result, csid
    return None


def _verify_salvaged_scope(result: AutoDevResult, session: Session) -> AutoDevResult:
    """Correct a salvaged sentinel's self-reported scope against git facts (#1487).

    Salvage recovers a sentinel the worker wrote about itself; nothing has
    checked its ``scope.files``/``scope.lines_actual`` against the branch. A
    config failure must not cost us the sentinel, so an unresolvable client
    falls back to the ``main`` default rather than propagating.
    """
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _parse_any_sentinel_from_transcript(
    session: Session,
) -> tuple[AutoDevResult | BlockedResult, str] | None:
    """Parse any sentinel from the transcript, regardless of status.

    Like :func:`_salvage_terminal_result` but applies no status filter — returns
    the result for any valid parse including PAUSED_FOR_USER_INPUT statuses that
    :func:`_salvage_terminal_result` would skip.  Returns None when no sentinel
    framing is present (the no-frame case ``parse_stdout`` reports as
    BLOCKER_REASON_NO_RESULT_EMITTED).

    Uses a two-layer transcript search (mirrors queue_peek's locate logic):

    - Layer 1: csid-exact transcript (``<project_dir>/<csid>.jsonl``).  Returned
      immediately when a sentinel is found, so the csid transcript always wins
      when it contains the result.
    - Layer 2: surface_ref newest-only transcript (``<surface_ref>*.jsonl``).
      Fires when the csid transcript is absent **or** contains no sentinel — the
      latter catches the case where a REVIEW worker emitted the sentinel before
      spawning fanout subagents (the sentinel lives in the pre-resume V1 transcript
      while backfill has already updated ``claude_session_id`` to the resumed V2
      session that has no sentinel).  Skipped when Layer 2 would return the same
      path already tried in Layer 1.

    Used by the ROUTE_EMITTED_SENTINEL detection path for sessions where the
    sentinel was emitted but the Stop hook never fired.  See GitHub #578, #731,
    #892.
    """

    def _try(path: Path) -> tuple[AutoDevResult | BlockedResult, str] | None:
        result = _parse_sentinel_from_blocks(path)
        if result is None or (
            isinstance(result, BlockedResult)
            and result.blocker.reason == BLOCKER_REASON_NO_RESULT_EMITTED
        ):
            return None
        if isinstance(result, AutoDevResult):
            result = _verify_salvaged_scope(result, session)
        return result, path.stem

    project_dir = _session_project_dir(session)

    # Layer 1: csid-exact (does NOT fall through to surface_ref)
    csid_transcript: Path | None = None
    if session.claude_session_id is not None and project_dir is not None:
        csid_transcript = locate_transcript(
            project_dir=project_dir,
            claude_session_id=session.claude_session_id,
            surface_ref=None,
            started_at=session.started_at,
        )
        if csid_transcript is not None:
            parsed = _try(csid_transcript)
            if parsed is not None:
                return parsed

    # Layer 2: surface_ref newest-only — fires when csid transcript absent or
    # contains no sentinel.  Skip when it resolves to the same path as Layer 1.
    if session.surface_ref is not None and project_dir is not None:
        surface_transcript = _newest_surface_ref_transcript(project_dir, session)
        if surface_transcript is not None and surface_transcript != csid_transcript:
            return _try(surface_transcript)

    return None


def classify_sentinel_stage_position(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> tuple[_StagePosition, list[Stage] | None, int | None]:
    """Circular-safe re-export of dispatch's stage-position classifier (#1149).

    ``cw.dispatch`` imports ``cw.reconcile`` at module level, so a top-level
    import of the classifier from a reconcile sweep would create a cycle. This
    thin wrapper (co-located with ``_apply_sentinel_to_task``, which delegates to
    dispatch the same way) lets stalled.py's Path 1 backstop resolve a sentinel's
    stage position against ``task.stage`` without an inline import at its own call
    site. Returns ``(position, stages, target_idx)``; see
    ``dispatch._classify_sentinel_stage_position`` for the semantics.
    """
    from cw.dispatch import _classify_sentinel_stage_position

    return _classify_sentinel_stage_position(task, last_result, clients)


# #1692: genuinely terminal QueueItemStatus values -- distinct from
# "outside OCCUPIED_LANE_STATUSES", which also includes PENDING (redispatch-
# eligible, not terminal). Used by _apply_sentinel_to_task's lookup-miss
# branch to classify task_already_terminal precisely. Review round 2 (#1692):
# this was an independently-defined literal identical to
# cw.reconcile.review_recipes.auto_fix_ci._REQUEUE_ELIGIBLE_STATUSES --
# both now alias the single cw.models.TERMINAL_QUEUE_STATUSES definition.
_GENUINELY_TERMINAL_QUEUE_STATUSES: frozenset[QueueItemStatus] = TERMINAL_QUEUE_STATUSES


class SentinelRouteOutcome(NamedTuple):
    """Result of routing a sentinel through ``_apply_sentinel_to_task`` (#1019).

    ``rescued`` is True iff a parked (non-RUNNING) task was rescued via the
    #918 AutoDevResult arm. ``routed`` is False iff (a) the shared
    staged-advance guard refused the sentinel's stage position, (b)
    ``_route_blocked_result_to_task`` just landed the task terminal-FAILED via
    a BlockedResult, or (c) the lookup matched a same-ticket/session task
    outside ``OCCUPIED_LANE_STATUSES`` (raced to terminal by a concurrent
    caller). A truly-absent task is the only remaining ``routed=True`` miss
    shape (#1189).

    ``landed_terminal`` (#1273) is True only for cause (b) above -- this very
    call just wrote the task terminal-FAILED via
    ``_route_blocked_result_to_task``. It is False for causes (a) and (c) and
    for every ``routed=True`` case. The distinction matters because (a) and
    (c) both leave a still-legitimately-running (or already-handled) worker
    alone, while (b) means the worker backing this task is now leaked --
    ``routed=False`` alone can't tell them apart. Callers (``signal_stop``)
    use ``landed_terminal`` to `daemon.stop()` the leaked worker in the (b)
    case without touching a worker refused by the #986 stage-mismatch guard.

    GitHub #1406: a catch-all BlockedResult vetoed by the transcript-liveness
    guard re-queues to PENDING, so it reports ``routed=True`` and (derived from
    it) ``landed_terminal=False`` -- exactly the signal that keeps the still-
    advancing worker alive rather than stopping it as leaked.

    ``task_already_terminal`` (GitHub #1692) is True only for the raced-to-
    terminal lookup-miss branch of cause (c) above: a same-ticket/session task
    was found but had already been landed in a genuinely terminal status --
    ``COMPLETED``, ``FAILED``, or ``CANCELLED`` (see ``_GENUINELY_TERMINAL_
    QUEUE_STATUSES``) -- by a concurrent caller before this call's own lookup
    ran. A match outside ``OCCUPIED_LANE_STATUSES`` but not in that terminal
    set (i.e. ``PENDING``) is not terminal -- it is redispatch-eligible, so
    ``task_already_terminal`` is False for it. ``routed`` is still False in
    that case too, same as the terminal sub-cause -- both are a
    ``matched_excluded`` miss, and ``routed`` is ``not matched_excluded``
    regardless of which excluded status was matched; only a true "no such
    task anywhere" miss (R3b) reports ``routed=True``. It is mutually
    exclusive with ``landed_terminal``
    by construction -- ``landed_terminal`` is set only in the ``target is not
    None`` arm, ``task_already_terminal`` only in the ``target is None`` arm.
    Once a task is landed genuinely terminal under this exact session_id, no
    dispatch path will ever give this session another leg for this ticket, so
    a caller (``signal_stop``) can safely complete the now-leaked session on
    this sub-cause -- unlike a stage-mismatch refusal, where a still-advancing
    worker may legitimately produce a later, matching-stage sentinel.

    ``stage_refused`` (GitHub #2490) is True only when cause (a) above fired:
    the shared staged-advance guard refused the sentinel's stage position
    (``sentinel.stage_mismatch`` was emitted). It lets a caller whose session
    is provably dead tell that permanent refusal apart from the other
    ``routed=False`` causes, which are not stage mismatches. ``refused_stage``
    is the row's stage as read under ``dev_queue_lock`` at the moment of that
    refusal (None for every other outcome), so a page can name the row's live
    stage rather than a stale per-pass snapshot of it.
    """

    rescued: bool
    routed: bool
    landed_terminal: bool
    task_already_terminal: bool = False
    stage_refused: bool = False
    refused_stage: Stage | None = None


class _TaskLookupResult(NamedTuple):
    """Result of scanning ``store.tasks`` for a same-ticket/session match.

    Round-3 (#1692): excluded (non-``OCCUPIED_LANE_STATUSES``) matches are
    aggregated across the *entire* scan rather than last-seen-wins -- a
    terminal excluded row and a non-terminal (e.g. ``PENDING``) excluded row
    sharing the same ticket_id/session_id can both exist, and which one a
    naive last-seen implementation reports depends on scan order alone.
    ``seen_terminal_excluded``/``seen_nonterminal_excluded`` let the caller
    make the live/redispatch-eligible interpretation win whenever both are
    present, regardless of order.
    """

    target: TicketTask | None
    target_status: QueueItemStatus | None
    matched_excluded: bool
    seen_terminal_excluded: bool
    seen_nonterminal_excluded: bool
    terminal_excluded_status: QueueItemStatus | None
    terminal_excluded_client: str | None


class AuditedSentinelRouteOutcome(NamedTuple):
    """Result of routing a sentinel through the audited write-ahead seam."""

    route: SentinelRouteOutcome | None
    emit: EmitOutcome | None


def _lookup_matching_task(
    store: DevQueueStore, ticket_id: str, cw_session_id: str
) -> _TaskLookupResult:
    """Scan *store* for the task matching *ticket_id*/*cw_session_id*.

    Returns on the first occupied-status match (the ordinary live-completion
    case); otherwise keeps scanning to the end so a later occupied row is not
    missed behind an earlier excluded-status row (post-review amendment A2),
    aggregating every excluded match encountered along the way. See
    ``_TaskLookupResult`` for why the excluded-match fields are aggregated
    rather than last-seen.
    """
    target: TicketTask | None = None
    target_status: QueueItemStatus | None = None
    matched_excluded = False
    seen_terminal_excluded = False
    seen_nonterminal_excluded = False
    terminal_excluded_status: QueueItemStatus | None = None
    terminal_excluded_client: str | None = None
    for task in store.tasks:
        if task.ticket_id == ticket_id and task.session_id == cw_session_id:
            if task.status in OCCUPIED_LANE_STATUSES:
                target = task
                target_status = task.status
                break
            matched_excluded = True
            if task.status in _GENUINELY_TERMINAL_QUEUE_STATUSES:
                if not seen_terminal_excluded:
                    # First terminal match seen -- used for the
                    # SENTINEL_RACE_MISS payload.
                    terminal_excluded_status = task.status
                    terminal_excluded_client = task.client
                seen_terminal_excluded = True
            else:
                seen_nonterminal_excluded = True
    return _TaskLookupResult(
        target=target,
        target_status=target_status,
        matched_excluded=matched_excluded,
        seen_terminal_excluded=seen_terminal_excluded,
        seen_nonterminal_excluded=seen_nonterminal_excluded,
        terminal_excluded_status=terminal_excluded_status,
        terminal_excluded_client=terminal_excluded_client,
    )


# Everything ``load_dev_queue`` can raise for a dev_queue.json an operator
# (or a torn write) can actually leave on disk, verified against the real
# loader: ``OSError`` for an unreadable file; ``ValueError`` for a malformed
# one (``json.JSONDecodeError``, pydantic's ``ValidationError`` and
# ``UnicodeDecodeError`` are all subclasses); ``AttributeError`` for a payload
# whose top level is not an object, which reaches ``raw.get`` in
# ``migrate_dev_queue``. A queue we cannot read is treated as "no row"
# (fail-closed): the Stop hook must never raise out of claude exiting, and
# "defer" is the safe answer to every unknown here.
_DEV_QUEUE_LOAD_ERRORS = (OSError, ValueError, AttributeError)


def _load_dev_queue_or_none(purpose: str, ticket_id: str) -> DevQueueStore | None:
    """Load the dev queue, or ``None`` with one WARNING (#2135).

    The ``load_plan`` idiom (``dev_queue/storage.py``), applied at the two
    abandoned-exit park call sites only -- ``load_dev_queue`` itself keeps
    raising, because everywhere else a corrupt queue SHOULD be loud.

    *purpose* names the caller in the log so an operator can tell the cheap
    precondition read apart from the park's authoritative reload. The class
    name alone is logged, never a traceback: this fires on a hook that runs at
    every turn boundary.
    """
    try:
        return load_dev_queue()
    except _DEV_QUEUE_LOAD_ERRORS as exc:
        _log.warning(
            "%s skipped for ticket %s: dev queue unreadable (%s)",
            purpose,
            ticket_id,
            type(exc).__name__,
        )
        return None


def find_running_task_for_session(
    ticket_id: str, cw_session_id: str
) -> TicketTask | None:
    """Return the RUNNING dev-queue row *cw_session_id* owns, if any (#2135).

    A lock-free read (``load_dev_queue`` takes no lock) of the same lookup
    :func:`_route_stopped_without_sentinel` repeats authoritatively under
    ``dev_queue_lock`` before it mutates. This is the read-only precondition
    form: the Stop hook needs the row to resolve the abandoned-exit park's
    per-lane enablement, and a row that is absent or not RUNNING means there
    is nothing to park, so the marker read and transcript guard are skipped
    outright.

    An unreadable queue is "no row, defer" rather than an exception out of the
    Stop hook -- see :func:`_load_dev_queue_or_none`.
    """
    store = _load_dev_queue_or_none("find_running_task_for_session", ticket_id)
    if store is None:
        return None
    lookup = _lookup_matching_task(store, ticket_id, cw_session_id)
    if lookup.target is None or lookup.target_status is not QueueItemStatus.RUNNING:
        return None
    return lookup.target


def _route_stopped_without_sentinel(ticket_id: str, session: Session) -> None:
    """Park a headless task BLOCKED_ON_USER after an abandoned exit (GitHub #2135).

    The caller has already established the evidence: the Stop fired with empty
    ``background_tasks``, no sentinel was parsed, AND the worker recorded a
    ``park_comment_marker`` covering this session, ticket and row stage, with
    no sentinel framing text in the transcript after it.

    ``session.status`` is never touched -- a late sentinel still routes through
    the #918 rescue in :func:`_apply_sentinel_to_task`, which re-finds the row
    by the ``session_id`` this park deliberately leaves set.

    Two events follow, in this order: the ``task.transition``
    ``transition_task_status`` emits inline while ``dev_queue_lock`` is held,
    then ``session.needs_attention`` once that lock is released. Only a RUNNING
    row transitions -- ``AWAITING_OPERATOR_SIGNOFF`` and an already-parked row
    return before either event, so an operator-armed hold is never clobbered
    and a repeat Stop is idempotent.

    No ``fire_push_notification``: it backgrounds into a daemon thread
    (``cw/notify.py``) that a Stop-hook process exiting immediately afterwards
    would drop. The event bus already delivers ``session.needs_attention``.
    """
    with dev_queue_lock():
        store = _load_dev_queue_or_none("_route_stopped_without_sentinel", ticket_id)
        if store is None:
            return
        lookup = _lookup_matching_task(store, ticket_id, session.id)
        target = lookup.target
        if target is None or lookup.target_status != QueueItemStatus.RUNNING:
            return
        # The park post is positive evidence the stage did its work, so this
        # RUNNING exit is not charged against unproductive_attempts (#1750).
        transition_task_status(
            target,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=_STOPPED_WITHOUT_SENTINEL_REASON,
            unproductive=False,
        )
        save_dev_queue(store)
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _STOPPED_WITHOUT_SENTINEL_REASON,
            "breadcrumbs": (
                "Stop hook fired with no sentinel after the worker recorded "
                "its park comment"
            ),
            "crashed": False,
            "lane": session.lane,
        },
        correlation_id=ticket_id,
    )


def _apply_sentinel_to_task(
    ticket_id: str,
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    before_persist: Callable[[], bool | None] | None = None,
) -> SentinelRouteOutcome:
    """Update the matching dev-queue task based on the sentinel result.

    Shared by signal_stop (cli.py) and the ROUTE_EMITTED_SENTINEL reconcile
    path so both use the same sentinel→QueueItemStatus mapping.  Called before
    marking the session COMPLETED so the task is in its terminal state when
    revert_completed_silent_tasks runs.  See GitHub issues #251, #578.

    The lookup matches a RUNNING task (live completion) or a BLOCKED_ON_USER /
    AWAITING_OPERATOR_SIGNOFF task that still carries this session_id (an
    idle-parked or signoff-parked session whose late Stop-hook sentinel
    finally arrived, #918, #990). A parked task retains its session_id (the
    idle watchdog does not clear it), so the rescue can re-find it. Returns a
    ``SentinelRouteOutcome`` -- see its docstring for ``rescued``/``routed``.

    Takes the owning ``session`` (not just its id) because the BlockedResult
    arm records ``session.id`` on its ``SENTINEL_BLOCKED_RESULT_REQUEUED``
    audit event; the task lookup itself keys off ``session.id`` alone.

    GitHub #1692/#2140: the returned outcome's ``task_already_terminal`` flag
    is now consumed at all four call sites -- the Stop-hook (``cw.cli.
    stop_hook``, #1692's original fix) and the three reconcile-driven callers
    (``cw.reconcile.idle._mutations``, ``cw.reconcile.phantom._mutations``,
    and the LOCAL-DAEMON git-harvest reaper ``cw.reconcile.local``, #2140) --
    each completing the now-leaked session on this sub-cause instead of
    orphaning it.

    ``before_persist`` is an optional reconcile-only write-ahead hook. When a
    route is accepted, it runs after the in-memory queue mutation but before
    ``save_dev_queue``; a failure therefore cannot leave the persisted queue
    ahead of the caller's session mutation. Returning ``False`` abandons the
    in-memory queue mutation without persisting it.
    """
    cw_session_id = session.id
    with dev_queue_lock():
        store = load_dev_queue()
        # #1189: distinguishes "raced to terminal by a concurrent caller"
        # (R3a) from "no such task anywhere" (R3b). See _TaskLookupResult /
        # _lookup_matching_task for the round-3 (#1692) excluded-match
        # aggregation this depends on.
        lookup = _lookup_matching_task(store, ticket_id, cw_session_id)
        target = lookup.target
        target_status = lookup.target_status
        if target is None:
            if lookup.matched_excluded:
                # #1189: surface the race so an operator can tell "raced to
                # terminal by a concurrent caller" apart from "no such task
                # ever existed" -- both silently returned routed=True before
                # this fix, with no signal a race had occurred at all. WARNING
                # (not INFO) to match this module's convention for anomalous-
                # but-non-fatal conditions (see worktree_cleanup_skip_dirty).
                _log.warning(
                    "sentinel_race_miss_detected: ticket=%s session=%s",
                    ticket_id,
                    cw_session_id,
                )
            # Round-2 (#1692): SENTINEL_RACE_MISS fires only for a genuinely
            # terminal excluded row -- the same condition as
            # task_already_terminal below. A non-terminal excluded row (e.g.
            # PENDING) is redispatch-eligible, not a race, so it emits
            # nothing. Round-3: any non-terminal excluded match seen anywhere
            # in the scan vetoes the terminal classification, even if a
            # terminal match was also seen.
            already_terminal = (
                lookup.seen_terminal_excluded and not lookup.seen_nonterminal_excluded
            )
            if already_terminal:
                # #1692: durable trace alongside the log line -- record_event
                # nests the leaf inbox lock inside dev_queue_lock (ADR-0019), as
                # this module's SENTINEL_BLOCKED_RESULT_REQUEUED call (below)
                # does. No queue/session mutation
                # precedes this in this branch, so there is nothing for a
                # failed write to leave half-applied.
                record_event(
                    OrchestratorEventType.SENTINEL_RACE_MISS,
                    {
                        "ticket_id": ticket_id,
                        "client": lookup.terminal_excluded_client,
                        "session_id": cw_session_id,
                        "excluded_status": lookup.terminal_excluded_status,
                    },
                    correlation_id=ticket_id,
                )
            routed = not lookup.matched_excluded
            if routed and before_persist is not None and before_persist() is False:
                routed = False
            return SentinelRouteOutcome(
                rescued=False,
                routed=routed,
                landed_terminal=False,
                task_already_terminal=already_terminal,
            )

        rescued = False
        routed = True
        landed_terminal = False
        stage_refused = False
        mutated = True
        if isinstance(sentinel, AutoDevResult):
            # Delegate to the shared B2 staged advance decision so both the
            # consume path (_apply_events_to_store) and the reconcile
            # ROUTE_EMITTED_SENTINEL path use the same routing table (#698).
            # Why function-level import: cw.dispatch imports reconcile at
            # module level (see reconcile.py module docstring); a module-level
            # import here would create a circular dependency.
            from cw.dispatch import _route_staged_decision, apply_staged_decision

            clients = _deps.load_effective_clients()
            last_result = sentinel.model_dump(mode="json")
            if target_status == QueueItemStatus.RUNNING:
                routed = apply_staged_decision(
                    target, sentinel.status, last_result, clients
                )
            else:
                # #918: rescue an idle-parked (BLOCKED_ON_USER) task through the
                # same assert-free routing core so it lands in exactly the state
                # its RUNNING counterpart would.
                routed = _route_staged_decision(
                    target, sentinel.status, last_result, clients
                )
                rescued = routed
            # #1019: a stage-mismatch refusal is a true no-op -- the routing
            # core already left `target` untouched, but skip the write too.
            mutated = routed
            stage_refused = not routed
        elif target_status == QueueItemStatus.RUNNING:
            # BlockedResult on a live RUNNING task. A parked task falls through
            # to an implicit no-op — a BlockedResult carries no success signal,
            # so leave it parked (never a false FAILED/COMPLETED on a rescue
            # miss, #918/Comment 9).
            # #1189: `routed` reflects whether this call landed the task
            # terminal-FAILED (False) or re-queued it PENDING (True) --
            # callers must not complete/rescue the session on a FAILED
            # landing. Do NOT set `mutated = routed` here (unlike the
            # AutoDevResult arm above): _route_blocked_result_to_task ALWAYS
            # writes a real transition (FAILED or PENDING) that must be
            # persisted below even when routed=False -- routed=False means
            # "don't also complete the session," not "don't write the task."
            routed = _route_blocked_result_to_task(target, session, sentinel)
            landed_terminal = not routed
        else:
            # True no-op: a late BlockedResult against an already-parked task
            # carries no success signal and must not write (#918).
            mutated = False

        if routed and before_persist is not None and before_persist() is False:
            routed = False
            mutated = False
        if mutated:
            save_dev_queue(store)
        return SentinelRouteOutcome(
            rescued=rescued,
            routed=routed,
            landed_terminal=landed_terminal,
            stage_refused=stage_refused,
            refused_stage=target.stage if stage_refused else None,
        )


def _apply_sentinel_to_task_audited(
    ticket_id: str | None,
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    source: LastResultSource,
    audit_existing_result: bool = False,
) -> AuditedSentinelRouteOutcome:
    """Apply a reconcile sentinel with one audited write-ahead operation.

    For a fresh sentinel, first-writer arbitration and the audit event happen
    before the queue is persisted. Existing-result callers (the stalled sweep)
    only record the already-present sentinel and leave its session mutation to
    their caller. Keeping both forms here prevents the idle, phantom, stalled,
    and local reconcile paths from drifting apart on ordering or refusal.
    """
    payload = sentinel.model_dump(mode="json")
    emit_outcome: EmitOutcome | None = None

    def before_persist() -> bool:
        nonlocal emit_outcome
        if audit_existing_result:
            _record_result_emitted_audit(
                session, payload, source=source, status=sentinel.status
            )
            return True
        emit_outcome = emit_result_on_audited(session, payload, source=source)
        return not emit_outcome.refused

    if ticket_id is None:
        if audit_existing_result:
            _record_result_emitted_audit(
                session, payload, source=source, status=sentinel.status
            )
        else:
            emit_outcome = emit_result_on_audited(session, payload, source=source)
        return AuditedSentinelRouteOutcome(None, emit_outcome)

    route = _apply_sentinel_to_task(
        ticket_id,
        session,
        sentinel,
        before_persist=before_persist,
    )
    if emit_outcome is not None and emit_outcome.refused:
        # The queue was deliberately not persisted by before_persist(). Make
        # the refusal shape explicit to callers that otherwise complete on a
        # task-already-terminal route.
        route = route._replace(routed=False, task_already_terminal=True)
    elif audit_existing_result and route.task_already_terminal:
        # No callback runs on the lookup-miss/raced-terminal branch, but the
        # existing session sentinel is still a successful completion audit.
        _record_result_emitted_audit(
            session, payload, source=source, status=sentinel.status
        )
    elif not audit_existing_result and route.task_already_terminal:
        # There is no queue mutation to persist in this branch, so finish the
        # accepted emit directly after the lookup result is known.
        emit_outcome = emit_result_on_audited(session, payload, source=source)
    return AuditedSentinelRouteOutcome(route, emit_outcome)


def _requeue_blocked_result_under_cap(
    target: TicketTask, session: Session, sentinel: BlockedResult
) -> bool:
    """Land FAILED at the attempt cap, else re-queue to PENDING (GitHub #2401).

    Shared by the deterministic-parse, validation_failed, and unrecognized-
    reason catch-all branches of :func:`_route_blocked_result_to_task`, which
    became identical once all were put under
    ``_VALIDATION_FAILED_MAX_ATTEMPTS`` (#2401, #2405): a worker that keeps
    emitting the same rejected sentinel may well have committed real work
    each time, so the cap is evidence-based (repeated identical rejection),
    never a transcript-age/clock comparison (ADR-0014).

    At the cap: persists the rejected sentinel to ``last_blocked_result``
    (#1266) and lands terminal FAILED/abandoned, returning False. Under the
    cap: re-queues to PENDING, clears ``target.session_id``, and emits
    ``SENTINEL_BLOCKED_RESULT_REQUEUED`` -- a re-queue rejects nothing, so
    there is no rejected sentinel for the field, but the event still leaves
    an operator-visible trace of what happened -- returning True.
    """
    if target.attempts >= _VALIDATION_FAILED_MAX_ATTEMPTS:
        target.last_blocked_result = sentinel.model_dump(mode="json")
        transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
        return False
    transition_task_status(target, QueueItemStatus.PENDING)
    target.session_id = None
    record_event(
        OrchestratorEventType.SENTINEL_BLOCKED_RESULT_REQUEUED,
        {
            "ticket_id": target.ticket_id,
            "client": target.client,
            "session_id": session.id,
            "blocker_reason": sentinel.blocker.reason,
            "attempts": target.attempts,
            "attempt_cap": _VALIDATION_FAILED_MAX_ATTEMPTS,
        },
        correlation_id=target.ticket_id,
    )
    return True


def _land_blocked_result_failed(target: TicketTask, sentinel: BlockedResult) -> bool:
    """Persist a rejected sentinel and land the task terminal FAILED."""
    target.last_blocked_result = sentinel.model_dump(mode="json")
    transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
    return False


def _blocked_result_requeue_enabled(
    target: TicketTask, sentinel: BlockedResult
) -> bool:
    """Return the client's #2401 deterministic-parse rollout setting."""
    try:
        enabled = get_client(target.client).blocked_result_requeue_enabled
    except CwError:
        # Fail closed when a legacy/local queue row has no usable clients.yaml
        # entry; rollout must be explicitly enabled for the client.
        enabled = False
    if not enabled:
        _log.warning(
            "sentinel.blocked_result_requeue_shadowed: client=%s ticket=%s; "
            "reason=%s attempts=%s attempt_cap=%s cap_only_would_requeue=%s; "
            "blocked_result_requeue_enabled is false",
            target.client,
            target.ticket_id,
            sentinel.blocker.reason,
            target.attempts,
            _VALIDATION_FAILED_MAX_ATTEMPTS,
            target.attempts < _VALIDATION_FAILED_MAX_ATTEMPTS,
        )
    return enabled


def _route_blocked_result_to_task(
    target: TicketTask,
    session: Session,
    sentinel: BlockedResult,
) -> bool:
    """Route a malformed/unparseable BlockedResult to a RUNNING task's status.

    A BlockedResult means the sentinel failed to parse or was malformed.
    Deterministic parse failures re-queue to PENDING (clearing session_id)
    under a rollout gate; the unrecognized-reason catch-all re-queues under
    the shared evidence-based attempt cap unconditionally, landing FAILED only
    once the cap is reached (GitHub #2401, #2405). Transient failures re-queue
    unconditionally.
    Extracted from _apply_sentinel_to_task to keep that function under the
    branch cap (#918).

    Returns False when this call just landed the task terminal-FAILED (the
    caller must not also complete the owning session on that outcome), True
    when it re-queued to PENDING instead (#1189).

    GitHub #2405 (ADR-0014 audit): the catch-all's former #1406 transcript-
    liveness veto is gone -- no transcript-age comparison decides FAILED vs.
    PENDING. A still-advancing
    worker whose sentinel merely failed to *parse* is protected by the attempt
    cap instead: it is re-queued until it has been rejected
    ``_VALIDATION_FAILED_MAX_ATTEMPTS`` times, and only then landed terminal
    (and, via #1273's ``landed_terminal``, has its daemon stopped).
    """
    # GitHub #2401: the branches below share _requeue_blocked_result_under_
    # cap's attempt-cap gate, closing the #1266 last_blocked_result gap -- a
    # FAILED/abandoned landing from any of them records why, and a sub-cap
    # re-queue leaves a SENTINEL_BLOCKED_RESULT_REQUEUED audit event since
    # there is no rejected sentinel to store on a non-terminal landing.
    if sentinel.blocker.reason in _DETERMINISTIC_PARSE_FAILURES:
        if not _blocked_result_requeue_enabled(target, sentinel):
            return _land_blocked_result_failed(target, sentinel)
        return _requeue_blocked_result_under_cap(target, session, sentinel)
    if sentinel.blocker.reason == BLOCKER_REASON_VALIDATION_FAILED:
        return _requeue_blocked_result_under_cap(target, session, sentinel)
    if sentinel.blocker.reason in _TRANSIENT_PARSE_FAILURES:
        transition_task_status(target, QueueItemStatus.PENDING)
        target.session_id = None
        return True
    # An unparseable/unknown-status sentinel (status_unknown,
    # multiple_result_blocks, any unrecognized reason) carries NO success
    # signal. Never mark it COMPLETED — that silently retires unshipped work
    # as "shipped" (#750, the #728 loss). GitHub #2405 (ADR-0014 audit): it
    # shares the same evidence-based attempt cap as the branches above, so a
    # FAILED landing is decided by repeated rejection, never transcript age.
    return _requeue_blocked_result_under_cap(target, session, sentinel)


def _apply_salvaged_completion(
    session: Session,
    result: AutoDevResult,
    claude_session_id: str,
    *,
    now: datetime,
) -> EmitOutcome:
    """Mark ``session`` COMPLETED from a salvaged sentinel (like signal_stop).

    RFC 0012 A3 (#1459): the ``last_result`` write is routed through the door
    (:func:`emit_result_on`, source=SALVAGE_TRANSCRIPT) FIRST. On a first-
    writer-wins refusal -- another authority already recorded a terminal result
    for this session -- the status/completed_at/completed_reason/cost_usd/
    claude_session_id mutations are ALL skipped and the refusal ``EmitOutcome``
    is returned so the caller can drop this candidate from its downstream
    ticket-routing / event-emission accounting. The door's own ``emit_result_on``
    warning already logs ``existing_source``/``attempted_source`` on refusal, so
    no duplicate log is emitted here.

    Returns the ``EmitOutcome`` (``refused=True`` when the door declined). All
    four callers (phantom/idle/stalled/concierge) check ``.refused``.
    """
    outcome = emit_result_on_audited(
        session,
        result.model_dump(mode="json"),
        source=LastResultSource.SALVAGE_TRANSCRIPT,
    )
    if outcome.refused:
        return outcome
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    if result.cost_usd is not None:
        session.cost_usd = result.cost_usd
    session.claude_session_id = claude_session_id
    return outcome


def _worktree_dirty_reason_by_path(
    client_name: str, worktree_path: Path | None
) -> str | None:
    """Return why the worktree at *worktree_path* has unsaved work, or None.

    Uses worktree_path (always set on DAEMON sessions) instead of
    session.branch (always None on DAEMON sessions, making the branch-based
    check a production no-op). Fail-safe *direction*: a None/empty
    worktree_path, an unresolvable checked-out branch, or any other error all
    return None (not dirty) — the opposite direction from
    unsaved_work_reason's own inner fail-safe (which leans toward "has
    unsaved work" on a git-level error), preserved here unchanged since the
    three direct unit tests on this outer wrapper pin it.
    """
    if not worktree_path:
        return None
    try:
        branch = _deps.checked_out_branch(worktree_path)
        if not branch:
            return None
        client = get_client(client_name)
        return unsaved_work_reason(client, branch, wt_path=worktree_path)
    except Exception:  # noqa: BLE001 — fail-safe on any error (client lookup or git)
        return None


def _read_agent_spawn_stamp_context(
    worktree_path: Path | None,
) -> dict[str, Any] | None:
    """Return the parsed ``.claude/cw-context.json`` for *worktree_path*, or None.

    Shared read half of :func:`_read_unresolved_subagent_spawn` and
    :func:`_unresolved_subagent_spawn_age_seconds` (#2012) so the two readers
    of the same on-disk stamp cannot drift onto different fail-open rules.

    Fail-open (``None``) on a None path, a missing worktree, a missing or
    pre-v5 context, malformed JSON, a non-dict payload, or any other error.
    Both callers translate that ``None`` into their own "no evidence" answer.

    Reads the file directly rather than via ``cw.cli._hook_io``: reconcile must
    not import from ``cw.cli`` (the dependency runs the other way).
    """
    if not worktree_path:
        return None
    try:
        context_path = worktree_path / HOOK_CONTEXT_RELATIVE_PATH
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fail-safe on any error; mirrors _worktree_dirty_reason_by_path
        return None
    return context if isinstance(context, dict) else None


def _unresolved_subagent_spawn_age_seconds(
    worktree_path: Path | None, now: datetime
) -> float | None:
    """Return how long *worktree_path*'s unresolved subagent spawn has run.

    Age sibling of :func:`_read_unresolved_subagent_spawn` (#2012), reading the
    ``last_stamped_at`` half of the same ``agent_spawn_stamp`` payload that
    ``cw agent-spawn-pre`` already writes — purely additive on the read side,
    no new writer. ``last_stamped_at`` advances only when the count *increases*
    (see :func:`cw.cli.agent_spawn_stamp._adjust_unresolved_count`), so it
    answers "when did the oldest outstanding spawn begin", which is exactly the
    quantity a deadline needs.

    Returns ``None`` — meaning "no bound available" — when there is no
    outstanding spawn, when the context is unreadable, or when
    ``last_stamped_at`` is missing or unparseable. Every one of those is the
    same fail-open direction as ``_read_unresolved_subagent_spawn``'s ``False``:
    the caller must treat "no bound" as "do not suppress", never as "suppress
    forever" — the unbounded-suppression bug this exists to close.

    A naive ``last_stamped_at`` is read as UTC (matching the writer, which
    stamps ``datetime.now(UTC).isoformat()``) so an old tz-less stamp cannot
    raise on the subtraction. The result may be negative if the stamp is in the
    future relative to *now*; callers compare against a positive deadline, for
    which a negative age correctly reads as "not yet exceeded".
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None or extract_unresolved_spawn_count(context) <= 0:
        return None
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    raw = (
        stamp.get(AGENT_SPAWN_LAST_STAMPED_AT_KEY) if isinstance(stamp, dict) else None
    )
    if not isinstance(raw, str):
        return None
    try:
        stamped_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamped_at.tzinfo is None:
        stamped_at = stamped_at.replace(tzinfo=UTC)
    return (now - stamped_at).total_seconds()


def _read_unresolved_subagent_spawn(worktree_path: Path | None) -> bool:
    """Return True iff *worktree_path* carries an unresolved subagent spawn.

    Reads the ``agent_spawn_stamp`` counter the ``cw agent-spawn-pre`` /
    ``cw agent-spawn-post`` hook pair maintains in the worktree's
    ``.claude/cw-context.json`` (#1646). A count above zero means a subagent
    spawn started and its matching Post hook never fired — the worker died or
    hung mid-spawn.

    Fail-open in one direction only, mirroring ``_worktree_dirty_reason_by_path``:
    a None path, a missing worktree, a missing or pre-v5 context, malformed
    JSON, a non-dict payload, a non-dict stamp, a non-int count, or any other
    error all return False. Reporting an unresolved spawn on ambiguous evidence
    would park healthy tickets under a reason that also overrides
    ``reap_policy: auto`` — strictly worse than missing one crash's precision.

    Reads the file via :func:`_read_agent_spawn_stamp_context` rather than
    ``cw.cli._hook_io``: reconcile must not import from ``cw.cli`` (the
    dependency runs the other way). The shared path constant and the
    count-extraction logic both come from ``cw.models``
    (:func:`cw.models.extract_unresolved_spawn_count`) so this reader and
    ``cw.cli.agent_spawn_stamp``'s write-side reader cannot drift onto
    different literals or validation rules for the same on-disk shape (#1646
    review finding).

    Deliberately **age-blind**, and unchanged by #2012: its callers (the
    phantom sweep) ask "did this worker die mid-spawn", for which any
    outstanding stamp is evidence no matter how old. The age-bounded question
    belongs to :func:`_unresolved_subagent_spawn_age_seconds`.
    """
    context = _read_agent_spawn_stamp_context(worktree_path)
    if context is None:
        return False
    return extract_unresolved_spawn_count(context) > 0


def _apply_queue_mutations(
    mutations: dict[str, QueueItemStatus],
    clear_session_id: set[str],
    disposition: str | None = None,
) -> list[str]:
    """Apply ticket-status mutations to the dev queue under dev_queue_lock.

    *mutations* maps ticket_id → target QueueItemStatus for RUNNING tasks.
    *clear_session_id* is the subset of ticket_ids whose session_id should be
    set to None (only PENDING-routed tasks; BLOCKED_ON_USER tasks keep their
    session_id for operator traceability).
    *disposition* is stamped on every mutated task via
    ``transition_task_status`` — each call site passes a single reason string
    appropriate to its own sweep (safe because each call's *mutations* dict is
    built from that sweep's own homogeneous candidate population in one tick).
    See GitHub #976.

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
            transition_task_status(
                task, mutations[task.ticket_id], disposition=disposition
            )
            if task.ticket_id in clear_session_id:
                task.session_id = None
            mutated.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return mutated


class SessionLivenessForTask(NamedTuple):
    """A dev-queue row's owning cw ``Session`` plus that session's daemon liveness.

    ``native_surface`` distinguishes "this surface_ref is a daemon short id, and
    the roster genuinely does not list it" from "this surface_ref belongs to some
    other surface kind, so roster membership says nothing" -- the check
    ``cw dev-queue wait``'s ATTENTION predicate has always made and the reason
    ``in_roster`` alone is not a sufficient liveness answer.
    """

    session: Session
    surface_ref: str | None
    native_surface: bool
    in_roster: bool


def resolve_session_for_task(task: TicketTask, state: CwState) -> Session | None:
    """Resolve *task*'s owning cw ``Session`` in *state* (hops 1-2 of the chain).

    ``TicketTask.session_id`` is cw's own ``Session.id`` -- NOT the daemon
    roster's short id (that is ``Session.surface_ref``) and NOT a transcript
    filename (that is ``Session.claude_session_id``). See the session-id
    namespaces section in ARCHITECTURE.md. Returns ``None`` when *task* has no
    session_id or it resolves to no session in *state*.
    """
    if task.session_id is None:
        return None
    return next((s for s in state.sessions if s.id == task.session_id), None)


def session_daemon_liveness(
    session: Session, live_short_ids: set[str]
) -> SessionLivenessForTask:
    """Resolve *session*'s liveness against the daemon roster (hop 3 of the chain).

    Split from :func:`resolve_session_for_task` so a caller that already holds
    the ``Session`` (``cw dev-queue wait``'s ``_check_stale_attention``) does not
    re-resolve it, and a caller that only wants the lookup
    (``_blocked_on_user_exit_code``) does not pay for a roster query.

    *live_short_ids* is passed in rather than fetched here: ``reconcile`` already
    queries ``claude agents --json`` exactly once per tick, and a helper that
    re-queried per session would turn one subprocess into one per row.
    """
    surface_ref = session.surface_ref
    native_surface = surface_ref is not None and _is_native_surface_ref(surface_ref)
    return SessionLivenessForTask(
        session=session,
        surface_ref=surface_ref,
        native_surface=native_surface,
        in_roster=native_surface and surface_ref in live_short_ids,
    )


def resolve_session_liveness_for_task(
    task: TicketTask, state: CwState, live_short_ids: set[str]
) -> SessionLivenessForTask | None:
    """Resolve *task*'s owning session AND its liveness against the daemon roster.

    The full three-hop chain ``cw dev-queue wait`` already gets right (GitHub
    #1738/#1774/#1762): ``task.session_id`` -> a ``Session`` in *state* by
    ``.id`` -> that session's ``surface_ref`` -> the live daemon roster. Extracted
    here so a second consumer never re-derives it from a bare roster or
    transcript-filename comparison, which is what produced the "session_id
    mismatch" reports on #1738/#1774 (three namespaces compared as if they were
    one). Returns ``None`` when the task resolves to no session.
    """
    session = resolve_session_for_task(task, state)
    if session is None:
        return None
    return session_daemon_liveness(session, live_short_ids)


def find_live_sessions_for_ticket(
    state: CwState, ticket_id: str, client: str, live_short_ids: set[str]
) -> list[Session]:
    """Every session in *state* for (*ticket_id*, *client*) still live in the
    daemon roster (GitHub #2275).

    Unlike :func:`resolve_session_liveness_for_task`, does NOT key off any
    ``TicketTask.session_id`` -- derives the ticket id from each
    ``Session.name`` via :func:`ticket_id_for_session`, so a stray session no
    dev-queue row points at any more is still found.
    """
    live: list[Session] = []
    for session in state.sessions:
        if session.client != client or session.status not in _LIVE_STATUSES:
            continue
        if ticket_id_for_session(session.name) != ticket_id:
            continue
        liveness = session_daemon_liveness(session, live_short_ids)
        if liveness.native_surface and liveness.in_roster:
            live.append(session)
    return live


def _session_id_advisory_mismatch(
    liveness: SessionLivenessForTask | None, spawn_cutoff: datetime
) -> bool:
    """True when *liveness* is the advisory-worthy shape (#1762).

    Either the row's session_id resolved to nothing at all, or it resolved to a
    session whose native daemon surface is absent from the roster. A session
    still inside its spawn-grace window is never flagged: it may simply not have
    registered with the daemon yet, the same allowance :func:`compute_drift`
    makes before calling a surface phantom.
    """
    if liveness is None:
        return True
    if liveness.session.started_at > spawn_cutoff:
        return False
    return liveness.native_surface and not liveness.in_roster


def _stamp_session_id_mismatch_advisories(
    state: CwState, live_short_ids: set[str], *, now: datetime | None = None
) -> None:
    """Flag RUNNING rows whose session_id no longer resolves to a live session.

    GitHub #1762: makes the namespace-confusion signal operator-visible in
    ``cw dev-queue tasks``'s REASON column via ``TicketTask.advisory_note``,
    instead of leaving an operator to compare a row's session_id against a
    roster short id by hand and conclude, wrongly, that cw itself lost track.

    A resolvable, roster-live row is never flagged, nor is one still inside its
    spawn-grace window, and an existing note is cleared the moment the condition
    lifts -- this is a live re-derivation each tick, not a latch, so no history
    is kept. See :func:`_session_id_advisory_mismatch` for the predicate.

    Writes under ``dev_queue_lock`` through the same load/save path
    :func:`_apply_queue_mutations` above uses.
    """
    spawn_cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status is not QueueItemStatus.RUNNING or task.session_id is None:
                continue
            liveness = resolve_session_liveness_for_task(task, state, live_short_ids)
            is_mismatch = _session_id_advisory_mismatch(liveness, spawn_cutoff)
            new_note = _SESSION_ID_MISMATCH_ADVISORY_NOTE if is_mismatch else None
            if task.advisory_note == new_note:
                continue
            _log.warning(
                "session_id_mismatch_advisory_%s: ticket=%s task_session_id=%s "
                "surface_ref=%s",
                "set" if new_note else "cleared",
                task.ticket_id,
                task.session_id,
                liveness.surface_ref if liveness else None,
            )
            task.advisory_note = new_note
            changed = True
        if changed:
            save_dev_queue(store)


def holds_staged_emit_result(session: Session) -> bool:
    """True when *session* carries a staged, still-routable ``cw result emit`` result.

    The shared base predicate for "an emit_cli result is staged and not yet
    routed" -- EMIT_CLI source plus a terminal-shaped sentinel. Hoisted here
    (#2458) after three independent reviewers found this exact pair
    re-derived four times across three files (``cw.cli.stop_hook``'s
    ``_peek_staged_emit_result`` and ``_sentinel_unroutable``,
    ``cw.reconcile.idle._detect``'s ``_holds_staged_emit_result``, and
    ``cw.cli.spawn``'s ``_route_staged_emit_result``): this predicate is the
    load-bearing gate for the exact defect class #2458 exists to fix, so a
    future change to what counts as "staged and routable" now has one site to
    update instead of four. Each caller ANDs its own extra context-specific
    condition (DAEMON origin, no route refusal yet, etc.) on top of this base
    pair.
    """
    return (
        session.last_result_source is LastResultSource.EMIT_CLI
        and _has_terminal_sentinel(session)
        and not _sentinel_partial_route_consumed(session)
    )


def _sentinel_partial_route_consumed(session: Session) -> bool:
    """True when a #2458 partial route already routed this staged sentinel.

    See ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY`` for why this is a merged-in
    flag rather than a ``_PAUSED_STATUS_KEY`` replacement: the session's
    terminal-shaped ``last_result`` must stay terminal-shaped (so a later Stop
    hook can still complete the session from it) while no longer counting as
    *staged and routable* for ``holds_staged_emit_result``'s callers.
    """
    last_result = session.last_result
    return (
        isinstance(last_result, dict)
        and last_result.get(_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY) is True
    )


def _stamp_sentinel_partial_route_consumed(session: Session) -> None:
    """Merge ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY`` into ``session.last_result``.

    Mutates *session* in place; the caller is responsible for ``save_state``.
    A no-op guard (``isinstance`` check) mirrors the merge-safe convention
    ``stalled/_mutations.py`` and ``phantom/_mutations.py`` already use for
    ``_SENTINEL_ADVANCE_REFUSED_KEY`` -- called only when ``last_result`` is
    already known terminal-shaped (the emit-precedence path that ran
    immediately before this call), so the ``dict`` branch is the only one that
    should ever execute; the ``else`` is defensive, not an expected path.
    """
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {
            **existing,
            _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY: True,
        }


def stage_refusal_latched(session: Session) -> bool:
    """True once a stage-mismatch refusal was latched on *session* (#1149, #2490).

    The one read side of the refusal latch the phantom, stalled and local-harvest
    sweeps (and the idle sweep's staged-result producer) all stamp: the
    single-key ``_PAUSED_STATUS_KEY`` marker, or the ``_SENTINEL_ADVANCE_REFUSED_KEY``
    flag merged into a ``last_result`` dict that already held other content.
    """
    last_result = session.last_result
    return isinstance(last_result, dict) and (
        last_result.get(_PAUSED_STATUS_KEY) == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        or last_result.get(_SENTINEL_ADVANCE_REFUSED_KEY) is True
    )


def stamp_stage_refusal(session: Session) -> None:
    """Latch a stage-mismatch refusal on *session* (the write side).

    A refused candidate leaves the task row and the session untouched, so
    without a latch every tick re-detects it and re-refuses it forever. A
    pre-existing ``last_result`` dict is merged into under its own key, never
    overwritten: it may carry another sweep's ``paused_status`` park marker
    (idle's ``silently_idle``, salvage's ``needs_salvage``) that stalled's
    SKIP_PARKED check still has to read. Only a missing ``last_result`` gets the
    single-key stamp. Mutates *session* in place; the caller owns ``save_state``.

    The idle sweep's own stamp (``idle/_mutations.py``) is a deliberate
    single-key variant (#2458) and does not use this helper.
    """
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {**existing, _SENTINEL_ADVANCE_REFUSED_KEY: True}
    else:
        session.last_result = {
            _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }


def _has_terminal_sentinel(session: Session) -> bool:
    """True when the session has already emitted a terminal sentinel.

    A real AUTO_DEV sentinel dump always carries a ``"status"`` key; the park
    markers (``silently_idle``/``needs_salvage``) carry ``"paused_status"`` and
    no ``"status"``. Key presence — not value — is the structural discriminant,
    so a parked session is correctly NOT treated as terminal and the idle
    watchdog re-checks it for a late terminal sentinel. See #418, #497.

    Thin delegation onto ``cw.result.has_terminal_result`` (RFC 0012 S2,
    #1456), which now owns this predicate since the emit_result_locked door
    also uses it to arbitrate first-writer-wins.
    """
    return has_terminal_result(session.last_result)


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


def resolve_attempt_ceiling(
    client: ClientConfig | None,
    task: TicketTask,
    global_cfg: OrchestratorConfig,
) -> int | None:
    """Resolve the effective attempt ceiling for a task's lane.

    Precedence (highest to lowest):
      1. Lane-level ``LaneConfig.attempt_ceiling`` for ``task.lane``.
      2. Global ``OrchestratorConfig.global_attempt_ceiling``.

    Returns ``None`` when the lane sets ``attempt_ceiling=False`` — an explicit
    "this lane has no ceiling", not a missing value. A supervised lane
    (``signoff: operator``) has a human answering every park, so the human IS
    the rate limiter an automated bound exists to be; that is the case #1751
    exists to express. Callers must therefore treat ``None`` as "never park on
    the ceiling", never as "fall back to some default".

    A ``None`` *client* (unresolvable / removed from clients.yaml) or a lane
    name not declared in that client's lanes falls through to the global
    config, keeping behaviour identical to the pre-#1751 flat read — the same
    fallthrough contract :func:`resolve_reap_policy` gives its own candidates.

    Takes a single ``client`` rather than the ``clients`` dict its sibling
    resolvers take: neither real call site holds a dict of every client at that
    depth (``cw.dispatch.claim`` is handed one ``ClientConfig``; the concierge
    detect functions do a per-task ``get_client()``), and threading one down
    would touch four call chains for no gain.

    Lives here, not in ``cw.dispatch``, because both consumers need it and the
    import direction only runs ``cw.dispatch -> cw.reconcile`` (#786, #1750,
    #1751).
    """
    if client is not None:
        for lane_cfg in client.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.attempt_ceiling is not None:
                if lane_cfg.attempt_ceiling is False:
                    return None
                return lane_cfg.attempt_ceiling
    return global_cfg.global_attempt_ceiling


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
        ProposedAction.CLOSE_ROUTED_RESULT_SESSION,
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

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER and the
    proposal-only CLOSE_ROUTED_RESULT_SESSION (#2524) candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    Returns the set of session_ids newly stamped in this call. Callers use this
    to gate edge-triggered events (e.g. SESSION_STAGE_TIMED_OUT_RETRIED) so they
    fire only on first detection, not on every re-detect tick. See GitHub #782.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.

    evidence.transcript_age_seconds reuses the same content-aware staleness
    computation (_transcript_age_seconds) the liveness veto decided on (#1427);
    evidence.transcript_mtime_age_seconds is the raw file-mtime age, retained
    separately for diagnostics.
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

        # Content-aware staleness — same computation the liveness veto used to
        # make its park/no-park decision (#976, #1277), so the audit evidence
        # never diverges from what was actually decided (#1427).
        transcript_age_seconds = _transcript_age_seconds(session, _now)

        # Raw mtime age, retained separately for diagnostics only — a trailing
        # metadata-only record (queue-operation/ai-title/mode/...) can bump
        # this far above transcript_age_seconds; do not confuse the two (#1427).
        transcript_mtime_age_seconds: float | None = None
        transcript_path = _locate_session_transcript(session)
        if transcript_path is not None and transcript_path.exists():
            with contextlib.suppress(OSError):
                mtime = transcript_path.stat().st_mtime
                transcript_mtime_age_seconds = _now.timestamp() - mtime

        payload: dict[str, object] = {
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
                "transcript_mtime_age_seconds": transcript_mtime_age_seconds,
            },
        }
        # #1625: stalled_retry_cap_parked carries the correction-signal fields
        # (crashed is always False on this park path — it never corresponds to
        # a crash) so a consumer doesn't have to cross-reference the task
        # record by hand. Scoped strictly to this reap_reason — other reasons
        # (wall-clock budget, usage-limit cutoff, etc.) do not carry these keys.
        if candidate.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED:
            payload["crashed"] = False
            _apply_correction_signal_fields(payload, candidate)
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
detect_provider_overload = _detect_provider_overload
usage_limit_is_recent = _usage_limit_is_recent
salvage_terminal_result = _salvage_terminal_result
worktree_dirty_reason_by_path = _worktree_dirty_reason_by_path
read_unresolved_subagent_spawn = _read_unresolved_subagent_spawn

__all__ = [
    "AUTO_DEV_LABEL_PREFIX",
    "SPAWN_GRACE_SECONDS",
    "TRANSCRIPT_LIVENESS_WINDOW_SECONDS",
    "USAGE_LIMIT_BACKOFF_WINDOW_SECONDS",
    "USAGE_LIMIT_SALVAGE_WINDOW_SECONDS",
    "_CAUSE_IDLE_STALL",
    "_CAUSE_USAGE_LIMIT",
    "_DANGLING_TOOL_USE_REASON",
    "_DETERMINISTIC_PARSE_FAILURES",
    "_DEV_QUEUE_LOAD_ERRORS",
    "_DIRTY_WORKTREE_REASON",
    "_DISPATCH_LOOP_STALE_REASON",
    "_EXTERNAL_COUNTERPARTY_IDLE_REASON",
    "_FINALIZE_BLOCKED_REASON",
    "_FIX_DISPATCH_REF_UNRESOLVED_REASON",
    "_FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON",
    "_FRESHNESS_BLOCK_ESCALATED_REASON",
    "_GENUINELY_TERMINAL_QUEUE_STATUSES",
    "_GH_CHECK_BLOCKED_REASON",
    "_LIVE_STATUSES",
    "_MAIN_CHECKOUT_DRIFT_REASON",
    "_NEEDS_SALVAGE_REASON",
    "_NEVER_CLAIMED_COMPLETION_REASON",
    "_PAUSED_STATUS_KEY",
    "_PHANTOM_REAP_MERGED_REASON",
    "_QUEUE_OPERATION_ENQUEUE",
    "_QUEUE_OPERATION_RECORD_TYPE",
    "_REAP_ELIGIBLE_DISPOSITIONS_BASE",
    "_REAP_PROPOSED_ACTIONS",
    "_RESCUE_PR_BODY_TEMPLATE",
    "_RESCUE_PR_CLOSES_TRAILER_TEMPLATE",
    "_SALVAGE_KIND_GIT_STATE",
    "_SALVAGE_PR_BODY_TEMPLATE",
    "_SALVAGE_PR_TITLE_TEMPLATE",
    "_SALVAGE_SKIP_ESCALATED_REASON",
    "_SALVAGE_SKIP_REASON",
    "_SALVAGE_TERMINAL_STATUSES",
    "_SENTINEL_ADVANCE_REFUSED_KEY",
    "_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY",
    "_SENTINEL_STAGE_MISMATCH_REFUSED_REASON",
    "_SESSION_ID_MISMATCH_ADVISORY_NOTE",
    "_SESSION_UNRESPONSIVE_REASON",
    "_SILENTLY_IDLE_REASON",
    "_STAGE_REVIEW_COMPLETE",
    "_STALLED_CAP_PARKED_REASON",
    "_STOPPED_WITHOUT_SENTINEL_REASON",
    "_SUBAGENT_SPAWNING_TOOL_NAMES",
    "_TERMINAL_NO_RETRY_STATUSES",
    "_TIMED_OUT_MERGED_REASON",
    "_TOOL_USE_COMMAND_SNIPPET_MAX_CHARS",
    "_TRANSIENT_PARSE_FAILURES",
    "_UNCONSUMED_QUEUE_NOTIFICATION_REASON",
    "_UNRESOLVED_SUBAGENT_SPAWN_REASON",
    "_USAGE_LIMITED_MID_TURN_REASON",
    "_VALIDATION_FAILED_MAX_ATTEMPTS",
    "AuditedSentinelRouteOutcome",
    "DanglingToolUseEvidence",
    "ProposedAction",
    "ReapCandidate",
    "ReconcileReport",
    "SentinelRouteOutcome",
    "SessionLivenessForTask",
    "UsageLimitDetection",
    "_TaskLookupResult",
    "_TranscriptRecordIterator",
    "_apply_correction_signal_fields",
    "_apply_queue_mutations",
    "_apply_salvaged_completion",
    "_apply_sentinel_to_task",
    "_apply_sentinel_to_task_audited",
    "_apply_tool_use_block",
    "_backfill_claude_session_ids",
    "_bash_command_snippet",
    "_blocked_result_requeue_enabled",
    "_claude_agents_json",
    "_csid_from_transcript",
    "_detect_dangling_tool_use",
    "_detect_post_review_clean",
    "_detect_provider_overload",
    "_detect_unconsumed_queue_notification",
    "_detect_usage_limit",
    "_effective_transcript_timestamp",
    "_emit_reap_proposed",
    "_foreign_result_target_queue_status",
    "_has_terminal_sentinel",
    "_is_headless",
    "_iter_assistant_records",
    "_iter_notification_records",
    "_iter_transcript_records",
    "_land_blocked_result_failed",
    "_load_dev_queue_or_none",
    "_locate_session_transcript",
    "_log",
    "_looks_like_daemon_outage",
    "_lookup_matching_task",
    "_newest_surface_ref_transcript",
    "_parse_any_sentinel_from_transcript",
    "_parse_sentinel_from_blocks",
    "_project_transcripts_latest_timestamp",
    "_queue_status_for_salvaged",
    "_read_agent_spawn_stamp_context",
    "_read_unresolved_subagent_spawn",
    "_redact_and_truncate",
    "_requeue_blocked_result_under_cap",
    "_resolve_routed_sentinel",
    "_route_blocked_result_to_task",
    "_route_stopped_without_sentinel",
    "_salvage_terminal_result",
    "_sentinel_partial_route_consumed",
    "_session_id_advisory_mismatch",
    "_session_project_dir",
    "_stamp_sentinel_partial_route_consumed",
    "_stamp_session_id_mismatch_advisories",
    "_transcript_age_seconds",
    "_transcript_recently_active",
    "_unresolved_subagent_spawn_age_seconds",
    "_usage_limit_is_recent",
    "_validate_existing_result_for_routing",
    "_verify_salvaged_scope",
    "_widened_transcript_timestamp",
    "_worktree_dirty_reason_by_path",
    "classify_sentinel_stage_position",
    "compute_drift",
    "detect_provider_overload",
    "detect_usage_limit",
    "feature_branch_key",
    "find_live_sessions_for_ticket",
    "find_running_task_for_session",
    "holds_staged_emit_result",
    "read_unresolved_subagent_spawn",
    "resolve_attempt_ceiling",
    "resolve_reap_policy",
    "resolve_session_for_task",
    "resolve_session_liveness_for_task",
    "salvage_terminal_result",
    "session_daemon_liveness",
    "stage_refusal_latched",
    "stamp_stage_refusal",
    "ticket_id_for_session",
    "usage_limit_is_recent",
    "worktree_dirty_reason_by_path",
]
