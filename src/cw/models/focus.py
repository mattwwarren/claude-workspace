"""Session-scoped focus pointer model (#1644).

One ``FocusEntry`` per Claude Code session id, persisted in the bare
``dict[str, FocusEntry]`` map at ``~/.local/share/cw/focus.json``. Unlike every
other cw store the on-disk root IS the map (no wrapping model with a named
collection field) — the shape is pinned by the ticket, not chosen here.

Depends on nothing else in ``cw.models``; sits alongside ``enums`` at the DAG
root. See ``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class FocusEntry(BaseModel):
    """What a single session is currently working on.

    ``lane`` is None for the client-only form (``cw focus set <client>``), which
    ``cw statusline render`` renders as an aggregate across every lane.
    ``set_at`` is informational only: R6 pins no expiry, no TTL, and no pruning,
    so nothing reads it to decide staleness.
    """

    model_config = ConfigDict(extra="forbid")

    client: str
    lane: str | None = None
    set_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
