#!/usr/bin/env bash
# Cron entrypoint for /review-monitor.
#
# - Gates on Mon-Fri, 8a through the 8pm hour, local (machine TZ).
#   The 8pm hour is included so a late-day push from a coworker still gets a
#   delta-review + approval before sign-off, rather than waiting overnight.
# - Cheap precheck before spending tokens: pending registrations + gh search
#   for any PR updated since the last successful fire. Skips the claude
#   invocation when there's no signal.
# - On non-zero claude exit, enqueues a cron_failure action to the Desktop
#   action queue with the tail of the log (rate-limited to one entry per hour
#   to avoid pile-up if the failure persists). The cron itself sends nothing
#   externally — the Claude Desktop schedule drains the queue.

set -uo pipefail

# Template: edit REPO and WORKDIR to point at the repo this cron monitors.
REPO=owner/repo
WORKDIR=~/workspace/owner/repo
LOG=~/.cache/review-monitor.log
ERR_MARKER=/tmp/review-monitor-last-error-enqueue
LAST_FIRE=~/.claude/.review-monitor-last-fire
ERROR_ENQUEUE_COOLDOWN=3600

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') cron: $*" >> "$LOG"; }

H=$(date +%H)
D=$(date +%u)
if [[ $D -gt 5 || 10#$H -lt 8 || 10#$H -gt 20 ]]; then
  exit 0
fi

cd "$WORKDIR" || { log "cd failed"; exit 0; }

# --- Precheck: skip the LLM session when nothing's likely actionable ---

PENDING_COUNT=$(find /tmp/review-monitor/pending/ -name '*.json' 2>/dev/null | wc -l | tr -d ' ')

if [[ -f "$LAST_FIRE" ]]; then
  LAST_TS=$(date -u -d "@$(stat -c %Y "$LAST_FIRE")" '+%Y-%m-%dT%H:%M:%SZ')
else
  # First run after install — look back 24h to catch anything missed.
  LAST_TS=$(date -u -d '1 day ago' '+%Y-%m-%dT%H:%M:%SZ')
fi

GH_OUT=$(gh pr list --repo "$REPO" --search "updated:>=$LAST_TS" --state all --json number --limit 100 2>&1)
GH_EC=$?
if (( GH_EC != 0 )); then
  log "gh precheck failed (exit $GH_EC); firing to be safe"
  UPDATED_COUNT=999
else
  UPDATED_COUNT=$(echo "$GH_OUT" | jq 'length' 2>/dev/null || echo 999)
fi

if (( PENDING_COUNT == 0 && UPDATED_COUNT == 0 )); then
  log "precheck: no work (pending=0, updated=0 since $LAST_TS); skipping claude"
  exit 0
fi

log "precheck: firing (pending=$PENDING_COUNT, updated=$UPDATED_COUNT since $LAST_TS)"

# --- Invoke claude ---

claude --dangerously-skip-permissions \
  -p "Run /review-monitor. Execute the full poll cycle. Leave any code changes uncommitted (auto-fix agents push their own branches)." \
  --allowedTools Bash,Read,Write,Edit,Glob,Grep,Task,ToolSearch \
  --model sonnet \
  --max-budget-usd 15 \
  >> "$LOG" 2>&1
EC=$?

if [[ $EC -eq 0 ]]; then
  touch "$LAST_FIRE"
  exit 0
fi

# --- Enqueue a cron_failure action on failure (rate-limited) ---
# The cron does not message anyone directly; it drops a queue entry the Claude
# Desktop schedule picks up and routes.

NOW=$(date +%s)
LAST_ERR=$(stat -c %Y "$ERR_MARKER" 2>/dev/null || echo 0)
if (( NOW - LAST_ERR < ERROR_ENQUEUE_COOLDOWN )); then
  exit "$EC"
fi

TAIL=$(tail -n 40 "$LOG" 2>/dev/null | tail -c 1800)
PAYLOAD=$(jq -n --argjson ec "$EC" --arg log "$LOG" --arg tail "$TAIL" \
  '{exit_code: $ec, log_path: $log, log_tail: $tail}')
if ~/.claude/scripts/review_monitor.py enqueue-action \
  --type cron_failure \
  --payload "$PAYLOAD" >> "$LOG" 2>&1; then
  touch "$ERR_MARKER"
else
  log "enqueue-action failed (exit $?) — cron_failure not recorded"
fi

exit "$EC"
