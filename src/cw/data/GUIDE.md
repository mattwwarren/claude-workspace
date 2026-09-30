# cw — orchestrating a sprint

`cw` runs parallel Claude Code sessions to ship work across your repos. You stay the
coordinator: you harden tickets, dispatch workers, watch, and verify. Workers implement.
This guide is the operator-facing how-to; it ships with the tool, so it always matches the
version you have (`cw guide`). For internals, see the repo's `docs/`.

## Vocabulary

One name per thing. The canonical term comes first; the names you will still
meet in older docs, events, or field names follow in parentheses. Three
different things are called a "status" — a task has one, a session has one, and
a sentinel carries one — and they never share values, so always say which.

**Work items**

- **Ticket** — the tracker issue (GitHub issue or Linear ticket). `ticket_id`
  is its number or key. What you enqueue, harden, approve, and close.
- **Task** (queue row, `TicketTask`, "entry") — the dev-queue record for one
  ticket on one client. `cw dev-queue tasks` prints one row per task; a ticket
  that is requeued keeps its row. `add`, `remove`, `cancel`, `clear`, and
  `prune` all act on tasks, addressed by ticket id.
- **Client** (project) — one configured repo in `clients.yaml`, selected with
  `-c <client>`. `cw init` calls it a project; the config calls it a client.
- **Lane** — a scheduling boundary inside a client: its own `max_parallel`
  concurrency cap, `reap_policy`, and executor overrides. `--lane`, `cw lane`.

**Runtime**

- **Session** — cw's record of one worker run in `sessions.json`: `session_id`
  (short hex), client, purpose, worktree, branch, `surface_ref`,
  `claude_session_id`, `last_result`, and a **session status** of its own:
  `active`, `idle`, `backgrounded`, `completed`, `timed_out`.
- **Worker** — the process behind a session: a `claude --bg` daemon session, or
  a codex, opencode, or aider run. Docs say "worker" for the thing doing the
  work and "session" for cw's record of it; they are one object. (In
  `cw orchestrate workers` and `--parent`, "worker" narrows to a session an
  orchestrator session spawned.)
- **Surface** (`surface_ref`) — the daemon-side handle for a worker: the
  native daemon's short id in `~/.claude/daemon/roster.json`. Its id-space is
  different from the cw `session_id` and from `claude_session_id`
  (the transcript file name) — three ids, one worker.
- **Worktree** — the git checkout a worker is homed in, one per client and
  ticket. **Occupied** means a live session or roster worker is homed there.
- **Purpose** — a static label stamped on a session at spawn (`impl`,
  `orchestrate`, `fix`; `idea`/`debt` are interactive-only). It never changes
  and is *not* the pipeline stage: read `stage` for where the work is.
- **Stage** — where a task is in the pipeline: `harden` → `plan` → `impl` →
  `review` → `finalize`. Both the task and the session carry it. The
  `/auto-dev` skill numbers its internal steps (Stage 0 Intake … Stage 5 CI
  wait) and the sentinel's `stage_reached` uses `stage1_plan` … `stage5_post_create`;
  those are the worker's own bookkeeping, not the queue's `stage`.

**Task status** (`cw dev-queue tasks` STATUS column; `QueueItemStatus`)

- `pending` — eligible for the next dispatch tick.
- `running` — claimed; a session is, or should be, live. Holds a lane slot.
- `blocked_on_user` — **parked**: waiting on an operator. Holds a lane slot.
  "Park" is the verb, `blocked_on_user` is the status, and `disposition`
  says why.
- `awaiting_operator_signoff` — parked for a `--signoff operator` gate before
  it ships. Holds a lane slot; cleared by a second `approve`.
- `completed` / `failed` / `cancelled` — terminal. The row frees its slot.

**Why a row stopped** — three fields, read in this order:

- **Disposition** (`disposition`, DISPOSITION column) — the one-word reason
  the row reached `completed`, `blocked_on_user`, or `failed`: usually the
  sentinel status (`shipped`, `plan_pending_approval`, …), otherwise a
  reconcile reason (`attempt_cap_blocked`, `stale_dispatch_gate`,
  `finalize_gate_held`, `awaiting_operator`, …). Cleared on requeue.
- **Blocked reason** (`blocked_reason`, REASON column) — the sentinel's
  `blocker.reason` (`ci_check_failed`, `dirty_worktree`,
  `codex_must_fix_findings`, …), copied onto the row when a `blocked`-shaped
  sentinel parked it. Absent when the park carried no blocker.
- **Advisory note** (`advisory_note`, the same REASON column with a `?`
  prefix) — a live, per-tick hint on a row that is *not* parked. Clears itself.
- **Gate** — a checkpoint the pipeline will not pass without evidence: plan
  approval, review approval, operator signoff, the merge gate, the finalize
  hold, the attempt ceiling. A gate that stops a row parks it;
  `cw dev-queue approve` supplies the evidence. `finalize_gate_held` is the
  deliberate `--hold-finalize` stop; `drain --held` releases only
  `awaiting_operator` parks, never that hold.

**Results**

- **Sentinel** (result, `AUTO_DEV_RESULT`, `AutoDevResult`) — the
  `<<<AUTO_DEV_RESULT … >>>` JSON block a worker emits at the end of a stage.
  "The sentinel" and "the result" are the same artifact; `AutoDevResult` is
  its parsed form. It is the only authoritative statement of what a run did.
- **Sentinel status** (`status` inside the sentinel; §4 of
  `docs/headless-contract.md`) — `shipped`, `stage_complete`, `no_op`,
  `blocked`, `merge_pending`, `merge_gate_blocked`, `plan_pending_approval`,
  `review_pending_approval`, `ambiguities_pending_resolution`,
  `premises_pending_verification`, `scope_exceeded`, `forbidden_area`,
  `empty_diff_blocked`, `stale_dispatch`. Not a task status.
- **Blocker** (`blocker.reason`, `blocker.retry_eligible`, `blocker.details`) —
  the structured payload inside a `blocked` sentinel. The task's
  `blocked_reason` is a copy of `blocker.reason`.
- **`last_result`** — the sentinel as recorded on the session (`cw session
  result`). It is written through one door; `last_result_source` names which:
  `emit_cli` (`cw result emit`), `stop_hook_harvest`, `executor_direct`,
  `git_synthesis`, `salvage_transcript`.
- **Emit** — a worker printing the sentinel into its transcript, or `cw result
  emit` staging it onto the session. **Harvest** — reconcile reading a
  finished session's sentinel and routing it to the task. **Salvage** —
  reconstructing an outcome for a session that died with no sentinel from its
  branch and transcript.

**Operator actions**

- **Approve** (`cw dev-queue approve`) — records approval on the row
  (`plan_approved_at` + `plan_approved_fingerprint`, bound to the draft as it
  stands) and moves the row past a plan, review, signoff, or finalize-hold
  gate. `--post-marker` posts an audit-only comment that nothing reads back.
- **Requeue** (`cw dev-queue requeue`) — `blocked_on_user` → `pending`, at the
  same stage or `--stage`/`--regress`; `--from-cancelled`, `--from-failed`,
  `--from-completed` recover terminal rows.
- **Unblock** (`cw dev-queue unblock`) — requeue plus clearing the session's
  salvage-park markers (`last_result`, `reap_reason`).
- **Signoff** — the `awaiting_operator_signoff` gate armed by `add --signoff
  operator` or a lane's `signoff:`; distinct from plan/review approval.

**Attempts** (two counters, one gate)

- `attempts` (ATTEMPTS column) — claims, one per stage entry. A healthy
  ticket accrues them as it advances. **Gates nothing.**
