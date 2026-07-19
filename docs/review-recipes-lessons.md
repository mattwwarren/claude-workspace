# Review-Recipes Lesson Index (RFC 0010 P5, #1100)

Durable audit of every operational lesson accumulated by the retired
`.claude/scripts/review_monitor.py` + `/review-monitor` skill against the code
that replaced them in `cw`: the ported attention-state / CI-summary logic in
[`src/cw/pr_hydrate.py`](../src/cw/pr_hydrate.py) and the review-recipe
automation layer in
[`src/cw/reconcile/review_recipes/__init__.py`](../src/cw/reconcile/review_recipes/__init__.py).

Source of the lessons: `global-claude/wiki/review-monitor.md` — **47** dated
`##`-level entries (each tagged `[session:<id>, <date>]`). This index enumerates
**all 47**; none is silently dropped. Every lesson carries a disposition and a
link (a `file:line` guard + `# Why:` comment, a regression test name, or an
`N/A` reason).

## Dispositions

- **APPLIES + GUARDED** — the failure mode is live in the ported design and is
  already prevented by a guard clause. P5 annotated that guard with a
  self-contained `# Why:` comment (citing the lesson by session id, no
  global-claude checkout needed) and pinned it with a regression test.
- **N/A — dropped subsystem** — the lesson is about a review_monitor subsystem
  cw deliberately did **not** port (delta-review baselines, `register`/
  `discover`, the argparse CLI surface, nudge/comms queues, draft-promotion,
  the comment-review classifier, reviewer-role logic, the cron/shell runtime,
  the standalone state file). Per RFC W6, dropped subsystems are **not** rebuilt
  to make a lesson "apply." No guard to add.
- **N/A — structurally prevented** — the failure class cannot occur in cw's
  design (e.g. state-file write races are eliminated by the `dev_queue_lock()`
  flock, which serializes every writer).
- **N/A — runtime / environment** — a macOS/Python-version/shell-portability
  note about the retired script that has no analogue in cw (runs under
  `uv` / Python 3.11+).
- **N/A — not code / design preserved** — a repo-workflow note, or a scope
  boundary cw intentionally keeps.

## Applied lessons (detail)

Five lessons map onto ported code. All were already guarded (their regression
tests pass on first write), so they are class "applies + guarded" — P5 added the
`# Why:` comment + a regression pin, not new logic.

1. **Draft PRs must not enter attention/escalation paths** `[session:fc766c55]`
   — guard: `pr_hydrate._compute_attention_state` Row 0 (`if is_draft: return
   None`), `src/cw/pr_hydrate.py`. Tests:
   `test_pr_hydrate.py::TestAttentionState::test_row0_draft_returns_none`,
   `::test_row0_gates_row4_draft_zero_reviewers`, and the recipe-layer
   `test_reconcile_review_recipes.py::test_draft_pr_never_a_candidate`.
2. **`changes_requested` fires on top-level `reviewDecision`**
   `[session:1a93541b]` — guard: `_compute_attention_state` Row 3
   (`review_decision == "CHANGES_REQUESTED"`). cw has no inline-thread
   subsystem, so this top-level signal is the sole `changes_requested` trigger.
   Tests: `test_pr_hydrate.py::TestAttentionState::test_row3_changes_requested`,
   `test_reconcile_review_recipes.py::
   test_detect_address_review_only_changes_requested_positive`.
3. **Abandoned PR auto-completion** `[session:94a665a5]` — guard:
   `pr_hydrate._is_candidate` excludes `_TERMINAL_PR_STATES` (MERGED/CLOSED), the
   same predicate the review-recipe detect phase reuses, so no recipe fires on a
   merged/abandoned PR. cw's analogue of review_monitor auto-completing such PRs
   out of the queue. Tests:
   `test_pr_hydrate.py::TestCandidateSelection::test_skips_closed_pr_state`,
   `test_reconcile_review_recipes.py::test_closed_pr_never_a_candidate` (added by
   P5).
