"""Re-export completeness guard for ``cw.review_convergence`` (#2787).

Mirrors ``tests/test_review_adjudication_reexports.py``: ``__all__`` is asserted
against an exhaustive hardcoded set, deliberately NOT re-derived from the
package, so a dropped or renamed export is a falsifiable failure rather than a
tautology. A deliberate addition updates this set in the same commit.
"""

from __future__ import annotations

import cw.review_convergence as rc

EXPECTED_EXPORTS = {
    # Constants
    "CONFIRM_UNAVAILABLE_CAP",
    "DECISION_CLEAN",
    "DECISION_STOP_CONFIRM_ESCALATED",
    "LEDGER_RELATIVE_PATH",
    "ROUND_VERDICT_DIR_RELATIVE_PATH",
    "STALL_NOISE_FLOOR",
    "STALL_WINDOW",
    "STOP_DECISIONS",
    "STOP_MAX_CYCLES",
    "STOP_NO_PROGRESS",
    "STOP_OPERATOR_ACTION",
    "STOP_OSCILLATION",
    # Vocabulary aliases
    "ConfirmBackend",
    "Decision",
    "GatingSeverity",
    "LEDGER_STATUS_AWAITING_CONFIRM",
    "LEDGER_STATUS_CONVERGED",
    "LEDGER_STATUS_IN_PROGRESS",
    "LEDGER_STATUS_STOPPED",
    "LedgerStatus",
    "NextAction",
    "ReleaseCause",
    "RoundKind",
    "TipDecision",
    "TipReason",
    # Models and errors
    "ConfirmFailure",
    "ConvergenceError",
    "ConvergenceLedger",
    "FindingIdentity",
    "FixedEntry",
    "FrozenCounts",
    "OperatorSettleReport",
    "Reconciled",
    "ResolvedBackend",
    "RoundFinding",
    "RoundInput",
    "RoundRecord",
    "RoundReport",
    "SettleTarget",
    "SettledEntry",
    "StopRelease",
    "TipVerdict",
    "UnmatchedTarget",
    "UnreadFindings",
    # Functions
    "classify_findings",
    "clean_mode_supported",
    "confirm_blocker_fragment",
    "consecutive_confirm_failures",
    "demote_for_rereview",
    "detect_no_progress",
    "detect_oscillation",
    "evaluate_tip",
    "finalize_ready",
    "finding_identity",
    "friction_lines",
    "frozen_counts_from_verdict",
    "from_verdict",
    "history_rewritten",
    "load_ledger",
    "max_cycles_exhausted",
    "new_ledger",
    "next_action",
    "one_line",
    "operator_settle",
    "operator_settle_event_payload",
    "reconcile_for_init",
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
}


class TestPackageExportCompleteness:
    """Guards that ``cw.review_convergence`` re-exports its full surface."""

    def test_all_matches_full_surface(self) -> None:
        assert set(rc.__all__) == EXPECTED_EXPORTS

    def test_all_has_no_duplicates(self) -> None:
        assert len(rc.__all__) == len(set(rc.__all__))

    def test_every_exported_name_is_bound(self) -> None:
        """A typo'd re-export must fail here, not at a downstream import site."""
        missing = [name for name in rc.__all__ if not hasattr(rc, name)]
        assert missing == []
