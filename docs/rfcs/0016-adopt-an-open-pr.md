# RFC 0016 — Adopt an Open PR into the Pipeline

## Summary

cw dispatches only branches it named itself. Every part of the pipeline derives the branch as `<feature_branch_prefix>/<ticket_id>` and discovers a PR by that name, so a PR opened by a human (or by another account) is invisible to the review ledger, the finalize fence, the merge gate and the repair recipes. The operator's only options today are to hand-fix the PR or to close it and re-implement it from the ticket.

This RFC adds adoption. `/adopt-pr <PR>` resolves the PR's ticket, then calls one cw primitive, `cw dev-queue add <TICKET> --adopt-pr <N>`, which creates a normal dev-queue row that carries an explicit branch, the PR url and a pinned head SHA. A green PR enters at review; a conflicted or red-CI PR enters at impl with feedback synthesized from the PR itself. From there the existing reactor, review ledger, finalize fence and recipes apply unchanged. A foreign push to the adopted branch parks the row rather than being absorbed.

Three ordered units, each one ticket and one PR: core adoption, repair routing, and wiring the #2905 red-shipped-PR recovery onto the repair primitive. PR #2933 is the worked example and the first real customer: it is adopted through the core unit and finished through the pipeline, and the repair-routing unit depends on its `--review-feedback-file` mechanism.

## Motivation