4. **Stale git worktrees cause `check` to fail silently** `[session:8f738500]` —
   review_monitor's git diff/fetch against a deleted worktree failed **silently**.
   cw's analogue guard (`review_recipes._prepare_dispatch_job`, `wt is None or
   not wt.exists()`) fails **loud**: an absent worktree emits a durable
   `PR_ACTION_FAILED` correction rather than dispatching against nothing. Test:
   `test_reconcile_review_recipes.py::test_missing_worktree_emits_pr_action_failed`.
5. **`mergeStateStatus` can read `UNKNOWN` immediately after push/rebase**
   `[session:826a27f3]` — GitHub computes `mergeStateStatus` asynchronously.
   Guard: `pr_hydrate._ROW1_MERGE_BLOCKING_STATES` is a strict allow-list
   (`{DIRTY, BEHIND}`), so a transient `UNKNOWN` never reads as `merge_blocked`
   and cannot misfire `escalate_merge_block`; the next poll re-hydrates the real
   value. Test (added by P5): `test_pr_hydrate.py::TestAttentionState::
   test_row1_unknown_merge_state_not_merge_blocked`.

## Full enumeration (all 47, in wiki order)

1. **Self-comment handling** `[cbba269e]` — N/A — dropped subsystem
   (comment-review classifier; cw reads only `reviewDecision`, no COMMENTED
   classification path).
2. **Auto-merge disable actor from GitHub timeline** `[cbba269e]` — N/A —
   dropped subsystem (auto-merge re-arm / timeline-actor inspection).
3. **global-claude repo is trunk-based** `[cbba269e]` — N/A — not code
   (repo-workflow note for the global-claude repo, not `cw`).
4. **Draft PRs must not enter attention/escalation paths** `[fc766c55]` —
   **APPLIES + GUARDED** — `_compute_attention_state` Row 0; see Applied #1.
5. **`review_monitor.py register` CLI arg failure** `[fc766c55]` — N/A — dropped
   subsystem (`register`/`status` argparse CLI).
6. **Nudge grace period must fail closed** `[f30c6232]` — N/A — dropped
   subsystem (nudge / channel-bump scheduling).
7. **`--threads` flag is space-separated** `[f30c6232]` — N/A — dropped
   subsystem (`register --threads` inline-thread tracking).
8. **Re-review baseline anchoring** `[f30c6232]` — N/A — dropped subsystem
   (delta-review baseline / `commit_id` anchoring).
9. **Cron cycle budget exhaustion with many PRs** `[fc766c55]` — N/A — dropped
   subsystem (the `$5` review-monitor cron; cw has no per-cycle budget).
10. **Desktop action queue pattern** `[f30c6232]` — N/A — dropped subsystem
    (file-based desktop comms queue).
11. **`docs/` branch prefix: hold as draft** `[310b1657]` — N/A — dropped
    subsystem (draft→ready auto-promotion).
12. **same-cycle rebase dispatch** `[af5a0fdc]` — N/A — dropped subsystem
    (draft-promotion + `dispatch_rebase`).
13. **`changes_requested` fires on top-level `reviewDecision`** `[1a93541b]` —
    **APPLIES + GUARDED** — `_compute_attention_state` Row 3; see Applied #2.
14. **Comment reviews as fallback signal** `[76ded467]` — N/A — dropped
    subsystem (COMMENTED-review Haiku classifier + precedence chain; the
    `_compute_attention_state` docstring explicitly drops the comment-review
    inputs).
15. **Stacked draft PRs — nested chain holds** `[7fb1ab1c]` — N/A — dropped
    subsystem (draft-promotion base-branch chaining).
16. **`delta_base_sha` two-field baseline design** `[8a68ce09]` — N/A — dropped
    subsystem (delta-review baseline).
17. **`cmd_check` unconditionally advances `last_seen_sha`** `[90a2cdda]` — N/A —
    dropped subsystem (delta-review baseline; the destructive-read bug class
    does not exist without `last_seen_sha`).
18. **Delta diff silently fails when SHAs not in local repo** `[90891341]` — N/A
    — dropped subsystem (delta-review `git diff` baseline).
19. **`status --all --json` structure and size limit** `[90a2cdda / b50774fe]` —
    N/A — dropped subsystem (argparse CLI + JSON stdout contract).
20. **`git cat-file blob` vs `git show`** `[90a2cdda]` — N/A — dropped subsystem
    (per-SHA file-read helper for delta review).
21. **Deferred threads must create Linear follow-up tickets** `[9db889e9]` — N/A
    — dropped subsystem (thread-deferral → Linear ticket creation).
22. **Abandoned PR auto-completion** `[94a665a5]` — **APPLIES + GUARDED** —
    `_is_candidate` terminal-state exclusion; see Applied #3.
23. **`status --json` requires `--all` or `--repo`** `[2a3177d6]` — N/A — dropped
    subsystem (argparse CLI).
24. **`check` requires `--repo` flag** `[298a3921 / b2da7beb]` — N/A — dropped
    subsystem (argparse CLI + parallel-batch invocation).
25. **Parallel `confirm-thread` calls cause state loss** `[bc39649d]` — N/A —
    structurally prevented (cw persists via `dev_queue_lock()`; the `.tmp`→rename
    write race cannot occur under the flock).
26. **SonarCloud HTTP 403 can be a spurious diff artifact** `[6905f71e]` — N/A —
    not code (external CI-diagnosis heuristic; no cw subsystem).
27. **`review_monitor.py` requires Python 3.11+ — shebang fix** `[89ea14fb]` —
    N/A — runtime/environment (cw runs under `uv` / Python 3.11+; no macOS
    shebang path).
28. **`enqueue-action` shell JSON quoting is fragile** `[a935dd30]` — N/A —
    dropped subsystem (`enqueue-action` CLI / desktop queue).
29. **`pending-channel-bumps` does NOT accept `--all`** `[b2dbd911]` — N/A —
    dropped subsystem (channel-bump CLI).
30. **Delta baseline mis-anchoring: `--sha` must be the review's `commit_id`**
    `[f8664baf]` — N/A — dropped subsystem (delta-review baseline).
31. **Tilde path not expanded in `subprocess.run`** `[f8664baf]` — N/A —
    runtime/environment (script path-expansion pitfall; cw does not shell out to
    `review_monitor.py`).
32. **reviewer-role PRs: `nudge_ok`/`open_threads`/`all_addressed` return
    `null`** `[c93b78c3]` — N/A — dropped subsystem (reviewer-role approval/nudge
    logic).
33. **GitHub inline comments: `line: null` signals outdated anchor**
    `[a62e5d45]` — N/A — dropped subsystem (inline-comment thread fetching).
34. **review-monitor config file `~/.claude/review-monitor/config.yaml`**
    `[c3e62cf9]` — N/A — dropped subsystem (standalone YAML config; cw uses
    `OrchestratorConfig`).
35. **parallel `mark-notified` calls corrupt state file** `[83ef70bf]` — N/A —
    structurally prevented (`dev_queue_lock()` serializes writers).
36. **State file corruption recovery** `[8f738500]` — N/A — structurally
    prevented (`dev_queue_lock()`; no standalone `.tmp`→rename state file to
    corrupt).
37. **Stale git worktrees cause `check` to fail silently** `[8f738500]` —
    **APPLIES + GUARDED** — `_prepare_dispatch_job` missing-worktree fails loud;
    see Applied #4.
38. **No merge-conflict auto-resolve** `[89ee1309]` — N/A — design preserved: cw
    likewise does not auto-rebase/resolve conflicts. A `DIRTY` merge state →
    `merge_blocked` → `escalate_merge_block` **emits an escalation event**
    (`review_recipes._fire_escalate_merge_block`), it does not resolve.
39. **`review_monitor_cron.sh`: GNU-only `stat`/`date` flags** `[89ee1309]` — N/A
    — dropped subsystem (cron shell wrapper).
40. **`discover` filters by creation date, not last activity** `[89ee1309]` —
    N/A — dropped subsystem (`discover` PR-onboarding).
41. **7-day activity gate design** `[89ee1309]` — N/A — dropped subsystem
    (`discover` / stale-activity gate).
42. **`autoMergeRequest` is null for merge-queue repos** `[89ee1309]` — N/A —
    dropped subsystem (auto-merge-enabled detection).
43. **Author self-feedback via inline comments on own PR** `[a62e5d45]` — N/A —
    dropped subsystem (author-role COMMENTED-review inline-body classifier).
44. **`review_monitor.py check` output is mixed log+JSON** `[d7391ebe]` — N/A —
    dropped subsystem (CLI stdout parsing).
45. **`check` and `status --json` return different field sets** `[826a27f3]` —
    N/A — dropped subsystem (CLI per-subcommand schemas).
46. **`mergeStateStatus` can read `UNKNOWN` immediately after push/rebase**
    `[826a27f3]` — **APPLIES + GUARDED** — `_ROW1_MERGE_BLOCKING_STATES`
    allow-list; see Applied #5.
47. **`review_monitor.py` invocations must use `python3.11` explicitly**
    `[eb126265]` — N/A — runtime/environment (macOS interpreter selection; cw
    runs under `uv` / Python 3.11+).

## Notes for reviewers

- The five "applies" guards that exist in the ported code (#4, #13, #22, #37,
  #46) were **already correct** before P5 — this phase is an audit + backfill:
  it added `# Why:` provenance comments and two regression pins
  (`test_closed_pr_never_a_candidate`,
  `test_row1_unknown_merge_state_not_merge_blocked`); it added **no new recipe
  logic** and rebuilt **no** dropped subsystem.
- Two ported guards are strictly *stronger* than the review_monitor original:
  #22 (terminal PRs are structurally excluded, not auto-completed after the
  fact) and #37 (a missing worktree fails loud with `PR_ACTION_FAILED` rather
  than the silent `git` failure the lesson describes).
- Cross-subsystem caveat (out of P5 scope, no code change): the #46 `UNKNOWN`
  transient is fully contained *within* the review-recipe layer (none of the
  four recipes fire on it). The separate `gate_recipes` auto-approve path keys
  off `ready_to_approve`; its own poll re-hydration is the mitigation there, not
  this allow-list. Flagged here for traceability only.
