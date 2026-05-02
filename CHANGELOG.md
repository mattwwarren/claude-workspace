# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.3] — 2026-05-01

Fixes a dispatch-spawn bug that made DAEMON-session spawning unusable
end-to-end:

1. `spawn_create_impl` invoked `claude -w <worktree-path>`, but
   `claude -w` takes a worktree *name*, not a filesystem path — the
   leading `/` made the first segment empty and failed claude's name
   validator.
2. `dispatch_tick` only ran `worktree_path.mkdir(...)` — it never
   created a real git worktree. Even with the `-w` issue fixed, the
   spawned shell would have landed in a plain (non-repo) directory.
3. The dispatch / pr_responder / plan call sites all wrote a prompt to
   a temporary file and immediately read it back into the spawn command
   string — pointless file roundtripping that obscured the real call.

### Fixed
- `cw.dispatch.dispatch_tick` now calls `create_worktree(client, branch)`
  (idempotent — returns existing path if already created) instead of
  `mkdir`-ing an empty directory. The spawned shell `cd`s into the
  resulting worktree and runs `claude --print …`. `cw.pr_responder`
  uses the same pattern for PR-event branches.
- Regression test added asserting the spawn command does not contain
  ` -w ` and starts with `cd `.

### Changed
- `spawn_create_impl` and `pr_responder._spawn_session` now take a
  ``prompt: str`` rather than a ``prompt_file: Path``. Callers inline
  the prompt directly. The `cw spawn` CLI reads the file at the
  user-facing boundary and passes the contents through.
- `cw.plan` still persists the planner prompt to disk for audit /
  debugging but no longer reads it back to inline into the spawn.
- `tests/conftest.py::make_git_repo` now initialises with an empty
  `main` commit (and a per-repo `user.email` / `user.name`) so callers
  that exercise `git worktree add` have something to branch from.
- `cw.worktree._run_git` strips `GIT_*` from the subprocess env so
  cw's git operations always target the client repo at *cwd*. Without
  this, running cw from inside a git hook (e.g. a pre-commit pytest
  run that exercises dispatch) would leak `GIT_DIR` / `GIT_INDEX_FILE`
  and produce confusing "Not a directory" errors.

## [0.6.2] — 2026-04-19

Phantom-session reconciliation. When tmux dies (machine sleep/restart)
or cmux surfaces close, `sessions.json` used to drift from reality —
dead sessions stayed ACTIVE/IDLE, blocking new dispatch and lying in
`cw status`. This release adds multiplexer/state reconciliation with
a transient-outage safety guard so a short cmux/tmux hiccup cannot
mass-reap live sessions.

### Added
- Multiplexer/state reconciliation. Phantom sessions (tmux/cmux surfaces
  that no longer exist) are detected and reaped automatically on `cw status`,
  `cw list`, `cw start`, and at the top of each `dispatch_tick`. Explicit
  reconciliation is available via `cw doctor --reap`.
- `MultiplexerAdapter.list_surfaces()` on the adapter protocol; implemented
  for tmux, cmux (macOS), and fake backends.
- Public `dev_queue_lock` context manager in `cw.dev_queue` for callers
  that need load → mutate → save around the queue.

### Changed
- `start_session`'s "Launching ... surfaces" message now names the active
  backend (tmux/cmux/fake).
- Dev-queue `TicketTask`s associated with reaped DAEMON sessions revert
  from RUNNING to PENDING so the dispatch loop retries them.
- `RealCmuxAdapter._call` now normalises socket `OSError` and
  `json.JSONDecodeError` into `CwError`, giving callers a single
  exception type to guard against backend failures.
- `reconcile()` refuses to mass-reap when the adapter reports zero live
  surfaces but the persisted state still has ACTIVE/IDLE sessions with
  surface refs — a transient cmux/tmux outage no longer marks every
  session COMPLETED/CRASHED. `compute_drift` stays pure; the guard
  lives only in the side-effecting path.
- `RealCmuxAdapter.list_surfaces` returns an empty set on any enumeration
  failure (including per-workspace `surface.list` errors) rather than a
  partial set, matching the all-or-nothing protocol contract expected by
  the reconciler.
- `ReconcileReport` carries `phantom_session_names` alongside IDs so
  callers no longer need to reload state to resolve names.
- `dispatch_tick` guards the reconcile call and logs failures instead
  of halting the dispatch loop on a reconcile error.
