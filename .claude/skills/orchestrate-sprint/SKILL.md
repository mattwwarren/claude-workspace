---
name: orchestrate-sprint
description: Conductor skill for running a sprint or queue of work as a long-lived orchestrator session — sets the orchestrator discipline up front, then carries a body of work through harden → dispatch → monitor → triage → handoff while keeping the main thread's context pristine. Loads /cw-orchestration first, then composes the existing cw skills (queue-issues, harden-ticket, cw-fanout, cw-queue-peek, cw-followup) and the session.needs_attention event bus into one steady-state loop. Use this whenever the user wants to "orchestrate a mountain of work with me", run or babysit a sprint, drive a dev-queue of tickets to done, set up a long-running orchestrator/monitor session, or says things like "let's run the queue", "be my orchestrator", "dispatch these and watch them and tell me when something needs me", or "kick off the sprint and hand off when you fill up". Reach for it even when the user names only one phase (just "monitor the queue", just "harden and dispatch these") — this skill is the home for the whole arc and the discipline that makes it good.
---

# orchestrate-sprint

The conductor. You are running a sprint as an **orchestrator session**: a body
of work comes in (a backlog, a queue, a "mountain"), more arrives as you
validate what's going out, and your job is to keep it all flowing — hardening
tickets so they plan first-try, dispatching them to headless `/auto-dev` via
cw, watching the queue, triaging what the pipeline parks (escalating to a human only genuine product/scope forks), and handing off cleanly
before you run out of room.

This skill adds no new parsing or validation logic. It composes pieces that
already exist:

- **state-to-command map** → `/cw-orchestration` (load first: which `cw`
  command releases each queue state, and who runs it)
- **work-list** → `/queue-issues` (select + enqueue ready tickets)
- **pre-flight** → `/harden-ticket` (one ticket plans first-try)
- **dispatch + wave watch** → `/cw-fanout` (enqueue N, run the loop, watch)
- **in-flight health** → `/cw-queue-peek` (WAIT/PEEK/STOP ladder)
- **attention stream** → the `session.needs_attention` event bus (`cw orchestrate watch`)
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

1. **Read cw state through cw; delegate only what has no cw view.** Queue
   and session state comes from `cw dev-queue show <T> -c <C>`,
   `cw dev-queue tasks -c <C>`, `cw queue peek -c <C>` and
   `cw session result <ref>`. Each is one cheap, authoritative read, so run it
   yourself rather than sending an agent to reconstruct it. Code sweeps, log
   pulls, confidence checks and "what does this file do" still go to a
   subagent that returns a *verdict*, not a file dump. Pin the model
   explicitly (`model: sonnet` for analysis/review, `haiku` for pure lookups);
   never fan out Opus. Never use Read Only Helper for `gh` or `git` (its
   tools are Read, Grep and Glob). Anything that needs `gh` or `git` runs
   directly, or in a `general-purpose` agent. Accept an agent's verdict when
   it cites its evidence, or read the source yourself, not both. If you catch
   yourself about to Read a third file into the main thread, stop — that's a
   subagent.

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

5. **Batch decisions; don't dribble them.** When a ticket needs a human decision,
   resolve every technical/convention question yourself (you're ≥70% sure and
   the alternative is just wrong) and surface only the genuine product/scope
   forks — all of them at once, each with a recommendation and a one-line
   trade-off. One good `AskUserQuestion` beats five round-trips. This is what
   `/harden-ticket` does per ticket; do it for the sprint too.

6. **Park the un-unblockable; don't blindly re-dispatch it.** Some tickets
   cannot be moved by a worker — they're gated on an external HAR capture, a
   credential, a product decision, a dependency PR. The autonomous loop will
   faithfully re-attempt such a ticket *forever*, dying the same way each time
   and holding a lane slot while it does (this is the classic queue wedge).
   When you spot one, harden what *is* resolvable, write the gate down
   explicitly on the ticket, pull it out of the queue, and un-gate it
   yourself where an AI session can (merge the dependency PR, capture the
   artifact, supply the credential from the environment). Only a genuine
   product/scope fork goes to the human. Blind re-dispatch is not a strategy
   for a gated ticket.

