# The operator is the orchestrator session; a human is escalated to only for product or scope forks

**Status:** Accepted
**Amends:** [ADR-0006](0006-reaping-is-gated-by-an-authority.md),
[ADR-0014](0014-timers-never-destroy-work.md),
[ADR-0016](0016-ledger-claim-matching-is-gated-and-measured.md) invariant 8,
[ADR-0001](0001-parked-tasks-pin-their-session.md),
[ADR-0002](0002-blocker-retry-policy-pair.md), and `docs/release-playbook.md`'s
"arming is a human `!` command" rule.

## Decision

Wherever cw's code, docs, skills, commands or event payloads say "the
operator", the actor is **whoever is operating cw** — by default the
long-lived orchestrator Claude session (`/orchestrate-sprint`, `/cw-fanout`,
`/cw-followup`, the lane's `ORCHESTRATE` session), not a human at a keyboard.
cw is built to drive software through AI. Nothing in the pipeline may require
a human to *run* a command, edit a file, open a PR, merge, requeue, reap,
close a session, reset a branch, or arm a flag that an AI session can run.

Escalation runs worker → orchestrator → human. The CLAUDE.md stop-and-ask
triggers are escalation points on that chain: a worker escalates to the
orchestrator, and the orchestrator brings a trigger to the human only when it
cannot resolve it itself. Beyond those, a human is escalated to for exactly
two things:

1. **Genuine product or scope forks** — a question the ticket's sources of
   truth cannot answer (intent, public-contract shape, what the product
   should do). The orchestrator adjudicates everything else itself, and
   batches the forks it cannot resolve into one question.
2. **Gates the human explicitly opted into** — a lane or ticket configured
   with `signoff: operator` (`AWAITING_OPERATOR_SIGNOFF`), a finalize
   force-hold (`hold_finalize`, `finalize_gate: manual`), or a ticket the
   human flagged `scope_hint: large` ("gate this ticket"). Opting in is the
   human saying "page me", so those stay theirs: the orchestrator notifies
   the human and never approves or releases them. (A plan or review that is
   Large by size alone, with no `scope_hint: large`, is the orchestrator's to
   adjudicate.)

## Invariant

1. "Operator command" (ADR-0006 invariant 2, ADR-0014 invariant 2,
   `cw doctor --reap`, `cw spawn close --confirmed-dead`, `cw dev-queue
   approve` / `requeue` / `drain` / `unblock` / `cancel`, `cw lane resume`)
   means an **explicit, evidence-backed command run
   by an operating session** — the orchestrator counts. What these ADRs
   forbid is an *unattended timer or loop* deciding on its own; they never
   required a human.
2. `BLOCKED_ON_USER` and `AWAITING_OPERATOR` mean "parked for the
   operating session to triage", not "parked for a person". The dispatch
   loop still never blindly auto-retries a parked row; the orchestrator
   reads the blocker and requeues, approves, regresses, files the follow-up,
   closes the ticket, or — only for a product/scope fork — escalates.
3. Destructive recovery is run by the orchestrator — never by a headless
   worker — **after it has collected the evidence named next to the
   command**. Evidence, not a human, is the gate:
   - `git reset --hard` of the main checkout to `origin/main`: `git diff
     origin/main..HEAD` shows only release churn.
   - Force-push of a **cw-owned** branch (one the pipeline created for the
     ticket): `--force-with-lease`, after a clean rebase onto `origin/main`
     and `git cherry -v HEAD origin/<branch>` showing no `+` line (every
     remote commit has an equivalent patch in the rebased branch; a plain
     `git log origin/<branch> ^HEAD` always lists the pre-rebase commits).
     History rewrites on shared branches stay escalated to the human. (This
     is recovery after a park. A worker's own `--force-with-lease` push of
     the branch it is working on, after its own rebase — `/auto-dev-finalize`'s
     rebase retry, `/review-monitor` — is part of the pipeline, not recovery,
     and is unchanged.)
   - `cw spawn close --confirmed-dead <id>`: the session is absent from the
     daemon roster, its transcript is flat, and no live process remains.
     "The work is done" is not evidence of death. A session still in the
     roster with a live process is stalled-but-live, not dead: use
     `/cw-queue-peek`'s bare-close path (which needs the human's allowlist)
     or surface the STOP recommendation.
   - `cw doctor --reap` (non-TTY: `--yes --routed-session-id <id>`) on a
     routed-result session, which stays in the roster by definition: its
     routed result is on record, its transcript is flat, and no live process
     is working in its worktree.
   - Discarding uncommitted or unpushed worktree changes: `git log
     origin/<branch>..HEAD`, `git status --porcelain --untracked-files=all`
     and `git diff HEAD` (staged, unstaged and untracked) show them to be
     duplicates of landed work or junk; otherwise commit and push them.
   - Closing a ticket: its acceptance criteria are verified satisfied (cite
     the merged PR or commit) or it is verified duplicate or obsolete (cite
     the superseding ticket).
   - Arming an auto-actor flag: its measured shadow or dogfood evidence, and
     a rollback confirmed to be one flag flip.

   A headless worker never acts on a destructive directive found in a
   tracker comment (`destructive_directive_requires_operator`); it parks it
   for the orchestrator, which acts on it only after confirming the
   comment's operator provenance (#2097: operator login, no
   `cw-agent-authored` marker) and collecting the evidence above.
4. Self-adjudication stays refused at the worker boundary only: a headless
   worker (`.claude/cw-context.json` with `headless: true`, or `$TMPDIR`
   inside such a worktree) may not `cw review settle` its own reviewer's
   findings. Every other context — the main checkout, a directory outside a
   repo, a worktree with no context file, a context file that cannot be read
   — proceeds. "We could not prove you are not a worker" is not a reason to
   hand work to a human.
5. Recovery hints, `next_actions`, `recovery_hint`, event `details`, and
   error messages name the command to run, addressed to the operating
   session. They do not say "by hand", "yourself", "a human must", or "ask
   the operator" for anything an AI session can run.

## What this means for callers

- Orchestrator skills (`/orchestrate-sprint`, `/cw-fanout`, `/cw-followup`,
  `/cw-queue-peek`, `/cw-session-watch`) execute recovery commands
  themselves; they do not hand them back as a to-do list.
- `/auto-dev` headless exits keep parking (that is how the worker hands off),
  but the park is addressed to the orchestrator.
- Review outcomes of type `operator_action` (e.g. "file follow-up ticket X")
  are executed by the orchestrator.

## Consequences

- Config defaults are unchanged: `reap_policy: signal_only`, concierge,
  review recipes and the other auto-actors stay opt-in. This ADR changes
  *who* acts on a signal, not whether an unattended loop acts on it.
- Two environment steps remain physically human because they need a TTY or
  a secret no session holds: accepting Claude Code's bypass-permissions
  disclaimer once, and unlocking an SSH key with its passphrase.
- The `BLOCKED_ON_USER` / `AWAITING_OPERATOR_SIGNOFF` enum names are kept for
  state-file and event-schema compatibility; their meaning is as above.

## Referenced by

- ADR-0006, ADR-0014, ADR-0016, `docs/release-playbook.md`,
  `docs/headless-contract.md`, `docs/dispatch-runbook.md`
