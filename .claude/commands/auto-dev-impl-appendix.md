> Companion appendix to /auto-dev-impl. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 2 Implement — Appendix

Rare-branch procedures and design rationale extracted from
`.claude/commands/auto-dev-impl.md` (#1879). Each section is reached from a
named trigger sentence in the core doc; nothing here is required on the common
path (dispatch worktree present, plan on disk, gates pass first time).

---

## Orientation: tracker-aware plan recovery when `.cw/plan.md` is absent

Reached from the core doc's **Orientation** paragraph, and only when
`.cw/plan.md` is missing from the worktree — the normal per-stage dispatch
carries Stage 1's plan file forward, so this path is rare.

Fall back to the tracker (#943), branching by the active tracker (mirrors the "Comments are live, not cached" paragraph in the core doc): fetch the NEWEST ticket comment containing `<!-- plan-spec-reviewed` **and authored by the currently authenticated `gh` identity** (GitHub: resolve `ME=$(gh api user --jq .login)`, then `gh issue view <ticket> --json comments -q "[.comments[] | select(.body | contains(\"plan-spec-reviewed\")) | select(.author.login == \"$ME\")] | last | .body"` — mirrors the authorship check `fetch_approved_plan_comment` enforces in `src/cw/gh.py`, #1128; a marker-bearing comment from any other commenter is never authoritative and must be skipped). If `$ME` resolves empty, treat that identically to "no reviewed-plan comment found" — do not fall back to an unauthenticated substring match. **Linear (#1906):** fetch ticket comments via `list_comments(<id>)` and scan newest-first for a comment bearing the same marker, authored by the pipeline's own Linear identity (resolve it via the Linear MCP's `viewer` identity operation and require the candidate comment's author to match before honoring the marker — mirrors GitHub's authorship check exactly, #1128; a marker-bearing comment from any other commenter is never authoritative and must be skipped). **Fail-closed fallback:** if the connected Linear MCP exposes no viewer/identity operation, do NOT fall back to trusting any marker-bearing comment or a sentinel-string check — treat that identically to "no reviewed-plan comment found" for this ticket, and report the actual Linear MCP tool surface found (concrete op names) so the trust-check design can be revisited with real data. Write the comment verbatim to `.cw/plan.md` and proceed with it as the approved plan. Only when the active tracker's branch above — GitHub or Linear — also has no reviewed-plan comment has Stage 1 genuinely not completed — then exit `blocked` with `blocker.reason: "plan_missing"`.

---

## Dispatch detection: the alternative check and the #766 nested-worktree leak

**Alternative detection.** Instead of testing for the file's presence, inspect
`.claude/cw-context.json` for the `"headless"` field's **value**. The field is
always written (interactive USER-origin sessions get `headless: false`, not an
absent field), so detection must key on truthiness, not presence, and fail open
to interactive when the file is missing, unreadable, or the field is
absent/false — the authoritative check `_is_headless()` implements
(`src/cw/reconcile/_shared.py:493-505`).

**Why the nested worktree is wrong.** The `isolation: "worktree"` flag on the
Agent() call creates a **second, nested worktree inside the main checkout**
(`<main_repo>/.claude/worktrees/<slug>`). When cw dispatch already provided an
isolated worktree as the session cwd, that nested worktree is redundant and
makes the main checkout path trivially derivable — the #766 leak pattern
(worker `cd`s to main checkout and commits there).

---

## Async dispatch: why never to busy-wait on the impl agent (verified 2026-08-19)

The Agent tool is asynchronous unconditionally — `run_in_background` is no
longer one of its parameters and there is no way to block on a spawn. Waiting
for the impl agent means **ending the parent turn** and resuming on its
completion notification.

That is safe in headless: the Stop hook payload lists the in-flight subagent in
`background_tasks` (`{"type": "subagent", "status": "running", ...}`) and
`cw signal-stop` defers session completion while that list is non-empty
(`src/cw/cli/stop_hook.py:364`), so the run is not orphaned.

**Never** hold the turn open with no-op `Bash` calls (`true`, `sleep`, repeated
polls). Each is a wasted model round-trip, and busy-waiting camouflages a stuck
worker: ADR-0014 removed every kill timer, so the only automated stuck-worker
signal left is the liveness distress sweep (`src/cw/reconcile/liveness.py`),
which keys on transcript staleness — no-op polls keep the transcript fresh, pin
the session at LIVE, and `SESSION_NEEDS_ATTENTION` never fires.

The asymmetry matters when writing the impl agent's own prompt: a parent's
turn-end is a pause, but a **subagent's** turn-end is a *return* — work it
leaves running in the background does not survive, so the impl agent must finish
its build/test commands inside its own turn rather than backgrounding them and
returning. (`run_in_background` is still a valid `Bash` parameter; only the
Agent spawn lost it.)

This async-dispatch exemption is scoped to the Agent tool's subagent spawn only —
it does not extend to a raw Bash call; see `auto-dev.md`'s Worker Execution Discipline
section for the no-backgrounding rule that applies there.

---

## Pre-Stage Detector Guard: resume dispositions and the staleness check (#1794)

Reached from `### Pre-Stage Detector Guard` in the core doc, and only when
`detect_current_stage()` reported `s2_implementing` or a stage past S2 — i.e.
this ticket already carries branch work. A fresh dispatch never reaches here.

**Staleness/regress check (#1794) — run before applying either bullet below, whenever the detector reports a stage past S2.** The "past S2, do not re-implement" premise — that HEAD's `Auto-Dev-Stage: impl-complete` trailer means nothing is left to do — holds only if nothing has asked for more work since HEAD was written. Compute:

```bash
HEAD_COMMIT_AT=$(git log -1 --format=%cI HEAD)
REGRESSED_INTO_STAGE=$(jq -r '.queue_metadata.regressed_into_stage // empty' .claude/cw-context.json 2>/dev/null)
MIN_VERSION=1  # per the script version table in auto-dev-impl.md
GUARD_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
CTX_WORKTREE=$(jq -r '.worktree_path // empty' \
  "$GUARD_ROOT/.claude/cw-context.json" 2>/dev/null)
[ -n "$CTX_WORKTREE" ] && GUARD_ROOT="$CTX_WORKTREE"
RESOLVED=""
for candidate in "$GUARD_ROOT/.claude/scripts/check_impl_guard_staleness.py" "$HOME/.claude/scripts/check_impl_guard_staleness.py"; do
  if [ -f "$candidate" ]; then RESOLVED="$candidate"; break; fi
done
if [ -n "$RESOLVED" ]; then
  FOUND_VERSION=$(head -n 5 "$RESOLVED" \
    | sed -nE 's/^#[[:space:]]*cw-script-version:[[:space:]]*([^[:space:]]*)[[:space:]]*$/\1/p' \
    | head -n 1)
  # Bounded to 1-6 digits so an oversized value can never overflow `[ -lt ]`
  # (which errors, evaluates false, and would fall through to the invocation).
  if [[ ! "$FOUND_VERSION" =~ ^[0-9]{1,6}$ ]] || [ "$FOUND_VERSION" -lt "$MIN_VERSION" ]; then
    echo "STALE: $RESOLVED missing/stale cw-script-version marker (need >= $MIN_VERSION)"
    # HARD STOP: EXIT blocked with impl_failed (see bullet below); never run
    # the script, and never fail open to the absent-from-both-locations
    # `stale: false` short-circuit.
    exit 3
  else
    VERDICT=$(uv run python "$RESOLVED" \
      --head-commit-at "$HEAD_COMMIT_AT" \
      --comments-file /tmp/impl-comments-$CW_SESSION.json \
      --regressed-into-stage "$REGRESSED_INTO_STAGE")
  fi
fi
```
(`/tmp/impl-comments-$CW_SESSION.json` is the freshly live-fetched comments array from the Orientation step in the core doc, written to a temp file before this call.)

Resolution follows "Guard-script path resolution and staleness marker (#2141)" in the core doc. Two dispositions attach to it, and neither is the script's own verdict:

- **Absent from both locations** (no repo-local copy, no global install): log `"check_impl_guard_staleness: script absent, skipped"` in `friction_highlights` and treat as `stale: false` — the unchanged fail-open short-circuit. A missing file coincidentally exits 2 as well, but it is not an unparseable timestamp and must not be filed as one.
- **Candidate found but its marker is missing or below minimum:** do NOT run it and do NOT fail open — EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Pre-Stage Detector Guard: HEADLESS BLOCK — check_impl_guard_staleness.py at <resolved-path> — missing/stale cw-script-version marker (need >= 1)"`, and STOP.

**This file-staleness check is a distinct, earlier gate from the script's own `stale` output field.** The marker answers "is this copy of the script current?"; the verdict's `stale: true/false` answers "have the impl comments moved past HEAD?". A stale *script* can produce a confidently wrong *verdict*, which is exactly why the marker gate runs first and blocks rather than feeding the verdict downstream. Do not conflate the two.

`REGRESSED_INTO_STAGE` reads `.claude/cw-context.json` → `queue_metadata.regressed_into_stage` (written by `spawn_create_impl` from `TicketTask.regressed_into_stage`, `src/cw/spawn.py`). A non-empty value means THIS impl-stage entry was reached via `_stage_regress` — the operator's `cw dev-queue requeue <T> --regress --stage impl`, or the FINALIZE self-heal regress — an explicit external assertion that the stage is NOT actually complete. It is a **per-arrival** signal (cleared by dispatch the moment this session was spawned, `src/cw/dispatch/claim.py`), deliberately distinct from `TicketTask.regress_attempts` (a cumulative, never-reset-on-advance counter bounding the FINALIZE self-heal cap, which would otherwise misfire on every later IMPL entry after a single regress anywhere in the ticket's history, #1794). A missing/unreadable `queue_metadata` reads as empty, not an error. **Known limitation:** the marker is consumed and cleared at spawn time, so a session that dies before acting on it loses the regress signal; the comment-staleness check above is the backstop, but a bare `--regress` with no accompanying comment would not be caught. #1801 evaluated making the marker survive a no-sentinel death and rejected it (would fragment the shared `_stage_regress` seam and reintroduce the same gap at Orientation's early `blocked` exit) — an accepted, documented limitation, not an oversight.

On script exit 2 (`--head-commit-at` unparseable — fail open): treat as `stale: false`, proceed as the unchanged behavior below, and log `"impl_guard_staleness_check_failed"` in `friction_highlights`. A malformed or unreadable `--comments-file` does NOT trigger exit 2 (#1794 follow-up): the script still exits 0 with a computed verdict and `comments_load_failed: true`, because `REGRESSED_INTO_STAGE` is an independent queue-state-derived signal that must not be discarded over a transient comments-fetch hiccup. If the verdict's `comments_load_failed` is `true`, log `"impl_guard_comments_load_failed"` in `friction_highlights` alongside the verdict's `reasons`.

- If `stage == "s2_implementing"`: the branch exists but the `Auto-Dev-Stage: impl-complete` trailer is absent. **Resume from current branch HEAD; do not reset.** Log the resumed-from SHA for audit, skip the worktree-create + branch-init steps in the core doc, and have the new impl agent continue on top of existing commits.
- If `stage` is past S2 (`s3_*`, `s4_*`, `s5_*`, `merged`) **AND the verdict's `stale` is `false`**: advance to that stage's entry point; do not re-implement. The unchanged fast path — a completed impl with no new operator activity and no regress.
- If `stage` is past S2 **AND the verdict's `stale` is `true`**: the trailer's premise ("impl is done, nothing to do") is stale — do NOT advance to the next stage's entry point and do NOT treat the ticket as complete. **Resume from current branch HEAD; do not reset** (same discipline as the `s2_implementing` bullet). Log the verdict's `reasons` (`regressed_to_impl` and/or `stale_comment_after_head`) in `friction_highlights`, and pass the live-fetched comments (verbatim, chronological, especially any postdating `HEAD_COMMIT_AT`) to the Stage 2 agent as new, binding instructions to read and act on before re-declaring completion. The agent still must append a fresh `Auto-Dev-Stage: impl-complete` trailer to its new final commit per the S2 Completion Marker in the core doc — the old trailer's commit is not this run's answer.

---

## Step 2.5 gate 2: scope-conformance disposition by exit code (#1779)

Reached from gate 2 of `### Step 2.5: Orchestrator Completion Gate` in the core
doc. Exit 0 with an empty `extra_files` — the delivered file set matches the
plan's enumeration — is the common path and needs nothing from this section.

- **Exit 1 (drift) — only after validating the verdict:** exit 1 is not by itself proof of drift. A transient `uv run` / script failure (sync error, uncaught exception, environment issue) also commonly exits 1, and is indistinguishable from genuine drift by exit code alone. Before building `blocker.details` from the verdict, confirm `$SCOPE_CONFORMANCE_OUTPUT` parses as JSON and contains a `triggered` key (ideally also `extra_files`, `allowed_extra`, `plan_file_count`, `delivered_file_count`) — e.g. `echo "$SCOPE_CONFORMANCE_OUTPUT" | uv run python -c 'import json,sys; d=json.load(sys.stdin); assert "triggered" in d'`.
  - **Valid JSON verdict with a `triggered` key present:** EXIT `blocked` with `blocker.reason: "plan_scope_drift"`, `blocker.stage: "stage2_impl"`, and `blocker.details` carrying the verdict's `extra_files` list **verbatim** (e.g. `"Step 2.5 gate 2: <N> files outside the plan's enumeration (allowed <allowed_extra>). Extra: <comma-joined extra_files>"`). Do NOT spawn reviewers — a diff that outgrew its approved file set is a diff no reviewer can converge a fix loop against, which is the failure this gate exists to stop. The enumerated paths are the operator's **entire** authorization surface: if the growth was legitimate, the operator requeues the parked task directly (there is no separate in-band "this was requested" signal, by design — see #1786). The emitted sentinel MUST populate `scope.lines_actual` from the already-computed `git diff --stat` (`stage_reached: "stage2_impl"` is post-impl, and the schema rejects a null `lines_actual` there).
  - **No valid JSON verdict (tooling failure, not drift):** do NOT treat as `plan_scope_drift`. Route through the existing `impl_failed` disposition instead — EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Step 2.5 gate 2: scope-conformance script exited 1 without producing a valid verdict — treating as tooling failure, not drift: <stderr/stdout excerpt>"`. Do NOT spawn reviewers.
- **Exit 0 with a non-empty `extra_files` (within allowance):** append `"impl_scope_growth: <files>"` to `friction_highlights` and continue (non-blocking; routes through the existing Stage 3b scope-growth handling).
- **Exit 2 (parse error, e.g. the plan has no parseable `## Files Modified` section):** do NOT block. Append `"impl_scope_conformance_unparsed: <stderr>"` to `friction_highlights` and continue — a plan the gate cannot read is a plan-format problem, not an implementation failure, and failing closed here would park every run against a legacy plan document.

---

## Type check gate: mypy baseline detection and comparison

Reached from the core doc's **Type check gate** (Stage 2 agent prompt), from
Step 2.5 gate 4, and from `auto-dev-review.md` Step 3b — whenever a worker or
the orchestrator is about to treat a repo as having a mypy baseline. The
common path (no baseline, or a strict override applies) never needs this.

**Detection — both conditions required, evaluated at `$FORK_POINT`:**

1. A baseline file is tracked: `git ls-tree -r --name-only "$FORK_POINT"` lists
   it (e.g. `mypy-baseline.txt`, `.mypy/baseline.json`). Record its
   repo-relative path, verbatim, as `MYPY_BASELINE_FILE`.
2. The repo's own gate consumes it: its pre-commit config, CI workflow, or lint
   script (as of `$FORK_POINT`) runs mypy through a mechanism that reads that
   file — e.g. `mypy ... | mypy-baseline filter`, or basedmypy's
   `--baseline-file`. Record that exact command, verbatim, as
   `MYPY_BASELINE_CMD`.

A baseline-looking file that no gate consumes is not a baseline — use the
no-baseline branch. Never construct a baseline mechanism the repo does not
already run.

**Blocking set.** Run `MYPY_BASELINE_CMD` (scoped to touched files if the
command accepts paths; otherwise whole-repo, filtered to touched files). Every
error it reports in a touched file blocks. Errors plain `uv run mypy
<touched_files>` reports that `MYPY_BASELINE_CMD` suppresses are the baselined
pre-existing set — they do not block. The core doc's transitive rule still
applies: untouched-file errors that did not exist at `$FORK_POINT` block.

**The baseline may never grow in this diff.** Check:

```bash
git diff "$FORK_POINT" -- "$MYPY_BASELINE_FILE" | grep -E '^\+[^+]'
```

Any output (an added or rewritten entry line) means the diff baselined an
error — that is a suppression, same class as `# type: ignore`: STOP and report
a BLOCK needing explicit user approval. Pure deletions (debt you fixed) are
fine. Do not run the tool's re-sync/regenerate command; if it would rewrite
unrelated lines, leave the file untouched.

**Orchestrator record (Step 2.5 gate 4 and Step 3b only).** For each touched
file carrying baselined pre-existing errors, append one bare-shape entry to the
`DEFERRED-REVIEW-FINDINGS` block in `.cw/deferred-findings.md`. Skip a file
that already has an entry. `cw review adjudicate` merges this as prior content,
but its parser fails closed (`src/cw/review_adjudication/_deferred_md.py`), so
the shape is a contract:

- **File absent or empty** — create it with exactly this skeleton, then the
  entries inside the block (the title and provenance lines are required; a
  file without the title makes Stage 3's next `cw review adjudicate` refuse to
  run):

  ```
  # Deferred Review Findings
  <!-- written by Stage 3 (auto-dev-review.md), consumed by Stage 4 Step 4d (auto-dev-finalize.md) -->

  ## Review adjudication

  <!-- DEFERRED-REVIEW-FINDINGS
  <entries>
  DEFERRED-REVIEW-FINDINGS -->
  ```

- **File present without a `DEFERRED-REVIEW-FINDINGS` block** — append the
  block (with the entries) at the end, leaving existing lines untouched. Only
  safe when every existing non-blank line is one of the skeleton's structural
  lines or a well-formed `- <file> — "<summary>" — <rationale>` rejected
  bullet (the parser rejects anything else). If any line is neither, do not
  write: report `deferred_findings_unparseable` in friction with the file's
  contents and leave the file untouched.
- **Block present** — insert the entries immediately before its closing
  `DEFERRED-REVIEW-FINDINGS -->` line.

Each entry, exactly four lines, no `round:`/`recorded_at:` lines (those are
written only by the CLI), and no `"` inside the quoted values:

```
- severity: SHOULD_FIX
  summary: "pre-existing baselined mypy errors in <file>"
  file: <file>
  rationale: "baselined debt in a file this change touched; not introduced by it: <file:line [code], ...>"
```

Stage 4 Step 4d copies these into the PR body and Step H3 files them as
tickets, so inherited debt surfaced by the gate is never left only in
`friction_highlights`.

---

## Step 2.5 gate 1: why the empty-diff check is not the only line of defence (#1870)

Dispatch independently re-verifies the empty-diff condition with its own git
measurement at the REVIEW→FINALIZE checkpoint
(`dispatch/review_gates.py::_should_gate_for_empty_diff`), because a
stage-pointer walk, resume, or requeue can advance `task.stage` past IMPL
without gate 1 re-running against the final branch state — which is exactly how
an empty branch once reached a human approval prompt. Nothing changes in the
core doc's gate; it stays the first and cheapest catch.

---

## Worktree isolation: the #402 breach this codifies

The invariant exists because of the #402 isolation breach — a worker's
`git checkout` resolved "the workspace" to the operator's main checkout. The
interactive "continue manually from the worktree" fallback, and any direct-git
fallback assuming the main session's checkout is the work tree, **does not apply
in headless mode**: a shared checkout mutated to make progress is never an
acceptable workaround, which is why the only sanctioned response is to EXIT
`blocked` and let the orchestrator re-dispatch onto a fresh worktree.

---

## Implementation failure escalation (interactive only)

If any agent returns friction level **BLOCK**: surface the blocker immediately
via AskUserQuestion and do NOT proceed to the next stage.

If the implementation agent fails tests/lint/mypy after 2 attempts: surface the
failure details via AskUserQuestion — "Continue manually from worktree, skip
ticket, or abort pipeline?" — and do NOT loop indefinitely.

In headless mode neither branch applies: escalate exclusively through the
sentinel's `blocker` field, per "Stage 2 Completion" in the core doc.

---

## S2 completion marker: why the trailer survives squash-merge

Squash-merge to main hides the trailer from main's history but it remains on the
branch's commits, which is where the detector reads. On resume, a branch with
this trailer and no PR resolves to `s3_review_pending`; a branch without it
resolves to `s2_implementing` (resume in-flight).

---

## Read-only helper spawns: capability, not instruction (#2211)

### The incident

An impl-stage worker needed the text of a tracker comment whose thread was too
large to read directly. It forked a harness subagent for that narrow,
read-only extraction.

The subagent ignored that scope. It inherited the implementation mandate from
the surrounding context and did real work on the live branch: edited a source
file and a test file, ran tests, lint and type checks, committed, and **pushed
to the ticket's feature branch**. It self-reported afterwards. The parent's
mid-flight instruction to stop arrived after the push had landed.

Two failures, and the second is the one #2017 was about:

1. **Scope inheritance.** A fork inherits the parent's tools *and* its
   context, so the read-only remit was carried only by the prompt — and the
   prompt lost to the mandate sitting in the inherited context.
2. **No roster entry.** A harness subagent never enters cw's session roster.
   cw could not see it start, could not observe what it was doing, and had no
   channel to stop it. The parent's own message was the only control channel,
   and it lost the race.

The content of the push happened to be correct. That is luck, not a
mitigation: the same failure with a wrong diff puts unreviewed work on a
branch with no cw record of where it came from.

The detection cost was its own problem. From outside, a correctly-escalating
parent presented as a session idle ~19 minutes with no PR — `STOP-OR-PEEK` on
the peek ladder, indistinguishable from a wedge. Only a manual peek showed a
healthy session waiting.

### Why the fix is a tool allowlist

`Read Only Helper` (`.claude/agents/read-only-helper.md`) carries
`tools: [Read, Grep, Glob]` — no `Bash`, no `Edit`, no `Write`. It cannot
edit, commit or push because it has no tool that does those things. No
phrasing of a task and no inherited context can restore the capability.

Every other registered agent type, including every reviewer, carries `Bash`.
`Bash` alone restores file writes, commits and pushes, so "read-only by
convention" is not read-only. `general-purpose` is not a safe substitute for a
lookup either.

Because the helper has no `Bash`, it cannot fetch its own input. **The parent
fetches first** (`gh issue view <n> --json comments > .cw/<name>.json`) and
hands over a file path. That asymmetry is the design, not a limitation to work
around: the fetch is the parent's authority, the extraction is the helper's.

### What `cw agent-spawn-pre` now refuses

The hook already ran on the `^(Agent|Task)$` matcher in every dispatched
worktree, so the policy folded into it at zero additional interpreter cost,
and already-provisioned worktrees pick it up on a `cw` upgrade.

In a **headless** worker (`.claude/cw-context.json` with `headless: true`),
with the guard enabled:

- `subagent_type` explicitly `fork` (any case) or blank → **refused**, exit 2.
  The spawn does not happen and no stamp is written.
- `subagent_type` **not named at all** → **allowed**, with a `WARN` line on
  stderr. This is **record-only**, not enforcement.
- any named type → allowed silently.
- anything it cannot classify, any non-headless session, any cwd with no
  ancestor cw-context.json → allowed, no verdict.

**Why the omitted case is record-only rather than refused.** The spawn-site
inventory needed to make denial safe is incomplete. `review-sweep.md` names
six roles — `Bug Hunter`, `CLAUDE.md Auditor`, `Context Checker`,
`silent-failure-hunter`, `pr-test-analyzer`, `type-design-analyzer` — that are
not registered agent types, so there is no correct `subagent_type` for a
refused caller to retry with. Denying on omission before those six have a home
would break working spawn sites and teach workers to route around the guard.
Flipping that case to deny is gated on finishing the inventory: either
register real agent types for those roles, or state explicitly that they run
as `general-purpose`.

### The config gate

`subagent_spawn_guard_enabled` is a global default-on bool in
`orchestrator.yaml`, with a bidirectional per-lane override in
`clients.yaml` — the same shape as `busy_wait_guard_enabled` (#1946). A
guard that can refuse work on a seam every dispatched worktree crosses needs
a kill switch that does not require a code release.

```yaml
# ~/.claude-workspace/orchestrator.yaml — disable everywhere
subagent_spawn_guard_enabled: false
```

```yaml
# ~/.config/cw/clients.yaml — disable for one lane only
clients:
  acme:
    lanes:
      - name: fast
        subagent_spawn_guard_enabled: false
```

See `config/CONFIG_REFERENCE.md`, section "Subagent Spawn Guard (#2211)".

### Residual gaps

- **R1 — the impl and finalize agents are still unrostered.** Stage 2's impl
  agent and Stage 4's finalize/prep-pr agents are harness subagents with no
  roster entry. They now name `subagent_type: "general-purpose"` explicitly,
  which is an improvement in legibility, not in observability: cw still cannot
  see or stop them. Closing this needs the `dispatch_fix_agent` migration
  #2017 applied to the review fix loop, applied here — a separate ticket.
- **R4 — a cwd outside any cw context escapes the policy.** A subagent that
  `cd`s into a directory with no ancestor `.claude/cw-context.json` (a
  detached `$TMPWT` gate worktree, a nested `isolation: "worktree"` spawn)
  fails open. Same limitation `cw guard-cwd` has today, carried forward.
- **R7 — the spawn-site inventory is incomplete**, which is why the
  omitted-`subagent_type` case ships record-only. See above.

### The deferred half — #2248

The other half of #2211 is **not** implemented: `classify_git_mutation` (a
classifier refusing `git commit`/`git push` from a non-writer subagent), its
enforcement in `cw guard-cwd`, and the durable `guard.subagent_action` event
that would record every subagent push.

That half is a fail-closed guard on the seam every headless `Bash` call
crosses, with unresolved regex false positives (`echo git push`,
`grep "git push"`), and it rests on an "`agent_id` is subagent-only"
assumption never empirically captured in this repo. #2248 owns it, and
requires its own Phase-0 capture and toggle before it ships.

Until then, the ticket's third acceptance bullet — *a push from an unrostered
agent is refused, or at minimum recorded* — is **not satisfied**. Nothing in
the shipped scope refuses or records a subagent's push.
