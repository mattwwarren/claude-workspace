# Sprint plan: Close RFC 0005 Phase 1+2 + unblock Linear (lite)

**Date:** 2026-06-16. **Predecessor handoff:** `2026-06-16-v1.2.0-staged-engine-shipped.md`.
**Goal:** the staged engine reaches a clean Phase-2 exit — the **handoff protocol is real**
(trace comments, draft-PR, finalize scrub), not just stages firing — and cw's deterministic
path stops assuming GitHub, **proven against a real Linear ticket.**

## Why this sprint (context)
v1.2.0 shipped the staged engine; #475 proved it completes a real ticket end-to-end
(B4 closed on that evidence). But re-scoping against live code showed the C-tier
**handoff protocol is mostly unbuilt** — #475 worked only because stages delegate to
the existing `/auto-dev-*` skills. Separately, the operator uses cw against **Linear at
work** (headless Linear MCP auth confirmed working), so gh-only deterministic resolution
is a real blocker, not deferrable polish.

## Locked decisions
1. **Handoff-protocol = skill-prompt-driven.** Plain-string `<!-- cw-stage:* -->` markers,
   tracker-neutral by construction. The stage skill posts via whatever MCP/CLI is active —
   works for Linear *and* github-issues for free. No cw-core tracker shim this sprint.
2. **#662 board = flat, documented deviation** (one Panel per client×lane). Nested per-client
   deferred until a real multi-lane client exists.
3. **Engine-core changes (#618, #675-lite) = hand-done on main, monolith-style.** They edit
   paths the dispatcher itself runs on (worktree lifecycle, reap merged-detection) — ouroboros
   if staged-dispatched.
4. **Linear scope this sprint = "lite" only.** Full typed `TrackerDescriptor` seam (#680 +
   A2–A6) is its **own future session** — foundational, but NOT needed to use cw on Linear.

## Re-scoped status (verified against code, 2026-06-16)
- **#625 E1 — DONE already** (executor.py:67-69, per-stage model + lane override, tested).
  Remaining E-work is only the #626 *proof*.
- **#618 B3 — PARTIAL.** Worktree created-once + reused (dispatch.py:504, idempotent).
  **Missing:** teardown at FINALIZE — no `remove_worktree()` on COMPLETED; manual `--cleanup` only.
- **#620 C1 — ABSENT.** `.cw/` git-excluded (worktree.py:199) but never written/read/scrubbed.
  Zero `cw-stage` emission in code.
- **#621 C3 — ABSENT** in core; only the salvage path has draft-PR logic. PR creation delegated.
- **#622 C2 — ABSENT.** COMPLETED just sets status; no scrub/fold/ready/guard.
- **#675 (Linear) — narrower than it reads.** `resolve_client` (dev_queue.py:361) already
  resolves `GEN-403` via `linear_prefix_map` — Linear works there; bare github ints break
  (irrelevant to operator). The one broken deterministic op is `pr_is_merged_for_ticket`
  (gh.py:90) which does `gh issue view <ticket_id>` — breaks on `GEN-403`. Everything else
  for Linear is already MCP/prose-driven (auto-dev-intake.md).

## Workstreams (by dependency + risk)

### A — closeouts + Linear-lite (do first)
- [x] **#619 B4** — closed on #475 evidence (done 2026-06-16).
- [ ] **#662** — board flat + remove dead `_STAGE_COLUMNS` (board.py:41) + multi-lane test.
      Scope: `src/cw/board.py`, `tests/test_board.py`. Small → **auto-dev dispatch**
      (`cw dev-queue add 662 -c claude-workspace -s small -t 7200`). Doubles as a
      "pipeline still green on a fresh small ticket" check.
- [ ] **#675-lite** — `cw doctor` validates `.claude/project-config.yaml` (parses,
      `tracking.primary.system ∈ {github-issues, linear}`, prereq probe: gh on PATH /
      Linear MCP reachable) **+** branch-key `pr_is_merged_for_ticket` so it stops doing
      `gh issue view <linear-id>` (key off `gh pr list --head <branch>` — already
      tracker-agnostic). **Hand-done** (touches reap path). Unblocks Linear; do early so
      the rest dogfoods against Linear. Acceptance items 1+2 of #675; leave #680 descriptor
      for its own session.

### B — engine core (hand-done, ouroboros)
- [ ] **#618 B3** — `remove_worktree()` at terminal stage + reap/teardown hardening.
      Watch: don't double-remove a worktree another stage may reuse on re-run.

### C — handoff-protocol sub-epic (sequential, skill-driven, Linear-aware)
- [ ] **#620 C1** — each stage posts `<!-- cw-stage:<name> -->` (decisions/friction/deferred)
      before its sentinel + writes ephemeral detail under gitignored `.cw/`.
- [ ] **#621 C3** — REVIEW pushes branch + opens **draft** PR; idempotent re-run updates
      the existing draft by branch lookup, never a 2nd.
- [ ] **#622 C2** — FINALIZE scrub `.cw/`, fold stray tracked plan docs, draft→ready,
      assign reviewers; backstop: refuse-ready if `git ls-files .cw/` non-empty → BLOCKED_ON_USER.
      *(depends C3)*
- [ ] **#623 C4** — migrate `harden-ticket` skill into a delegated HARDEN stage.
      Posts `<!-- cw-stage:harden -->`. *(depends C1)*

### D — proof (last)
- [ ] **#626 E2** — heterogeneity dogfood: opus-plan / sonnet-impl / sonnet-review via
      `StagePipelineConfig.executors`. E1 already coded; this proves the map end-to-end.

## Critical path
A(#675-lite) → create free Linear account + 1 test ticket → **dogfood the full C-chain
against Linear** → D proves heterogeneity. B is independent — slot anywhere.

## The validation loop that justifies the sprint
Once #675-lite lands: free Linear account + one test ticket, then dogfood the entire C-chain
against Linear. If trace comments + draft-PR + finalize all work through Linear MCP with
**zero tracker-specific cw code**, that's empirical proof the "agnostic" claim is real and
retroactively de-risks the #680 epic.

## Dispatch strategy
- **auto-dev-dispatchable** (safe, not engine-core): #662, and the C-chain skill-prompt edits
  *as long as they don't touch `src/cw/dispatch.py`/`reconcile.py`*. If a C-ticket needs a
  thin cw-core helper (e.g. the FINALIZE `.cw/` guard), do that part hand-done.
- **hand-done on main** (ouroboros): #618, #675-lite, any cw-core dispatch/reconcile change.

## Operator state at sprint start
- main clean, level with origin (0/0). `cw` INSTALLED = 1.2.0.
- dev-queue: claude-workspace clean.
- Reinstall recipe after any cw-behavior merge: `uv tool install --reinstall --no-cache .`
  then `bash scripts/install-skills.sh`.
- Quality gates: see CLAUDE.md (ruff / ruff format / mypy --strict / pre-commit /
  unit cov ≥88% / patch ≥90% / integration / diff-cover ≥90%).

## Carry-forward gotchas (from predecessor handoff)
- `-s small` REQUIRED on dogfood adds (null-tier PLAN sentinel can't resolve scope otherwise).
- global-claude parked on stale `dev/669` — noisy per-tick; `git -C <global-claude> checkout main` when convenient.
- `~/.cw/wt/` + `.claude/worktrees/` accumulate (#630) — do NOT mass-remove.
