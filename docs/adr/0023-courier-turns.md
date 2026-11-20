# The outbox drain posts MCP tracker writes through courier turns

**Status:** Accepted
**Driven by:** #3740 (RFC 0019 O2b, D-B3, D-B5, D-B6, D-B8); #3695 (plugin-app argv); #3368 (project-layer MCP reader); #3383 (status vocabulary); #3760 (RFC 0019 T2b, D-D2, the refresh turn)

## Decision

An outbox entry whose tickets binding has a codex MCP realization is posted by
one codex agent turn per drain step (`cw.tracker_io._drain`), not by `gh`. A
declared server (an `mcp_servers` entry in the user or system layer) takes one
post turn, checked by its read-back. A plugin app (no such entry) takes three
turns: a before snapshot, the post, and an after snapshot, and the entry is
delivered only when the before/after delta is exactly the expected marker
comments (`cw.tracker_io._snapshot_check.compare_snapshots`). Nothing is
retried. A timeout or unparseable result is `unverified`.

## Invariants

1. A `per_server` entry is never `delivered` unless the snapshot delta matches
   exactly: one new comment per expected marker, no removed comment or marker,
   and state, assignee and labels unchanged.
2. The agent marker is appended to every courier part except when the entry's
   own `operator_authored` flag is true. The flag is read from the entry and is
   never inferred from the binding or the tracker (R21).
3. A config layer that exists but cannot be read or parsed refuses every turn of
   that entry before launch (`MCP_SERVER_UNNAMED`, binding-level UNREACHABLE).
   A missing config is "nothing declared" and proceeds. On `per_server` every
   turn counts as write-capable, because read-only cannot be enforced without a
   per-tool allowlist (fail-narrower).
4. One gate record per (client, binding) episode for every unreachable release.
   The "already filed" bit is `gate_filed` on the existing unreachable marker, so
   a tick never re-files while the marker is set. A failing filer is retried on
   the next tick and never fails a drain.
5. The `turns` payload key appears only on outbox events that followed a courier
   turn. It never appears on `gh`-drain events, skipped events, or pre-launch
   releases.

## What this means for callers

- `ToolNarrowing` (`per_tool`, `per_server`, `none`) from H1 is reused. The
  declared/app split is a per-binding refinement of H1's codex `PER_TOOL`
  declaration: a server declared in the user or system layer is `per_tool`, and
  an undeclared server is a plugin app (`per_server`).
- `post_parts_via_turn` keeps its name and signature. Its only behaviour change
  is invariant 3: a launched write-capable turn refuses when a config layer is
  unreadable. This reaches `codex_stage_post` and `codex_review_persist` through
  the shared function. Both are named in the CHANGELOG.
- `declared_server_names` is a thin wrapper over `scan_declared_servers` with its
  old contract. Its three sibling callers (`codex_plan`, `codex_finalize`,
  `codex_impl`) are unchanged; they keep the project-layer-blind, fail-open
  posture. Filed as debt.

## Unverified premises (operator checklist)

Both are accepted premises (RFC 0019 row:2). They are **unverified until run on a
plugin-app Linear host**. CI cannot check them.

- **R23.** codex accepts `-c apps.<id>.default_tools_enabled=true` for a
  plugin-app binding. The argv is built with that key only; no other key is
  guessed. If the key is wrong, the app turn sees no tools and reports
  `tool_unavailable`. The entry is released with the unreachable marker, one
  pending event is emitted, and a gate record is filed. A post is never lost.
- **R24.** Linear keeps an HTML-comment marker line in a posted comment and
  returns it from the list tool. The fixture
  `tests/fixtures/tracker_io/linear_marker_probe.json` is out of this change
  because it needs a live host. If Linear strips the comment, dedup is blind and
  an entry can re-post, and read-back settles `unverified/readback_mismatch`,
  never `delivered`.

## Blast radius of a `per_server` turn

The agent holds the app's full tool surface during the turn. For Linear, the only
app host in O2b, that includes other issues, issue creation and state changes.
The delta compares only this ticket's comment ids, markers, state, assignee and
labels. There is no pre-write restriction and no automatic undo. On a failed
delta the entry settles `unverified/readback_mismatch`, and the event is
forwarded to the operator. The operator inspects the ticket's activity and the
tracker's audit log, then reverts by hand. A client opts out with
`outbox.enabled: false`.

Other apps are not enumerated, so disabling them is best effort. Adding an app
enumeration later is a one-parameter change to the argv builder.

## The refresh turn (#3760, RFC 0019 T2b, D-D2)

- **Kind and allowlist.** `TurnKind.REFRESH` is one read-only courier turn. Its
  allowlist is exactly the binding's `get` and `list_comments` tool names,
  resolved through the binding with the `TRACKER_GET_TOOLS` and
  `TRACKER_COMMENT_TOOLS` fallback.
- **Result parsing.** The result is the bare T1 stdin document (`ticket_url`,
  `title`, `body`, `comments[...]`), parsed by a strict no-defaults model and
  then by the existing `snapshot_from_stdin`. cw computes `agent_authored`; the
  model never supplies it or `author_conflict`. There is no model-reported
  status, detail or ticket-id echo: a failure is a missing output file,
  non-JSON or schema-invalid output, mapped to the closed refresh reasons.
