# RFC 0013 — Retire the Interactive Session Surface

## Summary

`cw` grew up as a multiplexer-era session manager: `cw start` spawned a Claude
session per purpose in a cmux/tmux pane, `cw bg` injected `/session-done`
keystrokes to background it with a handoff document, `cw resume` reattached
with that handoff, `cw done` retired it, and `cw watch` drew a live flat table
of everything. None of that is how the tool is used any more. The operator
drives work exclusively through `cw dev-queue`, the `pr-channel` /
`queue-channel` push servers, and `cw board`; every session that exists today
is a daemon-spawned worker created by dispatch, not a human sitting in a pane.

This RFC deletes the interactive surface and the modules that only it used —
`session.py`, `prompts.py`, `history.py`, `tui.py` — in one epic, then retires
the `SessionOrigin.USER` concept and its three timestamp fields from the state
model in a second epic, so the twenty-odd `origin is DAEMON` guards scattered
through reconcile and dispatch become unconditional. Read-only inspection
(`cw status`, `cw list`, `cw peek`, `cw session …`) stays; the native daemon
spawn path (`cw spawn`) is the substrate everything else runs on and is out of
scope.

## Motivation

- **The operator has said so.** Only `dev-queue`, the two channels, and `board`
  are in use. The interactive commands are dead weight in `cw --help`, in the
  README command table, and in CLAUDE.md's "Common Workflows" section, which
  still teaches `cw start` / `cw bg` / `cw switch` as the lifecycle.
- **The mechanism is already gone.** cmux/tmux is referenced in `src/` only by
  comments and a config-migrate cleanup of legacy pane refs
  (`src/cw/_config_migrate.py:242`). `cw peek` reads native daemon transcripts
  since #504; `cw daemon` is a deprecated no-op stub. The commands survive, the
  thing they drove does not.
- **The USER origin is pure guard noise.** Every non-legacy consumer of
  `SessionOrigin` is an `is DAEMON` / `is not DAEMON` filter — reconcile core,
  liveness, tasks, local, main_drift, concierge, codex_boot, dispatch tick,
  gating, host_capacity, stop_hook, spawn. With no USER sessions, each guard is
  a tautology that still has to be read, tested, and kept consistent.
- **Skills carry the exclusion.** cw-fanout, cw-queue-peek, and
  cw-session-watch each explain that "USER-origin sessions started via
  `cw start`" are out of their scope. That sentence is a category that no
  longer exists.

## Design

Hard delete, no deprecation stubs. `cw` is a single-user tool and every
consumer of the removed surface is in this repository; a stub would document a
migration nobody has to make. CHANGELOG gets a `### Removed` section and the
release is a minor bump.

### Epic I — Remove the interactive session surface

Delete `cw start`, `cw bg`, `cw resume`, `cw done`, `cw watch`, and the
`cw daemon` stub from `src/cw/cli/sessions.py`, and delete the four modules
that exist only to serve them: `src/cw/session.py` (start / background /
resume / done flows and the `/session-done` injection), `src/cw/prompts.py`
(per-purpose prompt specs), `src/cw/history.py` (per-client lifecycle JSONL,
imported only by `session.py`), and `src/cw/tui.py` (`watch_flat`, imported
only by `cw watch`; `board.py` does not depend on it). Their one-to-one test
modules go with them. `cw status`, `cw list`, and `cw peek` remain in
`cli/sessions.py` as read-only inspection; `_is_native_surface_ref`, which
`dev-queue wait` imports from `session.py`, moves to `native_daemon.py` first
as a wave-0 seam so nothing in dispatch ever depends on a module being deleted.
The `auto_purposes` client config, `cw init --purpose`, and the `IDEA` / `DEBT`
purposes were consumed only by `cw start` and are retired in the same epic.
Docs, README, CLAUDE.md, INSTALL, the runbooks, the `install-cw` command, and
the three skills that mention the exclusion are updated last, together with the
CHANGELOG `### Removed` entry and the 1.46.0 version bump.

### Epic II — Retire the USER session origin

