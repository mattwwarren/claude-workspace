# RFC 0017 — Repo Review Engine, Sign-off Attestation and Accepted Debt

## Summary

Teams that build a stricter review process on top of cw's generic `/review` have one option today: fork the skill and run the fork as a gate inside their project ship-it. cw's review stage cannot see that fork. So the pipeline reviews a change to convergence under one rubric, and then the ship-time gate reviews it again under another rubric, in a different session, at the same SHA. That repeated review shows up as #2992: about three extra stage runs per ticket. It also means a repo's real review policy never takes part in cw's convergence ledger, fix loop or confirm round.

This RFC lets a repo provide its own review skill as the **review engine** for the Claude-native review stage. cw keeps everything around a round: the review workspace, the convergence ledger, the fix loop, the confirm round and the operator gates. The engine decides which findings are real; cw only decides what to do with each one. When the review passes, cw records a head-pinned **sign-off attestation** on the queue row. cw hands it to finalize through `.claude/cw-context.json`, so a project ship-it can accept the pass instead of re-running its reviewer, and finalize can render it into the PR body as evidence.

Convergence is also allowed to clear through **accepted debt**. A real finding the pipeline chooses not to fix becomes a ticket at the moment of deferral, linked to its PR. The ticket is closed if that PR closes unmerged, and when the PR merges it is optionally queued into a dedicated debt lane. Deciding which debt to accept, and making sure it is not forgotten, is the job cw does here.

## Motivation

- **The engine is invisible.** `/prep-pr` already resolves a project-owned ship-it from three layouts and refuses when none exists (`.claude/commands/prep-pr.md:368`). A team's custom reviewer reaches the pipeline only through that ship-it, which runs after review has converged.
- **Review runs twice.** A project ship-it that gates on its own reviewer reuses a pass only within the same session. cw's review stage and finalize run in different sessions, so the reviewer always runs again at a SHA that already passed. #2992 records the cost: review converges clean, then the finalize gate parks the row on test-quality SHOULD_FIX findings that the review stage never checked.
- **The extension points cannot add a reviewer.** `.claude/review-extras.md`, `.claude/review-policy.md`, `.claude/sensitive-files.yml` and per-role agent overrides exist. The reviewer roster, though, is fixed in three places: the `/review` Step 3 table, `auto-dev-review.md` Step 3a and `_REVIEWER_ROLE_AGENT_FILES` (`src/cw/codex_review/_context/_agent_spec.py:30`).
- **Review config comes from the branch under review.** The codex lane reads `review-extras.md` from the task worktree (`src/cw/codex_review/_context/core.py:661`), and the Claude-native stage loads it from the session worktree (`.claude/commands/auto-dev-review.md:116`). A branch can therefore edit the rules its own review applies.
- **Headless deferrals are filed by hand or not at all.** Deferred findings are written to the PR body and harvested by Step H3 (`.claude/commands/auto-dev.md:824`). Step H3 "is interactive-only"; headless leaves filing to the orchestrator "or the next interactive sweep" (`.claude/commands/auto-dev.md:849`). Debt can be lost.

## Design

### Epic I — Engine seam

A client names a repo review skill as the engine for its Claude-native review lanes. The review-stage session runs that skill each round, pinned to the round's head SHA, and reads the engine's machine-readable summary from a path cw supplies. cw validates the summary against a versioned schema, maps the engine's findings into the review workspace, and lets the engine's judgment stand: Checkpoint 3a stops deciding whether engine findings are real and only routes each one to fix, defer to debt, or park for the operator. A round counts only if the engine reports complete coverage. Review configuration, the engine included, is read from the default branch, and a branch that modifies its own engine parks for the operator. The independent confirm round still runs, but it can only escalate to the operator or file debt.

### Epic II — Sign-off attestation

