# RFC 0018 — Backend stage parity: codex on every lane, one contract source

## Summary

A client can route any pipeline stage to the claude-native, codex, opencode or local executor, but only claude-native honors the full stage contract. Codex runs REVIEW only. Opencode runs all four stages from hand-copied prompts that have not followed the command docs since 2026-08-19, so it silently drops about 27 contract elements. Codex and opencode also lose tracker content silently on any non-GitHub tracker, and neither records usage.

This RFC brings codex to every stage (plan, impl, review completeness, finalize) and puts the stage contracts behind one source. The source is a tagged-element manifest with a drift test, followed by prompts rendered from tagged command-doc blocks. Opencode keeps fail-closed guards and the manifest, with no live parity work. Non-GitHub trackers (Linear today) are reached through the backend's own MCP, so ADR-0013 stands. Silent loss becomes a visible blocker first, and a working path follows.

Adjudication, the built-in reviewer roster and egress policy for codex review stay with #2464. RFC 0017 has landed (v1.91.0), so this RFC also lifts its codex deferral (amended 2026-11-09).

A repo review engine, meaning a custom review skill named by `review.engine`, now runs on a codex review lane:
- codex runs each role the engine declares;
- one codex aggregation turn writes the cw-owned engine summary;
- no Claude call is made.

Opencode keeps the engine fail-closed.

## Motivation

The contract lives only as prose in `.claude/commands/auto-dev-*.md` (6,730 lines). Every other copy is hand-forked and nothing detects drift.

- **Opencode prompts are a frozen snapshot.** `src/cw/opencode_runner.py` carries self-contained PLAN, IMPL and REVIEW prompts written 2026-08-19. About ten contract elements landed in the docs between 2026-10-16 and 2026-11-08 and none reached the prompts. The prompt files contain no mention of `review_feedback`, `operator_resolutions`, `scope_hint`, `plan_approved_*`, `must_fix_override`, Decided Constraints or the pre-flight resolutions marker. Only the clean-review module was kept in sync.
- **The opencode prompts disagree with each other.** The IMPL prompt recovers the plan from a `<!-- plan-spec-reviewed` comment (`src/cw/opencode_runner.py:501`); the PLAN prompt never writes that marker.
- **Codex review re-implements the review contract in Python** and diverges: no adjudication or deferred-findings file, no quality gates after a fix cycle, delta-only re-reviews, and a thin sentinel (`plan_source: none`, empty `commits` after fix commits).
- **Non-GitHub trackers lose content silently.** The codex verdict comment is skipped with a log line (`src/cw/codex_review_persist.py:225-234`), and the ticket's operator comments are never fetched (`src/cw/codex_review/_context/core.py:155-156`). The opencode plan prompt says "use the configured tracker's CLI otherwise" and "when the tracker is writable" with no command (`src/cw/opencode_runner.py:455-457,485-486`).
- **Usage is invisible off claude-native.** Cost comes from the sentinel's self-reported `cost_usd`. Opencode, local and the codex fix invocations record nothing; codex review token fields are null on codex-cli 0.164.0 (#2837).
- **Parity is decided one ticket at a time.** #2983 chose to fail closed on repair rows instead of implementing the contract, and nothing records which other elements were skipped on purpose.

## Design

The design keeps REVIEW Python-driven, adds agent-turn stages for the rest, and makes drift impossible to miss. It is built from four epics.

### Epic I — One contract source

Tag every contract element in the command docs with a stable id, record a per-backend disposition for each in a manifest, and fail a test on any gap or stale reference. Then render backend prompts from tagged neutral blocks, starting with the new codex stages, so no new hand copy is created. Legacy opencode prompts stay hand-written under the manifest guard. Tags are single-line HTML comments, so the docs read the same to Claude. The renderer must also cover `/prep-pr` and `/ship-it`, because FINALIZE delegates to them through the Skill tool, which codex does not have.

### Epic II — Codex stage driver

Split the stage-neutral completion scaffolding out of `src/cw/codex_background.py` and thread the stage through `cw codex run`, requeue and the crash-recovery gate. Then add PLAN, IMPL and FINALIZE as one agent turn per stage on a rendered contract prompt, with cw running the same `.claude/scripts` gates claude-native runs after the turn and validating the sentinel itself. Codex REVIEW gets only the stage-completeness slice: sentinel fields, post-fix quality gates and `review_feedback`. IMPL reuses the fix-loop plumbing (fence, growth budget, commit, verified push), which already edits, commits and pushes.

### Epic III — Non-GitHub trackers and accounting