7. **Act, then report.** When an agent result, a watch event, or a queue check
   tells you something new, decide and execute — including side-effectful
   actions (closing a ticket, stopping a session, dispatching a wave) —
   after the evidence the runbook names for that action (see *Decision
   ownership* below). Act, then report: say what you did and why
   afterwards; there is no pre-action approval gate. Only the escalations
   under *The human decides* below pause for a person.

Orchestrator command shapes: do not prescribe command substitution, shell loops
or jq filter flags in commands you run; the allowlist and the rule are in
docs/dispatch-runbook.md, section Orchestrator permission allowlist.
tests/test_orchestrator_command_shapes.py enforces it.

## Decision ownership

Rule 5 says batch decisions to the human; this section is the reference
table it points at — the line between what you simply do and what you ask.

Invoking `/orchestrate-sprint` is the standing authorization to approve
in-scope gates (ADR-0020): there is no kickoff question, and the human-kept
gates listed under *The human decides* stay excluded.

**The orchestrator decides and acts, without asking:**

- Dispatch and enqueue timing — including running `/cw-fanout` immediately
  after a successful harden (Phase 3).
- Inline gate-closure re-dispatch during a wave (`/cw-fanout` Step 4b already
  does this).
- Tracker bookkeeping — status transitions, labels (e.g. `auto-dev`),
  cross-links, and adding filed tickets to the sprint page.
- Filing a new ticket for scope discovered during hardening.
- Arming, stopping, and de-duplicating the attention watch (Phase 4).
- Parking a gated ticket and pulling it from the queue (rule 6).
- Force-pushing a cw-owned branch (one the pipeline created for the ticket;
  never a shared branch, never from a headless worker) with
  `--force-with-lease`, only after a clean rebase onto `origin/main` and
  `git cherry -v HEAD origin/<branch>` showing no `+` line (no remote-only
  work missing from the rebased branch).
