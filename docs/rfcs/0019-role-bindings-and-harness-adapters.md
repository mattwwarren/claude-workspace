# RFC 0019 — Role bindings and harness adapters: cw as a harness of harnesses

## Summary

ADR-0013 says portable ticket work is agent work. cw Python keeps one GitHub-only client for the few actions that run with no agent present. The code no longer holds that line cleanly. Python still renders and posts prose to trackers on its own. It launches a `codex exec` turn only to post a comment on Linear, and it refuses or skips the rest. Thirty-two `src/cw` modules branch on which tracker a client uses, and forty-seven branch on which backend runs a stage. The prompts tell each harness how to reach the tracker in that harness's own terms.

This RFC changes the unit of reasoning from *system names* to *roles*. A client binds four roles (`tickets`, `knowledge`, `code_host`, `chat`) to an MCP server or to CLI commands. One rendered bindings block is injected into every harness at spawn, and stage docs speak only of roles (Epic I). The daemon stops posting prose. It writes an outbox entry, and a courier drains it: the tracker-IO turn of today, made harness-agnostic (Epic II). A harness becomes an adapter that declares its capabilities, and one generic preflight replaces the per-backend gates (Epic III). Finally no stage turn after intake needs the tracker: intake writes the ticket snapshot that cw already keeps in `.cw/context.json`, extended, and the courier refreshes it each review pass, so later stages run on harnesses without that MCP (Epic IV).

Several things stay in code: the queue, locks, worktrees, supervision, sentinel validation, the merge gate, CI evidence, review convergence and cost. `code_host` stays GitHub-only until a second host exists.

