# Handoff: RFC 0005 forward-compat seams shipped + v1.1.3 released (milestone #7 complete) → engine sprint (milestone #8) next

**Date:** 2026-06-14. **From:** the RFC 0005 A-series orchestration session.
**Prior handoff:** `2026-06-14-v1.1.2-released.md` (the 1.1.2 reliability cluster +
the A-series sprint framing — still the source for the dispatch recipe and the
RFC-seam ordering). This doc covers the milestone-#7 sprint (all four
forward-compat seams), the unusually clean execution, and what's next.

## Shipped — milestone #7 "v1.1.x — RFC 0005 forward-compat seams" (open=0, closed=4)

main = `e54b09f`. All four merged, all CI green (ubuntu + macos + package-smoke).
All shipped **first try — zero ambiguity bounces, zero plan-review bounces** (see
the lesson below; this is the headline result of the sprint).

- **#615 A4 / PR #651** — `.cw/` registered in `$GIT_COMMON_DIR/info/exclude` on
  worktree create (idempotent check-then-append; never touches the consumer's
  committed `.gitignore`). Independent, parallel-safe; dispatched alongside A1.
- **#612 A1 / PR #652** (the "harden hard" one) — additive stage data-model +
  schema bump. Added `Stage(StrEnum)`, `DEFAULT_STAGE`, `StageExecutorConfig`,
  `StagePipelineConfig`, `LaneConfig.pipeline` (lane override) + `ClientConfig.
  pipeline`, `TicketTask.stage`/`stage_base_ref`, `Session.stage`. **Bumped
  CW_STATE_SCHEMA_VERSION 9→10 and DEV_QUEUE_SCHEMA_VERSION 3→4** with store-level
  migration helpers (mirroring the v8→v9 `lane` precedent) + round-trip tests.
  All fields dormant; no dispatch behavior change. Touched `models.py`/`config.py`/
  `dev_queue.py` + tests only.
- **#613 A2 / PR #653** (worker-opened; my redundant #654 merged empty — see lesson) — new `src/cw/executor.py`: `StageExecutor` Protocol
  (`@runtime_checkable`, `spawn -> str`, `stage_sentinel_schema -> dict`) +
  `ClaudeNativeExecutor` wrapping `spawn_create_impl` + `native_daemon`. Model
  forwarded via `client.model_copy(update={"worker_model": effective})` (no
  double `--model`). `stage_sentinel_schema` bridges through
  `AutoDevResult.model_json_schema()`. Unwired — not called by dispatch.
- **#614 A3 / PR #655** (worker-opened; my redundant #656 merged empty — see lesson) — `cw schema stage-output <stage>` subcommand (list/show
  already existed). Reuses A2's `ClaudeNativeExecutor().stage_sentinel_schema`
  as the single schema source; free-arg `<stage>` validated via `Stage(...)` →
  `UsageError`; raw JSON output. `REGISTRY`/`len==3` untouched.

## DO THIS NEXT — two open decisions, then the engine sprint

### 1. Release — DONE: v1.1.3 shipped (2026-06-14)
**v1.1.3 is released** (tag `v1.1.3` on `6a6fe2f`; PR #657; `release.yml` green;
GitHub Release published, not draft; `cw --version` → 1.1.3; `cw doctor` healthy).
The A1 acceptance criterion was satisfied first: the v9→v10 / v3→v4 migration was
**verified against the real live on-disk state** (`~/.local/share/cw/sessions.json`
v9, 341 sessions → v10 with all `stage` defaults present, full `CwState` validation;
`dev_queue.json` v3 → v4, `DevQueueStore` validates). Installed 1.1.3 migrates the
live state files to v10/v4 on first write. No further release action needed.

### 2. The engine sprint (milestone #8 "v1.2.0 — RFC 0005 staged pipeline", 11 open)
This is where the seams get wired. Ordering per RFC 0005:
- **#616 B1** decompose /auto-dev into per-stage entrypoints → **#617 B2**
  (stage advance loop + executor-by-stage spawn — consumes A1+A2) → **#618 B3**
  (shared per-ticket worktree lifecycle — consumes A4) → **#619 B4** (parity vs
  monolith, Phase-1 exit bar).
- **#620-623 C*** stage trace comments + FINALIZE + REVIEW draft-PR + HARDEN
  stage. **#636's real fix folds in here** — C2/C3 (FINALIZE/REVIEW) own PR
  creation; the carry-forward requirement is already posted on #621/#622.
