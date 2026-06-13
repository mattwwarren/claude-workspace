# Handoff: RFC 0004 Phase 4 complete → next sprint

**Date:** 2026-06-13. **From:** the Phase 4 orchestration session. **Prior handoff:**
`2026-06-12-milestone-1.1-complete.md` (still the source for 1.1 / Waves 0–1 / lanes Ph1–3).
This doc covers Phase 4 and how to drive the next sprint with the now-complete dogfood toolkit.

## Shipped (all merged to main)

- **4a #594 / PR #599** — `Session.lane`, state schema v8→v9 + migration, `SessionPurpose.ORCHESTRATE`,
  `spawn_create_impl(purpose=…, lane=…)`. `WORKER_PURPOSES` keeps ORCHESTRATE out of `--purpose`
  choices. Occupancy counting stayed task-join based (the locked trap held).
- **4b #595 / PR #601** — `cw orchestrate start --lane <name>`: **binding-only** ORCHESTRATE session
  (operator decision on the 4b/4c boundary), `--lane` validation (`LaneNotFoundError`),
  one-live-per-(client,lane) reject, `purpose` param on `spawn_create_impl`.
- **4c #596 / PR #602** — `cw orchestrate run --lane <name>`: **cw-side** consumption loop (operator
  chose Option A over a kept-alive claude session). Authorizes reaps via the existing
  `_reap_session_by_selector` (same path as `doctor --reap`; **no new mutation surface**, ADR-0006
  inv 2). Lane-isolation filter + adversarial test; idempotent replay via status-check;
  `compute_drift` phantom guard excluding `purpose==ORCHESTRATE` bindings; salvage logs-and-leaves
  `BLOCKED_ON_USER`.

Parent epic **#583** has the completion summary; close it when convenient.

## Deferred follow-ons (filed, not started)

- **#603** (`bug`) — `_reap_session_by_selector` emits no audit event; destructive reap is unrecorded
  under the automated 4c consumer. Add `record_event` at the authorize site.
- **#604** — `cw orchestrate run` poll loop lacks a graceful `KeyboardInterrupt` handler.
- **#605** — agent-driven **salvage** judgments (continue-session / ship-branch) — the deferred
  Option B kept-alive session.
- Side items from the sprint: **#597** (unreachable-remote spams dispatch ticks), **#598** (docs:
  `dev-queue status` footer is a historical snapshot, not live).

## Operator state (a fresh session must know)

- **cw runtime is current** (main = `18a2842`, 4a+4b+4c). Reinstalled with `--reinstall --no-cache`
  (see the cache gotcha below).
- `~/.claude-workspace/orchestrator.yaml`: `per_client_max_parallel: claude-workspace: 3`
  (drop to 2 for same-subsystem waves); **`reap_policy: auto`** (deliberate opt-out of the
  `signal_only` default — unattended dispatch self-heals).
- Queue is empty; no sessions running; `cw doctor` healthy (only the environmental
  `bypass-disclaimer` advisory; run `claude --dangerously-skip-permissions` once interactively to
  clear it).

## The dogfood toolkit (everything available to drive a sprint)

**Hardening (do this first, per non-trivial ticket):**
- `/harden-ticket` skill — sweeps the ticket spec against real code, surfaces ambiguities + likely
  plan-review MUST_FIX, resolves the technical ones, escalates genuine forks, posts a binding
  **Pre-flight Resolutions** comment the worker reads. This is the single biggest first-try-ship lever.

**Queue dispatch:**
- `cw dev-queue add <tickets…> -c <client> -s large|small [-p PRIORITY] [-t TIMEOUT]` — enqueue.
  `-s large` = more auto-approval / bigger headless budget; the worker still self-scopes.
- `cw dev-queue run --once` — single dispatch tick (spawn pending up to lane/client caps). Drop
  `--once` for a loop. `--use-plan` respects a persisted DispatchPlan; `--max-parallel N` overrides cap.
- `cw dev-queue move <ticket> -c <client> --to-lane <lane>`; `cw dev-queue plan` (orchestrate-plan
  DispatchPlan); `cw dev-queue cancel|remove|clear`.

**Lanes (Ph1–3) + lane authority (Ph4 — NEW):**
- `cw lane …` — declare/list lanes; per-lane concurrency + `reap_policy` (signal_only|auto) via
  `clients.yaml`/`orchestrator.yaml`. `--lane` routes a ticket to a lane on `add`.
- `cw orchestrate start --lane <name>` — **bind** a lane's reap authority (creates the ORCHESTRATE
  binding record; the session self-completes — binding is the record, not a live process).
- `cw orchestrate run --lane <name> [--once]` — run the **cw-side authority loop**: consume
  `SESSION_REAP_PROPOSED` for the lane, authorize clear-cut phantom reaps via the `doctor --reap`
  path. Only meaningful for **`signal_only`** lanes (under `auto`, reconcile already acts — the loop
  is an idempotent no-op there). Requires a binding (`orchestrate start`) first.

