> Companion appendix to /auto-dev-impl. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 2 Implement — Appendix

Rare-branch procedures and design rationale extracted from
`.claude/commands/auto-dev-impl.md` (#1879). Each section is reached from a
named trigger sentence in the core doc; nothing here is required on the common
path (dispatch worktree present, plan on disk, gates pass first time).

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
