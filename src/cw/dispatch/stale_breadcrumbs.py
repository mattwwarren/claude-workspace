"""The ``dispatch_loop_stale`` page's self-describing breadcrumb (#2694).

The stale page used to carry only ``pending=<n> age_s=<n>``, which says that
the loop stopped dispatching but not why. :func:`build_stale_breadcrumbs`
appends what the last ``dispatch.tick`` recorded for the client -- its skip
reason (``cap_full`` and ``lane_cap_blocked`` arrive here; there is no
separate lane-cap page), its lane occupancy and its running/cap count -- and,
read fresh from state, the ceiling-counted sessions no occupied row owns
(the usual reason a client sits at ``cap_full`` with nothing RUNNING).

Append-only: the old prefix always comes first and the event payload keys do
not change. ``orphan_sessions`` is last because its value contains spaces.
The tick fields describe the LAST tick (``age_s`` ago); ``orphan_sessions``
is read now. A separate module because ``cw.dispatch.lanes`` is over the
module-size ceiling and this needs state reads the loop module does not.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.exceptions import CwError
from cw.reconcile.routed_result_sessions import (
    format_orphan_sessions,
    unowned_ceiling_sessions,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.orchestrate import TickSummary

_log = logging.getLogger(__name__)

# Rendered when no ceiling-counted session is orphaned, so a reader can tell
# "checked, none" from a degraded breadcrumb that omits the key.
_NO_ORPHANS = "none"


def build_stale_breadcrumbs(client: str, tick: TickSummary, now: datetime) -> str:
    """The ``breadcrumbs`` text for *client*'s ``dispatch_loop_stale`` page.

    ``pending=<n> age_s=<n> skip=<reason> occupied=<n> running=<r>/<cap>
    orphan_sessions=<none | <id> (<ticket>), ... (+K more)>``. When state or
    the queue cannot be read it degrades to the pre-#2694
    ``pending=<n> age_s=<n>`` text with one WARNING; the page is debounced by
    its own notify interval, so that is one line per page, not per tick.
    """
    age_s = int((now - tick.tick_at).total_seconds())
    prefix = f"pending={tick.pending} age_s={age_s}"
    try:
        sessions = load_state().sessions
        tasks = load_dev_queue().tasks
    except (OSError, ValueError, CwError) as exc:
        _log.warning(
            "stale_breadcrumbs_degraded: client=%s error=%s: %s",
            client,
            type(exc).__name__,
            exc,
        )
        return prefix
    orphans = format_orphan_sessions(unowned_ceiling_sessions(sessions, tasks, client))
    return (
        f"{prefix} skip={tick.skip_reason} occupied={tick.occupied}"
        f" running={tick.running}/{tick.cap}"
        f" orphan_sessions={orphans or _NO_ORPHANS}"
    )