When the review stage exits clean, it writes a head-pinned attestation to the queue row. The attestation records the engine, head SHA, verdict, counts, acceptance results, risk, gate signals, deferrals, overrides and the findings ledger. cw serializes it into every later spawn's `queue_metadata.review_attestation`, so a project ship-it can treat a matching attestation as an earlier pass. On clients that opt in, finalize renders it into a `## Review sign-off` PR-body section through the redaction seam.

### Epic III — Accepted debt

A deferral files a debt ticket at deferral time, from cw code rather than worker prose. The ticket is labelled with its PR and its ID is recorded on the attestation. When tracker writes have to go through MCP, cw emits the payload and verifies the IDs the worker reports back. The sweep closes debt tickets whose PR closed unmerged. Behind a per-client toggle, debt tickets are queued into a named lane when the parent PR merges, one level deep.

## Phasing

| Wave | Epic I | Epic II | Epic III |
|------|--------|---------|----------|
| 0 (seams) | S1 — engine schema and config · S2 — split review_workspace · I2 — base-read review config | | |
| 1 | I1 — engine round in the review stage | II1 — attestation on the row and in cw-context | III1 — file debt at deferral |
| 2 | I3 — confirm routing over an engine pass | II2 — PR sign-off section | III2 — close debt of unmerged PRs · III3 — auto-enqueue debt on merge |

## Resolved decisions

These are firm leans, decided to unblock the breakout. Each one can be reversed at ticket-hardening if the code contradicts it.

- **D-1 — The repo's review skill is the round engine.** The engine runs each review round. cw keeps the review workspace, convergence ledger, fix loop, confirm round and gates around it. cw does not re-encode a repo's review policy as cw configuration.
- **D-2 — Review configuration is read from the default branch.** The engine, `review-extras.md`, `review-policy.md` and `sensitive-files.yml` are all read at `origin/<default-branch>`, never from the reviewed head.
- **D-3 — A round counts only when the engine reports complete coverage.** An engine summary whose coverage is not `COMPLETE` is a failed round, never a clean one.
- **D-4 — A head-pinned sign-off attestation lives on the row.** It is written when the review stage exits clean, is valid only for its `head_sha`, and is serialized into `queue_metadata.review_attestation` on every later spawn. A missing, unreadable or wrong-head attestation counts as no pass.
- **D-5 — The engine's adjudication is final; cw routes.** Checkpoint 3a routes each engine finding to fix, defer to debt, or park for the operator. cw never rejects an engine finding. A finding cw believes is a false positive becomes an operator park.
- **D-6 — Accepted debt clears the review.** A deferred finding counts as cleared for convergence and for the attestation. The coordinating agent may defer SHOULD_FIX. Only the operator defers MUST_FIX, through the existing override. Engine `coverage.*` and `acceptance.*` findings and plan deviations are never deferrable. A re-raised finding whose fingerprint is already deferred stays deferred and does not count as new.
- **D-7 — Debt is filed when deferred.** cw code files the ticket at deferral time, labels it with its PR and records the ID on the attestation. Where the tracker must go through MCP, cw emits the payload and verifies the IDs the worker reports back. The sweep closes debt tickets whose PR closed unmerged, with a back-link.
- **D-8 — Debt can be auto-scheduled.** A per-client toggle names a lane that must be declared, or config load fails. Debt tickets are queued into that lane when the parent PR merges, never earlier. Debt filed from a row in that lane is filed but never auto-queued.
- **D-9 — Confirm escalates; it does not judge.** The independent confirm round still runs after an engine pass. A confirm MUST_FIX parks for the operator. A confirm SHOULD_FIX is deferred to debt automatically.
- **D-10 — Existing extension files stay where they are.** `review-extras.md`, `review-policy.md` and `sensitive-files.yml` are not moved to a new directory in this RFC. D-2 applies to them at their current paths.
- **D-11 — The client names the engine.** `clients.yaml` names the engine skill (operator opt-in, ADR-0020). The repo provides the skill files. Nothing probes for an engine implicitly.
- **D-12 — The engine summary is a cw-owned, versioned schema.** cw passes the engine an output path. The engine writes a JSON summary there: verdict, coverage status, counts, findings with stable fingerprints, acceptance rows, risk, gate signals and `head_sha`. cw validates it strictly and fails closed. cw never parses an engine's markdown report.
- **D-13 — A branch that edits its own engine parks.** When the reviewed diff touches the engine skill's files, the round parks for the operator instead of running either copy.

