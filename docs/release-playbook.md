# Release Playbook — phased RFC → dark release → gated activation

How a multi-phase feature sprint gets from an accepted RFC to a shipped release
in `cw`, without a human touching a merge and without a release ever activating
an auto-actor by surprise.

The one property that makes the whole thing safe: **merged ≠ armed.** Every
behavioural change lands behind a default-off flag, so phases can merge and a
release can ship "dark." Turning it on is a separate, permission-gated operator
action decoupled from the merge.

This is the pattern RFC 0009 (gate recipes) shipped under as v1.18.0, and the
pattern RFC 0010 (native review-monitor) follows. The RFC 0010 sprint is the
worked example throughout.

## The four stages

### 1. RFC with an explicit `Phasing` table

The RFC's `Phasing` table is not documentation — it is the **work breakdown**.
Each row (P1..Pn) becomes exactly one GitHub issue; the row's *Gate* column
becomes that issue's acceptance criteria. Dependencies between phases are
stated in the table and carried onto the issues (`Blocked on P<x> (#<n>)`).

RFC 0010 → issues #1096 (P1) … #1100 (P5), each body derived from the phase's
Design (W-sections) and Gate.

### 2. Incremental merges behind a default-off flag

Phases merge to `main` in dependency order, each as its own PR through the
normal `/auto-dev` pipeline. Because the feature is gated by a **master switch
that defaults to `False`** plus a **per-lane map whose floor is `False`**,
merging a phase changes no runtime behaviour. The code is present, tested, and
inert.

The flag convention (mirror it for every new auto-actor):

- A master opt-in on `OrchestratorConfig`, default `False` — e.g.
  `concierge_enabled` (`models.py:868`), `gate_recipes_enabled`
  (`models.py:882`), and `review_recipes_enabled` (RFC 0010). A `False` master
  switch short-circuits the entire module.
- A **per-lane** `dict[str,bool] | None` map on `LaneConfig` (+ a `TicketTask`
  override) resolved most-specific-wins (ticket → lane → hardcoded-off floor),
  so risk is armed per lane, never globally by accident. See
  `resolve_gate_recipe_enabled` for the shape.

A fresh install therefore auto-does nothing. This is what lets the pipeline
merge aggressively.

### 3. Merge waves (sequence by dependency, parallelise the rest)

Order the phase PRs by the dependency graph, run independent phases in
parallel, serialise only where a phase needs another's code:

RFC 0010 example:

- **Wave 1 — P1 (#1096):** the module skeleton everything imports. Merges
  first. Inert (master switch off).
- **Wave 2 — P2 (#1097) + P3 (#1098):** both depend only on P1 and touch
  mostly disjoint surfaces (events/act-phase vs config plumbing) → dispatch in
  parallel once P1 is in.
- **Wave 3 — P4 (#1099):** needs P1+P2+P3. This is where an unresolved RFC
  **Open Question** is confirmed with the operator *before merge* (RFC 0010's
  OQ2, `auto_fix_ci` semantics).
- **Wave 4 — P5 (#1100):** docs/tests-only tail (e.g. porting operational
  lessons). Lands last, lowest risk.

Then cut the release (next minor — RFC 0009 was v1.18.0, RFC 0010 targets
v1.19.0) bundling P1..Pn.

### 4. Activation — a separate, classifier-gated operator action

The release ships **dark**. Activation is a deliberate later step, never part
of the release:

1. Opt a lane in via `clients.yaml` (`gate_recipes: {…}` /
   `review_recipes: {…}`).
2. Flip the master switch on in `~/.claude-workspace/orchestrator.yaml`.

The master-switch flip **arms a production auto-actor**, so it is
classifier-gated to the operator — handed over as a `! <command>`, never
written by an agent. This is the same posture RFC 0009 used: shipped v1.18.0
dark, then dogfooded on the `dogfood` lane by opting the lane in and flipping
`gate_recipes_enabled` via an operator `!` command; disarmed back to the
shipped default afterwards.

Rollback is symmetric and instant: flip the lane map or the master switch back
to `False` — it takes effect on the next reconcile tick, no redeploy.

## Release mechanics

- `scripts/release.sh <version>` creates and pushes the release tag; the
  version bump in `pyproject.toml` precedes it (`cw.__version__` is resolved
  dynamically from the installed distribution — there is no separate version
  literal to edit).
- **Before cutting a release, ensure `uv.lock` is re-locked to the new version**
  (see #1101). The v1.18.0 cut left `uv.lock` lagging at the prior version,
  which later shows up as a dirty main checkout that trips `main_checkout_drift`
  and eventually freshness-gates dispatch. Re-lock as part of the release, or
  the next cut repeats the cycle.

## What does *not* ride the RFC release

Keep the RFC's release bundle to the RFC's phases. Independent work ships on its
own cadence or bundles opportunistically into whatever release is open:

- Standalone fixes (e.g. #1091 docs/install, PR #1102) ship as their own PR.
- Cross-repo parity ports (e.g. #1080/#1081 into `global-claude`) are separate.
- Release-hygiene fixes (#1101) should land *before* the next cut but aren't
  part of the feature.

## The reusable shape (for any repo)

1. An RFC whose `Phasing` table Gate column *is* each ticket's acceptance.
2. A **default-off feature-flag floor** (master switch + per-lane map) so
   incremental merges are inert — merged ≠ armed.
3. Merge waves ordered by dependency, parallel where independent.
4. One bundled release at phase-set completion, shipped dark.
5. **Activation decoupled from the merge** and gated to a human as the sole
   arming authority.

Stages 2 and 5 are the load-bearing ones: they are what let an autonomous
pipeline merge and release without a human in the loop, while keeping the human
as the only one who can turn a behaviour *on*.

## Related

- `docs/rfcs/0009-gate-recipe-automation.md` — the first sprint under this
  pattern (shipped v1.18.0).
- `docs/rfcs/0010-native-review-monitor.md` — the worked example here.
- `docs/dispatch-runbook.md` — the per-wave `cw dev-queue` dispatch procedure.
- `docs/operator-channel.md` — how activation-time events surface to the operator.
