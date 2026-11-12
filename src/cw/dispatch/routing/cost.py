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
    from cw.models import Session, TicketTask


def _add_optional(current: int | None, added: int | None) -> int | None:
    """Add two optional counts; ``None`` only when both are ``None``.

    Mirrors ``codex_review._shard_run._sum_or_none`` for two operands. Not
    imported from there: dispatch must not depend on the codex_review package.
    """
    if added is None:
        return current
    return added if current is None else current + added


def _reported_cost(session: Session) -> float | None:
    """The producer's own cost figure, from ``cost_usd`` then ``last_result``."""
    if session.cost_usd is not None:
        return session.cost_usd
    if isinstance(session.last_result, dict):
        raw_cost = session.last_result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            return float(raw_cost)
    return None


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost and token usage to the owning task, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_stop — normal headless path)
      2. session.last_result.get('cost_usd') (populated by the RFC 0012 door —
         the harvest-authority write path used when signal_stop did not run)

    Only when both are absent does the sum of ``session.cost_breakdown`` (the
    operator-priced estimate, #3063) count toward ``total_cost_usd``, so a
    session that reports its own cost is never double-counted. An estimate is
    never written into ``cost_usd`` itself.

    Tokens (``session.usage.total_tokens``) accumulate into
    ``task.total_tokens`` independently of the cost branch: a priced and an
    unpriced session both add theirs. When nothing is available the task is
    left unchanged.
    Called inside dev_queue_lock so the mutation is covered by the same
    save_dev_queue call that persists the COMPLETED status.
    """
    if session_id is None:
        return
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None:
        return
    cost = _reported_cost(session)
    if cost is None and session.cost_breakdown:
        cost = sum(session.cost_breakdown.values())
    if cost is not None:
        task.total_cost_usd = (task.total_cost_usd or 0.0) + cost
    if session.usage is not None:
        task.total_tokens = _add_optional(task.total_tokens, session.usage.total_tokens)
