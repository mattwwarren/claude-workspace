> Companion appendix to /auto-dev-plan. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 1 Plan — Appendix

Interactive-only procedures and design rationale extracted from
`.claude/commands/auto-dev-plan.md` (#1879). Each section is reached from a named
trigger sentence in the core doc; a headless run on the common path needs none of
it.

---

## Why an inline ambiguity scan is never a substitute for the agent spawn

None of these are valid reasons to skip the Step 1c agent spawn:

- *"Ticket is highly prescriptive — file paths, exact code, test cases."* Detail
  creates false confidence; implicit assumptions go unstated precisely because
  the author thought everything was covered.
- *"User said move without pausing / don't ask questions."* That governs
  clarifying questions to the user. The PM Reviewer runs in background and asks
  nothing of anyone.
- *"I can scan it faster myself."* The agent is cheap; a missed ambiguity is
  rework or a wrong implementation.
- *"Ticket is short / scope is small."* Small scope is not unambiguous scope.

If you catch yourself drafting prose that explains *why* the agent isn't needed
this time, that IS the signal — spawn it.

---

## Checkpoint 1 — interactive plan-approval gate

**If plan was auto-skipped** (existing plan found): skip this checkpoint
entirely.

**If plan was generated or built on partial:** present ticket summary, plan
source, file list + estimated scope, scope classification, Phase 1 test approach,
Phase 2 implementation approach, and friction highlights (skip if NONE). Then
**AskUserQuestion:** "Approve plan, adjust, or skip ticket?"

- **Approve** → proceed to Stage 2
- **Adjust** → re-plan with user's adjustments, re-present
- **Skip** → move to next ticket in queue

---

## Step 1f — the two review lenses and how they compose with Step 1c

Step 1f fires after Checkpoint 1 (approval), after the Step 1e `no_op`
short-circuit, and after Step 1c ambiguity resolutions are merged into the plan
body. Two stations, two lenses:

- **Plan Reviewer** — *is the plan specified well enough to implement?* Catches
  under-specification.
- **Plan Soundness Reviewer** — *is the plan's chosen direction sound?* Catches a
  well-specified plan that builds the wrong thing — a direction contradicting a
  codified `ARCHITECTURE.md` §7/§8 rule, or matching a known high-blast-radius
  shape.

With Step 1c (Product Manager Reviewer Mode 1 — "did the ticket leave gaps?"),
these are the plan-time pre-review: requirements, specification, direction. All
three run.
