# RFC 0015 — Epic Intake: One Plan, Many Tickets

## Summary

`/auto-dev` takes one ticket and produces one plan, one worktree, one branch and one PR. An epic whose children were sized for that shape ships as a drip: each child round-trips through plan, review and merge on its own, each merge races the siblings for the default branch, and the design that made the children coherent is re-derived by every worker from the parent's prose. `cw-fanout` parallelizes the drip; it does not change its shape.

This RFC adds an epic intake mode. `cw dev-queue add --epic <parent>` enqueues one row for the parent ticket that carries its children. Stage 0 materializes parent and children into one context, Stage 1 writes one plan that must cover every child, Stage 2 implements it in one worktree and one branch, Stage 3 reviews one diff, and Stage 4 opens one PR whose merge closes every child with a citation. Scope stays governed by v4 tiering (`auto-dev.md`, "Scope Tier Evolution"): the tier is a forbidden-area risk classification, and size is the advisory `large_diff` that selects the impl model and the reviewer set. One plan approval, when the gate asks for one, covers the whole epic.

Two epics. The first lands the row, the context and the stage prose so a swing can be dispatched end to end. The second makes big diffs review well: reviewer sharding by file cluster, and the orchestration vocabulary (swing vs fan-out) that tells an operator which to reach for.

## Motivation

Observed on a real backlog: a security launch gate with eleven gate tickets, of which five describe one capability-profile system (profile store, limits bundle, param allowlist, action gating, tenancy ratchet), and an ORM consolidation with fourteen model-group tickets that were sized tiny on purpose. Dispatched as children, each one plans against the same prose, waits in the same merge queue, and the fifth cannot be reviewed as a system because the first four already merged.

The pipeline already has every mechanism the swing needs except the shape: the plan stage can approve one fingerprint, the impl stage already requires incremental commits, the review stage already scales its reviewer set to the diff, and finalize already closes the ticket it was given. What is missing is a row that names more than one ticket, a context that carries them, a coverage check that stops a plan from silently dropping one, and a finalize that closes all of them.

## Design

### Epic I — Swing dispatch

One queue row for the parent, carrying `queue_metadata.epic_children`. Stage 0 fetches the parent and every child (body and comments) and writes them into `.cw/context.json` under a `children` array, and also appends a `## Children` section to `ticket_body` so a prose reader that only reads the body still sees the whole scope. The open-PR self-check runs per child. Stage 1 writes one plan with a mechanically parsed `## Child coverage` section that maps every child id to the plan items that deliver it; an uncovered child is a `blocked` exit, never a silent drop. Stages 1 through 3 live-fetch comments on the parent and on every child, so an operator resolution posted on a child reaches the worker. Stage 4 opens one PR that cites every child and, on ship, closes each child with a citation comment and leaves the parent to the tracker's own roll-up. The sentinel gains an optional `epic` block naming the parent, the children and which were closed.

### Epic II — Big diffs review well

When a diff is `large_diff`, Stage 3 shards the full reviewer set by file cluster (one cluster per top-level package touched, capped) so that no single reviewer reads a diff it cannot hold, and the adjudication step merges the shards. The `cw-fanout`, `orchestrate-sprint` and `cw-followup` skills learn the vocabulary: a *fan-out* is N independent tickets in N worktrees, a *swing* is one epic in one worktree; the orchestrator picks the swing whenever the children share a design or a merge order.

## Phasing

| Wave | Epic I | Epic II |
|------|--------|---------|
| 0 (seam) | S1 — epic row and context | |
| 1 | A1 — intake and plan coverage · A2 — comments across children | |
| 2 | A3 — finalize closes children | B1 — reviewer sharding |
| 3 | | B2 — swing vs fan-out vocabulary |

## Resolved decisions

