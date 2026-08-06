---
name: orchestrate-sprint
description: Conductor skill for running a sprint or queue of work as a long-lived orchestrator session — sets the orchestrator discipline up front, then carries a body of work through harden → dispatch → monitor → triage → handoff while keeping the main thread's context pristine. Composes the existing cw skills (queue-issues, harden-ticket, cw-fanout, cw-queue-peek, cw-followup) and the session.needs_attention event bus into one steady-state loop. Use this whenever the user wants to "orchestrate a mountain of work with me", run or babysit a sprint, drive a dev-queue of tickets to done, set up a long-running orchestrator/monitor session, or says things like "let's run the queue", "be my orchestrator", "dispatch these and watch them and tell me when something needs me", or "kick off the sprint and hand off when you fill up". Reach for it even when the user names only one phase (just "monitor the queue", just "harden and dispatch these") — this skill is the home for the whole arc and the discipline that makes it good.
---

# orchestrate-sprint

The conductor. You are running a sprint as an **orchestrator session**: a body
of work comes in (a backlog, a queue, a "mountain"), more arrives as you
validate what's going out, and your job is to keep it all flowing — hardening
tickets so they plan first-try, dispatching them to headless `/auto-dev` via
cw, watching the queue, triaging what needs a human, and handing off cleanly
before you run out of room.

This skill adds no new parsing or validation logic. It composes pieces that
already exist:

- **work-list** → `/queue-issues` (select + enqueue ready tickets)
- **pre-flight** → `/harden-ticket` (one ticket plans first-try)
- **dispatch + wave watch** → `/cw-fanout` (enqueue N, run the loop, watch)
- **in-flight health** → `/cw-queue-peek` (WAIT/PEEK/STOP ladder)
- **attention stream** → the `session.needs_attention` event bus (`scripts/attention_monitor.sh`)
- **act on a finished session** → `/cw-followup`
- **clean exit** → `/handoff`

What this skill *is* that those aren't: the **discipline** that holds the whole
loop together, and the **sequencing** that decides which piece to reach for
next. The pieces are the instruments; this is the score.

## The prime directive: keep the orchestrator's context pristine

Your context is the one scarce, non-renewable resource in a long sprint. Every
file you read, every log you tail, every diff you pull into the main thread is
context you can't get back — and when it fills, the session ends and someone
has to rebuild your state from a handoff. So the entire discipline below is in
service of one rule: **the orchestrator reads conclusions, not raw material.**

Think of yourself as a conductor who never picks up an instrument. You decide
what plays when; the agents and pipelines do the playing.

### The rules, and why they exist

1. **Delegate nearly everything to sonnet agents.** Searches, log pulls, code
   sweeps, confidence checks, "what does this file do" — all of it goes to a
   subagent that returns a *verdict*, not a file dump. Pin the model explicitly
   (`model: sonnet` for analysis/review, `haiku` for pure lookups); never fan
   out Opus. The agent burns its own context on the raw material; you keep only
   its conclusion. If you catch yourself about to Read a third file into the
   main thread, stop — that's a subagent.

2. **Contain code generation to auto-dev / headless cw sessions.** The point of
   the pipeline is that implementation happens in isolated, test-gated,
   reviewable worktrees — not in your conversation. When you feel the pull to
   "just write the fix here," that's the signal to harden a ticket and dispatch
   it instead. The exceptions are real (see *When to break containment* below)
   and worth naming out loud when you take them.

3. **Read conclusions, not file dumps.** A ticket body + its existing
   `/auto-dev` plan comments are cheap, high-signal context — read those. The
   2,000 lines of code the plan was derived from are not — leave them in the
   agent that already read them. If a ticket already cycled through auto-dev
   planning, the sweep is *already done and sitting in the comments*; re-running
   a Plan Reviewer just re-derives it into your context. Read the artifact that
   exists before you generate a new one.

