---
name: cw-orchestration
description: Canonical vocabulary and state-to-command map for cw (claude-workspace), the multi-session orchestrator. Load first in any orchestrator session, including /orchestrate-sprint, and before spawning a worker, dispatching a ticket, fanning work out, babysitting a run, or rescuing a wedged session. Triggers on cw, dev-queue, cw spawn, AUTO_DEV_RESULT, "dispatch", "fan out", "background worker", "headless run", and on any impulse to hand-roll claude --bg or a polling loop.
---

# `cw` — Multi-Session Orchestrator (Use It; Don't Reinvent It)

`cw` (claude-workspace) is the orchestration substrate for parallel and
autonomous Claude work: enqueue tickets, dispatch background workers, watch
them to terminal, triage the gates they park on, ship with auto-merge. When
work is dispatch-, parallel- or autonomous-shaped, reach for the cw primitive
first instead of hand-rolling a worse one.

- **Registered project** (in `~/.config/cw/clients.yaml`): drive it through cw.
- **Not registered**, but the work is cw-shaped: offer
  `cw init <name> --path <repo>` before hand-rolling. Do not silently fall back
  to shell glue.

Placeholders below: `<T>` is the ticket, `<C>` the client, `<lane>` a lane
name. Every command here is a plain single command, safe for the orchestrator
permission allowlist (no command substitution, loops or jq filters).

## Version

Written against cw 1.77.0. Run `cw --version`; if newer, trust `cw dev-queue show <T> -c <C>`'s `next:` over this list.

## State to command

`cw dev-queue show <T> -c <C>` is authoritative: its `next:` line lists the
commands that are legal for the row as it stands, and a human-kept gate is
labelled operator-only there. Read it first. This map says which of those
commands the orchestrator runs, when, and why. `cw dev-queue tasks -c <C>`
shows the first `next:` command per row in its NEXT column.

