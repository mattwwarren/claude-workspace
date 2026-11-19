# RFC 0020 — Epic delivery on a base branch: build the feature, land it once

## Summary

cw ships one ticket as one PR into the client's default branch. An epic therefore reaches the default branch as a drip: each child merges on its own, a half-built feature sits on the default branch for days, and every merge that needs a human costs that human one context switch without the whole picture.

This RFC separates the build unit from the merge unit. A dev-queue row gains an optional `base_branch`. Rows that share a base are an epic: each one runs the unchanged plan, impl, review and finalize contract on whatever backend its lane names, but its worktree is cut from the base, its diff and freshness checks compare against the base, and its PR targets and auto-merges into the base. When the feature works end to end, one PR from the epic branch to the default branch is adopted through the existing RFC 0016 primitive. That landing row has no `base_branch`, so it gets a whole-feature review against the default branch and one human approval.

cw gains no epic entity, no new task status, no new park disposition and no work on the dispatch tick. An epic is a value rows share plus a landing procedure the orchestration skills run. RFC 0015's swing (one worker, one worktree, one PR) is untouched and orthogonal: a swing can be one row inside an epic. This is the "later RFC" that RFC 0015 D-B2 deferred, and it answers that deferral by rejecting the stack.

## Motivation

- **The drip is the only shape available.** `Client.default_branch` (`src/cw/models/client.py:549`) is the only merge target cw knows. A row can record its own feature branch (`src/cw/models/tasks/ticket_task.py:67`, resolved by `resolve_feature_branch`, `src/cw/models/branch.py:29`), but nothing lets a row say where that branch merges.
- **The base has no seam.** There are 94 direct `.default_branch` reads across about 40 modules in `src/cw` (heaviest: `src/cw/worktree/_freshness.py`, `src/cw/review_convergence/_diff_base.py`, `src/cw/worktree/_scope.py`). Stage prose hard-codes the name as well: `.claude/commands/ship-it.md:67,77,153` use a literal `origin/main` although `--base` is parsed at line 21, and `.claude/commands/auto-dev-finalize.md:607` runs `cw worktree merge-probe --base main`.
- **`default_branch` carries two meanings.** It is the merge target (where PRs go, what diffs and freshness compare against) and it is the trust root: RFC 0017 reads the repo review engine from the default branch so a PR cannot edit its own reviewer (`config/CONFIG_REFERENCE.md:147`). On an epic branch, agent-merged code sits at the merge target with no human review. If the engine were read from there, the first ticket could weaken the reviewer for every later ticket. The two meanings have to be split before a second merge target can exist.
- **Size is no longer the constraint.** Scope tier is a risk classification since v4 (`a92468d0f`, RFC 0015 D-A4) and line-level review is done by agents. What a human approves at landing is a feature: the RFC they signed, the attested review, the working result.
- **The pieces already ship.** RFC 0016 adoption creates a normal row for an existing PR with an explicit branch and a pinned head SHA, and parks when the head moves (`src/cw/adopt_pr.py:417`). Its repair routing (B1 #2944, B2 #2945, both closed) sends a conflicted or red PR to impl with synthesized feedback. ADR-0022 already says when a base is merged into a branch. ADR-0012 already keeps the approval human.
- **RFC 0018 makes the per-ticket stage contract the unit every backend honors.** A delivery mode that changes only the base ref rides that contract on codex and claude-native alike. A delivery mode with its own row type and child tracking would be a second contract to keep at parity.

## Design

Two epics. The first makes the base a row-level value behind one seam and separates merge target from trust root. The second is the thin layer that lets an operator build on a base and land it.

### Epic I — The base is a row value behind one seam

`TicketTask.base_branch` is optional; null means the client's default branch, so every existing row behaves as it does today. One function, `resolve_base_branch(client, task)`, sits beside `resolve_feature_branch` and is the only way code learns a row's merge target. Every `.default_branch` read in `src/cw` is classified by grep as merge target (routed through the seam) or trust root (left on `client.default_branch`, with a recorded reason), and a static test fails on an unclassified read. The resolved base reaches workers through `.claude/cw-context.json`, the stage prose stops naming `main`, and the new contract elements enter the RFC 0018 manifest with a disposition per backend. An ADR records the split.

### Epic II — Build on a base, land once

`cw dev-queue add --base <branch>` stamps the base on the row and refuses a base that does not exist on origin. An optional per-client size ceiling is checked there and nowhere else. A read verb lists the rows of one base. Adoption learns to check a PR's base against the row's, which is what makes the landing row safe: the PR from the epic branch to the default branch is adopted under the epic's parent ticket with no `base_branch`, reviewed whole against the trust root, and parked if the epic branch moves underneath it. The orchestration skills gain the two procedures (open an epic, land an epic) and the vocabulary. `cw doctor` reports each live base's size, age and mergeability as advisories. A dogfood run proves the arc on claude-native and codex lanes.

## Phasing

Human-facing summary, not parsed; `## Tickets` is the parse source.

- Wave 0, Sprint 1: S1 — row field, seam, ADR.
- Wave 1, Sprint 2: S2 — sweep of worktree, spawn, dispatch and reconcile readers. A1 — context delivery, stage prose, contract manifest.
- Wave 2, Sprint 3: S3 — sweep of review, convergence, codex and stage-verification readers, plus the static guard. B1 — enqueue on a base. B2 — adoption checks the base; landing row.
- Wave 3, Sprint 4: B3 — orchestration skills. B4 — doctor advisories. Both are built on `epic/3778` as the first real epic (D-13), with one further child on a codex lane.
- Wave 4, Sprint 5: D1 — land that epic through adoption and record the run.

Sprints 1 to 3 merge to the default branch as ordinary waves: a row with a base is not safe before S3, because until then review still diffs against the default branch and the trust-root guard does not exist.

## Resolved decisions

Firm leans, decided to unblock the breakout. Each is binding but reversible: reversible if the code contradicts it, at the named reversal point.

- **D-1 — An epic is rows that share a base.** Tickets inside an epic are ordinary dev-queue rows on the unchanged stage contract. There is no epic row type, no child tracking in cw, no new `TicketStatus` and no new park disposition. RFC 0015's swing is orthogonal and unchanged. Reverses never inside this RFC.
- **D-2 — The merge target is `TicketTask.base_branch`, resolved at one seam.** Null means `client.default_branch`. `resolve_base_branch(client, task)` in `src/cw/models/branch.py` is the single reader; a caller that has the row passes it. The value is stamped at enqueue, is immutable once the row has had a session, and does not change when the row moves between lanes. Reverses at S1 hardening.
- **D-3 — Merge target and trust root are different things.** Anything that exists so a branch cannot alter its own judge (the repo review engine source, its guard set and manifest, review attestation rules, review configuration read from git) stays on `client.default_branch` regardless of the row's base. Everything that asks "what does this branch merge into or diverge from" goes through the seam. The classification of each read is established by grep and recorded, per ARCHITECTURE §7.11. Reverses at S2 or S3 hardening for an individual read, never for the principle.
- **D-4 — One landing PR, adopted.** The epic lands as a single PR from the epic branch to the default branch, entered into the pipeline with `cw dev-queue add <parent> --adopt-pr <N>` and no `--base`. It merges by the pipeline's existing method. A replayed stack of per-ticket PRs is rejected. A merge-commit option for the landing row is deferred to a later ticket outside this RFC. Reverses never inside this RFC.
- **D-5 — The size ceiling is optional and checked only at enqueue.** `epic.max_diff_lines` on the client is unset by default; unset means no refusal. When set, `cw dev-queue add --base` measures the base against the default branch once and refuses over the ceiling, naming the remedy (land now, open a second epic). There is no dispatch-tick gate. Size and age are otherwise advisories; age can never be more than one (ADR-0014). Reverses at B1 hardening.
- **D-6 — cw creates and deletes no epic branch.** The orchestration skill creates the branch from the default branch and pushes it; cw only verifies it exists on origin. There is no `cw epic` command group. Reverses at B3 hardening if the skill-layer procedure proves unreliable.
- **D-7 — The base reaches workers through the context file, and prose names no branch.** `.claude/cw-context.json` carries the resolved base; every literal `main` / `origin/main` used as a merge target in the stage and ship prose reads it instead. New contract elements get RFC 0018 manifest ids and per-backend dispositions: `implemented` for claude-native and codex, `fails-closed` for opencode and local (RFC 0018 D-4). Reverses at A1 hardening.
- **D-8 — No scheduled sync of the default branch into an epic branch.** ADR-0022 applies to each ticket against its own base unchanged. Divergence between the epic branch and the default branch surfaces at landing, where adoption's non-green path (RFC 0016 Epic II) routes a conflicted PR to impl, and earlier as a doctor advisory. Reverses at D1 if the dogfood shows landing conflicts are routinely too large to repair in one impl turn.
- **D-9 — No client-level epic mode.** A row is in an epic because it carries a base. Which work is built as an epic is decided per RFC by the orchestrator. A per-client default is out of scope. Reverses never inside this RFC.
- **D-10 — A child is done when it is on the default branch.** A per-ticket PR merged into an epic branch does not close its tracker ticket (GitHub's `Closes` only fires on the default branch, and nothing in cw compensates). The landing PR body cites every child, so the landing merge closes them. The dev-queue row still reaches `COMPLETED` when its own PR merges into the base. Reverses at B2 hardening for trackers where a body citation does not close a ticket.
- **D-11 — This RFC adds no reviewer sharding.** A landing diff too large for the Claude review path fails loud as it does today (`ReviewInputTooLargeError`). The documented route for a large landing is a codex review lane, which shards. RFC 0015 B1 remains the claude-native fix and is a pull-in candidate, not a dependency. Reverses at D1.
- **D-12 — Branch protection on epic branches is the operator's.** An epic branch must not require a human review for per-ticket PRs, or every child knocks. cw documents the prerequisite and reports it; it never changes repository settings and never approves (ADR-0012). Reverses never.

- **D-13 — Sprint 4 is the first epic; D1 lands it.** (Operator decision, 2026-11-19, after buildout.) B3 and B4 are enqueued with `--base epic/3778` once S3, A1, B1 and B2 are on the default branch, together with one further child that runs on a codex lane, chosen by the orchestrator at sprint-4 dispatch from open tickets that touch neither B3's nor B4's files. The orchestrator opens and lands the epic by hand, following the procedure B3 is writing, and D1 is the landing plus its record. This replaces the throwaway dogfood epic. Reverses at sprint-4 dispatch if no suitable third child exists, in which case B4 runs on the codex lane and the run proceeds with two children, recorded as such.

## Tickets

### S1 — Row base field, `resolve_base_branch` seam, and the merge-target/trust-root ADR

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** A row records its feature branch but not where it merges; every reader takes `client.default_branch` directly. This ticket adds the field and the seam every other ticket reads, and records the decision that merge target and trust root are separate.
- **Scope:** D-1, D-2, D-3
- **Acceptance:**
  - `TicketTask.base_branch: str | None = None` lands with a dev-queue schema bump and a migration under which every existing row reads as null; the value passes the same charset check a row `branch` does.
  - `resolve_base_branch(client, task)` in `src/cw/models/branch.py` returns the row's base when set and `client.default_branch` otherwise, with tests for null row, set row, null task and null client.
  - A set `base_branch` is refused for change once the row has a `session_id` history; `cw dev-queue move` leaves it untouched; `cw dev-queue show` and `tasks --json` print it.
  - A new ADR states that `default_branch` is the trust root, `base_branch` is the merge target, and lists the classes of read that stay on the trust root; `ARCHITECTURE.md` §6 and the reference table cite it without renumbering §7 or §8.
  - No reader other than the seam's own tests is changed in this ticket.

### S2 — Sweep worktree, spawn, dispatch and reconcile readers onto the seam

- **Epic:** I
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S1
- **Context:** Worktree creation, the spawn-time world-state snapshot, the freshness and merge-probe gates and the reconcile recipes all read `client.default_branch` as the merge target. A row with a base must be cut from, compared against and repaired against that base.
- **Scope:** D-2, D-3
- **Acceptance:**
  - Every `.default_branch` read under `src/cw/worktree/`, `src/cw/spawn.py`, `src/cw/dispatch/`, `src/cw/reconcile/`, `src/cw/branch_ahead.py` and `src/cw/merge_probe` is enumerated by grep in the PR description and classified merge target or trust root with a one-line reason.
  - Merge-target reads go through `resolve_base_branch` with the row passed wherever a row is in scope; a call site with no row in scope is listed with why the client default is correct there.
  - A worktree for a row with a base is created from `origin/<base>`, and the spawn snapshot's recorded base ref names that base.
  - The `branch_behind_main` gate, the review-staleness gate and the conflict-repair recipes probe and merge the row's base; ADR-0022's rule and its lockless-prefetch bounds (§7.16) are unchanged, shown by the existing tests passing plus one base-row case per gate.
  - A row with null base produces byte-identical git argv to today in the existing tests.

### A1 — Deliver the base to workers; stage prose and the contract manifest stop naming `main`

- **Epic:** I
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S1
- **Context:** Headless workers learn client settings from `.claude/cw-context.json`, and the stage and ship prose names `main` literally in several places. The contract manifest from RFC 0018 must record the new element for every backend so a backend cannot silently ignore a base.
- **Scope:** D-7
- **Acceptance:**
  - The context writer stamps the resolved base for the row with a `CW_CONTEXT_SCHEMA_VERSION` bump; `cw schema show` documents the key; a context without the key reads as the client default.
  - Every literal `main` or `origin/main` used as a merge target in `.claude/commands/auto-dev-*.md`, `prep-pr.md` and `ship-it.md` is enumerated by grep and replaced by the delivered base; `/prep-pr` and `/ship-it` receive it as `--base`; a markdown guard test fails on a reintroduced literal.
  - Finalize's open-PR inventory and overlap filter consider only PRs whose base equals the row's base.
  - The new contract elements carry manifest ids with dispositions `implemented` for claude-native and codex and `fails-closed` for opencode and local, and the manifest drift test passes.
  - An opencode or local row with a non-null base is refused before any agent runs, with a named reason, and `cw doctor` reports a lane that would hit it.

### S3 — Sweep review, convergence, codex and stage-verification readers; static guard

- **Epic:** I
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** S2
- **Context:** The review diff base, merge tolerance, tip rule, codex stage executors and stage verification read `client.default_branch` as merge target, while the review engine and attestation read it as trust root. This is where the two meanings sit side by side, so the classification here is the one that closes the self-review hole.
- **Scope:** D-2, D-3
- **Acceptance:**
  - Every remaining `.default_branch` read in `src/cw` is enumerated by grep in the PR description and classified with a one-line reason; none is left unclassified.
  - Review diff base, merge tolerance, the clean-mode tip rule, `delta-confirm` and the codex plan, impl, review and finalize executors compute against the row's base.
  - The review engine materialize, guard-set preflight and manifest source, attestation rules and any review configuration read from git stay on `client.default_branch`; a test shows a row with a base whose base branch carries a modified engine still reviews with the default branch's engine.
  - A static test fails on any `.default_branch` attribute read in `src/cw` outside the seam and an explicit trust-root allowlist whose every entry carries a reason.
  - The engine-modified park fires for a base row whose diff touches the engine exactly as it does for a default-branch row.

### B1 — Enqueue on a base: `--base`, origin check, optional size ceiling, base listing

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** S1
- **Context:** Nothing lets an operator put a row on a base. `cw dev-queue add` is also the one natural checkpoint for an epic's size, because epics are enqueued in waves.
- **Scope:** D-1, D-5, D-6
- **Acceptance:**
  - `cw dev-queue add <T> --base <branch>` stamps `base_branch`, refuses a branch absent on origin with a message naming the fix, and refuses a base equal to the client's default branch as redundant.
  - `epic.max_diff_lines` is an optional client key, unset by default and documented in `config/CONFIG_REFERENCE.md`; when set, `add --base` measures the base against the default branch once, honors `merge_gate_ignore_paths`, and refuses over the ceiling naming both remedies; when unset no measurement runs.
  - The measurement runs outside every state lock (ADR-0019) and nothing in the dispatch tick reads the ceiling.
  - `cw dev-queue tasks --base <branch>` (with `--json`) lists the rows of one base with status and PR; it is a pure read.
  - `--base` combined with `--adopt-pr` is accepted and handled by B2's check; until B2 lands it is refused with a named reason.

### B2 — Adoption checks the PR's base; the landing row

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** S1
- **Context:** Adoption reads a PR's head but never its base, so a PR could be adopted under a row whose merge target differs from where the PR actually merges. The landing PR is an adopted row with no base, and its correctness rests on that check and on the pinned-head park.
- **Scope:** D-4, D-8, D-10
- **Acceptance:**
  - Adoption reads the PR's `baseRefName` and refuses when it differs from the row's resolved base, with a reason naming both; the check takes its place in the existing refusal precedence in `src/cw/adopt_pr.py`.
  - A PR from an epic branch to the default branch adopts under the parent ticket with null base, enters at review when green and at impl with synthesized feedback when conflicted or red, with no new code path beyond the base check.
  - A push to the epic branch after adoption parks the landing row through the existing pinned-head rule; a test covers a child PR merging into the epic branch mid-landing.
  - `/prep-pr` and `/ship-it` under a landing row write one citation line per child into the PR body from a supplied list, in the tracker's closing form; a child that cannot be cited is a `friction_highlights` entry, not a failed ship.
  - The landing review reads the engine from the default branch and its diff is the whole epic against the default branch, shown by a test.

### B3 — Orchestration skills: open an epic, land an epic, vocabulary

- **Epic:** II
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** B1, B2
- **Context:** The procedures that make rows-sharing-a-base into a delivery mode live at the skill layer by design. The orchestrator needs to know when to reach for an epic, how to open one and how to land one.
- **Scope:** D-1, D-4, D-6, D-9, D-10, D-12, D-13
- **Acceptance:**
  - `cw-orchestration` defines *epic* beside *fan-out* and *swing*: a feature that should reach the default branch once, whose children can each stand as a ticket; it states that a swing may be a row inside an epic.
  - `orchestrate-sprint` gains "open an epic": create `epic/<parent-id>` from the default branch, push it, verify the branch-protection prerequisite, and enqueue children with `--base`.
  - `orchestrate-sprint` gains "land an epic": confirm every row of the base is terminal via `cw dev-queue tasks --base`, open the PR to the default branch with the design summary and child citations, adopt it under the parent ticket, and hand the human approval to `/review-monitor` as a knock.
  - `cw-followup` and `queue-issues` recognize a base row and do not treat a merged child PR as shipped to the default branch.
  - `docs/dispatch-runbook.md` carries the operator procedure, including deleting the epic branch after landing as an operator action.

### B4 — Doctor advisories for live bases

- **Epic:** II
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** B1
- **Context:** With no tick gate, an operator needs to see an epic that is growing, aging or drifting from the default branch before the landing PR shows it.
- **Scope:** D-5, D-8, D-12, D-13
- **Acceptance:**
  - `cw doctor` reports one advisory `epic-base/<client>/<branch>` per distinct non-null base on a non-terminal row, with diff size against the default branch, age of the oldest unlanded commit and the merge-probe result against the default branch.
  - The check is capped per run, bounded in wall-clock time and fails open to "unknown" on a git error; it never fails `cw doctor` and never mutates anything (ADR-0014).
  - When `epic.max_diff_lines` is set the advisory states whether the base is over it.
  - The check reports a base whose child PRs are waiting on a required review as a likely branch-protection misconfiguration, reading only already-recorded row state.
  - `docs/INSTALL.md`'s doctor section lists the check.

### D1 — Dogfood: one epic end to end on claude-native and codex lanes

- **Epic:** II
- **Wave:** 4
- **Sprint:** 5
- **Depends on:** S3, A1, B3, B4
- **Context:** The design claims the unchanged contract carries an epic on any backend. That is only shown by running one, and sprint 4 is run as that epic (D-13): this ticket is the landing of `epic/3778` and the record of the run. It is run by the orchestrator session, not dispatched to a worker.
- **Scope:** D-8, D-11, D-13
- **Acceptance:**
  - The sprint-4 epic on `epic/3778` (B3, B4 and one further child, at least one of the three on a codex lane) is landed on the default branch through adoption under the Epic II parent ticket; the run is recorded in `docs/` with the commands used, including how the epic was opened.
  - The record states the size of the landing diff, whether the landing review needed a codex lane (D-11) and whether a landing conflict was repaired in one impl turn (D-8).
  - Every friction found is filed as a ticket; D-8 and D-11 are either confirmed in the record or amended in an Errata section of this RFC.
  - No child ticket was closed before the landing merge and every child was closed by it.
  - Every place where the hand-run procedure differed from B3's written procedure is fixed in the skill prose before the landing PR merges.

## References

- `src/cw/models/client.py:549` — `default_branch`, the only merge target today and, after this RFC, the trust root.
- `src/cw/models/branch.py:29` — `resolve_feature_branch`, the seam pattern `resolve_base_branch` copies.
- `src/cw/models/tasks/ticket_task.py:67` — the row's own `branch` (schema v49, #2948), the precedent for a row-level branch value.
- `src/cw/models/tasks/schema.py:291` — `DEV_QUEUE_SCHEMA_VERSION = 63`; S1 bumps it.
- `src/cw/context_schema.py:127` — `CW_CONTEXT_SCHEMA_VERSION = 22`; A1 bumps it.
- `src/cw/spawn.py:743` — spawn-time `rev-parse origin/<default_branch>` and the `origin_main_branch` snapshot key at line 768.
- `src/cw/worktree/_lifecycle.py:112` — the worktree is cut from `origin/<client.default_branch>`.
- `src/cw/worktree/_freshness.py` — 33 `default_branch` references, the densest merge-target reader.
- `src/cw/review_convergence/_diff_base.py:93` — `resolve_default_branch_ref`, the review diff base.
- `src/cw/review_engine/manifest_source.py` — engine read from the default branch; a trust-root read.
- `src/cw/codex_review/_context/_default_branch_config.py` — review configuration read from the default branch; classification decided in S3.
- `src/cw/adopt_pr.py:122` — fork refusal in the adoption precedence; `read_adopted_pr` at 354 and `build_adopted_task` at 417 never read the PR's base.
- `src/cw/cli/dev_queue/crud.py:162` — `--adopt-pr` on `cw dev-queue add`; `--base` lands beside it.
- `src/cw/codex_review/_shard.py:56` — `ReviewInputTooLargeError`; the codex path shards.
- `src/cw/claude_review/core.py:115` — "The Claude path has no sharding"; an oversized landing review fails loud here.
- `src/cw/contract_manifest/` — RFC 0018 manifest; A1 adds elements.
- `src/cw/doctor/merge_gate.py` — an advisory doctor check in the shape B4 follows.
- `.claude/commands/ship-it.md:21` — `--base` is parsed; lines 67, 77 and 153 use a literal `origin/main` anyway; lines 113-126 build the `Closes #<n>` trailer; line 269 arms squash auto-merge.
- `.claude/commands/auto-dev-finalize.md:607` — `cw worktree merge-probe --base main`; lines 225 and 327 inventory open PRs with no base filter.
- `.claude/commands/prep-pr.md:69` — `--base` defaults to `main`.
- `config/CONFIG_REFERENCE.md:147` — "The skill is read from the default branch", the RFC 0017 guarantee D-3 preserves.
- `docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md` — applies per ticket against its base.
- `docs/adr/0014-timers-never-destroy-work.md` — why age is only ever an advisory.
- `docs/adr/0012-cw-never-grants-github-review-approvals.md` — the landing approval is human.
- `docs/rfcs/0015-epic-intake-one-plan-many-tickets.md` — the swing; D-B2 deferred stacked PRs to a later RFC.
- `docs/rfcs/0016-adopt-an-open-pr.md` — adoption and repair routing, the landing mechanism.

## Handoff Brief

**Authored:** 2026-11-19 at cf4c2d73d
**Exit:** 1 sprint block

### Decisions

Recorded in `## Resolved decisions` above as D-1 through D-12, with their reversal points. The operator chose D-1 (epic-as-base), D-2 (row field), D-4 (single adopted landing PR) and D-5 (enqueue-time check) directly in the design dialogue, and added that size no longer matters, which is why D-5's ceiling is optional and unset by default. D-3 and D-6 through D-12 are technical calls presented to the operator in the consolidated design and not vetoed.

### Rejected approaches

- **Swing-first with stack splitting** (build RFC 0015, then split the one branch into a stack at finalize) — one worker context holds the whole feature, there is no parallelism, and splitting a finished diff into individually green PRs has no existing mechanism.
- **Both shapes with a shared landing** — two build shapes to keep at backend parity and a selection rule for the orchestrator.
- **Base on the lane** — every epic becomes a `clients.yaml` write, `cw dev-queue move` would silently retarget an in-flight row, and lane lifecycle becomes epic state under another name.
- **Epic as a derived client** (clone the client entry with a different `default_branch`) — no reader changes, but the trust root moves with the merge target, so a ticket could weaken the review engine for its siblings; it also splits one repo's ceiling in two.
- **Replayed stack of per-ticket PRs onto the default branch** — N human approvals, every PR re-cut after its parent squash-merges (tripping adoption's pinned-head park each time), conflict merges and cross-ticket fixups that belong to no slice, and a new slicing mechanism.
- **Size-switched landing** (single PR under a threshold, stack above) — all the cost of the stack plus a threshold.
- **Dispatch-tick size gate** — a new park disposition in every backend's contract and bounded prefetch machinery on the tick, for a constraint the operator no longer considers binding.
- **Signal-only size, with no enqueue check at all** — rejected in favor of the optional enqueue-time ceiling, so a client that does want a bound has one without a tick gate.
- **Continuously rebasing the epic branch on the default branch** — contradicts ADR-0022, and a rebase is a force-push under live child branches.

### Evidence

Recorded in `## References` above. The counts (94 `.default_branch` reads, about 40 modules) were taken by grep at the authored commit and are a floor for S2 and S3, not a substitute for their own enumeration.

### Do not re-adjudicate

- **Cut from scope:** a `cw epic` command group; a client-level epic mode; scheduled default-branch sync into epic branches; reviewer sharding; a merge-commit option for the landing row; any change to RFC 0015's swing.
- **Deferred:** per-ticket history on the default branch (merge commit instead of squash for the landing row) → a ticket after D1, if a client asks. Claude-native sharding for large landing reviews → RFC 0015 B1, pull-in candidate at buildout. Trackers where a PR body citation does not close a ticket → B2 hardening.
- **Exit choice:** Exit 1, because the seam sweep is a wave-0 dependency for everything else and RFC 0015 D-B2 promised an RFC.

### Orchestrator brief

- **Drives:** `sprint-buildout` on this RFC, then dispatch by wave. S1, S2, S3, A1 and B2 define or change contracts (schema, context key, manifest elements, adoption precedence): harden them before dispatch. Sprints 1 to 3 merge to the default branch; sprint 4 is enqueued with `--base epic/3778` and landed by D1 (D-13).
- **Gates:** `sprint-buildout`'s Approve/Abort; the trust-root allowlist in S3 is a human-read item at review; D1's record goes to the operator before the milestone closes.
- **Terminal state:** D1's dogfood epic landed through adoption with every child closed by the landing merge, and D-8 and D-11 confirmed or amended.

## Issues

Issues: Epic I #3777 · Epic II #3778 · S1 #3779 · S2 #3780 · A1 #3781 · S3 #3782 · B1 #3783 · B2 #3784 · B3 #3785 · B4 #3786 · D1 #3787

Milestone: https://github.com/mattwwarren/claude-workspace/milestone/26

Pulled in: #3069 (review configuration surfaces; land before or with S3), #2990 (whether the `branch_behind_main` gate probes dispatch-driven rows; decide before or with S2), #2994 (bound the in-lock staleness tip check; same lines as S3).
