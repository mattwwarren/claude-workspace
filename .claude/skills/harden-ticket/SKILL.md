---
name: harden-ticket
description: >-
  Pre-flight harden a ticket BEFORE dispatching it to /auto-dev (or any
  plan→implement→review pipeline): sweep the ticket spec against the actual
  code to surface implementation-determining ambiguities and the plan-review
  MUST_FIX issues a reviewer will raise, resolve the technical ones yourself,
  batch only the genuine product/scope decisions to the operator, then post a
  single "Pre-flight Resolutions" comment so the worker passes plan review on
  the first try. Use this whenever you are about to queue or dispatch a
  non-trivial ticket (new event types, schemas, multi-condition logic, status
  or contract output, anything touching freshly-merged code) to auto-dev,
  queue-issues, cw dev-queue, or a fan-out wave — and ALWAYS use it when a
  ticket keeps bouncing back with ambiguities_pending_resolution or
  plan_unreviewable. Run it BEFORE enqueue to kill the multi-run
  ambiguity/plan-review whack-a-mole.
---

# harden-ticket

## Why this exists

The auto-dev pipeline pauses a run when it finds an ambiguity, and its plan
reviewer exits `plan_unreviewable` after a **single** revision cycle. For a
non-trivial ticket, the reviewer (and the ambiguity scanner) tends to surface
problems **one at a time, across separate runs** — each run costs ~10 minutes
plus a re-dispatch. A ticket can burn 4–6 runs converging on a plan the
operator could have nailed down in one pass.

This skill front-loads that convergence. It runs the same kind of review the
auto-dev plan reviewer runs, but *before* dispatch, surfaces everything at
once, resolves the mechanical questions itself, and asks the operator only the
decisions that genuinely need a human. The output is a resolution comment the
dispatched worker reads — so the worker plans correctly on attempt one.

Observed effect: in the run that motivated this skill, the first ticket took
6 runs (ambiguity whack-a-mole → plan-review whack-a-mole); after hardening,
sibling tickets shipped on the first or second run.

## When to run it

Run before enqueueing/dispatching when the ticket is anything more than a
trivial one-liner — especially when it:

- introduces new event types, enum values, schema/contract fields, or `--json`
  output (these become public contracts; a wrong shape is a breaking change later),
