# RFC 0001 — Native session backend

| Field | Value |
|---|---|
| Status | **Accepted — decision implemented; native `claude --bg` backend in production (see ADR-0000)** |
| Owner | @mattwwarren |
| Spike ticket | #105 |
| CLI version under test | Claude Code 2.1.148 |
| Date | 2026-05-22 |
| Phase gated | Phase 1 (#108–#110) |

## Summary

cw hand-rolls a session lifecycle on top of cmux: `Cmux.spawn` opens a multiplexer pane, a wrapper script inside the pane emits IDLE/COMPLETED sentinels via `wrapper.py`, and `reconcile.py` reaps phantoms. Claude Code 2.1.139+ ships a per-user supervisor (`~/.claude/daemon/`) that owns the same lifecycle natively. This RFC asks: do the native primitives cover cw's surface today, and what blocks Phase 1's `NativeBackend` adoption?

## TL;DR

**Decision: green-light Phase 1.** Nine of ten cw surfaces have one-to-one native equivalents — some strictly better than cw (auto-respawn, structured state.json, richer status enum). One material gap remains:

- **Multi-client env vars (`CW_CLIENT`, `CW_PURPOSE`, `CW_SESSION_ID`) do not propagate** through `claude --bg`. The dispatch envelope (`roster.json.dispatch.env`) has a slot, but the CLI doesn't populate it from the parent shell. No `--env` flag exists.

Workaround paths are listed in Row 10 and turned into a design question for #108. Not a blocker — Phase 1 can ship a NativeBackend that injects per-session settings via `--add-dir`/`.claude/settings.json` while we ask upstream for an `--env` flag.

## Native primitives discovered in 2.1.148

These are all real commands; most are hidden from `claude --help` but documented inline.

| Command | Purpose | Notes |
|---|---|---|
| `claude --bg [prompt]` | Dispatch a background session, returns short ID | Hidden from `--help`. `claude --bg --help` actually dispatches a session (the `--bg` wins). Supports `--agent`, `--permission-mode`, `--cwd`, `--add-dir`, `--worktree`, `--model`. |
| `claude agents` | TUI agent view | |
| `claude agents --json` | List sessions as JSON | `pid`, `cwd`, `kind` (`background`/`interactive`), `startedAt` (ms), `sessionId`, optional `name`, `status` (`idle`/`busy`). |
| `claude agents --cwd <path> --json` | Filter by cwd | Returns matching subset; `[]` if none. |
| `claude attach <id>` | Attach bg session to TTY | `Ctrl+Z` detaches without stopping. |
| `claude logs <id>` | Print recent bg session output | Raw PTY scrollback, not structured. For UX, not machine consumption. |
| `claude stop <id>` | Stop bg session; keep transcript | Resume later via `claude attach` / `claude --resume`. `state.json` is retained at `~/.claude/jobs/<short>/state.json`. |
| `claude rm <id>` | **Destructive:** delete session AND its worktree | "Unlike `stop`, works on already-exited sessions." |
| `claude respawn <id>` / `--all` | Restart workers to pick up new Claude binary | Auto-fixes #86 (daemon-restart-on-upgrade). |
| `claude daemon status` | Pid, version, uptime, sock paths, worker counts, what's holding daemon open | |
| `claude daemon run` | Foreground supervisor (or under launchctl/systemd) | |
| `claude daemon logs` | Tail `~/.claude/daemon.log` | |
| `claude daemon stop` | Shut down supervisor + workers | `--keep-workers` to leave detached sessions running; `--any` for transient daemons. |
| `claude daemon uninstall` | Remove service install | |

State files (canonical sources of truth):

- `~/.claude/jobs/<short>/state.json` — per-session state record (schema below)
- `~/.claude/jobs/<short>/timeline.jsonl` — append-only state transitions
- `~/.claude/jobs/pins.json` — pinned-session list (programmatic, file-based — not TUI-only as the ticket assumed)
- `~/.claude/daemon/roster.json` — live worker registry (pid, sockets, dispatch metadata, isolation mode, `attempt` counter)
- `~/.claude/daemon/dispatch/` — pending dispatch envelopes
- `~/.claude/daemon.log` — supervisor log

### state.json schema (observed in 2.1.148)

```jsonc
{
  "state": "working" | "running" | "done" | "crashed",
  "detail": "<human readable, e.g. 'exit -1; respawning'>",
  "tempo": "idle" | "blocked",
  "needs": "<message>",                  // present when blocked
  "inFlight": { "tasks": 0, "queued": 0, "kinds": [] },
  "output": { "result": "..." } | null,
  "children": null,                       // sub-sessions (untested)
  "linkScanOffset": 0,                    // transcript-tail cursor
  "linkScanPath": "<transcript .jsonl path>",
  "template": "bg",
  "respawnFlags": [],
  "providerEnv": {},                      // Anthropic provider env (not arbitrary)
  "intent": "<original prompt>",
  "sessionId": "<uuid>",
  "resumeSessionId": "<uuid>",            // usually == sessionId
  "daemonShort": "<8 hex>",
  "cliVersion": "2.1.148",
  "cwd": "<path>",
  "createdAt": "<iso>",
  "updatedAt": "<iso>",
  "firstTerminalAt": "<iso>" | null,      // set on first terminal state
  "backend": "daemon",
  "name": "<auto-named from intent or user-supplied>",
  "nameSource": "auto" | "user"
}
```

### roster.json schema (observed)

```jsonc
{
  "proto": 1,
  "supervisorPid": <int>,
  "updatedAt": <ms>,
  "workers": {
    "<short>": {
      "pid": <int>,
      "procStart": "<linux jiffies>",
      "sessionId": "<uuid>",
      "rendezvousSock": "/tmp/cc-daemon-<uid>/<id>/rv/<short>.sock",
      "ptySock": "/tmp/cc-daemon-<uid>/<id>/spare/<id>.pty.sock",
      "cliVersion": "2.1.148",
      "startedAt": <ms>,
      "attempt": <int>,                  // bumps on auto-respawn
      "cwd": "<path>",
      "dispatch": {
        "proto": 1, "short": "...", "nonce": "...", "sessionId": "...",
        "createdAt": <ms>, "source": "shell",
        "cwd": "<path>",
        "launch": { "mode": "prompt", "args": ["--session-id", "<uuid>"] },
        "env": {},                       // populated from dispatch envelope, NOT parent shell
        "isolation": "none" | "worktree",
        "respawnFlags": [],
        "seed": { "intent": "..." },
        "cols": 192, "rows": 12
      },
      "decModes": [...]                  // VT-100 modes from the PTY
    }
  }
}
```

## Surface-by-surface gap analysis

| cw surface (file:line) | Native candidate | Verified | Evidence |
|---|---|---|---|
| `cmux.spawn(workspace, command, surface) → surface_ref` (cmux.py:138-164) | `claude --bg [opts] [prompt]` returning short ID | ✅ | Row 1 |
| `cmux.list_surfaces() → set[str]` (cmux.py:174-198) | `claude agents --json` | ✅ | Row 2 |
| `cmux.identify() → dict` (cmux.py:170-172) | `claude agents --cwd <path> --json` | ✅ better | Row 3 |
| `cmux.close(surface_ref)` (cmux.py:166-168) | `claude stop <id>` (or `claude rm <id>`) | ✅ | Row 4 |
| `wrapper.signal_idle()` (wrapper.py:188-230) | `state.json.state==working` + `tempo==blocked` poll; `agents --json.status` | ✅ | Row 5 |
| `wrapper.signal_completed()` (wrapper.py:233-282) | `state.json.state==done` + `firstTerminalAt` | ✅ | Row 6 |
| `reconcile.py` phantom detection | Supervisor PID watching + auto-respawn; `state=crashed`, `attempt` counter | ✅ better | Row 7 |
| Resume via `session.claude_session_id` (session.py:75-76) | `claude --resume <uuid>` / `claude attach <id>`; `state.json` retained on stop | ✅ | Row 8 |
| `worktree.py:127-176` per-session worktree | `--worktree [name]` flag + `dispatch.isolation` slot | 🔗 #107 | Row 9 |
| `CW_CLIENT`/`CW_PURPOSE`/`CW_SESSION_ID` injection (spawn.py:71-76) | None — parent shell env does **not** propagate; no `--env` flag | ❌ **gap** | Row 10 |

### Row 1 — Dispatch (`cmux.spawn` vs `claude --bg`)

**cw today (spawn.py:71-77):** Builds a shell command with `CW_CLIENT/CW_PURPOSE/CW_SESSION_ID` exports, `cd $cwd`, then `cw run-claude -- --print "$prompt"`. `Cmux.spawn` opens a multiplexer pane and sends the command. Returns the cmux surface id.

**Native (verified 2026-05-22):**

```bash
$ cd /home/matthew/.cw/wt/758234be/auto-dev-105
$ claude --bg "Print READY then exit."
backgrounded · 359cb085
  claude agents             list sessions
  claude attach 359cb085    open in this terminal
  ...
```

- Short ID is the first capture group of `^backgrounded · ([0-9a-f]{8})` on stdout — stable across invocations
- Full UUID lives in `state.json.sessionId` and `roster.json.workers.<short>.sessionId`
- `claude --bg` without a prompt enters `state=working, tempo=blocked, needs="send a prompt to start"` — useful for cw if it wants to prime a worker before deciding what to dispatch
- The supervisor auto-names sessions from intent (e.g. `name: "exit sequence"`, `nameSource: "auto"`)

**Verdict:** ✅ covered. Caveat — see Row 10 for the env-var injection gap.

### Row 2 — List surfaces (`cmux.list_surfaces` vs `claude agents --json`)

**cw today (cmux.py:174-198):** Enumerates `workspace.list` then `surface.list` per workspace; returns set of surface IDs. All-or-nothing semantics to avoid partial-results false phantoms.

**Native (verified):**

```bash
$ claude agents --json | jq '.[] | select(.kind=="background")'
{
  "pid": 4148899,
  "cwd": "/home/matthew/.cw/wt/758234be/auto-dev-105",
  "kind": "background",
  "startedAt": 1779458663030,
  "sessionId": "359cb085-dfac-4a1b-af27-088328516a27",
  "name": "exit sequence",
  "status": "idle"
}
```

- Includes both `interactive` and `background` kinds; cw filters on `kind=="background"`
- `status` field surfaces busy/idle directly (richer than cmux's pane existence)
- Stable schema across all invocations observed

**Verdict:** ✅ covered. Slightly richer than cmux because it exposes status directly.

### Row 3 — Identify (`cmux.identify` vs `--cwd` filter)

**cw today (cmux.py:170-172):** Returns the *currently focused* cmux surface — single result, used by `cw status`/`cw bg` to figure out where the user is.

**Native (verified):**

```bash
$ claude agents --cwd /home/matthew/.cw/wt/758234be/auto-dev-105 --json
[ { "sessionId": "359cb085...", ... } ]

$ claude agents --cwd /tmp --json
[]
```

- Semantic mismatch: cw asks "which surface am I in?"; native asks "which sessions live under this cwd?"
- For cw's needs (route a `/session-done` to the right session), the cwd filter is *more useful* — you get a list scoped to your worktree rather than relying on terminal focus
- Drawback: no equivalent of "the focused pane" — but that concern goes away when there are no panes (NativeBackend has no multiplexer)

**Verdict:** ✅ covered — actually better fit for the post-multiplexer model.

### Row 4 — Close (`cmux.close` vs `claude stop` / `claude rm`)

**cw today (cmux.py:166-168):** `surface.close` via cmux IPC; idempotent on missing surface.

**Native (verified):**

```bash
$ claude stop 359cb085
stopped 359cb085

$ ls ~/.claude/jobs/359cb085/
state.json                              # transcript kept

$ claude agents --json | grep 359cb085
                                        # empty — gone from live list
```

`claude rm <id>` is the destructive variant: deletes the session record *and* its worktree. cw should use `stop` for normal completion (preserves history), and `rm` only when the user asks to fully wipe (analog to `cw done <id> --cleanup`).

**Verdict:** ✅ covered. Mapping: `cmux.close → claude stop`; `cw done --cleanup → claude rm`.

### Row 5 — ACTIVE → IDLE transition

**cw today (wrapper.py:188-230):** A wrapper script inside the Claude pane watches the process and emits a JSON signal file (`events/<short>.idle`) when the underlying `cw run-claude` exits with idle status. State.find_session is updated atomically.

**Native (verified):** No wrapper needed — the supervisor mutates `state.json` directly.

| cw status | Native `state` | Native `tempo` | `agents --json.status` |
|---|---|---|---|
| ACTIVE | `working` | `idle` (inFlight.tasks > 0) | `busy` |
| IDLE | `working` | `blocked` (`needs` set) | `idle` |
| COMPLETED | `done` | `idle` | (session absent, or `idle` while not yet stopped) |
| CRASHED | `crashed` | varies | (session absent until respawn) |

cw's reconcile loop can either:
- **Poll `state.json` + `timeline.jsonl`** (file-based, no extra process). Push semantics via inotify on the jobs dir if latency matters. Latency budget: state.json is updated within ~1s of state changes (observed).
- **Stream `claude agents --json`** every N seconds (more expensive but simpler).

**Verdict:** ✅ covered. cw's wrapper.py becomes unneeded — `signal_idle` and `signal_completed` move into a state.json poller.

### Row 6 — IDLE → COMPLETED on clean exit

**cw today (wrapper.py:233-282, PR #99/#101):** Wrapper emits `SESSION_COMPLETED` sentinel when `cw run-claude` exits cleanly; daemon's `consume_completed_sessions` consumes the sentinel and routes by `completed_reason`.

**Native (verified — session `359cb085`):**

```jsonc
// state.json on completion
{
  "state": "done",
  "detail": "printed READY and exiting",
  "output": { "result": "READY" },
  "firstTerminalAt": "2026-05-22T14:04:27.349Z",
  ...
}
// timeline.jsonl
{"at":"2026-05-22T14:04:27.349Z","state":"done","detail":"printed READY and exiting","text":"READY"}
```

- `firstTerminalAt` is the analog of cw's `completed_at`
- `output.result` is structured (parseable) — better than cw's free-form stdout sentinel
- Critical detail: **a done session is still listed in `agents --json` with `status: "idle"` until explicitly stopped.** cw cannot use "absent from agents list" as a completion signal — must poll `state.json`.

**Verdict:** ✅ covered, with better structured output. The current cw sentinel format (auto_dev_result, PR #99/#101/#103) can map onto `output.result` directly.

### Row 7 — CRASHED detection / phantom reaping

**cw today (reconcile.py:89-219):** A periodic loop compares cw's session state to `cmux.list_surfaces()`; sessions tagged as `RUNNING` whose cmux surface disappeared get marked `CRASHED` and routed.

**Native (verified — session `7d8eb576`, `kill -9` on worker pid 4161399):**

| Phase | state.json | agents --json | roster.json |
|---|---|---|---|
| Pre-kill | `working/blocked` | listed, `idle` | `attempt: 1`, pid 4161399 |
| Post-kill (≈2s) | `crashed`, detail `"exit -1; respawning"` | `[]` | entry still present |
| Post-respawn (≈8s after kill) | `running/idle` | listed, `idle`, new pid 4172863 | `attempt: 2`, new sockets |

Three new facts:

1. **Supervisor auto-respawns crashed workers** with the same `sessionId`, incrementing `roster.workers.<short>.attempt`. cw's reconcile no longer needs to "reap and re-dispatch" — the supervisor does it.
2. **`state` has at least four values**: `working`, `running`, `done`, `crashed`. cw's `CompletionReason` enum should map to these.
3. **`respawnFlags` array** in dispatch envelope controls respawn policy (currently empty/default; investigate in Phase 1 whether to disable for one-shot dispatched workers).

**Verdict:** ✅ better than cw. NativeBackend can largely retire `reconcile.py`'s phantom path; phantom detection moves to *transient state* (gap between roster.json showing a pid and that pid's process being alive), which the supervisor itself owns.

### Row 8 — Resume via session_id

**cw today (session.py:75-76):** `session.claude_session_id` stores the Anthropic SDK session ID returned in the wrapper sentinel. `cw resume <name>` runs `claude --resume <claude_session_id>` from the same cwd.

**Native (verified — declarative):**

- `state.json.resumeSessionId` is persisted across `claude stop` (file kept in `~/.claude/jobs/<short>/`)
- `claude --resume <uuid>` is a top-level option, documented in `--help`
- `claude attach <id>` short-form works for live sessions
- Roundtrip across `claude respawn` is documented but not exercised in this spike

**Verdict:** ✅ covered. cw's `session.claude_session_id` field maps directly to `state.json.resumeSessionId`; no schema change needed.

### Row 9 — Worktree isolation

**cw today (worktree.py:127-176):** Per-client `worktree_base` from `clients.yaml`; cw creates `<base>/<client>/<branch>` worktrees on dispatch.

**Native (cross-ref #107):** Observed slot `dispatch.isolation: "none"` in roster.json; `--worktree [name]` is a documented flag. Not exhaustively tested in this spike — see #107 RFC `docs/rfcs/0003-native-worktree-isolation.md`.

Crossover note for #107: `claude rm <id>` deletes the session's worktree along with the conversation. cw's `worktree.py` can hand off teardown to `rm` for sessions that own a worktree, keeping its policy layer thin (per-client base directory, submodule init).

**Verdict:** 🔗 deferred to #107.

### Row 10 — Multi-client routing env vars

**cw today (spawn.py:71-76):**

```python
env_prefix = (
    f"CW_CLIENT={shlex.quote(client.name)} "
    f"CW_PURPOSE={shlex.quote(SessionPurpose.IMPL.value)} "
    f"CW_SESSION_ID={shlex.quote(sess.id)} "
)
command = f"cd {cwd} && {env_prefix}cw run-claude -- --print {prompt!r}"
```

These vars are how cw's hooks, slash commands, and `cw run-claude` know which client/session they're running for.

**Native (verified — session `31616ec2`):**

```bash
$ CW_CLIENT=test-client CW_PURPOSE=impl CW_SESSION_ID=fake-1 claude --bg
backgrounded · 31616ec2

$ cat /proc/<worker-pid>/environ | tr '\0' '\n' | grep '^CW_'
# (empty — none of the CW_* vars propagated)

$ cat ~/.claude/jobs/31616ec2/state.json | jq .providerEnv
{}

$ cat ~/.claude/daemon/roster.json | jq '.workers["31616ec2"].dispatch.env'
{}
```

**The parent shell's environment is NOT inherited by the bg worker.** The dispatch envelope has an `env` slot but the CLI doesn't populate it. `--help` has no `--env` / `--secret` / `--provider-env` flag. `providerEnv` in state.json is for Anthropic provider config (not arbitrary user env).

**Workaround paths for Phase 1:**

1. **Per-cwd `.claude/settings.json`** — write a per-session settings file at the worker's cwd before dispatch. Settings can carry environment values (verify in Phase 1).
2. **Per-session subagent** — define an agent with embedded values and dispatch via `claude --bg --agent <name>`. Frontmatter or system prompt carries the routing context.
3. **`--append-system-prompt`** — pass `CW_CLIENT=X CW_PURPOSE=Y CW_SESSION_ID=Z` as system context. Works for read-by-Claude but not for shell-level env (e.g., `cw run-claude` subprocess).
4. **Direct dispatch-envelope write** — bypass `claude --bg` and write a dispatch envelope into `~/.claude/daemon/dispatch/` with populated `env`. Brittle (private interface) but possible.
5. **Upstream ask** — request a `--env KEY=VAL` flag on `claude --bg`. Smallest, cleanest fix; aligns with documented dispatch envelope schema.

**Verdict:** ❌ real gap. Not a blocker for Phase 1 (workaround 1 or 2 is viable), but the design must commit to one workaround before NativeBackend lands.

## Concerns called out in the ticket

| Concern | Outcome |
|---|---|
| Supervisor's 1hr idle-stop for daemon-origin workers | ⚠️ **Wrong order of magnitude.** Daemon idle-stop is 5s once no clients are attached and no bg workers are running. Workers themselves don't appear to have a documented idle-stop in 2.1.148. `pins.json` exists as a programmatic pin mechanism (file-based) — cw can pin workers it cares about without TUI interaction. |
| `claude --bg --permission-mode bypassPermissions` carries through `claude respawn` | Not tested in this spike. Defer to Phase 1 verification. Low risk: `respawnFlags` in dispatch envelope is empty by default, suggesting flags persist by default. |
| `claude agents --json` reachable from inside another Claude session | ✅ Verified during this spike (I am inside a Claude session; agents --json works). |
| Supervisor survives `cw` restart cleanly (#86) | ✅ `claude daemon` runs as a separate process; `cw` restarts can't kill it. `claude respawn --all` is the upgrade story. |
| `claude --bg --agent <name>` honors subagent `isolation: worktree` frontmatter | Defer to #107. |
| CRASHED vs COMPLETED distinction for `completed_reason` | ✅ `state` enum includes `crashed`; richer than cw's NORMAL/CRASHED bool. |

## Decision

**Green-light Phase 1.** Recommend proceeding with #108 NativeBackend implementation under the following constraints:

1. **Polling model:** NativeBackend polls `~/.claude/jobs/<short>/state.json` for state transitions. Optional inotify upgrade if latency proves an issue (target: ≤2s end-to-end).
2. **Resolve Row 10 before merging NativeBackend.** Pick workaround 1 (per-cwd settings.json) or 2 (per-session subagent) as Phase 1's env-injection strategy. Open ticket against upstream for `--env` flag.
3. **Map state.state → cw SessionStatus** explicitly: `working+busy → ACTIVE`, `working+blocked → IDLE`, `done → COMPLETED`, `crashed → CRASHED`.
4. **Retain `reconcile.py` only as a thin "supervisor down" detector.** Crash recovery itself moves to the supervisor; reconcile checks that the supervisor itself is alive (`claude daemon status`) and surfaces an error if not.
5. **Map cw cleanup verbs to native:** `cmux.close` → `claude stop`; `cw done <id> --cleanup` → `claude rm`.

## Open questions for Phase 1 (#108 NativeBackend)

1. **Env injection commitment.** Which Row 10 workaround does Phase 1 commit to? (Recommended: per-cwd `.claude/settings.json` + an upstream request for `--env`.)
2. **Polling cadence.** Settle on a default poll interval for state.json (suggest 1s for active sessions, 5s for stopped, with inotify upgrade path documented).
3. **Spare-worker pool exposure.** Supervisor pre-spawns "bg spare" workers. Should cw surface that latency win to users (e.g., reduce `cw start` to ~0s)? Or treat it as a hidden optimization?
4. **`respawnFlags` semantics.** What flags exist? Should NativeBackend disable auto-respawn for one-shot `/auto-dev` dispatches (where re-running is wasteful) and enable for long-lived `cw start` panels?
5. **Migration story for cw 0.x state.** Existing `Session.surface_ref` (cmux pane ids) become meaningless. Schema migration in `state.json` (cw's, not Claude's): set `surface_ref` to `daemonShort`, fill in `claude_session_id` from `resumeSessionId`.
6. **Backend selection.** Keep cmux backend as a fallback for users without 2.1.139+? Or hard-require native and deprecate cmux in 1.0? (Recommended: hard-require, since cw exists in part to bridge the 0.x→1.0 gap.)

## References

- Native docs: [Agent view](https://code.claude.com/docs/en/agent-view), [Headless](https://code.claude.com/docs/en/headless)
- cw modules: `src/cw/session.py`, `src/cw/wrapper.py`, `src/cw/spawn.py`, `src/cw/reconcile.py`, `src/cw/cmux.py`
- #103 (auto_dev_result parser) — confirms 0.8.3 sentinel/wrapper flow is current baseline
- #86 (daemon restart-on-upgrade) — auto-resolved by `claude respawn --all`
- Related spikes: #106 (Agent SDK + channels), #107 (native worktree isolation)
- Crossover handoff: `~/.claude/handoffs/2026-05-22-cw-phase0-spikes-interactive.md`
