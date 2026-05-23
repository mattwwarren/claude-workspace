---
name: cw-smoke-test
description: Dogfood the /auto-dev --headless pipeline end-to-end against a single ticket via cw — runs pre-flight checks, enqueues + dispatches one tick, monitors until completion, validates the sentinel, and reports PASS/FAIL with a recommended follow-up. Use when the user wants to smoke-test the auto-dev pipeline, validate the headless contract end-to-end, dogfood a fresh tech-debt ticket, run a one-ticket pipeline sanity check, or "run /auto-dev on <id> via cw and tell me what happened".
---

# cw-smoke-test

End-to-end dogfood loop for the `/auto-dev --headless` pipeline against a single ticket. Composes the existing skills — pre-flight + dispatch + monitor + `/cw-validate-result` + `/cw-followup` suggestion — into one orchestrated call.

This is pure orchestration. No new parsing or validation logic; the heavy lifting belongs to the sibling skills.

## When to use

- The user wants to validate that a producer-side change (e.g. a parser enum fix, a new agent prompt) still works end-to-end before broadcasting it.
- A freshly opened tech-debt ticket needs a one-shot smoke test before a wider meta-test spray.
- The user asks "did `/auto-dev` regress?" — the smoke test answers it with a real run.

## Inputs

One required argument:
- ticket id (e.g. `171` or `#171`) — a GitHub issue in `mattwwarren/claude-workspace` (or `--repo OWNER/NAME` to target another).

Optional flags:
- `--client <NAME>` — cw client name for the dev-queue lookup (default `claude-workspace`).
- `--skip-preflight` — run pre-flight in advisory mode (still report; do not abort). Useful when the operator already knows about an issue (e.g. cw doctor reports stale linkage drift).

## How it works

### Step 1 — pre-flight

Run the bundled checker:

```bash
uv run --project /home/matthew/workspace/personal/claude-workspace python \
  .claude/skills/cw-smoke-test/scripts/preflight.py \
  --ticket-id <NUMBER>
```

The script emits one JSON object with `ok` (bool) and `checks` (list). Each row carries `name`, `passed`, `severity` (`hard` | `soft`), and `detail`.

Hard checks (must pass):
- `agents_present` — `plan-reviewer.md` and `plan-soundness-reviewer.md` exist under `~/.claude/agents/` or repo-local `.claude/agents/`.
- `cw_backend_healthy` — `cw doctor` reports backend + config + state file all OK.
- `ticket_open` — `gh issue view <id>` returns `state=OPEN`.
- `no_open_pr_for_ticket` — no open PR in the repo whose title references the ticket number.
- `not_already_queued` — the ticket is not RUNNING / PENDING / CLAIMED in `cw dev-queue status`.

Soft checks (warn but proceed):
- `cw_doctor_clean` — `cw doctor` reports zero issues. Linkage drift and stale-session warnings are common on a working system; surface them but do not abort.

If `ok` is false (any hard check failed), print the failing rows and stop. Do not dispatch. Unless the user passes `--skip-preflight`, in which case continue with a clear warning banner that includes the failed checks.

### Step 2 — dispatch

Enqueue and run a single tick:

```bash
cw dev-queue add <TICKET> -c <CLIENT>
cw dev-queue run --once
```

Record the wall-clock timestamp before `dev-queue run --once` returns — that's the cursor for Step 3.

### Step 3 — monitor

The dispatch tick spawned a worker session. Tail the event bus for the lifecycle markers:

```bash
cw event tail --since <DISPATCH_TS> --type session.spawned --type session.completed --json
```

Resolve the spawned worker's cw session id from the first `session.spawned` event whose payload references the ticket. Then poll:

```bash
cw event tail --since <DISPATCH_TS> --type session.completed --json | \
  jq -r 'select(.payload.session_id == "<WORKER_ID>")'
```

Until that returns a row, or a reasonable timeout (default 30 minutes — match the Layer 1 backstop in `signal-stop`).

While waiting, surface progress sparingly. Print one line on `session.spawned` (with the worker id + worktree path), then go silent until completion. The user can `cw event tail` themselves if they want intermediate noise.

