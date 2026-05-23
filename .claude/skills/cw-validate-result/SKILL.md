---
name: cw-validate-result
description: Inspect what a cw-dispatched /auto-dev session actually did — locate its transcript, extract the AUTO_DEV_RESULT sentinel, validate against the headless contract, and print a clear PASS/FAIL with what the run shipped, blocked on, or skipped. Use when the user asks "what happened on session X", "did #N actually ship", "validate the headless result", "why did /auto-dev exit", or wants any forensic read on a completed auto-dev run.
---

# cw-validate-result

Forensic validator for a finished `/auto-dev` session. Answers "what actually happened" with concrete PASS/FAIL rows instead of a vague "looks blocked".

This is the diagnostic surface. For taking action on the result, hand off to `/cw-followup`.

## When to use

- A `/auto-dev` session finished but the worker session's `last_result` is null.
- The user asks "did session X actually ship the thing".
- A run looks blocked and the cause is unclear — what failure mode hit?
- After a meta-test spray, when you need to triage N sessions in parallel without acting on any of them yet.

## Inputs

One of:
- `--session-id <SHORT>` — short cw session id (prefix match allowed).
- `--ticket-id <N>` — most recent session matching the ticket.
- `--transcript-path <PATH>` — direct path to a Claude JSONL transcript (for off-machine or archived runs).

## How it works

Runs the bundled validator script:

```bash
uv run --project /home/matthew/workspace/personal/claude-workspace python \
  .claude/skills/cw-validate-result/scripts/validate_sentinel.py \
  --ticket-id <N>
```

The script delegates transcript resolution + sentinel parsing to the `/cw-followup` skill's `parse_sentinel.py` (single source of truth — never re-implement). On top of that, it walks the headless-contract checklist and emits PASS/FAIL per row.

## Outcomes

The script reports one of four `outcome` values. Treat each differently:

| `outcome` | Meaning | Exit |
|---|---|---|
| `valid` | Sentinel parsed cleanly as a canonical `AutoDevResult` | 0 |
| `producer_status_unknown` | Sentinel present, but the producer emitted a status not yet in the parser's enum (e.g. `premises_pending_verification`). Treat as a real outcome the human should act on. | 0 |
| `invalid_sentinel` | Sentinel present but schema validation failed. Producer/consumer drift — report it. | 1 |
| `no_sentinel` | No sentinel block in the transcript — the run exited before emitting. Diagnose via `references/no-sentinel-patterns.md`. | 1 |

## Checks

Each output row is one of:

| Check | What it verifies |
|---|---|
| `sentinel_emitted` | At least one assistant text block contained the sentinel pair. |
| `status_is_canonical` | `effective_status` is one of the 8 canonical `Status` Literal values. Failing this with `outcome=producer_status_unknown` is expected today; failing it with other outcomes is a bug. |
| `health_present` | `health` carries `lowest_agent_confidence`, `any_incomplete_risk`, `recommendation`. |
| `blocker_iff_blocked` | Cross-field invariant from §3.3: `blocker` non-null iff `status=blocked`. |
| `pr_iff_shipped` | Cross-field invariant: `pr` non-null iff `status=shipped`. |
| `wait_for_ci_iff_shipped` | Cross-field invariant from §4.3: `wait_for_ci` in `next_actions` iff `status=shipped`. |

## Reporting

After running the validator, print:

1. The `outcome` (one of the four above).
2. The `effective_status` and the session's worktree path.
3. Any failing checks with their `detail` fields.
4. The `next_actions` array verbatim — these are the producer's own follow-up hints.

Keep it tight — caveman style:

```
session 76f060e7 (#185): outcome=producer_status_unknown, status=premises_pending_verification
  - status_is_canonical: FAIL (premises_pending_verification not in Status enum)
  next_actions: ['resolve_premises', 're_dispatch_after_disposition']
```

If outcome is `valid` or `producer_status_unknown` and the user wants to act on the result, suggest `/cw-followup --session-id <SHORT>`.

If outcome is `no_sentinel`, read `references/no-sentinel-patterns.md` and surface the most likely cause based on what the transcript tail looks like.

If outcome is `invalid_sentinel`, surface the validation error verbatim — the parser's `BlockedResult.blocker.details` carries it.

## Out of scope

- Performing post-run actions — that's `/cw-followup`.
- Re-dispatching — that's `cw spawn --headless` or `cw dev-queue add`.
- Modifying the sentinel schema — surfacing drift is in scope, fixing the parser is a separate ticket (#190, #191 surfaced via dogfood today).

## Related

- #188 / PR #189 — `/cw-followup` (companion skill; owns `parse_sentinel.py`)
- #171 — `/cw-smoke-test` (end-to-end dogfood; uses this validator as its check step)
- #117 / #140 — sentinel parsing / schema migration (this skill rides on top of whatever schema is current)
- #190 / #191 — producer/consumer drift surfaced through dogfood; will reduce the `producer_status_unknown` / `invalid_sentinel` outcomes when fixed
