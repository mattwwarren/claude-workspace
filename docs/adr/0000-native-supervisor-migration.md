# Migrate session lifecycle from wrapper + multiplexer to `claude --bg` + supervisor

**Status:** Accepted
**Driven by:** #105, #108, #109, #111, #112, #113, #114, #119

## Decision

cw is migrating its daemon-session lifecycle off the hand-rolled
`wrapper.py` + multiplexer (`cmux` / `tmux`) + `reconcile.py` substrate
and onto Claude Code's native primitives: `claude --bg`, the per-user
supervisor process, `~/.claude/jobs/<id>/state.json`, and the
`claude agents --json` query interface.

This ADR is the foundational record. All other ADRs assume this
trajectory as ground truth.

## Initial state (pre-migration, today)

The wrapper-based stack that cw shipped through 0.8.x:

1. **Spawn** (`src/cw/spawn.py`). Dispatch composes a shell command of the
   form `cd <cwd> && CW_CLIENT=... cw run-claude -- --print <prompt>` and
   hands it to the multiplexer adapter (`cmux.spawn` or `tmux.spawn`),
   which creates a pane and returns a `surface_ref` (pane id).
2. **Wrapping** (`src/cw/wrapper.py`, 282 LoC). `cw run-claude` is a
   Python subprocess wrapper that:
   - launches `claude` as a child process
   - captures stdout into a 1 MiB circular buffer
   - parses the `<<<AUTO_DEV_RESULT` sentinel on exit
   - writes either `SESSION_IDLED` or `SESSION_COMPLETED` signal files
     to `events_dir()`
3. **Lifecycle signal pickup** (`src/cw/dispatch.py:consume_completed_sessions`).
   The dispatch loop polls those signal files per tick, advances the
   matching `TicketTask` to `COMPLETED`, and calls `persist_last_result`
   to dump the parsed `AutoDevResult` onto the Session.
4. **Reconciliation** (`src/cw/reconcile.py`, 219 LoC). Periodically
   compares persisted `sessions.json` against `adapter.list_surfaces()`;
   sessions in state but with no live pane are reaped (DAEMON-origin
   reverts RUNNING → PENDING). A transient-outage guard prevents
   mass-reaping when the adapter returns zero surfaces while state has
   many.
5. **Resume** (`src/cw/session.py`). Resume re-attaches to a multiplexer
   surface (if alive) or spawns a new `claude --resume <claude_session_id>`
   when the original surface is gone.

The substrate works, has paid for itself, and is being deliberately
retired — not because it's broken, but because the native primitives
now exist and cover the surface more cleanly.

## Target state (`claude --bg` + supervisor)

Claude Code v2.1.139+ ships a per-user supervisor process. Each
background-launched session is owned by the supervisor:

- **Spawn:** `claude --bg --name <name> --add-dir <cwd> "<prompt>"`
  returns a short session id immediately. The supervisor owns the
  process and persists state under `~/.claude/jobs/<id>/state.json`.
- **Query:** `claude agents --json` lists every live session with its
  `sessionId`, `status` (`working` / `idle` / `completed` / `failed`),
  `pid`, `cwd`, `kind`, `startedAt`, `name`.
- **Attach / resume:** `claude --bg <id>` foregrounds an existing
  backgrounded session. `claude attach <id>` and `claude --resume <id>`
  enter the same transcript. `claude respawn <id>` restarts a stopped
  session from its transcript.
- **Terminate:** `claude stop <id>` ends the session; `claude rm <id>`
  removes it from the supervisor.
- **Crash handling:** the supervisor watches each session's process;
  on crash, state transitions to `failed` and surfaces via the JSON
  query. cw no longer has to detect this from the outside.

The supervisor replaces three things at once: the wrapper subprocess,
the multiplexer surface as the source of truth, and the
phantom-detection job that reconcile.py performs.

## Conversion path

Phased per the issues listed under *Driven by*. Roughly:

1. **#105 (Phase 0 spike)** — confirm the native primitives cover every
   cw lifecycle surface; write the gap analysis. Throwaway code, written
   decision deliverable.
2. **#108 / #109 (Phase 1)** — implement `NativeBackend` as a new
   multiplexer adapter behind the existing `CmuxAdapter` protocol so
   the choice of backend stays a single config switch.
3. **#111 (Phase 2)** — rewrite `spawn.py` to call `claude --bg` when
   `NativeBackend` is selected. Legacy cmux/tmux spawn path retained
   for compatibility.
