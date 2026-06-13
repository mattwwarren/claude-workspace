# Handoff: Sprint 0 shipped + v1.1.1 released → 1.1.2 reliability backlog

**Date:** 2026-06-13 (evening). **From:** the Sprint-0 orchestration session.
**Prior handoff:** `2026-06-13-phase4-complete.md` (RFC 0004 Phase 4 + the dogfood
toolkit + dispatch recipe — still authoritative for the dispatch loop).
This doc covers the Sprint-0 hardening wave, the v1.1.1 release, and how to drive
the **1.1.2 reliability backlog** next. The dispatch recipe and toolkit from the
prior handoff are unchanged — re-read its "How to start the next sprint" section;
this doc only adds what's new.

## Shipped — v1.1.1 (released 2026-06-13 21:47 UTC)

main = `54d11b2`; `cw --version` = `1.1.1`; `cw doctor` → "installed 1.1.1 matches
source". GitHub release published, `release.yml` green, **#600 closed**.

- **#611 / PR #632** — CI `package-smoke` job: `uv build --wheel` → `uv tool
  install --no-cache dist/*.whl` → `cw --version` + `cw guide | grep -q
  "orchestrating a sprint"` (asserts the `GUIDE.md` data file is packaged — the
  exact #609 break class). Now gates every PR.
- **#603 / PR #638** — `SESSION_REAP_AUTHORIZED = "session.reap_authorized"`
  audit event emitted from `_reap_session_by_selector` (after the `sessions_lock`
  block), carrying `authority`/`lane`/`proposed_action`. Closes the ADR-0006
  auditability gap under the 4c consumer.
- **#598 / PR #631** — `cw dev-queue status` footer labelled a historical
  snapshot (separate clarifying line; header string left verbatim to preserve the
  `test_cli.py:5202` substring assertion) + runbook "Reading the status output".
- **#597 / PR #634** — worktree fetch-fail log dedup: single-line stderr collapse
  + caller-owned `warned_fetch_fail` set (mirrors `warned_stale`), threaded
  `is_main_behind_origin → _fetch_default_branch`. Returncode path only.
- **#604 / PR #640** — graceful `Ctrl-C` on `cw orchestrate run`: `except
  KeyboardInterrupt → click.echo("orchestrate run: stopped.", err=True); raise
  click.exceptions.Exit(130)`. No cursor flush (idempotent replay handles it).
- **PR #642** — `cw.__version__` single-sourced via `importlib.metadata` (was a
  hardcoded literal that drifted; see Gotchas). `_resolve_version()` is testable;
  both branches covered.
- Plus the **RFC 0005 design doc** (#629) landed on main earlier in the day.

**#438 closed** as fixed-by-#504 (the `save_state` it cited is already under
`sessions_lock`; verified, no code).

## 1.1.2 — reliability backlog (DO THIS NEXT)

All OPEN. These are the dispatch-reliability cluster surfaced this session. They
share one systemic root (see #639). Ship the quick fixes as **v1.1.2**; #639 is a
decision, not a quick fix.

- **#635** (csid backfill) — **root-caused, narrowed, ready to harden+dispatch.**
  `reconcile()` never runs between operator `cw` commands, so a live worker's
  `claude_session_id` isn't backfilled for the whole gap (hours). Narrow fix:
  **backfill at spawn-return** in `spawn_create_impl` (try `_csid_from_transcript`
  before `save_state`; transcript appears ~6s post-spawn). NOT a model/schema
  change — fields already exist. (The original body's `created_at`/`updated_at`/
  `worktree` nulls were a query artifact — corrected on the ticket.)
- **#637** (re-dispatch of shipped ticket — **data-safety, highest severity**) —
  before a reap reverts a RUNNING task to PENDING, **consult world state**: if the
  ticket's PR is MERGED (or branch merged/deleted), mark `completed`, don't
  revert+re-dispatch. Extends #315.
- **#633** (disposition surfacing) — `review_pending_approval` is recorded as
  `completed` (disposition=null), hiding sessions awaiting approval. Map
  non-terminal approval-pending states to a distinct disposition; surface it.
- **#636** (prep-pr permission) — ship-it subagent blocked from `gh pr create` by
  a permission classifier; falls back to orchestrator session. Allowlist it or
  make the fallback the documented path. (prep-pr is a global skill — may route to
  `global-claude`.)
- **#639** (arch: periodic reconcile / potential primary runner) — **ADR FIRST,
  no code yet.** Open an ADR on reconcile cadence + ownership (on-demand vs
  background ticker vs daemon primary-runner). Operator note: *no background
  daemons run today; if a ticker proves reliable it could become the primary
  runner.* The #635/#637 quick fixes ship independently of this ADR.

Suggested 1.1.2 sequencing: harden+dispatch #635, #637, #633, #636 (mostly
independent files — #635 spawn/reconcile, #637 reconcile/doctor, #633 cli/queue,
#636 skill). Then bump 1.1.1→1.1.2, tag. Open the #639 ADR in parallel.

## RFC seams — DEFERRED to a dedicated sprint AFTER 1.1.2

Milestone #7 (`v1.1.x` forward-compat seams), all OPEN. **Do not fold into 1.1.2**
(operator decision this session). Run as their own sprint once 1.1.2 ships:

- **#612 A1** — additive stage data-model + schema bump (v9→v10) + migration.
  **Solo, riskiest single change.** Harden it against a main that ALREADY has the
  1.1.2 #635/#637 fixes (they touch the same `Session`/`reconcile`/`spawn`
  surface) — that's the whole reason for ordering seams after 1.1.2. Verify the
  migration against a real on-disk v9 state before release.
- **#613 A2** (StageExecutor seam) → **#614 A3** (`cw schema`) — sequential; A3
  consumes A1's models + A2's `stage_sentinel_schema`.
- **#615 A4** (`.cw/` exclude, worktree.py) — independent; parallel-safe.

The engine (B*/C*/D1/E*, milestone #8 = v1.2.0) and foreign executors (F*,
milestone #9) remain out of the 1.1.x line.

