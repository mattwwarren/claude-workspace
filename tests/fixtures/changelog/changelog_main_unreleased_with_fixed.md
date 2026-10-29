# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The `cw-smoke-test` Step 3 completion poll no longer fails on its first empty read (#1119).** `cw event tail --json` prints the literal `[]` when no event matches yet, which is the normal state while the worker runs, and the skill's `jq -r 'select(.payload.session_id == ...)'` exited 5 (`Cannot index array with string "payload"`). The filter now starts with `objects |`, which skips the `[]` and still returns the matching event. A new test runs the filter from the skill file through `jq` for the empty, matching and non-matching cases. Reported with a reproduction by an outside contributor on #1119.

## [1.78.1] - 2026-10-25

### Fixed

- **A large ticket on a codex review lane parks for review approval instead of shipping without one (#2767).** The codex review set its tier from `scope_hint` alone, so a ticket that was large by its plan but had no hint completed review as small and went on to finalize. It now resolves the tier in the Claude review's order (the `**Scope tier:**` line in `.cw/plan.md`, then `scope_hint`, then the measured diff, small being at most 10 files and 500 lines), parks a large ticket as `review_pending_approval`, and blocks as `scope_tier_unresolvable` when no source yields a tier. `cw dev-queue approve` on a review gate now records who approved which reviewed head on the row (`review_approved_at`, `review_approver_kind`, `review_approved_sha`; dev-queue schema v48, no migration needed), prints it, and passes it to the worker as `queue_metadata.review_approval` (cw-context schema v14). The row is saved first and the audit event is best-effort: if the event cannot be written the approval stands, a warning is logged, and `approve` says so and exits 0. The `dispatch.scope_routing_decision` `gate_release` event gains `approver_kind` and `reviewed_sha`, which outlive the row record when a regress clears it. A new headless finalize Step 4.0 runs before a PR is created or an open one is armed: it ships a large ticket only when that approval is recorded and its reviewed head is still in the branch history with nothing but merges of the default branch after it, and otherwise parks as `review_pending_approval`. `approve` is still refused at finalize; `cw dev-queue show` lists the release, the operator-only `requeue --stage review`, which returns the ticket to review for a fresh approval. `auto_approve_clean_review` releases a review gate only when the review's measured numbers confirm the large tier and no `scope_hint` says small; a ticket that is large by its plan line alone is left for the orchestrator. A large ticket approved before this change, or with a commit after its approval other than a default-branch merge, parks once at finalize and goes back through review.

