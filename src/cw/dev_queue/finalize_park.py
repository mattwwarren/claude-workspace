"""Finalize-park predicates and text for the next-command table (#2767, #2911, #2779).

A finalize park is a FINALIZE ``review_pending_approval`` sentinel stamped
``stage4a_merge_gate`` (``gates.is_finalize_park``): Step 4.0's large-tier
approval park (rule 0c) or Step 4.0b's stale-tip park (rule 0d). This module
holds the row-and-session predicates both rules match on and the purpose text
they print, moved out of ``next_commands`` to keep that module under the size
ceiling and so ``approval`` can ask "is this an approval park?" without
importing the rule table.

Pure: no state is loaded, nothing is resolved, and it imports only leaf
modules (``gates``, ``cw.models``, ``cw.review_convergence``,
``cw.auto_dev_result``), never ``next_commands`` or ``approval``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.dev_queue.gates import (
    REVIEW_PENDING_APPROVAL_STATUS,
    approval_park_token,
    is_finalize_park,
    stale_tip_reason,
)
from cw.models import QueueItemStatus, Stage
from cw.review_convergence import TIP_CHECK_FETCH_FAILED

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import Session, TicketTask

# Rule 0c (#2767): the finalize Step 4.0 park on a lane with a review stage.
FINALIZE_APPROVAL_PARK_PURPOSE: Final = (
    "returns the ticket to review to obtain the large-tier approval; approve"
    " refuses at the terminal stage and a finalize re-run would only park again"
)
# Rule 0c on a lane with no review stage (#2779): the operator's approve of the
# branch head, then the finalize requeue that ships it.
REVIEW_LESS_APPROVE_PURPOSE: Final = (
    "record your approval of the branch head as it stands; this lane has no"
    " review stage, so nothing was reviewed and you vouch for that head; the"
    " row stays parked until the finalize requeue"
)
REVIEW_LESS_REQUEUE_PURPOSE: Final = (
    "release the finalize approval gate and ship; finalize re-checks the"
    " recorded approval against the branch head and parks again without a"
    " current one"
)
# The same pair once an approval was recorded after a ``missing`` park: the
# requeue leads, and a re-approve is needed only if commits landed since.
REVIEW_LESS_APPROVED_REQUEUE_PURPOSE: Final = (
    "release the finalize approval gate and ship; your approval is recorded for"
    " the head you approved, and a later commit parks finalize again"
)
REVIEW_LESS_APPROVED_APPROVE_PURPOSE: Final = (
    "re-record your approval for the branch head as it stands now, only if"
    " commits landed since; the row stays parked"
)
# The Step 4.0 park token that means "no approval was recorded at park time".
_PARK_NO_APPROVAL: Final = "missing"

# Rule 0d (#2911): the finalize Step 4.0b stale-tip park. The verify-tip
# reasons a review round clears, with the clause each one prints.
_STALE_TIP_ROUND_CLAUSES: dict[str, str] = {
    "commits_after_clean_round": "commits landed after the last clean review round",
    "no_clean_round": "no clean review round is recorded",
}
_STALE_TIP_ROUND_TAIL = (
    "requeue returns the ticket to review, which re-reviews them before they can merge"
)
_STALE_TIP_OTHER_TAIL = (
    "requeue re-runs review, which re-checks the tip; if the cause is push or"
    " network state, fix that first"
)
_STALE_TIP_LARGE_NOTE = (
    "this ticket is large tier, so review parks it for the large-tier approval"
    " after the round"
)
# Rule 0d (#2910): the tip check could not fetch origin. Not a review problem,
# so its release is a finalize re-run, never ``--stage review``.
STALE_TIP_FETCH_PURPOSE: Final = (
    f"the final-tip check could not fetch origin ({TIP_CHECK_FETCH_FAILED}); fix"
    " the network or credential cause, then requeue re-runs finalize, which"
    " re-checks the tip; no review round is needed"
)


def finalize_park_result(
    task: TicketTask, session: Session | None
) -> Mapping[str, object] | None:
    """The bound sentinel of a parked FINALIZE row's finalize park, or None.

    A FINALIZE ``review_pending_approval`` park whose sentinel is stamped
    ``stage4a_merge_gate`` (``gates.is_finalize_park``): finalize Step 4.0's
    approval park (#2767) or Step 4.0b's stale-tip park (#2911).
    """
    if not (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.stage == Stage.FINALIZE
        and task.disposition == REVIEW_PENDING_APPROVAL_STATUS
        and session is not None
    ):
        return None
    result = session.last_result
    if isinstance(result, dict) and is_finalize_park(result):
        return result
    return None


def stale_tip_park_reason(task: TicketTask, session: Session | None) -> str | None:
    """The ``verify-tip`` reason of a Step 4.0b stale-tip park, or None (#2911).

    ``""`` when the park's ``review_tip_check: stale`` token names no reason.
    """
    result = finalize_park_result(task, session)
    return None if result is None else stale_tip_reason(result)


def is_stale_tip_park(task: TicketTask, session: Session | None) -> bool:
    """A finalize park carrying Step 4.0b's stale-tip token (#2911).

    Fails closed to rule 0c when the sentinel also carries Step 4.0's
    ``review_approval_park:`` line (``gates.stale_tip_reason``).
    """
    return stale_tip_park_reason(task, session) is not None


def is_approval_park(task: TicketTask, session: Session | None) -> bool:
    """A finalize park that is not a Step 4.0b stale-tip park (#2767, #2779).

    That is the finalize Step 4.0 park. The match is on the stamp, so any
    FINALIZE sentinel carrying it qualifies (A18) unless it is the stale-tip
    park; rule 0d runs first in the table, so rule 0c's semantics are
    unchanged, and ``approval``'s review-less release can use it alone.
    """
    result = finalize_park_result(task, session)
    return result is not None and stale_tip_reason(result) is None


def approval_recorded_after_park(task: TicketTask, session: Session | None) -> bool:
    """True iff the row carries an approval recorded after a ``missing`` park.

    Step 4.0 parked because no approval existed (token ``missing``), and the
    row now holds one, so it was recorded since. A ``stale`` park never
    qualifies: its approval was already there and is not valid for the head.
    """
    result = finalize_park_result(task, session)
    return (
        task.review_approved_at is not None
        and result is not None
        and approval_park_token(result) == _PARK_NO_APPROVAL
    )


def stale_tip_purpose(task: TicketTask, reason: str) -> str:
    """Why rule 0d's review requeue clears the park, by ``verify-tip`` reason."""
    clause = _STALE_TIP_ROUND_CLAUSES.get(reason)
    if clause is not None:
        body = f"a review round is needed: {clause}; {_STALE_TIP_ROUND_TAIL}"
    else:
        shown = f" ({reason})" if reason else ""
        body = f"the final-tip check failed{shown}; {_STALE_TIP_OTHER_TAIL}"
    if SCOPE_TIER_LARGE in (task.scope_hint, task.computed_scope_tier):
        body = f"{body}; {_STALE_TIP_LARGE_NOTE}"
    return body