- `unproductive_attempts` (UNPRODUCTIVE column) — claims that left `running`
  with no evidence of progress. The attempt ceiling (`global_attempt_ceiling`,
  overridden per lane by `attempt_ceiling`) compares *this* counter and parks
  the row `attempt_cap_blocked`.

**Reconcile** (what the sweeps are called)

- **Reconcile** — the pass every `cw status`, `cw list`, and dispatch tick
  runs: compare `sessions.json`, the queue, and the daemon roster, then act
  on drift. There is no background daemon of cw's own.
- **Phantom** — a `running` session whose surface is gone from the roster.
- **Stalled / idle** — a live session whose transcript has stopped advancing
  (liveness buckets `live`, `stale_15m`, `stale_30m`, `stale_45m`). The idle
  sweep routes a staged result or pages `session_unresponsive`.
- **Leaked worker** — a roster entry whose session is already terminal;
  stopped every tick. (The runbook's "worktree leak" is stray commits or
  files in a worktree — unrelated.)
- **Orphan** — a session that finished before reconcile read its sentinel,
  or a codex review whose driver crashed (`cw codex migrate-legacy`).
- **Reap** — the destructive act on a phantom or stalled session: revert the
  row to `pending`, stop the daemon surface, remove the worktree. Under the
  default `reap_policy: signal_only` reconcile only *proposes*
  (`session.reap_proposed`) and parks the row; `reap_policy: auto` on the lane
  or an explicit `cw doctor --reap` performs it.
- **Wedge** — `cw doctor`'s word for a lane that cannot make progress
  (`wedge/<class>` findings); `--reap` is the remedy.

**Which command reads what**

- `cw status`, `cw list`, `cw session show` — sessions.
- `cw dev-queue status`, `cw dev-queue tasks` — the queue (task status).
  `BLOCKED` in the status summary counts `blocked_on_user` only.
- `cw board` — lanes × stages. `cw queue peek` — a liveness verdict per
  `running` session. `cw session result` — one session's `last_result`.

## Orient (start of every sprint)

```bash
# from the target repo (cw operates by configured path, not your cwd)
cw dev-queue status      # expect empty / known state
cw doctor                # expect: status healthy
```

If you just pulled new cw code, reinstall with a real rebuild — `uv tool install --force`
alone serves a STALE cached build when the version string is unchanged:

```bash
uv tool install --force --reinstall --no-cache "<path-to-claude-workspace>[mcp]"
cw orchestrate run --help   # verify the NEW command exists — don't trust `cw --version`
cw upgrade-workers          # restart daemon workers so they pick up the new binary
```

## The toolkit

**Harden first.** Before dispatching any non-trivial ticket, run the `harden-ticket` skill:
it sweeps the ticket against real code, resolves the technical ambiguities, escalates the
genuine forks, and posts a binding **Pre-flight Resolutions** comment the worker reads. This
is the single biggest lever for first-try ships.

> **What makes resolutions binding is a marker, not the heading.** The plan stage greps
> the live-fetched ticket comments and body for the literal string
> `<!-- auto-dev-preflight-resolutions -->`. Only a source carrying that marker is injected
> into the plan agent's prompt as binding constraints; anything else is ordinary comment
> text the agent may or may not follow. `harden-ticket` appends it for you.
>
> **If you write a resolutions comment by hand, you must append the marker yourself.**
> Omitting it fails silently — the comment still reads as authoritative to a human, the plan
> agent often complies anyway out of good judgment, and nothing anywhere reports that the
> mechanism never engaged. Two related consequences: the plan is also told to *omit* its
> `## Pre-flight Resolution Conformance` section when no marker-bearing source exists, so a
> reviewer's "missing conformance section" finding is a false positive in that case — check
> for the marker before acting on it.
>
> Use **exactly one** marker-bearing source per ticket. Two trip a multi-marker gate. When
> re-resolving on a later round, post one consolidated comment restating every prior
> resolution plus the new ones, and leave the earlier unmarked comments as history.

