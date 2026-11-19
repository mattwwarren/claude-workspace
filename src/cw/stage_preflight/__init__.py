"""Generic stage pre-flight: one decision for every harness (RFC 0019 H2, #3709).

A leaf package below the executors, the dispatch tick and the doctor: it
imports ``cw.models`` and nothing else, so any of them can read it without a
cycle (``tests/test_stage_preflight_imports.py`` pins that).

- ``reasons``  — :class:`PreflightReason`, the legacy alias table and
                 :func:`canonical_reason`.
- ``needs``    — :data:`STAGE_NEEDS`, the static needs per (harness, stage).
- ``details``  — the ``blocker.details`` wording.
- ``decision`` — :class:`SpawnFacts`, :class:`Unmet` and :func:`first_unmet`.
"""

from cw.stage_preflight.decision import SpawnFacts, Unmet, first_unmet
from cw.stage_preflight.needs import STAGE_NEEDS, StageNeeds, needs_for
from cw.stage_preflight.reasons import (
    LEGACY_REASON_ALIASES,
    PreflightReason,
    canonical_reason,
)

__all__ = [
    "LEGACY_REASON_ALIASES",
    "STAGE_NEEDS",
    "PreflightReason",
    "SpawnFacts",
    "StageNeeds",
    "Unmet",
    "canonical_reason",
    "first_unmet",
    "needs_for",
]