- **#624 D1** cw board TUI; **#625-626 E*** per-stage executor resolution + e2e
  heterogeneity proof.
Then milestone #9 (foreign executors **#627-628 F*** — CodexExecutor/GlmExecutor),
which consume A3's `cw schema stage-output` contract.

## Other open reliability backlog (still not scheduled)
Unchanged from prior handoff: **#630** (worktree gc for squash-merged branches),
**#588** (skip_reason=no_pending while pending>0), **#589** (add_ticket lane
validation), **#590** (doctor --reap can't unwedge BLOCKED_ON_USER lane),
**#591** (sentinel accepts pr=42 placeholder), **#592** (epic: post-crash slot
lifecycle), **#605** (agent-driven salvage judgments), **#608** (graceful config
upgrade on knob rename).

## Operator state (a fresh session must know)
- **cw runtime:** main `6a6fe2f`; installed `cw` = **1.1.3** (released this session).
  `~/.claude-workspace/orchestrator.yaml`: `per_client_ceiling:
  claude-workspace: 3`; `reap_policy: auto`.
- **dev-queue:** claude-workspace shows 615/612/613/614 as COMPLETED/CANCELLED;
  no live sessions, no monitors running. Other clients (global-claude,
  ai-home-lab) have their own residual rows — untouched this session.
- **Orchestrator checkout** on clean `main`, level with origin. `uv.lock` carries
  a benign uncommitted `M` (pre-existing; survives `--ff-only` since no overlap).
- **Worktrees:** auto-dev-612/613/614 worktrees still present under `~/.cw/wt/`;
  #630 debt unchanged — do NOT mass-remove.
- **global-claude** still has the uncommitted `commands/auto-dev.md` local-coder
  testing block (per prior handoff) — do not clobber.

## Being a well-behaved orchestrator (the playbook that worked)

This is the conduct that made the last few sessions go well — follow it, and keep
it honest with the corrections below.

1. **Harden before you dispatch — every non-trivial ticket.** Run a read-only Plan
   Reviewer sweep against *current `main`* (not a stale worktree), grounded in a
   precise file list with line ranges. Resolve the technical/convention findings
   yourself; escalate ONLY genuine product/scope forks to the operator in one
   batched question. Post a `## Pre-flight Resolutions` comment with a hard scope
   fence ("in-scope = these files ONLY; do NOT touch X/Y/Z"). This bought 4/4
   first-try ships this sprint.
2. **Dispatch chokepoints, respect the dependency graph.** Parallel-safe + independent
   tickets go out together; dependent ones wait for their prerequisite to *merge*
   (not just ship). Before each dispatch: `git fetch origin main && git merge
   --ff-only origin/main` to clear the freshness gate.
3. **Verify world-state, never trust queue status.** A "completed"/"timeout" queue
   row is often a #578/#315 false signal. Confirm via the real artifact: the PR
   (`gh pr view`), the merge commit, the branch diff. State what you verified.
4. **Use immediately-consistent queries.** `gh pr list --head dev/<id>` and
   `git ls-remote`/`git log` are immediately consistent; `gh pr list --search
   "in:title"` is INDEXED and lags minutes. Concluding "no PR" from a laggy search
   is how I opened redundant no-op PRs this session (see lesson). When a signal is
   ambiguous, **re-poll before acting** — the worker's own machinery may still be
   in flight.
5. **Surface before you act on async results.** When a monitor/subagent/background
   job returns, state what you learned and what you propose, THEN act — especially
   for outward/irreversible steps (opening a PR, deleting a branch, tagging a
   release). Don't bundle a consequential action into the report of a result.
6. **Gate outward actions on the operator.** Dispatching workers, opening PRs with
   auto-merge, cutting releases — name the action and get the go, unless the
   operator has given standing authorization (e.g. "trust CI" → orchestrator PRs
   may go straight to `--auto --squash`). "Execute the sprint" authorizes the loop,
   not every irreversible step inside it.
