# cw — orchestrating a sprint

`cw` runs parallel Claude Code sessions to ship work across your repos. You stay the
coordinator: you harden tickets, dispatch workers, watch, and verify. Workers implement.
This guide is the operator-facing how-to; it ships with the tool, so it always matches the
version you have (`cw guide`). For internals, see the repo's `docs/`.

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
cw dev-queue cancel|remove|clear -c <client>     # queue hygiene (clear takes -s <status>)
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
cw orchestrate status|watch|workers      # orchestrator view
cw done <session> [--cleanup]            # mark completed; --cleanup removes its worktree
```
Prefer the **event bus and blocking waits** (`cw dev-queue wait`, `cw session wait`,
`cw event tail -f`) over hand-rolled timed polling. `cw watchdog install` sets up a
standalone systemd/launchd tick (escalation sweep + dispatch liveness) when no loop is
running.

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
