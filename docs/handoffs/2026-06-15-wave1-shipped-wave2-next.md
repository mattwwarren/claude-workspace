# Handoff: milestone #8 Wave 1 shipped (B1 + D1) → Wave 2 (B2 engine) next

**Date:** 2026-06-15. **From:** the RFC 0005 milestone-#8 orchestration session (engine sprint, Wave 1).
**Prior handoff:** `2026-06-14-rfc0005-seams-shipped.md` (milestone #7 seams + the dispatch recipe + the well-behaved-orchestrator playbook — still authoritative; read it).

## Shipped this session — Wave 1 of milestone #8 (v1.2.0 staged pipeline)

main = `c9fbfe6`, clean, level with origin. `cw` installed = 1.1.3.

- **#616 B1 / PR #659 — RFC 0005 B1 decompose `/auto-dev` into per-stage entrypoints.** Option C
  (operator decision): 5 new `.claude/commands/` files — a shared `auto-dev-intake.md` (sole home of
  ticket fetch + tracker resolution + scope orientation → `.cw/context.json`) and 4 self-contained,
  idempotent per-stage files (`auto-dev-plan.md`, `-impl.md`, `-review.md`, `-finalize.md`) that orient
  via intake, read predecessor output from `.cw/`, run their stage body, write their output to `.cw/`.
  `auto-dev.md` rewritten (−1216 lines) to chain `intake → plan → impl → review → finalize` by prose
  delegation (no duplication). Markdown-only, zero Python. These are now live as skills
  (`auto-dev-intake/plan/impl/review/finalize` appear in the skill list). **One MUST_FIX caught in
  review + fixed before merge** (`e1c734d`): the decomposition had dropped #658's `Checkpoint 3a`
  adjudication + `Step H3` harvest (a parity regression — #658 landed mid-session); ported into
  `auto-dev-review.md` + `auto-dev-finalize.md` (deferred findings handed off via
  `.cw/deferred-findings.md`) + `Step H3` restored in `auto-dev.md` orchestration.
- **#624 D1 / PR #660 — RFC 0005 D1 `cw board` live TUI.** `src/cw/board.py` (`BoardState` +
  pure `render_board` + `run_board` Live loop), `cw board` CLI (`--once`/`--interval`/`--client`),
  `tests/test_board.py` + `test_cli.py`. CI green. Honored all resolutions (effective-config loaders,
  `—` placeholders + no-crash, derive model/backend from `client.pipeline.executors`, lane-stat mirror,
  footer formula). **D1 shipped via the WORKER's own PR #660** (see the redundant-PR lesson below).
- **#662 filed** — fast-follow tech-debt from the D1 review (3 SHOULD_FIX, no blockers): S1 board layout
  nested-vs-flat (needs a design call — identical for single-lane, differs multi-lane), S2 remove dead
  `_STAGE_COLUMNS`, S3 add a multi-lane test. Small; `board.py` + `test_board.py` only.

## THE answer on timeouts (operator asked) — and why it's the RFC's whole point

Both Wave-1 workers **completed plan + impl and pushed their branch**, then got reaped at the
review/PR step. **Two different reapers, two different knobs:**

- **#616 B1 → wall-clock budget** (~56 min total). The wall-clock budget is the headless timeout
  (scope-derived). Bump per-ticket via `cw dev-queue add -t <seconds>`.
- **#624 D1 → idle watchdog.** `orchestrator.yaml: idle_watchdog_seconds: 1800` (30 min). The review
  ran a long subagent with no parent activity for 31 min → reaped. (D1's worker had *already* opened
  PR #660 and merged it at 03:02 — the idle reap fired during the post-merge wait, not before shipping.)

**Recommendation for the remaining Wave-2 tickets** (B2/B3/B4/C* are comparable or larger):
1. Dispatch with an explicit larger wall-clock: `cw dev-queue add <id> -t 7200` (120 min) for the big ones.
2. Be aware the 1800s idle watchdog can still bite a long single review/impl subagent. No per-ticket idle
   override is exposed on `cw dev-queue add` (only `-t`); raising the global `idle_watchdog_seconds`
   trades faster real-stall detection. **Cheaper than fighting it: the salvage-from-pushed-branch pattern
   is reliable** — the worker commits+pushes impl *before* the review timeout, so a timed-out big ticket
   is recovered by: confirm the pushed branch (`git ls-remote --heads origin '*<id>*'`), open a PR from
   it, review, merge. Did this cleanly for B1.
3. **The real fix is the sprint itself.** Once #617 B2 wires per-stage spawning, each stage is a
   short-lived session — no single 60-min monolith run, no wall-clock/idle reap at the review step. B1
   literally proved the premise by blowing the monolith budget.

## Wave 2 — what's next (dependency order, all of milestone #8 that's left)

`#617 B2 → #618 B3 → #619 B4` (Phase-1 exit bar), then `#620-623 C*`, `#624-done`, `#625-626 E*`,
plus `#662` (D1 follow-up, any time).

**Dispatch #617 B2 next — it is now UNBLOCKED (B1 merged).** B2 = generalize
`consume_completed_sessions` (`dispatch.py:817`): success+remaining → advance stage + reset PENDING;
pause → BLOCKED_ON_USER; failure → BLOCKED_ON_USER; terminal → COMPLETED. Resolve executor by stage at
claim/spawn; record `stage_base_ref = HEAD`. Also owns the `executor.py:69` prompt-stub fix (change
`f"auto-dev stage {stage} for ticket {id}"` → invoke the B1 entrypoints, e.g. `/auto-dev-<stage> <id>
--headless`). Depends A1+A2 (merged) + B1 (merged). This is a real `dispatch.py`/`executor.py` change —
**harden it before dispatch** (Plan Reviewer sweep vs current main, scope fence, post Pre-flight
Resolutions). B3, C*, D1-follow-up all gate behind B2 merging.

**Pre-flight hardening reminders that paid off / cost us this sprint:**
- Run the read-only Plan Reviewer sweep against *current main*, resolve technical findings yourself,
  escalate only genuine product/scope forks (one batched `AskUserQuestion`), post one
  `## Pre-flight Resolutions` comment with a hard scope fence. **This works** — B1's worker landed Option C
  exactly. But workers still surface 1-2 *new* questions in planning (B1 asked 4 good ones, D1 asked 3);
  disposition them the same way and append a follow-up resolution.
- **Bake the v2 plan-format `## Touch-point Contract` requirement into every Wave-2 pre-flight comment.**
  D1 bounced `plan_unreviewable` purely because its plan lacked a `## Touch-point Contract` section
  (file:line quotes for each call site it attaches to) — a v2 plan-reviewer requirement, not a substance
  gap. Every B/C/E ticket attaches to existing call sites, so pre-instruct it and skip the bounce.

## Lessons (carry forward — the sharp ones)

- **I REPEATED the #654/#656 redundant-PR mistake. Read this before any salvage.** D1's worker opened
  its own PR #660 (03:01) which auto-merged (03:02). Hours later I misread a `needs_attention` + parked
  task + a *stale* "no PR" check as "stalled before PR," ran a redundant salvage, and opened **PR #661 —
  which merged EMPTY** (zero file changes). Root cause: I ran `gh pr list --head dev/624-cw-board` *hours
  before* opening, not immediately before. **Rule: on any "worker left roster / needs_attention / no PR"
  signal, run `gh pr list --head dev/<id>` IMMEDIATELY before concluding it didn't ship, and look for the
  WORKER's PR (any author, recent timestamp), not just your own.** A `needs_attention` fires *after* the
  worker opens its PR (post-merge idle), so it is NOT proof of failure. Net damage this time: zero (no
  content dup on main), but it cost a wasted salvage cycle + an empty merge.
- **`cw dev-queue add` on a blocked task APPENDS a duplicate, it does not reset.** To re-dispatch a
  `blocked_on_user` task: `cw dev-queue remove <id> -c <client> --all` then `add` then tick. A `pending`
  (reverted) task just needs a tick. (Captured to wiki auto-memory: `cw-redispatch-blocked-task`.)
- **The salvage-from-pushed-branch pattern is reliable for timed-out big tickets** (used for B1). Worker
  pushes impl before the review reap; open PR from the branch, review, merge. For markdown tickets CI is
  trivial so the review IS the gate; for Python tickets (D1) CI is a real gate.
- **Scope/budget:** I dispatched B1 as `scope=small` — too small for Option C; it timed out. Size the
  scope/`-t` to the actual work (see timeout section).

## Operator state (a fresh session must know)

- **main `c9fbfe6`, clean, level with origin.** Orchestrator checkout was hard-reset to origin/main this
  session (it had accumulated redundant staged D1 files from the salvage churn — all verified identical
  to origin before reset). `cw` = 1.1.3. `orchestrator.yaml`: `per_client_ceiling claude-workspace: 3`,
  `reap_policy: auto`, `idle_watchdog_seconds: 1800`.
- **dev-queue:** claude-workspace clean — 616/624 cancelled (terminal), 0 pending/running/blocked. No live
  sessions, no monitors running. Other clients (life-platform, ai-home-lab, global-claude) carry their own
  residual rows — the global `cw dev-queue run` tick spawns across ALL clients, so it incidentally spawned
  life-platform/2,3 this session; untouched otherwise.
- **Leftover cleanup (non-blocking):** the two merged branches `dev/616-per-stage-entrypoints` and
  `dev/624-cw-board` are still on origin — remote deletion was soft-blocked by the auto-mode classifier
  this session; delete them when convenient (`git push origin --delete ...`). Their worktrees under
  `~/.cw/wt/` + the agent worktrees linger (#630 worktree-gc backlog — do NOT mass-remove).
- **Open in milestone #8:** #617,618,619 (B2-B4), #620-623 (C1-C4), #625-626 (E1-E2), #662 (D1 follow-up).
  #616, #624 CLOSED.

## Being a well-behaved orchestrator — carry the prior playbook

The `2026-06-14-rfc0005-seams-shipped.md` playbook (harden-before-dispatch; respect the dependency graph
+ `git fetch && merge --ff-only` between dispatches; verify world-state via the real PR with
`--head` NOT `--search`; surface async results before acting; gate outward actions on the operator;
clean up carefully; hand off honestly; correct the record when wrong) held up — and item #4 (the `--head`
discipline) is exactly what I violated with #661. Re-read it; it is still the operating manual.