- A branch not named `<prefix>/<ticket>` cannot enter the pipeline. The derivation is repeated across twelve literal sites plus one partial helper (`feature_branch_key`, #728); see References.
- `create_worktree` already provisions a worktree on an existing remote branch (#2032), and `gh` resolves a PR number as an issue, so the provisioning and ticket-read halves of adoption already work. The branch identity is the missing piece.
- The conflict recipes (#2771, #2917) fire only on a `COMPLETED` row, and they re-arm auto-merge. They cannot be the entry for a foreign PR: adopting as `COMPLETED` would arm auto-merge on unreviewed code.
- Finalize ships; it does not repair. Headless Stage 5 (CI wait and feedback) is skipped, and any commit after a clean review round invalidates the convergence ledger. A merge conflict or red CI on an adopted PR therefore cannot be fixed at finalize.
- RFC 0010 scoped the native review monitor to auto-dev PRs and left human-authored PRs to a later RFC. This is that RFC for the dispatch side.
- #2933 (draft, CI green, mergeability UNKNOWN on 2026-11-04) has `closingIssuesReferences: []` and no ticket id in its branch, commits or body, so ticket resolution cannot assume a link.

## Design

### Epic I — Adoption core

One queue row per adopted PR, created by a single primitive. The row carries an explicit branch resolved at one seam that replaces every `<prefix>/<ticket>` derivation, the PR url, and a pinned head SHA; it enters at review. A skill resolves the ticket through a ladder that ends in a knock, echoes the proposal before creating anything, and refuses fork PRs. A head that moves for any reason other than cw's own lease-pinned push parks the row for the operator to re-pin. Non-green PRs park at intake with a named reason until Epic II removes the park. RFC 0010's scope line is reworded in the same change.

### Epic II — Repair and recover

A source-agnostic synthesizer turns a PR's conflicted files or failing checks into head-SHA-pinned feedback. Adoption uses it to route a non-green PR to impl instead of parking; the recipe fence stays untouched. The same primitive is then the recovery path for #2905, a cw-shipped PR that goes red with no signal and no way back to impl.

## Phasing

Human-facing summary, not parsed; `## Tickets` is the parse source.

- Wave 0, Sprint 1: A1 — core adoption (Epic I).
- Wave 1, Sprint 2: B1 — repair routing (Epic II). Starts only after #2933 has been adopted through A1 and merged.
- Wave 2, Sprint 3: B2 — #2905 recovery on the shared primitive (Epic II), last in the sprint.

## Resolved decisions

Firm leans, decided to unblock breakout. Each is reversible at ticket-hardening if the code contradicts it, except where a decision names its own reversal point.

- **D-1 — Adoption is a dev-queue row.** `cw dev-queue add <TICKET> --adopt-pr <N>` creates a normal row that carries an explicit branch, `pr_url`, a pinned head SHA and `--stage review`, so the reactor, recipes, review ledger and finalize all apply. This rewords RFC 0010's "auto-dev PRs only" scope line, in the same PR that ships it.
- **D-2 — The row carries an explicit branch, resolved at one seam.** One resolver (the row's branch when set, else `<prefix>/<ticket_id>`) replaces every literal derivation and replaces `feature_branch_key` at `_reap.py:106` rather than wrapping it. It lives below `worktree`, `reconcile`, `dispatch`, `dev_queue`, `doctor` and `review_history` in the import graph so none of them import a sibling. All consumers migrate directly with no shim (ARCHITECTURE principle 11, repo backwards-compat rule). If the migration balloons at hardening, split it into its own ticket then.
- **D-3 — Ticket resolution is a ladder ending in a knock.** In order: explicit `--ticket`; GitHub `closingIssuesReferences`; an agent parse of body, branch and commits at the skill layer (ADR-0013); otherwise a knock where the operator picks a ticket or the PR as its own ticket. A parsed id is a proposal echoed before the row exists, Stage 0 verifies it, and a cross-tracker id (for example `ABC-123`) is recorded as a label only.
- **D-4 — A foreign head move parks the row.** The row pins the head SHA at adoption. A head that moves for any reason other than cw's own lease-pinned push parks the row as `adopted_head_drift`, naming both SHAs and the commit range; `requeue` re-pins and fails closed when the new head does not descend from the old one. Absorbing a fast-forward and reopening the review ledger is deferred as a follow-up; owner-convention handoff (draft flip or label) is rejected because it is an outward write that relies on the author.
- **D-5 — Entry stage follows PR condition.** A green PR enters at review. A conflicted or red-CI PR enters at impl with feedback synthesized from the PR, using the `--review-feedback-file` mechanism that carries feedback into impl pinned to repo, PR and head SHA. The recipe fence (`is_repair_shape`, `repair_fence.py`) is not touched. Until the repair-routing ticket lands, a non-green PR parks at intake as `adopt_pr_not_green` naming the condition.
- **D-6 — The adopt command is the push authorization for that branch.** It is the operator's own PR and every cw push uses the lease pinned to the expected SHA. Fork PRs are refused at intake.
- **D-7 — Three ordered units.** Core adoption, then repair routing (gated on #2933 finished through core adoption), then the #2905 recovery. A unit too big for one PR is split, not stacked (RFC 0015 D-B2).
- **D-8 — `/adopt-pr <PR>` skill plus one primitive.** The skill owns ladder rungs 1 to 4, the echo and the knock; cw owns one row-creation path, `add <TICKET> --adopt-pr <N>`, which is where D-2 and D-5 live. There is no separate `adopt` verb and no second creation path.
- **D-9 — A swing is one ticket until RFC 0015 `--children` ships.** Buildout files an epic and separate tickets, each dispatching as its own PR, and `--children` is not shipped. So each unit is a single ticket whose Acceptance lines carry the child-level scope. This reverses when `--children` ships, at which point a unit may be re-cut as a parent with children.
- **D-10 — Fail-closed intake rules.** An adopted row never enters as `COMPLETED`, because the recipes re-arm auto-merge. Mergeability `UNKNOWN` is never green: intake re-polls a bounded number of times, then parks `adopt_pr_state_unknown`. Synthesized feedback and CI log text are untrusted data pinned to the head SHA, never instructions. A draft PR must reach ready-for-review before finalize arms auto-merge, or park with a named reason.
- **D-11 — The feedback synthesizer is shared, and #2905 wiring comes last.** The synthesizer takes a PR and returns head-SHA-pinned feedback with no adoption-specific argument. Adoption is its first caller; #2905's recovery is the second, in its own last ticket. The #2905 finalize and review-gate semantic changes stay out of the repair-routing ticket.

## Tickets

### A1 — Core adoption: `/adopt-pr`, `--adopt-pr`, branch seam, drift pin

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** The pipeline derives the branch as `<prefix>/<ticket>` in twelve literal sites, so a PR on any other branch cannot enter. This ticket makes the branch an explicit field resolved at one seam, adds the adoption primitive and skill, pins the head, and enters green PRs at review.
- **Scope:** D-1, D-2, D-3, D-4, D-6, D-8, D-9, D-10
- **Acceptance:**
  - `cw dev-queue add <TICKET> --adopt-pr <N>` creates one row carrying branch, `pr_url`, pinned head SHA and stage review, and refuses a fork PR, a closed or merged PR, and a PR already carried by a live row.
  - One branch resolver replaces every literal `<prefix>/<ticket>` derivation and `feature_branch_key`, and a test fails if a module outside the resolver rebuilds the branch from the prefix.
  - Provisioning, the open-PR screening, the stale-dispatch gate, fix dispatch, requeue, scope-drift and the repair engine read the row's branch, and each has a test with an adopted row on a non-prefix branch.
  - A new row field stores the pinned head SHA, and a head move not made by cw parks the row as `adopted_head_drift` naming both SHAs and the commit range.
  - A cw push made under the pinned lease updates the pin, and `requeue` re-pins when the new head descends from the old one and fails closed when it does not.
  - A non-green PR (conflicted or red CI) parks at intake as `adopt_pr_not_green` naming the condition, and mergeability `UNKNOWN` re-polls then parks as `adopt_pr_state_unknown` and is never treated as green.
  - `/adopt-pr <PR>` runs ladder rungs 1 to 4, echoes the proposed ticket before the row exists, records cross-tracker ids as labels only, and knocks with a ticket-or-PR-as-ticket choice when nothing resolves.
  - A draft PR reaches ready-for-review before finalize arms auto-merge, or parks with a named reason; the ticket records which at hardening.
  - RFC 0010's scope line is reworded and `docs/dispatch-runbook.md` documents adopt, drift, requeue re-pin and the interim non-green park.
  - Kill-checks: removing the head comparison makes the drift test fail, and removing the descendant check makes the fail-closed re-pin test fail.
  - Hermetic tests with stubbed subprocess and frozen clock, and all repo quality gates pass with coverage floors unchanged.

### B1 — Repair routing: non-green PRs enter at impl with synthesized feedback

- **Epic:** II
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** A1
- **Context:** After A1 a non-green PR parks at intake. This ticket removes that park by synthesizing feedback from the PR and entering at impl through the review-feedback mechanism. It starts only after PR #2933 (which carries `--review-feedback-file`) has been adopted through A1 and merged.
- **Scope:** D-5, D-9, D-10, D-11
- **Acceptance:**
  - A source-agnostic synthesizer takes a PR and returns head-SHA-pinned feedback text for the conflicted files or the failing checks, with no adoption-specific parameter in its signature.
  - A conflicted or red-CI adopted PR enters at impl carrying that feedback, and Stage 2 fails closed on a repo, PR, branch or head SHA mismatch.
  - The `adopt_pr_not_green` park is removed, a green PR still enters at review, and the diff touches neither `is_repair_shape` nor `repair_fence.py`.
  - Check-log or conflict text containing an instruction is passed as data and never followed, covered by a test with an injected instruction.
  - An impl agent that exhausts its attempts parks with a named reason and `requeue` is documented as the one-command recovery.
  - `docs/dispatch-runbook.md` documents the non-green route and the shared synthesizer.

### B2 — Recover a red shipped PR through the shared primitive (#2905)

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** B1
- **Context:** #2905 reports that a cw-shipped PR with auto-merge armed can go red with no signal and no way back to impl. B1's synthesizer is exactly the evidence that recovery needs, so this ticket wires #2905 onto it as the last unit of the sprint. It closes #2905.
- **Scope:** D-9, D-11
- **Acceptance:**
  - A required check failing on a cw-shipped, auto-merge-armed PR raises one attention event naming the check, once per episode, on the orchestrator stream and in `cw queue peek`.
  - A documented and tested command path takes that event back to a fix and a re-review using the shared synthesizer, with no hand-editing of queue state.
  - A fix after a clean review round is still re-reviewed before it can merge, and a green PR behaves exactly as today.
  - The choice between regressing a completed row to impl and a bounded CI check in finalize is made at ticket-hardening and recorded with its reason.
  - `docs/dispatch-runbook.md` describes what an orchestrator does on a red shipped PR.

## References

- `src/cw/cli/dev_queue/crud.py:57` — `_STAGE_CHOICES = ("plan", "impl", "review", "finalize")`; the `--stage` option sits at `:151` and `:321`. `--adopt-pr` joins `add` here.
- `src/cw/reconcile/_shared/_reap.py:106` — `feature_branch_key(client_name, ticket_id, clients)`, the existing partial branch SSOT (#728). D-2's resolver replaces it.
- `src/cw/dev_queue/requeue.py:93` and `:161` — literal `<prefix>/<ticket>` derivations.
- `src/cw/reconcile/fix_dispatch.py:649` — literal derivation passed to fix provisioning.
- `src/cw/dispatch/claim/spawn.py:592` — literal derivation at spawn.
- `src/cw/dispatch/claim/screening.py:234` and `:282` — literal derivations in the open-PR screening.
- `src/cw/dev_queue/scope_drift.py:200` — literal derivation.
- `src/cw/reconcile/review_recipes/repair_engine.py:483` and `:793` — literal derivations in the repair engine.
- `src/cw/reconcile/unowned_running.py:179` — literal derivation.
- `src/cw/review_history/collect.py:123` — literal derivation.
- `src/cw/worktree/_paths.py:169` — literal derivation that sits below `reconcile`, which is why the resolver must live lower still.
- `src/cw/doctor/wedge/task_running.py:127` and `:162`, `src/cw/doctor/loop_health.py:200` — fallback and documented derivations to re-read when migrating.
- `src/cw/worktree/_lifecycle.py:326` — three-way branch resolution (local ref, `origin/<branch>`, new from default; #2032); provisioning on an existing remote branch already works.
- `src/cw/gh.py:877` — `_PR_HEAD_FIELDS` includes `headRefOid`; `:903` to `:915` read it; `:551` `branch_head_sha_on_origin`. Head-drift detection needs one new stored field and a comparison, not a new fetch.
- `src/cw/models/tasks.py:462` — `TicketTask`; `stage` at `:703`, `pr_url` at `:713`, `pr_state` at `:764`. There is no branch field and no pinned head SHA today.
- `src/cw/reconcile/review_recipes/repair_engine.py:232` — `is_repair_shape` requires `COMPLETED`, which is why adoption cannot enter through the recipes.
- `src/cw/reconcile/review_recipes/mechanical_repair.py:9` to `:28` — the recipe disarms, force-pushes with a lease and re-arms auto-merge; the re-arm is the hazard behind D-10.
- `src/cw/pr_hydrate.py:253` and `:503` — `_compute_attention_state` and `_is_candidate`; the candidate set is rows with a `pr_url`.
- `.claude/commands/auto-dev-review.md:424` — fix dispatch merges `origin/<default>` into the branch and aborts on conflict; it never auto-resolves.
- `.claude/commands/auto-dev-finalize.md` — Step 4.0 fence, Step 4.0b verify-tip, Step 4a merge gate, and the headless Stage 5 auto-skip.
- `docs/rfcs/0010-native-review-monitor.md` — the "auto-dev PRs only" scope line that D-1 rewords.
- `docs/rfcs/0015-epic-intake-one-plan-many-tickets.md:38` (D-S1) and `:45` (D-B2) — children supplied never resolved by cw; stacked PRs out of scope.
- `docs/adr/0012-cw-never-grants-github-review-approvals.md`, `docs/adr/0013-agent-delegated-ticket-work.md`, `docs/adr/0020-the-operator-is-the-orchestrator-session.md`, `ARCHITECTURE.md` principle 11 — standing decisions this design attaches to.
- Issue #2905 (red shipped PR, no signal, no recovery) and #2921 (changelog-repair rebase invalidates the ledger; not a dependency, D-5 repairs before the first review round).
- PR #2933 — draft, head `bb649bbc`, five CI checks green, mergeability UNKNOWN on 2026-11-04; adds `cw dev-queue add --review-feedback-file`.

## Handoff Brief

**Authored:** 2026-11-04 at f8bc5629
**Exit:** 1 sprint block

### Decisions

- **D-1 to D-11** live in `## Resolved decisions` above, the single source, with ids stable. Reversal point for all: ticket-hardening if the code contradicts them. D-9 also reverses when RFC 0015 `--children` ships. D-2's seam may be split into its own ticket at hardening if the migration balloons.

### Rejected approaches

- **Repair at finalize** — finalize ships and does not repair: Stage 5 is skipped headless and a post-review commit invalidates the ledger (#2921, #2905).
- **Adopt as `COMPLETED` so the recipes fire** — the recipes re-arm auto-merge, so unreviewed foreign code would be armed.
- **Widen `is_repair_shape` into a pre-review repair lane** — it covers only declared-path conflicts, still parks the rest, and touches the most safety-critical fence in the pipeline.
- **Knock on every non-green PR as the final behavior** — it hands every red or conflicted PR back to the operator by hand; kept only as A1's interim park.
- **Hand-fix before adopting** — same hand-work objection.
- **Absorb a foreign fast-forward and re-review** — silently re-spends a review round per push and races a cw fix agent against a mid-rebase author; deferred, not cut.
- **Ownership handoff by draft flip or label** — an outward write per PR that relies on the author honoring it.
- **A flag-only `add` with no skill** — fails the no-hand-work rule whenever the ticket is not linked.
- **A new `cw dev-queue adopt` verb** — cannot run the agent parse without breaking ADR-0013, and creates a second row-creation path.
- **One swing, or three** — one swing exceeds a PR (twelve derivation sites plus new intake); three adds a dispatch round for a refactor that can be split at hardening if needed.
- **An independent feedback synthesizer for adoption, or pulling #2905 into repair routing** — the first builds the same path twice; the second drags finalize and review-gate semantics into an adoption unit.

### Evidence

- See `## References` for the `file:line` facts; the load-bearing ones are `_reap.py:106` (the existing partial seam), `worktree/_paths.py:169` (forces the resolver low in the import graph), `gh.py:877` (head SHA already fetched, no stored pin), `repair_engine.py:232` (recipes need `COMPLETED`) and `auto-dev-review.md:424` (the fix path aborts on conflict).

### Do not re-adjudicate

- **Cut from scope:** absorbing foreign pushes (D-4); fork PRs (D-6); pulling #2905's finalize and gate changes into B1 (D-11); stacked PRs (RFC 0015 D-B2); extending the global review-monitor to human PRs beyond the dispatch side.
- **Deferred:** whether a draft PR is flipped to ready by finalize or by adoption → resolved by the A1 ticket-hardening pass, which reads the finalize flow. Whether #2905 regresses a completed row to impl or adds a bounded finalize CI check → resolved by B2 hardening.
- **Exit choice:** Exit 1, because D-1 rewords RFC 0010 and D-2 touches ARCHITECTURE principle 11, so the change belongs in the RFC series with provenance.

### Orchestrator brief

- **Drives:** `sprint-buildout` on this RFC, then dispatch A1. After A1 merges, run `/adopt-pr 2933` to finish PR #2933 through the pipeline, and dispatch B1 only after #2933 has merged. Dispatch B2 after B1.
- **Gates:** `sprint-buildout`'s Approve/Abort gate before any filing; the review approval gates the pipeline already enforces; B1 is not dispatchable until #2933 is merged.
- **Terminal state:** #2933 merged through the pipeline, B1 and B2 shipped, and #2905 closed by B2.

## Issues

Issues: Epic I #2941 · Epic II #2942 · A1 #2943 · B1 #2944 · B2 #2945

Milestone: https://github.com/mattwwarren/claude-workspace/milestone/22

Pulled in: #2836 (split `gh.py`; land before B1), #2905 (closed by B2).

## Errata

Added 2026-11-05, after ticket hardening. The decisions and tickets above are left as merged so the provenance stays readable. Where they conflict with this section, this section wins. The binding per-ticket resolutions are recorded on the tickets themselves.

- **D-6 overstates the lease.** "Every cw push uses the lease pinned to the expected SHA" holds only for the repair recipe (`src/cw/reconcile/review_recipes/repair_git.py`, `--force-with-lease=refs/heads/{branch}:{expected_sha}`), which runs on `COMPLETED` rows only. Impl, review-fix and finalize pushes are worker-prose plain pushes that cw never sees, and `src/cw/codex_fix_loop/push.py` is a plain `git push origin HEAD:refs/heads/<branch>`. The A1 acceptance line "a cw push made under the pinned lease updates the pin" therefore covers the repair push only. The pin otherwise advances at the end of a cw-owned session, and only when the old pin is an ancestor of the origin head.
- **Head-drift detection needs a new read.** The References line "one new stored field and a comparison, not a new fetch" is wrong. `_PR_VIEW_FIELDS` in `src/cw/gh.py`, which hydration uses, has no `headRefOid`, and `PrState` has no head field. `_PR_HEAD_FIELDS` is a separate, narrower fetch. Drift is checked at claim and spawn time by reading the origin head with `branch_head_sha_on_origin`, not from hydration, which stays a throttled best-effort observer.
- **The References list omits the `feature_branch_key` call sites.** Nine calls in eight files migrate to the resolver: `dispatch/pr_gate.py`, `dispatch/tracker_mcp_gate.py`, `doctor/loop_health.py`, `doctor/wedge/task_running.py`, `reconcile/codex_boot.py` (two), `reconcile/prepasses.py`, `reconcile/shipped_release.py` and `reconcile/tasks.py`.
- **A1 was split, seam first.** The branch resolver and the row's explicit branch field moved to their own ticket (#2948), and A1 (#2943) depends on it. D-2 anticipated this split ("split it into its own ticket then").
- **Draft PRs are flipped at finalize.** D-10 left the draft-PR choice to hardening. Decision: a draft PR is marked ready after review and the Step 4.0 fence pass, immediately before auto-merge is armed, for adopted rows only. Failure to flip emits `agent_block`; no new disposition.
- **Finalize's merge-gate overlap check does not see adopted PRs.** The Step 4a sibling inventory filters `<branch-prefix>/*`, so an adopted PR on another branch name is invisible to other tickets' overlap checks. A1 documents the gap in the runbook and does not change the inventory. Tracked separately as debt in #2949.