4. **Pull the empirical artifact before trusting a diagnosis.** auto-dev's plan
   stage reasons from *code* and can produce a fluent, confident,
   **wrong** diagnosis — it has no eyes on runtime. Before you act on "the
   nightly fails because X" or "this errors due to Y", delegate a pull of the
   actual log / metric / CI run. (Real example: two plan runs blamed a CI
   failure on Azure-network timeouts; the actual run log showed a single-worker
   wall-clock timeout — a completely different fix. The code-reasoning was
   plausible and wrong.) Logs are evidence; fluent prose is not.

5. **Batch decisions; don't dribble them.** When a ticket needs the operator,
   resolve every technical/convention question yourself (you're ≥70% sure and
   the alternative is just wrong) and surface only the genuine product/scope
   forks — all of them at once, each with a recommendation and a one-line
   trade-off. One good `AskUserQuestion` beats five round-trips. This is what
   `/harden-ticket` does per ticket; do it for the sprint too.

6. **Park the un-unblockable; don't re-dispatch it.** Some tickets cannot be
   moved by an agent — they're gated on an external HAR capture, a credential,
   a product decision, a dependency PR. The autonomous loop will faithfully
   re-attempt such a ticket *forever*, dying the same way each time and holding
   a lane slot while it does (this is the classic queue wedge). When you spot
   one, harden what *is* resolvable, write the gate down explicitly on the
   ticket, pull it out of the queue, and tell the operator what's needed to
   un-gate it. Re-dispatch is not a strategy for a human-gated ticket.

7. **Surface observations before acting on them.** When an agent result, a
   monitor event, or a queue check tells you something new, say what you learned
   and what you propose *before* you execute — especially for anything
   side-effectful (closing a ticket, stopping a session, dispatching a wave).
   The operator needs to see the decision point, not just the aftermath.

## Decision ownership

Rule 5 says batch decisions to the operator; this section is the reference
table it points at — the line between what you simply do and what you ask.

**The orchestrator decides and acts, without asking:**

- Dispatch and enqueue timing — including running `/cw-fanout` immediately
  after a successful harden (Phase 3).
- Inline gate-closure re-dispatch during a wave (`/cw-fanout` Step 4b already
  does this).
- Tracker bookkeeping — status transitions, labels (e.g. `auto-dev`),
  cross-links, and adding filed tickets to the sprint page.
- Filing a new ticket for scope discovered during hardening.
- Arming, stopping, and de-duplicating the attention monitor (Phase 4).
- Parking a human-gated ticket and pulling it from the queue (rule 6).
- Stopping a wedged session per the peek-stop ladder (`/cw-queue-peek`'s
  explicit STOP rows).

**The operator decides:**

