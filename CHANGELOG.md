# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Sentinel salvage on timeout/crash** (#372): the `TIMED_OUT` and
  crashed-phantom sweeps in `reconcile` now recover a terminal-success
  `AUTO_DEV_RESULT` (`shipped`/`no_op`) from the session's transcript before
  finalizing disposition. A headless worker that emitted a valid sentinel and
  then stalled (e.g. waiting on CI) or whose surface died is now recorded
  COMPLETED with its real result — and its ticket is **not** reverted to
  PENDING — instead of being mislabeled `timed_out`/`crash` and re-dispatched
  (dup-PR risk). Guards the reused-worktree stale-transcript case (#358) by
  only trusting a transcript modified after the session started.

## [0.12.0] — 2026-05-29

Minor release covering the cw 1.0-march observability and orchestration
substrate: live work board, read-only session peek, atomic terminal
transitions, the queue-events MCP channel, cost tracking, and `cw doctor`
wedge detection. Also enables the native nightly soak clock toward 1.0.

### Added

- **`cw watch` live work board** (#126 → PR #347): full-screen TUI streaming
  cross-client session + queue state, refreshed from the event bus.
- **`cw peek`** (#122 → PR #346): read-only tail of a running session's output
  without attaching to or disturbing the surface.
- **`cw spawn complete`** (#121 → PR #344): atomic session terminal-state
  transition, closing the race between session-flip and queue-flip.
- **cw-queue-events MCP channel** (#125 → PR #355): pushes queue-state deltas
  with persist-on-emit + cursor replay, mirroring the PR-events channel. Track C
  complete (7/7).
- **`cost_usd` persistence, schema v4** (#124 → PR #351): per-session and
  per-ticket USD cost recorded on `Session` + `TicketTask`.
- **`cw doctor` wedge detection** (#123 → PR #353): drift checks for wedged
  sessions, `--reap` recipes, and `--json` output.
- **AUTO_DEV_RESULT schema Phase C+D** (#174 → PR #343): expanded contract for
  queue-orchestrator observability.

### Changed

- **CI: native nightly scheduled, cmux nightly de-scheduled** (PR #363):
  `nightly-native.yml` gains a daily 09:00 UTC `schedule:` trigger, starting the
  2-week native-soak clock toward 1.0 (gates #242/#119/#120). `nightly.yml`
  (cmux integration) is de-scheduled to `workflow_dispatch`-only ahead of cmux
  removal (#119).

### Fixed

- **Silently-idle watchdog → flag-only** (#348 → PR #349): the `silently_idle`
  watchdog no longer reaps the worker; it flags only and lets the run reach the
  60-min ceiling, avoiding false kills of active workers.
- **`cw_queue_peek` stale-transcript false STOP** (#358 → PR #359):
  `find_transcript_for_ticket` no longer picks the oldest stale transcript in a
  reused worktree (which produced bogus age + a false STOP recommendation).

### Removed / chore

- **Delete `pr_responder.py`** (#245 → PR #357): superseded by the event-driven
  review-monitor path.
- **Suppression audit** (PR #362): `noqa` / `type: ignore` count reduced 110 → 52.
- **Skill audit + `/cw-fanout`** (PR #350): cw skills re-aligned to current
  workflows; new `/cw-fanout` multi-ticket dispatch skill added.

## [0.11.2] — 2026-05-28

Patch release with two reliability fixes for the dev-queue dispatch path,
surfaced during the 2026-05-28 dogfood wave.

- **Code-fenced sentinel parsing** (#337 → PR #339): `parse_stdout` now
  tolerates AUTO_DEV_RESULT JSON wrapped in a Markdown code fence (```` ```json ````)
  when the explicit `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>` markers are
  absent. Previously the dispatcher treated such sessions as no-sentinel and
  spawned wasteful att2/att3 retries on already-shipped work. Closes #336
  (downstream consequence — silently_idle hangs after the parser returned None).
- **Watchdog default bump** (#340 stopgap → PR #341): `IDLE_WATCHDOG_SECONDS`
  raised from `300` → `900` (15 min), `idle_watchdog_by_tier['large']` from
  `600` → `1800`. The previous 300s budget false-positively flagged active
  small-tier workers (#337 itself took 14 min wall time and tripped the
  watchdog at 5 min). The deeper fix — transcript-mtime liveness detection
  — remains open under #340.

## [0.11.1] — 2026-05-27

Patch release covering #129's BLOCKED_ON_USER producer + watchdog and the
SHOULD_FIX follow-up batch from PR #323's review, plus the new
`cw-queue-peek` skill for in-flight session inspection.

- **`BLOCKED_ON_USER` producer + watchdog** (#129/#322 → PR #323):
  `QueueItemStatus.BLOCKED_ON_USER` + `OrchestratorEventType.SESSION_NEEDS_ATTENTION`
  enum additions; `signal_needs_attention` path in `wrapper.py` for paused-for-input
  sentinels; `flag_silently_idle_daemon_sessions` watchdog in `reconcile.py` for
  silently-stalled DAEMON sessions; `notify.py` peon-ping + `notify-send` push
  helper; `docs/headless-contract.md` updated with the BLOCKED_ON_USER section.
- **Watchdog hardening** (#324/#332): reorder writes — `save_state` (session →
  COMPLETED + `last_result`) fires before queue mutation; crash between
  session-flip and queue-flip recovers cleanly on next reconcile tick.
- **Per-ticket / per-tier IDLE_WATCHDOG_SECONDS override** (#326/#331): mirrors
  the `HEADLESS_TIMEOUT_SECONDS` override pattern from #265.
- **`notify.py` debug logging** (#327/#330): each fail-quiet exception path now
  logs at debug level so `CW_LOG_LEVEL=DEBUG` surfaces misconfigured peon.sh.
- **Test rigor** (#328/#333): `_is_paused_for_user_input` tests construct real
  `AutoDevResult` instances instead of `MagicMock`, so future schema changes
  fail loudly.
- **`cw-queue-peek` skill + script** (PR #335): in-flight inspection of
  RUNNING dev-queue sessions. Computes age, idle gap, last sentinel status,
  and PR state per session; recommends WAIT / PEEK / STOP via a 10-rule
  peek-stop ladder. Reports only — operator runs `cw spawn close <id>`
  after reviewing. Closes the gap between `cw-session-watch` (post-mortem
  exit status) and `cw-validate-result` (post-mortem sentinel inspection).

## [0.11.0] — 2026-05-27

Pre-1.0 substrate release covering multiplexer-removal Phase D, the PR
event channel architecture, orchestrator subagent + `cw orchestrator-start`,
dispatcher routing hardening, and freshness-gate guardrails.

Major themes since 0.10.0:

- **Multiplexer Phase D complete**: `MultiplexerAdapter` removed from
  `reconcile`, `dispatch`, `doctor`, `orchestrate`, and `cli`. Liveness
  checks switched to `claude agents --json` + `roster.json`. Net -252 lines
  across the substrate. (#167/#269)
- **PR event channel architecture**: new `cw_pr_events_server` MCP channel
  pushes review-monitor deltas with durable persist-on-emit and cursor
  replay (#138/#282, #139/#284, #114/#288). Daemon's pr-watcher loop
  retired in favor of event-driven routing (#299). Stdio MCP channel
  proxy added for capability declaration (#291). Channel server lazy-imports
  starlette so the module loads without the `[mcp]` extra (#306); 307
  redirect on bare `/sse` path fixed (#309).
- **Orchestrator subagent + `cw orchestrator-start`**: new `cw-orchestrator`
  subagent routes channel events (#115/#296/#301); new `cw orchestrator-start`
  CLI command spawns the orchestrator session (#295/#302). Frees the
  daemon from PR-response logic and centralizes routing.
- **Dispatcher routing hardening**: v4 statuses (`ambiguities_pending_resolution`,
  `premises_pending_verification`) now recognized as terminal at
  `schema_version=2` and route to BLOCKED (no more retry-on-paused-sentinel
  bug) (#316/#319, e538638). New `QueueItemStatus.CANCELLED` prevents the
  dispatcher race when `cw spawn close` runs against an in-flight tick
  (#317/#320). Short-form `stage_reached` aliases mapped to canonical
  values (#292/#293). Code-fence wrapped sentinels parsed correctly
  (#307). `dispatch_tick` guards against worktree == main checkout
  (#300/#311). Permission_mode uses explicit None check (#298).
- **Reconcile resilience**: 30s spawn grace window prevents same-tick
  phantom reaping (#271/#272). Short-id `surface_ref` matches against
  UUID-prefix `sessionId` (#273). Stale Stop hook dropped on worktree
  reuse for retry (#285/#287).
- **Freshness gate**: pre-dispatch `dispatch_tick` checks each client's
  local `main` against `origin/main`; stale clients emit
  `OrchestratorEventType.TICKET_NEEDS_SYNC` and skip the tick without
  burning a dispatch slot (#215/#268). Misconfigured clients no longer
  dump tracebacks (#278). New `cw dev-queue refresh-all` subcommand
  fast-forwards every configured client.
- **Headless config**: default `HEADLESS_TIMEOUT_SECONDS` bumped 30→60
  minutes (#266). Per-ticket / per-`scope.tier` override (#265/#279)
  allows large-scope work room to finish.
- **CI scaffolding**: nightly integration workflow at
  `.github/workflows/nightly-integration.yml` exercises real `claude --bg`
  / `claude agents --json` / `claude stop` via `pytest -m integration`;
  `workflow_dispatch`-only until API budget is allocated (#110/#318).
  `[mcp]` extra installed in CI so pr-events tests + coverage run
  (#283).
- **Cleanup**: legacy `handoff.py` deleted (#246/#277); transcript handoff
  is now covered by `claude --resume`.

Full per-PR detail in the GitHub release notes (auto-generated by
`release.yml`).

## [0.10.0] — 2026-05-25

Pre-1.0 substrate release covering native-daemon dispatch hardening,
sentinel-capture observability, queue retry-cap, AUTO_DEV_RESULT
schema v4, and ruff/lint discipline.

Major themes since 0.9.0:

- **Native-daemon path**: `cw start` / `cw resume` migrated to
  `claude --bg + attach`; origin-aware Stop hook; settings.local.json
  write hardened.
- **Sentinel observability**: headless DAEMON sessions now persist
  `last_result` via `signal_stop` parsing (#225/#226); contract-level
  Blocker carries explicit retry policy (#193); `/cw-validate-result`
  and `/cw-followup` skills surface sentinel-aware post-run actions.
- **Queue resilience**: dedupe + remove + clear + reconcile-sweep
  (#177/#221); validation_failed retry-cap + sentinel-to-task routing
  in `signal_stop` (#251/#261); reconcile sweeps stalled headless
  sessions to TIMED_OUT (#185/#260).
- **AUTO_DEV_RESULT v4**: `ambiguities_pending_resolution` and
  `premises_pending_verification` promoted to canonical statuses
  with closed `next_actions` vocabulary (#191/#262).
- **Pipeline guardrails**: `_validate_worktree` pre-flight gate (#186);
  ANSI-strip on `claude --bg` stdout parsing (#203/#204); `cw doctor`
  setup checks (#142/#219); diff-cover pre-push hook (#250).
- **Cost guardrails**: per-client `worker_model` field pins spawned
  workers to Sonnet by default (#248/#254).
- **Ruff hardening**: rule selection expanded across exception
  handling, logging, pathlib, async, and defensive bundles (#230 family
  → #240, #241, #252, #253, #256, #258). Diff-cover gate mirrored
  locally via pre-push hook (#250).

Full per-PR detail in the GitHub release notes (auto-generated by
`release.yml`).

## [0.8.3] — 2026-05-21

Patch release — closes the wedged-`RUNNING` loop called out as a known
issue in 0.8.2. Dispatched `/auto-dev` workers that exit cleanly now
produce a `SESSION_COMPLETED` event with `crashed: false`, so the queue
task transitions `RUNNING → COMPLETED` instead of sitting on a consumed
concurrency slot.

### Fixed
- **Clean-completion signal for dispatched workers** (#99, #101).
  - `cw.spawn` now routes daemon spawns through
    `cw run-claude -- --print '<prompt>'` (instead of raw
    `claude --print`), passing `CW_CLIENT` / `CW_PURPOSE` /
    `CW_SESSION_ID` env so the wrapper can target the specific session
    even when concurrent daemon sessions share `(client, purpose=impl)`.
  - `cw.wrapper` detects headless mode (`--print` in args), tees
    claude's stdout to fd 1 while capturing the last 1 MiB into a
    bounded buffer, parses for the `<<<AUTO_DEV_RESULT…>>>` sentinel
    on clean exit, and emits `SESSION_COMPLETED` via the new
    `signal_completed()`. Idempotent against reconcile racing ahead.
    Falls back to `signal_idle` on parse failure or non-zero exit so
    reconcile's phantom-pane path still catches real crashes.
  - `CompletionReason.NORMAL` added for the wrapper-signaled terminal
    path (distinct from `CRASHED` written by reconcile).
- 0.8.2's consume-side `crashed: true` skip remains intact — no
  regression on the genuinely-crashed path.

### Known issues
- Full per-status routing (`shipped` / `no_op` → COMPLETED,
  `blocked` / `plan_pending_approval` → PAUSED_NEEDS_HUMAN) is still
  scoped to #58. This release ships the terminal-vs-respawn distinction
  only; all non-crashed completions currently land as COMPLETED.
- #59 resume-detection on re-invoke depends on `last_result` being
  populated, which this release enables — but the dispatch-side
  resume-injection is separate work.

## [0.8.2] — 2026-05-06

Patch release — fixes a queue-accounting bug discovered while validating
0.8.1 end-to-end. With 0.8.1's producer fix in place, `SESSION_COMPLETED`
events now carry `ticket_id` — but `consume_completed_sessions` was
matching on `ticket_id` alone, so a `crashed: true` event from a prior
(reconcile-reverted) session could falsely COMPLETE a freshly-respawned
task for the same ticket.

### Fixed
- **`consume_completed_sessions` now skips `crashed: true` events** and
  matches non-crashed events on `session_id` when both sides have one
  (#97, #98). Reconcile is the authoritative path for crashed sessions
  (RUNNING → PENDING revert); the consumer no longer shadows that with a
  spurious COMPLETED transition. Stale events from older sessions for
  the same `ticket_id` are rejected via `session_id` disagreement.
  - `TicketTask` gains a nullable `session_id` field stamped by
    `dispatch_tick` after spawn returns and cleared by `reconcile` on
    revert. Legacy tasks/events without the field fall back to
    `ticket_id`-only matching for backward compatibility.

### Known issues
- Reconcile still has no clean-completion signal — workers that exit
  successfully (`/auto-dev` returns to a bash prompt inside the tmux
  pane) sit `RUNNING` indefinitely, and tmux-session kills are still
  observed as `crashed: true` (now correctly skipped, but the queue
  task ping-pongs RUNNING ↔ PENDING via dispatch respawn). Tracked in
  #99 — not blocking this release; #98's fix prevents the false
  COMPLETED transitions that previously masked the issue.

## [0.8.1] — 2026-05-04

Patch release — fixes a queue-accounting bug introduced in 0.8.0 that
caused every `cw dev-queue` task to stay `RUNNING` forever.

### Fixed
- **`session.completed` events now carry `ticket_id`** (#94, #95). The
  reconciler emitted `SESSION_COMPLETED` without the `ticket_id` field,
  so `consume_completed_sessions` skipped every event and dev-queue
  tasks never transitioned to `COMPLETED`. Concurrency-cap accounting
  treated stranded `RUNNING` tasks as live, progressively starving
  available slots until the queue would refuse to dispatch anything new.
  - **Producer side:** the reconciler now includes `ticket_id` in the
    emitted payload whenever the session name parses as auto-dev,
    mirroring what `session.spawned` already does.
  - **Consumer side:** `consume_completed_sessions` falls back to
    parsing `ticket_id` from `session_name` when the payload lacks it.
    Drains historical `RUNNING` tasks whose completion events predate
    the producer-side fix — no manual queue-file surgery needed.
- `ticket_id_for_session` is now public (renamed from
  `_ticket_id_for_session`) so dispatch and reconcile share the parsing
  helper rather than duplicating the prefix logic.

## [0.8.0] — 2026-05-04

First release of the **0.8.0 milestone** — substrate for autonomous
`/auto-dev` dispatch via `cw`. This release ships parent/worker session
linkage, the headless contract parser, the structured `AutoDevResult`
sentinel, and the dispatch path that wires it all together.

Skipping 0.7.0 — the work landed under a 0.8.0 milestone tag (cw#52–#55
substrate, cw#56–#59 dispatch arc) and a single-digit-up bump matches
the surface-area change.

### Added
- **Spawn API + bidirectional parent-child persistence** (#53, #65). New
  `cw.spawn` module owns session creation; parent and worker sessions
  reference each other via `parent_session_id` / `worker_session_ids`
  fields on `Session`. Replaces the ad-hoc state writes in `start_session`.
- **State schema v2** (#62). `Session` gains linkage fields. Migration
  is automatic on read; old states are upgraded in place.
- **Doctor linkage drift detection** (#55, #68). `cw doctor` reports
  stale `worker_session_ids`, mismatched `parent_session_id`, and
  asymmetric references as failed checks contributing to the exit code.
  Each `linkage/*` check carries a remediation hint. `cw doctor --reap`
  additionally reconciles phantom sessions via the surface liveness
  check.
- **Dispatch headless mode** (#78). `dispatch_tick` spawns daemon
  workers with `--headless` and threads `parent_session_id` through so
  the worker's `AutoDevResult` lands on the parent.
- **AutoDevResult sentinel parser** (#57, #80). `cw.auto_dev_result`
  parses the `<<<AUTO_DEV_RESULT … AUTO_DEV_RESULT>>>` block emitted by
  headless `/auto-dev`, validates §3-§5 invariants, and persists the
  result on the worker `Session`. Six failure modes return synthetic
  `BlockedResult` rather than raising.
- **AutoDevResult `schema_version: 2`** (#82). Adds the `no_op` status
  (skill detected the ticket already satisfied; no plan, no branch, no
  PR) and `close_issue_as_completed` advisory `next_actions` value.
  v1-tagged payloads with v2-only statuses are rejected as
  `validation_failed`. Parser accepts both v1 and v2 during the rollout
  window.
- **Headless `/auto-dev` contract spec** (#60, #69). New
  `docs/headless-contract.md` documents the producer/consumer surface
  the skill emits and `cw` consumes. Source of truth for cross-repo
  drift checks.
- **Project `/ship-it` command** (#61). `.claude/commands/ship-it.md`
  used by the auto-dev pipeline's `/prep-pr` integration.

### Changed
- **CI hardening**:
  - `mypy --strict` enforced project-wide (#73).
  - 88% baseline coverage gate (#73).
  - 90% **patch** coverage enforced via `diff-cover` on PRs (#79).
  - Nightly cmux smoke run scaffolded (#73), launching cmux via the
    macOS `.app` bundle (#74).
- **Worktree slug charset** (#84). `slugify_branch` now matches
  `claude -w`'s validator (`[A-Za-z0-9._-]+`) instead of just collapsing
  path separators. Unblocks GitHub-issue ticket ids like `#7` from
  `cw dev-queue` dispatch.

### Fixed
- **`start_session` parent edge cases** (#64, #72). Idempotent on
  re-spawn; rejects parent IDs that don't exist; rejects self-parent.
- **`start_session` dual `save_state`** (#63, #70). Single atomic
  write — earlier flow wrote twice and could leave linkage half-applied
  on crash.
- **Dead `WorkerEntry.missing` field** (#66). Removed; tests tightened.

### Removed
- `WorkerEntry.missing` (#66) — never read by any consumer.

## [0.6.4] — 2026-05-01

Completes the dispatch-spawn fix started in 0.6.3. The earlier release
addressed the surface-level `claude -w` validator error, but two
deeper problems still made `cw dev-queue run` non-functional end-to-end:

1. `dispatch_tick` only ran `worktree_path.mkdir(...)` — it never
   created a real git worktree. Claude would have started in a plain
   (non-repo) directory.
2. The dispatch / pr_responder / plan call sites all wrote a prompt to
   a temporary file and immediately read it back into the spawn
   command string — pointless file roundtripping that obscured the
   real call.

### Fixed
- `cw.dispatch.dispatch_tick` now calls `create_worktree(client, branch)`
  (idempotent — returns the existing path if already created) instead
  of `mkdir`-ing an empty directory. `cw.pr_responder` uses the same
  pattern for PR-event branches.
- `cw.worktree._run_git` strips `GIT_*` from the subprocess env so
  cw's git operations always target the client repo at *cwd*. Without
  this, running cw from inside a git hook (e.g. a pre-commit pytest
  run that exercises dispatch) would leak `GIT_DIR` / `GIT_INDEX_FILE`
  and produce confusing "Not a directory" errors.

### Changed
- `cw.spawn.spawn_create_impl` and `cw.pr_responder._spawn_session`
  now take a `prompt: str` rather than a `prompt_file: Path`. Callers
  inline the prompt directly. The `cw spawn` CLI reads the file at the
  user-facing boundary and passes the contents through.
- `cw.plan` still persists the planner prompt to disk for audit /
  debugging but no longer reads it back to inline into the spawn.
- `tests/conftest.py::make_git_repo` now initialises with an empty
  `main` commit (and a per-repo `user.email` / `user.name`) so callers
  that exercise `git worktree add` have something to branch from.

## [0.6.3] — 2026-04-19

Surface-level fix for a `cw dev-queue run` regression: dispatch was
running `claude -w <absolute-worktree-path>`, but `claude -w` takes a
worktree *name*. The absolute path's leading `/` made the first segment
empty and failed claude's name validator, so no DAEMON session ever
started. This release dropped the `-w` flag and `cd`s into the
worktree path. (See 0.6.4 for the rest of the fix — the path itself
was still just an empty directory rather than a real git worktree.)

### Fixed
- `cw.spawn.spawn_create_impl` and `cw.pr_responder._spawn_session`
  no longer pass `-w` to claude.

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
