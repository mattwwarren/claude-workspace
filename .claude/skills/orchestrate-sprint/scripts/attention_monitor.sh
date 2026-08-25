#!/usr/bin/env bash
# attention_monitor.sh — stream the cw event bus for sessions that need a human.
#
# Intended to be run as the command of a persistent Monitor (one stdout line =
# one notification to the orchestrator). It follows the orchestrator event bus
# for a single client, filtered to the attention-worthy terminal types and
# deduped so a parked/re-firing session emits once, not on every reconcile tick.
#
# Why these types: needs_attention (operator decision needed), operator.escalation
# (escalation-timer fallback for un-actioned parks), timed_out (hit the wall),
# reap_proposed (wedge / dead session holding a lane), and the two stage/phantom
# signals. dispatch.tick and ticket.needs_sync are excluded as noise — at steady
# state they dominate the bus and tell you nothing actionable.
#
# session.liveness_changed is the only *positive*-style signal in the list —
# a stall, not a named failure. It is filtered (below) to stale_30m/stale_45m
# only: live/stale_15m transitions are expected noise during a legitimate
# quiet stretch (#1795's ~20m review-stage guidance), and showing every flap
# there would teach operators to skip this channel, reproducing the exact
# blindness this subscription exists to close (#2004).
#
# Starting from "now" (--since) avoids replaying the (large) historical backlog
# of needs_attention events on arm. With --follow this then waits for new ones.
#
# Note: the embedded Python uses %-formatting and no nested quotes on purpose —
# it lives inside a single-quoted bash -c, where f-strings with escaped quotes
# (f"{\" \".join(x)}") break across shells/Python versions. Keep it boring.
#
# Usage:  attention_monitor.sh [CLIENT] [LANE]   (default client: claude-workspace)
#   LANE, if set, scopes the event stream to that lane (--lane) — use in a
#   parallel-orchestrator setup so two orchestrators on the same client don't
#   cross-deliver each other's attention events.
# Arm via the Monitor tool with persistent: true.

set -euo pipefail
CLIENT="${1:-claude-workspace}"
LANE="${2:-}"
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

LANE_ARGS=()
if [[ -n "$LANE" ]]; then
  LANE_ARGS=(--lane "$LANE")
fi

# Why: on bash < 4.4 (macOS system /bin/bash is 3.2), "${LANE_ARGS[@]}" on an
# EMPTY array raises "unbound variable" under set -u. The [@]+ alternate-value
# guard below expands to nothing when unset/empty and to the full args when
# populated — safe on 3.2, identical behavior when LANE is set (#1413).
cw event tail --follow --client "$CLIENT" --dedup-terminal \
  --since "$SINCE" \
  "${LANE_ARGS[@]+"${LANE_ARGS[@]}"}" \
  --type session.needs_attention \
  --type operator.escalation \
  --type session.timed_out \
  --type session.reap_proposed \
  --type session.stage_timed_out_retried \
  --type session.phantom_reverted \
  --type session.liveness_changed \
  --json \
| python3 -u -c '
import sys, json

# Only these paused_status values put blocker.reason verbatim into
# breadcrumbs -- see BREADCRUMB_ELIGIBLE_PAUSED_STATUSES,
# src/cw/dispatch/routing.py (GitHub #1511, #1597, #1729); breadcrumbs
# is polymorphic for every other paused_status producer (worktree paths,
# fixed reason-strings, full sentences), so this is a gated allowlist, not a
# blanket breadcrumbs fallback.
_BLOCKER_REASON_PAUSED_STATUSES = {
    "blocked", "awaiting_operator_availability", "merge_gate_blocked",
    "codex_must_fix_mechanically_rejected", "empty_diff_blocked",
    # #1862: STAGE_FAILURE_STATUSES member, so breadcrumb-eligible by
    # derivation. Its gate-side twin ("stale_dispatch_gate") is deliberately
    # NOT here -- no session ran, so that park hardcodes breadcrumbs="".
    "stale_dispatch",
}

