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
uv tool install --force --reinstall --no-cache <path-to-claude-workspace>
cw orchestrate run --help   # verify the NEW command exists — don't trust `cw --version`
```

## The toolkit

**Harden first.** Before dispatching any non-trivial ticket, run the `harden-ticket` skill:
it sweeps the ticket against real code, resolves the technical ambiguities, escalates the
genuine forks, and posts a binding **Pre-flight Resolutions** comment the worker reads. This
is the single biggest lever for first-try ships.

**Dispatch:**
```bash
cw dev-queue add <ticket…> -c <client> -s large|small   # enqueue (large = more auto-approval)
cw dev-queue run --once                                  # one dispatch tick (drop --once to loop)
cw dev-queue move <ticket> -c <client> --to-lane <lane>  # re-lane
cw dev-queue cancel|remove|clear -c <client>             # queue hygiene
```

**Lanes & authority:**
```bash
cw lane …                                # declare/list lanes; per-lane concurrency + reap_policy
cw orchestrate start --lane <name>       # bind a lane's reap authority (records the binding)
cw orchestrate run   --lane <name> [--once]   # cw-side loop: authorize reaps for the lane
```
`orchestrate run` only matters for **signal_only** lanes — under `reap_policy: auto`,
reconcile already self-heals, so the loop is an idempotent no-op there.

**Watch & inspect:**
```bash
cw status                                # live sessions
cw doctor [--reap]                       # health; --reap clears a wedged lane
cw orchestrate status|watch|workers      # orchestrator view
cw done <session>                        # mark a dead/finished session completed
```
Prefer an **event-driven monitor** on `~/.local/share/cw/dev_queue.json` (status transitions
+ transcript silence) over timed polling.

**Find a worker's transcript** (canonical — do NOT grep the daemon roster by the cw session id;
it keys on the daemon short-id, a different id-space):
cw `session_id` → `sessions.json` `surface_ref` / `claude_session_id` →
`~/.claude/projects/<encoded-cwd>/<csid>*.jsonl`.

## Sprint recipe

1. **Orient** (above): fetch + reset to main, reinstall cw (`--reinstall --no-cache`), confirm
   `cw dev-queue status` empty and `cw doctor` healthy.
2. **Scope**: take the epic; split into sub-tickets if large (note sequential deps).
3. **Harden** each sub-ticket → post Pre-flight Resolutions. Escalate only genuine product/
   architecture forks (one batched question, with a recommendation); resolve technical things.
4. **Dispatch**: `cw dev-queue add <id> -c <client> -s large` → `cw dev-queue run --once`.
5. **Watch** with an event-driven monitor (status + >25-min transcript silence).
6. **Verify on terminal**: read the worker's OWN sentinel (assistant/`tool_result`, never the
   prompt's illustrative example), run the gate, check the PR. Sequential deps: harden N+1
   against post-N main and reinstall cw before dispatching it.
7. **Salvage** if a worker dies mid-run (rate limit, or a turn that never completes after the
   sentinel): the branch is usually pushed — verify the full gate in a clean worktree
   (`git worktree add /tmp/v origin/dev/<n>`), then `gh pr create` + `gh pr merge --squash --auto`.
   Pure local compute; no model quota.
8. **Clean up**: `cw done <dead-session>`; `git worktree remove <wt> --force` for merged
   branches; `cw dev-queue clear -c <client> -s completed|cancelled`; `cw doctor` to confirm green.

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
