"""Converge-to-clean review state machine (#2787, ``review.convergence: clean``).

The prose command files cannot own round state: the fix loop is asynchronous
(``auto-dev-review.md`` Step 3b.2 records a pending dispatch and ends the
session, and a fresh REVIEW session resumes later). So the clean-mode review
loop is deterministic, unit-tested, pure Python in this package, persisted as a
ledger at ``<worktree>/.cw/review-convergence.json``, and exposed to the prose
through the ``cw review converge`` subcommands (``cw.cli.review``).

Every engine function here is pure: it takes a ledger and returns an updated
copy, and reads no clock (callers pass ``now`` as an ISO-8601 UTC string). The
I/O is confined to ``_store`` (the ledger file) and the two git readers in
``_release`` (:func:`history_rewritten`, :func:`worktree_state`). Failure
direction is always "more review, never a skipped one": an unread finding, a
role that never reviewed, an unconfirmed loop round and an unverified tip are
all refused, never recorded as clean.

Submodules, each importing only the ones above it:

- ``_identity`` -- :class:`FindingIdentity`, the ``(severity, file, normalized
  summary)`` anchor (the voided matcher's fingerprint minus ``evidence``).
- ``_models`` -- the ledger, its entries, the request/report envelopes and the
  vocabulary, plus :class:`ConvergenceError`.
- ``_backend`` -- :func:`resolve_confirm_backend`.
- ``_policy`` -- ``max_cycles_exhausted``, ``release_cause``, the no-progress
  and oscillation detectors, ``consecutive_confirm_failures``.
- ``_findings`` -- :func:`from_verdict`, :func:`unread_from_verdict`, the
  new-accepted classification and :func:`suppress_settled`.
- ``_engine`` -- :func:`record_round`, :func:`record_confirm_failure`,
  :func:`operator_settle`, :func:`new_ledger`.
- ``_tip`` -- :func:`finalize_ready`, :func:`evaluate_tip` (the strict
  final-sha rule), :func:`demote_for_rereview`.
- ``_render`` -- history comment, friction lines, stop summary, sentinel and
  blocker fragments, audit-event payload.
- ``_release`` -- :func:`reconcile_ledger_for_init`, and the git readers
  :func:`history_rewritten` and :func:`worktree_state` (#2791).
- ``_store`` -- atomic ledger and per-round verdict persistence.
- ``_status`` -- :func:`next_action` and :func:`status_summary`.

``load_ledger`` raises :class:`ValueError` subclasses (``pydantic
.ValidationError``, ``UnicodeDecodeError``) on a corrupt file; the CLI maps
them to :class:`cw.exceptions.CwError`. :class:`ConvergenceError` already is
one.
"""

from __future__ import annotations

from cw.review_convergence._backend import ResolvedBackend, resolve_confirm_backend
from cw.review_convergence._engine import (
    CONFIRM_UNAVAILABLE_CAP,
    new_ledger,
    operator_settle,
    record_confirm_failure,
    record_round,
)
from cw.review_convergence._findings import (
    classify_findings,
    from_verdict,
    frozen_counts_from_verdict,
    round_input_findings,
    suppress_settled,
    unread_from_verdict,
)
from cw.review_convergence._identity import FindingIdentity, finding_identity
from cw.review_convergence._models import (
    DECISION_CLEAN,
    DECISION_STOP_CONFIRM_ESCALATED,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    LEDGER_STATUS_STOPPED,
    STALL_WINDOW,
    STOP_DECISIONS,
    STOP_MAX_CYCLES,
    STOP_NO_PROGRESS,
    STOP_OPERATOR_ACTION,
    STOP_OSCILLATION,
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
    round_verdict_path,
    save_ledger,
    save_round_verdict,
)
from cw.review_convergence._tip import demote_for_rereview, evaluate_tip, finalize_ready

__all__ = [
    "CONFIRM_UNAVAILABLE_CAP",
    "DECISION_CLEAN",
    "DECISION_STOP_CONFIRM_ESCALATED",
    "LEDGER_RELATIVE_PATH",
    "LEDGER_STATUS_AWAITING_CONFIRM",
    "LEDGER_STATUS_CONVERGED",
    "LEDGER_STATUS_IN_PROGRESS",
    "LEDGER_STATUS_STOPPED",
    "ROUND_VERDICT_DIR_RELATIVE_PATH",
    "STALL_WINDOW",
    "STOP_DECISIONS",
    "STOP_MAX_CYCLES",
    "STOP_NO_PROGRESS",
    "STOP_OPERATOR_ACTION",
    "STOP_OSCILLATION",
    "ConfirmBackend",
    "ConfirmFailure",
    "ConvergenceError",
    "ConvergenceLedger",
    "Decision",
    "FindingIdentity",
    "FixedEntry",
    "FrozenCounts",
    "GatingSeverity",
    "LedgerStatus",
    "NextAction",
    "OperatorSettleReport",
    "Reconciled",
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
    "classify_findings",
    "confirm_blocker_fragment",
    "consecutive_confirm_failures",
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
    "max_cycles_exhausted",
    "new_ledger",
    "next_action",
    "one_line",
    "operator_settle",
    "operator_settle_event_payload",
    "reconcile_ledger_for_init",
    "record_confirm_failure",
    "record_round",
    "release_cause",
    "render_history_comment",
    "resolve_confirm_backend",
    "round_input_findings",
    "round_verdict_path",
    "save_ledger",
    "save_round_verdict",
    "sentinel_review_fragment",
    "settle_payload",
    "status_summary",
    "stop_summary",
    "suppress_settled",
    "unread_from_verdict",
    "worktree_state",
]
