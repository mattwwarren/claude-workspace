# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`cw worktree gc`'s default all-clients run no longer silently truncates
  on a per-client failure** (#1389): the per-client loop had no failure
  isolation — an exception while processing one client aborted the whole
  sweep, silently skipping every client ordered after it (e.g. 16
  git-registered worktrees on one client going completely unexamined with no
  error, skip line, or summary). Each client's GC pass is now wrapped so a
  failure prints `[<client>] ERROR — <reason>` and the sweep continues to the
  next client; a `[<client>] N examined / M skipped` summary line makes a
  short/incomplete per-client pass visible instead of reading as a complete
  sweep. Exit code intentionally stays 0 on partial failure this round (see
  #1399 for the deferred exit-code-contract decision).
- **`_claim_next_pending` no longer starves younger pending tasks behind an
  attempt-capped task** (#1248): the plain pending-scan used to `return None`
  the instant it hit a task at the global attempt ceiling, abandoning the
  rest of the sorted pending list for that tick. On a `max_parallel: 1` lane
  this was indefinite head-of-line starvation — the capped task parks
  `BLOCKED_ON_USER` (occupying the lane's only slot) while a claimable task
  sorted behind it is never reached. The attempt-cap branch now `continue`s
  the scan instead of returning, matching the backoff branch immediately
  above it.
- **Diagnostics bundle filenames no longer overwrite on repeat failures**
  (#1330): a diagnostics bundle filename used to be
  `<role-slug>-<reason>[.json|-schema.json|-output.json]` with no
  disambiguator, so a second same-role/same-category failure within one
  session silently clobbered the first's bundle files. Filenames now include
  the failure's `occurred_at` microsecond timestamp
  (`<role-slug>-<category>-<timestamp>...`), so every failure gets its own
  set of files.
- **`cleanup_expired_diagnostics` no longer walks the filesystem on every
  dispatch tick** (#1330): the sweep is now internally throttled to at most
  once per hour via a sentinel file under `state_dir()`, independent of the
  configured retention window.

### Documentation

- **Aider `edit-format` guidance for the local backend** (#1204): documents
  that aider's `whole`-mode default for model ids it doesn't recognize (i.e.
  most non-Claude models) can regenerate the entire target file on every
  edit and blow a stage timeout — a symptom that looks like a model/timeout
  failure rather than the real cause. Operators should set
  `edit-format: diff` in `~/.aider.conf.yml` (or `AIDER_EDIT_FORMAT=diff` in
  the dispatcher's own environment before it starts) to force `diff` mode.
  Doc-only — no `src/cw` changes; edit-format policy stays with aider's own
  config/env precedence rather than a new `cw` passthrough flag.

## [1.21.0] — 2026-07-17

Dispatch-latch hardening plus installer ownership of subagents. The two
`*_fired_at` latches stop recipe re-dispatch storms, `disallowed_mcp_tools`
moves worker tool policy from a tracker heuristic to explicit operator config
(**see MIGRATION below**), and cw's installer now owns agent definitions
outright.

### Added

- **Subagents install globally alongside commands and skills** (#1278):
  `scripts/install-skills.sh` now installs `.claude/agents/*.md` into
  `~/.claude/agents/`, tracked in the same manifest and covered by the same
  manifest-scoped prune invariant (an agent present only in the destination is
  never pruned). `EXCLUDED_AGENTS` withholds experiment-scoped agents from the
  global install, mirroring the existing `EXCLUDED_COMMANDS` precedent. cw is
  now the single source of truth for agent definitions, which previously lived
  as a drifting duplicate set in the `global-claude` checkout.
- **`auto_fix_ci_fired_at` latch + schema v16→17** (#1205): the `auto_fix_ci`
  recipe now re-dispatches once per ci-failing episode instead of spawning a
  worker session on every reconcile tick until hydration catches up.
- **`address_review_fired_at` latch + schema v17→v18** (#1206): the
  `address_review` recipe now dispatches once per changes-requested episode
  instead of re-dispatching an `/address-review` session on every reconcile
  tick until a human re-reviews.

### Changed

- **Daemon worker MCP-tool restrictions are now operator-configurable.** The
  hard-coded, tracker-gated block that stripped Linear MCP tools from
  `github-issues`-client DAEMON workers (added in #726 to avoid a headless
  Linear-OAuth stall) is replaced by a global `disallowed_mcp_tools` list on
  `OrchestratorConfig` (`~/.claude-workspace/orchestrator.yaml`), default empty
  and applied uniformly at both DAEMON spawn chokepoints. cw no longer decides
  tool availability from a tracker heuristic; the operator declares the exact
  deny patterns. **MIGRATION:** any `github-issues` client that relied on the
  old automatic block must now set
  `disallowed_mcp_tools: ["mcp__plugin_linear_linear__*"]` explicitly, or its
  workers will have Linear MCP tools available (a Linear-tracked ticket
  mis-routed to such a client previously blocked at pre-flight with zero Linear
  tools). The single `--disallowed-tools=` token form (#733) is preserved.
- **Premise gate gains a self-verified-→-proceed path** (#1192): the auto-dev
  plan stage's premise contract now distinguishes evidence quality instead of
  treating every unverified factual claim identically. A premise the
  plan-stage agent settled itself with authoritative evidence (official docs,
  `<tool> --help` output, source, or a quoted command + its verbatim output
  from this session) is recorded under a new `## Self-Verified Premises` plan
  section and `friction_highlights`, and the run proceeds; a premise with
  absent, ambiguous, self-contradictory evidence, or one turning on operator
  intent still parks with `premises_pending_verification` exactly as before.
  Headless-only, mirrors the existing ambiguity `Recommendation: ADOPT | PARK`
  fast path. Skill-markdown only — no `src/cw` or schema changes.

## [1.20.0] — 2026-07-15

RFC 0011 availability- & counterparty-aware holding — wave-0 seams plus the
first two epic tickets, all shipped dark or naturally inert (the counterparty
axis has no live "external" producer yet, so the new park class and idle-reap
exemption only fire once a later ticket wires real PR-author comparison).
Alongside it: groundwork for a native `/sprint-buildout` RFC→ticket pipeline
(`cw.sprint` parser, config/plan builder, gh creation helpers — no `cw sprint`
command yet, that lands with the remaining tasks), two webhook/config security
hardenings, and a batch of dispatch/reconcile correctness fixes surfaced while
dogfooding the sprint's own dispatch.

### Added

- **RFC 0011 S1 — counterparty axis + operator self-identity** (#1153):
  `derive_counterparty` (self|external), `ClientConfig.operator_github_login`
  override, and `cw.gh.current_gh_login` promoted to a public contract. Always
  resolves "self" today — no candidate-selection path can reach a PR authored
  by anyone but the operator yet.
- **RFC 0011 S2 — native review-requested register** (#1154): a `WatchedPr`
  model (schema v14→15) for externally-requested PRs, `register_watched_pr`,
  watched-PR hydration wired into `hydrate_pr_states`, a `review_requested`
  webhook event, and a new `cw review register` CLI command.
- **RFC 0011 A1 — `awaiting_operator` park class** (#1155):
  `OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, a distinct axis from
  `FINALIZE_REGRESS_BLOCKER_REASONS` meaning "operator/dependency unreachable"
  rather than "leg is broken." `push_auth_failed` (#1049) is retro-classified
  as the first instance.
- **RFC 0011 B1 — external-counterparty idle-reap exemption** (#1158): a
  DAEMON session idling while reviewing a teammate's PR now escalates to the
  operator via `SESSION_NEEDS_ATTENTION` instead of being silently
  parked/reaped.
- **`request_reviewer_fired_at` latch + schema v15→16** (#1197): the
  `request_reviewer` recipe now fires once per no-reviewer episode instead of
  re-requesting on every reconcile tick.
- **`cw-deps` dependency drift check** (#1077): `cw doctor` now warns when the
  running `cw` interpreter is missing a distribution declared in the source
  `pyproject.toml` — the drift class that crash-looped `cw dev-queue serve`
  after #1075 added `psutil` without a venv resync.
- **`cw dev-queue requeue --from-failed`** (#1190): a FAILED row whose
  underlying session actually completed clean now has a CLI path back to
  PENDING, mirroring the existing `--from-cancelled` escape hatch.
- **Sprint-buildout groundwork** (#1174, #1193, #1209): `cw.sprint`'s
  RFC→ticket parser (`docs/rfcs/TEMPLATE.md` as the strict input contract),
  `BuildoutConfig`/`build_plan`, and gh issue/milestone creation helpers —
  foundation tasks for an upcoming `cw sprint plan|apply` command; the CLI,
  idempotent apply, and acceptance fixture are not in this release.
- **`ConfigValidationError` + `extra="forbid"` on config-facing models**
  (#1200): a typo'd `clients.yaml`/`orchestrator.yaml` key (e.g.
  `review_recipies`) now surfaces as an actionable error at load time instead
  of silently resolving to a hardcoded default.
- **`CodexExecutor` parses real findings** (#1203): codex review counts now
  come from structured `--output-schema` JSON output instead of a hardcoded
  clean review on every exit.
- **ADR-0012 — cw never grants a GitHub review approval** (#1199): documents
  the invariant and adds a guard test scanning for the GraphQL/REST call
  shapes that would violate it.

### Fixed

- **`/pr-event` default-denies when the HMAC secret is unset** (#1127):
  previously fell through unauthenticated; now returns 401 unless
  `--allow-unsigned` is explicitly passed to `cw pr-channel serve`.
- **Plan-review marker comments now require an authorship match** (#1128):
  `fetch_approved_plan_comment` no longer trusts a "plan reviewed" marker
  comment posted by anyone other than the authenticated `gh` identity.
- **`ticket_id` charset widened to permit `#`** (#1184): the #1129 validator
  rejected `repo#N`-shaped ids already live in production (e.g.
  `redact-api#1`), which would have taken down the dev queue for every client
  on the next upgrade since one bad row fails the whole store's validation.
  `#` is now percent-encoded only at the one sink where it's actually
  dangerous (a `gh` API URL path segment).
- **Later-stage sentinel self-escalation walks the stage pointer forward**
  (#1149): a sentinel mapped to a later pipeline stage now advances
  `task.stage` one rung at a time instead of silently no-op-ing — the fix for
  the 76-event Stop-hook storm on session `cbfdc122`. Refusal loops now latch
  instead of re-proposing the same doomed candidate every tick.
- **`SESSION_NEEDS_ATTENTION` now emits on every `blocked_on_user` park**
  (#1117): previously only Rule 2 emitted attention; Rule 3b
  (`merge_pending`), the Rule 5 fallthrough, and Rule 6 (unparseable
  sentinel) parked silently.
- **Transcript liveness derives from the last content-bearing record, not
  mtime** (#1076): Claude Code's metadata-only transcript writes (ai-title,
  permission-mode, etc.) no longer reset a stalled session's idle counter and
  mask a real stranding.
- **Salvage LOW path merges into `last_result` instead of replacing it**
  (#1105): a wholesale replace was destroying pre-existing keys (`status`,
  `review`) that `cw dev-queue approve` depends on to detect an approval gate.
- **`events._parse_lines` tolerates an unknown event type** (#1210): a line
  written by a newer producer no longer crashes `read_events`/`prune_events`;
  only skipped and summarized in one warning per call.
- **`approve` on an unreviewed plan re-parks at PLAN, not IMPL** (#968): a
  Large-scope plan approved before its quality review ran previously advanced
  straight to Stage 2 against an empty `.cw/plan.md`.
- **Forbidden-area classification inspects diff content, not just path**
  (#1104): a `.github/workflows/**` change with no actual pipeline-logic diff
  no longer permanently sticks a ticket at large-tier; Stage 3 can one-time
  downgrade a Stage-1 false positive.
- **Push-path PR hydration recomputes `attention_state`** (#1196): a
  webhook-driven update previously carried the prior `attention_state`
  forward unchanged instead of re-deriving it from the overlaid facts.
- **`aider` stdout/stderr captured to a per-run log file** (#958): a
  died-without-committing failure previously surfaced with no diagnostic
  text at all.
- **`ship-it` PR title no longer wins from TDD scaffolding commits** (#1208):
  title derivation now prefers a substantive `feat`/`fix`/`refactor` commit
  over a `test`/`docs`-only one.
- **Real state-write guard + dev-queue backup rotation** (#1017): writes to
  the real (pre-monkeypatch) state/config dirs are refused under pytest, and
  `save_dev_queue()` now rotates a timestamped backup before every write.
- **`AutoDevResult` empty-item filtering extended** (#1130): the existing
  blank-item guard now also covers `commits`, `friction_highlights`,
  `next_actions`, `Health.shortcuts`, and `AgentHealthEntry.agent_id`.

## [1.19.0] — 2026-07-12

RFC 0010 native review-monitor — a native, event-driven port of the global
`/review-monitor` skill into cw's `reconcile()` loop as config-gated **review
recipes**. Four recipes (`address_review`, `auto_fix_ci`, `request_reviewer`,
`escalate_merge_block`) detect a PR's attention-state and act on it — dispatch a
fix, request a reviewer, escalate a merge block — each emitting auditable
`PR_ACTION_TAKEN`/`PR_ACTION_FAILED` events. Ships **dark** behind a master
`review_recipes_enabled=False` switch with a per-lane enablement map, so a fresh
install auto-does nothing. Includes two finalize-pipeline hang fixes discovered
while dogfooding the sprint's own dispatch.

### Added

- **RFC 0010 P1 — detect-only `address_review` recipe + `review_recipes.py`
  skeleton** (#1096, #1107): the detect→act module (modeled on `concierge.py` /
  `gate_recipes.py`) that reads a PR's `attention_state` from `pr_hydrate.py`'s
  `_compute_attention_state`. Detect-only first; inert behind the master switch.
- **RFC 0010 P2 — `PR_ACTION_TAKEN` / `PR_ACTION_FAILED` events + `address_review`
  act-phase** (#1097, #1121): the act phase re-dispatches a `changes_requested`
  PR, emitting the durable event *before* mutating and a `_FAILED` correction on
  error (the concierge fail-safe pattern).
- **RFC 0010 P3 — per-lane `review_recipes` config gate** (#1098, #1115):
  `LaneConfig.review_recipes` + `TicketTask.review_recipes` +
  `resolve_review_recipe_enabled()` 3-tier precedence (ticket > lane >
  global default-off), mirroring `resolve_gate_recipe_enabled`.
- **RFC 0010 P4 — remaining recipes** (#1099, #1123): `auto_fix_ci` (re-dispatch
  on CI failure), `request_reviewer` (driven by a repo-committed
  `review_strategy` key in `.claude/project-config.yaml` — modes
  `ci | repo_owner | reviewer_team`, default `ci`), and `escalate_merge_block`
  (one-shot-per-episode via a new `escalate_merge_block_fired_at` latch). Adds
  `resolve_review_strategy` (`review_strategy.py`), an `add_pr_reviewer` gh
  helper, a `cw doctor` WARNING on a misconfigured `review_strategy`, and retires
  the coarse `pr.ci_failed` / `pr.review_received` orchestrator rows. Dev-queue
  schema v13→14 (additive).
- **RFC 0010 P5 — ported review-monitor operational lessons** (#1100, #1125): the
  47 dated lessons from the global `review-monitor` skill audited against the
  native port — `# Why:` comments at the guards they justify, regression tests
  for the reproducible failure modes, and a durable `docs/review-recipes-lessons.md`
  index tagging every lesson applies / N-A with where it landed.

### Fixed

- **Finalize isolation gate made non-skippable** (#1122): the `/auto-dev-finalize`
  `#766` dispatch-detection was skippable prose after the spawn instruction; a
  headless finalize defaulted to `isolation:"worktree"`, collided with the live
  dispatch worktree, and hung ~40 min. Folded into mandatory numbered Steps
  4c.1/4c.2.
- **Finalize spawn moved out of the Step 4c intro** (#1124): #1122 numbered the
  gate but left an actionable spawn instruction *leading* Step 4c, so a headless
  finalize (even on Sonnet) still executed the spawn before reaching the gate and
  hung on the same collision. Step 4c's first actionable line is now the gate
  check; the spawn moves to 4c.2. Validated end-to-end on P5's own finalize.
- **`uv.lock` re-locked as part of the cut** (#1101): the v1.18.0 release left
  `uv.lock` lagging, tripping `main_checkout_drift`; this cut re-locks.

## [1.18.0] — 2026-07-09

RFC 0009 gate-recipe automation — the first event-driven auto-actor. Two
config-gated "gate recipes" auto-clear the two operator-approval gates that an
operator was clearing *mechanically* — a clean Stage-3 review and a clean
Stage-1 plan — whenever the pipeline's own structured evidence already says
"clean." Ships **opt-in** behind a master `gate_recipes_enabled=False` switch
with per-lane enablement, so a fresh install auto-approves nothing. Plus two
model-cost fixes (the impl and plan stages no longer default to Opus) and
assorted finalize/install hardening.

### Added

- **RFC 0009 gate recipes — `auto_approve_clean_review` + `auto_adopt_clean_plan`**
  (#1065, #1078): a detect→act reactor (modeled on `concierge.py`) that
  auto-clears a `review_pending_approval` gate when the review is clean
  (`must_fix_initial=0`, `deferred=0`, `recommendation=PROCEED`,
  `forbidden_touched=false`) and a `plan_pending_approval` gate when both plan
  signoff markers are present. Emits an auditable `gate.auto_approved` event
  (carrying a `predicate_snapshot`) *before* mutating, forwarded to the operator
  channel; fails safe via `gate.auto_approve_failed` + a self-clearing latch.
  Behind the master `gate_recipes_enabled=False` opt-in.
- **Per-lane gate-recipe enablement** (#1067): `LaneConfig.gate_recipes` +
  `TicketTask.gate_recipes` + `resolve_gate_recipe_enabled()` 3-tier precedence
  (ticket > lane > global default-off), mirroring `resolve_signoff`. Dev-queue
  schema v12→13 (additive, non-destructive).
- **Behavioral `reconcile()`-tick integration test for the gate recipes** (#1088):
  drives a full reconcile tick with the switch enabled end-to-end (clean review
  auto-approved, clean plan auto-adopted, `GATE_AUTO_APPROVED` emitted). Plus
  shared `_newest_by_created_at` (dev_queue) and `post_issue_comment` (gh)
  primitives extracted from duplicated call sites.
- **Event catalog entries for `gate.auto_approved` / `gate.auto_approve_failed`**
  (#1087).

### Fixed

- **Impl agent no longer hardcodes Opus** (#1079): the Stage-2 implementation
  agent's model is resolved from the ticket's scope tier (Small→Sonnet,
  Large→Opus) instead of unconditionally Opus — matching the documented model
  matrices and cutting Opus spend on every small-scope ticket.
- **Plan agent runs Sonnet, not Opus** (#1084): the Stage-1 plan agent is Sonnet
  so the plan review is calibrated to the weaker model's output (the
  plan-revision agent was already Sonnet).
- **Gate recipe could clear an `AWAITING_OPERATOR_SIGNOFF` gate it never
  validated** (#1083): the act phase now threads the resolved row identity
  through `_approve_ticket_locked` (plus a status assertion) instead of
  re-resolving by `(ticket_id, client)` string key, so a newer duplicate
  signoff row can no longer be cleared by a review recipe.
- **`--headless` now propagates into finalize's `/prep-pr`** (#1074, #1071).
- **Cross-platform LocalExecutor liveness** (#921): a single psutil code path
  replaces the Linux-only `/proc` read, with a schema migration clearing stale
  liveness handles.
- **`mypy .` scoped to the CI gate** (#1064).
- **Project-scoped `/ship-it` no longer installed globally** (#1090).

### Changed

- RFC 0009 design doc + errata: lock-free approve helper, marker-read reality
  (#1068).

## [1.17.0] — 2026-07-08

Finalize-reliability wave: eliminates the `needs_salvage` false-block class
that made successful ships look like failures, closes the primary-checkout
leak and the push-auth silent-death in finalize, and adds per-stage timeout
axes — plus the accumulated recovery-gap wave.

### Added

- **`headless_timeout_by_stage` resolution axis** (#1020): per-stage
  wall-clock budgets (plan 3600 / impl 4200 / review 7200 / finalize 5400s)
  so long review/finalize passes are not reaped on the flat global budget.
- **`cw dev-queue requeue --from-cancelled`** (#1018): escape hatch to
  recover a CANCELLED ticket (forward/same-stage only).

### Fixed

- **Finalize `needs_salvage` false-block / salvage deadlock** (#1054):
  `idle.py`'s stage-blind `SALVAGE_GIT` classification raced ahead of the
  finalize-aware `stalled.py` path (900s vs 5400s), parking FINALIZE-stage
  sessions `needs_salvage` and tripping the `park_marker_blocks_salvage`
  deadlock — so a session whose PR had already merged showed
  `blocked_on_user` despite shipping. Idle now defers FINALIZE-stage
  git-branch sessions and consults merged-PR ground truth; the same
  merge-aware check is applied to `rescue_finalize_blocked_sessions`, with
  cross-client `(client, ticket_id)` scoping throughout.
- **Finalize leaked branches into the primary checkout** (#1047): finalize
  Steps 4c / 4d.1 now apply the #766 dispatch-worktree detection (omit
  `isolation: "worktree"` when already in a cw worktree), preventing the
  `git checkout` leak that freshness-gated the whole client.
- **Stale `.cw/context.json` on requeue** (#1046): dispatch invalidates the
  per-worktree context before re-spawn so a requeue's newly-added operator
  resolutions reach the planner (LocalExecutor lane excluded).
- **Push-auth silent death in finalize** (#1049): a git-push auth failure
  (locked SSH key / expired credentials) now emits a `push_auth_failed`
  `blocked_on_user` blocker with a recovery hint instead of dying silently.
- **Rescued-finalize carried context** (#1050): the computed scope tier and
  `plan_source` are now written back onto the task after each stage, so a
  respawned finalize synthesizes a full PR body — and the large-tier idle
  budget (3600s) is finally fed instead of always falling back to 900s.
- **Concierge false-park recovery backoff** (#1030): dead-on-arrival
  detection plus backoff so an instant-death retry loop recovers
  mechanically without operator action.
- **Events inbox prune/rotate** (#856): `cw event prune` plus a doctor
  inbox-size check bound unbounded inbox growth.
- **`cw orchestrate status` recent-events flood + stale `last_stage`
  placeholder** (#854): the human renderer now aggregates consecutive
  `dispatch.tick` events in the "Recent events:" section (a new
  `--raw-events` flag restores the unaggregated stream), and the stale
  `last_stage` placeholder is replaced with a neutral, producer-agnostic
  message.
- **Stage-mismatch sentinel refusal** (#1031): the idle/local/signal_stop
  paths refuse a sentinel whose `stage_reached` disagrees with `task.stage`.
- **Adopt-assumption fast path** (#1032): self-answerable plan ambiguities
  are adopted inline instead of parking for the operator.

### Removed

- **Dead local task-queue (`cw queue`) and stale ROADMAP** (#1051).

## [1.16.0] — 2026-07-07

Post-RFC-0008 reliability wave: false-park elimination, a wrong-stage
sentinel guard, and headless hang mitigations — the operational gaps the
2026-07-03/05 sprint incidents exposed.

### Added

- **Stage-mismatch sentinel guard** (#1019): `_route_staged_decision` now
  validates the sentinel's `stage_reached` against `task.stage` via a new
  `_STAGE_REACHED_TO_STAGE` mapping table before routing; a late or
  replayed sentinel from a previous leg (the #986 incident class) is
  refused with a new `sentinel.stage_mismatch` event and a true no-op on
  the row. Missing `stage_reached` (legacy/`BlockedResult` payloads)
  bypasses the guard. A new `SentinelRouteOutcome` return contract is
  threaded through `_apply_sentinel_to_task` so `reconcile/phantom.py` no
  longer unconditionally completes the session on a refused route
  (orphaned-row hazard); the same gating for `idle.py`/`cli/sessions.py`/
  `local.py` is tracked in #1031.
- **Wall-clock liveness veto** (#976): the stalled sweep re-classifies
  transcript staleness fresh (via `_classify_liveness_bucket`, per-stage
  floors included) before proposing a wall-clock-budget park; a
  demonstrably-alive session is never parked or killed — a new
  side-effect-only `ProposedAction.PARK_VETOED` candidate emits
  `session.park_vetoed` instead. Applies to both AUTO and SIGNAL_ONLY
  lanes; the cap-exceeded park branch is unaffected.

### Fixed

- **Null-disposition BLOCKED_ON_USER parks** (#976): every operator-facing
  park now carries a disposition — `ReapCandidate.paused_status` is stamped
  in the idle/stalled `PARK_BLOCKED_ON_USER` branches, `_apply_queue_mutations`
  threads a disposition at all three call sites, and six bare park sites
  (gh-blocked, salvage, config-error fallbacks, terminal-sibling) are
  normalized. The concierge and escalation eligibility frozensets are
  extended to the new disposition strings so live auto-recovery and the
  45-minute operator-escalation latch keep their coverage.
- **Watchdog systemd unit fails 203/EXEC** (#1027): `cw watchdog install`
  now resolves the absolute path of the running `cw` executable
  (argv[0]-derived with a `shutil.which` fallback; hard error if neither
  resolves) into `ExecStart`/`ProgramArguments` instead of writing bare
  `cw`, which systemd user managers cannot resolve.
- **Body-folded pre-flight resolutions invisible to the plan scanner**
  (#980): `auto-dev-plan` now live-fetches the ticket body on every
  invocation (the #952 rule extended beyond comments) and treats a
  marker-bearing body resolutions section as an authoritative operator
  response channel; body markers are excluded from the multi-marker gate
  tally, and the Step 1c scan-exit comment gets a pinned
  `## Pending Verification Scan` header.
- **Dispatched workers can hang on interactive gh/git prompts** (#979):
  `native_daemon.py:_spawn_clean_env` now unconditionally sets
  `GH_PROMPT_DISABLED=1`, `GH_PAGER=cat`, `GH_NO_UPDATE_NOTIFIER=1`, and
  `GIT_TERMINAL_PROMPT=0` on every `claude --bg` spawn, and worker
  prose (`auto-dev.md`) now requires `timeout 120` on gh/git/curl calls
  and prohibits WebFetch of external docs in headless runs.

## [1.15.0] — 2026-07-07

RFC 0008 (orchestrator push channel) lands in full: queue-event producers,
the operator attention channel, subscribe-first monitoring docs, and the
gate-concierge capstone.

### Added

- **Gate concierge + durable escalation + `cw watchdog`** (#1015, RFC 0008
  capstone): `cw.reconcile.concierge` — a mechanical recovery reactor with
  three recipes (wall-clock false-park requeue, park-marker-poison clear,
  cancelled-row restore), gated behind a new global opt-in
  `OrchestratorConfig.concierge_enabled` (**default `false`**, per
  ADR-0006) with per-recipe `concierge_recoveries` flags
  (merge-with-defaults semantics — omitting a key does not disable it).
  `cw.reconcile.escalation` — a durable escalation latch
  (`escalation_parked_at`/`escalation_fired_at`, flat 45-minute threshold)
  over six judgment gates, emitting a latched `operator.escalation` event
  on the operator channel; `concierge.recovered` is audit-trail only. New
  `cw watchdog` CLI group (`tick`/`install`/`uninstall`/`status`) installs
  a session-independent systemd user timer (Linux) / launchd plist (macOS)
  dead-man's switch. Dev-queue schema v9 → v10.
- **Operator attention channel** (#1002, RFC 0008 W3): operator-relevant
  event filtering over the queue-events SSE channel, configured via
  `OrchestratorConfig.operator_channel_forward` and
  `_DEFAULT_OPERATOR_EVENT_TYPES`.
- **`task.transition` / `task.stage_changed` / `task.deleted` producers**
  (#1000, RFC 0008 W1): queue-row lifecycle events with disposition
  payloads, emitted from `transition_task_status` and friends.
- **`session.liveness_changed` producer** (#1001, RFC 0008 W2): a new
  `cw.reconcile.liveness` sweep classifies each live DAEMON session's
  transcript-mtime staleness into a latched `Session.liveness_bucket`
  (`live` / `stale_15m` / `stale_30m` / `stale_45m`), edge-triggering
  `session.liveness_changed` only on a bucket crossing. Per-stage floor
  overrides (`OrchestratorConfig.liveness_first_bucket_by_stage`) can raise
  the entry-point threshold for a pipeline stage without renaming or
  reassigning the global bucket labels — see `docs/events.md` for the
  floor-suppression semantics. Pure observation: no disposition, no queue
  mutation. `CW_STATE_SCHEMA_VERSION` bumped 12 → 13.

### Fixed

- **Detached-HEAD main checkout misclassified** (#964): dispatch's freshness
  gate now surfaces `freshness_detail=main_detached_head` (with accurate
  checkout advice in the WARN and `dev-queue status` subline) instead of
  falling through to the generic `main_behind_origin` label.
- **Release-tag workflow skipped on squash merges** (#1009): the
  `chore(release):` commit-subject guard now tolerates the PR-number suffix
  GitHub appends on squash merge; v1.14.0 had to be tagged by hand because
  of this.

### Documentation

- **Subscribe-first monitoring** (#1003, RFC 0008 W4): the dispatch runbook's
  monitoring section now leads with operator-channel subscription
  (`task.transition` disposition payloads); the poll ladder is demoted to an
  explicitly labeled fallback. `cw-session-watch` Mode B and `cw-fanout`
  gate-closure consume the pushed events.

## [1.14.0] — 2026-07-06

**Observability sprint Phase 4 (RFC 0007 W2 push + deprecation) — sprint
close.** Zero-latency PR signal via GitHub-webhook push, latch-style
escalation for the silent repeated-skip classes, release tagging automated,
`cw orchestrate watch` formally deprecated. RFC 0008 (orchestrator push
channel) accepted as the follow-on design.

### Added

- **GitHub webhook push producer** (#930, #1005): `.github/workflows/pr-events.yml`
  POSTs `merged` / `ci_failed` / `review_received` wire events to
  `POST /pr-event` through an operator-run relay tunnel; the server verifies
  an HMAC signature (`CW_PR_EVENTS_HMAC_SECRET` env, `X-Cw-Signature:
  sha256=…`, fail-open with a startup warning when unset) and routes pushed
  observations through the same persist/diff/emit path as the poll layer
  (shared `apply_pr_state_observation` extracted from `pr_hydrate`) — push and
  poll dedupe against the same persisted `pr_state`, with a TOCTOU re-read
  fix from review. `COMMENTED` reviews emit without mutating
  `review_decision`; `mergeable` stays poll-only; unmatched PRs no-op.
  Runbook §10 documents the tunnel as operator infrastructure and the
  poll-covers-push-down degradation contract.
- **Consecutive-skip escalation counters** (#996, #1006, closes #974):
  per-client `consecutive_freshness_blocks` (override store) and per-session
  `consecutive_salvage_skips` (state schema v12) — latch semantics (one
  `session.needs_attention` per streak at threshold 5, reset on recovery),
  new `paused_status` values `freshness_gate_blocked` /
  `salvage_skip_escalated`, and a client-header attention badge on `cw board`
  so client-scoped signals are actually visible (the #940 silent freeze now
  pages the board within 5 ticks).
- **Tag-on-release-merge workflow** (#997, #1004): `chore(release): vX.Y.Z`
  commits landing on main are tagged and get a GitHub Release with the
  matching CHANGELOG section automatically — idempotent, version
  cross-checked against pyproject.toml, never force-moves a tag. This release
  is the first to use it.

### Deprecated

- **`cw orchestrate watch`** (#995, #998): prints a deprecation notice to
  stderr on every invocation before launching the board. Board-backed since
  #986 (v1.12.0); removal targeted for the release after v1.14.0. Use
  `cw board`. `cw watch` (the flat table) is unaffected.

### Documentation

- **RFC 0008 — orchestrator push channel** (#999): transition/liveness
  producers + server-side operator attention filter; implementation tickets
  #1000-#1003.

## [1.13.0] — 2026-07-05

**Observability sprint Phase 3 (RFC 0007 W3 — operator signoff gates)**:
`--signoff operator` reliably parks a ticket before ship, code-enforced in
dispatch regardless of worker scope classification (the #926 lesson).

### Added

- **Operator signoff gates** (#990, #991): `TicketTask.signoff` /
  `LaneConfig.signoff` (`Literal["operator"] | None`) and
  `OrchestratorConfig.default_signoff`, resolved ticket > lane > global at
  gate-check time by the new 3-tier `resolve_signoff` (extends the 2-tier
  `resolve_reap_policy` shape). New `QueueItemStatus.AWAITING_OPERATOR_SIGNOFF`
  routed from both REVIEW-exit branches of `_route_staged_decision`
  (`stage_complete` small tier and `review_pending_approval` small-downgrade),
  parked with disposition `signoff_gate`. `cw dev-queue approve` gains the
  clearance arm (advance to FINALIZE, no `gh` call — the ready-flip arrives
  with RFC 0005 C3/C2); large+signoff tickets take two approvals by design
  (scope gate, then ship gate — the first approve says so on stdout).
  `cw dev-queue add --signoff operator`; `signoff` in the tasks `--json`
  contract; dev-queue schema v9 with migration fill. Invalid signoff config
  raises loudly (no fail-safe coercion — coercing would silently disable the
  gate).
- **Occupancy consolidation**: new `OCCUPIED_LANE_STATUSES` constant replaces
  four inline `(RUNNING, BLOCKED_ON_USER)` literals (dispatch lane stats,
  board lane panel, reconcile rescue lookup, `lane rm` guard) — signoff-parked
  tickets hold their lane slot and count as occupying, never as "running";
  the lane breakdown line gains a `signoff=N` field. `cw dev-queue wait`
  treats the signoff park as terminal with a new distinct exit code;
  `requeue` accepts signoff-parked tickets (reject-via-regress lever);
  `move` refuses them; default `status` view shows them.

## [1.12.0] — 2026-07-05

**Observability sprint Phase 2 (RFC 0007 W1 — board consolidation)**: `cw board`
becomes the primary interactive read surface; tui.py shrinks to the `cw watch`
stack per the operator-decided usefulness bar.

### Added

- **Board PR/CI column, session age, attention badges, bounded event feed**
  (#985, #987): per-ticket PR/CI cell rendered from persisted
  `TicketTask.pr_state` (no subprocess in the render path); session-age column
  (session `started_at` → `created_at` fallback); attention/park badges from
  the event bus with first-match precedence `reap_proposed` >
  `needs_attention` > `pr_state.attention_state`, joined on `ticket_id` with
  the #857 bounded-window pattern; new global event-feed panel that aggregates
  consecutive `dispatch.tick` runs (`dispatch.tick ×N over Xm`) *before*
  tailing to the display limit so tick bursts cannot evict real signal
  (absorbs the #854 tick-flood slice); `cw board --raw-events` restores the
  raw stream; `--client` scopes the feed.
- **`cw board --detail`** (#986, #988): session-grouped toggle panel with
  worktree-contention column (`⚠×N` when ≥2 sessions share a path), built on
  the new non-display `cw.session_groups` module (extracted from tui.py per
  RFC 0007 resolved Q4); orchestrate summarizers promoted to public.

### Changed

- **`cw orchestrate watch` repoints to the board** (#986): the client-grouped
  dashboard renderer and its helpers are deleted from tui.py (837 → ~420
  lines; git history is the archive); `cw watch` keeps the flat session table;
  `cw orchestrate status --json` contract unchanged; `--compact`/`--verbose`
  options removed with the DetailLevel machinery. Formal deprecation notice for
  `cw orchestrate watch` lands in Phase 4.
- **`docs/dispatch-runbook.md` routine status reads point at `cw board`**
  (#986); `cw dev-queue status` remains documented for scripting/parseable
  reads (RFC 0007 resolved Q3).

### Documentation

- 2026-07 sprint operational lessons encoded into `cw-fanout` Step 4 and the
  dispatch runbook §7 — liveness-first monitoring, gate workarounds, recovery
  recipes (#984).

## [1.11.0] — 2026-07-05

**Observability sprint Phase 1 (RFC 0007 W2 poll layer + W4 push completion)**:
the orchestrator stops re-deriving PR state every prompt, and workers can push
validated completion results instead of relying solely on transcript parsing.

### Added

- **PR-state hydration in the serve tick — first `pr.*` event producer**
  (#929, #981): throttled (default 150s) `gh pr view` hydration for dev-queue
  tasks with open PRs; persisted `TicketTask.pr_state` (dev-queue schema v8);
  attention-state decision table (ported `_summarize_status_checks`, rows
  5a-5c distinguishing BLOCKED-waiting-on-CI from genuinely approvable);
  `pr.merged` / `pr.ci_failed` / `pr.review_received` / `pr.mergeable` emitted
  on transitions diffed against persisted state (`pr.merged` also fires on
  first observation so late-enqueued tasks still retire). `retire_merged_prs`
  begins actually firing. `cw dev-queue tasks` gains `pr_state` (JSON) and an
  ATTENTION column (human); `status` gains per-client NEEDS_ATTN counts.
- **`cw result emit` — push-based completion** (#536 Phase 1, #977): workers
  push the full `AutoDevResult` through a strictly-validated CLI (reusing
  `cw result validate`, inheriting the A6 empty-ambiguity invariant); on
  success `session.last_result` is written synchronously under the sessions
  lock. The Stop hook remains the completion-event source but now defers to
  an emitted terminal result instead of overwriting it; the phantom-reconcile
  path gains the same gate so an emit-then-crash session is never re-salvaged
  over its authoritative result. Transcript sentinels are demoted to forensic
  fallback. `_seed_daemon_session` promoted to conftest for shared test use.

## [1.10.0] — 2026-07-04

**Observability sprint Wave 0**: worker-context correctness (the RFC 0007 W4
data-integrity floor), the per-lane circuit breaker, and defense-in-depth
against worker escapes onto the operator's main checkout.

### Added

- **Per-lane circuit breaker on consecutive spawn errors** (#875, #969): a
  lane that accumulates consecutive `spawn_error`s pauses instead of grinding
  a ticket to the attempt cap. Manual `cw lane resume` resets the
  consecutive-error counter (operator decision: resuming asserts the
  underlying problem is fixed). No auto-resume.
- **Main-checkout escape defenses** (#940, #972): new `cw guard-cwd`
  subcommand wired as a `PreToolUse` hook that blocks worker Bash calls whose
  cwd resolves to the client `workspace_path`; `resume_session` respawn guard
  (DAEMON-origin with no worktree hard-fails; USER worktree-purpose sessions
  get `check_not_main_checkout`); new `main_drift` reconcile sweep emitting
  `session.needs_attention` per tick while a live session's client checkout
  is dirty/ahead with the session homed elsewhere; freshness-gate divergence
  message now gives inspect-first advice instead of `pull --rebase`.
- **`cw event record session.needs_attention`** (#952, #965): the attention
  event type workers are instructed to emit on comments-fetch failure is now
  accepted by the record CLI.

### Fixed

- **Workers no longer plan against stale ticket context** (#952, #965): the
  plan stage live-fetches issue comments on every invocation (including
  re-dispatch rounds) and pins the resolutions-marker grep to that live fetch;
  intake's single-ticket fetch now requests comments explicitly. Root cause of
  the #949 three-round resolution-blindness incident; RFC 0007 W4's original
  suspect corrected in the RFC.
- **Empty ambiguity items can no longer park a ticket with nothing to answer**
  (#953, #966): strict model-layer rejection of question-less ambiguity items
  (the future `cw result emit` in-turn error), with the parse boundary
  filtering empty items and coercing all-empty arrays to a labeled synthetic
  placeholder that parks visibly. Contract invariant A6; no schema bump.
- **Event-follow staleness guard deduplicated and hardened** (#954, #971):
  `tail_events_follow` and `wait_for_event` share one offset-tracking guard;
  the unreachable size-decrease case logs and continues instead of silently
  misbehaving. Delivery contract unchanged.

## [1.9.1] — 2026-07-03

### Fixed

- **Deterministic `cw event tail --follow` test** (#948, #959): the flaky
  follow-mode test's fixed 2-iteration sleep budget is replaced with a
  poll-until-observed loop (spy on the streaming seam), tolerating the
  `tail_events_follow` st_size guard lag tracked as #954. Test-only change.

## [1.9.0] — 2026-07-03

The **reliability bug sprint, waves 2–3 (P0 correctness)**: every P0 from the
109-issue triage is now shipped. The pipeline can no longer emit a
false-positive review PROCEED for plan-contradicting work, silently bypass the
operator's scope gate, park still-working sessions without rescue, or lose an
approved plan between stages.

### Added

- **Late-sentinel rescue for parked sessions** (#918, #946): a terminal
  `AutoDevResult` recorded by the Stop hook on an already-parked task now
  rescues it through a shared routing helper that mirrors live
  `apply_staged_decision` semantics (including FINALIZE self-heal regress and
  terminal-stage `pr_url` preservation). Large-tier idle budget default
  raised to 3600s. `BlockedResult` arms stay RUNNING-gated — an operator park
  is never reversed by an unparseable late signal.
- **Non-deferrable review findings + `requeue --regress`** (#917, #955): a
  finding that the implementation contradicts the approved plan (or an
  operator mandate) cannot be adjudicated away — review exits `blocked` with
  `blocker.reason: "plan_deviation"`. Spec-citation claims ("required by
  spec") are cross-checked against `.cw/plan.md` verbatim. New optional
  `review.deferred` sentinel field surfaces deferral counts. New
  `cw dev-queue requeue --regress` permits a backward stage target on a
  blocked task — the operator's bounce-to-impl path.
- **`cw init --no-onboarding` runnability warning** (#922, #945): init now
  warns the client is not runnable until `--onboard-only`, and the Next-steps
  block points at onboarding first.

### Changed

- **Operator scope gate is escalate-only** (#926, #950): the gate fires when
  EITHER the operator's `--scope large` hint OR the worker's sentinel tier
  says large — a worker reclassifying to small can no longer bypass the
  operator's primary safety gate. Budget resolution unchanged.
- **Pre-flight Resolutions are binding plan constraints** (#828, #951): the
  plan stage extracts the single authoritative resolution set (body or
  comment marker), refuses multi-marker accretion, and emits a per-item
  `## Pre-flight Resolution Conformance` section the Plan Reviewer enforces.

### Fixed

- **Plan stage persists `.cw/plan.md`** (#943, #944): Stage 1 writes the
  reviewed plan to the worktree before posting/sentinel; Stage 2 falls back
  to the newest reviewed tracker comment before exiting `plan_missing`. The
  latent gap became fatal with instruction-literal worker models.

## [1.8.0] — 2026-07-02

The **reliability bug sprint, waves 0–1**: stabilized the CI gate, hardened
the review stage's sentinel contract, and closed a cluster of dispatch /
reconcile / doctor correctness gaps found by a full 109-issue triage. Also
lands the RFC 0005 F3 async LocalExecutor spawn.

### Added

- **Async `LocalExecutor.spawn()` + local-harvest crash recovery** (#888,
  #920): aider launches fire-and-forget via `Popen`; reconcile's local
  harvest completes the session when the process exits, including after a
  cw crash.
- **`cw spawn close --confirmed-dead`** (#928, #938): flag-distinguished
  close for provably-dead cross-session workers, so an auto-mode allow rule
  can authorize cleanup without weakening bare `spawn close`.
- **argv regression guard for `claude --bg` spawns** (#736, #933): asserts
  the prompt stays the trailing positional at both spawn chokepoints
  (`spawn_create_impl`, `resume_session`) — the #716/#731/#733 silent-idle
  regression class now fails tests instead of burning 30-minute sessions.
- **`wedge/active-no-daemon-entry` doctor class** (#925, #939): `doctor
  --reap` detects and clears ACTIVE/IDLE sessions whose daemon entry is gone
  (crash without sentinel), releasing the hook-context lock.

### Changed

- **Review-stage sentinel hardening** (#916, #932): `scope.tier` carries
  forward from the plan sentinel (no more small→large flip-flop loops) and a
  pre-exit dirty-tree invariant stops the review session exiting with
  uncommitted fix-loop work.
- **`_local_preflight` returns a discriminated union** (#919, #934):
  `_PreflightOK` NamedTuple replaces the tuple-with-dead-`or ""`-guards
  shape; callers narrow via `isinstance`.

### Fixed

- **Flaky `test_cli_event_tail_client_filter_comma_separated`** (#913,
  #931): hex-compatible session ids in negative assertions made the test
  order-dependent; the CI gate is deterministic again.
- **`session.completed` without a PR now emits `session.needs_attention`**
  (#923, #935): plan-stage parks are no longer invisible to operators.
- **Stale PENDING dedup** (#876, #937): reconcile parks (auto: cancels) a
  PENDING task whose ticket already has a globally-terminal sibling row —
  scoped strictly to `{COMPLETED, CANCELLED}` so multi-stage tickets and
  legitimate retries are never false-reaped.
- **Finalize double-fire** (#912, #941): doctor's BLOCKED_ON_USER collapse
  skips tasks that already carry a `pr_url`, killing the re-dispatch path
  that produced redundant empty PRs.

## [1.7.0] — 2026-06-30

The **local-model validation** sprint (RFC 0005 executors): the exit bar was
met — a debt-tier ticket (#889) walked `plan(claude) → impl(local/qwen) →
review(claude) → merged PR` on a lane pinned to the local backend. Alongside
the validation, this release hardens dispatch reliability (merge_pending,
review-gate harvest), adds a Codex review-only runner seam, tightens the aider
subprocess environment, and makes a silently dispatch-blocked client visible in
`cw status`.

### Added

- **Codex review-only runner seam** (#627, #904): `CodexRunResult` +
  `CodexRunner` + `FakeCodexRunner` — the foundation for a review-stage
  executor backed by the hosted Codex CLI, parallel to `LocalExecutor`.
- **`merge_pending` status** (#899, #901, #903): a PR-created-but-awaiting-CI
  run is coerced at the parse boundary (`status=blocked` + non-null `pr` →
  `merge_pending`) and routed to `BLOCKED_ON_USER` with its PR URL preserved,
  instead of being recorded `failed`.
- **Freshness-gate surfacing in `cw status`** (#908, #910): the headline
  `cw status` now shows a "Freshness gates (action required):" section listing
  each dispatch-blocked client with the reconcile hint, so a silently gated
  client is visible without parsing `cw dev-queue status`.

### Changed

- **Constructor-inject `StageExecutorConfig` into `ClaudeNativeExecutor`**
  (#887, #905): closes the E1 double-resolution — the executor receives its
  resolved config instead of re-resolving it.

### Fixed

- **Review-gate sentinel harvest** (#892, #907): `_parse_any_sentinel_from_transcript`
  now falls back to the surface_ref transcript when the csid transcript yields
  no sentinel, so a `review_pending_approval` emitted by a still-alive worker is
  harvested instead of being recorded `needs_salvage` (which had blocked
  `cw dev-queue approve`).
- **Pipeline model defaults + recommended-defaults docs** (#900).

### Security

- **aider subprocess environment allowlist** (#891, #906): `build_env()` now
  forwards only an allowlisted set of environment variables to the aider
  subprocess instead of the entire parent environment (which included secrets);
  adds an `aider_available()` seam.

### Documentation

- **Dispatch runbook — freshness-gate diagnosis** (#908, #909): §9.1 gains a
  symptom-first rule ("dispatcher up but nothing dispatching → check
  `skip_reason` before suspecting the monitor"), and §9.3b documents
  diverged-main caused by release/merge artifacts rather than a worker leak.

## [1.6.0] — 2026-06-29

The **executor infrastructure** sprint (RFC 0005): `cw` gains a pluggable
`Executor` abstraction so stages can run via aider + a local OpenAI-compatible
endpoint instead of Claude Code, with per-lane override support. Alongside it:
spawn-error exponential backoff, an in-process `cw dev-queue serve` supervisor,
prior-attempt context on retry, and a version-drift self-check exception.

### Added

- **LocalExecutor backend** (RFC 0005 F3, #866): aider-based executor that
  targets a local OpenAI-compatible endpoint, fully integrated with the
  per-stage resolution path.
- **Per-stage executor resolution + lane override** (E1, #874): each dispatch
  stage resolves its executor independently; lanes can pin a non-default
  executor via config.
- **`cw dev-queue serve`** (#871): in-process supervisor that runs the dispatch
  loop with configurable backoff restart on failure — replaces the bare
  `dispatch run` call for long-lived operator sessions.
- **`prior_attempts_summary` on retry** (#872): workers spawned for a retry
  receive a structured summary of all previous attempts, giving the model
  context on what was tried before.
- **`VersionDriftExit` exception** (#880): dispatch loop self-check raises a
  typed exception when it detects it is running a stale version, allowing the
  supervisor to restart cleanly.
- **spawn_error exponential backoff** (#879): failed spawn attempts back off
  with jitter before retry, preventing tight error loops on model/API outages.

### Changed

- **`merge_gate` blocker reason is now a static constant** (#881): removes the
  last inline string literal from the finalize path; finding is now
  grep-able across the codebase.
- **Subagent models pinned explicitly in `/auto-dev` pipeline** (#877, #220):
  each stage's spawned subagent declares its model tier rather than inheriting
  the operator's default, cutting unnecessary Opus fan-out.

## [1.5.0] — 2026-06-26

A **dispatch-hardening** release: a global attempt ceiling kills dead-requeue
churn, the finalize path gains a regress route for gate failures, and the
attention digest is scoped to live sessions only. Plus file-overlap merge gate
and several targeted fixes from the v1.4.0 stability wave.

### Added

- **Global attempt ceiling** (#850, #786): dispatch stops requeueing a ticket
  once it has hit the configured max-attempts threshold — prevents dead tasks
  from churning indefinitely.
- **finalize → IMPL regress path** (#858, #770): when a gate check fails during
  finalize, the task reverts to IMPL rather than wedging in an unrecoverable
  state.

### Changed

- **Attention digest scoped to live sessions** (#857): `orchestrate watch`
  attention column is now bounded to the recent window and live sessions only,
  eliminating noise from stale/completed sessions.
- **heartbeat + sentinel promoted to `SessionSummary`** (#845): moved from
  inline columns to the shared summary model so both `cw status` and
  `orchestrate watch` render them consistently.

### Fixed

- **File-overlap merge gate** (#849, #777): finalize now blocks on overlapping
  file edits between the worktree and main, with a null-safe blocker field for
  the `merge_gate_blocked` case.
- **cw-fanout blocked_on_user gate loop** (#860): the wave-monitoring loop now
  closes correctly when a task enters `blocked_on_user` — previously it could
  spin indefinitely.
- **gh_blocked disposition** (#846): the gh-blocked idle-park transition now
  passes its disposition through `transition_task_status` like all other
  terminal branches.

## [1.4.0] — 2026-06-23

The **live work dashboard** sprint: `cw orchestrate watch` gains the signals it
was missing, on top of a single audited status-transition seam (ADR-0010,
ADR-0011). Plus a dev-queue queue-view cleanup, the reaper's branch-absence
diagnostic (ADR-0009), and a fix to requeue context staleness.

### Added

- **Storm-deduped attention indicator** on `orchestrate watch` (#537): repeated
  `session.needs_attention` collapse into one actionable row per
  `(session, condition)` with an affected-session count — readable during a
  reconcile storm.
- **Heartbeat + sentinel columns** on the sessions table (#833):
  transcript-freshness age and the session's paused/sentinel status.
- **CI / mergeable column** on the monitored-PRs table (#834).
- **Terminal disposition column** on `cw dev-queue tasks` (#310): `shipped` /
  `no_op` / blocker reason per terminal row, via new `TicketTask.disposition` /
  `pr_url` / `completed_at` fields (dev-queue schema v4 → v5).
- **`transition_task_status` seam** (#835): the single authority for every
  `TicketTask` status change (ADR-0011).
- **Branch-absence diagnostic** on `SESSION_TIMED_OUT` (#808): a nullable
  `branch_state` annotation; never inferred as completion (ADR-0009).

### Changed

- **`cw dev-queue status` defaults to active-only** (#308): the TICKETS column
  shows PENDING/RUNNING/BLOCKED_ON_USER by default; `--all` restores the full
  list. Counts are unchanged.

### Fixed

- **Requeue context staleness** (#837): `requeue` reused the worktree's
  `.cw/context.json` materialized at first dispatch, so operator resolutions
  added between requeues never reached the worker. Intake now stamps
  `materialized_by_session` and re-fetches the ticket on a new session.
- **gh-blocked park disposition** (#842): the gh-blocked idle-park branch now
  stamps its disposition like its siblings, plus added stamping test coverage.

## [1.3.2] — 2026-06-20

A **hotfix** release closing the root cause of the dispatch worktree-leak
class of bugs (#766) that the v1.3.1 round only mitigated downstream.

### Fixed

- **GIT_* env leak into spawned workers** (#766, #790): `RealNativeDaemonClient`
  spawned `claude --bg` workers with no `env=`, so workers inherited the
  orchestrator's `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE`. Git honors
  those over `cwd`, redirecting every worker git operation to the
  orchestrator's `.git` / index — producing both a FILE leak (worker edits
  appearing uncommitted in the main checkout) and a COMMIT leak (worker
  commits landing on local `main`, diverging it from `origin` and wedging the
  dispatch loop's `--ff-only` auto-fast-forward). Workers now spawn with a
  GIT_*-stripped environment via a new `_spawn_clean_env()` helper.

### Documentation

- **#766 leak-recovery + #774 manual-finalize runbook procedures** (#788):
  added operator procedures to `docs/dispatch-runbook.md` for recovering from
  a leak and manually finalizing a stuck session.

## [1.3.1] — 2026-06-20

A **dispatch-reliability + observability/inspection CLI** release. This patch
round closes the remaining rough edges from the v1.3.0 reliability sprint and
ships the first wave of operator-facing inspection commands (`cw event
tail/wait`, `cw session show/list/wait/result`, `cw queue peek`) that make
it possible to observe a running dispatch loop without reaching into state
files by hand.

### Added

- **`cw event tail`** (#769): stream events from the history log; supports
  `--follow` for live streaming, `--since`, `--json`, and `init_cursor_at_end`
  for efficient tailing without replaying history.
- **`cw event wait`** (#275, #773): block until a matching event arrives,
  enabling scripted polling for CI integration and operator runbooks.
- **`cw session show / list / wait / result`** (#779): four inspection
  subcommands for a running or completed session — show full session detail,
  list all sessions with filter/sort, wait until a session reaches a terminal
  state, and print the final result sentinel.
- **`cw queue peek`** (#778): promote the ad-hoc `cw_queue_peek.py` script to
  a proper `cw queue peek` subcommand for inspecting the dispatch queue.
- **`LANE_CAP_BLOCKED` skip reason** (#588, #775): dispatch now records a
  dedicated `LANE_CAP_BLOCKED` skip reason when a lane is full but not
  otherwise blocked, giving operators a precise signal vs. the generic skip.
- **`SESSION_STAGE_TIMED_OUT_RETRIED` event** (#724, #785): reconcile emits
  this event before applying route policy on a genuine stage timeout, providing
  an auditable record of every retry decision.
- **`HeadNotOnDefaultBranchError`** (#761): new `WorktreeError` subclass for
  the "HEAD not on default branch" condition, enabling callers to handle it
  specifically without string-matching the exception message.
- **`is_documented_example` sentinel field** (#771): `auto_dev_result` parser
  rejects placeholder sentinels that set `is_documented_example: true`,
  preventing no-op completions from being accepted as real results.

### Fixed

- **Dirty-worktree push notification storm** (#763, #767): edge-trigger the
  dirty-worktree notification instead of level-triggering it — the notification
  is now emitted once on transition to dirty, not on every reconcile tick while
  the worktree stays dirty.
- **`dev_queue_lock` deadlock on `record_event`** (#765): `record_event` was
  called inside `dev_queue_lock` in `_collapse_blocked_on_user_tasks`, creating
  a potential deadlock when the event writer also acquires the lock; the call is
  now outside the lock.
- **Lane validation in `add_ticket`** (#760): `add_ticket` now validates the
  lane name and raises a clear error on unknown lanes, preventing silent ticket
  starvation when a caller supplies a misspelled lane.
- **World-state timeout check** (#315, #776): the session-timed-out check now
  consults the world state (last-seen timestamp) before declaring a timeout,
  reducing false positives for sessions that are alive but quiet.

### Chore / Docs

- Gitignore local artifacts: `.claude/worktrees/`, `scheduled_tasks.lock`,
  `prep-pr-state.json`, `docs/handoffs/` (#759).
- README and RFC/ADR status fields updated to reflect current reality (#755).

## [1.3.0] — 2026-06-18

A **pipeline-reliability hardening** release. The v1.2.0 staged engine was
dogfooded hard; this release fixes the silent-failure classes that surfaced —
each of which could waste a worker, lose completed work, or mislabel a ticket —
and adds `cw worktree gc` for squash-merge-aware worktree cleanup. Several of
these fixes were found and shipped by the staged pipeline dogfooding itself.

### Added

- **`cw worktree gc`** (#630): prune cw-managed worktrees whose branch PR is
  MERGED, determined via **PR state** (`gh pr list --state all`) rather than
  `git branch --merged` — which misses every squash-merged branch and let
  worktrees accumulate unbounded. Dry-run by default; `--apply` to act;
  `--include-closed` opt-in. Skips locked/bare worktrees and never force-removes
  a worktree with unsaved/unpushed work.

### Fixed

- **Stage-advance sentinels from exited workers were dropped** (#716): a staged
  worker emits `stage_complete` and exits, so the phantom path handled it with
  terminal-only salvage and never advanced the stage — every dispatch paid a
  ~21–26 min/stage wall-clock-timeout tax. The phantom path now routes the
  emitted advance sentinel through `apply_staged_decision`.
- **Tool-emitted sentinels were invisible to the transcript scan** (#731): a
  worker that emits the `AUTO_DEV_RESULT` block via a Bash `cat` lands it in a
  `tool_result` block, which neither `signal_stop` nor reconcile scanned (both
  read assistant-text only) → no routing → stall. Both paths now scan
  `tool_result` content.
- **`--disallowed-tools` swallowed the worker prompt** (#733): the variadic flag
  was passed as two tokens immediately before the positional prompt, consuming
  it — workers launched promptless and did nothing. Now passed as a single
  `--disallowed-tools=<pattern>` token.
- **A near-miss `stage_reached` failed the whole sentinel** (#748): an
  off-contract stage label (e.g. `stage4_pr_creation`) hard-failed validation
  and discarded completed work; it now coerces to the canonical stage by
  stage-number prefix (informational field), while genuine garbage still rejects.
- **An unknown-status sentinel was marked COMPLETED** (#750): an unparseable /
  unknown-status `BlockedResult` fell through to `COMPLETED`, silently retiring
  unshipped work as "shipped". It now routes to `FAILED` — terminal, but never
  false success.
- **Spawn flake went undetected for ~30 min** (#520): a `claude --bg` spawn that
  returned a short id but never registered in the daemon roster was marked
  RUNNING and only caught by the idle watchdog. Spawn now verifies roster
  registration and fails fast.
- **github-issues workers could stall on Linear OAuth** (#726): the Linear MCP
  is withheld (`--disallowed-tools`) from headless workers when the tracker is
  github-issues, so a Linear-flavored ticket can't trigger an unanswerable
  headless OAuth prompt.
- **`prep-pr` titled the PR from the trailing chore commit** (#722): on
  squash-merge this became a misleading permanent `main` subject. Title now uses
  tiered selection (first substantive commit / ticket title), skipping trailing
  lockfile/chore commits.
- **`dev-queue wait` rode to exit 124 on a mid-wait reap** (#542): a session
  reaped mid-wait (task → PENDING, `session_id` cleared) was indistinguishable
  from the spawn window and never surfaced; it now returns ATTENTION.
- **Branch-key merged check hardcoded `dev/`** (#728): the merged-PR check used a
  literal `dev/` prefix, breaking for any client with a custom
  `feature_branch_prefix`; it now resolves the prefix per-session from the SSOT.
- **Supervisor session continuity was never verified** (#519): reconcile now
  compares `Session.claude_session_id` against the supervisor's
  `resumeSessionId` and clears stale continuity on mismatch.

## [1.2.0] — 2026-06-16

The RFC 0005 **staged pipeline engine** goes live (milestone #8, Phase 1). The
forward-compat seams from v1.1.3 are now wired: `dispatch_tick` spawns one
session per stage (`/auto-dev-{plan,impl,review,finalize}`) and a stage-advance
machine drives the ticket PLAN→IMPL→REVIEW→FINALIZE→COMPLETED. Validated
end-to-end by dogfooding: the engine autonomously planned, implemented,
reviewed, shipped, and auto-merged a real ticket (#475 via PR #706).

### Added

- **Staged dispatch engine — RFC 0005 B2** (#617): `dispatch_tick` spawns
  per-stage via `ClaudeNativeExecutor` (no monolith prompt); `_stage_advance` +
  the scope-gated advance decision in `_apply_events_to_store`; `stage_base_ref`
  stamped at spawn and cleared on advance.
- **`stage_complete` sentinel status** (#699): a PR-less intermediate
  stage-success status so IMPL can advance to REVIEW. (PLAN/REVIEW advance via
  the scope-gated `*_pending_approval` statuses; IMPL previously had no valid
  success status and mis-emitted `shipped`, which the schema rejects without a
  PR.)
- **`cw dev-queue run --client/-c`** (#663): scope a dispatch tick to a single
  client's queue instead of ticking all clients.
- **Skill distribution on install** (#473 follow-on): `install.sh` now syncs
  `.claude/commands/` *and* `.claude/skills/` into `~/.claude/` on every
  install/upgrade, with a manifest-scoped prune that removes only paths cw
  itself installed — foreign skills are never touched. cw-coupled commands and
  skills that previously lived only in `~/.claude` (queue-issues, graduate-plan,
  review-sweep, setup, cw-session-watch) are migrated into this repo as the
  single source of truth.

### Fixed

- **Staged advance machine was unreachable in production** (#698): reconcile's
  emitted-sentinel router (`_apply_sentinel_to_task`) routed completed-stage
  tasks with the stale monolith mapping *before* the advance machine ran,
  blocking every staged ticket at PLAN. The advance decision is extracted into
  `apply_staged_decision` and shared by both the consume and reconcile paths.
- **Scope-gated advance ignored a null `scope.tier`** (#696): a real PLAN
  sentinel can emit `scope.tier=null` pre-impl; the advance machine now falls
  back to `task.scope_hint` (mirroring reconcile's tier resolution) instead of
  blocking small tickets.
- **Sentinel persisted after the advance decision** (#694): `last_result` is now
  written before the advance decision in `consume_completed_sessions` (forward-
  compat for stdout-carrying completion events).
- **Install served a stale cached wheel** (#473): `install.sh` now passes
  `--reinstall --no-cache`, so a code-changed/version-unchanged rebuild is never
  skipped.
- **Headless worker isolation guard** (#402): `auto-dev-impl.md` codifies that a
  headless worker's git operations target only its worktree
  (`cw-context.json:worktree_path`), never the operator's main checkout; the
  cw-side `worktree_path` injection landed earlier in `spawn.py`.

### Changed

- **`cli.py` split into a `cli/` package** (#705): the 3568-line module is broken
  into focused submodules; no behavior change.
- **Docs**: tracker-descriptor seam (ADR 0008), review-system-in-cw design
  (RFC 0006), and the stage ledger (#692).

## [1.1.3] — 2026-06-14

RFC 0005 forward-compat seams (milestone #7). Additive, dormant data-model and
executor scaffolding so a later staged-pipeline engine (v1.2.0) can be wired in
without another schema migration. No dispatch behavior change.

### Added

- **Stage data-model + schema bump** (#612): `Stage` enum, `StageExecutorConfig`,
  `StagePipelineConfig` (with a `LaneConfig.pipeline` lane override and
  `ClientConfig.pipeline`), and dormant `TicketTask.stage`/`stage_base_ref` +
  `Session.stage` fields. Bumps `CW_STATE_SCHEMA_VERSION` 9→10 and
  `DEV_QUEUE_SCHEMA_VERSION` 3→4 with store-level migrations that default the new
  fields on load (verified against real on-disk v9/v3 state).
- **`StageExecutor` seam** (#613): a `@runtime_checkable` Protocol plus
  `ClaudeNativeExecutor` wrapping `spawn_create_impl` + the native daemon
  (forwards `--model`). Unwired — not yet called by dispatch.
- **`cw schema stage-output <stage>`** (#614): exposes the per-stage sentinel JSON
  schema (the validation contract foreign executors will consume).
- **`.cw/` worktree exclude** (#615): worktree creation idempotently registers
  `.cw/` in `$GIT_COMMON_DIR/info/exclude`, never touching the consumer's
  committed `.gitignore`.

## [1.1.2] — 2026-06-14

Dispatch-reliability cluster — worker liveness, re-dispatch safety, and
disposition visibility (the 1.1.x reliability backlog; all share the
on-demand-reconcile staleness root captured in ADR-0007).

### Fixed

- **`claude_session_id` backfilled at spawn-return** (#635): `spawn_create_impl`
  now attempts `_csid_from_transcript` before `save_state`, so a live worker's
  session id is populated immediately instead of staying null until the next
  operator-triggered `reconcile()` (hours later). Makes liveness detection
  (csid→transcript→silence) actually work for fresh workers.
- **Shipped tickets are no longer re-dispatched** (#637): before reverting a
  RUNNING task to PENDING, reconcile consults world state — if the ticket's PR
  is merged, the task is marked completed instead of reverted+re-dispatched. The
  PR-merge pre-pass runs outside `sessions_lock` and the guard covers all three
  revert sites (`_act_on_phantom_candidates`, `_act_on_stalled_candidates`,
  `_act_on_idle_candidates`). Closes the data-safety hazard that re-ran an
  already-merged ticket under `reap_policy: auto`.
- **Approval-pending sessions are surfaced, not hidden** (#633):
  `plan_pending_approval` / `review_pending_approval` now map to
  `BLOCKED_ON_USER` (were silently recorded as `completed` with null
  disposition); `cw dev-queue status` gains a `BLOCKED` column.

### Docs

- **ADR-0007 — reconcile cadence and ownership** (#639): on-demand vs
  background-ticker vs daemon-primary-runner; recommends an opt-in periodic
  ticker (Option B2) with a cron fallback, daemon promotion deferred.
- **#636 known limitation** documented in `auto-dev.md` Step 4c: headless
  `gh pr create` is blocked by the `auto` permission classifier inside the
  worktree-isolated `/prep-pr` subagent; the effective fix (non-`auto` worker
  `permission_mode`) is deferred to the RFC 0005 FINALIZE/REVIEW stages
  (#621/#622), which carry the requirement.

## [1.1.1] — 2026-06-13

Dispatch/reconcile reliability and packaging-gate hardening (Sprint 0 follow-ons to RFC 0004 Phase 4).

### Added

- **CI `package-smoke` job** (#611): builds the wheel (`uv build`), installs it
  into a clean env (`uv tool install --no-cache`), and smoke-tests the installed
  `cw` — including `cw guide` to assert the `GUIDE.md` data file is packaged.
  Closes the gap where a packaging break (e.g. #609) passed CI and only failed
  at `uv tool install`.
- **`SESSION_REAP_AUTHORIZED` audit event** (#603): `_reap_session_by_selector`
  now emits `session.reap_authorized` after the destructive reap, recording
  authority (`operator` vs `orchestrate-run`), lane, and the proposed action —
  closing the auditability gap under the automated 4c consumer (ADR-0006).

### Changed

- **`cw dev-queue status` footer** (#598): the "Last dispatch tick per client"
  block is now labelled as a historical snapshot, not live state; added a
  "Reading the status output" section to the dispatch runbook.

### Fixed

- **Worktree fetch-fail log spam** (#597): an unreachable client `origin` no
  longer floods every dispatch tick with a multi-line git `fatal:` block —
  stderr is collapsed to one line and de-duplicated per client per run
  (caller-owned warn set, mirroring `warned_stale`).
- **Graceful `Ctrl-C` on `cw orchestrate run`** (#604): the poll loop now exits
  130 with a one-line "stopped" message instead of a raw `KeyboardInterrupt`
  traceback; cursor state stays clean (idempotent replay, no cursor flush).
- **Version single-sourced from package metadata**: `cw.__version__` is now
  derived via `importlib.metadata` instead of a hardcoded literal, so
  `cw --version` / `cw doctor` can never diverge from `pyproject.toml` again.
  (The first `v1.1.1` tag's release build correctly failed its version-match
  guard because the literal was missed — this removes that failure class.)

### Docs

- **RFC 0005 — staged pipeline & heterogeneous executors** (#629): the
  pipeline-axis design (stage as a first-class concept; `StageExecutor` seam).
  v1.1.x ships only the forward-compat seams; the engine lands in v1.2.0.

## [1.1.0] — 2026-06-12

Lane-aware dispatch, gated reaping, and state-integrity hardening.

### Added

- **RFC 0004 lane system — Phases 1–4** (#575/#576/#577/#581): First-class
  `lane` routing throughout the dispatch pipeline.
  - **Phase 1** (#575): `TicketTask.lane` field (default `"default"`);
    DEV_QUEUE_SCHEMA_VERSION v3.
  - **Phase 2** (#576): Two-tier lane-aware scheduler — per-lane concurrency
    caps, pause/resume, priority ordering.
  - **Phase 3** (#581): `cw lane` CLI (`create`, `pause`, `resume`, `list`,
    `show`, `set-concurrency`); `--lane` flag on `cw dev-queue add`; runtime
    concurrency overrides persisted in `ConcurrencyOverrides` outside
    `orchestrator.yaml`.
  - **Phase 4** (#577): Lane-aware `cw status` / dashboard grouping; consumer
    audit.
- **`SESSION_REAP_PROPOSED` event + gated-act authorization** (#573): reconciler
  emits `session.reap_proposed` before any destructive reap; the owning task is
  routed to `BLOCKED_ON_USER` under the default `signal_only` policy.
- **`ReapPolicy` enum** (#572): `signal_only` (default) keeps surfaces intact
  and waits for operator action; `auto` enables self-healing (stop daemon, revert
  task to PENDING, clean worktree).
- **Per-lane `reap_policy` resolution** (#582): each lane can override the
  client-level reap policy; the reconciler resolves the effective policy per
  session by checking lane → client → global in order.
- **Auto fast-forward local main** (#569): `cw dev-queue` fast-forwards the
  local main branch when it is behind origin before dispatching, preventing
  stale-base merges.
- **CI status + mergeable in `MonitoredPR`** (#571): `cw status` surfaces
  `ci_status` and `mergeable` for each open PR alongside the existing fields.
- **Agent onboarding wiring** (#562): `cw install` now registers the MCP server,
  adds the `cw` allowlist entry, wires the SessionStart hook, and appends the
  `CLAUDE.md` snippet in a single command.
- **Route emitted-but-unrouted sentinels** (#584): reconciler now routes
  `AUTO_DEV_RESULT` sentinels that were emitted before the idle watchdog fires,
  preventing orphaned successful results from blocking further dispatch.

### Fixed

- **dev-queue wait resolves oldest terminal duplicate** (#585): `cw dev-queue
  wait` now resolves the oldest terminal-state duplicate task rather than a live
  one, preventing premature wait-exit when duplicate entries exist.
- **Inbox cursor-not-found wedge + torn-read** (#565): event-history reader no
  longer wedges when the inbox cursor points past the end of a rotated file; torn
  reads under concurrent writers are also closed.
- **Config re-resolved each tick** (#566): `run_watcher_tick` re-reads
  `clients.yaml` on every dispatch tick so hot-edited config takes effect without
  restarting the watcher.

### Refactored

- **Migrate remaining `sessions.json` writers to `mutate_state()`** (#567): all
  state mutations now go through the single-lock path introduced in v0.14.2,
  closing the last direct-write windows.

### Schema

- **v8** (#573): added `Session.reap_proposed_at`. Purely additive; defaults to
  `None`. Auto-migrates on first load.
- **DEV_QUEUE v3** (#575): added `TicketTask.lane`. Purely additive; defaults to
  `"default"`. Auto-migrates on first load.

### Documentation

- **ADR-0006**: reaping is gated by an authority, not automatic.
- **State-integrity audit** (#38042be): ADR-0005, RFC 0004 §State integrity,
  implementation plans.
- **Headless post-sentinel hardening** (#580): salvage-ship recipe and
  turn-end hardening notes for headless sessions.

## [1.0.0] — 2026-06-11

First stable release: the multiplexer layer (cmux/tmux) is deleted entirely,
workers spawn via `claude --bg` and are tracked by the native daemon roster,
and the reap path has been bulletproofed end-to-end (#543).

### Architecture

- **Native supervisor replaces multiplexer layer** (#119/#521): cmux/tmux
  adapters, the `MultiplexerAdapter` abstraction, and all wrapper shims are
  deleted. Workers spawn via `claude --bg`; liveness tracked through
  `~/.claude/daemon/roster.json`. No external multiplexer required.

### Added

- **Observable reaps: `queue.session_reaped` + `ReapReason` taxonomy**
  (#380): the reconciler emits a structured `queue.session_reaped` event for
  every reap decision, carrying one of eight `ReapReason` values
  (`phantom_surface`, `idle_stall`, `usage_limit_cutoff`, `retry_cap_parked`,
  `wall_clock_budget`, `completed_backstop`, `salvage_completed`,
  `salvage_parked`). `Session.reap_reason` (schema v7) records the reason on
  the session object.
- **Confirm-before-reap** (#545): idle watchdog waits for
  `idle_confirm_observations` (default: 2) consecutive idle observations before
  triggering an idle-stall reap. `Session.idle_observation_count` (schema v6)
  tracks the accumulating count.
- **Widened subagent-liveness window** (#544): `SUBAGENT_LIVENESS_WINDOW_SECONDS`
  raised 900 s → 1800 s so a single long quiet tool call or in-flight subagent
  is not reaped as idle. The transcript-mtime window and elapsed budgets are
  unchanged, and roster presence is deliberately NOT treated as proof-of-life
  (a dead worker can linger in the roster).
- **Unified transcript locator via `surface_ref`-prefix glob** (#541): precise
  liveness check resolves the transcript path from the daemon session id prefix
  rather than scanning all transcripts.
- **Sentinel-aware `cw dev-queue wait`** (#535): `wait` detects
  `AUTO_DEV_RESULT` sentinels in the transcript directly, eliminating
  false-timeout (exit 124) for long-running workers whose reconcile cycle
  hasn't fired yet.
- **`cw result validate`** (#482): pre-emit gate validates a candidate
  `AutoDevResult` JSON object against the authoritative schema.
- **`cw schema`**: inspect Pydantic model schemas for `AutoDevResult`,
  `TicketTask`, and `Session` directly from the CLI.

### Schema

- **v5** (#119/#521): cleared legacy multiplexer `surface_ref` values on
  upgrade. Auto-migrates on first load.
- **v6** (#545): added `Session.idle_observation_count`. Purely additive;
  defaults to `0`.
- **v7** (#380): added `Session.reap_reason`. Purely additive; defaults to
  `None`.

### Documentation

- **Operator runbooks** (#538/#539): `docs/dispatch-runbook.md` (end-to-end
  `cw dev-queue` dispatch procedure) and `docs/session-disposition.md` (how to
  read a session's outcome from the transcript sentinel).
- **`docs/MIGRATION-0.x-to-1.0.md`**: updated to cover the v5→v7 schema
  chain, the new 1.0 contract surface, and the `cw daemon` deprecation shim.

### Known issues

- **#542**: `cw dev-queue wait` can ride to the hard timeout (exit 124) instead
  of returning exit 3 (ATTENTION) when a session is reaped mid-wait. The
  sentinel-aware path only catches successful sentinels; a reap-during-wait
  is not yet signalled early.

## [0.14.2] — 2026-06-03

Reliability-hardening release: a wave of concurrency, atomicity, and
crash-safety fixes across the state store, config writes, worktree lifecycle,
event history, and the reconcile/daemon loops — closing torn-read, TOCTOU, and
lost-update windows that surface under parallel dispatch.

### Fixed

- **`sessions.json` read-modify-write locking** (#424 → PR #437): the state
  file is now locked across the full read-modify-write, closing a lost-update
  window when concurrent session operations raced.
- **Atomic + locked `clients.yaml` write** (#429 → PR #442): config writes go
  through an atomic, locked replace so a crash or concurrent writer can't leave
  a truncated `clients.yaml`.
- **Atomic hook-context writes + live-session guard** (#427 → PR #440): spawn
  writes hook context atomically and refuses to overwrite a live session's
  context.
- **Event-history hardening** (#433 → PR #449): torn-read, TOCTOU, fsync, and
  poll-lock fixes in `history.py` and the channel servers.
- **Worktree dirty-state guards** (#425 → PR #439, #426 → PR #441): dirty-check
  before force-removing a stale/timed-out worktree, and refuse dirty-worktree
  reuse in `create_worktree`.
- **Interactive-start isolation guard** (#428 → PR #448): guard interactive
  `start` isolation and `fast_forward_main` so an interactive session can't
  clobber in-flight work.
- **Reconcile salvage + parked-session skip** (#431 → PR #446): reconcile
  salvages all terminal statuses and skips parked sessions instead of
  re-flagging them.
- **Malformed-roster tolerance + `idle_watchdog=0`** (#432 → PR #443):
  reconcile tolerates malformed roster JSON and honors `idle_watchdog=0` as
  "disabled".
- **Daemon tick guards** (#390 → PR #444): per-client and whole-tick guards in
  `run_watcher_tick` so one client's failure can't abort the tick.
- **Sparse sentinel coercion** (#430 → PR #445): legitimate-but-sparse
  `AutoDevResult` sentinels are coerced rather than rejected.
- **Dispatch stdout visibility** (#420 → PR #435): operator-visible stdout for
  `cw dev-queue run`.
- **Logging handler at entrypoint** (#423 → PR #434): the logging handler is
  configured once at the CLI entrypoint.
- **dispatch-guard workflow YAML** (PR #450): repair invalid YAML in the
  dispatch-guard workflow.

### Documentation

- **Full CI gate set documented** (#436 → PR #447): the quality-gate docs now
  cover format, `mypy --strict`, pre-commit, coverage thresholds, and
  diff-cover.
- **Two-branch model + stale comment** (PR #414): correct the two-branch model
  in `headless-contract.md` and a stale cmux comment in `worktree.py`.

### Operations

- **Dispatch-critical drift guard + release closer** (PR #415): CI guard for
  dispatch-critical drift, plus auto-close of `dispatch-drift` issues on
  release.

## [0.14.1] — 2026-05-30

Bug-fix release closing dispatch-reliability gaps surfaced by the 2026-05-30
fanout-cascade RCA: a re-dispatched ticket can no longer inherit a prior run's
worktree state, and the idle-watchdog budget is now operator-tunable so workers
mid-plan/review aren't reaped at 15 minutes.

### Fixed

- **Stale-worktree reuse** (#404 → PR #410): `create_worktree` verifies the
  existing worktree is checked out on the requested branch before reusing it,
  raising `StaleWorktreeError` on a mismatch (wrong branch, detached HEAD, or not
  a registered worktree). The dispatch loop catches it, force-removes the stale
  tree, and reverts the task to `PENDING` so the retry rebuilds clean — closing
  an infinite-respawn window. `reconcile` also reaps a timed-out session's
  worktree on both the wall-clock-timeout and idle-stall-recover paths.

### Added

- **Tunable idle-watchdog budget** (#412): new `idle_watchdog_seconds` key in
  `orchestrator.yaml` overrides the hardcoded 900s (15 min) global default —
  15 min was reaping headless workers still mid-plan/mid-review. Falls back to
  the constant when unset; per-ticket and per-tier overrides still take
  precedence. Also adds `.claude/project-config.yaml` pinning
  `tracking.primary.system: github-issues` for this repo.

## [0.14.0] — 2026-05-30

Dispatch-reliability release: the idle watchdog now **auto-recovers** stalled
headless workers (bounded retries, then parks) and **salvages** a terminal
sentinel before flagging, so a worker that already shipped is never mis-parked
as `blocked_on_user`. Plus deterministic sentinel routing, parse-boundary
coercions, a cross-client `cw queue list`, and watchdog/doctor robustness fixes.

### Added

- **Idle-stall auto-recovery** (#384 → PR #394): the idle watchdog now reverts a
  provably-idle headless worker to `PENDING` for re-dispatch (capped per tier via
  `idle_retry_cap_by_tier`), only parking it `BLOCKED_ON_USER` once retries are
  exhausted — instead of parking on the first stall.
- **Cross-client `cw queue list`** (#201 → PR #397): `cw queue list` with no
  CLIENT arg now shows tasks grouped across all configured clients.

### Fixed

- **Idle watchdog salvages terminal sentinels** (#398 → PR #400): a shipped/no_op
  session idle past the budget is salvaged to `COMPLETED` rather than flagged
  `BLOCKED_ON_USER`.
- **Deterministic parse-failure routing** (#263 → PR #396): `schema_version_unsupported`
  and other deterministic parse failures route to `FAILED` (not an infinite
  PENDING retry); unknown blocker reasons route to `COMPLETED`.
- **no_op pre-impl `scope.lines_actual` coercion** (#399 → PR #401): a `no_op` at
  `stage1_pre_flight`/`stage1_plan` with a stray non-null `lines_actual` is
  coerced clean at the parse boundary instead of failing `validation_failed`.
- **blocked + stray `next_actions` coercion** (#371 → PR #376).
- **Transcript-mtime liveness check** (#340 → PR #383): prevents false-positive
  watchdog fires on workers that are actively writing their transcript.
- **`cw doctor` wedge-detection robustness** (#354 → PRs #378/#379): BACKGROUNDED
  exclusion + subprocess timeouts.
- **Reviewer stale-ref helper** (#381 → PR #385): `fetch_feature_branch` resolves
  stale local refs before review.
- **`_accumulate_task_cost` docstring + is-None guard** (#352 → PR #377).

## [0.13.0] — 2026-05-29

Minor release hardening the dispatch-reliability path: terminal-sentinel
salvage before timeout/crash disposition, cleaner no_op contract parsing, and a
`cw doctor` worktree-path check — plus the dev-queue missing-dir guard and a
type-suppression cleanup batch.

### Added

- **`cw doctor` worktree path existence check** (#143 → PR #365): `cw doctor`
  now flags sessions whose recorded worktree path no longer exists on disk,
  catching the drift where a worktree is removed out from under a tracked
  session.

### Fixed

- **Sentinel salvage on timeout/crash** (#372 → PR #374): the `TIMED_OUT` and
  crashed-phantom sweeps in `reconcile` now recover a terminal-success
  `AUTO_DEV_RESULT` (`shipped`/`no_op`) from the session's transcript before
  finalizing disposition. A headless worker that emitted a valid sentinel and
  then stalled (e.g. waiting on CI) or whose surface died is now recorded
  COMPLETED with its real result — and its ticket is **not** reverted to
  PENDING — instead of being mislabeled `timed_out`/`crash` and re-dispatched
  (dup-PR risk). Guards the reused-worktree stale-transcript case (#358) by
  only trusting a transcript modified after the session started.
- **`no_op` + stray-PR coerced at parse boundary** (#367 → PR #370): a sentinel
  reporting `no_op` while also carrying a stray `pr_url` is now coerced to a
  clean `no_op` at the parse boundary, so the disposition isn't ambiguous
  downstream.
- **dev-queue `refresh-all` skips missing client dirs** (#356 → PR #369):
  `refresh-all` now gracefully skips clients whose workspace directory is
  missing (e.g. a dead `sigma` entry) instead of crashing the whole refresh.

### Removed / chore

- **`get_item` queue helper** (#360 → PR #368): adds a `get_item` accessor,
  dropping 7 `type: ignore[union-attr]` suppressions.
- **Test annotation fixes** (#361 → PR #373): removes 32 `type: ignore`
  suppressions across the test suite via proper annotations.

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