**Monitoring & inspection:**
- Event-driven **Monitor** on `~/.local/share/cw/dev_queue.json` — watch a ticket's status
  transitions + >25-min transcript silence; terminal-filtered; exit on completed/cancelled/blocked.
  Beats timed polling.
- `cw status`, `cw doctor [--reap]`, `cw orchestrate status|watch|workers|parent`.
- **Transcript lookup** (canonical): cw `session_id` → `sessions.json` `surface_ref` /
  `claude_session_id` → `~/.claude/projects/<encoded-cwd>/<csid>*.jsonl`. Documented in
  `docs/session-disposition.md`. Do NOT grep the daemon roster by the cw session_id — it keys on
  the daemon short-id (different id-space).
- Skills: `cw-session-watch`, `cw-queue-peek`, `cw-validate-result`, `cw-followup`, `queue-issues`,
  `dev-status`.

## How to start the next sprint (recipe)

1. **Orient.** `git fetch && git reset --hard origin/main`; reinstall cw
   (`uv tool install --force --reinstall --no-cache ~/workspace/projects/claude-workspace`);
   `cw dev-queue status` (expect empty); `cw doctor` (expect healthy). Confirm `branch --show-current`
   is `main` and fetched before reading any ranges.
2. **Scope.** Take the epic/brief; split into sub-tickets if large (note sequential deps). Create the
   GitHub sub-tickets grounded in real code (delegate the code-mapping sweep to a sonnet subagent;
   keep your context for judgment).
3. **Harden each sub-ticket** with `/harden-ticket` → post Pre-flight Resolutions. Escalate only
   genuine product/architecture forks (one batched question with a recommendation); resolve the
   technical ones yourself.
4. **Dispatch.** `cw dev-queue add <id> -c <client> -s large` → `cw dev-queue run --once`.
5. **Watch** with an event-driven Monitor (status transitions + >25-min silence).
6. **On terminal:** read the worker's **own** sentinel (assistant/`tool_result`, never the prompt's
   `pr=42` example), verify the gate, check the PR. Sequential deps: harden N+1 against post-N main
   and reinstall cw before dispatching the next.
7. **Salvage** if a worker dies post-implementation (rate limit / #578): the branch is usually pushed
   — verify the full gate in a clean worktree (`git worktree add /tmp/verify origin/dev/<n>`), then
   `gh pr create` + `gh pr merge --squash --auto`. Pure local compute; no Claude quota.
8. **Clean up:** `cw done <dead-session>`; `git worktree remove <wt> --force` for merged branches;
   `cw dev-queue clear -c <client> -s completed|cancelled`; `cw doctor` to confirm green.

For an **unattended signal_only lane**: `cw orchestrate start --lane X` once, then run
`cw orchestrate run --lane X` (loop) so dead-session reaps self-authorize without an operator.

## Patterns that worked

- **Harden-per-ticket caught real things before code:** the `ORCHESTRATE` `click.Choice` leak (4a),
  the `orchestrate start` vs `orchestrator-start` name collision (4b), and forced the 4b/4c
  architecture decision (binding-only; cw-side loop) up front.
- **Escalate architecture forks with a recommendation; resolve technical things.** Two operator
  decisions this sprint (4b/4c boundary, 4c consumer) reshaped the plan correctly.
- **Salvage-ship recipe** (verify gate locally → PR) recovered both 4b (rate limit) and 4c (#578).
- **Event-driven Monitor**, not timed polling.

## Gotchas (carry forward)

- **uv reinstall cache trap:** `uv tool install --force` serves a STALE cached build when
  `pyproject.toml`'s version string is unchanged (it stayed `1.1.0` across all of Phase 4).
  `cw --version` and `doctor` report green because they compare the version string, not the build.
  Use `--reinstall --no-cache` and verify by invoking the new subcommand's `--help`. This is the
  runtime cause of the recurring "cw binary is stale" hazard.
- **#578 turn-never-completes-after-sentinel:** a worker can ship the PR + emit a real `shipped`
  sentinel, then its turn never completes → the dev-queue task is stuck `running` and never routes.
  Detect via 25-min silence + a role-filtered worker sentinel + a real merged PR; salvage/clean up.
- **#591 sentinel example:** the `/auto-dev` prompt embeds an illustrative `pr=42` sentinel. Any
  monitor/parser reading raw transcript text will latch it as `shipped`. Always role-filter to the
  worker's own assistant/`tool_result` output.
- **Diverged main / worktree confusion** at session start: an unpushed local release commit + a
  worktree-vs-checkout mixup looked like a divergence. Always `branch --show-current` + `fetch`
  before reading ranges; reset local main to origin once the operator confirms.
- **reap_policy: auto** on claude-workspace → reconcile self-heals phantom RUNNING; ORCHESTRATE
  binding sessions are now excluded from phantom-reap (4c, `compute_drift`).
- **Rate limits** kill a worker mid-run (branch pushed, no PR). Salvage-ship needs no Claude quota.
