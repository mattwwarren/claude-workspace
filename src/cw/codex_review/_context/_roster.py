"""Which reviewer roles a codex pass runs, and their agent specs (#3216, R3).

A built-in pass selects roles from the diff (:func:`_select_reviewer_roles`)
and resolves each role's spec repo-local-first (:func:`_resolve_agent_spec`).
A repo review engine pass on a codex lane brings its own roster instead: the
role ids and spec texts of the default-branch ``cw-engine.json``. Every branch
between the two lives here, so ``_prepare_review_pass`` makes one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.codex_review._context._agent_spec import (
    _load_agent_spec_fallback_gate,
    _resolve_agent_spec,
)
from cw.codex_review._context._file_selection import _select_reviewer_roles
from cw.review_findings import AgentSpecStatus

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review._context._file_selection import _FileCategories


@dataclass(frozen=True)
class EngineRoster:
    """An engine's roles in manifest order: ``(role id, spec text)`` pairs."""

    roles: tuple[tuple[str, str], ...]


def resolve_roster(
    worktree: Path,
    engine_roster: EngineRoster | None,
    *,
    scope_tier: str,
    categories: _FileCategories,
    mutates_persisted_state: bool,
    has_ticket_context: bool,
) -> tuple[list[str], dict[str, str], list[AgentSpecStatus]]:
    """``(roles, agent spec text by role, per-role spec status)`` for one pass.

    With *engine_roster* the ids and texts pass through verbatim, each status
    ``source="repo"``; no ``.claude/agents`` file is read, in the worktree or
    globally. Without one it is today's built-in selection and resolution.
    """
    if engine_roster is not None:
        roles = [role for role, _ in engine_roster.roles]
        specs = dict(engine_roster.roles)
        statuses = [
            AgentSpecStatus(role=role, source="repo", empty=False) for role in roles
        ]
        return roles, specs, statuses
    roles = _select_reviewer_roles(
        scope_tier,
        categories=categories,
        mutates_persisted_state=mutates_persisted_state,
        has_ticket_context=has_ticket_context,
    )
    fallback_enabled = _load_agent_spec_fallback_gate(worktree)
    resolutions = {
        role: _resolve_agent_spec(
            worktree, role, global_fallback_enabled=fallback_enabled
        )
        for role in roles
    }
    return (
        roles,
        {role: resolutions[role].text for role in roles},
        [resolutions[role].status for role in roles],
    )
