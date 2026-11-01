"""Shared data carriers and tiny readers of :mod:`cw.review_history` (#2790).

A leaf of the package: every other submodule may import it, and it imports no
sibling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cw.models import OrchestratorEvent, TicketTask
    from cw.models.review_history import (
        HistorySummary,
        RoundView,
        SettledView,
    )
    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import ReviewVerdict


@dataclass(frozen=True)
class HistoryInputs:
    """Everything :func:`build_review_history` needs, read once."""

    task: TicketTask
    ledger: ConvergenceLedger | None = None
    verdicts: Mapping[int, ReviewVerdict] = field(default_factory=dict)
    missing_verdict_rounds: tuple[int, ...] = ()
    sentinel: Mapping[str, object] | None = None
    bound_session_ids: frozenset[str] = frozenset()
    stage_events: tuple[OrchestratorEvent, ...] = ()
    voided_events: tuple[OrchestratorEvent, ...] = ()
    notes: tuple[str, ...] = ()


class _Part(NamedTuple):
    rounds: list[RoundView]
    settled: list[SettledView]
    summary: HistorySummary


def _review_block(result: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if result is None:
        return None
    review = result.get("review")
    return review if isinstance(review, dict) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _series(rounds: Sequence[RoundView]) -> list[int]:
    return [r.new_accepted for r in rounds if r.new_accepted is not None]
