"""Stub event bus — will be replaced by full implementation in #7."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.models import OrchestratorEventType


def record_event(
    event_type: OrchestratorEventType,
    payload: dict[str, object],
    correlation_id: str | None = None,
) -> None:
    """Stub: no-op until #7 lands."""
