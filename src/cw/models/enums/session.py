"""Session purposes, statuses, completion and liveness enums (#3739)."""

from __future__ import annotations

from enum import StrEnum


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"
    ORCHESTRATE = "orchestrate"
    # #2017 -- spawned via the fix_agent recipe (src/cw/reconcile/review_recipes/
    # fix_agent.py), direct from the REVIEW stage's fix loop. Never a `cw
    # start`/`--purpose` choice (see WORKER_PURPOSES exclusion below), same
    # carve-out shape as ORCHESTRATE.
    FIX = "fix"
    # #3054 -- stage-neutral codex sessions record their stage's purpose. Like
    # FIX and ORCHESTRATE, never a `cw start --purpose` choice (WORKER_PURPOSES
    # exclusion below). Declaration order is pinned by the schema golden.
    PLAN = "plan"
    REVIEW = "review"
    FINALIZE = "finalize"


# Purposes a worker session can be dispatched/created with. ORCHESTRATE is
# excluded: an ORCHESTRATE session is created only via `cw orchestrate start`
# (#595 / Phase 4b), never selected as a worker --purpose. FIX is excluded for
# the same reason (#2017): it is dispatched only by the fix_agent recipe.
# PLAN, REVIEW and FINALIZE (#3054) are excluded likewise: codex sessions only.
WORKER_PURPOSES: tuple[SessionPurpose, ...] = (
    SessionPurpose.IMPL,
    SessionPurpose.IDEA,
    SessionPurpose.DEBT,
)


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"
    # Headless daemon session exceeded wall-clock budget without emitting a
    # sentinel. Terminal-ish but retry-eligible: reconciler reverts the
    # owning TicketTask to PENDING so the dispatch loop can retry.
    # See GitHub issue #176 Layer 1.
    TIMED_OUT = "timed_out"


# Statuses a Session never leaves once reached. Single source of truth for
# "is this session done" checks scattered across spawn.py, reconcile, cli,
# and doctor (GitHub #1674 — was independently duplicated as an inline tuple
# or a locally-defined frozenset in several places before this constant
# existed; consolidated at four of those call sites here, alongside
# SessionStatus itself, following the WORKER_PURPOSES derived-constant
# precedent above — reconcile/salvage.py:231 carries one remaining
# unconsolidated copy, tracked separately, not folded into this ticket).
TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.TIMED_OUT}
)


class CompletionReason(StrEnum):
    USER = "user"
    HANDOFF = "handoff"
    CRASHED = "crashed"
    NORMAL = "normal"
    TIMED_OUT = "timed_out"
    # GitHub #2324 — the worker's turn ended on a usage-limit message. Not a
    # crash: the session did its work up to the limit.
    USAGE_LIMITED = "usage_limited"


class SessionOrigin(StrEnum):
    USER = "user"
    DAEMON = "daemon"


class LastResultSource(StrEnum):
    """RFC 0012 S2 — provenance of ``Session.last_result``.

    Records which mechanism wrote the session's terminal sentinel, so the
    ``emit_result_locked`` door (``cw.result``) can arbitrate first-writer-
    wins and log a collision naming both sources. ``None`` on ``Session``
    means pre-migration (no writer has stamped a source yet). See #1456.
    """

    EMIT_CLI = "emit_cli"
    STOP_HOOK_HARVEST = "stop_hook_harvest"
    EXECUTOR_DIRECT = "executor_direct"
    GIT_SYNTHESIS = "git_synthesis"
    SALVAGE_TRANSCRIPT = "salvage_transcript"


class LivenessBucket(StrEnum):
    """RFC 0008 W2 staleness bucket, latched onto ``Session``.

    The bucketed age is transcript-mtime age for a surface-backed session, or
    time since ``started_at`` for an unobservable one with no transcript at all
    (#2417); ``session.liveness_changed`` names which via ``staleness_source``.

    Closed 4-value set: per-stage overrides
    (``OrchestratorConfig.liveness_first_bucket_by_stage``) move the
    entry-point threshold for a stage, but never rename or add labels.
    Downstream consumers of ``session.liveness_changed`` match these
    literals directly without reading config. See GitHub #1001.
    """

    LIVE = "live"
    STALE_15M = "stale_15m"
    STALE_30M = "stale_30m"
    STALE_45M = "stale_45m"