- **`pending`**: Run `cw dev-queue wait <T> -c <C>` (in the background, only to sequence work) or `cw dev-queue cancel <T> -c <C>` when the ticket is verified satisfied or obsolete. Who: orchestrator. A pending row at a lane cap is correct, not a wedge.
- **`lane_paused` / `lane_circuit_paused`**: Run `cw lane resume <C> <lane>`. Who: orchestrator. A pending row whose lane is paused; read why the lane paused (a circuit breaker trips on consecutive spawn errors) before resuming it. Resume also resets the error counter.
- **`running`**: Run `cw dev-queue wait <T> -c <C>` (background, sequencing only) or `cw queue peek -c <C>` at a checkpoint. Who: orchestrator. A STOP row from peek is handled per `/cw-queue-peek`.
- **`completed` / `failed` / `cancelled`**: Run `cw dev-queue requeue <T> -c <C> --from-completed`, `cw dev-queue requeue <T> -c <C> --from-failed` or `cw dev-queue requeue <T> -c <C> --from-cancelled`, matching the row's status, after reading `show`. Who: orchestrator. A terminal row is re-entered only on evidence that the work is not done.
- **`ambiguities_pending_resolution` / `premises_pending_verification`**: Run `cw dev-queue resolve <T> -c <C> --file <decisions.md>` at the plan stage. Who: orchestrator. Answer only from the ticket's sources of truth (ticket body, plan-of-record, recorded decisions, linked RFCs, the code); a genuine product or scope fork those sources cannot answer still goes to the human. Outside the plan stage, `cw dev-queue requeue <T> -c <C>` only after a tracker delta (#1653). A second park on the same disposition goes to `/harden-ticket`.
- **`plan_pending_approval`**: Run `cw dev-queue approve <T> -c <C>` when the plan stays within the ticket's agreed scope. Who: orchestrator. When the plan is not yet quality-reviewed, approve may requeue it at the plan stage for Plan Quality Review instead of advancing (`plan_requeued` in the output); that is not an error. `cw dev-queue requeue <T> -c <C> --stage plan` re-runs the plan instead. Never `cw dev-queue add`: a re-add against a parked row is refused.
- **`review_pending_approval`**: Run `cw dev-queue approve <T> -c <C>`. Who: orchestrator. With signoff configured, approve re-routes the row to `awaiting_operator_signoff` instead of advancing; that park is the human's. `cw dev-queue requeue <T> -c <C> --stage review` re-runs review instead.
- **`plan_scope_drift`**: Run `cw dev-queue approve <T> -c <C> --scope-drift <paths>` when the growth is operator-directed or inside the agreed scope, otherwise `cw dev-queue requeue <T> -c <C>`. Who: orchestrator.
- **`codex_must_fix_findings`**: Adjudicate each finding via `/cw-followup`. Settle an out-of-scope or non-reproducible one with `cw review settle <file> --ticket <T> --reason "<why>" -c <C>`; send a real in-scope defect back with `cw dev-queue requeue <T> -c <C> --stage impl --regress`. To ship as-is once every finding is settled, run `cw dev-queue approve <T> -c <C> --override-must-fix --reason "<why>"` then `cw dev-queue requeue <T> -c <C> --stage finalize`. Who: orchestrator.
- **`attempt_cap_blocked`**: Run `cw dev-queue requeue <T> -c <C> --reset-attempts` once the cause of the failed attempts is read and fixed. Who: orchestrator. Never hand-edit the queue store to reset the counter.
- **salvage-parked row**: Run `cw dev-queue unblock <T> -c <C>`, or `cw dev-queue requeue <T> -c <C>` to re-run the stage. Who: orchestrator. The bound session's reap reason is `salvage_parked`.
- **`awaiting_operator` / `review_health_gate` / `review_artifacts_stale`**: Run `cw dev-queue requeue <T> -c <C>`, or `cw dev-queue drain --held -c <C>` to release held parks in bulk. Who: orchestrator. `drain` releases ALL held tickets of the client, not just this one; preview with `cw dev-queue drain --held -c <C> --dry-run`.
- **`branch_behind_main`**: Rebase the branch onto the default branch first, then run `cw dev-queue requeue <T> -c <C>`. Who: orchestrator. Approve is refused here: it would ship a tree that no longer matches main.
- **`merge_gate_blocked` / `stale_dispatch`**: Land or close the open PR an earlier run left for the ticket, then run `cw dev-queue requeue <T> -c <C>`. Who: orchestrator.
- **`awaiting_operator_signoff` / `finalize_gate_held` / `scope_hint: large`**: Who: operator only. Notify the operator with the ticket, the gate and what `show` reports; do not release it yourself. These are gates the human opted into (ADR-0020). A large-by-size gate without `scope_hint: large` is not one of them; adjudicate that one as `plan_pending_approval`.
- **`blocked`**: With no bound session, run `cw dev-queue requeue <T> -c <C>` after reading the blocker (`cw session result <ref>` for a session that still exists). Who: orchestrator.
- **any other state**: Run `cw dev-queue show <T> -c <C>` and follow its `next:` line, which is the authority for every state not listed here. Who: orchestrator. That covers, for example, `scope_exceeded`, `forbidden_area`, `plan_unreviewable`, `plan_unsound`, `agent_block`, `tool_denied`, `empty_diff_blocked` and `merge_pending`. Escalate to the human only a genuine product or scope fork.

## Command semantics

- **Enqueue and placement.** `cw dev-queue add <T> -c <C> --lane <lane>` always passes `--lane`; without it the row lands in the `default` lane. `cw dev-queue move <T> -c <C> --to <lane>` fixes a wrong lane.
- **Read verbs.** `cw dev-queue tasks -c <C>` (all rows, NEXT column) and `cw dev-queue show <T> -c <C>` (one row with its legal `next:` commands) are the read verbs; `cw queue peek -c <C>` reads in-flight sessions; `cw session result <ref>` prints a session's `AUTO_DEV_RESULT` sentinel.
- **Readiness.** `cw preflight <T> -c <C>` runs the pre-flight checks for one ticket before dispatch.
- **Lane limits.** `cw lane set <C> <lane> max_parallel=N` changes a lane's concurrency. The settable keys are `max_parallel` and `attempt_ceiling` only; gate keys (`signoff`, `finalize_gate`), `reap_policy` and the guard toggles are refused (ADR-0020).
- **Client ceiling.** `cw config concurrency set ceiling=N -c <C>` sets a client's dispatch ceiling. With `-c` the only key is `ceiling`; every other key, gate keys included, is refused, and so is a run inside a dispatch worker.
- **Settling review findings.** `cw review settle <file> --ticket <T> --reason "<why>" -c <C>` records a settled finding on the row. `--ticket` and `--reason` are both required. The orchestrator runs it; it is refused inside a headless worker. It is also the `codex_must_fix_findings` route above.
- **Waiting.** `cw dev-queue wait <T>... -c <C>` takes positional tickets; several tickets need `--any` (without it exactly one ticket is allowed), which returns on the first ticket to reach an exit state. `--stuck-after-minutes N` (default 15) sets the stuck threshold and `--timeout` defaults to 6 hours (`0` = no limit). It is one-shot, not a watch: on a row that is already parked or terminal it returns on the first poll. Exit codes: 0 shipped or no_op; 1 scope_exceeded, forbidden_area, FAILED or CANCELLED; 2 blocked, merge_pending, ambiguities_pending_resolution, premises_pending_verification or BLOCKED_ON_USER; 3 ATTENTION; `exit 4` human gate (operator signoff, finalize hold, `scope_hint: large`); `exit 5` stuck (a pending blocker that will not clear on its own, or an approval result dispatch has not routed); 124 hard timeout. Run it as a background task, for sequencing only.
- **Watching.** `cw orchestrate watch -c <C>` is the one standing attention stream. Arm it once per sprint under one Monitor with `timeout_ms: 1800000` and re-arm only when it expires; the saved position resumes, so nothing is replayed or lost. It prints nothing while everything is healthy. The first stdout line is the resolved scope: check it for `lanes=`. A second watch on the same scope needs `--force`, and only after confirming no watcher is alive. Flags: `--lane`, `--ticket`, `--all-lanes`, `--since`, `--stuck-after-minutes`, `--force`. Pass `--all-lanes` unless you are deliberately scoping: the default scope is your focus-pointer lane.