- has multi-condition logic (a reviewer will demand a precedence/decision table),
- asserts against or renders data from **freshly-merged** code (the ticket may
  reference fields/functions that don't exist, or were renamed),
- is a test ticket that replays incidents (mock seams + time-window determinism
  are easy to get silently wrong).

Also run it reactively the moment a ticket comes back
`ambiguities_pending_resolution` or `plan_unreviewable` — don't just
re-dispatch and hope.

## Workflow

### 1. Sweep — spawn a Plan Reviewer subagent

Spawn the project **Plan Reviewer** subagent (model: sonnet) to read the ticket
+ all its comments + the relevant code on the branch it will be built from
(usually current `main` — make sure the dependency PRs are merged), and report
findings. The agent must ground every finding in the real code, not the ticket
prose alone.

Use a prompt shaped like this (adapt the file list to the ticket):

```
Read-only pre-flight under-specification + plan-quality sweep of ticket #<N>
against the code, BEFORE it goes through auto-dev. Surface EVERY
implementation-determining ambiguity AND every likely plan-quality MUST_FIX up
front so the implementation passes plan review on the first try. No edits.

Read the ticket fully (`gh issue view <N> --comments`) and the code it touches
(<list the exact src + test files, incl. any dependency code just merged>).

The dispatched worker builds in a FRESH worktree off current `main` — review
against `main`, and do NOT report the orchestrator's own checkout/worktree
being behind `main` as a finding (it is not worker-facing noise). The only
branch-state issue worth flagging is a dependency PR that is genuinely NOT yet
merged to `main`.

State, with evidence, for each finding:
- QUESTION (one implementer-facing sentence)
- EVIDENCE (file:line + short quote showing why it is ambiguous/wrong)
- OPTIONS (the 2-3 realistic choices)
- RECOMMENDATION (which + one-line why, grounded in the feature's intent)

Anticipate the plan-quality classes a reviewer flags:
precedence/decision tables for multi-condition logic; data-availability at the
call/render site (does the value actually exist where the code runs?);
acceptance criteria that reference data the code/state does NOT contain;
magic strings that should be enums/consts; raw dicts that should be typed
models; network-call error handling + offline/timeout behavior; test
determinism (freezegun for time windows, the exact monkeypatch/mock seam);
function signatures + the run-site wiring; None/empty handling for every new
field; public --json schema shape; idempotency / lock-ordering.

Confirm the file set and flag any temptation to touch code outside it.
Order findings by how likely they are to actually block/mislead the worker.
If a category is genuinely clean, say so. Only REAL implementation-determining
issues — no style nits. End with a one-line verdict.
```

If the ticket has explicit scope constraints (e.g. "4 fixtures only, the 5th is
out of scope"), state them in the prompt and tell the agent to treat a plan
that violates them as wrong — reviewers and workers both drift toward the
ticket body otherwise.

### 2. Triage — resolve vs escalate

Split the findings into two buckets. The discipline here is what makes the
skill worth running: don't dump 15 questions on the operator, and don't
silently decide things that are genuinely theirs.

**Resolve yourself (state them, don't ask)** — anything that is
technical-correctness or follows an existing codebase convention:
mock seams, freezegun windows, function signatures, wiring location, error
handling that mirrors a sibling, named-constant extraction, None/empty
handling, "use the existing helper/pattern", test determinism. You are ≥70%
sure and the alternative is just wrong.

**Escalate to the operator (one batched AskUserQuestion)** — only genuine
forks where a human's intent changes the outcome:
scope (fold a bug fix in vs separate ticket), public contract shape
(per-client dict vs single value), product behavior (how much signal to
capture), and especially **acceptance criteria that ask for data that does not
exist** (scope down to what's real vs extend a producer). Lead each option
with a recommendation and one-line trade-off.

A useful tell: if the answer follows directly from the ticket's stated intent
or an established pattern, resolve it. If choosing wrong would still be a
*defensible* product/scope choice, ask.

### 3. Compose + post the resolution comment

Write one comment titled `## Pre-flight Resolutions (operator)` containing
**every** resolution — the ones you decided and the ones the operator decided —
phrased as direct instructions to the implementer. Prefer named constants and
typed models over magic numbers and raw dicts (say so explicitly; reviewers
flag these). Restate any scope exclusion verbatim. End with `Proceed.` and the
marker `<!-- auto-dev-preflight-resolutions -->`.

Post it to the issue (`gh issue comment <N> --body-file ...`). The dispatched
worker reads issue comments, so this is what makes the next plan correct.

**Re-harden (2nd+ round) — supersede, don't append.** On any harden round after
the first, do NOT append to the prior comment. Post a NEW comment titled
`## Pre-flight Resolutions (operator) — supersedes all prior` containing the
FULL consolidated numbered list (every still-valid prior resolution plus the new
ones), ending with `Proceed.` and the `<!-- auto-dev-preflight-resolutions -->`
marker. No in-place PATCH or comment-ID plumbing — a fresh superseding comment is
the single source of truth. Exactly one marker-bearing comment may be present at
dispatch: the pipeline refuses with `ambiguities_pending_resolution` when it
finds more than one, so strip the marker from (or delete) the superseded comment
by hand before re-dispatch.

### 4. Hand off

Report what you resolved, what the operator decided, and that the ticket is
ready to dispatch. **Do not dispatch** — that's the caller's job (queue-issues,
cw dev-queue, or the orchestrate-sprint flow). This skill's contract is "ticket
hardened, resolutions posted, ready to enqueue."

## Notes

- **Hardening is not perfect.** A worker may still surface 1–2 new questions
  during planning (the sweep cuts the count, it doesn't zero it). Treat those
  the same way: resolve the technical ones, escalate the real forks, post a
  fresh superseding comment (see Section 3 above), re-dispatch.
- **Front-load resolutions; don't rely on between-requeue addendums.** The
  worker plans from the ticket context materialized at dispatch (body + the
  comments present then). Put your resolutions where they will be in that
  context — the first-dispatch comment, or better, the ticket **body**.
  Resolutions appended as comments *after* dispatch only reach a re-plan because
  requeue now re-fetches comments (#837 fixed the stale-context bug that
  silently dropped them); if a ticket keeps bouncing on questions you already
  answered, suspect the worker never saw the answer — verify it's in the
  materialized context, don't just append again. When in doubt, fold the
  resolutions into the body and re-dispatch fresh. When folding resolutions into the body, the pre-flight resolutions HTML-comment marker `<!-- auto-dev-preflight-resolutions -->` moves with them — place it at the end of the body's resolutions section, exactly as it would end a marker-bearing comment. This keeps detection symmetric (marker-based) across both channels: the pipeline's Step 1b extraction (`auto-dev-plan.md`, #980) greps the body's resolutions section for the identical marker it greps comments for, and treats a marker-bearing body as the authoritative source when both channels carry it.
- **For migration-style tickets, give a deriving grep, not a hand-list.** When
  the change is "route all N call sites through helper X" / "rename every Y" /
  "add a field at every Z", do NOT enumerate the sites by hand in the
  resolution — hand-lists repeatedly miss sites (indirect assignments like
  `x.status = lookup[k]`, variants under a different variable name, sites added
  since you last looked), and each miss is another plan bounce. Instead give the
  worker the exact `grep -rEn '<pattern>' src/` that *derives* the full set,
  plus an explicit exclusion rule (e.g. "every match except `queue.py` — that's
  a different model") and a callout for any site the grep structurally cannot
  show. Make the acceptance criterion that same grep returning only the helper
  body. The inventory stays correct as the tree changes; your enumeration error
  stops being a failure mode.
- **Ground the sweep on `main`, not your worktree.** The worker gets a clean
  worktree off current `main`, so review `main` itself: ensure the dependency
  PRs are merged and `main` is fast-forwarded, then point the sweep at it. Tell
  the agent to ignore the orchestrator's own worktree being behind `main` —
  flagging that is noise (the worker never sees that checkout). Reviewing stale
  code, on the other hand, makes the agent miss real seams and the worker hit
  import errors the sweep should have caught.
- **The Plan Reviewer agent reads excerpts, not whole files** — give it the
  precise file list (and line ranges if known) so it lands on the real seams.

## Example

**Trigger:** "About to queue #461 (add last-tick + review-monitor + last-stage
to `cw status`). Harden it first."

**Sweep finds (abridged):** `last_tick` shape is unspecified (public --json
contract); the acceptance asks for PR "CI status" + "mergeable" but
`MonitoredPR` and the state file have neither field; the `last_stage`
placeholder text references a now-merged ticket and is stale; `_load_monitored_prs`
reads the wrong state key so the section is always empty.

**Triage:** resolve the placeholder wording, the empty-handling, and the
read-key bug (one-line, in-scope, makes the feature work) directly; escalate
two real forks to the operator — the `last_tick` JSON shape (per-client dict vs
single), and the missing CI/mergeable data (scope down to what exists vs extend
the producer in a future ticket).

**Output:** a `Pre-flight Resolutions` comment fixing the key bug, pinning the
per-client `TickSummary` Pydantic model, scoping the review-monitor section to
real fields, and correcting the placeholder — then "ready to dispatch."
