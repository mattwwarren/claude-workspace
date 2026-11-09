"""File review-debt tickets at deferral time (GitHub #3041, RFC 0017 D-7).

``cw review adjudicate`` (and ``cw review debt file``, finalize's re-file)
turns every ticketless ``defer`` entry into one debt ticket. On a GitHub
client with ``tracking.primary.auto_create_issues: true`` cw files it through
``gh``; otherwise (``false``, or a Linear client) cw emits a redacted payload
and, for Linear, verifies the IDs the worker reports back. Finalize links the
filed tickets to the PR.

A library with typed inputs and results (R13). It imports neither ``cw.cli``
nor ``cw.sprint``: the CLI decides the tracker mode, resolves the identity and
the redaction anchors, and passes them in; filing goes through the
:class:`DebtTracker` Protocol, which ``cw.sprint.GhClient`` satisfies.

Submodules, one concern each:

- ``_constants``: labels, budget, timeouts, caps, marker text.
- ``_models``: :class:`DebtEntry`, :class:`FilingContext`, the results.
- ``_marker``: the key and PR body markers and the dedup match.
- ``_render``: title, body and payload, all redacted.
- ``_tracker``: :class:`DebtTracker` and the tracker-mode decision.
- ``_file``: :func:`file_review_debt`.
- ``_emit``: :func:`emit_review_debt`.
- ``_verify``: :func:`verify_reported_ids`.
- ``_link``: :func:`link_pr`.
- ``_events``: the five ``review.debt_*`` events.
- ``_artifact``: deferrals read from, and ``ticket:`` lines written back to,
  the ``DEFERRED-REVIEW-FINDINGS`` artifact.
"""

from __future__ import annotations

from cw.review_debt_filing._artifact import (
    artifact_debt_entries,
    debt_entry,
    read_artifact,
    ticket_value,
    write_artifact_tickets,
)
from cw.review_debt_filing._constants import (
    DEBT_FILING_BUDGET_S,
    DETAIL_MAX_CHARS,
    KEY_DISPLAY_CHARS,
    PR_VIEW_TIMEOUT_S,
    REVIEW_DEBT_HARVESTED_LABEL,
    REVIEW_DEBT_LABEL,
    TRACKER_LINEAR,
    VISIBILITY_TIMEOUT_S,
)
from cw.review_debt_filing._emit import (
    CAUSE_AUTO_CREATE_OFF,
    CAUSE_MCP,
    EmitResult,
    EmittedFile,
    emit_review_debt,
    payload_path,
)
from cw.review_debt_filing._events import emit_verify_failed, emit_verify_succeeded
from cw.review_debt_filing._file import file_review_debt, unticketed
from cw.review_debt_filing._link import LinkResult, link_pr, linkable
from cw.review_debt_filing._marker import debt_marker, pr_marker
from cw.review_debt_filing._models import (
    DebtEntry,
    DebtRunStatus,
    FilingContext,
    FilingFailure,
    FilingFailureKind,
    FilingResult,
    LinkOutcome,
    TrackerMode,
)
from cw.review_debt_filing._tracker import DebtTracker, auto_create_issues, tracker_mode
from cw.review_debt_filing._verify import (
    ReportedId,
    VerifyFailure,
    VerifyResult,
    apply_reported,
    verify_reported_ids,
)

__all__ = [
    "CAUSE_AUTO_CREATE_OFF",
    "CAUSE_MCP",
    "DEBT_FILING_BUDGET_S",
    "DETAIL_MAX_CHARS",
    "KEY_DISPLAY_CHARS",
    "PR_VIEW_TIMEOUT_S",
    "REVIEW_DEBT_HARVESTED_LABEL",
    "REVIEW_DEBT_LABEL",
    "TRACKER_LINEAR",
    "VISIBILITY_TIMEOUT_S",
    "DebtEntry",
    "DebtRunStatus",
    "DebtTracker",
    "EmitResult",
    "EmittedFile",
    "FilingContext",
    "FilingFailure",
    "FilingFailureKind",
    "FilingResult",
    "LinkOutcome",
    "LinkResult",
    "ReportedId",
    "TrackerMode",
    "VerifyFailure",
    "VerifyResult",
    "apply_reported",
    "artifact_debt_entries",
    "auto_create_issues",
    "debt_entry",
    "debt_marker",
    "emit_review_debt",
    "emit_verify_failed",
    "emit_verify_succeeded",
    "file_review_debt",
    "link_pr",
    "linkable",
    "payload_path",
    "pr_marker",
    "read_artifact",
    "ticket_value",
    "tracker_mode",
    "unticketed",
    "verify_reported_ids",
    "write_artifact_tickets",
]