## Tickets

### S1 — Engine summary schema and client engine config

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** The engine seam needs a cw-owned contract before any stage can run an engine: a client config field naming the engine skill and a strict, versioned JSON schema for the summary the engine writes.
- **Scope:** D-1, D-3, D-11, D-12
- **Acceptance:**
  - `clients.yaml` accepts a review-engine setting naming a skill for Claude-native review lanes, unknown sub-keys fail loud, and `config/CONFIG_REFERENCE.md` documents it.
  - A Pydantic model validates the engine summary (schema version, verdict, coverage status, counts, findings with fingerprint, severity, rule id, file, line and evidence, acceptance rows, risk, gate signals, head_sha) with `extra="forbid"`, and an invalid or oversized summary is rejected with a named error.
  - A `cw review engine validate <file>` command exits non-zero on an invalid summary and prints the failing field.
  - `docs/headless-contract.md` documents the summary schema and its version history.
  - Hermetic tests cover every rejection branch, and all repo quality gates pass with coverage floors unchanged.

### S2 — Split `review_workspace.py` into a package by concern

- **Epic:** none
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** `src/cw/review_workspace.py` is 1432 lines, over the repo's 1000-line module ceiling, and I1 maps engine findings into it, so it is split before I1 grows it.
- **Scope:** D-1
- **Acceptance:**
  - `src/cw/review_workspace.py` becomes a package with one submodule per concern and an `__init__.py` that re-exports the public surface, and every submodule is under 800 lines.
  - Every existing import of `cw.review_workspace` keeps working, pinned by a test that imports each public name from the old path.
  - No behavior change: the existing review-workspace and CLI review tests pass unchanged.
  - All repo quality gates pass with coverage floors unchanged.

### I2 — Read review configuration from the default branch

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** Both review paths read `review-extras.md` and `review-policy.md` from the reviewed worktree, so a branch can rewrite the rules its own review applies; the codex lane reads extras at `src/cw/codex_review/_context/core.py:661`.
- **Scope:** D-2, D-10
- **Acceptance:**
  - The codex lane, the opencode lane and the Claude-native review stage load `review-extras.md`, `review-policy.md` and `sensitive-files.yml` from `origin/<default-branch>`, not from the worktree.
  - A branch that changes one of those files is reviewed under the default-branch copy, and the review records that the branch modified review configuration.
  - A repo with none of those files on the default branch behaves as today.
  - Tests prove a worktree edit to each file does not change the prompt or policy applied, and all repo quality gates pass with coverage floors unchanged.

### I1 — Engine round in the Claude-native review stage

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1, S2
- **Context:** With an engine configured, the review-stage session runs the engine skill each round in place of the built-in reviewer roster and maps its validated findings into the review workspace and ledger, keeping cw's fix loop and gates.
- **Scope:** D-1, D-3, D-5, D-6, D-11, D-12, D-13
- **Acceptance:**
  - On a client with an engine configured, a Claude-native review round invokes the engine pinned to the round's head SHA with a cw-supplied summary path, and the built-in roster does not run.
  - The validated summary's findings are recorded in the review workspace and ledger under their engine fingerprints, and a summary that fails validation or reports coverage other than COMPLETE fails the round with a named reason.
  - Checkpoint 3a routes engine findings to fix, defer or operator park only, and a test proves no engine finding can receive a reject outcome.
  - A finding re-raised with a fingerprint already deferred stays deferred and is not counted as new by the convergence ledger.
  - `coverage.*` and `acceptance.*` engine findings cannot be deferred, and a MUST_FIX can be deferred only through the existing operator override.
  - A reviewed diff that touches the engine skill's files parks the row for the operator before the engine runs.
  - Codex and opencode review lanes on the same client are unchanged, and `cw doctor` reports an engine configured on a non-Claude-native review lane as a warning.
  - Hermetic tests with a stub engine cover pass, fail, incomplete, invalid summary and engine-modified branch, and all repo quality gates pass with coverage floors unchanged.

