# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
