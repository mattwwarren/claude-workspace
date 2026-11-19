"""The first unmet static need of a spawn (RFC 0019 H2, #3709).

:func:`first_unmet` reads a harness's :class:`HarnessCapabilities` and the
spawn's :class:`SpawnFacts` and returns the first need the harness does not
meet, checked in this order:

1. the stage is declared (``stage_unsupported``);
2. a clean-mode REVIEW runs where the harness writes a convergence ledger
   (``clean_review_unsupported_backend``);
3. a REVIEW with ``review.engine`` runs where the harness runs an engine round
   (``review_engine_unsupported_backend``);
4. an IMPL row carrying repair feedback runs where the harness reads it
   (``repair_entry_unsupported_backend``).

Pure: no I/O, no logging. The executors call it at their existing park
positions (``cw.executor._preflight``) and build the blocked result with their
own runner's ``make_blocked``, so attempt charging and the persisted shape are
unchanged.

``retry_eligible`` keeps two pre-H2 quirks: a HARDEN ``stage_unsupported``
park carries ``None`` (the old opencode and codex stage parks), every other
stage ``False``; a ``blocks_any_feedback`` repair park carries ``None`` (the
old opencode repair park), a ``blocks_adopted`` one ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.models.capabilities import RepairEntry
from cw.models.enums import Stage
from cw.stage_preflight.details import (
    clean_review_details,
    repair_entry_details,
    stage_unsupported_details,
)
from cw.stage_preflight.reasons import PreflightReason

if TYPE_CHECKING:
    from cw.models.capabilities import HarnessCapabilities


@dataclass(frozen=True, slots=True)
class SpawnFacts:
    """The facts about one spawn that the static needs are checked against.

    ``engine_refusal`` is the pre-worded refusal text for the configured repo
    review engine, or ``None`` when the client configures no engine. ``feedback``
    is "the row carries repair feedback"; ``adopted`` is "the row is an
    adoption row carrying it".
    """

    stage: Stage
    clean_review: bool
    engine_refusal: str | None
    feedback: bool
    adopted: bool


@dataclass(frozen=True, slots=True)
class Unmet:
    """The first unmet need: the park reason, its details and retry flag."""

    reason: PreflightReason
    details: str
    retry_eligible: bool | None


def _repair_blocked(caps: HarnessCapabilities, facts: SpawnFacts) -> bool:
    if facts.stage is not Stage.IMPL or not facts.feedback:
        return False
    if caps.repair_entry is RepairEntry.BLOCKS_ANY_FEEDBACK:
        return True
    return caps.repair_entry is RepairEntry.BLOCKS_ADOPTED and facts.adopted


def first_unmet(caps: HarnessCapabilities, facts: SpawnFacts) -> Unmet | None:
    """Return the first static need *caps* does not meet for *facts*, or ``None``."""
    harness = caps.harness.value
    if facts.stage not in caps.stages:
        return Unmet(
            reason=PreflightReason.STAGE_UNSUPPORTED,
            details=stage_unsupported_details(harness, facts.stage, caps.stages),
            retry_eligible=None if facts.stage is Stage.HARDEN else False,
        )
    review = facts.stage is Stage.REVIEW
    if review and facts.clean_review and not caps.clean_mode_review:
        return Unmet(
            reason=PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND,
            details=clean_review_details(harness),
            retry_eligible=False,
        )
    if review and facts.engine_refusal is not None and not caps.review_engine:
        return Unmet(
            reason=PreflightReason.REVIEW_ENGINE_UNSUPPORTED_BACKEND,
            details=facts.engine_refusal,
            retry_eligible=False,
        )
    if _repair_blocked(caps, facts):
        any_feedback = caps.repair_entry is RepairEntry.BLOCKS_ANY_FEEDBACK
        return Unmet(
            reason=PreflightReason.REPAIR_ENTRY_UNSUPPORTED_BACKEND,
            details=repair_entry_details(harness),
            retry_eligible=None if any_feedback else False,
        )
    return None