First make every silent skip a blocker or event and extend the tracker MCP gate and `cw doctor` to codex and opencode lanes. Then add a narrow tracker-IO agent turn for writes that have no agent to hold an MCP (the verdict comment, `approve --post-marker`, `resolve --post`): a `codex exec` with an explicit MCP allowlist that posts `--emit-only` parts and returns ids that cw verifies, the same emit-and-verify pattern RFC 0017 D-7 uses for debt tickets. Finally record usage for non-claude stage turns.

### Epic IV — Opencode guards

Opencode gets the manifest, fail-closed guards for the gaps that would otherwise run silently wrong, and the consistency fixes between its own prompts. No live parity work; tests use the existing mocks.

## Phasing

- Wave 0, seams (sprint 1): S1 contract manifest, S2 stage-neutral codex scaffolding, S3 loud-failure floor.
- Wave 1 (sprint 2): A1 codex REVIEW completeness, A2 tracker-IO turn, A3 contract renderer, B1 opencode guards.
- Wave 2 (sprint 3): C1 codex PLAN, C2 codex IMPL, A4 repo review engine on codex review lanes.
- Wave 3 (sprint 4): C3 codex FINALIZE, C4 usage accounting.
- Wave 4 (sprint 5): D1 all-codex lane validation.

## Resolved decisions

These are firm leans, decided to unblock the breakout. Each one can be reversed at ticket-hardening if the code contradicts it.

- **D-1 — Trackers other than GitHub are reached through the backend's own MCP.** Linear and any other MCP-backed tracker are expected to be installed, enabled, configured and authenticated for the backend that runs the stage. cw verifies this and parks when it is missing. cw gains no tracker client, so ADR-0013 stands. Daemon-only writes with no agent present go through a narrow tracker-IO agent turn (a `codex exec` with an explicit MCP allowlist) that posts the emitted parts and returns ids that cw verifies. Reverses at A2 hardening if codex cannot load an MCP headless, or if the extra turn per post proves too costly; the fallback is a narrow daemon-side client under its own ADR.
- **D-2 — REVIEW stays Python-driven; PLAN, IMPL and FINALIZE are one agent turn per stage.** The turn runs on a prompt rendered from the contract. After it, cw runs the same `.claude/scripts` gates claude-native runs (scope conformance, empty-diff, quality gates, push verification) and validates the sentinel. Reimplementing the stages in Python would create a third copy of the contract. The stage-neutral scaffolding is split out of `codex_background.py` before any stage is added. Reverses at S2 hardening.
- **D-3 — The confirm round is unchanged.** `review.confirm_backend: none` or `codex` already gives a Claude-free codex lane, flagged not independent. This RFC documents it and adds a `cw doctor` note for all-codex lanes. A new independent confirm family is out of scope.
- **D-4 — Opencode gets drift tests and fail-closed guards only.** No live parity work and no live tests; mocks only. Reverses never inside this RFC.
- **D-5 — Codex REVIEW parity covers stage-completeness and the repo review engine.** In scope: the sentinel fields, post-fix quality gates and `review_feedback` (A1), plus the RFC 0017 engine round on a codex review lane (A4, D-8). Adjudication of built-in findings and the built-in roster and egress policy stay with #2464. Deferred-finding filing on codex already landed in #3137. (Amended 2026-11-09: D-5's reversal point, RFC 0017 landing, has been reached.)
- **D-6 — The contract has one source: a manifest first, then rendering.** Stable element ids in the command docs, a per-backend disposition manifest and a drift test land first. New codex prompts are rendered from tagged neutral blocks from day one. A shared contract module that generates the docs is rejected as too large.
- **D-7 — RFC 0017 is respected; its codex deferral is lifted and its opencode deferral stays.** The elements RFC 0017 adds have these manifest dispositions:
  - review attestation, engine summary and engine round: `implemented` for codex once A4 lands, and `deferred` for codex until then;
  - opencode: `fails-closed`, per D-4.

  #2999 has merged, so `models/tasks/` is already a package. (Amended 2026-11-09.)
- **D-8 — Codex runs the engine itself; Claude is never called for the engine round.** (Operator decision, 2026-11-09.)
  - The engine declares what it needs in a cw-owned, versioned manifest inside its skill directory: its roles, each with an agent spec file, and an aggregation prompt.
  - cw's codex review lane runs each role through the per-role `codex exec` machinery it already uses for the built-in roster.
  - One codex aggregation turn then writes the RFC 0017 engine summary (D-12 there, backend-neutral) to the cw-supplied path.
  - cw validates the summary exactly as on claude-native: `cw review engine record`, coverage `COMPLETE`, fail closed.
  - The RFC 0017 guarantees carry over unchanged: the engine is read from the default branch (materialize), and a diff touching the engine parks (D-13 preflight).
  - An engine with no codex manifest declares itself claude-native only. A codex lane then refuses it loudly with a named reason, rather than silently falling back to the built-in roster.
  - Reverses at A4 hardening, if a codex aggregation turn cannot reliably produce a schema-valid summary.

