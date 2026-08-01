---
name: sprint-buildout
description: >-
  Turn a hardened RFC into a filed GitHub sprint block — milestone, epics,
  tickets, an adjacent-bug pull-in scan, and (config-gated) Notion mirror
  pages. The mechanical layer is `cw sprint plan|apply`; this skill owns
  every judgment call the code deliberately does not make: presenting the
  single approval gate, spawning the pull-in scan, and deciding what to do
  when the RFC itself is defective. Use when the user says "turn this RFC
  into tickets", "build out the sprint", "ticket up RFC NNNN", "break this
  RFC into sprints", or otherwise wants an RFC's `## Tickets` section filed
  as real GitHub issues.
---

# sprint-buildout

## Why this exists

Filing a sprint by hand — reading an RFC's `## Tickets` section, writing a
milestone, an epic per wave, a ticket per line, wiring up footers and
children checklists, catching bugs the RFC's own diffs touch in passing,
then mirroring the plan into Notion — is a ~1-hour hand-driven pipeline with
a lot of ways to drift from what the RFC actually says. RFC 0011's buildout
session is the reference case this design was built from.

The design splits the work into three layers: `src/cw/sprint.py` /
`src/cw/cli/sprint.py` do the mechanical parse-and-file work and refuse hard
on a malformed RFC or config; this skill sequences that CLI plus the judgment
calls the code will never make (which bugs are worth pulling in, whether the
operator approves what's about to be created); the operator makes the one
call that matters — approve or abort at the single gate. See
`docs/superpowers/specs/2026-07-13-sprint-buildout-design.md` for the full
rationale and `docs/superpowers/plans/2026-07-13-sprint-buildout.md` for the
task-by-task build log; this file does not duplicate either.

Pure orchestration on the operator-judgment side — the skill adds no
RFC-parsing or issue-shaping logic of its own (that's `src/cw/sprint.py`'s
job). It sequences the CLI, one subagent, one human gate, the pull-in
filing calls (`gh issue edit`/`gh issue comment`), and the footer-PR calls
(`gh repo view`/`gh pr create`) — none of which `cw sprint plan|apply` do
themselves.

## Pipeline

1. **Plan.**

   ```bash
   cw sprint plan <rfc-path> --out <scratch>/plan.json
   ```

   `--version` is optional (defaults to a minor bump of the latest release
   tag); pass it only to pin a specific milestone version.

   If this refuses — e.g. `missing section: ## Tickets`, or a malformed
   `sprint_buildout:` config block — do **not** work around it. Report the
   defect verbatim and offer to fix the RFC against `docs/rfcs/TEMPLATE.md`
   (or point at `config/CONFIG_REFERENCE.md` for a config defect). Silently
   inferring what the RFC failed to say is the exact failure this design
   exists to prevent.

2. **Adjacent-bug pull-in scan.** Spawn **one `model: sonnet` subagent**.
   Feed it `plan.json`'s `references` field (or the RFC's own `## References`
   section text if you're working from the RFC directly) — the `file:line`
   refs the RFC's own tickets touch. Instruct it to search open issues/bugs
   for ones whose touched paths overlap those refs, and to return
   **candidates plus overlap evidence** (which bug, which file:line, why it
   overlaps) — not file dumps.

   Worked example: the RFC 0011 buildout session (milestone v1.20.0, epics
   #1151/#1152, tickets #1153–#1165) pulled in #1149 this way — both its
   fixes touched the reap decision path that RFC 0011's A1/B1 tickets build
   on.

3. **The single operator gate.** Present, in one message:
   - the milestone title (`plan.json`'s `milestone_title`),
   - the sprint→ticket map (`sprint_map`),
   - every issue title (`epics[*].title` + `tickets[*].title`),
   - the pull-in candidates from step 2 with their evidence and your
     recommendation,
   - an offer to show any issue body on request (bodies are in the same
     `plan.json`, just not dumped by default).

   Then wait. **Create nothing before the operator approves.** `gh issue
   create` has no draft mode — a typo ships instantly, and cleaning up 15
   live issues is a lot worse than not creating them yet.

   The gate's interaction contract is Approve or Abort — there is no
   in-place "edit." If the operator wants a change, that means the *RFC* is
   wrong or incomplete: fix the RFC and re-run `cw sprint plan` for a fresh
   `plan.json`, don't hand-patch the generated JSON. (Same pattern as
   `auto-dev-plan.md`'s plan-approval gate, where "Adjust" means re-plan and
   re-present, never hand-edit the artifact.)

4. **Apply.**

   ```bash
   cw sprint apply <scratch>/plan.json
   ```

   Report the real issue numbers by echoing its stdout verbatim — it prints
   `Milestone: #N`, one `  Epic <code>: #<number>` / `  Ticket <code>:
   #<number>` line per issue, and (when relevant) `Skipped (already
   existed): ...` / `Backfilled children checklist: ...` lines. `apply` is
   idempotent by title, so a partial prior run is safe to re-apply.

5. **File the pulled-in bugs.** For every pull-in candidate the operator
   accepted at the gate:

   ```bash
   gh issue edit <bug-number> --milestone <milestone-title>
   gh issue comment <bug-number> --body-file <scratch>/rationale.md
   ```

   The rationale comment names the specific shared code path — cite the
   exact `file:line` overlap evidence from step 2, not a generic "related to
   this RFC" note. These are standard `gh` CLI commands (`gh issue edit
   --help` confirms `-m, --milestone name`; `gh issue comment --help`
   confirms `-F, --body-file file`), not a pattern mirrored by any other
   skill in this repo — there is no `cw.gh` helper for milestone-attach or
   issue-comment, and none is needed here.

6. **Notion phase — config-gated.** Read `.claude/project-config.yaml`
   directly to check whether `sprint_buildout.notion` is present. Neither
   `cw sprint plan` nor `cw sprint apply` surfaces a "notion configured"
   signal on stdout — this is a skill-side check, not something the CLI
   tells you.
   - **Absent** ⇒ skip this phase silently in the pipeline, but say so
     plainly in your report ("Notion: not configured, skipped").
   - **Present** ⇒ create one Notion page per sprint in the configured
     `data_source`, using `sprint_page_properties` verbatim as the page
     properties. The Goal / risk narrative / dependency-chain prose on each
     page is yours to write — the skeleton and the properties come from
     config, not from you.
   - A present-but-malformed `notion:` block is not this skill's problem —
     `cw sprint plan` already refuses with `RfcContractError` before you'd
     ever reach this step, and that refusal is reported like any other
     step-1 CLI error.

7. **RFC footer PR.** Back-fill the RFC's `Issues:` footer with both the
   real numbers from step 4 and the milestone URL. Construct the URL
   yourself — no pipeline step returns one:

   ```bash
   OWNER_REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
   # https://github.com/${OWNER_REPO}/milestone/<milestone_number from step 4>
   ```

   Open a docs-only PR (`gh pr create`) containing just the RFC footer edit.

## What this skill must NOT do

- **Decide wave→sprint granularity.** The RFC's own `Sprint:` fields on each
  ticket decide that — the skill transcribes, it never re-derives.
- **Decide the bug pull-in.** Step 2 surfaces candidates with evidence; the
  operator decides which (if any) actually get pulled in at the step-3 gate.
- **Resolve anything the RFC defers to ticket-hardening.** That's
  `/harden-ticket`'s job at dispatch time, not buildout's — this skill files
  the tickets the RFC describes, it does not pre-flight-harden them.

## The two non-negotiable clauses

Restated here because they are the two ways this pipeline can go wrong
irreversibly:

1. **If `cw sprint plan` refuses, do not work around it.** Report the defect
   verbatim and offer to fix the RFC against `docs/rfcs/TEMPLATE.md`.
   Silently inferring what the RFC failed to say is the exact failure this
   design exists to prevent.
2. **Create nothing before the step-3 gate is approved.** `gh issue create`
   has no draft mode — a typo ships instantly, and 15 issues is a lot to
   clean up. The gate is Approve or Abort; there is no "edit" — a change
   means fixing the RFC and re-running `cw sprint plan`, not hand-patching
   `plan.json`.

## Config

`cw sprint plan` requires a `sprint_buildout:` block in
`.claude/project-config.yaml` — its absence or malformation is a hard
`RfcContractError` refusal from the CLI itself, not something this skill
guesses around. See `config/CONFIG_REFERENCE.md` for the full block shape
(`milestone:`, `epic:`, `ticket:` sections, plus the optional `notion:`
sub-block covered in pipeline step 6). Only `notion:` and the two `labels:`
lists (`epic.labels`, `ticket.labels`) are optional within an
otherwise-present `sprint_buildout:` block.

## Example

**Trigger:** "Turn RFC 0011 into tickets."

**Expected gate presentation** (step 3), using the design's own validation
case:

```
Milestone: v1.20.0 — Availability- & Counterparty-Aware Holding
Sprint 0: S1, S2
Sprint 1: A1 (Epic I), ...
Epics: I, II
Tickets: S1, S2, A1, ... (13 total: #1153–#1165 once filed)

Pull-in candidates:
  #1149 — "reap decision path" fix. Overlaps src/cw/reconcile/<module>:<line>
  (same path RFC 0011's A1/B1 build on). Recommend: pull in.

Approve to file 2 epics + 13 tickets under this milestone, or Abort?
```
