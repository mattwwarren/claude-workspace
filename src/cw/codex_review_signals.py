"""Loud-failure signals for codex reviews and PLAN / PR-link comments (#3055, #3707).

RFC 0018 S3 gave codex reviews two durable signals on a non-GitHub tracker;
RFC 0019 O3 (#3707) narrowed them. This module now carries:

* :func:`emit_verdict_comment_skipped`: ``review.verdict_comment_skipped``, the
  only verdict-comment event still emitted. It fires when a producer enqueued
  *nothing* for a codex comment (``outbox_enqueue_failed``, ``plan_refused``,
  ``body_too_long``, ``free_text_ticket``, ``already_posted``). Delivery of an
  enqueued entry is reported by the outbox's own ``outbox.entry_*`` events.
* :func:`tracker_label`: the ``tracker`` string those events carry.
* :func:`constraints_source`: where the fix loop's operator constraints came
  from (``Review.operator_constraints_source``).
* :func:`emit_confirm_escalated` (#3216): ``review.confirm_escalated`` when a
  confirm round after a codex engine pass parks.

``review.verdict_comment_posted`` and ``review.verdict_comment_unverified`` are
no longer emitted (O3): the enum members stay for reading old logs.

**Best-effort (A15).** Every emitter catches ``OSError`` and ``CwError`` from
``record_event`` only, logs one warning, and returns.

Leaf module: it must not import ``cw.codex_fix_loop`` or
``cw.codex_review_persist`` (``stage_verification.baseline`` imports
``codex_review_persist``, which imports this module).
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import OperatorConstraintsSource
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_log = logging.getLogger(__name__)

# Shared by tracker-IO outcome handling and codex stage guards. This module is
# intentionally below both callers in the dependency graph: it contains no
# tracker-IO or stage-executor imports at runtime.
COMMON_TURN_FAULTS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    RuntimeError,
    LookupError,
    TypeError,
    AttributeError,
    subprocess.SubprocessError,
)

#: Which comment a skip dropped: a parsed verdict, an unparseable one, the
#: codex PLAN executor's plan or park comment (``plan``, #3060), or the codex
#: FINALIZE executor's PR-link comment (``pr_link``, #3062).
VerdictCommentKind = Literal["verdict", "unparseable", "plan", "pr_link"]


def tracker_label(client: ClientConfig) -> str:
    """The ``tracker`` string a skipped event carries: the system, or GitHub when unset.

    Fail-open on an unresolvable tracker, the same polarity as the rest of cw.
    """
    return resolve_tracker(client.workspace_path) or TRACKER_GITHUB_ISSUES


def constraints_source(
    *, has_comment: bool, row_entries: int, comments_readable: bool
) -> OperatorConstraintsSource:
    """Classify where the operator constraints reaching a codex review came from.

    A tracker comment and row entries give ``comment_and_row``; either alone
    gives ``comment`` or ``row``. Neither gives ``none`` when the comments were
    readable (nothing was set) and ``tracker_unreadable`` otherwise (no
    readable snapshot, or a non-GitHub tracker with no ``operator_ids``: the
    comments could not be read, so their absence proves nothing; one WARNING
    is logged). *row_entries* counts every ``task.operator_constraints`` entry,
    operator and plan-derived (A10).
    """
    has_row = row_entries > 0
    if has_comment and has_row:
        return OperatorConstraintsSource.COMMENT_AND_ROW
    if has_comment:
        return OperatorConstraintsSource.COMMENT
    if has_row:
        return OperatorConstraintsSource.ROW
    if comments_readable:
        return OperatorConstraintsSource.NONE
    _log.warning(
        "operator constraint comments unreadable: no readable ticket snapshot, "
        "or a non-GitHub tracker with no operator_ids"
    )
    return OperatorConstraintsSource.TRACKER_UNREADABLE


def record_event_best_effort(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> None:
    """``record_event``, swallowing event-write faults with one warning (A15)."""
    try:
        record_event(event_type, payload, correlation_id=ticket_id)
    except (OSError, CwError) as exc:
        _log.warning(
            "codex_review_signal_failed ticket=%s event=%s: %s",
            ticket_id,
            event_type.value,
            exc,
        )


def verdict_comment_skipped_payload(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    reason: str,
    entry_id: str | None = None,
) -> dict[str, object]:
    """The ``review.verdict_comment_skipped`` payload, in documented order.

    ``entry_id`` is the existing outbox entry for an ``already_posted`` skip,
    and ``None`` when no entry exists.
    """
    return {
        "ticket_id": ticket_id,
        "client": client,
        "tracker": tracker,
        "backend": CODEX_BACKEND,
        "kind": kind,
        "reason": reason,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "reviewed_sha": reviewed_sha or None,
        "session_id": session_id,
        "entry_id": entry_id,
    }


def emit_verdict_comment_skipped(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    reason: str,
    entry_id: str | None = None,
) -> None:
    """Record that a codex comment was not enqueued (best-effort).

    Emitted by a producer only when no outbox entry was created for the comment.
    """
    payload = verdict_comment_skipped_payload(
        ticket_id=ticket_id,
        client=client,
        tracker=tracker,
        kind=kind,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
        reason=reason,
        entry_id=entry_id,
    )
    record_event_best_effort(
        OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED, payload, ticket_id
    )


def emit_confirm_escalated(payload: dict[str, object], *, ticket_id: str) -> None:
    """``review.confirm_escalated`` for a codex engine-confirm park (#3216).

    The codex peer of claude-native's ``announce_confirm_park``: same event and
    payload, best-effort (an ``OSError`` logs ``codex_review_signal_failed``).
    """
    record_event_best_effort(
        OrchestratorEventType.REVIEW_CONFIRM_ESCALATED, payload, ticket_id
    )