7. **Clean up after yourself, carefully.** Cancel ship-but-stuck tasks
   (`cw dev-queue cancel <id>`) so they go terminal. Prune duplicate worker branches
   — but ONLY after confirming their PR isn't mid-merge (`gh pr list --head ...`).
   Do NOT mass-remove worktrees (#630).
8. **Know your context budget and hand off honestly.** Big multi-ticket sprints
   (e.g. milestone #8) deserve a fresh session rather than a depleted one. Writing a
   precise handoff IS the deliverable that lets the next session continue cleanly;
   don't push through on fumes (counter-cyclical rigor).
9. **Correct the record when you're wrong.** If a later finding contradicts an
   earlier claim (as the #636 correction below does), fix the handoff and say so
   plainly. No unverified claims — tool results are verification, fluent prose is not.

## Lessons (carry forward)

- **Pre-flight hardening + a hard scope guard produced 4/4 first-try ships, zero
  bounces.** Contrast the prior wave's #637 (4 auto-dev rounds of ambiguity/plan-
  review whack-a-mole). For each ticket: a read-only Plan Reviewer sweep against
  *current main* surfaced every implementation-determining gap; I resolved the
  technical ones, posted a `## Pre-flight Resolutions` comment, and fenced scope
  explicitly ("in-scope = these files ONLY; do NOT touch X/Y/Z"). Every worker
  landed a diff that touched *only* the named files and honored every resolution
  (A3's diff was a line-for-line match to the resolution comment). **The scope
  fence is as load-bearing as the ambiguity resolution** — it kept dormant tickets
  from drifting into dispatch.py/reconcile.py. Keep doing this for every non-trivial
  ticket; it's cheaper than one bounce.
- **CORRECTION — #636 did NOT block the workers this session; I jumped the gun and
  opened redundant no-op PRs.** Both #613 and #614 workers opened their OWN PRs
  successfully (#653 from `dev/613-fix`, #655 from `dev/614`) and auto-merged them.
  My monitor fired a "worker left roster, no PR yet" signal; I checked with
  `gh pr list --search "<id> in:title"`, got `[]`, concluded #636 had struck, and
  orchestrator-opened my own PRs (#654, #656). Those merged as **empty no-ops**
  because the worker's content was already on main. Root cause: **`gh pr list
  --search "in:title"` is eventually-consistent and lagged the worker's PR by
  minutes** — the PR existed (~9 min old) but didn't show in the indexed search.
  I even pruned `dev/613-fix` *after* its PR #653 had already merged.
  **Lessons:** (a) check for a worker's PR with `gh pr list --head dev/<id>` (direct
  ref, immediately consistent), NEVER `--search "in:title"` (indexed/laggy);
  (b) on a "left roster, no PR" signal, **wait and re-poll** for the worker's own
  PR before orchestrator-opening — the worker's `gh pr create` may still be in
  flight; (c) before orchestrator-opening, `gh pr list --head dev/<id>` AND check
  for sibling branches (`git ls-remote --heads origin '*<id>*'`) so you don't prune
  a branch whose PR is mid-merge. The #636 ship-block from the prior handoff may be
  narrower/intermittent than assumed — do not treat "worker finished, branch pushed"
  as proof the worker failed to open a PR.
- **`cw dev-queue wait` is a poor monitor for shipping workers — it rode to its
  full ceiling (124 / false `timeout`) on #612** because ship-but-stuck (#578)
  cleared `session_id`, so the sentinel-aware detection never matched. The PR had
  already merged ~mid-wait. **Better monitor:** a background poll on the *real*
  signals — PR appears (`gh pr list --head dev/<id>` — direct ref, NOT the laggy
  `--search "in:title"`) or the worker leaves the daemon roster (`cw status | grep
  auto-dev/<id>`) — then verify world-state on wake. Caveat learned the hard way:
  the roster signal fires *before* the worker opens its PR, and the `--search`
  variant I originally used lagged the PR by minutes — so on a "left roster, no PR"
  wake, re-poll with `--head` before concluding the worker failed to ship (else you
  open redundant no-op PRs, as I did with #654/#656).
- **Trust-CI is the operator's standing call for this solo repo.** Orchestrator-
  opened PRs go straight to `--auto --squash`; CI (incl. the worker's own tests)
  is the gate. Both #654 and #656 merged within ~3 min of opening.
- **Freshness gate trips between dispatches as origin advances.** After each merge,
  `git fetch origin main && git merge --ff-only origin/main` before the next
  dispatch (the dirty `uv.lock` survives `--ff-only` when there's no overlap). A
  shipped-but-stuck task must be `cw dev-queue cancel <id>`'d to go terminal.