# finalize_regress_repeat (GitHub #1717, src/cw/dispatch/regress_repeat.py) is
# a companion signal, not a blocker.reason-carrying park -- its breadcrumbs is
# a composite diagnostic string ("attempts=... branch_head=... pr_url=...
# disposition=..."), not a verbatim blocker.reason. Surfaced the same way
# (always shown when present) via a separate check below rather than folding
# it into _BLOCKER_REASON_PAUSED_STATUSES, to keep the docstring above
# ("blocker.reason verbatim") accurate.
_FINALIZE_REGRESS_REPEAT_PAUSED_STATUS = "finalize_regress_repeat"

# Only these LivenessBucket values page the operator (#2004). "live" and
# "stale_15m" are not actionable -- a session legitimately goes quiet
# during a long reviewer fan-out (the ~20m review-stage guidance in #1795),
# and showing every flap there teaches operators to skip this channel,
# reproducing the exact blindness this subscription exists to close. Per-
# stage entry thresholds (OrchestratorConfig.liveness_first_bucket_by_stage)
# mean "stale_30m" is not a fixed wall-clock duration across stages --
# render stale_minutes, never imply a duration from the bucket name alone.
_SURFACED_LIVENESS_BUCKETS = {"stale_30m", "stale_45m"}

# Process-lifetime dedup for session.liveness_changed lines only --
# --dedup-terminal (the _TERMINAL_EVENT_TYPES set in cli/queues.py) does not
# cover this type, and its (type, session_id, paused_status, renotify_marker)
# key would be wrong for it: paused_status/renotify_marker are always
# absent on a liveness payload, so extending that set would collapse
# every liveness line after the first for a session -- including a real
# 30m->45m escalation. Track the last *surfaced* bucket per session here
# instead; resets when the monitor process is rearmed, same as the
# seen_terminal set in _follow_loop (src/cw/cli/queues.py).
_last_surfaced_bucket = {}

for ln in sys.stdin:
    ln = ln.strip()
    if not ln:
        continue
    try:
        e = json.loads(ln)
    except Exception:
        continue
    p = e.get("payload", {}) or {}
    t = e.get("type", "?")
    tk = p.get("ticket_id") or "?"
    if t == "session.liveness_changed":
        nb = p.get("new_bucket")
        if nb not in _SURFACED_LIVENESS_BUCKETS:
            # A recovery ("live"/"stale_15m") ends the stall this session
            # was in. Clear its latch so the *next* surfaced bucket is
            # treated as a new stall, not a duplicate of the one that
            # just resolved -- otherwise a genuine re-stall into the same
            # bucket after a recovery is silently swallowed.
            _last_surfaced_bucket.pop(str(p.get("session_id") or ""), None)
            continue
        sid_key = str(p.get("session_id") or "")
        if _last_surfaced_bucket.get(sid_key) == nb:
            continue
        _last_surfaced_bucket[sid_key] = nb
    why = p.get("paused_status") or p.get("reason") or p.get("proposed_action") or p.get("new_bucket") or ""
    bits = []
    if p.get("stage"):
        bits.append("stage=" + str(p.get("stage")))
    if p.get("attempts") is not None:
        bits.append("att=" + str(p.get("attempts")))
    if p.get("stale_minutes") is not None:
        bits.append("stale_m=%.1f" % float(p.get("stale_minutes")))
    if (
        p.get("paused_status") in _BLOCKER_REASON_PAUSED_STATUSES
        or p.get("paused_status") == _FINALIZE_REGRESS_REPEAT_PAUSED_STATUS
    ) and p.get("breadcrumbs"):
        bits.append("reason=" + str(p.get("breadcrumbs")))
    joined = " ".join(bits)
    sess = str(p.get("session_id") or "")[:8]
    print("ATTENTION | %s | %s | %s | %s | %s" % (t, tk, why, joined, sess), flush=True)
'