## Operator state (a fresh session must know)

- **cw runtime current:** main `54d11b2`, installed 1.1.1, `cw doctor` healthy
  (only the env `bypass-disclaimer` advisory). Queue all-terminal (no pending/
  running for claude-workspace). No sessions live.
- `~/.claude-workspace/orchestrator.yaml`: `per_client_max_parallel:
  claude-workspace: 3`; `reap_policy: auto` (deliberate — but see the re-dispatch
  hazard below; `auto` is what re-dispatched shipped #597 this session).
- **Worktree debt:** started at 89 worktrees; removed 29 provably-merged + scratch
  (now ~60). The remaining ~55 are mostly **squash-merged-stale** but invisible to
  `git branch --merged` — see #630. A PR-state-based `cw worktree gc` is the fix;
  until then don't mass-`git worktree remove`.

## Bugs filed this session (8 — all from real signals)

#630 (worktree-gc: squash-merge invisible to `--merged`) · #633 (disposition
surfacing) · #635 (csid backfill, root-caused) · #636 (prep-pr gh-create block) ·
#637 (re-dispatch of shipped — data-safety) · #639 (arch reconcile / primary
runner). Plus #438 closed.

## New gotchas / lessons (carry forward)

- **Two version sources (now fixed):** `cw.__version__` was a hardcoded literal in
  `src/cw/__init__.py`, separate from `pyproject.toml`. A pyproject-only bump left
  `cw --version`/`cw doctor` reporting the old version, and `release.yml`'s
  "Verify version matches tag" step (`uv run python -c "import cw;
  print(cw.__version__)"` vs tag) **correctly failed** the first v1.1.1 build.
  Now single-sourced via `importlib.metadata` — don't reintroduce a literal.
- **The stale-state root (the #1 operational hazard):** `reconcile()` only runs on
  `cw start/resume`, each `dispatch_tick`, `cw doctor`, `cw status/list`. With no
  background ticker, session state is stale for the entire gap between commands —
  **hours** for long workers. Consequences seen this session: csid never
  backfilled (#635); a completed worker's task stayed `running` for ~1h then got
  **reverted + re-dispatched** by the next dispatch tick (#637); stuck-`running`
  tasks needed manual resolution. **Until #639 lands, after each worker finishes:
  resolve its task to terminal immediately** (`cw done <session>` does NOT update
  the dev-queue task — use `cw dev-queue cancel <ticket> -c <client>` to force it
  terminal and prevent re-dispatch under `reap_policy: auto`).
- **#578 ship-but-stuck recurred on most workers:** worker ships PR + emits a real
  `shipped` sentinel, then its turn never completes → task stuck `running`.
  ALWAYS validate via the worker's own sentinel (role-filter assistant/
  tool_result) + the real PR, never the queue status alone (and never the prompt's
  `pr=42` example — #591).
- **Workers took ~1h each** (even "small" tickets, due to fix loops / premise
  verification). The queue-status `Monitor` (1h max timeout) can expire right as a
  worker finishes — and it can't see a ship-but-stuck (no status transition). Add
  PR-existence / transcript-silence to the monitor, or accept the timeout and
  validate manually on wake.
- **harden-per-ticket earned its keep again:** the Plan Reviewer sweeps caught
  #438 already-fixed (no-op dispatch avoided), #635 misspecified (wrong field
  names — would've misled the worker), the `warned_stale` reuse, the GUIDE.md
  smoke assertion, the test-substring trap (#598), the two-`record_event`-
  functions gotcha (#603), and the no-cursor-flush insight (#604). Every dispatched
  ticket shipped first-try-to-spec. **Keep hardening every non-trivial ticket.**
- **Patch-coverage gate (≥90% new lines) bites new `except` branches:** the
  version fix's `except PackageNotFoundError` was uncovered → CI failed. Cover
  every new branch (extract to a testable function if it's import-time code).

## Release-cut recipe (validated this session)

1. Branch off main; bump `pyproject.toml` `version` (the ONLY version source now).
2. Fill `CHANGELOG.md` `## [Unreleased]` → `## [x.y.z] — DATE` (Keep a Changelog;
   Added/Changed/Fixed/Docs).
3. PR → CI (incl. `package-smoke`) → squash-merge.
4. `git tag -a vX.Y.Z <merged-sha>` + `git push origin vX.Y.Z` → `release.yml`
   runs verify (`cw.__version__` == tag) → builds wheel → publishes → closes the
   dispatch-guard release issue.
5. Reinstall: `uv tool install --force --reinstall --no-cache <repo>`; verify with
   `cw --version` AND `cw doctor` (the cw-version line).
6. If the tag was cut at the wrong commit: delete (`git push origin
   :refs/tags/vX.Y.Z` + `git tag -d`) and re-push at the right SHA; no consumers
   if done immediately.
