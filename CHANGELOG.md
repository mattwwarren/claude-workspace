# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
