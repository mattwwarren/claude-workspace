"""The WAIT / PEEK / STOP recommendation ladder for ``cw queue peek``.

Pure scoring over a row's age, idle gap, PR state, sentinel status and
attempt counters, with the thresholds it uses. No I/O.
"""

from __future__ import annotations

from cw.models import Stage

# Thresholds — absolute (tier-agnostic). Purely advisory: since the
# process-kill-timeout removal nothing in cw enforces a wall-clock ceiling,
# so these ages recommend operator attention (peek/stop is the OPERATOR's
# call), they no longer anticipate an automatic kill.
WAIT_AGE_MIN: int = 30  # below this, almost always healthy
PEEK_AGE_MIN: int = 45  # above this, peek even if active
STOP_AGE_MIN: int = 55  # long-running — worth an operator look/decision
STOP_CEILING_MIN: int = 60  # ceiling named in the STOP-by-age message; the
# trigger (STOP_AGE_MIN) fires before this so the operator has lead time —
# age_min >= this is "exceeded," below it is "approaches" (#1795)
IDLE_PEEK_MIN: int = 7  # idle this long with no PR → check for stall
IDLE_STALL_MIN: int = 15  # idle this long → likely stuck
IDLE_POST_PR_MIN: int = 5  # idle this long after PR shipped → stuck in stage5
STOP_UNPRODUCTIVE_ATTEMPTS_MIN: int = 3  # at or above this unproductive-attempt
# count → systemic failure (mirrors the admission gate's signal, GitHub
# #1750/#1768)

# idle_min at or below this = "demonstrably writing" (#2044) — any
# STOP-flavored verdict is downgraded to PEEK regardless of which arm
# proposed it, once, at the aggregation point, instead of each arm
# re-deriving its own liveness check.
#
# Grounded in the 2026-08-26/27 sprint's observed separation:
# false-positive STOP verdicts on live sessions fired at
# idle_min 0.0, 0.1, 0.3, 0.5 (#2017 sessions 8682e4d7, bf947d61,
# 7c159163); the sprint's one true-positive STOP fired at idle_min 31.5
# (#2029 session d505b5ef, all review subagents terminal). 2.0 sits
# inside that 0.5-to-31.5 gap with no observed values in it, so the
# exact value is not delicate — anything from ~1 to ~25 would have
# classified all of these identically.
IDLE_LIVE_MAX_MIN: float = 2.0

_STAGE_ORDER: tuple[Stage, ...] = (
    Stage.HARDEN,
    Stage.PLAN,
    Stage.IMPL,
    Stage.REVIEW,
    Stage.FINALIZE,
)


def _reached_deep_stage(high_water: Stage | None) -> bool:
    if high_water is None:
        # unknown = no signal; does NOT suppress STOP (per R2)
        return False
    return _STAGE_ORDER.index(high_water) >= _STAGE_ORDER.index(Stage.REVIEW)


def _stage5_rec(idle_min: float | None) -> tuple[str, str]:
    """Return recommendation for a shipped+PR-open (stage-5 wait) session."""
    if idle_min is not None and idle_min > IDLE_STALL_MIN:
        reason = f"shipped + PR open but idle {idle_min:.0f}min — CI may be hung"
        return ("PEEK", reason)
    return ("WAIT", "shipped + PR open — auto-merge CI in progress")


def _stall_check(
    age_min: float,
    idle_min: float | None,
    pr_state: str | None,
) -> tuple[str, str]:
    """Return recommendation based on stall/age thresholds.

    Handles the generic age/idle ladder when no PR-merge or stage-5 special
    case applies.
    """
    if idle_min is not None and idle_min > IDLE_STALL_MIN and not pr_state:
        return (
            "STOP-OR-PEEK",
            f"idle {idle_min:.0f}min, no PR — likely stuck; peek before stop",
        )
    if idle_min is not None and idle_min > IDLE_PEEK_MIN and not pr_state:
        return ("PEEK", f"idle {idle_min:.0f}min, no PR — check for tool denial")
    if age_min > PEEK_AGE_MIN:
        return ("PEEK", f"age {age_min:.0f}min mature — check stage")
    if age_min > WAIT_AGE_MIN:
        return ("WAIT", f"age {age_min:.0f}min in normal range")
    return ("WAIT", f"age {age_min:.0f}min — early/healthy")


