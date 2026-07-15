# cw never grants a GitHub review approval

**Status:** Accepted
**Driven by:** #1199 (and #1195, the case that made this property load-bearing)

## Decision

cw does not, and must never, call any GitHub API path that grants a
pull-request review approval. This is now a tested invariant
(`tests/test_review_approval_guard.py`), not an emergent property of the
current code.

## Invariant

1. No code path in `src/` invokes `gh pr review` with `--approve`.
2. No code path in `src/` invokes the GraphQL `addPullRequestReview`
   mutation with `event: APPROVE`.
3. No code path in `src/` invokes REST `POST /pulls/{n}/reviews` with
   `"event": "APPROVE"`.
4. `auto_approve_clean_review` (RFC 0009, `gate_recipes.py`) and any
   future `auto_approve_*`-named gate recipe refers exclusively to cw's
   internal dev-queue dispatch gate (`_approve_ticket_locked`) — never to
   GitHub review state. `gh pr merge --auto` (arming auto-merge on an
   already-human-approved PR) is not an approval and is unaffected by
   this invariant.
5. There is no escape hatch. No config flag, per-lane override, or scoped
   exception may reintroduce an approving-review call path. If a
   legitimate future need arises, it requires a new ADR that explicitly
   supersedes this one — not a flag on this one.

## What this means for callers

- Any future code that requests a review (`add_pr_reviewer` in `gh.py`),
  posts a comment, or arms auto-merge (`gh pr merge --auto` in
  `salvage.py`) is unaffected — those are not approvals.
- Any future PR adding a `gh pr review --approve` call, a GraphQL
  `addPullRequestReview`/`APPROVE` mutation, or a REST
  `POST /pulls/{n}/reviews` with `"event": "APPROVE"` call anywhere under
  `src/` fails `tests/test_review_approval_guard.py` and cannot merge.
- `gate_recipes.py`'s `auto_approve_clean_review` config key is NOT
  renamed (see Alternatives) — its module docstring and the comment above
  `RECIPE_AUTO_APPROVE_REVIEW` carry the disambiguation instead.

## What this means for producers

- `cw.reconcile.gate_recipes` is the module most likely to be mistaken
  for a producer of GitHub approvals (`auto_approve_clean_review`'s
  name); it explicitly documents that it is not one, at both the module
  docstring and the constant definition.
- `cw.gh` (the module owning every `gh` subprocess call) and
  `cw.reconcile.salvage` (the module owning `gh pr merge --auto` and
  `gh pr create`) are the modules any future GitHub-review-adjacent
  change is most likely to land in; both are covered by the guard test's
  `src/` scan.

## Consequences

- A genuinely legitimate future need for cw to grant a review approval
  (none currently envisioned) requires an ADR that explicitly supersedes
  this one, not a quiet code change — by design, this is a cost, not a
  gap.
- The guard test is a deny-list source scan, not a semantic/AST analysis;
  it can in principle be evaded by sufficiently obfuscated string
  construction. This is accepted as proportionate (per the ticket): the
  correct number of approving-review call sites is zero, and a
  present-tense evasion is a far less likely failure mode than the
  straightforward "someone adds `--approve` to unblock a stuck merge"
  scenario in #1195.

## Alternatives considered

- **Rename `auto_approve_clean_review`** to something that cannot be
  misread as a GitHub approval (e.g. `auto_clear_review_gate`). Rejected
  for this ticket: it is a documented operator-facing config key
  (`config/CONFIG_REFERENCE.md`); a silent rename would make a
  previously-armed lane's config key stop matching and silently go inert
  — the same failure shape as #1200. If a rename is wanted, it needs its
  own ticket with a deprecation-alias design so an old key does not
  silently no-op.
- **A config-flag escape hatch** (e.g. `allow_review_approval: true`
  per-lane). Rejected per the ticket's own stated preference ("Preferably:
  never") and R6: a flag that "quietly flips it" is exactly the failure
  mode this ADR exists to prevent.
- **A CI grep step instead of a pytest test.** Rejected: no CI-level grep
  gate exists in this repo's `.github/workflows/*.yml`; the precedented
  pattern for this kind of structural invariant is a repo-root-relative
  pytest source scan (`tests/test_runtime_paths.py`, `test_preflight.py`,
  `test_skill_script_bootstrap.py`), which already rides CI via the
  existing pytest gate.

## Referenced by

- #1199, #1195