- `doctor._check_reconcile` narrowed its `except Exception` to
  `except CwError` and wraps the reconcile call itself so that an
  unexpected failure is reported as a check result rather than
  crashing `cw doctor --reap`.

## [0.6.1] — 2026-04-19

Small correctness fixes for state durability on Linux and worktree path
sizing for cmux.

### Fixed
- `fix(state)`: all JSON state files (sessions, dev queue, plan, cursors)
  now write via atomic rename so a crash mid-write cannot leave a
  truncated file behind (#46 / #48).
- `fix(worktree)`: the default worktree path now stays under cmux's
  64-character workspace-name cap, avoiding spawn failures when branch
  names push the computed path over that limit (#47 / #49).

## [0.6.0] — 2026-04-18

The multi-platform bridge. `cw` now runs natively on Linux via tmux,
while keeping the macOS-native cmux path unchanged. Backend choice is
driven by a three-tier selector so CI, power users, and single-user
preferences all have a way in.

### Added
- `cw.tmux.TmuxAdapter`: a tmux backend that wraps the `tmux` CLI via
  `subprocess`. A workspace maps to a tmux session, a surface to a
  pane. Raises `CwError` at instantiation time if `tmux` is not on
  PATH (#38).
- Three-tier backend selector in `cw.cmux.get_backend_adapter()`:
  `CW_BACKEND` env var → `orchestrator.yaml` `backend:` field →
  platform default (`darwin` → cmux, everything else → tmux). Setting
  `CW_BACKEND=fake` returns a `FakeCmuxAdapter` for CI and local
  smoke tests (#37).
- `BackendName` enum in `cw.models`; optional `backend` field on
  `OrchestratorConfig`.
- `MultiplexerAdapter` protocol — the backend-neutral name the
  protocol carries going forward. `CmuxAdapter` is a type alias kept
  for one release (#36).
- `cw doctor` subcommand and module — reports resolved backend,
  binary/daemon availability, config and state file validity, and
  version. Exits non-zero when any check fails (#41).
- Parametrized protocol-conformance suite covering every adapter
  class (`tests/test_adapter_protocol.py`) (#39).
- `integration` pytest marker for end-to-end tests that shell out to
  a real multiplexer.

### Changed
- CI matrix is now `[ubuntu-latest, macos-latest]`; tmux is installed
  via apt/brew on the matching runner, and the tmux integration test
  runs on both OSes (#40). Release workflow mirrors the matrix.

### Migration notes
- `from cw.cmux import CmuxAdapter, get_cmux_adapter` keeps working
  in this release. Switch to `MultiplexerAdapter` and
  `get_backend_adapter` before 0.7.
- On Linux, `cw` now defaults to tmux — install `tmux` or set
  `CW_BACKEND=cmux` (if you really want the macOS-only cmux path).

## [0.5.0] — 2026-04-18

Foundations for the multi-platform bridge landing in 0.6.0. No new user
features in this release — the focus is de-risking the tmux backend by
paying down debt in state isolation, error handling, schema migration,
and docs.

### Added
- `schema_version` field on `CwState` and `DevQueueStore`, with a
  `migrate_cw_state` pass that handles field renames and coerces unknown
  `SessionOrigin` values to `user` with a warning instead of crashing
  (#31).
- Accessor functions in `cw.config` (`state_dir()`, `events_dir()`,
  `queues_dir()`, …) so path-consuming modules read the current value at
  call time rather than caching the import-time global (#29).
- `tests/test_exceptions.py` exercising the exception hierarchy, and a
  regression test locking in the Linux-safe retirement path (#34).

### Fixed
- `cw orchestrate retire` no longer crashes on Linux when no session is
  correlated to the merged PR — adapter resolution is deferred until a
  surface actually needs to be closed (#30).
- Test runs no longer leak state into `~/.local/share/cw` or
  `~/.config/cw`. The autouse `tmp_config_dir` fixture covers every
  consumer via the new accessors (#29).

### Changed
- Documentation no longer references the retired Zellij multiplexer.
  README, CLAUDE.md, install guides, and in-source docstrings now speak
  of the pluggable multiplexer backend (cmux today; tmux in 0.6.0) (#32).

### Removed
- `ZellijError` (unused) and the `zellij-plugin/` Rust/WASM scaffold
  (#33). The `zellij_pane → surface_ref` migration in `load_state` is
  preserved as migration armor for older state files on disk.