- **Independence and runner.** The turn is its own codex launch through the
  codex runner the stage already holds, so a stage on a harness without the
  tracker MCP still refreshes. Its usage is recorded through that stage's
  runner; the CLI confirm seam uses an unrecorded `RealCodexRunner`, which is
  the courier's existing usage gap.
- **Failures and exception contract.** They degrade to
  `ticket.snapshot_degraded` (`courier_unreachable`, `turn_failed`, `refused`,
  `no_binding`), the stage runs on the last snapshot, and there is no park and
  no gate record. `courier_snapshot` catches only a listed narrow set
  (`CwError`, `OSError`, `ValueError`, `subprocess.SubprocessError`) and maps it
  to `turn_failed`; any other exception propagates deliberately.
- **Timeout.** The turn timeout (300 s) and the review deadline bound it; there
  is no blind retry.
- **Cadence and cost.** The turn runs once per review pass, including a claude
  confirm round run from the codex stage (it shares the stage's context), plus
  once at IMPL entry and once at FINALIZE entry. This is per pass, not per
  recheck like the verify turn, so a fix loop with many passes runs many turns.
- **Residual exposure: `per_server` blast radius.** A `per_tool` harness gets the
  exact two-tool allowlist. On a `per_server` plugin app the whole app is enabled
  for a prompt-only read-only turn that reads untrusted ticket text on every
  pass. See "Blast radius of a `per_server` turn" above: that surface includes
  other issues, issue creation and state changes, and the higher cadence widens
  the window an injected comment has. A write by this turn is NOT detected: no
  before/after snapshot check is run (it would triple the cost, accepted). There
  is no per-client switch; see Reversal.
- **Residual trust: model-reported fields.**
  - `author_id` and `created_at` are model-reported by RFC design (D-D3) and feed
    the comment-binding evaluator. An injected comment body could make the model
    fabricate a new comment attributed to an operator id, and cw cannot detect a
    new fabricated comment. The consequence is that the fabricated text can bind
    as an operator comment (steering, the approved plan) in the next review pass,
    IMPL plan of record, or FINALIZE. This is accepted.
  - Comment order is prompt-level only, so a mis-ordered thread could change
    which comment is "newest".
  - A document describing the wrong ticket is also undetected, since there is no
    echo.
  - A model that cannot reach the tools may still emit a schema-valid document
    with empty comments. It is recorded as the fresh snapshot, so operator
    comments then bind nothing for that pass (fail-closed) until the next
    refresh.
- **Author conflicts.** The MCP refresh records with
  `detect_author_conflicts=True`, the same trust class as
  `cw ticket snapshot --from-json`; the GitHub refresh stays `False`. The flag is
  sticky: a model-reported `author_id` flip for an existing comment id sets
  `author_conflict` and permanently unbinds that comment, an unrecoverable loss
  of steering for it (fail-closed). `ticket.snapshot_author_conflict` can now
  fire on refresh. This is accepted. The snapshot the stage uses in memory is the
  stored one (`SnapshotRecord.snapshot`), so a conflict flagged by this record
  already applies to the same pass.
- **Premises and reversal.** R23 (the `apps.<id>.default_tools_enabled=true` key)
  also gates this turn. The reversal has no config switch and is code-level. For
  REVIEW (`_review_and_confirm`), dropping `refresh=True` is the whole reversal,
  T2's documented one-liner (the courier is consulted only inside the refresh).
  IMPL and FINALIZE call `snapshot_for_stage(..., courier=...)` directly with no
  `refresh=True`, and T2 keeps their stage-entry refresh, so the courier is
  disabled there by dropping the `courier=` argument at that hook. For the CLI
  confirm seam, the courier comes from `_courier_for` in `run_backend_round`.

## Consequences

- Each entry on a plugin-app host costs three turns, up to 300 s each. A drain of
  20 entries can run for hours. There is no wall-clock budget or timeout breaker
  (debt).
- The recheck verify turn on `per_server` runs before the before snapshot, so it
  is not bracketed by the delta. Its read-only nature is prompt-level.
- A hard kill after a successful post leaves a claimed entry that the next run
  restores to pending and re-posts without a verify turn. This is O1/O2 store
  behaviour, recorded as a known limit.
- A mistyped binding `server` name, or a server declared only in a project
  layer, is classed as an app. It then gets the app's full tool surface, guarded
  only by the delta.
- A cause change inside one gate episode does not file a second record. The
  marker clears on the first delivered role or on `cw outbox drain --force`.
- Courier turns have no owning session, so they record no usage. The launch
  site is shared with the legacy turn (`launch_turn`, `cw.tracker_io._turn`),
  which records through `flush_late_usage`; a courier turn's usage is lost
  (known gap, debt).
- `OutboxConfig.enabled` defaults to true, so every client with an MCP tickets
  binding and a codex realization starts making agent turns on the next tick
  after upgrade. Opt out per client with `outbox.enabled: false`.

## Alternatives considered

- A second narrowing enum. Rejected: it would duplicate H1's vocabulary.
- An `apps.*` reader or guessed keys (`apps._default.*`). Rejected: no code path
  can enumerate apps, and guessed keys would fail silently.

## Referenced by

- #3740, #3695, #3368, #3383, #3760, ADR-0013 (2026-11-10 update, superseded in part)