4. **#112 (Phase 2)** — replace `wrapper.py` IDLE/COMPLETED signaling
   with reads from `~/.claude/jobs/<id>/state.json`. RFC under #105
   picks between polling, Stop-hook, and the channels-based push.
5. **#113 (Phase 2)** — retire `reconcile.py` phantom detection on
   native backend; reduce it to a thin shim over `claude agents --json`.
6. **#114 (Phase 3)** — `cw-pr-events` channel demonstrates the
   push-based reaction model; future lifecycle events can follow the
   same pattern.
7. **#119 (Phase 6)** — delete the obsolete code. `wrapper.py`,
   `reconcile.py`'s legacy phantom-detection path, the `cmux` and
   `tmux` backends, and `handoff.py`'s wrapper-specific bits come out
   once the native path is the only path in use.

During the transition (phases 2 through 5), both stacks coexist.
Backend selection (`orchestrator.yaml` `backend:`, `CW_BACKEND` env,
platform default) already exists for this reason. The wrapper +
multiplexer stack stays the default on platforms where the supervisor
is not yet usable — but only until #119, at which point the native
path is the only path and there is no compatibility surface to
preserve.

## Reconciliation

Reconciliation is the load-bearing piece that changes the most:

- **Today:** "is this session's multiplexer pane still alive?"
  Answered by `adapter.list_surfaces()`. False negatives possible
  during multiplexer outages, hence the transient-outage guard.
- **Target:** "does the supervisor still own this session?"
  Answered by `claude agents --json`. The supervisor itself tracks
  process liveness; cw's job becomes status sync, not liveness probing.

The native reconciliation shim (#113) keeps three responsibilities
cw owns even after the migration:

1. **Status sync.** Map supervisor status → `SessionStatus` and
   `CompletionReason`. Update cw state accordingly.
2. **Queue revert.** When a DAEMON-origin Session transitions to
   `failed` or disappears from the supervisor, revert its `TicketTask`
   from RUNNING → PENDING (existing behavior, new trigger).
3. **Transient-outage guard.** Distinguish "supervisor unreachable"
   (e.g., `subprocess.CalledProcessError`) from "supervisor returns
   empty list." Only the second is allowed to reap.

Phantom detection driven by surface absence (the original reconcile
job) becomes redundant once #112 + #113 land, and the body of
`reconcile.py` shrinks accordingly.

## Consequences

- The wrapper substrate (`wrapper.py`, signal files, sentinel-block
  parsing on exit) is retired in phases 2–3 and **deleted in #119**.
  No long-lived compatibility shim survives the migration; the
  end-state is single-path and the obsolete modules are removed from
  the tree.
- The `cmux` and `tmux` multiplexer backends are deleted alongside
  the wrapper in #119. The `MultiplexerAdapter` protocol may survive
  in name if it still has one implementation (`NativeBackend`), or
  may be inlined away — that's a tactical call left to #119.
- cw's state model gains a load-bearing dependency on
  `Session.claude_session_id`. Pre-migration sessions that lack this
  field keep working via the wrapper path during the transition; once
  #119 deletes the wrapper path, every persisted Session must either
  have `claude_session_id` populated or be treated as orphaned by a
  one-time state migration shipped with #119.
- See [ADR-0001](0001-parked-tasks-pin-their-session.md) — the
  parked-tasks invariant only makes sense in a world where the
  supervisor + `claude --bg` resume primitive is the floor.
- cw loses direct control of session stdout capture. Stdout for
  `AutoDevResult` parsing now flows through `claude logs <id>` or the
  supervisor's transcript files, not the wrapper's circular buffer.
  Parsing logic in `auto_dev_result.py` is unchanged; the source of
  the string differs.
- Crash semantics get clearer. `completed_reason` distinctions
  (`CRASHED` vs `NORMAL`) are read from supervisor status rather than
  inferred from wrapper exit codes.

## Alternatives considered

- **Stay on the wrapper substrate indefinitely.** Rejected. The
  hand-rolled approach was correct when no native equivalent existed;
  with the supervisor available, every additional feature we built on
  the wrapper path was net technical debt.
- **Build a thin native adapter without retiring the wrapper.**
  Rejected. Two parallel lifecycle paths produce two parallel bug
  surfaces. The migration is phased so that *during* the transition
  both exist, but the end-state is single-path.

## Referenced by

- ADR-0001 (parked tasks pin their Session)
- #58, #59, #105, #108, #109, #111, #112, #113, #114, #119, #126, #127
