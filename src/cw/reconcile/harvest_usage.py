"""The unavailable-usage marker for opencode and local harvest (#3063, RFC 0018 C4).

opencode and local (aider) sessions complete through the dead-process harvest.
No real opencode output capture exists, so there is no pinned shape to read
token usage from (the real reader is follow-up #3552): such a session records
the *unavailable* state with reason ``backend_output_unpinned`` instead of
silently reporting nothing.

Everything here is pure in-memory work on data the caller already holds (the
session, its liveness handle and the ticket id). It runs under the harvest's
``sessions_lock``, so it reads no file (``opencode.log`` included), launches no
process and imports neither ``cw.opencode_runner`` nor
``cw.reconcile.harvest_synthesis``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.models.usage import (
    BACKEND_OUTPUT_UNPINNED_DETAIL,
    USAGE_UNAVAILABLE_FRICTION_PREFIX,
    USAGE_UNAVAILABLE_WARNING,
    SessionUsage,
    UsageStage,
    UsageState,
    UsageUnavailableReason,
)

if TYPE_CHECKING:
    from cw.models import Session

__all__ = ["stamp_backend_usage_unavailable", "usage_unavailable_friction"]

_log = logging.getLogger(__name__)


def stamp_backend_usage_unavailable(session: Session, ticket_id: str | None) -> None:
    """Mark *session*'s usage unavailable, once, with one warning (#3063).

    A no-op when the session has no liveness handle (production never passes
    one without it: the harvest act phase guards it) or already carries usage.
    ``Session.cost_breakdown`` is left ``None``. *ticket_id* is the harvest
    candidate's, which may be absent; the warning then names ``ticket=none``.
    """
    if session.local_liveness is None or session.usage is not None:
        return
    session.usage = SessionUsage(
        state=UsageState.UNAVAILABLE,
        unavailable_reason=UsageUnavailableReason.BACKEND_OUTPUT_UNPINNED,
    )
    _log.warning(
        USAGE_UNAVAILABLE_WARNING,
        session.local_liveness.backend,
        session.id,
        ticket_id or "none",
        UsageStage.SESSION.value,
        UsageUnavailableReason.BACKEND_OUTPUT_UNPINNED.value,
    )


def usage_unavailable_friction(session: Session) -> str:
    """Return the ``friction_highlights`` token for a harvested backend session.

    The bare prefix for a session with no liveness handle, which only unit
    tests reach.
    """
    if session.local_liveness is None:
        return USAGE_UNAVAILABLE_FRICTION_PREFIX
    return (
        f"{USAGE_UNAVAILABLE_FRICTION_PREFIX}{session.local_liveness.backend} "
        "sessions record no token usage "
        f"(reason={UsageUnavailableReason.BACKEND_OUTPUT_UNPINNED.value}: "
        f"{BACKEND_OUTPUT_UNPINNED_DETAIL})"
    )