## Tickets

### S1 — Contract element manifest and drift test

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** The stage contracts exist only as prose in the command docs, and the opencode prompts and codex review logic are hand copies with no drift check. This ticket tags every contract element with a stable id, adds a per-backend disposition manifest, and adds a test that fails on any gap. Use single-line comment tags so rebases over RFC 0017's review-doc edits stay trivial. It defines a schema, so harden it before dispatch.
- **Scope:** D-4, D-6, D-7
- **Acceptance:**
  - Each contract element in the intake, plan, impl, review and finalize docs, their appendices, `/prep-pr` and `/ship-it` carries a stable id in a single-line HTML comment that does not change how Claude reads the doc.
  - A manifest assigns every id a disposition per backend (claude-native, codex, opencode, local) from a closed vocabulary of implemented, fails-closed, not-applicable and deferred, each citing a code or prompt reference or a ticket number.
  - A test fails when a doc id has no disposition for a backend, when a disposition cites an id absent from the docs, and when an implemented disposition cites a reference that does not exist.
  - The manifest is seeded from this RFC's findings so every named opencode gap and codex gap has a disposition and no cell is blank.
  - Elements RFC 0017 adds enter as deferred for codex and opencode citing RFC 0017.
  - Kill-checks: deleting a tag from a doc, or a disposition from the manifest, fails the test.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### S2 — Stage-neutral codex scaffolding

- **Epic:** II
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** `src/cw/codex_background.py` (992 lines) mixes the legacy thread registry, queue-row writers, config resolvers and review orchestration, `Stage.REVIEW` is hardcoded in `cw codex run`, requeue and the crash-recovery gate, and codex sessions are recorded with purpose IMPL. This ticket extracts the stage-neutral completion scaffolding and threads the stage through with no behavior change for REVIEW. Land any new row field after #2999.
- **Scope:** D-2, D-7
- **Acceptance:**
  - The blocked marker, completion through the door, unexpected-error revert and session-id stamp live outside the review orchestration and `codex_background.py` falls below 800 lines.
  - `cw codex run --stage` accepts every stage the executor supports and dispatches on it, and an unsupported stage still exits non-zero naming the missing executor.
  - The crash-recovery gate takes the stage and, for a committing stage, accepts an advanced HEAD only when it descends from `stage_base_ref`, with tests for the review, plan and impl shapes.
  - Session purpose is recorded per stage instead of always IMPL, with every consumer of purpose listed in the PR and a test for each.
  - Codex REVIEW behavior is unchanged: the existing codex review tests pass without edits.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### S3 — Loud-failure floor for non-Claude backends on non-GitHub trackers

- **Epic:** III
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** On a non-GitHub tracker the codex lane skips the verdict comment with a log line and drops the ticket's operator comments, and the opencode plan prompt says to use the tracker's CLI and post when writable; none of it reaches the operator. This ticket makes each one a blocker or visible event and extends the tracker MCP gate and `cw doctor` to the codex and opencode lanes. It adds blocker reasons, so harden it before dispatch.
- **Scope:** D-1
- **Acceptance:**
  - A skipped verdict comment on a non-GitHub tracker parks the row with a named blocker reason or emits an operator-visible event, never a log line alone.
  - A non-GitHub tracker on a codex review emits an operator-visible event naming the comments that were not delivered to the reviewer.
  - The opencode PLAN, IMPL and REVIEW prompts name the tracker operations per tracker and say to emit blocked with a named reason when no tool is available, replacing "when the tracker is writable".
  - The tracker MCP gate parks a codex or opencode row before spawn when the configured tracker MCP is not enabled for that backend, and `cw doctor` reports the same condition per lane.
  - The GitHub path is unchanged and tests cover each new reason on both trackers.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### A1 — Codex REVIEW stage-completeness slice

- **Epic:** II
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S2
- **Context:** The codex REVIEW result hardcodes `plan_source` to none, leaves `commits` empty after fix commits and sets no `fork_point_sha`, runs no quality gates after a fix cycle, and never reads `review_feedback`. FINALIZE and IMPL depend on these. Adjudication, roster and egress stay with #2464 and RFC 0017. It changes sentinel fields, so harden it before dispatch.
- **Scope:** D-5
- **Acceptance:**
  - A codex review result carries `plan_source` from the plan, and `commits` and `fork_point_sha` for fix-loop commits, and passes sentinel validation.
  - FINALIZE accepts a codex review result carrying those fields, covered by a test.
  - After each fix commit the loop runs the repo's configured quality gates and a failing gate parks with a named reason instead of proceeding to re-review.
  - `queue_metadata.review_feedback` reaches the codex review prompt as untrusted data bound to the live PR, and a stale head fails closed.
  - The manifest dispositions for these elements move to implemented.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### A2 — Tracker-IO agent turn for daemon-only writes