With Epic I landed, every session in `sessions.json` is daemon-origin. Delete
the `SessionOrigin` enum and the `Session.origin` field, and make each of the
`is DAEMON` guards unconditional. Bump the state schema to v19 with a
`_config_migrate` step that drops any row still carrying `origin: user` and
prints how many it dropped — none of them can be resumed, and the read-only
views should not keep showing them. Then delete the three fields that only the
background / resume flow wrote — `backgrounded_at`, `resumed_at`,
`auto_backgrounded` — whose sole remaining reader is an optional-timestamp
tuple in `orchestrate.py`, with a v20 migration that strips them. This epic
touches `stop_hook.py`, which worker Stop hooks load fresh on every fire while
a running `cw dev-queue serve` keeps the old module in memory, so its tickets
ship only against a drained queue followed by an operator restart of `serve`.

## Phasing

| Wave | Epic I — surface | Epic II — model |
|------|------------------|-----------------|
| 0 (seam, blocking) | S1 — relocate `_is_native_surface_ref` | — |
| 1 | A1 — delete commands + modules · A2 — retire `auto_purposes` vocabulary | — |
| 2 | A3 — docs, skills, CHANGELOG, 1.46.0 | B1 — delete `SessionOrigin`, schema v19 |
| 3 | — | B2 — delete background/resume timestamps, schema v20 |

Sprint 1 is Epic I (S1, A1, A2, A3). Sprint 2 is Epic II (B1, B2). B1 depends
only on A1 and may be dispatched as soon as A1 merges, but it is held for the
operator gate described in D-B1.

## Resolved decisions

- **D-S1 — Hard delete, no stubs.** Removed commands are deleted outright; no
  `cw daemon`-style no-op stubs are added, and the existing `cw daemon` stub is
  deleted too. `cw` is a single-user tool whose every consumer is greppable in
  this repo. Reverses at: never.
