---
type: scope-complete
created: 2026-06-16T20:44Z
reason: Job 1 (live #712 validation) DONE and green — handing off for Job 2 (the bug sprint)
---

# Handoff: #712 LIVE-VALIDATED (pipeline self-completes autonomously) → bug sprint

**Date:** 2026-06-16 (UTC). **Predecessor:** `2026-06-16-rfc0005-phase2-712-shipped-test-and-sprint.md`.
**This session closed JOB 1.** The next session is **JOB 2 — the bug sprint** (agenda below).

## Summary
The staged auto-dev pipeline **self-completed end-to-end, autonomously, on a real ticket** —
#422 went enqueue → plan → impl → review → finalize → **PR #720 created + auto-merged → issue
#422 CLOSED**, no operator intervention. This proves #712 (FINALIZE now spawns) AND that the
final ship hop works headless. #636 and #716 were re-triaged with live evidence (both weaker
than filed). Nothing is in-flight; state is clean.

## JOB 1 — DONE (the live validation the predecessor gated the sprint on)

Two live runs of #422, dispatched from `main` (per #710), `-s small`:

**Run 1** (cw = dev/712 build, before #715 merged):
`plan→impl→review→FINALIZE` all spawned; finalize reached `stage4a_merge_gate` and correctly
**held** because PR #715 was still open (`merge_gate_blocked`→blocked_on_user). This is the
#712 proof — FINALIZE *spawned*, which never happened pre-#712.

**Run 2** (after operator merged #715; cw reinstalled from `main`):
`plan(3e46c6b1)→impl(aeff9ddf)→review(1f6cd543)→finalize(94178240)→COMPLETED`.
Finalize sentinel `status: shipped`, `stage5_post_create`; **PR #720** (`auto_merge: true`,
base main) → CI green → **merged 19:48:59** → **#422 CLOSED**. Fully autonomous.

Both runs confirmed live: worktree provisioned on `dev/422` (#712 branch contract) with
`base_ref` = main HEAD (#710 base correct). The #716 reconcile gap did **not** reproduce —
every stage advanced on the very next dispatch tick in both runs.

**Driver used (reusable):** `/tmp/cw422-drive.sh` — one `cw dev-queue run --once` per ~40s tick,
logs `stage/status/session`, flags FINALIZE spawn, **exits the instant status is terminal or
blocked_on_user** (no blind spin). This is the correct monitoring pattern for the staged engine
(one tick = one stage advance; `cw-smoke-test`'s single-tick model does NOT drive it).

## Re-triage completed this session (both already updated on GitHub)

- **#636** (ship-it subagent `gh pr create` classifier block) — **did NOT reproduce.** In Run 2,
  `gh pr create` ran in the finalize session and succeeded → PR #720; sentinel `friction_highlights`
  was `[]` (no `prep_pr_subagent_classifier_block`). One *transient* "Stage 2 classifier error
  (usually transient — retrying often succeeds)" denial appeared and self-recovered on retry —
  distinct from the persistent subagent block #636 describes. Comment posted
  (`#issuecomment-4723478996`). **Operator decision pending: close as not-reproducing, or keep
  open downgraded to hardening** (deterministically allowlist `gh pr create`/`gh pr merge --auto`).
- **#716** (was "headless PLAN worker can hang") — **misdiagnosed + did not reproduce.** The
  original "hung" worker `5429a292` actually completed Stage 1 and emitted a valid `plan_approved`
  sentinel; it sat `active`/`last_result: None` only because cw never reconciled that emitted
  sentinel into `last_result`. **Title reframed** to "emitted PLAN sentinel occasionally not
  reconciled into last_result — staged advance stalls (intermittent)"; comment posted
  (`#issuecomment-4723479149`). Real target: the reconcile emitted-sentinel router
  (`_apply_sentinel_to_task`/`apply_staged_decision` in `dispatch.py`). Intermittent — downgrade.

## JOB 2 — the bug sprint (start here)

All issues below are OPEN (verified 2026-06-16 20:44Z). Suggested order by leverage:

**Pipeline-reliability (highest leverage):**
- **#717** — preflight `cw_backend_healthy` false-negative in no-TTY subprocess (manual `cw doctor`
  exit 0); spuriously blocks the smoke-test preflight.
- **#710** — worktree cuts from operator HEAD, not `default_branch`. (Validated as the cause of
  base contamination this sprint; the fix is to provision from `default_branch`.)
- **#711** — branch-key `pr_is_merged_for_ticket` (Linear reap-path safety).
- **#716** — *re-triaged above* — intermittent sentinel-reconcile gap. Lower priority than filed.
- **#636** — *re-triaged above* — not reproducing. **Decide close vs downgrade before working it.**

**Standalone backlog:** #542 (wait→124), #630 (worktree gc — note `~/.cw/wt/` + `.claude/worktrees/`
accumulate; do NOT mass-remove), #395 (schema-version skew), #588/#589/#590 (lane/dispatch
correctness), #520, #519.
*(#422 is SHIPPED — remove from the predecessor's backlog list.)*

**RFC 0005 remainder:** #618 B3 (worktree teardown — intertwined with #710), C-chain
#620/#621/#622/#623 (handoff protocol), #626 E2 (heterogeneity proof).

## Operator state
- **main** = `f0fb289` (has #712/#715 + #422/#720). **Clean** (untracked handoff docs + `.claude/worktrees/` only).
- **cw INSTALLED = main build** (post-#712), skills synced from main. No reinstall needed.
- **dev-queue:** claude-workspace **clean** (0 pending / 0 running / 0 blocked). #422 in completed.
- **No open PRs** from this work (#715, #720 both merged). No live sessions.
- **Worktrees:** stale `dev/422` worktree + local/remote branches removed this session (clean re-dispatch).

## Carry-forward gotchas (unchanged + new)
- `-s small` REQUIRED on dogfood adds.
- Be on `main` when dispatching (#710) — worktree inherits operator HEAD as base until #710 lands.
- The dogfood driver MUST exit on `blocked_on_user` (not just terminal) — `/tmp/cw422-drive.sh` does.
- `~/.cw/wt/` + `.claude/worktrees/` accumulate (#630) — do NOT mass-remove; remove a specific
  worktree only for an intentional clean re-dispatch (delete its local AND remote branch).
- Transient "Stage 2 classifier error … usually transient — retrying often succeeds" Bash denials
  occur — retry, don't treat as a hard block.

## Resumption Prompt

```
Continuing claude-workspace JOB 2 — the bug sprint. Job 1 (live #712 validation) is DONE
and green; see docs/handoffs/2026-06-16-712-live-validated-bug-sprint.md.

State: main = f0fb289 (clean); cw installed = main build (no reinstall); dev-queue clean.
The staged auto-dev pipeline self-completes autonomously (proven live: #422 → PR #720 merged).

Sprint agenda, by leverage:
  pipeline-reliability: #717, #710, #711  (then re-triaged #716/#636 — see below)
  standalone backlog:   #542 #630 #395 #588 #589 #590 #520 #519
  RFC 0005 remainder:   #618 (B3) #620 #621 #622 #623 (C-chain) #626 (E2)

Already re-triaged this session — read the GitHub comments before touching:
  #636 NOT reproducing — DECIDE close vs downgrade-to-hardening first.
  #716 reframed (intermittent sentinel-reconcile gap, not a hang) — lower priority.
  #422 is SHIPPED — drop it from any backlog list.

Start by picking #717 (smoke-test preflight false-negative) — hardest blocker to further
dogfooding — OR ask the operator which sprint item to take first. Use harden-ticket before
dispatching any non-trivial ticket; dispatch from main; drive per-stage with
/tmp/cw422-drive.sh's pattern.
```