### II1 — Sign-off attestation on the row and in cw-context

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** A project ship-it re-runs its reviewer because a pass from another session is not visible to it; the row already carries a head-pinned review approval (`review_approved_sha`) that this attestation generalizes. Land after the `models/tasks.py` split in #2999, and put the attestation model in its own `cw.models` submodule.
- **Scope:** D-4, D-6, D-12
- **Acceptance:**
  - A clean review-stage exit writes an attestation on the row carrying engine, head_sha, verdict, counts, acceptance rows, risk, gate signals, deferrals with ticket IDs, overrides and the findings ledger, with a dev-queue schema bump.
  - Every spawn serializes it into `queue_metadata.review_attestation` with a cw-context schema bump, and null when absent.
  - Any later commit on the branch makes the attestation invalid for the new head, and a test proves a wrong-head attestation is never presented as a pass.
  - Every path that revokes `review_approved_sha` (including `_stage_regress`, `recover-red-pr` and a review-feedback recovery) also clears the attestation, with a test per path.
  - `docs/headless-contract.md` documents the attestation fields and the consumer rule (match on head_sha, fail closed), written so a project ship-it can adopt it.
  - All repo quality gates pass with coverage floors unchanged.

### III1 — File debt tickets at deferral time

- **Epic:** III
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** Deferred findings are filed only by the interactive Step H3 sweep or by hand, so headless debt can be lost (`.claude/commands/auto-dev.md:849`); filing moves into cw code at the moment of deferral. Land after the `models/tasks.py` split in #2999, and put any new row model in its own `cw.models` submodule.
- **Scope:** D-6, D-7
- **Acceptance:**
  - Each deferral recorded by `cw review adjudicate` files one debt ticket on a GitHub-tracker client from cw code, deduplicated by finding fingerprint, labelled `review-debt` and linked to the branch.
  - On an MCP-tracker client, cw emits the ticket payloads, the worker files them, and cw records and verifies the reported IDs, failing the stage when an ID cannot be verified.
  - The ticket ID is recorded with the deferral, and the PR link is added once finalize creates or reuses the PR.
  - Step H3 skips findings that already carry a ticket ID, and the runbook describes the new filing point.
  - A deferral whose finding names an owning ticket (`deferred_to`, #2111) comments on that ticket instead of filing a new one.
  - Tests cover GitHub filing, dedup, MCP emit and verify, and a verification failure, and all repo quality gates pass with coverage floors unchanged.

### I3 — Confirm routing over an engine pass

- **Epic:** I
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** I1, III1
- **Context:** The independent confirm round can contradict an engine pass; under D-5 it may escalate to the operator or file debt but never act as a second judge.
- **Scope:** D-5, D-6, D-9
- **Acceptance:**
  - After an engine pass, a confirm-round MUST_FIX parks the row for the operator with a named reason and does not re-enter the fix loop.
  - A confirm-round SHOULD_FIX is deferred to debt automatically through the III1 filing path.
  - A clean confirm round leaves the attestation unchanged, and tests cover all three outcomes.
  - All repo quality gates pass with coverage floors unchanged.

### II2 — Review sign-off section in the PR body

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** II1, III1
- **Context:** The attestation is the evidence a human reviewer needs on the PR itself; finalize Step 4d already writes the review adjudication and operator override sections into the PR body.
- **Scope:** D-4, D-7
- **Acceptance:**
  - On a client that opts in, finalize Step 4d writes a `## Review sign-off` section built from the attestation (engine, head_sha, verdict, counts, acceptance results, risk, accepted debt with ticket links, overrides) through the `cw post-comment` redaction seam.
  - The section is replaced in place across restarts, never duplicated, and is omitted on clients that have not opted in.
  - A section whose attestation head does not match the PR head is never written.
  - All repo quality gates pass with coverage floors unchanged.

### III2 — Close debt of PRs that close unmerged

- **Epic:** III
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** III1
- **Context:** Filing at deferral can orphan tickets when a PR closes without merging, so the sweep closes them with a back-link.
- **Scope:** D-7
- **Acceptance:**
  - A debt ticket whose linked PR is CLOSED and not merged is closed with a comment linking the PR, once.
  - A debt ticket whose PR merged, or whose PR is still open, is untouched.
  - Tests cover closed-unmerged, merged and open, and all repo quality gates pass with coverage floors unchanged.

### III3 — Auto-enqueue debt into a debt lane on merge

- **Epic:** III
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** III1
- **Context:** Accepted debt is paid down when lanes are free; a per-client toggle schedules it into a dedicated lane once the code it targets is on the default branch.
- **Scope:** D-8
- **Acceptance:**
  - A per-client setting enables debt auto-enqueue and names a lane, and config load fails when the lane is not declared for the client.
  - Debt tickets are added to the named lane only after their parent PR merges, and never while it is open.
  - Debt filed from a row in the debt lane is not auto-enqueued.
  - Enqueue never mints a sibling row for a ticket that already has a row, and the setting defaults off.
  - Tests cover merge-triggered enqueue, open-PR no-op, the one-level rule and the undeclared-lane failure, and all repo quality gates pass with coverage floors unchanged.

## References

- `.claude/commands/prep-pr.md:368` — the project ship-it probe; precedent for a repo-provided skill, and the path through which a custom reviewer gates today.
- `.claude/commands/auto-dev-review.md:116` — the Claude-native stage loads project review extensions from the worktree.
- `.claude/commands/auto-dev-review.md:282` — the optional SHOULD_FIX default to DEFER (#3009).
- `.claude/commands/auto-dev-review.md:300` — DEFER bucket: "record now, file as a ticket on merge".
- `.claude/commands/auto-dev-review.md:525` — the clean-mode confirm round and exits.
- `src/cw/codex_review/_context/_agent_spec.py:30` — the fixed reviewer role map.
- `src/cw/codex_review/_context/core.py:661` — the codex lane reads `review-extras.md` from the worktree.
- `src/cw/codex_review/_context/_repo_config.py:58` — `review-policy.md` parsing.
- `.claude/commands/auto-dev-finalize.md:108` — Step 4.0, the head-pinned review approval check.
- `.claude/commands/auto-dev-finalize.md:922` — Step 4d writes review adjudication into the PR body.
- `src/cw/models/tasks.py:733` — `review_approved_sha`, the existing head-pinned review record.
- `src/cw/models/tasks.py:265` — `DEV_QUEUE_SCHEMA_VERSION`.
- `src/cw/context_queue_metadata.py:71` — `build_queue_metadata`, where the attestation is serialized.
- `src/cw/spawn.py:158` — `CW_CONTEXT_SCHEMA_VERSION`.
- `.claude/commands/auto-dev.md:824` — Step H3, merge-time harvest of deferred findings.
- `.claude/commands/auto-dev.md:849` — Step H3 is interactive-only; headless filing is left to the orchestrator.
- `src/cw/_text.py:58` — `redact`, used by the post-comment seam.
- `src/cw/review_workspace.py` — 1432 lines, over the module ceiling; split by S2 before I1 extends it.
- `src/cw/models/tasks.py` — 1256 lines; split by #2999 before II1 and III1 add row fields.
- `src/cw/cli/post_comment.py` — the deterministic redact-and-post seam (#2810), including `--emit-only` for MCP trackers.

## Handoff Brief

**Authored:** 2026-11-08 at d0ffbd4e
**Exit:** 1 sprint block

### Decisions

- **D-1 to D-13** live in `## Resolved decisions` above, which is the single source, and their ids are stable. Reversal point for all of them: ticket-hardening, if the code contradicts them.

### Rejected approaches

- **Whole-skill override of the pipeline review stage with no cw loop around it.** It would bypass the review workspace, ledger and confirm round, and codex and opencode cannot run a skill.
- **A declarative review profile that cw reconciles into its own roster.** It re-encodes a team's tuned policy as cw configuration that drifts, and it leaves the repo's own validation scripts nowhere to run.
- **An external-reviewer slot alongside the built-in roster.** Deferred until a team has a reviewer that cannot be expressed as an engine.
- **Using the posted PR review as the attestation.** No PR exists at review time, a review skill may forbid posting without approval, and any writer can forge a comment.
- **A worktree file as the attestation.** It is writable by the agents it attests, and it is lost with the worktree.
- **cw and the engine both adjudicating.** The engine re-raises anything cw rejects, which makes oscillation structural and pays for adjudication twice.
- **Settlements the engine can read.** Deferred until an engine accepts operator-settled findings as input. Same-round engine SHOULD_FIX defers are reconciled at attestation time from the dev-queue row (#3625); engine-side reading stays deferred.
- **Filing debt at merge.** A missed sweep loses debt, and the attestation could only promise a ticket.
- **Skipping the confirm round after an engine pass.** It removes the only independent check.
- **Confirm findings with full blocking weight.** That creates a second judge, contrary to D-5.

### Evidence

- See `## References`. The load-bearing facts: the ship-it probe at `prep-pr.md:368` (the precedent), worktree-read extras at `core.py:661` and `auto-dev-review.md:116` (D-2), interactive-only Step H3 at `auto-dev.md:849` (D-7), and `review_approved_sha` at `tasks.py:733` (the shape D-4 generalizes).

### Do not re-adjudicate

- **Cut from scope:** engine support on codex and opencode review lanes, which keep today's behavior; moving the existing extension files (D-10); engine-readable operator settlements; absorbing foreign pushes.
- **Deferred:** the exact engine invocation from the review-stage session (Skill tool versus reading the skill file), resolved at I1 hardening.
- **Deferred:** the attestation's maximum size and whether the findings ledger is embedded or referenced, resolved at II1 hardening.
- **Deferred:** whether the attestation carries the approval basis #3015 adds to `review_approver_kind`, or the two schema bumps are ordered, resolved at II1 hardening.
- **Deferred:** whether an operator override of a confirm MUST_FIX in I3 must settle the ledger (#2834), resolved at I3 hardening.
- **Exit choice:** Exit 1, because the change rewords review-stage, finalize and sweep contracts and adds row and context schema fields, so it belongs in the RFC series with provenance.

### Orchestrator brief

- **Drives:** `sprint-buildout` on this RFC, then `/harden-ticket` on S1, I1, II1 and III1 before dispatch. Every ticket here defines a schema, a disposition or stage prose. Dispatch waves in order.
- **Gates:** `sprint-buildout`'s Approve/Abort gate before any filing; hardening before each dispatch; the review gates the pipeline already enforces.
- **Terminal state:** all tickets shipped, #3024 closed by this RFC's milestone, and #2992 re-checked against an engine-configured client.

## Issues

Issues: Epic I #3033 · Epic II #3034 · Epic III #3035 · S1 #3036 · S2 #3037 · I2 #3038 · I1 #3039 · II1 #3040 · III1 #3041 · I3 #3042 · II2 #3043 · III2 #3044 · III3 #3045

Pulled in: #2999 · #2834 · #2008. Supersedes: #3024.

Milestone: https://github.com/mattwwarren/claude-workspace/milestone/23