- Genuine product/scope forks where either answer is defensible
  (`/harden-ticket`'s escalation criteria).
- Acceptance criteria that ask for data the system does not have.
- Force-push or history-rewriting actions on shared branches (`cw-followup:106`
  — "Confirm with the user before force-push" — the existing precedent for
  this bucket).
- Whether to fold a bug discovered during hardening into an in-flight ticket
  or spin a new one.
- Changes to sprint direction or composition.

A useful tell: if you catch yourself ending an orchestrator turn with "want
me to dispatch?", that IS the signal — dispatch.

## The loop

A sprint is phases you move through and then *hold* in a steady state. You will
not do these strictly once or strictly in order — you'll be monitoring while
hardening the next batch while a wave runs. But this is the spine.

### Phase 0 — Set the frame (once, at kickoff)

State the discipline you're operating under so the operator knows what to
expect: you'll delegate to sonnet, keep implementation in auto-dev, surface
decisions in batches, and hand off before you fill. Confirm the client and the
source of the work-list (a Linear project, a label, an explicit ticket list).

### Phase 1 — Build the work-list

Use `/queue-issues` to select ready tickets from the tracker and stage them.
Don't enqueue everything reflexively — a ticket that's obviously human-gated
(no spec, open design question, missing dependency) should be hardened or parked
*before* it ever takes a lane slot.

### Phase 2 — Harden before dispatch

For anything past a trivial one-liner, run `/harden-ticket` first. A ticket that
plans first-try costs one run; a ticket that bounces on ambiguities can burn
4–6. The leverage here is enormous and it's the cheapest quality you'll buy all
sprint. Crucially: **if the ticket already has fresh auto-dev plan comments,
read them instead of re-sweeping** (rule 3). Harden by reconciling what's
already known + resolving the forks, not by re-running the whole sweep.

### Phase 3 — Dispatch the wave

Use `/cw-fanout` to enqueue the hardened batch, start the dispatch loop, and
begin the wave watch. Respect lane caps — a lane at its cap with one healthy
ticket running is *correct*, not a wedge. Don't over-dispatch to "go faster";
you'll just starve yourself of attention bandwidth.

### Phase 4 — Monitor (steady state)

Arm the attention monitor **via the Monitor tool, with `persistent: true`**:

```
Monitor(command: "bash ~/.claude/skills/orchestrate-sprint/scripts/attention_monitor.sh <client> [<lane>]",
        description: "cw attention events for <client>", persistent: true)
```

A `persistent: true` Monitor survives a `/clear` resume in the same process —
so on resume, check for a leftover attention Monitor (via the Monitor tool's
task list) and stop it before arming a new one, to avoid a duplicated event
stream.

This is the one mechanism that works, and getting it wrong is a silent,
sprint-long blind spot. **Do NOT arm it with a backgrounded `Bash`
(`run_in_background: true`)** — a backgrounded bash script's stdout only lands
in an output file that nothing reads, so every `needs_attention` / `reap_proposed`
event dies unseen and "silence" becomes a lie. Only the Monitor tool turns each
emitted line into a chat notification. (Real incident: a whole session ran with a
backgrounded-bash monitor; a severe ticket crashed to `failed/abandoned` and the
orchestrator only found out on a manual queue poll.) The script itself is a thin
wrapper over the first-class `cw event tail --follow --client <c> --dedup-terminal
--type session.needs_attention …` command — it adds `--since now` and a readable
one-line format; you could inline that `cw event tail … --json` in the Monitor
command directly if you don't need the pretty output.

It is a persistent watch on the `session.needs_attention` / `operator.escalation`
/ `timed_out` / `reap_proposed` / `phantom_reverted` event bus for your client,
deduped so a parked session doesn't spam. Events arrive to *you*; you triage; you
push the operator only what changes what they'd do next. Use `/cw-queue-peek` for
the WAIT/PEEK/STOP verdict on any session running long. **Silence means healthy —
but only once you've confirmed the monitor is armed via the Monitor tool.** With
that confirmed, the monitor covers the failure signatures, so no news genuinely
is good news (don't poll on top of it).

### Phase 5 — Triage attention events

When an event lands, classify it the way the peek-stop ladder and
`session-disposition.md` do, then act:

Before triaging, read `attempts=N` for what it actually is: the counter
reflects claims consumed — one per PENDING→RUNNING claim per pipeline stage —
not retry count. A clean plan→impl→review→finalize ticket consumes roughly
four attempts with zero failures along the way. The ceiling you're watching for
is a single shared counter across normal stage progression, plus up to two
finalize self-heal regressions (REVIEW→FINALIZE `agent_block` auto-regress),
plus any true retries. A `failed`/`abandoned` result sitting at the cap should
be read in light of that arithmetic — it is not evidence of "N real failures."

- **Blocked on user / ambiguities / plan-approval** → resolve the technical
  parts, batch the real forks to the operator (rule 5). Often `/harden-ticket`
  reactively, then re-dispatch.

  **Human-gated parks are not retry-eligible without a tracker-state delta
  (#1653).** A ticket parked on `ambiguities_pending_resolution` /
  `premises_pending_verification` / `plan_pending_approval` /
  `review_pending_approval` may be re-dispatched ONLY after its tracker state
  has changed since the park — a new operator comment, a body edit, or an
  approval reply. A retry without a state delta feeds a timer-driven loop:
  it burns an attempt, re-derives the identical park, and pages you again to
  learn nothing (observed: 10 mechanical retries on a fixed ~2h39m cadence,
  ~20.5h, ending in manual queue removal). Your job as orchestrator is to
  supply the delta (answer/harden/approve on the ticket) and THEN release —
  via `cw dev-queue requeue`/`approve`, never by `cw dev-queue add` (a
  re-add against a parked row is refused and would otherwise mint a
  duplicate row that later surfaces as `terminal_sibling` noise).
- **Premise-pending-verification on an external unknown** → it's human-gated.
  Park it, write the gate down, tell the operator what un-gates it (rule 6).
- **Stalled / retry-cap / reap_proposed** → a wedge. Stop the session
  (`cw spawn close`), then decide remove-vs-requeue. (`cw doctor --reap` clears
  dead sessions but will *not* auto-revert a BLOCKED_ON_USER task — those need
  the manual close+remove.)
- **Shipped / merge-gate** → `/cw-followup`.

Use `/cw-followup`, `/cw-validate-result`, and `/cw-queue-peek` as the actual
hands here; your job is the routing.

### Phase 6 — Hand off before you fill

This is non-optional and easy to skip until it's too late. At ~80% context, or
when the open decisions outnumber what you can hold, run `/handoff` — capture
the live waves, the parked-and-gated tickets (with their gates), the armed
monitor, and the next action per in-flight ticket. A clean handoff is what makes
the *next* orchestrator session as good as this one. Running yourself into a
context wall and dying mid-wave is the one failure mode that wastes everyone's
work.

## When to break containment (the honest exceptions)

"Contain code-gen to auto-dev" and "delegate everything" are defaults with a
high prior, not absolutes. Name the exception out loud when you take it:

- **Codifying judgment** (writing a skill, a CLAUDE.md rule, a Pre-flight
  Resolutions comment, a runbook). The artifact *is* your reasoning; delegating
  it loses the fidelity that's the whole point. Author these yourself.
- **A genuinely trivial, in-context edit** where dispatching a whole auto-dev
  run costs more than the fix — and you already have the file open for a reason
  the operator authorized. Still prefer a ticket if it touches anything shared.
- **An emergency** (prod is down, the queue itself is wedged) where the pipeline
  is too slow and you need to act directly. Surface it, then act.

If you're reaching for an exception more than occasionally, that's a smell —
the work probably wants to be a ticket.

## Anti-patterns

- **Re-sweeping a ticket that already has plan comments.** The sweep is in the
  thread; read it.
- **Re-dispatching a human-gated ticket.** It will wedge again. Park it.
- **Arming the monitor with a backgrounded `Bash` instead of the Monitor tool.**
  Its stdout goes to a file nothing reads — every attention event dies unseen and
  "silence" is a lie. Arm it via the Monitor tool with `persistent: true` (Phase 4).
- **Polling on top of the monitor.** The event bus is push; trust the silence —
  but only after confirming it's armed via the Monitor tool (see above).
- **Dribbling decisions.** Five one-question round-trips for what could've been
  one batched `AskUserQuestion`.
- **Over-dispatching.** More running ≠ faster done; it's just less attention per
  ticket and a higher wedge rate.
- **Reading raw material into the main thread.** The third file Read is a
  subagent you forgot to spawn.
- **Skipping the handoff.** Dying at the context wall mid-sprint.

## Related skills

- `/queue-issues` — select + enqueue ready tickets (Phase 1)
- `/harden-ticket` — pre-flight one ticket to plan first-try (Phase 2)
- `/cw-fanout` — dispatch N + watch the wave (Phase 3/4)
- `/cw-queue-peek` — in-flight WAIT/PEEK/STOP ladder (Phase 4/5)
- `/cw-followup` — act on a finished session's sentinel (Phase 5)
- `/cw-validate-result` — forensic read of one session (Phase 5)
- `/handoff` — clean session transition (Phase 6)
- `scripts/attention_monitor.sh` — the bundled event-bus watch (Phase 4)