**Skills** (ship with the repo; each wraps a whole operator motion):
- `harden-ticket` — pre-flight a ticket before dispatch (above).
- `sprint-buildout` — turn a hardened RFC into filed GitHub tickets (drives `cw sprint plan|apply`).
- `cw-fanout` — enqueue a batch, start the loop, and monitor the whole wave to terminal.
- `cw-queue-peek` — inspect RUNNING sessions; WAIT / PEEK / STOP verdict per session.
- `cw-session-watch` — "did session X finish, and how?" (sentinel status, PR, routing).
- `cw-validate-result` — forensic PASS/FAIL on a finished run's AUTO_DEV_RESULT sentinel.
- `cw-followup` — do whatever a finished run's sentinel says next (close no_op, ship a
  merge_gate_blocked branch, draft Decisions, escalate a blocker).
- `cw-smoke-test` — one-ticket end-to-end dogfood of the `/auto-dev --headless` pipeline.
- `orchestrate-sprint` — run a whole sprint as a long-lived orchestrator session (harden →
  dispatch → monitor → triage → handoff), composing the skills above.
- `queue-issues` — pick open tickets from the tracker and enqueue them for dispatch.

**Dispatch:**
```bash
cw dev-queue add <ticket…> -c <client> -s large|small \
    [--lane <lane>] [--signoff operator]        # enqueue (large = more auto-approval)
cw dev-queue run --once                          # one dispatch tick (drop --once to loop)
cw dev-queue serve                               # dispatch loop with auto-restart on crash
cw dev-queue approve <ticket> -c <client>        # clear a plan/review/signoff gate
cw dev-queue requeue <ticket> -c <client>        # BLOCKED_ON_USER → PENDING (--stage/--regress;
                                                 #   --from-cancelled / --from-failed to recover)
cw dev-queue unblock <ticket> -c <client>        # clear salvage-park markers and requeue
cw dev-queue move <ticket> -c <client> --to <lane>   # re-lane
cw dev-queue cancel|remove -c <client>           # queue hygiene, one ticket at a time
cw dev-queue prune --older-than <days> -c <c>    # bulk-retire stale terminal rows (dry-run
                                                 #   by default; needs --confirm to delete)
cw dev-queue clear -c <client> [-s <status>]     # DESTRUCTIVE: previews by default; needs
                                                 #   --confirm to delete. With no -s, excludes
                                                 #   RUNNING/BLOCKED_ON_USER/
                                                 #   AWAITING_OPERATOR_SIGNOFF; name one via -s
                                                 #   to delete it explicitly. Bulk-only --
                                                 #   prefer `prune` for age-based cleanup.
cw dev-queue refresh-all                         # fast-forward main on every client repo
```

**Lanes & authority:**
```bash
cw lane add|ls|pause|resume|rm           # manage lanes; per-lane concurrency + reap_policy
cw orchestrate start --lane <name>       # bind a lane's reap authority (records the binding)
cw orchestrate run   --lane <name> [--once]   # cw-side loop: authorize reaps for the lane
```
`orchestrate run` only matters for **signal_only** lanes — under `reap_policy: auto`,
reconcile already self-heals, so the loop is an idempotent no-op there.

**Watch & inspect:**
```bash
cw watch                                 # live work board (j/k, p=peek, c=spawn-complete, o=open)
cw board [--once]                        # lane x stage pipeline cockpit (--once for a snapshot)
cw status                                # live sessions
cw dev-queue status|tasks [--json]       # queue summary / typed per-task rows
cw queue peek [-c <client>]              # RUNNING sessions: age, idle gap, WAIT/PEEK/STOP verdict
cw peek <session> [-n <lines>]           # tail a worker's output — no manual transcript digging
cw session show|result|wait <session>    # one session's state / last sentinel / block-until-status
cw dev-queue wait <ticket> -c <client>   # block until terminal; sentinel-aware exit codes
cw event tail [-f] [--type <t>…]         # orchestrator event bus (poll or follow)
cw doctor [--reap]                       # health; --reap clears a wedged lane
cw orchestrate status|workers            # orchestrator view (`orchestrate watch` is
                                         #   deprecated: use `cw board`)
cw done <session> [--cleanup]            # mark completed; --cleanup removes its worktree
```
Prefer the **event bus and blocking waits** (`cw dev-queue wait`, `cw session wait`,
`cw event tail -f`) over hand-rolled timed polling. `cw watchdog install` sets up a
standalone systemd/launchd tick (escalation sweep + dispatch liveness) when no loop is
running.

