> Companion appendix to /auto-dev-intake. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 0 Intake — Appendix

Rare-branch procedures and design rationale extracted from
`.claude/commands/auto-dev-intake.md` (#1879). Each section below is reached
from a named trigger sentence in the core doc; nothing here is required on the
common path (healthy fetch, in-sync `main`, no pre-existing PR).

---

## Behind-only wait-and-recheck: why no re-fetch

`ORIGIN_MAIN` was captured in Step P1 and the shared `main` ref is what advances
(via the base checkout, not this worktree), so a local `rev-parse` observes it.
Checkpoints land at T+30s, T+60s, T+90s.

The T+90s ceiling matches the `TICK_STALE_SECONDS` convention
(3x `tick_interval_seconds`) rather than being an independently-chosen timeout;
if that convention moves, this window should move with it.

---

## Fetch-failure signature mirror: provenance and sentinel rationale

The signature list in the core doc is a PROSE MIRROR of
`src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES`; keep the two copies in
sync — `test_unavailability_signatures_mirrored_in_prose` is the drift guard.

`MCP-github-unreachable` is deliberately not mirrored — no verified signature
exists yet; see the `src/cw/unavailability.py` module docstring.

The EXIT must happen before spawning any agent and **before** the
`stage.entered` (`s0_intake`) emission: a fetch that never succeeded has no
stage-entry to correlate against.

`next_actions` must be `["manual_intervention"]`. The only other legal member of
`_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS` (`auto_dev_result/schema.py`) is
`sync_local_main`, which is the Origin Sync surface and wrong here.
`reason: "operator_unavailable"` is already in
`OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, so no schema change is required. A fetch
failure matching no signature is not handled by this block — fall through and
let the ordinary failure path report it.

---

## Open-PR self-check (#1862): rationale, reliability rule, and status choice

**Why the check exists.** A dispatch can succeed, push a branch, and open a PR
while its queue row is never advanced past PLAN/IMPL (the session died before
its sentinel landed, or the sentinel was never harvested). The next dispatch
then re-plans and re-implements a ticket whose work is already sitting in an
open, unmerged PR.

**Which prefix to probe.** Use the effective branch prefix (`--branch-prefix` if
given, else the client's `feature_branch_prefix`, default `dev`) — the same key
`cw` itself probes. A non-empty result means this ticket already has an open PR.

**Reliability rule — fail open.** Treat only a *reliable* answer as a hit: a
non-zero exit, a timeout, or a missing `gh` binary is **not** evidence of a PR.
Fall through and continue the run, exactly as `cw`'s own gate fails open. Do not
gate a refusal on an unreliable signal.

**Why these field values.** `pr` must stay null — this run did not create that
PR, and the schema rejects a non-null `pr` on this status. The discovered PR's
identity belongs in `blocker.details`; that string is the operator's whole
triage signal. `next_actions` must be empty, because the status is a
terminal-reject status. Do NOT report `no_op` (nothing is complete — the PR is
unmerged) or `blocked` (nothing is broken — the PR is healthy, just not this
session's to duplicate).

**Relationship to `cw`'s own gate.** This is not a substitute for `cw`'s
pre-dispatch gate, which catches the same condition before a session is even
spawned. This check covers the paths that gate does not — an interactive
`/auto-dev` run, and a resume that re-enters intake with the row already
claimed.

---

## Batch mode (interactive only)

Batch mode is undefined in headless mode; this procedure runs only from an
interactive `/auto-dev` invocation carrying filter flags.

- Call the tracker's batch-select op with the provided filters (`list_issues`
  for `linear`; `gh issue list --json number,title,labels,state [--label …]`
  for `github-issues`). Omitted filters default: `state` → `"Todo"`,
  `assignee` → `"me"`.
- For each issue, read the description to check for existing plan content and
  estimate a scope hint from its keywords.
- Present a numbered list (`N. <ID>: <title> [~<Small|Large>, has plan|no plan]`).
- **AskUserQuestion:** "Select tickets to process (e.g., '1,3' or 'all'), or 'abort':"
- Build an ordered queue from the selection. Order matters — tickets process in
  the order specified.

---

## `.cw/context.json` idempotency: why the guard keys on the writing session (#837)

`requeue` reuses the worktree (`create_worktree(..., allow_dirty_reuse=True)`),
so a stale `.cw/context.json` survives across runs. A `ticket_id`-only guard
skipped re-fetch on every requeue, and operator resolutions never reached the
plan stage. Keying the skip on the writing session re-fetches on requeue while
keeping the within-session fetch-once optimization.

`.cw/context.json` is distinct from `.claude/cw-context.json` (written by
`cw dispatch`; `session_id` and `ticket_id` only). Per-stage files read
`.cw/context.json` for full ticket orientation; the `CW_SESSION`/`TICKET`
bootstrap reads `.claude/cw-context.json`.
