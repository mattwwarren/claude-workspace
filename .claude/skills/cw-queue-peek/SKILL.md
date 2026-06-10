---
name: cw-queue-peek
description: In-flight inspection of RUNNING cw dev-queue sessions — for each, computes age, idle gap, last sentinel status, and PR state, and recommends WAIT / PEEK / STOP via a peek-stop ladder so the operator can decide whether to keep a session alive or close it via `cw spawn close`. Use when watching a parallel dispatch wave, when a session is running long, or when you suspect a session is stuck (post-PR-merge wait, tool denial, retry loop). Triggers on "peek the queue", "what's running", "check long-running sessions", "is anything stuck", "should I stop session X".
model: haiku
---

# cw Queue Peek

Reports-only inspection of in-flight dev-queue sessions. Surfaces stalls and
stuck sessions before they hit the 60-min hard timeout, so the operator can
stop wasteful work early and re-dispatch productively.

This skill is the **in-flight** counterpart to the orchestrator event bus
(`cw event tail --type session.needs_attention --type session.timed_out`,
which surfaces a session's exit/attention state after it ends) and
`cw-validate-result` (which inspects what a finished session produced). Use
`cw-queue-peek` *during* a wave to decide what to do with long-running
sessions.

## When to use

- Watching a parallel dispatch wave where one or more tickets are running long
- A session is past its expected wall-clock and you want to know if it's still progressing
- A PR was merged but the worker session is still alive (the canonical "stuck post-create" pattern)
- Periodic check during operator orchestration of a queue ("anything need attention?")
- Right before considering a `cw spawn close` — verify the peek confirms it's stuck

Do **not** use this skill for:

- Post-mortem on a session that already ended — tail the event bus
  (`cw event tail --type session.needs_attention --type session.timed_out`)
  for its exit/attention state, or use `cw-validate-result` for the sentinel
  content
- Deciding what to do with the sentinel result — use `cw-followup`
- Interactive (USER-origin) sessions started via `cw start` — they don't go through dev-queue

## Required tool

The skill wraps `.claude/scripts/cw_queue_peek.py` in the claude-workspace
repo. The script reads `~/.local/share/cw/dev_queue.json`, locates each
RUNNING task's claude transcript jsonl, parses last-emitted sentinel + last
activity timestamps, and calls `gh pr view` to resolve PR state.

The script never stops sessions itself — it only reports. The operator runs
`cw spawn close <session_id>` after reviewing the report.

## Execution

### Default — one-shot peek for a client

```bash
python3 .claude/scripts/cw_queue_peek.py --client claude-workspace
```

Output is a table with one row per RUNNING task. Columns:

| Column | Meaning |
|---|---|
| `ticket` | Ticket ID |
| `session` | First 12 chars of the cw session ID |
| `att` | Attempt counter (≥2 means dispatcher retried) |
| `age_m` | Minutes since the worker's first message in the transcript |
| `idle_m` | Minutes since the worker's last assistant message |
| `stage` | `stage_reached` from the last emitted sentinel |
| `status` | `status` from the last emitted sentinel |
| `pr` | PR number if one was opened |
| `pr_state` | OPEN / MERGED / CLOSED / UNKNOWN (via `gh pr view`) |
| `recommend` | WAIT / PEEK / STOP / STOP-OR-PEEK |

Rows recommended non-WAIT print a reason on the next line. A "Suggested
stops" footer prints the exact `cw spawn close` invocation for STOP rows.

### JSON output

```bash
python3 .claude/scripts/cw_queue_peek.py --client claude-workspace --json
```

For orchestrator subagents or wave-monitor automation that needs to act on
the recommendation programmatically.

### All clients

Omit `--client` to peek across all clients. Useful for cross-project wave
monitoring.

```bash
python3 .claude/scripts/cw_queue_peek.py
```

## Peek-stop ladder

The script's recommendation is computed from this ladder. Higher rules win:

1. **Stuck post-PR-merge**: `pr_state == MERGED` and `idle_min > 5` → **STOP**.
   The canonical "worker forgot to exit" pattern. PR has shipped; further
   compute is wasted.
2. **Healthy Stage-5 wait**: `status == shipped` and `pr_state == OPEN`:
   - `idle_min ≤ 15` → **WAIT** (auto-merge waiting for CI)
   - `idle_min > 15` → **PEEK** (CI may be hung; check `gh pr checks <n>`)
3. **Approaching timeout**: `age_min > 55` → **STOP** (60-min hard ceiling
   imminent — close cleanly or accept the auto-timeout).
4. **Retry loop**: `attempts ≥ 3` → **STOP** (systemic, not transient).
5. **Long stall without PR**: `idle_min > 15` and no PR → **STOP-OR-PEEK**
   (manually inspect before stopping — could be tool denial or model-side
   hang).
6. **Moderate stall without PR**: `idle_min > 7` and no PR → **PEEK** (check
   for tool denial or rate-limit).
7. **Mature session**: `age_min > 45` → **PEEK** (verify still progressing).
8. **Normal range**: `age_min > 30` → **WAIT** (in expected range).
9. **Early**: anything else → **WAIT** (early/healthy).

## Cost framing

- Peek is free (~30s operator time, no API calls beyond `gh pr view`).
- Per-session worker burn: roughly $0.05-0.15/min when actively producing.
- 30-min wasted ≈ $2-5; 60-min wasted ≈ $4-10.
- False-positive STOP cost: lose WIP, re-dispatch ≈ $2-5 + 30 min.

Bias: peek aggressively (free), stop conservatively (real cost on false
positives). Only stop when the peek confirms the session can't produce
useful work.

## Acting on recommendations

The script never executes stops. After reading the report:

```bash
# Stop the session
cw spawn close <session_id>

# Remove the queue task if you don't want a retry
cw dev-queue remove <ticket_id> -c <client>

# OR leave the queue task so a future tick can re-dispatch
```

Always `cw spawn close` **before** `cw dev-queue remove` — see #317 for why
(removing first races against the dispatcher loop, which can spawn att+1
between the remove and the close).

## What this skill cannot diagnose

- Whether the worker is *correctly* on the right path (only sentinel + PR
  state visible — not "should this be a no_op")
- Whether code being produced is high quality (post-mortem via review)
- API rate-limit state (no visibility into model-side throttling)
- CI failure reasons (the script reports PR_state OPEN/MERGED/CLOSED; deeper
  CI inspection requires `gh pr checks`)

## Related skills

- `cw event tail --type session.needs_attention --type session.timed_out` —
  durable exit/attention state for finished or stalled sessions. The bus is
  emitted automatically by reconcile (idle/timeout
  watchdog), so it supersedes a dedicated session-watch skill.
- `cw-validate-result` — post-mortem sentinel + PR inspection
- `cw-followup` — react to a finished session's sentinel result
- `cw-smoke-test` — end-to-end dogfood validation
