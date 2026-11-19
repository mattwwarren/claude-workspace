"""What each declared (harness, stage) pair needs (RFC 0019 H2, #3709).

Data only. The executor pre-flight parks on stage, clean review, review engine
and repair entry (:mod:`cw.stage_preflight.decision`); nothing parks on
``skills`` or ``write_sandbox``. ``bindings`` is the set of roles the stage's
prompt reads, which the tick-side reachability check (#3744) consumes; whether
a tickets read goes through MCP (``plugin_id`` set) is a client fact that
check owns.

Keyed by ``(Harness, Stage)`` so the tick side can read it without importing
the executor package. Pinned against the declarations in
``cw.executor._capabilities`` by ``tests/test_stage_preflight.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from cw.models.bindings import Harness, Role
from cw.models.enums import Stage

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class StageNeeds:
    """The static needs of one stage on one harness."""

    bindings: frozenset[Role]
    skills: bool
    write_sandbox: bool


_NONE = StageNeeds(bindings=frozenset(), skills=False, write_sandbox=False)
_TICKETS = frozenset({Role.TICKETS})


def _needs(stage: Stage, *, tickets: bool, skills: bool) -> StageNeeds:
    return StageNeeds(
        bindings=_TICKETS if tickets else frozenset(),
        skills=skills,
        write_sandbox=stage is Stage.IMPL,
    )


#: One entry per declared (harness, stage) pair: claude-native and codex
#: PLAN/IMPL/REVIEW/FINALIZE, opencode PLAN/IMPL/REVIEW, local IMPL.
STAGE_NEEDS: Mapping[tuple[Harness, Stage], StageNeeds] = MappingProxyType(
    {
        (Harness.CLAUDE_NATIVE, Stage.PLAN): _needs(
            Stage.PLAN, tickets=True, skills=True
        ),
        (Harness.CLAUDE_NATIVE, Stage.IMPL): _needs(
            Stage.IMPL, tickets=True, skills=True
        ),
        (Harness.CLAUDE_NATIVE, Stage.REVIEW): _needs(
            Stage.REVIEW, tickets=False, skills=True
        ),
        (Harness.CLAUDE_NATIVE, Stage.FINALIZE): _needs(
            Stage.FINALIZE, tickets=False, skills=True
        ),
        (Harness.CODEX, Stage.PLAN): _needs(Stage.PLAN, tickets=True, skills=False),
        (Harness.CODEX, Stage.IMPL): _needs(Stage.IMPL, tickets=False, skills=False),
        (Harness.CODEX, Stage.REVIEW): _needs(
            Stage.REVIEW, tickets=False, skills=False
        ),
        (Harness.CODEX, Stage.FINALIZE): _needs(
            Stage.FINALIZE, tickets=True, skills=False
        ),
        (Harness.OPENCODE, Stage.PLAN): _needs(Stage.PLAN, tickets=True, skills=False),
        (Harness.OPENCODE, Stage.IMPL): _needs(Stage.IMPL, tickets=True, skills=False),
        (Harness.OPENCODE, Stage.REVIEW): _needs(
            Stage.REVIEW, tickets=False, skills=False
        ),
        (Harness.LOCAL, Stage.IMPL): _needs(Stage.IMPL, tickets=False, skills=False),
    }
)


def needs_for(harness: Harness, stage: Stage) -> StageNeeds:
    """The needs of *stage* on *harness*; empty for an undeclared pair."""
    return STAGE_NEEDS.get((harness, stage), _NONE)
