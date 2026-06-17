# Handoff — 2026-06-17 — Pipeline-reliability sprint: #716/#724 root-caused, fix spec ready

## TL;DR
Sprint pivoted from "ship backlog" to "fix the pipeline" (operator decision).
Shipped 3 pipeline-reliability fixes (#717, #710, #711), filed 5 new pipeline
bugs (#722/#723/#724/#726/#728), and **root-caused the biggest one (#716/#724:
the ~21-26 min/stage timeout tax) down to exact code**. The fix is specified
below and ready to implement on branch `fix/716-route-emitted-sentinel` (created,
no commits yet). Operator asked to implement + hand off; diagnosis is the hard
90% and is locked — this handoff is the turnkey spec for the remaining 10%.

## Sprint state (main = a23f5b0 after #711)
SHIPPED this session (all merged, validated):
- **#717** (a23f5b0's ancestor) — cw-smoke-test preflight: `cw doctor --json` +
  backend-core filter, killed the `cw_backend_healthy` false-negative.
- **#710** (eed8ae8) — worktree: cut new branch from `origin/<default_branch>`,
  not operator HEAD. Verified.
- **#711** (a23f5b0) — tracker: branch-key `pr_is_merged_for_ticket` Linear-safe.
  Functional fix correct; **debt: hardcodes `"dev/"` → see #728**.

FILED this session (all on github, mattwwarren/claude-workspace):
- **#722** — prep-pr titles PR from the trailing chore commit, not the primary
  fix; on squash → wrong permanent main subject. Mechanism: GitHub auto-merge
  snapshots the squash subject at **arm-time**, so retitling after the PR
  appears is structurally too late. **This is the only real fix for the
  wrong-subject papercut** (the retitle-watcher can't win the race).
- **#723** — `_has_commits_beyond_base` hardcodes `origin/main`, ignores
  `client.default_branch` (sibling of #710, salvage-guard only, low pri).
- **#724** — stage timeout silently absorbed as a retry (the symptom of #716).
- **#726** — github-issues worker invoked Linear MCP `authenticate` → headless
  OAuth stall (observed on #711; ticket-themed distraction; fix = withhold
  Linear MCP from worker when tracker=github-issues).
- **#728** — #711's hardcoded `"dev/"` should use `client.feature_branch_prefix`
  SSOT (low blast radius — Linear-fallback path only).
- Commented **#716** with the "not intermittent, 2/2" evidence.

## #716/#724 — LOCKED ROOT CAUSE (this is the headline)

### Confirmed behavior (decisive evidence, 2/2 tickets)
The PLAN stage finishes its work in ~9-10 min (posts the plan to the issue),
emits its sentinel, then the worker goes silent — but cw does NOT advance the
stage. The session lingers until the ~30-min **wall-clock watchdog** times it
out, reverts the task to PENDING, re-dispatches, and only THEN advances.
~21-26 min wasted per stage, every dispatch.

Evidence #717: plan worker `ede37a16` (cw-session `d66a53d8`) — plan posted to
issue at **21:40:41Z**, transcript last write **21:41:09Z**, session marked
TIMED_OUT at **22:02:29Z** (21 min later), advanced to impl 22:11. Worker
emitted `AUTO_DEV_RESULT` with a completed/stage-advance status (grep of the
transcript confirms the sentinel block). #710: plan posted 00:00:03Z, advanced
~00:26 — same ~26 min gap. **NOT a budget problem — raising the timeout (#265)
makes it strictly worse.**

### Exact mechanism (the bug)
A staged worker's success/advance status is `stage_complete`
(`STAGE_SUCCESS_STATUSES`, `src/cw/auto_dev_result.py:104`). When the worker
emits it and the session is handled by reconcile:
- **`_salvage_terminal_result`** (`src/cw/reconcile/_shared.py:506-531`) only
  routes statuses in **`SALVAGE_TERMINAL_STATUSES`**
  (`src/cw/auto_dev_result.py:117-130` = shipped/no_op/`*_pending_approval`/
  merge_gate_blocked/scope_exceeded/forbidden_area + PAUSED_FOR_USER_INPUT).
  **`stage_complete` is NOT in that set** → the phantom/idle-salvage paths drop
  the advance sentinel.
- The only path that routes `stage_complete` is **`ROUTE_EMITTED_SENTINEL`**
  (`src/cw/reconcile/idle.py:152-176`), which uses the unfiltered
  `_parse_any_sentinel_from_transcript` — but it is gated by
  `session.surface_ref in native_live` (idle.py:146), i.e. **alive-in-roster
  workers only**.

So when a stage worker emits `stage_complete` and **exits** (surface leaves the
daemon roster — the staged engine spawns a fresh worker per stage, so the
plan worker exits after posting), it is handled by the **phantom path**, which
uses terminal-only salvage and **drops the `stage_complete` advance** → no
prompt advance → wall-clock timeout → revert → respawn → advance.

(Open sub-question the reproduction must settle: confirm the worker EXITS vs
stays idle-alive. If alive, ROUTE_EMITTED_SENTINEL should fire at the 300s
`sentinel_unrouted_check_seconds` mark — it didn't, so either it exited, or
there's a second gate. The fix below covers the exited case; verify the alive
case too.)

### THE FIX (spec)
Route intermediate (`stage_complete`) emitted sentinels for **exited/phantom**
DAEMON sessions through `apply_staged_decision` (advance the stage) — not only
terminal-status salvage. Concretely, in the phantom path
(`src/cw/reconcile/phantom.py` `_detect_phantom_candidates` /
`_act_on_phantom_candidates`, dispatched from `core.py`), before treating an
exited DAEMON session as a crashed phantom (revert/terminal-salvage), check
`_parse_any_sentinel_from_transcript(session)`; if it yields an `AutoDevResult`
with a non-terminal advance status (`stage_complete`), route it via
`_apply_sentinel_to_task(ticket_id, session.id, sentinel)` (which calls
`apply_staged_decision` → advance) and mark the session COMPLETED — mirroring
how `ROUTE_EMITTED_SENTINEL` does it for alive sessions.

Shared authority already exists: `apply_staged_decision`
(`src/cw/dispatch.py:1036`) and `_apply_sentinel_to_task`
(`_shared.py:559`, matches the task by ticket_id + `session_id == cw_session_id`
+ status RUNNING). **Watch the session-id match**: confirm the task's
`session_id` equals the emitting session's id (the #711 desync class can break
this — if mismatched, `_apply_sentinel_to_task` returns without advancing).

### Reproduction (TDD — write FIRST, must fail before the fix)
Model on `test_reconcile_crashed_phantom_salvages_shipped_sentinel`
(`tests/test_reconcile.py:1602`) but:
- payload status = `stage_complete` (need a `_stage_complete_payload()` helper;
  derive the minimal valid AutoDevResult — check `auto_dev_result.py` schema for
  required fields at the plan stage; `stage_reached`, `scope`, etc.),
- task at `stage=Stage.PLAN`, `status=RUNNING`,
- surface NOT in native_live (`_claude_agents_json` returns only a different
  live ref, as the existing test does),
- assert AFTER `reconcile()`: task advanced to `Stage.IMPL` and is still RUNNING
  (NOT reverted to PENDING, NOT timed out). This assertion FAILS today
  (it reverts/lingers) → that's the failing test. Then implement the fix → green.
Also add the alive-idle variant (surface IN native_live, past 300s) to confirm
ROUTE_EMITTED_SENTINEL covers `stage_complete` there too.

### Quality gates (run ALL before PR — CLAUDE.md / CI):
```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy --strict src/
uv run pre-commit run --all-files
uv run --extra mcp pytest tests/ -m 'not integration' --cov=cw --cov-report=xml --cov-fail-under=88
uv run pytest tests/ -m integration
uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90
```
Reap path is ADR-0006 safety-sensitive — cover every new branch incl. the
non-advance/error paths.

## Validation of what already shipped
- #717: `python3 .claude/skills/cw-smoke-test/scripts/preflight.py --ticket-id <open#>`
  → `cw_backend_healthy passed=True`, overall `ok=True` (was False). ✓ done.
- #710: `uv run pytest tests/test_worktree.py` (103 pass) + real-git regression
  test B in that file. ✓ done.
- #711: `uv run pytest tests/test_gh.py` (26 pass). ✓ done.

## Process lessons (operator-surfaced this session — IMPORTANT)
1. **Verify the worker transcript, not the dev-queue `status=running` proxy.**
   `status=running` hid a real Linear OAuth stall through two "healthy" reports.
   At each checkpoint, read the worker transcript (tool calls, errors, off-track
   MCP usage), not just the queue field.
2. The retitle-watcher CANNOT beat the merge-race — auto-merge snapshots the
   squash subject at arm-time. Stop using it; #722 is the real fix.
3. Even a harden sweep can propagate a stale ticket premise — #711 asserted the
   branch is `auto-dev/<id>` (AUTO_DEV_LABEL_PREFIX, the *session-name* prefix);
   the real branch is `dev/<id>` (`feature_branch_prefix`). When a ticket names a
   symbol as SSOT, verify it's what the runtime path actually uses.

## Driver scripts (reusable)
`/tmp/cw<NNN>-drive.sh` + `/tmp/cw<NNN>-state.py` (per-tick staged dispatch
driver; `: > LOG` truncates per re-arm so save logs you need). NOTE: drivers
report queue status only — per lesson #1, peek the worker transcript too.

## Next sprint items (after #716 fix)
Remaining pipeline-reliability: #722, #726 (both clear fixes). Then re-triaged
#636 (close vs downgrade) and #716-adjacent #265. Then standalone backlog
(#542 #630 #395 #588 #589 #590 #520 #519) and RFC 0005 C-chain
(#618 #620 #621 #622 #623 #626). Each dispatch costs ~2hr UNTIL #716 lands —
which is why the pipeline-fix pivot is correct.
```