- **Epic:** III
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S2, S3
- **Context:** Daemon-only writes such as the codex verdict comment, `cw dev-queue approve --post-marker` and `resolve --post` run with no agent that can hold a tracker MCP. This ticket adds a narrow `codex exec` turn with an explicit MCP allowlist that posts `cw post-comment --emit-only` parts and returns the created ids, which cw verifies as RFC 0017 D-7 does. It must reconcile with the lean profile in #1711 and the sandbox knob in #1767. How Linear markdown treats hidden HTML-comment markers is unverified and is settled here.
- **Scope:** D-1
- **Acceptance:**
  - A codex review verdict is posted on a non-GitHub tracker through the turn, and the returned ids are verified against the emitted parts with a mismatch parking under a named reason.
  - The turn's tool allowlist is limited to the tracker's comment create and read operations, with a test asserting the launch argv.
  - Emitted parts are posted verbatim after redaction, and the GitHub path still posts through `gh` unchanged.
  - A recorded probe or fixture shows whether the pipeline's hidden comment markers survive the tracker's markdown, and the ticket records the fallback if they do not.
  - A tracker MCP that is not enabled for the turn surfaces the S3 blocker instead of a silent skip.
  - An opt-in live probe covers headless MCP loading for codex and is never part of default CI.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### A3 — Contract renderer for backend prompts

- **Epic:** I
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S1
- **Context:** New codex stages need prompts without a new hand copy. This ticket marks backend-neutral blocks in the command docs, adds a renderer that builds a stage prompt for a backend from them, and covers `/prep-pr` and `/ship-it` because FINALIZE delegates to them through the Skill tool codex lacks. Claude-only machinery (Agent tool, Skill tool, hooks) stays outside the neutral blocks. It defines stage prose, so harden it before dispatch.
- **Scope:** D-2, D-6, D-7
- **Acceptance:**
  - Neutral blocks carry begin and end tags, and a renderer returns deterministic prompt text for a stage and backend, pinned by golden tests with a documented regeneration step.
  - A rendered prompt contains no reference to Claude-only machinery, enforced by a denylist test covering the Agent tool, the Skill tool, `AskUserQuestion` and `$CW_SESSION`.
  - Every manifest id a stage needs appears in its rendering or carries a fails-closed disposition for that backend, enforced by a test.
  - `/prep-pr` and `/ship-it` neutral blocks render for FINALIZE.
  - The docs differ from their pre-change text only by tags, and a test strips the tags and compares.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### B1 — Opencode fail-closed guards and prompt consistency

- **Epic:** IV
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** S1
- **Context:** Opencode keeps hand-written prompts under D-4, so the gaps that would run silently wrong must fail closed instead. A repair row with `review_feedback` and no adoption branch passes the current guard and runs as plain impl, the PLAN and IMPL prompts disagree about the plan marker, the FINALIZE prompt calls the stage backend-neutral although its doc now uses the Skill tool, and its sentinel framing differs from the doc. This ticket decides #2983 as fail-closed throughout.
- **Scope:** D-4, D-6
- **Acceptance:**
  - Opencode IMPL blocks any row carrying `review_feedback`, with or without an adoption branch, under the existing `repair_entry_unsupported_backend` reason.
  - The opencode PLAN prompt writes the plan marker its IMPL prompt reads, or IMPL stops depending on it, and a test pins the two prompts consistent.
  - The opencode FINALIZE prompt uses the same result-emit framing as the doc, the stale backend-neutral comment is corrected, and the ticket records at hardening whether FINALIZE fails closed or is verified against the mock harness.
  - Every remaining opencode gap in the manifest has a disposition and none is marked implemented without a cited prompt reference.
  - Tests use the existing opencode mocks only and all repo quality gates pass with coverage floors unchanged.