If completion never arrives within the timeout, surface the worker id + `cw status` snapshot and stop. Do not auto-retry — that's the user's call.

### Step 4 — validate

Once the worker completes, hand off to `/cw-validate-result` for the forensic read:

```bash
/cw-validate-result --session-id <WORKER_SHORT_ID>
```

That skill resolves the transcript, parses the sentinel via `cw-followup/scripts/parse_sentinel.py`, walks the headless-contract checklist, and prints one of four outcomes:

| Outcome | Smoke-test verdict |
|---|---|
| `valid` | PASS — the pipeline produced a usable result. Print the `effective_status` and the PR / branch / worktree as applicable. |
| `producer_status_unknown` | PASS-WITH-WARNING — the run finished and emitted a structured payload, but the producer used a status not yet in the parser's enum. File against `#190` / `#191` patterns if this is new. |
| `invalid_sentinel` | FAIL — schema validation failed. This is producer/consumer drift; surface the validation error verbatim and recommend filing a parser bug. |
| `no_sentinel` | FAIL — the run exited without emitting a sentinel at all. Walk the user through `references/no-sentinel-patterns.md` (in the validator skill's references). |

### Step 5 — suggest follow-up

On PASS / PASS-WITH-WARNING, suggest the appropriate `/cw-followup` invocation:

```text
smoke-test: #<TICKET> → <effective_status>; ready to <suggested action>
  /cw-followup --session-id <WORKER_SHORT_ID>
```

On FAIL, do NOT suggest `/cw-followup` — surface the failure and let the user decide whether to file a producer or parser ticket. If the failure looks like a known drift case (e.g. status not yet in enum, plan_source not yet in enum), point at the relevant open ticket (`#190`, `#191`) so the user can subscribe rather than re-file.

## Output shape

Print exactly one summary line at the end, caveman-tight:

```
smoke-test: #171 → valid (status=shipped, PR #234) — ready for review
smoke-test: #185 → producer_status_unknown (status=premises_pending_verification) — disposition via /cw-followup
smoke-test: #999 → no_sentinel — worker session 4f44d145 exited without emitting; transcript at <path>
```

## Failure modes

- **Pre-flight refuses, user wants override** — `--skip-preflight` advisory mode. Print a one-line warning banner before dispatching so the user sees the smoke test happened in degraded mode.
- **Worker spawned but `session.completed` never arrives** — surface worker id + last seen transcript timestamp; stop after the 30-minute window. The Layer 1 backstop should fire a `TIMED_OUT` sentinel; if it didn't, that's #185 / Layer 1 territory.
- **`/cw-validate-result` fails to resolve the transcript** — the worker session may have been reaped before the smoke test finished. Surface the session id and the `~/.claude/projects/...` candidate paths.
- **Pre-flight check `not_already_queued` fires for a stale RUNNING entry** — the queue may have a phantom from a prior crash. Surface and suggest `cw doctor --reap`; do not auto-reap.

## Out of scope

- Continuous-loop dispatching (use `cw dev-queue run` without `--once`).
- Multi-ticket batch dispatch (use `cw spawn --headless` per ticket; or build `/cw-fanout` (#187) for parallel N-way).
- Phase A stage-transition events. This skill works without them; with them, monitoring becomes more informative.
- Re-dispatching after a failure. The user owns the dispatch trigger; the smoke test is one-shot.

## Related

- `#172` / PR #192 — `/cw-validate-result` (the diagnostic step this skill delegates to).
- `#188` / PR #189 — `/cw-followup` (the action step suggested on PASS).
- `#187` — `/cw-fanout` (multi-ticket parallel dispatch; companion of this skill).
- `#190` — `plan_source` enum drift surfaced via dogfood.
- `#191` — Status enum drift (`premises_pending_verification` / `ambiguities_pending_resolution`) surfaced via dogfood.
- `#185` — Layer 1 backstop edge case (no Stop hook ⇒ no timeout fire). This skill's 30-minute timeout matches the Layer 1 cap.
