# A clean default-branch merge never causes a review; merge at most once, only on conflict

**Status:** Accepted
**Driven by:** #2979 (follow-ups #2980, and the narrowing of #1823, #2815, #2918)

## Decision

How a ticket branch meets `origin/<default>` follows three rules:

1. **Behind is not a reason to act.** A branch or PR that is merely behind the
   default branch is never merged, rebased, force-pushed or parked for that
   alone. GitHub merges a clean branch itself. Finalize Step 4c.5 treats
   `MERGEABLE` + `BEHIND` as no action (`behind_default_no_action`).
2. **Merge at most once, and only on a conflict.** Every site that used to
   merge or rebase on "behind" asks `cw worktree merge-probe` first and merges
   `origin/<default>` (a plain `git merge`, pushed with a plain push, never a
   rebase or a force-push) only when the probe reports a conflict. A merge that
   carries a conflict resolution is reviewed by a normal full round.
3. **A clean merge never causes a review.** The clean-mode tip rule tolerates a
   merge of `origin/<default>` exactly when git itself would write it
   (`git merge-tree --write-tree` of its parents reproduces its tree), whatever
   files it touches and on every loop backend.

## Invariant

1. **One probe, one spawner.** `cw.merge_probe.merge_tree` is the only
   spawner for the probe, the tip rule and the REVIEW gate. A separate,
   pre-existing caller, `repair_git._conflict_set` in
   `cw.reconcile.review_recipes`, runs its own `git merge-tree --write-tree
   --name-only` with its own parsing and error handling; it answers a
   different question (which files a repair must touch) and is deliberately
   left alone. `cw worktree merge-probe` exits 0 (`up_to_date` or
   `clean`: no merge), 1 (`conflict`: merge once and resolve) or 3
   (`unavailable`: do not merge). It is read-only and **never fetches**: every
   caller fetches `origin/<default>` first in the same fence and, if that fetch
   fails, does not probe (`merge_probe_unavailable`). Shell callers merge only
   when the exit code is 1 AND the JSON `state` is `conflict`: exit 1 is also
   what a crash yields, and a probe that never ran is not a conflict.
2. **"At most once" is structural, not a marker.** After one merge,
   `origin/<default>` is an ancestor of HEAD, so every later probe says
   `up_to_date`. A later, genuinely new conflict (main moved again) is still
   seen.
3. **No probe git under `dev_queue_lock`.** The `branch_behind_main` gate's
   probes run in `consume_completed_sessions` before the lock, against a
   lock-free queue snapshot, bounded per tick (20 probes, 30 s, 10 s per git
   call), and reach the in-lock predicate through a scope that ends with the
   locked section (ADR-0019's precompute pattern). This does not cover the
   REVIEW staleness gate (`review_gates` gate 3): its tip check runs git
   in-lock (rev-parse, merge-base, rev-list and one `git merge-tree
   --write-tree` per merge in the span). Every one of those calls has a 10 s
   timeout (`PROBE_TIMEOUT_SECONDS`), and the per-merge loop shares one 30 s
   wall-clock budget (`TIP_CHECK_BUDGET_SECONDS`); a timeout or a spent budget
   reads as stale (fail closed). There is no per-tick cap.
4. **The REVIEW gate fails open.** Anything but a probe-reported `conflict`
   (an error, a timeout, a skipped probe, no verdict) does not park.
5. **The tip rule fails closed.** A conflict resolution, a hand edit, an
   octopus, a merge of a non-default commit, a commit of the branch's own, a
   `merge-tree` timeout, a git older than 2.38 and any unreadable state stale
   the tip, and `verify-tip`'s `detail` names the first offending commit and
   the reason.

## What this means for callers

- `/prep-pr` Step 1, finalize's pre-PR sync, Stage 2's branch freshness, the
  review appendix's direct-execution fallback and the Python fix dispatch probe
  before they merge; `review-monitor` merges once only on a PR GitHub reports
  `DIRTY`.
- `verify-tip`, `finalize`, `converge init` / `status` and the REVIEW staleness
  gate share `default_merge_only` / `default_merge_rejection`; there is no
  loop-backend allow-list and no exempt-path list.
- The `branch_behind_main` disposition keeps its persisted name and now means
  "conflicts with the default branch".
- The delta confirm round (#2918) is retired: its only eligible shape is now
  always tolerated. `kind: delta` stays a readable historical round kind.

## Consequences

- **Strict-protection repos without a merge queue.** GitHub reports `BEHIND`
  only when up-to-date branches are required; there a `BEHIND` PR cannot merge
  and an armed auto-merge waits. The rule is unconditional, with no per-client
  key; the operator's "Update branch" button is a clean merge and never stales
  a ledger.
- **False conflicts are fail-closed.** A GitHub update-branch merge, a
  `rerere`-resolved merge, an author-local `-X` option or custom driver, or an
  ort difference across git versions can make a clean merge look hand-edited,
  so the tip reads stale and a normal round runs. Not overridable per merge;
  the only knob, `review.default_merge_tolerance: false`, is stricter.
- **`/prep-pr` has no Step 4c.5 backstop**, so its `unavailable` fallback
  (proceed with no merge) is weaker than finalize's.
- **The codex lane's post-loop reconcile stays strict**: a clean merge landing
  between a codex loop's end and its confirm round reopens the ledger. A narrow
  window that slightly contradicts rule 3, fail-closed.
- **`pr_hydrate` still treats `BEHIND` as `merge_blocked`** (Row 1, outside
  this rule): it mutates no branch but fires the `escalate_merge_block`
  escalation for a merely-BEHIND PR and masks the `ci_failing` /
  `changes_requested` recipes for it. A follow-up narrowing Row 1 is pending.
- **The REVIEW gate runs only on the consume path** and only for rows with a
  stamped `worktree_path`; the single-event settles and reconcile's recovery
  install no verdict (fail open), and finalize Step 4c.5 handles a conflict
  downstream.
- The review-staleness gate's in-lock tip check now runs one `merge-tree` per
  merge in the span, each git call under a 10 s timeout and the loop under one
  shared 30 s budget (invariant 3).
- **`verify-tip` and `finalize` refresh the default branch once.** They fetch
  only `origin <branch>`, so a clean merge of default-branch commits newer than
  the local `origin/<default>` (made by a human or GitHub's "Update branch")
  reads stale (`non_merge_commit`, `second_parent_not_on_default` or
  `no_default_ref`). For those three reasons alone they run one bounded
  `git fetch origin <default>` (10 s) and ask again once; a failed fetch keeps
  the stale verdict. `converge init` / `status` and the in-lock gate never
  fetch it, so they can still read such a merge as stale (fail closed).

## Alternatives considered

- **Review only the conflict resolution in this ticket.** Deferred to #2980: a
  conflict-resolution merge gets a normal full round today, which never skips a
  review.
- **A marker file recording "already merged once".** Rejected: the ancestry of
  `origin/<default>` already says so, and a marker would also block a
  legitimately new conflict after main moves again.
- **A per-client BEHIND policy key.** Rejected: the operator's rule is
  unconditional, and the strict-protection workaround is a clean merge.
- **Keeping the delta confirm round.** Rejected: under rule 3 its eligible
  shape is unreachable, so it would be dead code.

## Referenced by

- #2979, #2980, #1823, #2815, #2861, #2918