def _score_session(
    age_min: float,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    unproductive_attempts: int,
    stage_high_water: Stage | None = None,
    usage_limit_detected: bool = False,
    child_active: bool = False,
) -> tuple[str, str]:
    """Return recommendation when age_min is known."""
    if pr_state == "MERGED" and idle_min is not None and idle_min > IDLE_POST_PR_MIN:
        return (
            "STOP",
            f"PR merged + worker idle {idle_min:.0f}min — stuck in post-create wait",
        )
    if sentinel_status == "shipped" and pr_state == "OPEN":
        return _stage5_rec(idle_min)
    if age_min > STOP_AGE_MIN:
        if age_min >= STOP_CEILING_MIN:
            reason = (
                f"age {age_min:.0f}min exceeded {STOP_CEILING_MIN}-min "
                "timeout — stop or hand off"
            )
        else:
            reason = (
                f"age {age_min:.0f}min approaches {STOP_CEILING_MIN}-min "
                "timeout — stop or hand off"
            )
        return ("STOP", reason)
    if (
        unproductive_attempts >= STOP_UNPRODUCTIVE_ATTEMPTS_MIN
        and not _reached_deep_stage(stage_high_water)
        and not child_active
    ):
        if usage_limit_detected:
            return (
                "PEEK",
                f"unproductive attempt {unproductive_attempts} but usage-limit "
                "outage detected in transcript — verify before stopping",
            )
        return (
            "STOP",
            f"unproductive attempt {unproductive_attempts} — systemic, not transient",
        )
    return _stall_check(age_min, idle_min, pr_state)


def _gate_stop_on_liveness(
    rec: str, reason: str, idle_min: float | None
) -> tuple[str, str]:
    """Downgrade any STOP-flavored verdict to PEEK when idle_min shows the
    session is demonstrably writing (#2044).

    Applied once, after every _score_session arm has had its say, instead
    of as a per-arm condition each arm must remember to add — #2028/PR
    #2038 gated only the unproductive_attempts arm on child_active, and
    the raw age arm reproduced the identical false-STOP 46 minutes later
    because it never consulted idle_min at all (#2044 comment 1). One
    gate at the verdict level closes every arm, present and future.

    idle_min is None (no signal) does NOT suppress STOP — mirrors
    _reached_deep_stage's "unknown = no signal" convention.
    """
    if not rec.startswith("STOP"):
        return rec, reason
    if idle_min is None or idle_min > IDLE_LIVE_MAX_MIN:
        return rec, reason
    return (
        "PEEK",
        f"{reason}; idle {idle_min:.1f}m indicates active writing — "
        "verify before stopping",
    )


def recommend(
    age_min: float | None,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    unproductive_attempts: int,
    stage_high_water: Stage | None = None,
    usage_limit_detected: bool = False,
    child_active: bool = False,
) -> tuple[str, str]:
    """Return (recommendation, reasoning) from the peek-stop ladder.

    ``unproductive_attempts`` — not raw ``attempts`` — drives the
    STOP-by-attempt-count branch, mirroring the dispatch admission gate's
    #1750 signal (GitHub #1768). ``child_active`` (#2028) suppresses that
    same branch — an actively-writing subagent means the attempt counter is
    describing the session's past, not its present, so it must not
    independently drive a STOP while a child is demonstrably working; the
    row falls through to ``_stall_check``, which already reads the
    child-rescued ``idle_min``.

    Every arm's return value is finally routed through
    ``_gate_stop_on_liveness`` (#2044) — a single verdict-level check that
    downgrades any STOP to PEEK when ``idle_min`` shows the session is
    demonstrably writing, regardless of which arm proposed the STOP. A
    future arm that returns a new STOP needs no additional liveness
    plumbing of its own — it is covered by this gate automatically.
    """
    if age_min is None:
        return ("PEEK", "no transcript timestamps — verify session is alive")
    rec, reason = _score_session(
        age_min=age_min,
        idle_min=idle_min,
        pr_state=pr_state,
        sentinel_status=sentinel_status,
        unproductive_attempts=unproductive_attempts,
        stage_high_water=stage_high_water,
        usage_limit_detected=usage_limit_detected,
        child_active=child_active,
    )
    return _gate_stop_on_liveness(rec, reason, idle_min)
