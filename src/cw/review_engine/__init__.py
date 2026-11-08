"""Repo review engine (RFC 0017 I1, #3039).

With ``review.engine`` configured, the Claude-native review stage runs the
client's default-branch skill inline as one engine round, instead of the
built-in reviewer roster.

Import discipline. This ``__init__`` re-exports only the light modules
(``constants``, ``mapping``, ``route``). ``cw.review_adjudication`` imports the
light surface, so importing a heavy module here (workspace, convergence,
dev_queue) would create a cycle. The heavy modules (``context``, ``skill``,
``preflight``, ``materialize``, ``record``, ``allow``) are imported by their
submodule path, which is public. ``tests/test_review_engine_package.py`` pins
both facts.
"""

from __future__ import annotations

from cw.review_engine.constants import (
    ENGINE_ARGS_PREFIX,
    ENGINE_PREAMBLE,
    ENGINE_ROLE,
    EngineRoundFailure,
    EngineRouteRefusalCode,
)
from cw.review_engine.mapping import engine_document, engine_finding_to_finding
from cw.review_engine.route import (
    EngineRouteError,
    EngineRouteRefusal,
    RoutedOutcome,
    engine_route,
)

__all__ = [
    "ENGINE_ARGS_PREFIX",
    "ENGINE_PREAMBLE",
    "ENGINE_ROLE",
    "EngineRoundFailure",
    "EngineRouteError",
    "EngineRouteRefusal",
    "EngineRouteRefusalCode",
    "RoutedOutcome",
    "engine_document",
    "engine_finding_to_finding",
    "engine_route",
]