- Recovery commands, each only after the evidence named next to it:
  `cw spawn close --confirmed-dead <id>` (flag before the id) only once the
  session is verified dead — absent from `~/.claude/daemon/roster.json`,
  transcript flat, no live process ("work is done" is not death evidence;
  still in the roster with a live process is stalled-but-live — use
  `/cw-queue-peek`'s bare-close path or surface the STOP);
  `cw doctor --reap --yes --routed-session-id <id>` on a routed-result
  session (which stays in the roster by definition) once its routed result
  is on record, its transcript is flat and no live process works in its
  worktree;
  `cw dev-queue cancel` only after verifying the ticket is satisfied by a
  merged PR/commit or is a verified duplicate/obsolete (cite it); discarding
  uncommitted/unpushed worktree changes only after `git -C <worktree> log
  origin/<branch>..HEAD`, `git -C <worktree> status --porcelain
  --untracked-files=all` and `git -C <worktree> diff HEAD` show duplicates of
  landed work or junk (otherwise commit and push); and
  `cw dev-queue requeue/approve/unblock`, `cw lane resume` once the blocker
  is read and resolved.
- Resolving missing-data items an AI session can resolve (fetch the artifact,
  read the log, file the follow-up ticket).
- Recording the answers to a plan-stage ambiguities or premises park with
  `cw dev-queue resolve`, answered from the ticket's sources of truth, and
  fixing a row's lane with `cw dev-queue move` (forms in `/cw-orchestration`).
- Stopping a wedged session per the peek-stop ladder (`/cw-queue-peek`'s
  explicit STOP rows).
- Approving a plan or review gate whose plan stays within the ticket's agreed
  scope (`cw dev-queue approve`). A ticket's size is not a scope question:
  the gate recipes already release most such parks without a page. This does
  not cover the human-kept gates listed below (`AWAITING_OPERATOR_SIGNOFF`,
  `finalize_gate_held`, `scope_hint: large`) — notify the human instead.
- Adjudicating codex MUST_FIX findings against the ticket's sources of truth
  (`/cw-followup`): settle an out-of-scope, already-decided or
  non-reproducible finding with `cw review settle <file> --reason <why>
  --ticket <ID> -c <CLIENT>`, citing the source (that records it on the cw
  dev-queue row on every tracker; then mirror the printed marker onto the
  ticket with your own tool), and requeue a real in-scope defect into the fix
  loop.

**The human decides (product/scope forks and opted-in gates only):**

- Genuine product/scope forks where either answer is defensible
  (`/harden-ticket`'s escalation criteria), including a plan or review
  finding that would grow scope beyond the ticket or needs behavior the
  sources of truth do not decide.
- A forbidden-area touch (migrations, auth/security core; since v4 CI/CD
  files and shared base classes are not forbidden areas) only when it is
  itself such a fork; otherwise
  adjudicate it against the ticket's sources of truth.
- Acceptance criteria that ask for data the system does not have and no
  session can obtain, where the answer changes product intent.
- Changes to sprint direction or composition.
- Gates the human opted into: `signoff: operator` lanes/tickets
  (`AWAITING_OPERATOR_SIGNOFF`), finalize force-holds (`hold_finalize`,
  `finalize_gate: manual`, `finalize_gate_held`), and `scope_hint: large`
  (the operator's own "gate this ticket" flag). You never approve or release
  these; notify the human. A large-by-size gate *without* `scope_hint: large`
  is not one of these — you adjudicate it.
- Force-push or history-rewriting actions on a shared branch (a cw-owned
  branch is yours, above).

A useful tell: if you catch yourself ending an orchestrator turn with "want
me to dispatch?", that IS the signal — dispatch.

## The loop

A sprint is phases you move through and then *hold* in a steady state. You will
not do these strictly once or strictly in order — you'll be monitoring while
hardening the next batch while a wave runs. But this is the spine.

### Phase 0 — Set the frame (once, at kickoff)

Load `/cw-orchestration` first, before any cw command. Its state-to-command
map is how you read and release every queue state below.

State the discipline you're operating under so the human knows what to
expect: you'll read cw state through cw, delegate code reading to sonnet,
keep implementation in auto-dev, surface decisions in batches, and hand off
before you fill. Confirm the client and the
source of the work-list (a Linear project, a label, an explicit ticket list).

### Phase 1 — Build the work-list

Use `/queue-issues` to select ready tickets from the tracker and stage them.
Don't enqueue everything reflexively — a ticket that's obviously human-gated
(no spec, open design question, missing dependency) should be hardened or parked
*before* it ever takes a lane slot.

**Swing or fan-out?** Decide the shape before the slots. A *fan-out* is N
independent tickets in N worktrees (`/cw-fanout`). A *swing* is one epic whose
children share a design or a merge order, dispatched as ONE parent ticket with
the children inlined under `## Children` (`/graduate-plan` swing track): one
plan, one worktree, one PR. Children that would each re-derive the same design,
or whose fifth PR cannot be reviewed because the first four already merged, are
a swing. Expect a swing's plan stage to park on consolidated scans more than
once; answer from the ticket's sources of truth and approve on the row only when
the park is approval-only.

### Phase 2 — Harden before dispatch (targeted, #1655)

For ordinary tickets, **dispatch first**: round 1's consolidated park (#1650)
is the hardening sweep — grounded in the code at dispatch time, covering both
scope ambiguity and plan-quality findings in one comment, with the draft
persisted (#1649) so round 2 resumes the same plan instead of regenerating.
Answer the one comment; ship on round 2.

Reserve `/harden-ticket` for the targeted cases its skill names: multi-task
plan docs whose literal code the worker transcribes verbatim, tickets
defining public contracts (new event types, schemas, `--json` output),
tickets already bouncing, and waves that must run with zero mid-wave
interrupts. Crucially: **if the ticket already has fresh
auto-dev plan comments, read them instead of re-sweeping** (rule 3). Harden
by reconciling what's already known + resolving the forks, not by re-running
the whole sweep.

### Phase 3 — Dispatch the wave

Use `/cw-fanout` to enqueue the hardened batch, start the dispatch loop, and
begin the wave watch. A swing is one row in that batch (`cw dev-queue add
<parent> -c <C> --lane <lane>`); never enqueue its children alongside it. Respect lane caps — a lane at its cap with one healthy
ticket running is *correct*, not a wedge. Don't over-dispatch to "go faster";
you'll just starve yourself of attention bandwidth.

### Phase 4 — Monitor (steady state)

Arm **one** `Monitor` on the attention stream, once per sprint:

```
Monitor(command: "cw orchestrate watch -c <client>",
        description: "<client> attention stream",
        timeout_ms: 1800000)
```

Never use ScheduleWakeup, cron or sleep polling; arm one Monitor per sprint.
Arm it with `timeout_ms: 1800000` and re-arm only when it expires. Its flags:
`--lane` (repeatable), `--ticket` (repeatable), `--all-lanes`, `--since`
(start after an event id), `--stuck-after-minutes` and `--force`; pass
`--all-lanes` unless you are deliberately scoping.

`cw orchestrate watch` is a persistent, silent process. It follows the event
bus in process and prints one line per event that needs you, each ending in
the command to run next (`| next: <cmd>`), and **nothing at all while
everything is healthy — silence means healthy.** You get a Monitor
notification exactly when there is a line to triage. Do not poll on top of it.

**Re-arm only on the Monitor's expiry notice, never per event.** The harness
caps every Monitor at 30 minutes (`timeout_ms: 1800000` is the ceiling), so
the Monitor ends on its own about every half hour; re-arm it then, with the
same command. The process saves its position after each printed line, so a
re-arm (or a restart of any kind) resumes exactly where the last run stopped:
nothing replayed, nothing lost.

**Read the first line.** It is always the resolved scope, on stdout so the
Monitor shows it:

```
WATCH | scope | client=acme | lanes=fast | tickets=- | source=focus | start=resume | cursor=attention-acme-1f2e3d4c
```

Always check the first line for `lanes=` (and `client=`) before you trust
the silence. **The default scope
is your focus-pointer lane** (`cw focus set <client>/<lane>`; `source=focus`),
or the whole client when you have no focus lane (`lanes=*`, `source=client`).
That default is right when you own one lane. If you are the only orchestrator
on the client, or you dispatch into more than one lane, widen it:
`--all-lanes` watches every lane, and `--ticket <id>` (repeatable) adds a
ticket in any lane (a `--ticket`-only scope shows `lanes=-`). Getting this
wrong reads exactly like health. (Real incident: `#382` sat 105 minutes in a
stalled session while the orchestrator reported "monitor armed, silence means
healthy" — the watch was scoped to `default` and the ticket was in `debt`.)
The stream also resolves a lane-less event (liveness, phantom revert) through
the ticket's row lane, so a scoped watch no longer silently drops those.

**Widening or narrowing the scope starts a new position.** The saved position
is keyed by the resolved scope, so changing the focus lane or adding
`--all-lanes` / `--ticket` at a re-arm starts at `start=now`: events from the
gap between the old scope and the new one are not delivered. `start=now` on
the first line is the visible tell; run `cw queue peek` once to cover the gap.

**One watcher per scope.** Stop the old Monitor (`TaskStop`) before you arm a
replacement. A second `cw orchestrate watch` on the same scope refuses to start
with `WATCH | exited: another watcher (pid <pid>) owns cursor ...` while the
first is alive. After a harness restart the recorded pid may have been reused
by an unrelated process, which looks alive too — a recycled pid cannot be
detected. Confirm no watcher is really running (`pgrep -af "cw orchestrate
watch"`), then re-arm: a second watch on the same scope needs `--force`.

**A `WATCH | exited: <reason>` line means the stream is down** (a fatal error,
a signal, or a refused start): fix the cause and re-arm. That, and the expiry
notice, are the only times you re-arm. (`WATCH | exited: signal SIGTERM` next
to the expiry notice is just the Monitor ending the process on schedule.)

What it pages on: `session.needs_attention`, `operator.escalation`,
`session.timed_out`, `session.reap_proposed`,
`session.stage_timed_out_retried`, `session.phantom_reverted` and
`session.liveness_changed` (`stale_30m`/`stale_45m` only). A repeated page —
the same ticket (or lane) parked the same way, including the per-lane
"starved lane" page and the `dispatch_loop_stale` page — is suppressed until
something resolves it (the ticket transitions or is requeued, the lane is
resumed, dispatch ticks again), so a parked session does not spam you. Events
arrive to *you*; you triage; you push the human only what a product/scope fork
or opted-in gate requires of them.

**STUCK lines.** A PENDING ticket whose blocker will not clear on its own — a
tripped lane breaker with no auto-resume scheduled, a paused lane, the ssh-key
or disk-pressure gate — is reported once as

```
STUCK | 2026-10-18T14:03:00 | #ACM-7 | lane_circuit_paused | pending 15m | lane default circuit breaker tripped (spawn_errors); no auto-resume is scheduled | lane=default | next: cw lane resume acme default
```

after `--stuck-after-minutes` (default 15). `next: -` means there is no `cw`
remedy (unlock the SSH key, free disk). The age counts from when this watcher
first saw the row in that state, and that memory does not survive a re-arm:
a row that stays stuck is reported again roughly once per Monitor re-arm.
Caps, backoff and usage limits clear themselves and never page.

**Silence rule.** Nothing changed means no output. Emit one CKPT line only
when something changed. Its shape is
`CKPT | <ticket-or-lane> | <what changed> | <action taken or next>`.
Between Monitor lines, say nothing and start no timed checks of your own.
Leave PR review and CI watching alone unless the operator asks; the review
pipeline owns it.

**Waiting on specific tickets.** `cw dev-queue wait` is a safe long wait: it
keeps waiting through dispatch's own hand-offs (a fix-loop hand-off, an
unrouted plan/review approval result) instead of exiting early, defaults to a
6-hour ceiling (`--timeout 0` = no limit), and takes several tickets with
`--any`. Exit 4 is any human-kept gate (operator signoff, finalize hold,
`scope_hint: large`); exit 5 is a stuck PENDING ticket (or an approval result
dispatch has not routed for 300 s), with the reason and next command printed.
**Run it as a background task** (`run_in_background: true`): a foreground
tool call hits the harness's own timeout long before the 6-hour ceiling. A
background `cw dev-queue wait` is for sequencing work, not for watching: the
Monitor is the watch.

**A worker that spawns and then stalls (#2004 narrowed this gap).** The stream
subscribes to `session.liveness_changed`, filtered to `stale_30m`/`stale_45m`,
so a session that claims a ticket, registers in the roster, reports RUNNING,
and then does nothing eventually pages you once the reconcile liveness sweep
crosses a bucket boundary. That leaves a narrower residual gap: the window
before the first `stale_30m` crossing (and before the first liveness-sweep tick
runs at all), during which the session is indistinguishable from a healthy one
that is simply busy.

So still run `cw queue peek --client <client>` at each natural checkpoint —
after a gate, after a merge, before you report things are fine —
as a **confirmation** step rather than the only line of defense. It computes
`idle_m` (minutes since the session's last transcript *record*) alongside
`age_m`, and `idle_m ≈ age_m` is the signature of a worker that never did
anything. That is a positive-liveness check; the watch is a negative one, and
you need both. **Do not hand-roll a liveness check** — peek already computes
the number correctly, and an ad-hoc `find`-based substitute is easy to get
subtly wrong (`find -newermt` parses its argument in *local* time, so passing
a UTC timestamp on a non-UTC box yields a cutoff hours in the future that
matches nothing and reports a confident, false all-clear).

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
  parts, batch the real forks to the human (rule 5). Often `/harden-ticket`
  reactively, then re-dispatch. A plan or review approval park whose plan
  stays within the ticket's scope is yours to `cw dev-queue approve` (and
  usually a gate recipe already released it); codex MUST_FIX findings are
  yours to adjudicate via `/cw-followup`.

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
  duplicate row that later surfaces as `terminal_sibling` noise). For a
  `plan_pending_approval` / `review_pending_approval` park, `cw dev-queue
  approve` is itself the delta: it records the approval on the queue row,
  which is the evidence the plan stage reads. A prose approval comment is not.
  For an `ambiguities_pending_resolution` / `premises_pending_verification`
  park at the plan stage, `cw dev-queue resolve` records your answers on the
  row and requeues it in one step: the recorded resolution is the delta.

  Route to `/harden-ticket` when the operator flags hardening or a ticket
  parks twice on the same disposition.
- **Premise-pending-verification on an external unknown** → verify the
  premise yourself (pull the log / artifact / dependency state). If it
  genuinely cannot be resolved, park it, write the gate down, and escalate
  only if it is a product/scope fork (rule 6).
- **Stalled / retry-cap / reap_proposed** → a wedge. Stop the session
  (`cw spawn close --confirmed-dead <id>` only after the death checks: absent
  from the roster, flat transcript, no live process; see `/cw-queue-peek` for a
  stalled-but-live session), then decide remove-vs-requeue. (`cw doctor --reap`
  clears dead sessions, after the same liveness evidence, but will *not*
  auto-revert a BLOCKED_ON_USER task — for those, run the close+remove
  yourself.)
- **Long review stage on a clean-mode client** (`review.convergence: clean`,
  #2787) → a long review stage with a falling new-finding count is healthy:
  `/cw-queue-peek`'s clean-mode convergence exemption stops the age rules from
  flagging a live session, so do not stop it for age alone. A stalled or idle
  one is still a peek signal. A clean-mode ticket must be dispatched through
  `cw dev-queue` (`cw dev-queue add <T> -c <C> --lane <lane>`): an interactive
  `/auto-dev` or `/auto-dev-review` on that client refuses. The parks it can
  land on each have a release command in the `cw-orchestration` skill's state
  map: `confirm_backend_unavailable` (restore the backend, then requeue),
  `review_not_converging` (a commit releases `no_progress`; an operator settle
  then a requeue releases `oscillation`), `review_blocked` (the operator raises
  `review.max_cycles`, then a requeue) and `review_operator_actionable` (run
  the checklist, settle any re-raised finding, then a requeue).
- **Shipped / merge-gate** → `/cw-followup`.

Use `/cw-followup`, `/cw-validate-result`, and `/cw-queue-peek` as the actual
hands here; your job is the routing.

### Phase 6 — Hand off before you fill

This is non-optional and easy to skip until it's too late. At ~80% context, or
when the open decisions outnumber what you can hold, run `/handoff` — capture
the live ticket list, the gate each parked ticket sits at, the armed watch
(its scope line), and the next action per in-flight ticket. Carry state, not
recipes: the next session reads each ticket with `cw dev-queue show <T> -c <C>`
and re-arms `cw orchestrate watch -c <C>` per Phase 4. Leave out hand-edits
of the queue store, fixed `wait` timeouts and workarounds for a cw bug; file
the bug instead. A clean handoff is what makes
the *next* orchestrator session as good as this one. Running yourself into a
context wall and dying mid-wave is the one failure mode that wastes everyone's
work.

## When to break containment (the honest exceptions)

"Contain code-gen to auto-dev" and "delegate code reading" are defaults with a
high prior, not absolutes. Name the exception out loud when you take it:

- **Codifying judgment** (writing a skill, a CLAUDE.md rule, a Pre-flight
  Resolutions comment, a runbook). The artifact *is* your reasoning; delegating
  it loses the fidelity that's the whole point. Author these yourself.
- **A genuinely trivial, in-context edit** where dispatching a whole auto-dev
  run costs more than the fix — and you already have the file open for a reason
  the work already warranted. Still prefer a ticket if it touches anything shared.
- **An emergency** (prod is down, the queue itself is wedged) where the pipeline
  is too slow and you need to act directly. Surface it, then act.

If you're reaching for an exception more than occasionally, that's a smell —
the work probably wants to be a ticket.

### Shipping orchestrator-authored work — check your branch first

When an exception above leaves you with a real diff to ship (a codified skill, a
runbook edit), `/prep-pr` and `/ship-it` are still the right path — but they
assume something that is false in an orchestrator session.

Both push `git branch --show-current`. In a normal dev session that is the
feature branch. **In an orchestrator session you are standing on the session
branch of a cw-managed worktree**, so delegating from there pushes the session
branch, not the work. `/ship-it` also resolves to whichever project's
`ship-it.md` your cwd belongs to — in a cw-managed worktree that is cw's own,
which knows nothing about a client repo's PR conventions — and it hardcodes
`gh pr merge --auto --squash`, arming auto-merge even when you meant to leave
the PR open for review.

Three symptoms, one cause: an assumption about where you are standing.

**Cut and check out a feature branch off the freshly-fetched default branch
before delegating.** That single step makes all three correct — the push targets
the right ref, the cwd's project is the one you are actually shipping to, and
auto-merge is a deliberate choice rather than a surprise. If you cannot, create
the PR directly with `gh pr create` from the correct branch and skip the
delegation.

## Anti-patterns

- **Re-sweeping a ticket that already has plan comments.** The sweep is in the
  thread; read it.
- **Re-dispatching a gated ticket without a tracker-state delta.** It will wedge again. Park it, supply the delta, then release it.
- **Re-arming the stream per event.** The Monitor stays armed; re-arm only on
  its 30-minute expiry notice or a `WATCH | exited:` line (Phase 4).
- **Two watchers on one scope.** Stop the old Monitor before arming a new one;
  use `--force` only after confirming the recorded pid is not a live watcher
  (Phase 4).
- **Trusting silence without reading the scope line.** The default is your
  focus lane; if you own more than one lane, pass `--all-lanes` or `--ticket`
  and check `lanes=` on the first line (Phase 4).
- **Treating watch silence as proof a worker is alive.** The stream subscribes
  to `session.liveness_changed` (`stale_30m`/`stale_45m`), so a
  spawned-then-stalled worker eventually pages — but the residual gap
  (pre-`stale_30m`, or before the first liveness-sweep tick) still needs
  confirmation. Run `cw queue peek` at checkpoints and read `idle_m` vs
  `age_m` (Phase 4).
- **Polling on top of the stream.** The event bus is push; trust the silence
  once the scope line matches the work you dispatched (Phase 4).
- **Running `cw dev-queue wait` in the foreground.** It now waits up to 6
  hours; run it as a background task (Phase 4).
- **Dribbling decisions.** Five one-question round-trips for what could've been
  one batched `AskUserQuestion`.
- **Asking the same question twice.** Ask a recurring either/or question once
  per sprint, then apply the answer.
- **Handing the operator an orchestrator command.** If `/cw-orchestration`
  marks a command `Who: orchestrator`, run it yourself instead of asking the
  operator to.
- **Timer polling.** `ScheduleWakeup`, cron, sleep loops or a periodic peek
  on top of the armed Monitor (Phase 4).
- **Over-dispatching.** More running ≠ faster done; it's just less attention per
  ticket and a higher wedge rate.
- **Reading raw material into the main thread.** The third file Read is a
  subagent you forgot to spawn.
- **Skipping the handoff.** Dying at the context wall mid-sprint.

## Related skills

- `/cw-orchestration` — state-to-command map and cw vocabulary (load first)
- `/queue-issues` — select + enqueue ready tickets (Phase 1)
- `/harden-ticket` — pre-flight one ticket to plan first-try (Phase 2)
- `/cw-fanout` — dispatch N + watch the wave (Phase 3/4)
- `/cw-queue-peek` — in-flight WAIT/PEEK/STOP ladder (Phase 4/5)
- `/cw-followup` — act on a finished session's sentinel (Phase 5)
- `/cw-validate-result` — forensic read of one session (Phase 5)
- `/handoff` — clean session transition (Phase 6)