## Known cw hazards

- Never edit `~/.local/share/cw/dev_queue.json` with a dispatch loop alive: a tick's read-modify-write silently drops the edit. Use `cw dev-queue requeue <T> -c <C> --reset-attempts` instead (docs/dispatch-runbook.md).
- Close the session before removing its row: `cw spawn close --confirmed-dead <id>` first, then `cw dev-queue remove <T> -c <C>`; reversed, they race the dispatcher (#317).
- After upgrading cw, restart `cw dev-queue serve` before using `cw dev-queue resolve`: an older loop rewrites the queue without the resolutions ledger and erases recorded answers (#2702, #2729).
- `cw review settle` and `cw dev-queue resolve` are refused inside a headless dispatch worker; the orchestrator runs them (#2701, #2702).
- `cw dev-queue requeue` on a row that is already PENDING is a no-op, and `--reset-attempts` is not applied there (src/cw/dev_queue/requeue.py, #2692).
- `cw dev-queue add` against a row that is pending, running or parked is refused; release a parked row with `resolve`, `requeue` or `approve` (src/cw/dev_queue/crud.py, #1653).
- Do not start `cw dev-queue run` when a dispatch loop is already live; check `pgrep -f 'cw dev-queue (run|serve)'` first. A manual tick races the live loop and can phantom-sweep in-flight sessions (#317).

## Monitor and rescue

Skills compose over the primitives; prefer them for in-flight work.

- `/orchestrate-sprint`: the conductor loop for a sprint, built on this map.
- `/cw-queue-peek`: WAIT / PEEK / STOP per RUNNING session, at a checkpoint.
- `/cw-fanout`: enqueue and watch a wave in one motion.
- `/cw-session-watch`: did one session finish, and with what status.
- `/cw-validate-result`: forensic read of what a session shipped or blocked on.
- `/cw-followup`: act on a finished session's sentinel.
- A wedged session: verify death first (absent from
  `~/.claude/daemon/roster.json`, transcript flat, no live process), then
  `cw spawn close --confirmed-dead <id>` (flag before the id). Prune merged
  worktrees with `cw worktree gc --apply`.

## Do NOT

- Hand-roll `claude --bg` spawns when a cw command does it.
- Use `ScheduleWakeup`, sleep or cron polling, or `/loop` babysitting to watch
  work. The `cw orchestrate watch` Monitor is push; silence means healthy.
- Do not run `gh pr create` or `gh pr merge` for normal pipeline-owned
  headless shipping; the pipeline ships with auto-merge itself. Preserve the
  documented `attach_ui_evidence` and `pr.auto_merge: false` recovery paths,
  where the orchestrator may run `gh pr merge` after the required recovery
  work.
- Parse `cw list` or `cw watch` output: the text columns are unstable. Use
  `cw dev-queue show <T> -c <C>`, `cw dev-queue tasks -c <C> --json`,
  `cw session result <ref>` or `cw queue peek -c <C> --json`.
- Guess sentinel semantics. The `AUTO_DEV_RESULT` schema is the contract
  (`docs/headless-contract.md`); `cw result validate -` checks a payload.

If you catch yourself typing `claude --bg`, a polling loop, a `gh pr create`,
or a manual `cw dev-queue run` tick to coordinate autonomous work, that IS the
signal: stop and check whether a cw command, or a loop that is already
running, already owns that step.