- **D-S1 — The parent is the row.** The queue row's `ticket_id` is the parent; children live in `queue_metadata.epic_children` as an ordered list of ids. One branch `<prefix>/<parent-id>`, one worktree, one sentinel. A child is never enqueued on its own while its parent's row is live (the pre-dispatch gate refuses it as `stale_dispatch`).
- **D-S2 — Children are data in the context, prose in the body.** `.cw/context.json` gains `children: [{id, title, body, comments}]` with the same comment shape as the parent (#1794). `ticket_body` is the parent body followed by a `## Children` section with one `### <id> — <title>` subsection per child, so every existing body-only reader (the Python readers pinned by `tests/test_auto_dev_intake_context_schema.py`, the opencode and local runners) sees the full scope without a code change.
- **D-A1 — Coverage is mechanical.** The plan's `## Child coverage` section lists every child id with the plan items that deliver it. A missing id is `blocked` with `blocker.reason: "epic_child_uncovered"` (`stage1_plan`, `retry_eligible: true`), parked with the draft, never a silent partial plan. The plan-draft fingerprint is computed over the whole plan, so one approval covers the epic.
- **D-A2 — Live comments include the children.** Every stage that live-fetches comments (plan, impl, review) fetches the parent's and each child's, merges them by `created_at`, and records the source id on each entry. An operator resolution on a child counts as a resolution for the epic.
- **D-A3 — Finalize closes children, not the parent.** The PR body carries one citation line per child (`Closes #<n>` for GitHub Issues; the Linear identifier in the body for Linear). After the merge, finalize posts a citation comment on each child and sets it done through the tracker op; the parent is left to the tracker's roll-up. A child that cannot be closed is a `friction_highlights` entry, not a failed ship.
- **D-A4 — Scope stays v4.** No new tier. `large_diff` is expected for most swings and does what it does for a single ticket: Opus impl model, full reviewer set, a friction tag. The only approval gate is a forbidden-area touch or `scope_hint: large`, and it asks once for the whole epic.
- **D-B1 — Sharding is an adjudication-time concern.** Reviewer shards write to the same round findings directory with a `shard` field; Checkpoint 3a adjudicates the union. A reviewer role that does not shard well (Product Manager, spec compliance) reads the whole plan and the diff stat rather than a cluster.
- **D-B2 — Stacked PRs are out of scope.** One swing is one PR. Splitting a swing into an ordered PR stack is a later RFC; a swing too big for one PR is two swings.

## Tickets

### S1 — Epic row and context: `--epic`, `queue_metadata.epic_children`, `children` in `.cw/context.json`

- **Epic:** I
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** `cw dev-queue add` takes one or more ticket ids and no notion of a parent; `.cw/context.json` carries one ticket. This ticket adds the row shape and the context shape every other ticket in this RFC reads.
- **Scope:** D-S1, D-S2
- **Acceptance:**
  - `cw dev-queue add <parent> --epic` resolves the parent's children through the active tracker, stores their ids in `queue_metadata.epic_children` in tracker order, and refuses an explicit child id that is already carried by a live parent row with `stale_dispatch`.
  - `cw dev-queue add <parent> --epic --children <id,...>` overrides the resolved list and refuses an id that is not a child of the parent.
  - The `.cw/context.json` writer materializes `children: [{id, title, body, comments}]` and appends the `## Children` section to `ticket_body`; `tests/test_auto_dev_intake_context_schema.py` pins both and the two Python body readers pass unchanged.
  - `cw-context.json` schema version bumps; `cw schema show` documents the new field.

### A1 — Intake and plan stage prose: fetch children, open-PR check per child, `## Child coverage` gate

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** Stage 0 and Stage 1 prose read one ticket. This ticket teaches them the children: intake fetches and materializes them, runs the open-PR self-check per child, and the plan stage requires and parses `## Child coverage`.
- **Scope:** D-S2, D-A1, D-A4
- **Acceptance:**
  - `auto-dev-intake.md` Step 3 fetches every child when `epic_children` is set and runs the open-PR self-check for each; a hit on any child exits `stale_dispatch` naming the child.
  - `auto-dev-plan.md` requires `## Child coverage` when children exist and exits `blocked` / `epic_child_uncovered` on a missing id; the gate-collapse table and the `blocker.reason` values table in `auto-dev.md` gain the row.
  - A markdown guard test in the style of `tests/test_plan_stage_retier.py` pins the section name, the blocker reason and the park behavior.
  - The plan-draft fingerprint rule is unchanged and a test shows one `cw dev-queue approve` releases an epic plan.

### A2 — Live comment fetch across parent and children in plan, impl and review

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** The three "comments are live, not cached" paragraphs fetch one ticket's comments. An operator who answers on a child today is invisible to the worker.
- **Scope:** D-A2
- **Acceptance:**
  - Each of the three stages fetches the parent's and every child's comments, merges by `created_at`, and records `source_id` on each entry in the overwritten `comments` array.
  - The comment provenance rule applies per entry; an agent-authored child comment is never approval evidence.
  - `check_impl_guard_staleness.py` reads the merged array and a test covers a child comment newer than branch HEAD.

### A3 — Finalize closes every child with a citation; sentinel `epic` block

- **Epic:** I
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** A1
- **Context:** Finalize closes the ticket it was given and cites it in the PR. For an epic the children are what the PR delivers.
- **Scope:** D-A3
- **Acceptance:**
  - `/prep-pr` under an epic row writes one citation line per child into the PR body; a GitHub Issues child gets `Closes #<n>`.
  - After merge, finalize posts a citation comment on each child and transitions it to done through the tracker op; a failure is recorded in `friction_highlights` and does not fail the ship.
  - The sentinel schema gains an optional `epic: {parent, children, closed}` block; `cw result emit` validates it and `cw-validate-result` prints it.

### B1 — Reviewer sharding by file cluster for `large_diff`

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** none
- **Context:** The full reviewer set reads the whole diff. A swing-sized diff exhausts a reviewer's context before it reaches the last file, and the findings skew toward whatever came first.
- **Scope:** D-B1
- **Acceptance:**
  - When the Stage 3 diff is `large_diff`, each sharding reviewer role is spawned once per file cluster (top-level package of the touched paths, capped at a configurable count) with only that cluster's diff and the whole plan; non-sharding roles run once.
  - Shard findings carry a `shard` field and Checkpoint 3a adjudicates their union; the round marker records the shard roster.
  - A test on a synthetic 40-file diff shows the cluster split and the merged findings.

### B2 — Swing vs fan-out vocabulary in the orchestration skills

- **Epic:** II
- **Wave:** 3
- **Sprint:** 3
- **Depends on:** A3
- **Context:** `cw-fanout`, `orchestrate-sprint` and `cw-followup` describe one shape of parallel work. Once a swing exists the operator and the orchestrator need to know when to reach for each.
- **Scope:** D-S1, D-B2
- **Acceptance:**
  - `cw-orchestration` defines *fan-out* and *swing* and gives the rule: shared design or merge order means swing.
  - `orchestrate-sprint` triage offers "enqueue as a swing" for an epic whose children share files, with the `cw dev-queue add --epic` command.
  - `cw-followup` handles the epic sentinel: a closed-children list is reported, an uncovered child is a re-park with the draft.

## References

- `.claude/commands/auto-dev-intake.md` Step 0d — the `.cw/context.json` writer and its key contract (#2700).
- `.claude/commands/auto-dev-plan.md` Step 1d and Step 1g — v4 scope classification and the `**Scope tier:**` marker.
- `.claude/commands/auto-dev.md` "Scope Tier Evolution" — v4 risk-only tier, `large_diff` advisory.
- `src/cw/context_queue_metadata.py` — where `queue_metadata` reaches the worker.
- `src/cw/dev_queue/approval.py` — the row-path approval that binds a draft fingerprint.
- `.claude/scripts/check_plan_scope_conformance.py` — post-impl drift gate; its allowance already scales with plan size.

## Issues

Issues: _(filled by `/sprint-buildout`)_
