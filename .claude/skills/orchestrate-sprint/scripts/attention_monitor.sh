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
  --json \
| python3 -u -c '
import sys, json

# Only these paused_status values put blocker.reason verbatim into
# breadcrumbs (routing.py Rule 5, GitHub #1511); breadcrumbs is polymorphic
# for every other paused_status producer (worktree paths, fixed
# reason-strings, full sentences), so this is a gated allowlist, not a
# blanket breadcrumbs fallback.
_BLOCKER_REASON_PAUSED_STATUSES = {
    "blocked", "awaiting_operator_availability", "merge_gate_blocked",
}

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
    why = p.get("paused_status") or p.get("reason") or p.get("proposed_action") or ""
    bits = []
    if p.get("stage"):
        bits.append("stage=" + str(p.get("stage")))
    if p.get("attempts") is not None:
        bits.append("att=" + str(p.get("attempts")))
    if p.get("paused_status") in _BLOCKER_REASON_PAUSED_STATUSES and p.get("breadcrumbs"):
        bits.append("reason=" + str(p.get("breadcrumbs")))
    joined = " ".join(bits)
    sess = str(p.get("session_id") or "")[:8]
    print("ATTENTION | %s | %s | %s | %s | %s" % (t, tk, why, joined, sess), flush=True)
'
