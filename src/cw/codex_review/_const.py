"""Module-level constants and reason vocabulary for the codex-review package.

The coarse per-role ``ReviewerRunFailure.reason`` codes, the transient-failure
set that drives ``Blocker.retry_eligible``, the shared-deadline loop floor, and
the fine-grained :class:`ExecutorFailureCategory` -> reason mapping. Imported by
``_roles`` (failure classification) and ``_verdict`` (disposition).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.auto_dev_result import StageReached
    from cw.executor_diagnostics import ExecutorFailureCategory

STAGE3_REVIEW: StageReached = "stage3_review"

# Per-role failure reason codes (Resolution 4: reuse the existing coarse
# vocabulary per role rather than building a new typed taxonomy). These are
# owned here, not re-exported by executor.py — callers (including
# tests/test_codex_executor.py) import them directly from cw.codex_review.
CODEX_TIMEOUT = "codex_timeout"
CODEX_ERROR = "codex_error"
CODEX_REVIEW_UNPARSEABLE = "codex_review_unparseable"
CODEX_MUST_FIX_FINDINGS = "codex_must_fix_findings"
CODEX_BUDGET_EXHAUSTED = "budget_exhausted"
# A partial review (some roles produced documents, but at least one selected
# role skipped or errored without one) blocks rather than silently shipping a
# reduced review pass — Decision 7 (#1236 finish spec).
CODEX_REVIEW_PARTIAL = "codex_review_partial"

# Standalone fix-loop park reason (#1464): a successful fix-cycle commit whose
# changed paths fall both outside the cycle-0 reviewed diff's file set AND
# match the sensitive-files registry. Deliberately NOT added to
# _CATEGORY_TO_REASON below — that dict maps ExecutorFailureCategory -> reason
# for codex-invocation failures, while this reason parks a
# successful-but-out-of-policy fix, a distinct axis.
CODEX_FIX_SCOPE_VIOLATION = "codex_fix_scope_violation"

# A review whose only MUST_FIX finding(s) were MECHANICALLY rejected — dropped
# by review_findings' validation (bad file/line anchor, evidence absent from
# the diff, ...) before any adjudication could weigh them on their merits
# (#1714). Sibling of CODEX_MUST_FIX_FINDINGS above, deliberately NOT the same
# reason: that one means "a real MUST_FIX survived validation and is open",
# this one means "something MUST_FIX-shaped was thrown away unread".
#
# The distinction is load-bearing for the fix loop. codex_fix_loop's entry gate
# reads ReviewVerdict.blocking, which stays False here by design — a finding
# rejected because its anchor could not be trusted must never be handed to a
# fix agent, which would ask codex to patch code the finding may not even
# describe. So this reason parks for an operator instead of autofixing.
# Same reason it is also NOT in _TRANSIENT_FAILURE_REASONS: retrying the
# identical review pass reproduces the identical rejection.
CODEX_MUST_FIX_MECHANICALLY_REJECTED = "codex_must_fix_mechanically_rejected"

# Failure reasons transient enough that a retry might succeed without any
# code/config change on our side (the role either never got a turn at all, or
# codex itself timed out) — used to set Blocker.retry_eligible so reconcile
# can self-heal instead of parking the ticket (MUST_FIX 2).
_TRANSIENT_FAILURE_REASONS = frozenset({CODEX_TIMEOUT, CODEX_BUDGET_EXHAUSTED})

# Shared-deadline loop floor (Comment 3): never hand codex a per-role timeout
# below this; a role that cannot get at least this much budget is skipped as
# budget-exhausted instead.
_MIN_ROLE_TIMEOUT_SECONDS = 30

# Maps the fine-grained ExecutorFailureCategory (#1239 diagnostics taxonomy)
# to the coarse ReviewerRunFailure.reason vocabulary above — the single source
# of truth _run_codex_role delegates to instead of independently re-deriving
# the same reason via its own branch walk (#1330 item 5). Total (all 9
# category members are explicit keys, no .get() fallback) so a future category
# addition fails loudly (see test_category_to_reason_mapping_is_total) rather
# than silently KeyError-ing at runtime.
#
# spawn_error and nonzero_exit both map to CODEX_ERROR — exactly what the old
# `elif result.returncode != 0` branch produced for both shapes, so
# spawn_error's retry-eligibility (excluded from _TRANSIENT_FAILURE_REASONS)
# is unchanged by this refactor. runtime_error and semantic_validation_failure
# are unreachable through _classify_codex_failure today (the former is
# aider-only; the latter is a reserved category with no live producer) — both
# get the closest semantically-adjacent reason purely so the dict is total.
_CATEGORY_TO_REASON: dict[ExecutorFailureCategory, str] = {
    "timeout": CODEX_TIMEOUT,
    "spawn_error": CODEX_ERROR,
    "nonzero_exit": CODEX_ERROR,
    "runtime_error": CODEX_ERROR,
    "missing_output": CODEX_REVIEW_UNPARSEABLE,
    "empty_output": CODEX_REVIEW_UNPARSEABLE,
    "invalid_json": CODEX_REVIEW_UNPARSEABLE,
    "schema_mismatch": CODEX_REVIEW_UNPARSEABLE,
    "semantic_validation_failure": CODEX_REVIEW_UNPARSEABLE,
}
