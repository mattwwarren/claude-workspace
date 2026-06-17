# Handoff: #712 shipped (PR #715) — pending LIVE pipeline test → then bug sprint

**Date:** 2026-06-16 (UTC). **Predecessor:** `2026-06-16-rfc0005-phase2-sprint-plan.md`.
**Two jobs for the next session, in order:** (1) the LIVE end-to-end pipeline test
that this long session could not complete, then (2) the big bug sprint. They are
coupled — the live test is blocked by a sprint-class bug.

## What shipped this session (all merged unless noted)
- **#619** closed (B4 parity, #475 evidence).
- **#713 (#675-lite)** MERGED — `cw doctor` per-client project-config validation +
  `preflight.py`/`review-monitor.md` honor the configured tracker.
- **#714 (#662)** MERGED — board S1/S2/S3 polish (pipeline-produced, salvaged).
- **#715 (#712)** OPEN, **needs review/merge** — the staged-FINALIZE worktree fix
  (details below).
- **Filed for the sprint:** #710 (worktree cuts from operator HEAD not default_branch),
  #711 (branch-key `pr_is_merged` for Linear reap path), #712 (now PR #715).

## #712 — what it is and how far it's verified
**Root cause (runtime-confirmed, NOT the #636 code-guess):** cw provisioned the
per-ticket worktree on `auto-dev/<id>`, but the auto-dev IMPL skill creates the PR
branch `dev/<id>` and REVIEW checks it out into cw's worktree. FINALIZE's reuse guard
saw the branch mismatch + dirty `uv.lock` → `StaleWorktreeError` → `blocked_on_user`
with no reason. **FINALIZE never spawned.**

**Fix (PR #715, branch `dev/712-worktree-branch-contract`):** `ClientConfig.feature_branch_prefix`
(clients.yaml, default `dev`); dispatch provisions the worktree on `<prefix>/<id>`;
`create_worktree(allow_dirty_reuse=…)` tolerates same-ticket cross-stage churn (cross-ticket
check intact); doctor wedge fallback + skills (`checkout -B`) aligned. Session NAME keeps
`auto-dev/` (reconcile untouched).

**Verified:** B confirmed live **twice** (worktree provisioned on `dev/422`, not
`auto-dev/422`); `allow_dirty_reuse` unit-tested; full gate suite green; patch cov 100%.

**NOT verified:** a clean live run reaching FINALIZE — see the test below.

⚠️ **PR #715 mergeability:** `dev/712` was cut from `607f9e1` (#708), before #713/#714
merged. #712 and #675 both touch `doctor.py` but **different functions**
(`_check_wedge_repo_ahead` vs `_check_project_configs`) — should auto-merge clean, but
**confirm `gh pr view 715 --json mergeable` before merging**; rebase if needed.

## JOB 1 — the LIVE pipeline test (do FIRST)
**Goal:** prove FINALIZE now *spawns* on a real ticket (the #712 fix end-to-end), and
see whether the pipeline self-completes or hits #636 next.

**The blocker that stopped this session (investigate/work around first):** a **hung
PLAN worker** — 4th distinct stall. On the hardened #422 run, PLAN session `2b69a4c8`
sat `active` for 47 min with `last_result: None`, never emitting a sentinel (reaped at
cleanup). Not #712. Could be a headless worker hang (tool wait, permission stall) or a
roster/reconcile gap. Diagnose via the worker transcript (`~/.claude/projects/...` for
the surface_ref) before re-dogfooding, or just retry and watch the first PLAN session
closely.

**How to run it (lessons baked in):**
1. **Be on `main`** when dispatching (#710 — worktrees cut from operator HEAD; a feature
   branch contaminates the base). The **installed cw already has the B fix** (reinstalled
   from dev/712 this session), so dispatching from main exercises B without conflating it
   into the dogfood PR.
2. Pick a small ticket. **#422 is already hardened** (pre-flight resolutions posted,
   incl. a real staleness correction — the banner text in the ticket is wrong; `cw daemon`
   is now a no-op shim) and still OPEN — reuse it, or harden a fresh one with `harden-ticket`.
3. **Monitor properly — do NOT fire-and-forget a blind ticker.** The driver that worked is
   `/tmp/cw-dogfood-422b.sh` pattern: one `cw dev-queue run --once` per tick, log
   `stage/status/session`, and **exit the instant** status is terminal or `blocked_on_user`
   (no 45× spin). Watch for the `>>> #712 SIGNAL` (finalize stage spawns a session).
   Or use `cw-queue-peek` for active inspection. `cw-smoke-test`'s single-tick model does
   NOT drive the staged pipeline (one tick = one stage) — drive per-stage.
4. **Expected decisive outcomes:** FINALIZE spawns → COMPLETED (full self-complete, #712
   + #636 both OK) **or** FINALIZE spawns → blocked (#712 fixed, #636 is the next hop) **or**
   blocks before finalize (a different bug).

**After a green live run:** confirm #715 (or note #636 as the remaining gap), then proceed
to Job 2.

## JOB 2 — the bug sprint
The repeated stalls this session ARE the sprint. Agenda, roughly by leverage:

**Pipeline-reliability (surfaced this session):**
- **#716** — hung PLAN worker (47-min `active`, no sentinel, no timeout fire).
- **#717** — preflight `cw_backend_healthy` false-negative in no-TTY subprocess (manual
  `cw doctor` is healthy/exit 0); spuriously blocks the smoke-test preflight.
- **#636** — finalize `gh pr create` blocked by the headless permission classifier (the
  last hop to autonomous self-completion).
- **#710** — worktree cuts from operator HEAD, not `default_branch`.
- **#711** — branch-key `pr_is_merged_for_ticket` (Linear reap-path safety).

**Standalone backlog (from the predecessor plan):** #542 (wait→124), #630 (worktree gc),
#395 (schema-version skew), #588/#589/#590 (lane/dispatch correctness), #520, #519, #422
(banner fix — still unimplemented; hardened + ready).

**RFC 0005 remainder (deferred again):** #618 B3 (worktree teardown — now intertwined with
#710), C-chain #620/#621/#622/#623 (handoff protocol), #626 E2 (heterogeneity proof).

## Operator state
- **main** = `84e4c42` (has #708 + #713 + #714). Clean.
- **`cw` INSTALLED = the dev/712 build** (has B + allow_dirty_reuse). After #715 merges,
  `uv tool install --reinstall --no-cache .` from main + `bash scripts/install-skills.sh`.
- **Open PR:** #715 (#712) — review/merge. Auto-merge was correctly **denied** earlier
  (needs explicit operator authorization).
- **dev-queue:** claude-workspace clean (0/0/0). Hung session reaped.
- **Branches:** `dev/712-worktree-branch-contract` pushed (PR #715). dev/422 deleted.
- Reinstalled skills are live in `~/.claude` (incl. the `checkout -B` change).

## Carry-forward gotchas
- `-s small` REQUIRED on dogfood adds.
- Be on `main` when dispatching (#710), or the dogfood worktree inherits your branch.
- The dogfood ticker MUST exit on `blocked_on_user` (not terminal) — else it blind-spins.
- `~/.cw/wt/` + `.claude/worktrees/` accumulate (#630) — do NOT mass-remove.