### A4 — Repo review engine on codex review lanes

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** A1, S1
- **Context:** RFC 0017 lets a repo name its own review skill as the review engine (`review.engine`), but only on claude-native review lanes, so a codex lane on an engine-configured client keeps the built-in roster and `cw doctor` warns `review-engine/<client>/<lane>`. Under D-8, codex runs the engine itself: one `codex exec` per declared role plus one aggregation turn that writes the cw-owned engine summary, with no Claude call. The engine manifest, a new file in the skill directory, is a public contract between cw and a repo, so harden this ticket before dispatch.
- **Scope:** D-5, D-7, D-8
- **Acceptance:**
  - A versioned, cw-owned engine manifest in the skill directory declares the engine's supported backends, its roles (each with an agent spec file in the skill directory) and an aggregation prompt; the skill directory is the one `cw review engine materialize` copies from the default branch, and the manifest's name and format are fixed at hardening.
  - cw validates the manifest strictly, `cw review engine validate` gains a manifest check, and `docs/headless-contract.md` documents the manifest next to the engine summary schema.
  - On a client with `review.engine` set, a codex review round materializes the engine from the default branch and runs the D-13 preflight, so a diff that touches the engine or its `guarded_paths` parks as `review_engine_modified`.
  - The round then runs one `codex exec` per declared role through the existing per-role codex reviewer path, with each role's agent spec as its instructions, and one aggregation `codex exec` that writes the summary to the cw-supplied path.
  - The engine round enforces fixed caps of 8 roles, a 256 KiB manifest, 64 KiB per-role prompt, 256 KiB per-role result, and `CODEX_ENGINE_MAX_SUBPROCESS_CALLS = 9` total `codex exec` subprocess invocations (roles plus aggregation, counting every attempted invocation). The existing per-role audit-flag degrade-and-retry is disabled for engine rounds; any future retry must reserve another subprocess slot and fails closed if the shared counter cannot reserve it. Role executions have bounded concurrency of 4 and share a 15-minute round deadline. Exceeding any cap or deadline fails closed with `review_engine_limits_exceeded`.
  - The engine usage contract is `CODEX_ENGINE_USAGE_BUDGET_TOKENS = 1_000_000`: token units are `input_tokens + output_tokens` from each invocation's `turn.completed` audit event, covering every role attempt and the aggregation attempt. Before roles start, the shared counter reserves `CODEX_ENGINE_AGGREGATION_RESERVE_TOKENS = 256_000` for aggregation; each role invocation reserves `CODEX_ENGINE_CALL_RESERVE_TOKENS = 64_000` before launch and settles to `max(reported usage, CODEX_ENGINE_CALL_RESERVE_TOKENS)` afterward. A failed or timed-out invocation, or one without usable audit usage, consumes its call-reserve amount, so every attempted call counts. Aggregation settles to at least `CODEX_ENGINE_AGGREGATION_RESERVE_TOKENS`; any reservation or settlement that would exceed the budget fails closed with `review_engine_limits_exceeded`. The named constants, accounting source, reservation order, enforcement timing, and failure behavior are documented in the headless contract, and hermetic tests import those constants and cover the exact and over-budget boundaries.
  - Each role writes a deterministic JSON artifact at `roles/<role-id>.json` containing its role id, status (`COMPLETE`, `FAILED`, or `MISSING`), and result or failure reason. The aggregation input is a UTF-8 JSON envelope containing the rendered aggregation prompt and every declared role artifact in manifest order, passed on the aggregation subprocess's stdin, and is bounded by `CODEX_ENGINE_MAX_AGGREGATION_INPUT_BYTES = 3_145_728` bytes, covering eight maximum role results plus up to 1 MiB of envelope overhead. cw serializes and measures that envelope before launching aggregation and fails closed with `review_engine_limits_exceeded` when it exceeds the bound. cw rejects `COMPLETE` coverage whenever any role artifact is missing or has a non-`COMPLETE` status; a hermetic two-role test asserts both stub-role artifacts are represented in the aggregation input.
  - The round records the result through `cw review engine record`, and non-`COMPLETE` coverage or an invalid summary fails the round with the RFC 0017 named reasons.
  - An engine with no codex support in its manifest is refused on a codex lane before any reviewer runs, with a named blocker reason, and never falls back silently to the built-in roster.
  - Engine findings on a codex lane take the RFC 0017 routing (never rejected; `coverage.*` and `acceptance.*` never deferrable; fingerprint identity), and the engine-backed attestation records `engine` and `reviewer_backend: codex`.
  - The confirm-round routing (RFC 0017 I3) applies to a codex loop's engine pass as it does to a claude-native one, and the confirm round is otherwise unchanged (D-3).
  - The engine round on a codex lane makes no `claude` invocation, and a test asserts no Claude launch on that path.
  - `cw doctor` stops warning `review-engine/<client>/<lane>` for a codex lane whose engine declares codex support and warns when it does not, and `config/CONFIG_REFERENCE.md` (Review Engine) and the dispatch runbook document engines on codex.
  - An engine-configured client on an opencode review lane parks with a named reason, keeping the engine fail-closed there (D-4).
  - The manifest dispositions for the engine round, engine summary and attestation move to `implemented` for codex.
  - Hermetic tests use a stub engine with two roles to cover pass, fail, incomplete, invalid summary, a missing codex manifest and an engine-modified branch, and all repo quality gates pass with coverage floors unchanged.

