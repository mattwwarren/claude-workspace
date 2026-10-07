"""User-defined lifecycle event hooks: ``HookRule`` and ``EventHookRegistry``.

A leaf of ``cw.models.orchestrator_config``: depends on pydantic only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HookRule(BaseModel):
    """A user-defined shell command to run when a lifecycle event fires."""

    event_type: str
    command: str
    description: str = ""


class EventHookRegistry(BaseModel):
    """Persisted event hook rules for a client."""

    rules: list[HookRule] = Field(default_factory=list)