The brief this RFC was written from makes three claims. The code contradicts parts of each, and the RFC says so where it applies rather than bending the design to fit: see [Where the code contradicts the brief](#where-the-code-contradicts-the-brief). The most consequential one is that **RFC 0018 Epic II is not in flight; it has fully landed**, so the conflict section is about what to re-home, not what to stop.

## Motivation

All counts below are from `main` at `10da423d9` (v1.103.0).

### 1. The daemon writes prose to trackers with no agent present

cw Python composes and posts comment bodies from seven sites outside `cw/gh/`. Five run with no agent present and are what Epic II moves. The last two (review-debt filing and sprint buildout) run inside an agent turn or an operator command, and stay where they are (see Epic II):

- `src/cw/codex_review_persist.py:217-399`: the codex review verdict comment. On GitHub it goes through `gh`; on any other tracker through one `tracker_io` turn (`_post_via_tracker_io`, `:351`).
- `src/cw/codex_stage_post.py` (234 lines): the codex PLAN plan/park comments and the codex FINALIZE PR-link comment, through the same split (`gh` or a `tracker_io` turn).
- `src/cw/cli/dev_queue/approve.py:115-205` (`approve --post-marker`) and `src/cw/cli/dev_queue/resolve.py:170-225` (`resolve --post`). Both go through `cli/dev_queue/_tracker_post.py` (88 lines), which **refuses** on a known non-GitHub tracker with an echo and points at the dev-queue row as the record. Neither uses `tracker_io`.
- `src/cw/reconcile/gate_recipes.py:540-612`: the auto-approve and auto-adopt audit comments, posted from the reconcile tick through `post_issue_comment`. These have **no tracker guard** (`reconcile/gate_recipe_comments.py` has none either). On a Linear client the post is a `gh issue comment <PREFIX-NNN>` that can only fail, and it is logged best-effort.
- `src/cw/review_debt_filing/_file.py:320`, `_link.py:82`: review-debt issues and link comments, through `gh` on GitHub; on Linear the worker files through MCP and cw verifies (`_verify.py`).
- `src/cw/sprint.py:690,746`: sprint buildout issues and comments through `gh` (operator- or skill-invoked, GitHub repos only).

The non-GitHub workarounds are: `src/cw/tracker_io/` (867 lines, a `codex exec` turn whose only tools are the tracker's comment create and list); `cw post-comment --emit-only` (`src/cw/cli/post_comment.py`, 358 lines), which writes part files for an agent to post; and refusal echoes in `_tracker_post.py`.

### 2. Code knows system names, not roles

- `src/cw/tracker.py:28-66` defines `TRACKER_GITHUB_ISSUES`, `TRACKER_LINEAR`, `TrackerClass {GITHUB, OTHER, UNSET}` and `tracker_kind()`. Its comment at `:27-28` says the GitHub constant is used "at spawn/session chokepoints to decide whether to withhold Linear MCP tools"; no module under `spawn*`/`session*` imports it today, so the comment is stale.
- **32 modules** in `src/cw` match `TrackerClass.`, `TRACKER_GITHUB_ISSUES`, `TRACKER_LINEAR`, `tracker_kind(`, `resolve_tracker(` or a literal `== "github-issues"`/`"linear"`. 20 of them are outside the `codex*`/`opencode*` modules. Method: `grep -rlE` over `src/cw`; the list is in [References](#references).
- **47 modules** match a backend constant (`CODEX_BACKEND`, `OPENCODE_BACKEND`, `CLAUDE_NATIVE_BACKEND`, `LOCAL_BACKEND`) or a literal `backend ==`/`!=`/`in`/`not in` comparison, in `src/cw` outside `models/`. 27 of them have no path component starting with `codex` or `opencode`; that list is in [References](#references). Method: a regex over every `*.py` under `src/cw`, `models/` excluded. The modules that are backend-specific by name are claim 3.
- `src/cw/dispatch/tracker_mcp_gate.py` (407 lines) has one branch per backend and, for codex, one per stage. Claude-native reads `enabledPlugins` on the ticket branch (`_evaluate_branch`), and opencode reads `servers["opencode"]` through `backend_mcp_config`. Codex PLAN and FINALIZE get a stricter check (`_evaluate_codex_plan`, where an unset server is itself a hit). Codex REVIEW and IMPL are not gated, and FINALIZE is gated only on codex. The gate is **per-client opt-in and off by default** (`TrackerMcpGateConfig.enabled = False`, `models/client.py:40-89`).
- The tool names are hardcoded per tracker per stage. `codex_plan/_argv.py:50` has `PLAN_TRACKER_READ_TOOLS = {TRACKER_LINEAR: ("get_issue", "list_comments")}`, and `tracker_io/_argv.py:77` has `comment_tools_for(tracker)`.
- Tracker identity and tracker reachability come from two files. `tracking.primary.system` lives in the repo's `.claude/project-config.yaml`; `tracker_mcp_gate.plugin_id`/`servers` live in the machine's `clients.yaml`. Nothing checks that they agree.
- The stage docs are written "in Linear terms for historical reasons" (`auto-dev-intake.md:20`), with a 47-line operation-mapping section (`:17-63`) that each harness must apply. Across the `auto-dev*`, `prep-pr` and `ship-it` docs 106 lines mention `linear` (case-insensitive) and 33 lines mention `gh issue`; 37 of the `linear` lines are in `auto-dev.md`.

### 3. Each harness has its own driver

- About 33,900 lines live in modules with `codex` in their path. The largest are `codex_review/` (9,306), `codex_fix_loop/` (8,188), `codex_finalize/` (3,181), `codex_impl/` (2,164) and `codex_plan/` (2,010). Opencode has `opencode_runner.py` (960), `opencode_clean_review.py` (392), `opencode_tracker_prompt.py` (86) and `executor/opencode.py` (261).
- The opencode PLAN/IMPL/REVIEW prompts are still hand-written, under the RFC 0018 D-4 manifest guard. The codex stage prompts are **not** hand-forked: they render from tagged doc blocks through `contract_manifest/_render.py` (RFC 0018 A3).
- A large share of the codex packages is not codex-specific. These modules make zero runner or argv calls: `codex_finalize/_gates.py` (543), `_ci_evidence.py` (428), `_merge_gate.py` (301), `_pr.py` (290), `_arm.py` (222), `codex_impl/_gates.py` (535), `codex_plan/_gates.py` (140), `codex_fix_loop/fence.py` (422), `growth.py` (328) and `push.py` (91). Together that is about 3,300 lines of post-turn verification that any non-Claude harness needs. It lives under the codex name because codex was the first to need it.
- A seam already exists: the `StageExecutor` Protocol (`executor/core.py:147-192`, RFC 0005 A2) with `spawn()` and `stage_sentinel_schema()`. It declares nothing about what a harness can reach. So every capability question is answered by a backend branch somewhere else: `executor/_engine_guard.py`, `_finalize_guard.py`, `_repair_entry.py`, `_clean_review.py`, `dispatch/claim/codex_capability.py`, `doctor/tracker_mcp.py` and `doctor/all_codex_lane.py`.

## Where the code contradicts the brief

- **RFC 0018 Epic II has landed in full.** S2, A1, A4, C1, C2, C3, C4 and D1 (#3054, #3056, #3216, #3060–#3064) all closed COMPLETED, by 2026-11-12. The epic issues #3049–#3052 are still OPEN. Codex runs all four stages (`CODEX_SUPPORTED_STAGES`, `executor/codex.py:47`). Nothing is left to stop; the question is what to re-home.
- **The codex stage prompts are rendered, not forked.** Only opencode's are hand-written, and RFC 0018 D-4 deliberately keeps them that way.
- **`tracker_io` is not the path for `approve --post-marker` or `resolve --post`.** RFC 0018 D-1 named them, but A2 shipped only the verdict comment, and #3062 later added codex PLAN/FINALIZE posts. Both CLI paths refuse on a non-GitHub tracker. That costs nothing in correctness, because each records the decision on the row (`plan_approved_at`, `operator_resolutions`), and that row is what the plan stage reads (`context_schema.py` v7, v12).
- **The brief missed one daemon prose site:** the gate-recipe audit comments (`reconcile/gate_recipes.py:540-612`). It posts through `gh` on every tracker, with no guard.
- **On a host where Linear comes from a codex plugin app, `tracker_io` cannot run at all** (operator probe, codex 0.167.2, 2026-11-17). Such a host reaches Linear through the built-in `codex_apps` server, with tools named `linear_get_issue`, `linear_list_comments` and `linear_save_comment`; there is no `create_comment`. `tracker_io/_argv.py` addresses only `-c mcp_servers.<server>.enabled_tools=[...]`, and against `codex_apps` codex exits 1 with `Error loading config.toml: invalid transport in mcp_servers.<name>`. Plugin apps are configured under `apps.<app_id>.*` instead. The allowlist `TRACKER_COMMENT_TOOLS` (`_argv.py:46`, `("create_comment", "list_comments")`) uses the wrong names for this host too. So RFC 0018 A2's verdict mirror does not deliver on that host today. The bindings model (D-A3) has to carry the real tool names and the way each harness addresses them, rather than a constant per tracker. See D-B5; the live breakage is tracked on its own as #3695.
- **`tracker_mcp_gate` is opt-in and off by default**, so "a branch per backend per stage" describes the code shape, not production behavior. On a client that has not opted in, no backend is gated at all.
- **`models/ticket_view.py` is the wrong home for the ticket snapshot, and a snapshot already exists.** `ticket_view.py` is the output-only `cw dev-queue show` view: `extra="forbid"`, "never persisted", built from the row rather than stored on it. Intake Step 0d (`auto-dev-intake.md`) already writes `.cw/context.json` with `ticket_title`, `ticket_body` and `comments: [{author, created_at, body}]`, a reader contract (#2700) that `_load_ticket_context` and `local_runner.build_task_message` read and `tests/test_auto_dev_intake_context_schema.py` pins. Epic IV extends that artifact instead of adding one. See D-D1.
- **The `TrackerClass` docstring says it is "for new code that must tell not-GitHub apart from unknown".** In practice it is already the routing key for 32 modules, not a narrow addition.

## Design

The line this RFC draws: **cw code decides *whether* and *when*; it never decides *how to talk to a system*.** Talking to a system is a role, a binding says how the role is reached, and the only code that touches a binding is (a) the renderer that tells an agent about it and (b) the courier that drains the outbox through it.

### Epic I — Role bindings

The client config gains a `bindings` block. Each of `tickets`, `knowledge`, `code_host` and `chat` binds to one of two things. The first is `mcp: {server: <name>, tools: {<op>: <tool>}}` for each harness that reaches it, keyed by harness. The second is `cli: gh`, which names the existing GitHub client in `cw/gh/` and is valid only for a GitHub system (D-B2). A binding cannot carry an argv template: cw Python never runs a provider command a config file describes. The operations come from a closed list per role: `tickets` has `get`, `list_comments`, `comment`, `close`, `create`. A binding also names the system it fronts (`system: linear`), and doctor checks that against `tracking.primary.system`, so the two config sources can no longer disagree silently.

One renderer turns the resolved bindings into a single prompt block. The block says which roles this session can reach, how to call each operation, and which closed blocker reason to emit when a call is unavailable or fails. That block is injected at spawn for every harness: written into `cw-context.json` and inlined into rendered codex/opencode prompts. This is `opencode_tracker_prompt.py` generalized: the same shape (GitHub line, MCP line, failure paragraph, unset line), driven by config instead of `TrackerClass`. The stage docs drop the "written in Linear terms" mapping and say "post to the `tickets` role". The contract-manifest ids stay stable.

`TrackerMcpGateConfig.plugin_id`/`servers` fold into `bindings.tickets.mcp`, and its `enabled` flag and `settings_path` move to `bindings.tickets.preflight` with the same defaults (D-C3). The codex PLAN read allowlist and the `tracker_io` comment allowlist become lookups into the binding, not per-tracker constants.

This is ADR-0008's descriptor idea, applied at the agent edge rather than to cw's deterministic queries. ADR-0008 is still `Proposed`, and its registry was never built (`tracker.py` is 86 lines with no descriptor table). This RFC does not revive ADR-0008's daemon-side contract. A binding's operation mappings are read only by the renderer and the courier. Dispatch logic, through the Epic III preflight, reads one fact about a binding: whether a given harness can reach it. It never interprets an operation, so there is no daemon-side descriptor interpreter.

### Epic II — The outbox: the daemon never posts prose

Every prose write cw makes with no agent present becomes an outbox entry. An entry is one body file plus a JSON record: target role, ticket, idempotency marker, producer, the redaction already applied, and parts already split. Entries live in a per-client store under the cw state dir, with append and claim under their own leaf lock (ADR-0019 rank order), never under `sessions_lock`.

A courier drains entries. The courier is `tracker_io` made harness-agnostic: it plans parts, hands them to a drain, verifies the report, and marks the entry `delivered`, `unverified` or `failed`, each with a closed reason. Two drains exist:

- **CLI binding** (`cli: gh`, GitHub only): the courier calls `cw.gh.post_issue_comment`, the same client that posts today, so posting cost and latency on GitHub do not change. This *is* still cw Python running a post. It stays sanctioned because ADR-0013 keeps a GitHub-only programmatic client and the drain adds no caller outside `cw/gh/`; D-B2 records it.
- **MCP binding** (Linear today): one agent turn launched through the Epic III adapter on any harness that declares the `tickets` binding reachable. Today that means only `codex exec`. The turn's only tools are the binding's `comment` and `list_comments` where the harness can narrow that far (D-B5); it posts, reads back, and reports ids. The courier also runs read-only refresh turns for Epic IV. Both kinds of turn go beyond what ADR-0013's 2026-11-10 update sanctions (comment tools only), so O2 files ADR-0023, "Courier turns", which supersedes that update.

Six producers switch to `outbox.enqueue(...)`: the codex verdict, `codex_stage_post`, `approve --post-marker`, `resolve --post`, the gate-recipe audit comments, and `cw review settle`, which today prints a `## Review Finding Dispositions` marker for someone else to post and on Linear is never posted. Review-debt filing (`review_debt_filing/`) and sprint buildout (`sprint.py`) are excluded: the first runs inside a worker turn that holds the binding and is verified by `_verify.py`, the second is an operator command on a GitHub repo. Neither runs with no agent present. The `_tracker_post.py` refusals go away: on Linear these posts are queued and delivered rather than refused. Dedup uses the marker: before posting, the drain lists comments and skips an entry whose marker is already present. That replaces `approve.py`'s private dedup.

A drain that cannot run, because no reachable harness holds the binding, leaves the entry pending and emits an operator-visible event. It never holds a row. That keeps the posts best-effort, as they are today.

The courier never runs inside `reconcile()` or under any lock. The dispatch tick launches `cw outbox drain --client <c>` as a detached process when pending entries exist and no drain is live for that client, with at most one drain per client and a per-run cap on entries (D-B7). That keeps ARCHITECTURE §7.15's rule that the tick path makes no unbounded serial external calls.

### Epic III — Harness = adapter plus declared capabilities

The adapter contract is one sentence: **run one turn with this prompt in this worktree under this sandbox, and return a sentinel (or a raw report for a courier turn).** Each harness declares, as data:

- the stages it serves;
- whether it has skills and subagents (only claude-native today);
- which bindings it can reach, and how (claude-native: `enabledPlugins` on the branch; codex/opencode: the backend MCP config `backend_mcp_config.py` already reads);
- its sandbox modes;
- whether it supports clean-mode review convergence (today `executor/_clean_review.py`);
- how finely it can narrow a binding's tools (D-B5);
- its usage source.

`StageExecutor` gains a `capabilities()` method, and the existing four executors implement it.

One generic preflight then answers every "can this row spawn here?" question from those declarations plus the stage's needs. The stage's needs are which bindings it touches, from Epic IV, and whether it needs skills or a write sandbox. The preflight replaces the per-backend branches in `tracker_mcp_gate.py`, `codex_capability.py`, `executor/_engine_guard.py`, `_finalize_guard.py`, `_repair_entry.py` and `_clean_review.py`, and the doctor checks `doctor/tracker_mcp.py` and `doctor/all_codex_lane.py`. A missing capability parks with one closed reason that names the capability, not the backend. The binding-reachability half of the preflight runs only for a client whose `bindings.tickets.preflight.enabled` is true, which keeps today's opt-in (D-C3). Stage support, clean-mode support and sandbox checks run for every client, as the guards they replace do today.

The harness-neutral post-turn verification moves out of the `codex_*` namespace into a stage-verification package, with no behavior change: the stage gates, CI evidence, merge gate, PR create/reuse, arming, fence, growth budget and verified push. What remains under `codex_*` is the adapter itself: argv building, the runner, the profile, the per-role reviewer launch, JSON event parsing and usage parsing.

### Epic IV — Capability routing: no stage turn after intake needs the tracker

Only three kinds of work need the `tickets` role live: **intake** (fetch the ticket and its comments), the **per-pass refresh** of the snapshot, and **outbox drain**. Intake is a stage turn: claude-native `auto-dev-intake`, or codex PLAN, which is the codex lane's intake and keeps its read tools. Every stage after intake runs without the binding. Today they reach the tracker too, because each one re-reads ticket text. These are the production read sites after intake:

- `codex_review/_context/core.py:231,280,317,711` (`_load_operator_comments`, `_load_voided_findings`, `_load_finding_dispositions`, the pass context), all through `_fetch_ticket_comments` (`:148-172`).
- `codex_fix_loop/_driver.py:451` (`_load_comment_constraints`, also through `_fetch_ticket_comments`).
- `codex_impl/_run.py:329` and `codex_finalize/_run.py:319` (`prefetch_ticket`).
- `local_runner.py:229`, `dev_queue/requeue.py:192`, `dev_queue/lifecycle.py:859` and `reconcile/gate_recipes.py:333` (`fetch_approved_plan_comment`, approved-plan recovery).
- `codex_finalize/_post.py:48`, `cli/dev_queue/approve.py:163` and `tracker_post.py:357` (comment listing for post dedup). These go away with Epic II, because the courier dedups on the marker.

All of these read through one seam after T2: `ticket_snapshot.read(worktree)`, backed by `.cw/context.json`. The approval-time body checks (`adopt_pr.py:405`, `cli/body_sha.py:89`, `dev_queue/approval.py:569`) are out of scope. They compare a live body against a recorded digest to detect drift, so they must read live, and they stay GitHub-only Python under ADR-0013.

Intake already writes `.cw/context.json` with `ticket_title`, `ticket_body` and `comments: [{author, created_at, body}]` (Step 0d, reader contract #2700). T1 extends it, under a new context schema version and with every existing key kept: `ticket_url`, `snapshot_fetched_at`, a content digest, and for each comment an `author_id`, a stable comment `id` and the agent-authored marker flag. One `cw ticket snapshot` command writes it, so claude-native intake and codex PLAN stop hand-writing the file. The row carries only `snapshot_digest` and `snapshot_fetched_at` (D-D1). Later stages read the snapshot and need no tracker binding, so the Epic III preflight lets them run on a harness or host without that MCP.

Freshness is the risk, and D-D2 keeps it. Today an operator can steer a review mid-flight by commenting on the ticket, because codex REVIEW re-reads comments each pass and feeds operator comments in as binding inputs (#1730), but only on GitHub: off GitHub the fetch returns nothing. The snapshot is refreshed at every review pass through the binding: in Python for `gh`, exactly as today, and by a read-only courier turn for an MCP binding. So GitHub steering is unchanged and Linear gains it. `cw dev-queue resolve` (row `operator_resolutions`) stays the channel that works with no binding at all, and the context records `snapshot_fetched_at` so a stage can tell how old its view is.

## Trust cost

Agent posts are self-attested, and this RFC makes more of them, since every Linear post becomes an agent turn. What the checks prove today:

- `review_debt_filing/_verify.py` proves the **shape** of reported ticket ids: the prefix is one the client owns in `linear_prefix_map`, there is one id per emitted key, and each URL contains its id. Its docstring says so outright: "It cannot prove the tickets exist."
- `tracker_io/_verify.py` checks the reported ticket, the id count, order, format, uniqueness and URL, plus the first and last 200 characters (`READBACK_WINDOW`) of each stored comment against what cw emitted. Its docstring: "the ids and read-back windows are model-echoed, not proof of delivery. The middle of a part longer than two windows is not verified at all." Whether the marker survives is recorded, not enforced. An operator probe on 2026-11-17 found that a hidden `<!-- cw-probe:... -->` line survived in the stored Linear body, confirmed by a second, read-only list. That evidence is the codex turn's own verbatim report, not a captured raw MCP response, and the fixture (`tests/fixtures/tracker_io/linear_marker_probe.json`) is still not committed. Marker survival on Linear is therefore **likely but not pinned**.

What read-back **can** prove, given the agent is the only reader: that the agent's report agrees with what cw asked it to post. So a confused agent that posts to the wrong ticket, truncates a body, or reports a stale id fails visibly. A model that echoes the windows it was given without posting cannot be told apart from one that posted.

What read-back **cannot** prove: delivery, persistence, the middle of a long part, or that the comment is visible to anyone else. Only a reader that is not the poster can prove those.

Mitigations, in increasing cost:

1. **Cross-turn read-back.** A *different* later turn that holds the binding, namely the next courier run, lists comments and confirms the outbox markers it expects. That moves the proof from "the poster says so" to "a second, independent session saw it". It still trusts the tracker MCP, but no longer trusts the same model run. D-B3 adopts it for `unverified` entries.
2. **Treat tracker posts as non-authoritative.** Each post this RFC moves is already a mirror of row state (`plan_approved_at`, `operator_resolutions`, review verdict artifacts, gate events). The row and the event log stay the record, and the outbox entry's status is reported, never gated on. This RFC keeps that invariant (D-B1).
3. **Before/after snapshot where the tool set cannot be narrowed (D-B5).** A courier turn that runs with the whole tracker app enabled could change more than one comment. A read-only harness turn would list comments and read the issue before and after, and accept only a delta of exactly one new comment carrying the entry's marker, with the issue's state, assignee and labels unchanged. That catches an extra comment or a status flip. It does not catch edits to other issues, and it relies on the same model's reads.
4. **Author ids on MCP reads (D-D3).** On Linear a comment's author reaches cw only as an id the courier turn reports, so it is model-echoed, like the post ids above. D-D3 lets that id make a comment binding (constraints, voided findings, settle dispositions, the approved plan), which is a trust widening the operator chose on 2026-11-17. Two checks bound it. The comment must carry no `cw-agent-authored` marker. And the same comment `id` reported with a different `author_id` on a later refresh is a contradiction: it emits an event and the comment drops to untrusted context for the rest of the row. Neither check proves authorship. A model that misreports an author consistently is not caught, and the content-level gates (the destructive-directive gate, the fence) are the backstop. On GitHub the author comes from `gh` in Python and nothing changes.
5. **A narrow daemon-side read client** for the non-GitHub tracker, as the RFC 0018 D-1 fallback. It is out of scope here and would need a new ADR superseding ADR-0013.

## Conflict with RFC 0018 Epic II

**Already landed.** S2 (stage-neutral scaffolding, `codex_stage_completion.py`, `codex_stage_facts.py`), A1, A4, and C1–C3 (`codex_plan/`, `codex_impl/`, `codex_finalize/`), C4 (`codex_usage.py`) and D1 (`doctor/all_codex_lane.py`). Epic I's manifest and renderer (`contract_manifest/`, 1,882 lines; `src/cw/data/contract_manifest.yaml`) and Epic III's `tracker_io` and loud-failure floor have landed too.

**What should stop:** nothing is in flight. What should stop is the *pattern* RFC 0018 used to reach parity: add a backend-and-stage-specific gate branch (`_evaluate_codex_plan`), a per-tracker tool table (`PLAN_TRACKER_READ_TOOLS`) or a per-backend guard module for each new pair. From this RFC's acceptance on, a new harness or tracker is added only by declaring capabilities and a binding. A PR that adds a new `backend ==` or `TrackerClass.` branch outside the adapter and the courier gets challenged on sight, and Epic III adds a guard test for it.

**What survives as an adapter:**

- **Codex adapter (stays codex-specific):** `codex_runner.py`, `codex_driver.py`, `codex_lane_config.py`, the `_argv.py` and `_prompt.py` modules of each stage package, `codex_review/_profile.py`, `_roles.py`, `_shard_run.py`, `codex_usage.py`, and `reconcile/codex_boot.py` (liveness recovery of the detached job, ADR-0018).
- **Moves to stage verification (harness-neutral, Epic III ticket H3):** the gate, CI-evidence, merge-gate, PR, arm, fence, growth and push modules listed in Motivation §3, about 3,300 lines.
- **Stays as review convergence (cw-owned, out of scope here):** the rest of `codex_review/` (verdict synthesis, rendering, adjudication) and `codex_fix_loop/` (rounds, divergence, constraints, park). These are the Python review loop that RFC 0018 D-2 kept on purpose. Whether claude-native REVIEW should also run on this loop is an open question, not a decision this RFC makes.
- **Becomes the courier (Epic II):** `tracker_io/`. Only its `_argv.py` launch moves behind the adapter.
- **Replaced by the generic preflight (Epic III):** the codex branches of `tracker_mcp_gate.py`, `codex_capability.py`, and the opencode and codex guards in `executor/`.

**RFC 0018 D-4 and its opencode epic (#3052, B1 #3059).** D-4 limits opencode to drift tests and fail-closed guards, with no live parity work. This RFC changes two things for opencode and keeps the rest. R2 replaces the hand-written tracker fragment in its prompts with the rendered bindings block, carrying the same blocker reasons. H1 and H2 turn B1's fail-closed guards into capability declarations that park on the same inputs. No opencode stage gains behavior, and its Linear dogfood stays best effort (see Dogfood plan). D-4's "no live parity work" otherwise stands.

## File-level impact

Line counts are current file sizes, or the span of the named function where a file only shrinks. "Replaces" means a new module takes over the behavior; "shrinks" means the named lines go and the file stays.

**Epic I (role bindings):**

- `src/cw/opencode_tracker_prompt.py` (86): replaced by the bindings renderer.
- `src/cw/models/client.py:40-89` (`TrackerMcpGateConfig`, 50): replaced by the bindings model. `ClientConfig.tracker_mcp_server` (`:597-602`) is deleted.
- `src/cw/codex_plan/_argv.py:48-66` (`PLAN_TRACKER_READ_TOOLS`, `read_tools_for`, about 19 of 131): shrinks to a binding lookup.
- `src/cw/tracker_io/_argv.py:77-85` (`comment_tools_for`, plus the `TRACKER_COMMENT_TOOLS` table): shrinks to a binding lookup.
- `.claude/commands/auto-dev-intake.md:17-63` (47): replaced by a reference to the rendered bindings block. About 106 `linear` and 33 `gh issue` mentions across the stage docs are rewritten to role terms.
- `src/cw/tracker.py:35-66` (`TrackerClass`, `TrackerKind`, `tracker_kind`, 32 of 86): deleted by X1. `resolve_tracker` stays as the one reader of tracker identity.

**Epic II (outbox):**

- `src/cw/cli/dev_queue/_tracker_post.py` (88): deleted.
- `src/cw/cli/dev_queue/approve.py:115-205` (91) and `resolve.py:170-225` (56): shrink to enqueue calls, about 15 lines each.
- `src/cw/codex_stage_post.py` (234): shrinks to an enqueue, about 40 lines.
- `src/cw/codex_review_persist.py:217-399` (183 of 399): shrinks to an enqueue.
- `src/cw/reconcile/gate_recipes.py:540-612` (73): shrinks to enqueues. The template formatting stays.
- `src/cw/cli/review/commands.py` (`settle`): enqueues its disposition marker instead of printing it for relay.
- `src/cw/tracker_io/` (867): becomes the courier. `_turn.py` (273) loses its codex launch to the adapter; `_verify.py` (251) and `_prompt.py` (134) stay.
- `src/cw/tracker_post.py` (397): `post_parts` becomes the CLI-binding drain; splitting and redaction stay.
- `src/cw/cli/post_comment.py` (358): `--emit-only` becomes `outbox enqueue` for agent-authored bodies, and its eight-key result is kept.
- New: the outbox store and courier entry point, about 400 lines estimated.

**Epic III (harness adapters):**

- `src/cw/dispatch/tracker_mcp_gate.py` (407): replaced by the generic preflight. The branch-settings reader (`_read_branch_json`, `_plugin_enabled`, about 90 lines) moves into the claude-native capability probe.
- `src/cw/dispatch/claim/codex_capability.py` (260): shrinks to the codex adapter's version probe. The park and circuit logic go generic.
- `src/cw/executor/_engine_guard.py` (46), `_finalize_guard.py` (42), `_repair_entry.py` (67) and `_clean_review.py` (76): replaced by capability declarations. The `review-convergence` doctor check reads clean-mode support from the declaration instead of `CLEAN_REVIEW_GUARDED_BACKENDS`.
- `src/cw/doctor/tracker_mcp.py` (190) and `doctor/all_codex_lane.py` (127): replaced by one capability doctor check.
- `src/cw/executor/core.py` (771): `StageExecutor` gains `capabilities()`, about 30 lines added.
- `src/cw/backend_mcp_config.py` (284): stays; it is the codex/opencode reachability probe.
- Move without behavior change, about 3,300 lines: the stage-verification modules listed in Motivation §3.

**Epic IV (capability routing):**

- `src/cw/codex_review/_context/core.py:148-172` (`_fetch_ticket_comments`): moves into the courier as the CLI-binding refresh. Its three loaders, the pass context (`:711`) and `codex_fix_loop/_driver.py:451` read the snapshot. The `review.operator_context_undelivered` signal in `codex_review_signals.py` is deleted, since the refreshed snapshot covers every tracker.
- `codex_impl/_run.py:329`, `codex_finalize/_run.py:319` (`prefetch_ticket`), and `local_runner.py:229`, `dev_queue/requeue.py:192`, `dev_queue/lifecycle.py:859`, `reconcile/gate_recipes.py:333` (`fetch_approved_plan_comment`): read through `ticket_snapshot.read`.
- `src/cw/codex_plan/_argv.py` read-only MCP plumbing (`_mcp_argv`): stays. Codex PLAN is the codex lane's intake turn.
- `.claude/commands/auto-dev-intake.md` Step 0d: the hand-written `.cw/context.json` heredoc becomes a `cw ticket snapshot` call; the #2700 key names are kept.
- `.claude/commands/auto-dev-impl-appendix.md`: the Linear `withheld (identity_unresolved)` fail-closed rule is replaced by the D-D3 author-id rule.
- `src/cw/models/tasks/ticket_task.py` (818, near the yellow flag): gains `snapshot_digest` and `snapshot_fetched_at`.
- `src/cw/context_schema.py`: gains a schema version for the extended `.cw/context.json`.
- New: `ticket_snapshot` (writer, reader seam, refresh), about 250 lines estimated.

## Phasing

Epics I and II first, as two independent tracks; Epic III only after both; Epic IV last.

- Wave 0 (sprint 1): R1 bindings model · O1 outbox store. No shared files.
- Wave 1 (sprint 2): R2 bindings renderer · O2 courier drain.
- Wave 2 (sprint 3): R3 stage docs in role terms · O3 producers onto the outbox. Epics I and II have delivered their user-visible value: Linear posts deliver, and prompts carry one bindings block. X1, the tail of Epic I, retires `TrackerClass` last, in wave 6.
- Wave 3 (sprint 4): H1 capability declarations · H3 stage-verification move.
- Wave 4 (sprint 5): H2 generic preflight.
- Wave 5 (sprint 6): T1 ticket snapshot at intake.
- Wave 6 (sprint 7): T2 snapshot-only later stages · X1 retire `TrackerClass`.

## Resolved decisions

- **D-A1 — Bindings live in `clients.yaml`; system identity stays in project config.** A binding names reachable servers and credentials, which are per machine. Which system the repo tracks in is a repo fact. Doctor fails a mismatch between `bindings.tickets.system` and `tracking.primary.system`. Reverses at R1 hardening.
- **D-A2 — The role set is closed: `tickets`, `knowledge`, `code_host`, `chat`.** Only `tickets` and `code_host` have consumers today. `knowledge` and `chat` are declared so the schema has a place for them, and are validated but unused. Adding a role is an RFC amendment.
- **D-A3 — Operations per role are closed and named by cw.** A binding maps cw's operation names to tool names or argv; it cannot add operations. This keeps ADR-0008's "budget" idea without its daemon-side interpreter.
- **D-B1 — Tracker posts are mirrors, never the record.** No row transition waits on an outbox entry, and the row and event log stay authoritative. Reverses never inside this RFC.
- **D-B2 — The CLI-binding drain is the existing GitHub client, nothing more.** A `cli` binding is valid only as `cli: gh` on a GitHub system, and R1 rejects any other. The courier calls `cw.gh.post_issue_comment`, so the drain adds no caller outside `cw/gh/` and no provider-portable I/O, and `tests/test_ticket_boundary_guard.py`'s allowlist stays `{"cw/gh/"}`. That is the ADR-0013 GitHub-only client, unchanged in reach, at zero agent cost. "The daemon never posts prose" therefore means "the daemon never composes a post outside the outbox, and never reaches a non-GitHub tracker", not "Python never runs `gh issue comment`". A dated ADR-0013 update naming the outbox is enough for the drain; the courier's agent turns need ADR-0023 (D-B8).
- **D-B3 — Unverified entries get a cross-turn read-back.** An MCP-drained entry reported `unverified` is re-checked by the next courier run, a separate turn, which lists comments for its marker before draining anything new. Reverses at O2 hardening if courier runs are too rare to matter.
- **D-B4 — Piggyback drain is out of scope.** Only dedicated courier turns drain. Folding the drain into stage turns would widen every stage's tool allowlist and add a contract element to every stage doc. It can be reconsidered later with measured courier cost.
- **D-B5 — Tool narrowing is a harness capability, not an assumption.** Each harness declares how a binding's tools can be restricted: `per_tool` (codex `mcp_servers.<name>.enabled_tools`), `per_server` (codex plugin apps: `apps.<app_id>.default_tools_enabled=false` hides every Linear tool; the operator could not get per-tool re-enable to work), or `none`. A `per_tool` courier turn keeps today's exact allowlist. A `per_server` or `none` turn disables every other server and app, runs with the binding's app enabled, and must pass the before/after snapshot check (Trust cost, mitigation 3). One unrestricted operator run posted exactly one comment, which is the expected case, not proof. Reverses at O2 hardening if per-tool app restriction is found to work.
- **D-B6 — The dispatching host drains its own entries, and an unreachable binding is a knock, not a wait.** A Linear row reached intake on this host, so some harness here held the `tickets` binding then. An entry that later cannot drain means that binding went away, usually expired auth. The courier leaves the entry pending, emits one event per entry, and files one `needs_you.py` gate record per binding (`kind: gate`, the `*_mcp_unauthenticated` reason) so the operator re-auths instead of discovering a silent backlog. The outbox is not shared across machines; a row moved to another host takes its pending entries with its row state, which is out of scope here.
- **D-B7 — The courier is a bounded detached process, never part of the tick.** The dispatch tick launches `cw outbox drain --client <c>` when pending entries exist and no drain is live for that client. A drain claims at most 20 entries per run (a config default, `outbox.max_entries_per_run`) and runs them serially; one drain per client is live at a time. The per-pass refresh (D-D2) runs inside the review driver's own job, not the tick. Reverses at O2 hardening if a measured drain needs a different cap.
- **D-B8 — Courier turns get their own ADR.** ADR-0013's 2026-11-10 update sanctions a turn whose only tools are the tracker's comment create and list. Courier turns also run read-only refreshes (`get`, `list_comments`), and on a `per_server` harness they run with the whole app enabled. O2 files ADR-0023, "Courier turns", which supersedes that update, states the three turn kinds (post, verify, refresh), the narrowing rule (D-B5) and the before/after check, and keeps ADR-0013's GitHub-only Python client otherwise intact.
- **D-C1 — Capabilities are declared data on the executor, probed where they can be.** Declarations are static; reachability is probed with the reads that exist today (`enabledPlugins`, `backend_mcp_config`). It keeps today's fail-open posture on an ambiguous read, unless the stage needs the binding to produce any artifact at all.
- **D-C3 — The binding-reachability preflight stays opt-in.** `bindings.tickets.preflight` carries `enabled` (default false) and `settings_path`, migrated from `TrackerMcpGateConfig`. A client that has not opted in gets no reachability park, exactly as today. Stage-support, clean-mode and sandbox checks are not opt-in, because the guards they replace are not. Operator decision, 2026-11-17.
- **D-C2 — The stage-verification move changes no behavior.** H3 only moves and renames code, and the existing codex tests pass unedited, apart from import paths.
- **D-D1 — The snapshot is intake's existing `.cw/context.json`, extended, and the row carries only its digest.** Intake already writes title, body and comments there under the #2700 reader contract, so a second store would be a parallel implementation. T1 adds keys under a new schema version and keeps every existing one. `models/ticket_view.py` is an output-only view and cannot hold it, and inlining comment threads into `dev_queue.json` would bloat every locked read-modify-write of the queue, so the row keeps only `snapshot_digest` and `snapshot_fetched_at`. Reverses at T1 hardening.
- **D-D2 — The snapshot is refreshed at every review pass, through the `tickets` binding, not by the stage harness.** For a CLI binding (`gh`) the refresh is the Python read that `codex_review/_context/core.py:_fetch_ticket_comments` does today, so GitHub clients keep live steering unchanged. For an MCP binding the refresh is one read-only courier turn at the pass boundary, run by whichever reachable harness holds the binding. A failed refresh runs the pass on the last snapshot and emits an event, never a park, because the row (`operator_resolutions`) stays authoritative (D-B1). The stage turn itself never needs the binding. Today a codex review on Linear reads no ticket comments at all (`_fetch_ticket_comments` returns `None` off GitHub and emits `review.operator_context_undelivered`), so this gives Linear clients live steering they lack today instead of taking it from GitHub ones. Reverses at T2 hardening if the per-pass courier turn costs too much; the fallback is refresh at stage entry only.
- **D-D3 — On an MCP binding, a comment binds when its reported author id is an operator id.** `bindings.tickets.operator_ids` lists the operator's tracker user ids. A snapshot comment whose `author_id` is in that list and which carries no `cw-agent-authored` marker is operator-authored, and the loaders treat it as they treat an operator-login comment on GitHub: constraints, voided findings, settle dispositions and the approved plan all bind. This replaces the Linear `withheld (identity_unresolved)` fail-closed rule. The author id is model-reported; Trust cost mitigation 4 states what that costs and the two checks that bound it. An empty `operator_ids` keeps today's fail-closed behavior. Operator decision, 2026-11-17. Reverses at T2 hardening only by operator decision.

## Tickets

### R1 — Role bindings config model and resolver

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** Tracker reachability is configured as `tracker_mcp_gate.plugin_id` and `servers` in `clients.yaml` while tracker identity is `tracking.primary.system` in project config, and nothing checks they agree. This ticket adds a `bindings` block with the four roles, closed per-role operations and an MCP or CLI realization per harness, and folds `TrackerMcpGateConfig` into it with a config migration. It defines a public config contract, so harden it before dispatch.
- **Scope:** D-A1, D-A2, D-A3, D-B2, D-C3, D-D3
- **Acceptance:**
  - `ClientConfig.bindings` validates the four roles, the closed operation set per role and one realization per harness, and rejects an unknown role or operation by name.
  - An existing `tracker_mcp_gate` block loads as the equivalent `bindings.tickets`: `plugin_id`/`servers` into `mcp`, and `enabled`/`settings_path` into `preflight` with their defaults kept. It loads with a deprecation warning, and `cw schema` publishes the new shape.
  - A `cli` binding is accepted only as `cli: gh` on a GitHub system, and any argv template is rejected by name (D-B2).
  - `bindings.tickets.operator_ids` validates as a list of non-empty strings and defaults to empty (D-D3).
  - `cw doctor` fails when `bindings.tickets.system` and `tracking.primary.system` disagree, and warns when either is unset.
  - `config/CONFIG_REFERENCE.md` documents the block with a GitHub CLI example and a Linear MCP example.
  - No consumer changes behavior in this ticket: `tracker_mcp_gate.py` reads the migrated values through the resolver.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### R2 — Bindings block renderer injected at spawn for every harness

- **Epic:** I
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** R1
- **Context:** `opencode_tracker_prompt.py` tells opencode how to reach the tracker from `TrackerClass`, the codex PLAN turn gets a per-tracker tool table, and claude-native reads the doc's Linear-phrased mapping. This ticket generalizes the opencode fragment into one renderer driven by the resolved bindings and injects its block into `cw-context.json` and every rendered prompt.
- **Scope:** D-A3
- **Acceptance:**
  - One renderer returns deterministic text for a set of resolved bindings and a harness, pinned by golden tests for GitHub CLI, Linear MCP and unset.
  - The block lands in `cw-context.json` under a new schema version and in the codex and opencode rendered prompts.
  - `opencode_tracker_prompt.py` is deleted, and the opencode prompts carry the rendered block with the same blocker reasons.
  - `PLAN_TRACKER_READ_TOOLS` and `comment_tools_for` resolve tool names from the binding, and a Linear binding with renamed tools reaches the launch argv in a test.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### R3 — Stage docs speak in roles

- **Epic:** I
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** R2
- **Context:** The stage docs are written in Linear terms with a 47-line operation-mapping section in `auto-dev-intake.md` that each harness must apply. This ticket rewrites tracker operations in the auto-dev docs, `/prep-pr` and `/ship-it` as role operations that point at the rendered bindings block, keeping every contract-manifest id. It changes stage prose, so harden it before dispatch.
- **Scope:** D-A2, D-A3
- **Acceptance:**
  - The intake tracker-resolution section is replaced by a pointer to the bindings block, and no stage doc names a tracker MCP tool literally.
  - Every contract-manifest id still resolves and the drift test passes without disposition changes.
  - The codex rendered prompts regenerate with golden updates whose only diff is role wording.
  - A test fails if a stage doc reintroduces an `mcp__plugin_linear` literal or a tracker-specific operation table.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### O1 — Outbox store

- **Epic:** II
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** Daemon prose posts are made inline from seven sites with three different non-GitHub behaviors (a tracker-IO turn, a refusal echo, an unguarded failing `gh` call). This ticket adds a per-client outbox of entries (body file plus a record carrying role, ticket, idempotency marker, producer and parts) with enqueue, claim and settle under a leaf lock ranked per ADR-0019. It defines a durable store and its states, so harden it before dispatch.
- **Scope:** D-B1
- **Acceptance:**
  - Enqueue writes the body and record atomically and is idempotent on the marker.
  - Claim and settle move an entry through pending, claimed, delivered, unverified and failed with a closed reason set, and a claimed entry whose claimant died returns to pending on evidence, not on a timer (ADR-0014).
  - The lock is a leaf lock ranked per ADR-0019, and a test asserts no subprocess runs under it.
  - `cw outbox list` shows entries per client with state and reason.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### O2 — Courier drain over CLI and MCP bindings

- **Epic:** II
- **Wave:** 1
- **Sprint:** 2
- **Depends on:** O1, R1
- **Context:** `tracker_io` is a courier that only the codex verdict and codex stage posts use, launched as a hardwired `codex exec`. This ticket makes it drain outbox entries: a `cli: gh` binding calls `cw.gh.post_issue_comment`, an MCP binding runs one courier turn narrowed to the binding's comment tools as far as the harness allows, and both dedup on the marker by listing comments first. The drain is a bounded detached process the tick launches (D-B7). It adds a dated ADR-0013 update for the outbox and files ADR-0023 for courier turns (D-B8). Absorbs #3695 (turn argv cannot address codex plugin-app tools, wrong tool names), #3368 (the allowlist disables only user-layer MCP servers) and #3468 (decide the `review.verdict_comment_posted` success event; the outbox outcome events replace it).
- **Scope:** D-B1, D-B2, D-B3, D-B5, D-B6, D-B7, D-B8
- **Acceptance:**
  - A GitHub entry drains through `gh` with the same argv `cw.gh.post_issue_comment` uses today, pinned by a test.
  - A Linear entry drains through one courier turn using the binding's tool names, never a per-tracker constant. On a `per_tool` harness the allowlist is exactly the binding's comment and list tools. On a `per_server` harness (codex plugin apps, addressed under `apps.<app_id>`) every other server and app is disabled and the before/after snapshot check passes. Either way `tracker_io._verify` runs with no relaxed check.
  - Argv for a codex plugin-app binding loads: a test pins that no `mcp_servers.<name>` key is emitted for an app-provided binding.
  - An entry whose marker is already on the ticket settles delivered without posting.
  - An entry reported `unverified` is re-checked by the next courier run, a separate turn, before it drains anything new, and the outcome is an event.
  - The tick launches at most one drain per client, never from inside `reconcile()` or under a lock, and a drain claims at most `outbox.max_entries_per_run` entries (D-B7).
  - No reachable drain leaves the entry pending, emits one operator-visible event per entry and files one gate record per binding, never a row hold.
  - The courier turn disables every MCP server and app it can see, from every config layer, not only the user layer; a test with a project-layer server pins it (#3368).
  - The outcome event set is decided once for the outbox (delivered, unverified, skipped, pending) and `docs/events.md` and the orchestrator-config golden fixture agree with the code (#3468).
  - Dogfood: on this repo, one real verdict comment drains through the outbox with a marker and dedups on a forced second drain.
  - ADR-0013 carries a dated update naming the outbox and the `cli: gh` drain, and ADR-0023 "Courier turns" is filed and supersedes the 2026-11-10 update (D-B8).
  - `tests/test_ticket_boundary_guard.py`'s direct-gh allowlist is unchanged.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### O3 — Move daemon prose producers onto the outbox

- **Epic:** II
- **Wave:** 2
- **Sprint:** 3
- **Depends on:** O2
- **Context:** The codex verdict comment, `codex_stage_post`, `approve --post-marker`, `resolve --post` and the gate-recipe audit comments each post inline, and the last posts through `gh` with no tracker guard. `cw review settle` prints a `## Review Finding Dispositions` marker for someone else to post, which on Linear nobody does. This ticket moves all six to enqueue and deletes `cli/dev_queue/_tracker_post.py`, so on Linear these posts are delivered instead of refused or failed. Absorbs the post halves of #2701 (verdict and settle marker on non-GitHub trackers) and #3544 (the codex FINALIZE Linear PR-link post; its CI wait, ticket transition and bookkeeping stay on #3544).
- **Scope:** D-B1
- **Acceptance:**
  - Each of the six producers enqueues and returns without a tracker call, and a test per producer asserts the entry.
  - `_tracker_post.py` is deleted, `approve --post-marker` and `resolve --post` no longer refuse on a non-GitHub tracker, and their echo names the entry instead.
  - The gate-recipe audit comment on a Linear client produces an entry and no `gh` launch.
  - The existing verdict-comment events still fire with the same payload keys, now carrying the entry id.
  - `cw review settle` enqueues its disposition marker instead of printing it for relay.
  - A codex FINALIZE on a Linear client enqueues the PR-link comment.
  - Dogfood: on this repo, `approve --post-marker`, `resolve --post` and one gate-recipe audit comment each land through the outbox on a real dogfood ticket.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### H1 — Harness capability declarations

- **Epic:** III
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** R1
- **Context:** `StageExecutor` declares nothing about what a harness can reach, so each capability question is a backend branch in a guard module, a dispatch gate or a doctor check. This ticket adds `capabilities()` (stages, skills, subagents, reachable bindings with probe, sandbox modes, clean-mode review support, tool-narrowing granularity, usage source) to the four executors. It defines a contract every future harness implements, so harden it before dispatch. The `local` (aider) declaration names IMPL only, which is the declaration half of #3219.
- **Scope:** D-C1
- **Acceptance:**
  - Each executor returns a typed capability declaration and `cw schema` publishes it.
  - `local` declares IMPL as its only stage.
  - Binding reachability is probed with the existing reads (branch `enabledPlugins` for claude-native, `backend_mcp_config` for codex and opencode) and reports enabled, absent or unknown.
  - `cw doctor` prints each lane's declared capabilities.
  - No dispatch behavior changes in this ticket.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### H2 — One generic preflight replaces per-backend gates

- **Epic:** III
- **Wave:** 4
- **Sprint:** 5
- **Depends on:** H1, R2
- **Context:** `tracker_mcp_gate.py` branches per backend and per codex stage, and codex capability, engine, finalize, repair-entry and clean-review guards each special-case a backend. This ticket adds stage needs (bindings, skills, write sandbox) and one preflight that parks on the first unmet need with a reason naming the capability, then deletes the per-backend branches and folds the two doctor checks into one. It changes park reasons, so harden it before dispatch. Absorbs #3219 (`local` routed to PLAN, REVIEW or FINALIZE fails closed before spawn), #3309 (the `servers[backend]` lookup written three times) and #3306 (doctor mislabels a skipped MCP read as missing configuration).
- **Scope:** D-C1, D-C3
- **Acceptance:**
  - Every park the deleted branches produced today is produced by the generic preflight for the same inputs, pinned by a table-driven test carried over from the existing gate tests. The table includes a client with `preflight.enabled` false, which gets no reachability park (D-C3).
  - Park reasons name the capability, and the old reasons are kept as aliases for one release with a removal ticket filed.
  - `tracker_mcp_gate.py`, `executor/_engine_guard.py`, `_finalize_guard.py`, `_repair_entry.py` and `_clean_review.py` are deleted, `codex_capability.py` keeps only the version probe, and the `review-convergence` doctor check reads clean-mode support from the declaration.
  - For an opted-in client, the preflight checks each tool name a binding declares against the tool list the harness actually exposes, and parks with a reason naming the missing tool.
  - A lane routing a stage to a harness that does not declare it parks before spawn with a reason naming the stage, and `cw doctor` reports the lane (#3219).
  - Binding-server lookup has one implementation used by preflight and doctor (#3309), and doctor reports a skipped read as `unknown`, not `missing` (#3306).
  - Dogfood: `cw doctor` on this machine reports every lane's capabilities, and one dispatch per installed harness (claude-native, codex, opencode, local) reaches its first stage or parks with the capability-named reason.
  - A guard test fails on any new `backend ==` or `TrackerClass.` comparison outside the executor adapters, the courier and the preflight, with an allowlist that may only shrink.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### H3 — Lift harness-neutral stage verification out of the codex namespace

- **Epic:** III
- **Wave:** 3
- **Sprint:** 4
- **Depends on:** none
- **Context:** About 3,300 lines of post-turn verification (stage gates, CI evidence, merge gate, PR create and reuse, arming, fence, growth budget, verified push) live under `codex_plan`, `codex_impl`, `codex_finalize` and `codex_fix_loop` and make no runner call. This ticket moves them into a stage-verification package with no behavior change, so the next non-Claude harness reuses them instead of forking.
- **Scope:** D-C2
- **Acceptance:**
  - The named modules move to the new package and import no codex runner, argv or profile module, enforced by an import test.
  - The existing codex stage tests pass with only import-path edits.
  - No moved module exceeds the module-size ceiling and the codex packages keep only adapter code.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### T1 — Ticket snapshot at intake

- **Epic:** IV
- **Wave:** 5
- **Sprint:** 6
- **Depends on:** R1, O1
- **Context:** Later stages re-read ticket text from the tracker (codex review fetches comments every pass, codex PLAN gets read tools), so every stage needs the tracker binding. Intake already writes `.cw/context.json` (title, body, comments) under the #2700 reader contract. This ticket extends that file under a new schema version (`ticket_url`, `snapshot_fetched_at`, a digest, and per-comment `id`, `author_id` and agent-marker flag), adds one `cw ticket snapshot` writer that claude-native intake and codex PLAN both call, and puts `snapshot_digest` and `snapshot_fetched_at` on the row. `models/ticket_view.py` is output-only and is not the home. It changes a reader contract and adds row fields, so harden it before dispatch.
- **Scope:** D-D1, D-D3
- **Acceptance:**
  - Claude-native intake and codex PLAN intake both write the snapshot through `cw ticket snapshot`, and the row carries only `snapshot_digest` and `snapshot_fetched_at`.
  - Every #2700 key keeps its name and shape, `tests/test_auto_dev_intake_context_schema.py` passes with only additive edits, and `_load_ticket_context` and `local_runner.build_task_message` read the extended file unchanged.
  - A refresh rewrites the file, updates the row digest and records an event.
  - A snapshot whose content is marked untrusted in prompts is pinned by a test.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### T2 — Later stages run on the snapshot

- **Epic:** IV
- **Wave:** 6
- **Sprint:** 7
- **Depends on:** T1, H2, O3
- **Context:** With a snapshot on the row, only intake, the per-pass refresh and outbox drain need the tracker, and none of them is the stage turn. This ticket declares `tickets` unneeded for IMPL, REVIEW and FINALIZE stage needs, and switches the codex review and fix-loop context to the snapshot plus `operator_resolutions`. The live GitHub fetch becomes the CLI-binding refresh, and a courier read turn refreshes MCP bindings at each review pass (D-D2). Absorbs the read halves of #2701 (operator constraints, voided findings and dispositions are inert on non-GitHub trackers) and #3530 (codex IMPL on a non-GitHub tracker reads the ticket and recovers the approved plan, from the snapshot instead of a tracker MCP). It changes how operators steer a running review, so harden it before dispatch.
- **Scope:** D-D2, D-D3
- **Acceptance:**
  - A codex REVIEW, IMPL or FINALIZE row on a Linear client spawns on a harness with no Linear MCP and completes from the snapshot in a test.
  - A ticket comment posted between two review passes reaches the next pass on both a GitHub (CLI refresh) and a Linear (courier refresh) client, pinned by a test per binding kind.
  - A failed refresh runs the pass on the last snapshot and emits an event, never a park.
  - Every post-intake read site listed in Epic IV reads through `ticket_snapshot.read`, and a test fails on a new direct ticket fetch outside intake, the courier and the approval-time body checks.
  - `_load_operator_comments`, `_load_voided_findings`, `_load_finding_dispositions` and the fix loop's `_load_comment_constraints` read the snapshot. On Linear a comment binds when its `author_id` is in `bindings.tickets.operator_ids` and it carries no agent marker, and with empty `operator_ids` they stay fail-closed (#2701, D-D3). The `review.operator_context_undelivered` signal is deleted.
  - The same comment id reported with two different author ids emits an event and drops that comment to untrusted context for the rest of the row (D-D3).
  - `auto-dev-impl-appendix.md` replaces the Linear `withheld (identity_unresolved)` rule with the D-D3 rule, keeping its contract-manifest ids.
  - A codex IMPL on a Linear client with no `.cw/plan.md` recovers the approved plan from the snapshot instead of blocking `plan_missing` (#3530).
  - `operator_resolutions` recorded mid-review reach the next review pass, pinned by a test.
  - Dogfood: on this repo, a steering comment posted mid-review on a real dogfood ticket changes the next pass's context, read back from `.cw/context.json`.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

### X1 — Retire TrackerClass

- **Epic:** I
- **Wave:** 6
- **Sprint:** 7
- **Depends on:** O3, H2, T2
- **Context:** After the bindings, outbox, preflight and snapshot epics land, two kinds of tracker branch remain. *Reach* branches (how to talk to the tracker) should be gone. *Identity* branches stay legitimately: the tracker kind is a repo fact (D-A1), and some decisions depend on it, such as ticket-id shape and `linear_prefix_map` checks (`review_debt_filing/*`, `cli/review/debt.py`), close and state semantics (`dispatch/ticket_state_gate.py`, `adopt_pr.py`), the settle store (`cli/review/_settle_store.py`), preflight and requeue checks (`preflight.py`, `dev_queue/lifecycle.py`, `dev_queue/requeue.py`) and doctor cross-checks. This ticket deletes `TrackerClass` and `tracker_kind` and routes every identity read through `resolve_tracker`.
- **Scope:** D-A1
- **Acceptance:**
  - `TrackerClass`, `TrackerKind` and `tracker_kind` are deleted.
  - Each of the 32 modules in References is classified in the PR as removed by an epic (naming the ticket), migrated to `resolve_tracker` as an identity fact, or a doctor cross-check, with no module left unclassified.
  - The H2 guard's tracker allowlist contains only `resolve_tracker` callers, each with a one-line identity reason, and fails on any `TRACKER_LINEAR` or `== "linear"` comparison outside it.
  - Tests are hermetic and all repo quality gates pass with coverage floors unchanged.

## Dogfood plan

Every epic ends with a run on real tickets, not only hermetic tests. This repo tracks in GitHub and this machine has claude-native, codex, opencode and aider installed, so the GitHub half of every epic dogfoods here. The Linear half needs an operator run on a Linear host, so it is not in any ticket's acceptance: a dispatched worker cannot meet it. Each epic's last ticket carries its GitHub dogfood line in Acceptance. The Linear runs are an operator checklist posted on the epic issue, and the epic closes only after the checklist is done.

- **Epic I (after R2):** dispatch one throwaway dogfood ticket per installed harness with `cw dev-queue add`. Confirm from the rendered prompt in each worktree that it carries exactly one bindings block, and that the block names `gh` for `tickets` and `code_host`. On a Linear host, the same check shows the binding's real tool names (`linear_get_issue`, `linear_list_comments`, `linear_save_comment` on a codex plugin host).
- **Epic II (after O2, O3):** on this repo, drive one dogfood ticket through codex review so the verdict comment drains through the outbox, then force a second drain and confirm it dedups on the marker. Exercise `approve --post-marker`, `resolve --post` and a gate-recipe auto-approve on the same ticket. On a Linear host (operator checklist): run `tests/test_tracker_io_live.py`, commit `tests/fixtures/tracker_io/linear_marker_probe.json`, and run one codex review whose verdict lands on a canceled Linear ticket.
- **Epic III (after H2):** `cw doctor` lists every lane's capabilities. One dispatch per harness either reaches its first stage or parks with the capability-named reason. A lane routing PLAN to `local` parks before spawn.
- **Epic IV (after T2):** on this repo, post a steering comment between two codex review passes and confirm it is in the next pass's `.cw/context.json`. On a Linear host (operator checklist), the same check through the courier refresh with an operator-id comment that binds and an unlisted-author comment that does not, plus a codex IMPL that recovers its plan from the snapshot.

The Linear runs reuse the 2026-11-17 brief: a canceled ticket, codex on the plugin path, and an evidence note per run committed or posted back to the epic. The opencode Linear path is unproven until a working model host exists, so its Linear dogfood is best effort and not an acceptance gate.

## Open questions

These could not be settled from the code.

1. **Does Linear keep hidden HTML-comment markers? Mostly answered (operator probe, 2026-11-17).** Yes, on the codex plugin path: the marker line survived and a second read-only list confirmed it. Still open: the evidence is the model's report, not a raw MCP response, and `tests/test_tracker_io_live.py` cannot run on that host until the D-B5 argv change lands. O2 hardening commits the fixture once the live test runs. The design proceeds on hidden markers.
2. **What do gate-recipe audit comments do on Linear clients today? Answered.** `gh issue comment <PREFIX-NNN>` is rejected before posting with `invalid issue format: "<PREFIX-NNN>"` (checked through `gh issue view`, which uses the same argument parser). The comment posts nowhere, and `reconcile/gate_recipes.py:566`/`:606` logs `gate_recipe_comment_failed ticket=<id> rc=<n>: <stderr>`. The recipe event is still the audit trail, so nothing is corrupted, but the tracker mirror is lost on every Linear client. O3 fixes a live, silent mirror loss rather than a latent one.
3. **Do the harnesses expose stable Linear tool names? Answered for codex, open for opencode.** Codex (plugin app through `codex_apps`, stable across two runs): `linear_get_issue`, `linear_list_comments`, `linear_save_comment`. Neither `PLAN_TRACKER_READ_TOOLS` (`get_issue`/`list_comments`) nor `TRACKER_COMMENT_TOOLS` (`create_comment`) matches. Opencode: server `linear` at `https://mcp.linear.app/mcp`, OAuth complete, 78 tools connected, names and write path unproven because no run could finish on that host. R1 therefore makes tool names per binding and per harness (D-A3), and H2's preflight checks the declared names against the harness's tool list rather than trusting config (H2 acceptance).
4. **Should claude-native REVIEW move onto the Python review loop?** Today REVIEW convergence exists twice: in prose for claude-native (`auto-dev-review.md`) and in Python for codex (`codex_review/`, `codex_fix_loop/`). The brief lists review convergence as staying in code, which argues for one loop. That is a product-behavior change for the default lane and is out of scope here.
5. **Should claude-native run the H3 stage-verification package after its turn?** Claude-native runs the same gates in prose through `.claude/scripts`. Running them again in Python would make every harness verify the same way, at the cost of a second gate run on claude-native.
6. **Who drains when no reachable harness holds the binding? Resolved by D-B6.** The dispatching host drains its own entries. Intake on that host proves a binding existed, so a later failure is lost auth, and it becomes a gate knock plus pending entries, not a silent wait.
7. **Does the snapshot lose live comment steering on codex REVIEW? No; resolved by D-D2.** The earlier draft read the snapshot as intake-only. Per-pass refresh through the binding keeps GitHub steering as it is, and adds it on Linear, where codex review reads no comments today.

## References

- `docs/adr/0013-agent-delegated-ticket-work.md` — the GitHub-only client invariant; the 2026-11-10 update sanctions the tracker-IO turn.
- `docs/adr/0008-tracker-resolution-is-a-typed-seam.md` — still `Proposed`; the descriptor idea Epic I reuses at the agent edge.
- `docs/rfcs/0018-backend-stage-parity.md` — D-1, D-2, D-4; Issues line lists the closed tickets.
- `src/cw/tracker.py:28-66` — tracker constants, `TrackerClass`, `tracker_kind`; stale chokepoint comment at `:27-28`.
- `src/cw/dispatch/tracker_mcp_gate.py:329-360` — `_evaluate_task`, the per-backend, per-stage routing.
- `src/cw/models/client.py:40-89` — `TrackerMcpGateConfig`, opt-in, `enabled: False` default.
- `src/cw/tracker_io/_verify.py:1-14` — the self-attestation limits, stated.
- `src/cw/review_debt_filing/_verify.py:1-8` — shape-only id verification.
- `src/cw/cli/dev_queue/_tracker_post.py:31-70` — the non-GitHub refusal shared by approve and resolve.
- `src/cw/reconcile/gate_recipes.py:540-612` — unguarded audit comment posts.
- `src/cw/codex_review/_context/core.py:148-172` — per-pass live comment fetch, GitHub only.
- `src/cw/codex_plan/_argv.py:48-66` — per-tracker read tool table.
- `src/cw/opencode_tracker_prompt.py` — the fragment Epic I generalizes.
- `src/cw/executor/core.py:147-192` — `StageExecutor` Protocol.
- `src/cw/executor/codex.py:47` — `CODEX_SUPPORTED_STAGES` includes all four stages; the `CodexExecutor` docstring still says PLAN, IMPL and REVIEW, and the `CODEX_REVIEW_ONLY` reason name is stale.
- `src/cw/models/ticket_view.py:1-17` — output-only, never persisted.
- `src/cw/context_schema.py` — v7 and v12 make approve and resolve posts mirrors of row state.
- Tracker-identity modules (32): `adopt_pr.py`, `cli/dev_queue/_tracker_post.py`, `cli/review/debt.py`, `cli/review/_settle_store.py`, `codex_background.py`, `codex_finalize/_run.py`, `codex_fix_loop/_driver.py`, `codex_impl/_run.py`, `codex_plan/_argv.py`, `codex_plan/_run.py`, `codex_review/_context/core.py`, `codex_review_persist.py`, `codex_review_signals.py`, `codex_stage_post.py`, `dev_queue/lifecycle.py`, `dev_queue/requeue.py`, `dispatch/ticket_state_gate.py`, `dispatch/tracker_mcp_gate.py`, `doctor/config_checks.py`, `doctor/tracker_mcp.py`, `executor/local.py`, `executor/opencode.py`, `opencode_tracker_prompt.py`, `preflight.py`, `review_debt_filing/{_close,_constants,_events,__init__,_link,_tracker}.py`, `tracker_io/_argv.py`, `tracker.py`.
- Backend-branching modules outside `codex*`/`opencode*` (27 of 47): `backend_mcp_config.py`, `cli/review/confirm.py`, `cli/review/converge.py`, `contract_manifest/_check.py`, `contract_manifest/_models.py`, `contract_manifest/_render.py`, `contract_manifest/_stages.py`, `dev_queue/red_pr_recovery.py`, `dev_queue/requeue.py`, `dispatch/claim/events.py`, `dispatch/gating/context_json.py`, `dispatch/tracker_mcp_gate.py`, `doctor/all_codex_lane.py`, `doctor/review_convergence.py`, `doctor/review_engine.py`, `doctor/tracker_mcp.py`, `doctor/wedge/orphans.py`, `executor/_clean_review.py`, `executor/_resolve.py`, `executor/core.py`, `executor/local.py`, `reconcile/harvest_synthesis.py`, `reconcile/local.py`, `reconcile/unowned_running_acts.py`, `review_convergence/_backend.py`, `review_convergence/_engine.py`, `tracker_io/_turn.py`.
- `.claude/commands/auto-dev-intake.md` Step 0d — the existing `.cw/context.json` snapshot and its #2700 reader contract.
- `.claude/commands/auto-dev-impl-appendix.md` — the Linear `withheld (identity_unresolved)` rule D-D3 replaces.
- `tests/test_ticket_boundary_guard.py` — the `{"cw/gh/"}` direct-gh allowlist D-B2 keeps.

## Issues

Issues: _(filled by `/sprint-buildout`)_
