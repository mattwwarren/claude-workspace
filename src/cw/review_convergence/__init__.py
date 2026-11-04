"""Converge-to-clean review state machine (#2787, ``review.convergence: clean``).

The prose command files cannot own round state: the fix loop is asynchronous
(``auto-dev-review.md`` Step 3b.2 records a pending dispatch and ends the
session, and a fresh REVIEW session resumes later). So the clean-mode review
loop is deterministic, unit-tested, pure Python in this package, persisted as a
ledger at ``<worktree>/.cw/review-convergence.json``, and exposed to the prose
through the ``cw review converge`` subcommands (``cw.cli.review``).

Every engine function here is pure: it takes a ledger and returns an updated
copy, and reads no clock (callers pass ``now`` as an ISO-8601 UTC string). The
I/O is confined to ``_store`` (the ledger file), the two git readers in
``_release`` (:func:`history_rewritten`, :func:`worktree_state`), the
default-branch merge tolerance predicate in ``_merge_tolerance`` and the diff
base helpers in ``_diff_base``. Failure
direction is always "more review, never a skipped one": an unread finding, a
role that never reviewed, an unconfirmed loop round and an unverified tip are
all refused, never recorded as clean.

Submodules, each importing only the ones above it:

- ``_identity`` -- :class:`FindingIdentity`, the ``(severity, file, normalized
  summary)`` anchor (the voided matcher's fingerprint minus ``evidence``).
- ``_models`` -- the ledger, its entries, the request/report envelopes and the
  vocabulary, plus :class:`ConvergenceError`.
- ``_backend`` -- :func:`resolve_confirm_backend` and
  :func:`clean_mode_supported`.
- ``_policy`` -- ``max_cycles_exhausted``, ``release_cause``, the no-progress
  and oscillation detectors, ``consecutive_confirm_failures``.
- ``_findings`` -- :func:`from_verdict`, :func:`unread_from_verdict`,
  :func:`shard_tally_from_verdict`, the new-accepted classification,
  :func:`suppress_settled` and the SHOULD_FIX deferral helpers
  :func:`decided_finding` / :func:`decide_verdict`.
- ``_engine`` -- :func:`record_round`, :func:`record_confirm_failure`,
  :func:`operator_settle`, :func:`new_ledger`.
- ``_tip`` -- :func:`finalize_ready`, :func:`evaluate_tip` (the final-sha
  rule, strict by default), :func:`demote_for_rereview`.
- ``_render`` -- history comment, friction lines, stop summary, sentinel and
  blocker fragments, audit-event payload.
- ``_store`` -- atomic ledger and per-round verdict persistence (and the
  per-round verdict reader, :func:`load_round_verdict`).
- ``_merge_tolerance`` -- :class:`MergeTolerance` and its two readers
  (:func:`tolerance_from_context`, :func:`tolerance_for_client`), the git
  predicate :func:`default_merge_only` (#2815), and the keep-converged seam
  :func:`converged_across_tolerated_merge` / :func:`tolerates_review_move`
  that ``converge init``, ``converge status`` and the REVIEW staleness gate
  share with ``verify-tip`` (#2861).
- ``_delta`` -- :func:`assess_delta_round` and its :class:`DeltaSpec` /
  :class:`DeltaPlan` / :class:`DeltaAssessment` (#2918): whether a delta
  confirm round may record a default-branch merge that overlaps the
  branch's files, and what that round reviews.
- ``_release`` -- :func:`reconcile_for_init` (the shared ``init`` entry point,
  which builds the fresh ledger with ``_engine``'s :func:`new_ledger`),
  :func:`reconcile_ledger_for_init`, and the git readers
  :func:`history_rewritten` and :func:`worktree_state` (#2791).
- ``_diff_base`` -- :func:`resolve_diff_base`, :func:`refresh_diff_base`,
  :func:`verify_diff_base` and :func:`resolve_loop_diff_base` (the codex fix
  loop's non-fetching variant, #2823): the one review diff base the loop
  rounds, ``consolidate --base`` and the confirm round share (#2800).
- ``_status`` -- :func:`next_action` and :func:`status_summary`.

``load_ledger`` raises :class:`ValueError` subclasses (``pydantic
.ValidationError``, ``UnicodeDecodeError``) on a corrupt file; the CLI maps
them to :class:`cw.exceptions.CwError`. :class:`ConvergenceError` already is
one.
"""