> **Silence is not proof of life — run `cw queue peek` at checkpoints.** Every event on the
> attention stream is a *failure* event: something the pipeline noticed and named. A session
> that claims a ticket, registers in the roster, reports RUNNING and then simply stops
> produces none of them, and is indistinguishable from a healthy session doing slow work.
>
> One case is worth knowing by name: **a session waiting on a subagent will not page you at
> all.** `session_unresponsive` fires only when there is no sentinel *and* no pending
> subagent spawn (`docs/events.md`), so a spawn that never completes suppresses the distress
> signal indefinitely rather than for a bounded window.
>
> `cw queue peek` is the positive check. Read `idle_m` (minutes since the session's last
> transcript **record**) against `age_m`: `idle_m` well below `age_m` means it is producing
> output; `idle_m ≈ age_m` means it has done nothing since spawning. Run it after a gate,
> after a merge, and before telling anyone a wave is healthy — not on a timer.

**Push channels (MCP):** `cw queue-channel serve` (default `127.0.0.1:8789`; also hosts the
`cw-operator` topic) and `cw pr-channel serve` (default `127.0.0.1:8788`) push events into
subscribed Claude sessions; wire `cw queue-channel proxy`, `cw pr-channel proxy`, and
`cw operator-channel proxy` into `.mcp.json` (examples in `config/*.mcp.json.example`).
The `cw-operator` channel is the low-volume "operator should look at this" stream — see
`docs/operator-channel.md`.

**Find a worker's transcript** — `cw peek <session>` covers most reads. For the raw file
(canonical — do NOT grep the daemon roster by the cw session id; it keys on the daemon
short-id, a different id-space):
cw `session_id` → `sessions.json` `surface_ref` / `claude_session_id` →
`~/.claude/projects/<encoded-cwd>/<csid>*.jsonl`.

## Sprint recipe

1. **Orient** (above): fetch + reset to main, reinstall cw (`--reinstall --no-cache`), confirm
   `cw dev-queue status` empty and `cw doctor` healthy.
2. **Scope**: take the epic; split into sub-tickets if large (note sequential deps). Starting
   from an RFC? `cw sprint plan|apply` (or the `sprint-buildout` skill) files the ticket block.
3. **Harden** each sub-ticket → post Pre-flight Resolutions. Escalate only genuine product/
   architecture forks (one batched question, with a recommendation); resolve technical things.
4. **Dispatch**: `cw dev-queue add <id> -c <client> -s large` → `cw dev-queue run --once`
   (or `serve` for a self-healing loop). The `cw-fanout` skill does steps 4–6 for a whole
   batch in one motion.
5. **Watch**: `cw dev-queue wait <id>` / `cw watch` / `cw queue peek` (status + >25-min
   transcript silence). Gates park as BLOCKED_ON_USER — clear with `cw dev-queue approve`.
6. **Verify on terminal**: read the worker's OWN sentinel (assistant/`tool_result`, never the
   prompt's illustrative example) — `cw session result <session>` or the `cw-validate-result`
   skill — run the gate, check the PR. Sequential deps: harden N+1 against post-N main and
   reinstall cw before dispatching it.
