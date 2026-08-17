"""Per-session cost accounting onto the owning ``TicketTask``.

Extracted from the flat ``dispatch/routing.py`` by #1728. ``dispatch/loop.py``
imports ``_accumulate_task_cost`` through the package facade, unchanged.

No back-dependency on ``routing/__init__.py``, and no ``record_event`` /
``_stage_regress`` / ``_stage_advance_unchecked`` call, which is why it was
safe to move out (see the package ``__init__``'s "Monkeypatch coupling" note).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import load_state

if TYPE_CHECKING:
    from cw.models import TicketTask


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost_usd to task.total_cost_usd, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_stop — normal headless path)
      2. session.last_result.get('cost_usd') (populated by the RFC 0012 door —
         the harvest-authority write path used when signal_stop did not run)

    When both sources are absent, total_cost_usd is left unchanged.
    Called inside dev_queue_lock so the mutation is covered by the same
    save_dev_queue call that persists the COMPLETED status.
    """
    if session_id is None:
        return
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None:
        return
    cost: float | None = session.cost_usd
    if cost is None and isinstance(session.last_result, dict):
        raw_cost = session.last_result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            cost = float(raw_cost)
    if cost is not None:
        task.total_cost_usd = (task.total_cost_usd or 0.0) + cost