from __future__ import annotations

from cw.review_convergence._backend import (
    ResolvedBackend,
    clean_mode_supported,
    resolve_confirm_backend,
)
from cw.review_convergence._delta import (
    DeltaAssessment,
    DeltaPlan,
    DeltaSpec,
    assess_delta_round,
)
from cw.review_convergence._diff_base import (
    refresh_diff_base,
    resolve_diff_base,
    resolve_loop_diff_base,
    verify_diff_base,
)
from cw.review_convergence._engine import (
    CONFIRM_UNAVAILABLE_CAP,
    new_ledger,
    operator_settle,
    record_confirm_failure,
    record_round,
)
from cw.review_convergence._findings import (
    classify_findings,
    decide_verdict,
    decided_finding,
    from_verdict,
    frozen_counts_from_verdict,
    round_input_findings,
    shard_tally_from_verdict,
    suppress_settled,
    unread_from_verdict,
)
from cw.review_convergence._identity import FindingIdentity, finding_identity
from cw.review_convergence._merge_tolerance import (
    MergeTolerance,
    converged_across_tolerated_merge,
    default_merge_only,
    overlapping_default_merge,
    rev_parse_or_none,
    tolerance_for_client,
    tolerance_from_context,
    tolerates_review_move,
)
from cw.review_convergence._models import (
    CONFIRM_CAUSE_BACKEND_ERROR,
    CONFIRM_CAUSE_BINARY_MISSING,
    CONFIRM_CAUSE_CRASH,
    CONFIRM_FAILURE_CAUSES,
    DECISION_CLEAN,
    DECISION_STOP_CONFIRM_ESCALATED,
    DEFAULT_MERGE_TOLERATED_KEY,
    DELTA_CONFIRM_ELIGIBLE_KEY,
    DELTA_CONFIRM_PENDING_KEY,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    LEDGER_STATUS_STOPPED,
    OVERLAPPING_FILES_KEY,
    STALL_NOISE_FLOOR,
    STALL_WINDOW,
    STOP_DECISIONS,
    STOP_MAX_CYCLES,
    STOP_NO_PROGRESS,
    STOP_OPERATOR_ACTION,
    STOP_OSCILLATION,
    TIP_CHECK_FETCH_FAILED,
    ConfirmBackend,
    ConfirmFailure,
    ConvergenceError,
    ConvergenceLedger,
    Decision,
    FixedEntry,
    FrozenCounts,
    GatingSeverity,
    LedgerStatus,
    OperatorSettleReport,
    Reconciled,
    RecordableRoundKind,
    ReleaseCause,
    RoundFinding,
    RoundInput,
    RoundKind,
    RoundRecord,
    RoundReport,
    SettledEntry,
    SettleTarget,
    StopRelease,
    TipDecision,
    TipReason,
    TipVerdict,
    UnmatchedTarget,
    UnreadFindings,
)
from cw.review_convergence._policy import (
    consecutive_confirm_failures,
    detect_no_progress,
    detect_oscillation,
    max_cycles_exhausted,
    release_cause,
)
from cw.review_convergence._release import (
    history_rewritten,
    reconcile_for_init,
    reconcile_ledger_for_init,
    worktree_state,
)
from cw.review_convergence._render import (
    confirm_blocker_fragment,
    friction_lines,
    one_line,
    operator_settle_event_payload,
    render_history_comment,
    sentinel_review_fragment,
    settle_payload,
    stop_summary,
)
from cw.review_convergence._status import NextAction, next_action, status_summary
from cw.review_convergence._store import (
    LEDGER_RELATIVE_PATH,
    ROUND_VERDICT_DIR_RELATIVE_PATH,
    load_ledger,
    load_round_verdict,
    round_verdict_path,
    save_ledger,
    save_round_verdict,
)
from cw.review_convergence._tip import demote_for_rereview, evaluate_tip, finalize_ready

