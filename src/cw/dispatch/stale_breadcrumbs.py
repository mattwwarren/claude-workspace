"""Build self-describing ``dispatch_loop_stale`` breadcrumbs (#2694)."""

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


def build_stale_breadcrumbs(client: str, tick: TickSummary, now: datetime) -> str:
    """Append last-tick diagnostics and current orphan sessions to the prefix."""
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
        f" orphan_sessions={orphans or 'none'}"
    )