### C1 — Codex PLAN executor

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** A2, A3
- **Context:** Codex blocks any non-review stage with `codex_review_only`. This ticket adds PLAN as one `codex exec` turn on the rendered plan contract, followed by cw-run gates and cw-validated sentinel emission, with the plan posted through the tracker path from A2. The sandbox must allow writing only the plan artifact (see #1767). It defines stage prose and sentinel use, so harden it before dispatch.
- **Scope:** D-1, D-2, D-6
- **Acceptance:**
  - A codex PLAN run on the fake runner produces the plan file, the plan sign-off and provenance markers, and a plan draft fingerprint for a large tier, and parks a large plan as `plan_pending_approval`.
  - Ambiguities exit as `ambiguities_pending_resolution` and not as an ad-hoc reason, and a no-op ticket exits as `no_op`.
  - The turn consumes `operator_resolutions`, `plan_approved_at` and `plan_approved_fingerprint` and the pre-flight resolutions marker as the plan contract specifies.
  - Plan decisions are recorded through `cw dev-queue record-plan-decisions`.
  - The plan comment posts through `gh` on GitHub and through A2 on a non-GitHub tracker.
  - cw emits the sentinel with `stage_reached` stage1_plan and rejects a malformed result as a blocked result.
  - Tests use the fake runner and all repo quality gates pass with coverage floors unchanged.

### C2 — Codex IMPL executor

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** A1, A2, A3
- **Context:** Supersedes #1550. Codex already edits, commits and pushes in the fix loop, so IMPL reuses that plumbing under a rendered impl contract. #2987, #2967, #2907 and #1767 are prerequisites referenced here and not absorbed. It defines stage prose, completion gates and sentinel fields, so harden it before dispatch.
- **Scope:** D-2, D-5, D-6
- **Acceptance:**
  - An impl turn runs with the fence, growth budget, commit and verified push from the fix loop and commits with the `Auto-Dev-Stage: impl-complete` trailer.
  - After the turn cw runs the empty-diff gate, the scope conformance script and the quality gates by the same scripts claude-native uses, and parks with the documented reasons on failure.
  - `queue_metadata.branch` is honored and the default branch is merged only on conflict.
  - A repair row, with or without a plan, follows the impl doc's repair entry with `review_feedback` treated as untrusted data and a stale head failing closed.
  - `recover-red-pr` accepts a codex IMPL backend, with a test.
  - The sentinel carries `commits`, `fork_point_sha` and `branch` with `stage_reached` stage2_impl, and only the impl turn gets a write-capable sandbox.
  - Tests use the fake runner and all repo quality gates pass with coverage floors unchanged.

### C3 — Codex FINALIZE executor

- **Epic:** II
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** C2, A3
- **Context:** Supersedes #2473. Merge and ship authority are not relaxed: FINALIZE claims shipped only with verified PR and CI state and uses least-privilege headless `gh` writes. The turn runs the rendered finalize contract, including the neutral `/prep-pr` and `/ship-it` blocks from A3. It defines stage prose and sentinel use, so harden it before dispatch.
- **Scope:** D-1, D-2, D-6
- **Acceptance:**
  - A codex FINALIZE run on the fake runner creates or reuses the PR, arms auto-merge through `gh` only, and emits `shipped` only after cw verifies the PR state.
  - The large-tier approval check, the clean-mode tip check and the must-fix override script run as the finalize doc specifies and park under their documented reasons.
  - A large tier without recorded approval parks as `review_pending_approval`.
  - The tracker PR-link comment posts through `gh` on GitHub and through A2 on a non-GitHub tracker.
  - cw validates the sentinel and rejects a shipped result whose PR or CI state it cannot verify.
  - Tests use the fake runner and all repo quality gates pass with coverage floors unchanged.

### C4 — Usage accounting for non-claude stage turns

- **Epic:** III
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** C2
- **Context:** Folds in #2837 and #2842. Claude cost comes from the sentinel's self-reported `cost_usd`; codex review token fields are null on codex-cli 0.164.0, fix invocations run without `--json`, and opencode and local record nothing. This ticket records usage for every codex stage turn and fix invocation and adds a visible marker when usage is unavailable.
- **Scope:** D-2, D-4
- **Acceptance:**
  - Codex stage turns and fix invocations run with machine-readable output and their token usage is recorded on the session.
  - Recorded usage accumulates into the task's total cost where a price is known, and into a token total where it is not.
  - Absent usage from a backend produces a visible unavailable marker and a warning, and a pinned live fixture records the current codex-cli behavior.
  - Opencode and local record usage where their output provides it and the unavailable marker otherwise.
  - A per-model, per-stage spend report is delivered or filed as a follow-up ticket cited in the PR.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### D1 — All-codex lane validation

- **Epic:** II
- **Wave:** 4
- **Sprint:** 5
- **Depends on:** C3, C4
- **Context:** With every stage on codex, a client can run a lane with no Claude executor, but the clean-mode confirm round still defaults to Claude. This ticket adds the doctor check and docs for all-codex lanes and an opt-in end-to-end smoke probe, folding in #2463.
- **Scope:** D-1, D-3
- **Acceptance:**
  - `cw doctor` reports a lane whose stages are all codex and warns when clean mode leaves `confirm_backend` unset, naming the Claude binary dependency and the Claude-free opt-outs.
  - `config/CONFIG_REFERENCE.md` and the dispatch runbook document the all-codex lane, the confirm opt-outs and the tracker MCP requirement.
  - An opt-in live probe covers headless create and resume and MCP loading for codex and never runs in default CI.
  - The manifest shows no fails-closed disposition for codex on any stage.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

## References

- `src/cw/executor/codex.py:88` — the REVIEW-only pre-flight gate (`codex_review_only`) every non-review codex stage hits today.
- `src/cw/executor/core.py:671` — `_spawn_fire_and_forget`, the stage-neutral spawn skeleton; `core.py:406` (and `:230`) hardcode session purpose IMPL.
- `src/cw/executor/opencode.py:110` — the repair-entry fail-closed guard; `src/cw/executor/_repair_entry.py:42` shows it keys on `review_feedback` and an adoption branch together.
- `src/cw/opencode_runner.py:455` — the PLAN prompt's tracker wording; `:485` the "when the tracker is writable" post; `:501` the plan marker only IMPL reads; `:259` the stale backend-neutral FINALIZE premise.
- `src/cw/codex_review_persist.py:225` — the non-GitHub verdict skip that logs and returns.
- `src/cw/codex_review/_context/core.py:155` — comment fetch returns nothing for a non-GitHub tracker.
- `src/cw/codex_fix_loop/commit.py:72` — the write-capable codex invocation, commit and guards C2 reuses; `push.py:60` is the verified push.
- `src/cw/codex_background.py:825` — `_run_codex_review_and_complete`, the review-shaped seam S2 generalizes.
- `src/cw/review_convergence/_backend.py:43` — `resolve_confirm_backend`; an unset confirm backend resolves to Claude for a codex loop.
- `src/cw/dev_queue/red_pr_recovery.py:69` — `red_pr_refusal`, which refuses recovery on a non-claude-native IMPL backend.
- `src/cw/auto_dev_result/schema/_result.py:97` — `AutoDevResult`, the sentinel every backend must emit.
- `src/cw/cli/post_comment.py:248` — `cw post-comment` refuses a non-GitHub tracker without `--emit-only`.
- `docs/adr/0013-agent-delegated-ticket-work.md:7` — cw keeps one GitHub-only programmatic tracker client.
- `docs/rfcs/0014-codex-runs-as-a-detached-job.md:42` — D-3 reserves the codex impl stage as an addition, not a redesign.
- `config/CONFIG_REFERENCE.md:66` — the `review` row: confirm backends and the default-on egress rules.
- `.claude/commands/auto-dev-impl.md:56` — the repair entry the codex IMPL must honor.

## Handoff Brief

**Authored:** 2026-11-08 at d0ffbd4e
**Exit:** 1 sprint block

### Decisions

- **D-1 — Trackers other than GitHub through the backend's own MCP.** Rationale: ADR-0013 forbids a cw tracker client, and the MCP is already expected to be installed and authed. Reverses at: A2 hardening.
- **D-2 — REVIEW Python-driven, other stages one agent turn plus shared gate scripts.** Rationale: the gate scripts become the shared source for gates and a Python reimplementation would be a third contract copy. Reverses at: S2 hardening.
- **D-3 — Confirm round unchanged, Claude-free opt-out documented.** Rationale: the knob exists today. Reverses at: never.
- **D-4 — Opencode gets guards and drift tests only.** Rationale: low priority and no live environment to test against. Reverses at: a future RFC.
- **D-5 — Codex REVIEW parity covers stage-completeness and the repo review engine.** Rationale: RFC 0017 landed in v1.91.0, the original reversal point, so the engine round joins codex review parity. Built-in adjudication, roster and egress stay with #2464. Reverses at: never (amended 2026-11-09).
- **D-6 — Manifest first, then rendering from tagged blocks.** Rationale: cheap loud drift detection now, a single source for new prompts. Reverses at: S1 hardening if tagging proves unworkable.
- **D-7 — RFC 0017 respected: codex deferral lifted, opencode deferral kept.** Rationale: codex is the backend the operator leans on at work, where the team review process is a custom review skill, and opencode has no live environment (D-4). Reverses at: never.
- **D-8 — Codex runs the engine itself, from a cw-owned engine manifest.** Rationale: running the engine round through `claude -p` would spend Claude quota and egress on every codex review round, which defeats leaning on codex. A per-engine backend choice doubles the surface for no current need. Reverses at: A4 hardening.

### Rejected approaches

- **A shared Python contract module that generates the docs** — reverses "the command docs are authoritative" and means rewriting 6,730 lines of prose.
- **A cw-side Linear API client** — reverses Accepted ADR-0013, puts credentials in cw and splits behavior from the Claude MCP path. It remains the fallback if D-1 fails.
- **Wrapping a community Linear CLI** — there is no official Linear CLI, and it adds a third-party dependency to install and authenticate everywhere.
- **Python reimplementation of codex PLAN, IMPL and FINALIZE** — a third copy of the contract that drifts.
- **Full codex REVIEW parity in this RFC** — overlaps #2464's built-in roster and adjudication work, and would likely be redone. The engine round alone is in scope (A4).
- **The engine round through `claude -p` on a codex lane** — the most faithful option, needing no engine change, but it spends Claude quota and egress on every codex review round (operator, 2026-11-09).
- **A per-engine backend choice with two runners** — the most flexible option, but it doubles the runners, adds a config key and needs both paths tested, for no current need.
- **No review changes at all** — FINALIZE and IMPL need the sentinel fields and post-fix gates.
- **Live opencode parity** — no test environment and low priority.

### Evidence

- `src/cw/executor/codex.py:88` — codex is REVIEW-only, which sets the scope of Epic II.
- `src/cw/codex_review_persist.py:225` — the verdict is skipped on non-GitHub trackers with only a log line.
- `src/cw/codex_review/_context/core.py:155` — operator comments are never fetched off GitHub.
- `src/cw/opencode_runner.py:501` — the IMPL prompt reads a marker the PLAN prompt never writes.
- `src/cw/review_convergence/_backend.py:60` — an unset confirm backend gives a codex lane a Claude dependency.
- `docs/adr/0013-agent-delegated-ticket-work.md:7` — the invariant D-1 is built to respect.
- Line cites for the codex fix loop, executor core and sentinel schema come from recon and are re-verified at hardening.

### Do not re-adjudicate

- **Cut from scope:** adjudication of built-in findings and the built-in roster and egress policy for codex review (#2464); the engine on opencode (it stays fail-closed); live opencode parity; a new independent confirm family; a cw Linear client.
- **Deferred:** the engine manifest's file name, format and version scheme → resolved at A4 hardening.
- **Deferred:** whether a narrow daemon-side tracker client is needed → resolved by the A2 hardening and its probe, before C1 is dispatched.
- **Deferred:** how Linear markdown treats hidden comment markers → resolved by A2's recorded probe.
- **Exit choice:** Exit 1 because the change touches stage contracts, sentinel use and blocker reasons across several waves.

### Orchestrator brief

- **Drives:** `sprint-buildout` on this RFC, then `/harden-ticket` on S1, S3, A1, A3, A4, C1, C2 and C3 before any of them is dispatched.
- **Gates:** the `sprint-buildout` Approve/Abort gate; no dispatch without the operator's approval; the A2 probe result before C1 and C2 are dispatched.
- **Terminal state:** the RFC merged, the sprint block filed, and #1550, #2473, #2983, #2837, #2842 and #2463 marked as folded in or superseded; no wave dispatched.

## Issues

Issues: Epic I #3049 · Epic II #3050 · Epic III #3051 · Epic IV #3052 · S1 #3053 · S2 #3054 · S3 #3055 · A1 #3056 · A2 #3057 · A3 #3058 · B1 #3059 · A4 #3216 · C1 #3060 · C2 #3061 · C3 #3062 · C4 #3063 · D1 #3064

Milestone: https://github.com/mattwwarren/claude-workspace/milestone/24

Pulled in: #2701 (S3 absorbs its loud-failure slice only; the cw-side constraint store stays open there). Folded or superseded: #1550 (C2), #2473 (C3), #2983 (B1), #2837 and #2842 (C4), #2463 (D1); #2464 is rescoped to the adjudication, roster and egress work this RFC leaves out. Sequencing notes: #2390 and S2 both edit `codex_background.py`; #3027 and #2424 may conflict with C2.