__all__ = [
    "CONFIRM_CAUSE_BACKEND_ERROR",
    "CONFIRM_CAUSE_BINARY_MISSING",
    "CONFIRM_CAUSE_CRASH",
    "CONFIRM_FAILURE_CAUSES",
    "CONFIRM_UNAVAILABLE_CAP",
    "DECISION_CLEAN",
    "DECISION_STOP_CONFIRM_ESCALATED",
    "DEFAULT_MERGE_TOLERATED_KEY",
    "DELTA_CONFIRM_ELIGIBLE_KEY",
    "DELTA_CONFIRM_PENDING_KEY",
    "LEDGER_RELATIVE_PATH",
    "LEDGER_STATUS_AWAITING_CONFIRM",
    "LEDGER_STATUS_CONVERGED",
    "LEDGER_STATUS_IN_PROGRESS",
    "LEDGER_STATUS_STOPPED",
    "OVERLAPPING_FILES_KEY",
    "ROUND_VERDICT_DIR_RELATIVE_PATH",
    "STALL_NOISE_FLOOR",
    "STALL_WINDOW",
    "STOP_DECISIONS",
    "STOP_MAX_CYCLES",
    "STOP_NO_PROGRESS",
    "STOP_OPERATOR_ACTION",
    "STOP_OSCILLATION",
    "TIP_CHECK_FETCH_FAILED",
    "ConfirmBackend",
    "ConfirmFailure",
    "ConvergenceError",
    "ConvergenceLedger",
    "Decision",
    "DeltaAssessment",
    "DeltaPlan",
    "DeltaSpec",
    "FindingIdentity",
    "FixedEntry",
    "FrozenCounts",
    "GatingSeverity",
    "LedgerStatus",
    "MergeTolerance",
    "NextAction",
    "OperatorSettleReport",
    "Reconciled",
    "RecordableRoundKind",
    "ReleaseCause",
    "ResolvedBackend",
    "RoundFinding",
    "RoundInput",
    "RoundKind",
    "RoundRecord",
    "RoundReport",
    "SettleTarget",
    "SettledEntry",
    "StopRelease",
    "TipDecision",
    "TipReason",
    "TipVerdict",
    "UnmatchedTarget",
    "UnreadFindings",
    "assess_delta_round",
    "classify_findings",
    "clean_mode_supported",
    "confirm_blocker_fragment",
    "consecutive_confirm_failures",
    "converged_across_tolerated_merge",
    "decide_verdict",
    "decided_finding",
    "default_merge_only",
    "demote_for_rereview",
    "detect_no_progress",
    "detect_oscillation",
    "evaluate_tip",
    "finalize_ready",
    "finding_identity",
    "friction_lines",
    "from_verdict",
    "frozen_counts_from_verdict",
    "history_rewritten",
    "load_ledger",
    "load_round_verdict",
    "max_cycles_exhausted",
    "new_ledger",
    "next_action",
    "one_line",
    "operator_settle",
    "operator_settle_event_payload",
    "overlapping_default_merge",
    "reconcile_for_init",
    "reconcile_ledger_for_init",
    "record_confirm_failure",
    "record_round",
    "refresh_diff_base",
    "release_cause",
    "render_history_comment",
    "resolve_confirm_backend",
    "resolve_diff_base",
    "resolve_loop_diff_base",
    "rev_parse_or_none",
    "round_input_findings",
    "round_verdict_path",
    "save_ledger",
    "save_round_verdict",
    "sentinel_review_fragment",
    "settle_payload",
    "shard_tally_from_verdict",
    "status_summary",
    "stop_summary",
    "suppress_settled",
    "tolerance_for_client",
    "tolerance_from_context",
    "tolerates_review_move",
    "unread_from_verdict",
    "verify_diff_base",
    "worktree_state",
]