- **D-S2 — Read-only inspection survives.** `cw status`, `cw list`, and
  `cw peek` stay. `peek` reads native daemon transcripts (#504) and is the PEEK
  step of the queue-peek ladder; `status` / `list` are the operator's
  cross-client glance. Reverses at: ticket hardening, if a kept command turns
  out to import a deleted module.
- **D-S3 — `cw spawn` is substrate, not surface.** The native daemon spawn path
  and `cw spawn close` are what dispatch and four skills run on. Out of scope.
  Reverses at: never.
- **D-S4 — Anything the orchestrator actually uses is pulled back.** If a
  ticket's worker or the operator finds a removed command or module in use by
  dispatch, reconcile, doctor, or a skill while orchestrating, that item is
  retained and the ticket re-scoped, no re-adjudication needed. Reverses at:
  ticket hardening or plan review.
- **D-A1 — Regression pin on the CLI group.** A1 adds a test asserting that
  `main.commands` contains none of `start`, `bg`, `resume`, `done`, `watch`,
  `daemon`, so nothing re-registers them. Reverses at: never.
- **D-A2 — Purpose vocabulary shrinks to what dispatch creates.**
  `SessionPurpose` keeps `IMPL`, `ORCHESTRATE`, `FIX`; `IDEA` and `DEBT` go
  with `auto_purposes`. A stale `auto_purposes:` key in `clients.yaml` is
  dropped by config-migrate with a notice, not an error. Reverses at: plan
  review, if a lane or executor is found constructing `IDEA` / `DEBT`.
- **D-A3 — Minor release.** Epic I ships as 1.46.0 with a `### Removed`
  CHANGELOG section listing every command and module. Reverses at: never.
- **D-B1 — Origin retires with a forward-only migration and an operator gate.**
  `SessionOrigin` and `Session.origin` are deleted, every `is DAEMON` guard
  becomes unconditional, state schema v18→v19 drops `origin: user` rows with a
  count in the notice. Because `stop_hook.py` changes, B1 merges only when
  `cw dev-queue status --json` shows no RUNNING rows, and the operator restarts
  `cw dev-queue serve` afterwards. Reverses at: never.
- **D-B2 — Orchestrator links are not USER-era.** `parent_session_id` and
  `worker_session_ids` are written by `spawn.py` and read by `orchestrate.py`;
  they stay. Only `backgrounded_at`, `resumed_at`, `auto_backgrounded` are
  removed, schema v19→v20. Reverses at: never.

## Tickets

### S1 — Relocate `_is_native_surface_ref` out of session.py

- **Epic:** none
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** `cw dev-queue wait` imports `_is_native_surface_ref` from `src/cw/session.py`, the module A1 deletes; move the predicate to `src/cw/native_daemon.py` next to the surface-ref readers it describes and repoint the import, so dispatch never depends on a module slated for deletion.
- **Scope:** D-S3, D-S4
- **Acceptance:**
  - `_is_native_surface_ref` is defined in `src/cw/native_daemon.py` and `src/cw/session.py` re-exports nothing new.
  - `src/cw/cli/dev_queue/wait.py` imports it from `cw.native_daemon`; no other module imports it from `cw.session`.
  - Existing `wait` tests pass unchanged; all nine local gates green.

### A1 — Delete the interactive commands and their modules

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** Delete `start`, `bg`, `resume`, `done`, `watch`, and the `daemon` stub from `src/cw/cli/sessions.py`, delete `src/cw/session.py`, `src/cw/prompts.py`, `src/cw/history.py`, `src/cw/tui.py` and their test modules, and keep `status`, `list`, `peek` (and the `_complete_session` completer `peek` still uses) working.
- **Scope:** D-S1, D-S2, D-S3, D-S4, D-A1
- **Acceptance:**
  - `cw --help` lists none of `start`, `bg`, `resume`, `done`, `watch`, `daemon`; a test asserts `main.commands` excludes all six.
  - `src/cw/session.py`, `prompts.py`, `history.py`, `tui.py` and `tests/test_session.py`, `test_prompts.py`, `test_history.py`, `test_tui.py`, `test_session_handoff_compact_repr.py` no longer exist; no `from cw.session|prompts|history|tui` import remains in `src/` or `tests/`.
  - `cw status`, `cw list`, `cw peek` run against a populated `sessions.json` and render as before.
  - `.claude/scripts/check_imports.py` passes; all nine local gates green; total coverage stays at or above 88%.

### A2 — Retire the auto_purposes vocabulary

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** A1
- **Context:** `ClientConfig.auto_purposes`, `DEFAULT_AUTO_PURPOSES`, `cw init --purpose`, `_VALID_PURPOSES` / `_validate_purposes` in `config.py`, and `SessionPurpose.IDEA` / `DEBT` were consumed only by `cw start`; remove them and have config-migrate drop a stale `auto_purposes:` key from `clients.yaml` with a notice.
- **Scope:** D-A2, D-S4
- **Acceptance:**
  - `SessionPurpose` has exactly `IMPL`, `ORCHESTRATE`, `FIX`; grep finds no `IDEA` or `DEBT` purpose in `src/` or `tests/`.
  - `ClientConfig` has no `auto_purposes` field; `cw init` has no `--purpose` option; `config/CONFIG_REFERENCE.md` and `config/` examples no longer mention it.
  - Loading a `clients.yaml` that still carries `auto_purposes:` succeeds and logs one notice naming the client and the dropped key.
  - All nine local gates green.

### A3 — Docs, skills, CHANGELOG, and the 1.46.0 bump

- **Epic:** I
- **Wave:** 2
- **Sprint:** 1
- **Depends on:** A1, A2
- **Context:** Remove the interactive lifecycle from README's command table and Architecture section, CLAUDE.md's Common Workflows and Architecture Decisions, `docs/INSTALL.md`, `docs/dispatch-runbook.md`, `docs/session-disposition.md`, `.claude/commands/install-cw.md`, and the USER-origin exclusion sentences in cw-fanout, cw-queue-peek, cw-session-watch; add the CHANGELOG `### Removed` section and bump to 1.46.0 with `uv.lock` regenerated.
- **Scope:** D-A3, D-S1, D-S2
- **Acceptance:**
  - grep for `cw start`, `cw bg`, `cw resume`, `cw done`, `cw watch`, `cw switch`, `/session-done`, and `Keystroke injection` returns nothing under `README.md`, `CLAUDE.md`, `docs/`, `.claude/skills/`, `.claude/commands/` except CHANGELOG history and this RFC.
  - CHANGELOG `## [1.46.0]` carries a `### Removed` section naming every deleted command and module.
  - `pyproject.toml` version is 1.46.0 and `uv lock --check` is clean.
  - `tests/test_changelog_gate_prose_sync.py` and every doc-conformance test pass.

### B1 — Delete SessionOrigin and the USER guards

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** A1
- **Context:** Delete the `SessionOrigin` enum and `Session.origin`, make every `origin is DAEMON` / `is not DAEMON` guard in reconcile, dispatch, executor, spawn, stop_hook, and codex_boot unconditional, remove the USER-only branches in `spawn.py` and `stop_hook.py`, delete the origin backfill from `_config_migrate.py`, and add a schema v18→v19 migration that drops `origin: user` rows with a count in its notice.
- **Scope:** D-B1, D-S4
- **Acceptance:**
  - `SessionOrigin` no longer exists; grep for `origin` on `Session` returns nothing in `src/` or `tests/`.
  - `CW_STATE_SCHEMA_VERSION` is 19; loading a v18 `sessions.json` with USER rows drops them, keeps DAEMON rows byte-for-byte, and logs one notice with the dropped count.
  - Every reconcile and dispatch test that constructed a `Session(origin=…)` is updated rather than deleted, and the affected code paths keep their coverage.
  - Ticket is not finalized while `cw dev-queue status --json` reports any RUNNING row for any client; the PR description tells the operator to restart `cw dev-queue serve` after merge.

### B2 — Delete the background/resume timestamps

- **Epic:** II
- **Wave:** 3
- **Sprint:** 2
- **Depends on:** B1
- **Context:** Remove `backgrounded_at`, `resumed_at`, and `auto_backgrounded` from `Session`, drop them from the optional-timestamp tuple in `orchestrate.py`, and add a schema v19→v20 migration that strips them from existing rows; `parent_session_id` and `worker_session_ids` stay.
- **Scope:** D-B2
- **Acceptance:**
  - `Session` has none of the three fields; `orchestrate.py`'s timestamp tuple reads `(idle_at, completed_at)` only.
  - `CW_STATE_SCHEMA_VERSION` is 20; a v19 row carrying the fields loads cleanly and re-saves without them.
  - `parent_session_id` and `worker_session_ids` behavior in `spawn.py` and `orchestrate.py` is unchanged and its tests pass.
  - All nine local gates green.

## References

- `src/cw/cli/sessions.py:44` — `cw start` registration; `:69` `bg`, `:113` `resume`, `:135` `watch`, `:152` `done`, `:281` `daemon` stub; `:121` `list`, `:128` `status`, `:406` `peek` are the survivors.
- `src/cw/cli/sessions.py:34` — the only import of `start_session` / `background_session` / `resume_session` / `done_session`; `:41` the only import of `cw.tui.watch_flat`.
- `src/cw/session.py:69` — `_is_native_surface_ref`, imported by `src/cw/cli/dev_queue/wait.py:39`; the one live dependency dispatch has on the module.
- `src/cw/session.py:151` `start_session`, `:313` `background_session`, `:441` `resume_session`, `:554` `done_session` — the four flows being deleted.
- `src/cw/prompts.py:55` — `_PROMPT_SPECS` keyed by `SessionPurpose.IMPL|IDEA|DEBT`; only `session.py` imports this module.
- `src/cw/history.py:1` — lifecycle JSONL; only `session.py` imports it.
- `src/cw/tui.py:308` — `watch_flat`; `src/cw/board.py` does not import `cw.tui`.
- `src/cw/cli/_base.py` — `_complete_session` stays for `peek`; `_complete_client` stays (dev-queue, orchestrate, worktree, maintenance).
- `src/cw/config.py:714` — `_VALID_PURPOSES` / `_validate_purposes`; `:689`, `:730`, `:752`, `:813` the `auto_purposes` init path; `src/cw/cli/maintenance.py:276` `cw init --purpose`.
- `src/cw/models/enums.py:14` — `SessionPurpose` members `IMPL`, `IDEA`, `DEBT`, `ORCHESTRATE`, `FIX`; `src/cw/models/client.py:17` `DEFAULT_AUTO_PURPOSES`, `:53` `auto_purposes` field.
- `src/cw/models/session.py:53` — `origin: SessionOrigin = SessionOrigin.USER`; fields `auto_backgrounded`, `backgrounded_at`, `resumed_at`, `parent_session_id`, `worker_session_ids` in the same model.
- `src/cw/models/state.py:33` — `CW_STATE_SCHEMA_VERSION = 18`.
- `src/cw/_config_migrate.py:113` — resets unknown origins to `user`; `:131` backfills `parent_session_id` / `worker_session_ids` (stays); `:242` clears legacy cmux/tmux pane refs (stays as history).
- `src/cw/cli/stop_hook.py:484`, `:497`, `:522`, `:556`, `:573` — origin guards, including the USER-only branch at `:497`; changes here are why B1 needs a drained queue.
- `src/cw/spawn.py:368`, `:393`, `:417`, `:425`, `:603`, `:620` — origin parameter and the USER worktree-settings branch; `:668` writes `parent_session_id`.
- `src/cw/reconcile/core.py:131`, `:201`; `reconcile/liveness.py:205`; `reconcile/tasks.py:159`, `:503`, `:564`; `reconcile/local.py:89`; `reconcile/main_drift.py:100`; `reconcile/concierge.py:704`; `reconcile/_shared.py:668`; `reconcile/codex_boot.py:85`; `dispatch/tick.py:151`; `dispatch/gating.py:108`; `dispatch/host_capacity.py:76`; `executor.py:443`, `:696`, `:910` — the `is DAEMON` guards B1 makes unconditional.
- `src/cw/orchestrate.py:831` — the only reader of `backgrounded_at` / `resumed_at`; `:892` reads `worker_session_ids` (stays).
- `tests/test_session.py`, `tests/test_prompts.py`, `tests/test_history.py`, `tests/test_tui.py`, `tests/test_session_handoff_compact_repr.py` — test modules deleted with their sources; `tests/test_session_retention.py`, `tests/test_sessions_lock.py`, `tests/test_session_groups.py`, `tests/test_session_inspect.py` test surviving code and stay.
- `README.md:123` — command table rows for `start` / `bg` / `resume` / `done` / `list`; `:387` "Keystroke injection" architecture bullet; `:359` `auto_purposes` config row.
- `CLAUDE.md:182` — Common Workflows lifecycle; `:211` Keystroke injection decision; `:243` USER-origin `worker_model` note.
- `docs/INSTALL.md:190`, `:307`; `docs/dispatch-runbook.md:546`; `docs/session-disposition.md:248`; `.claude/commands/install-cw.md:96`, `:108` — `cw start` / `cw done` prose.
- `.claude/skills/cw-fanout/SKILL.md:39`, `:264`, `:370`; `.claude/skills/cw-queue-peek/SKILL.md:38`; `.claude/skills/cw-session-watch/SKILL.md:30` — USER-origin exclusion sentences and `cw watch` mentions.

## Handoff Brief

**Authored:** 2026-09-04 at 87f85939
**Exit:** 1 sprint block

### Decisions

Projected verbatim into `## Resolved decisions` above; ids D-S1 … D-B2 are stable.

### Rejected approaches

- **Deprecation stubs for one release** — doubles the PR count for a tool with no external consumers; the `cw daemon` stub it would copy is itself being deleted.
- **Unregister only** — detaches the commands from the CLI group but leaves four modules and ~3,500 test lines in place; zero cleanup benefit.
- **Everything in one epic** — the model cleanup touches `stop_hook.py`, which a live `serve` loop and fresh worker hooks load differently; sequencing it after the surface removal keeps the operator gate to one ticket.
- **Also removing `cw peek`** — rejected because peek reads native daemon transcripts and is the PEEK rung of the queue-peek ladder; it has no cmux dependency.

### Evidence

Projected into `## References` above.

### Do not re-adjudicate

- **Cut from scope:** `cw spawn`, `cw session`, `cw queue`, `cw orchestrate`, `cw focus`, `cw upgrade-workers`, `cw completion` — all native-daemon or dispatch surface.
- **Cut from scope:** `parent_session_id` / `worker_session_ids` — orchestrator links, not USER-era.
- **Deferred:** whether `cw status` / `cw list` should later fold into `cw board` → operator, at their discretion after Epic II.
- **Exit choice:** Exit 1 because the two-epic sequencing with an operator gate on B1 needs milestone/epic structure, not a flat ticket list.

### Orchestrator brief

- **Drives:** `sprint-buildout` files the milestone, two epics, six tickets; then `cw dev-queue add` S1 → A1 → (A2, B1) → (A3, B2) on `claude-workspace`, default lane, with `--hold-finalize` on B1.
- **Gates:** B1 finalizes only when `cw dev-queue status --json` shows no RUNNING rows; operator restarts `cw dev-queue serve` after B1 merges. A3 and B1 both touch CLAUDE.md-adjacent docs and should not run concurrently if the plan stage flags a shared file.
- **Terminal state:** 1.46.0 released with Epic I; Epic II merged with schema v20; `cw --help` shows no interactive commands; grep for `SessionOrigin` returns nothing.

## Issues

Milestone: [v1.46.0 — Retire the Interactive Session Surface](https://github.com/mattwwarren/claude-workspace/milestone/15)

Epics: #2126 (I — Remove the interactive session surface), #2127 (II — Retire the USER session origin)

Issues: S1 #2128 · A1 #2129 · A2 #2130 · A3 #2131 · B1 #2132 · B2 #2133