7. **Salvage** if a worker dies mid-run (rate limit, or a turn that never completes after the
   sentinel): the branch is usually pushed — verify the full gate in a clean worktree
   (`git worktree add /tmp/v origin/dev/<n>`), then `gh pr create` + `gh pr merge --squash --auto`.
   Pure local compute; no model quota. The `cw-followup` skill automates this per sentinel;
   `cw dev-queue unblock` (salvage-parked) and `requeue --from-cancelled/--from-failed`
   put recovered tickets back in the queue.

   > **Salvaging by hand? Check what branch you are standing on first.** `/ship-it` and
   > `/prep-pr` push `git branch --show-current` — correct in a normal dev session, wrong in
   > an orchestrator session, where you are on the *session* branch and not the feature
   > branch you mean to ship. Delegating from there pushes the session branch. `/ship-it`
   > also resolves to whichever project's `ship-it.md` the cwd belongs to, which in a
   > cw-managed worktree is cw's own — not the client repo's — and it hardcodes
   > `gh pr merge --auto --squash`, which will arm auto-merge even when you meant to leave
   > the PR open.
   >
   > The fix is one step, not an exception: **check out the feature branch before
   > delegating** (or cut it yourself, as when shipping orchestrator-authored work). All
   > three failure modes come from the same wrong assumption about where you are standing.
   > Otherwise create the PR directly with `gh pr create` from the correct branch.
8. **Clean up**: `cw done <dead-session> [--cleanup]`; `cw worktree gc` for squash-merged or
   closed branches; `cw dev-queue clear -c <client> -s completed` (one status per call);
   `cw doctor` to confirm green.

## Gotchas

- **Stale binary:** `uv tool install --force` caches by version string; use `--reinstall
  --no-cache` and verify by invoking the new subcommand's `--help`, not `cw --version`.
- **Turn-never-completes:** a worker can ship the PR + emit a real sentinel, then its turn
  hangs → the task is stuck `running` and never routes. Detect via silence + a role-filtered
  sentinel + a real merged PR; salvage and clean up.
- **Sentinel example:** the `/auto-dev` prompt embeds an illustrative result. Any monitor/parser
  reading raw transcript text will latch it as "shipped." Always role-filter to the worker's
  own output.
- **Diverged main:** check `git branch --show-current` and `git fetch` before reading ranges; an
  unpushed local commit or a worktree-vs-checkout mixup can look like divergence.
- **Confusing `claimed=0`:** a lane cap filled by `BLOCKED_ON_USER` tasks can report a misleading
  skip reason — read it skeptically rather than assuming a stuck dispatcher.
- **Operator answer, not observation:** a gate answer and monitor events arrive through the
  same channel, both as tool results. When an answer lands in the same turn as a burst of
  monitor events, it reads as one line among many instead of an instruction — execute the
  authorized action (`cw dev-queue approve`/`requeue`, plus any tracker-side evidence) as the
  *next* step, before summarizing queue state. Narrating the events first is the sign the
  answer was never executed.
- **`approve` is approval evidence, bound to one draft:** `cw dev-queue approve` stamps
  `plan_approved_at` + `plan_approved_fingerprint` on the row, and the plan stage's
  Checkpoint 1 reads them as operator approval (the row path). It accepts the row while the
  approved draft is unchanged, or while no operator-authority comment postdates the approval
  and the persisted `body_sha` still matches the live ticket body. Edit the body or post a
  new operator comment *after* approving and the re-dispatched plan stage re-parks
  `plan_pending_approval` quoting both fingerprints — a stale-approval re-ask, not operator
  inaction. `--post-marker` posts an audit-only comment that nothing reads back. The comment
  path (`<!-- auto-dev-comment-approval -->` as its own reply) is the CLI-free alternative;
  Linear-tracked tickets have only the row path.
- **Monitor noise drowns the signal:** a watcher that emits on every status change reports
  `pending → running` inside a single stage — roughly half of all events, none actionable.
  Key emission on stage transitions plus arrivals at `blocked_on_user`; let the periodic beat
  carry `cw queue peek` for liveness, and use `cw event tail --type`/`--dedup-terminal` to
  narrow the stream yourself.
