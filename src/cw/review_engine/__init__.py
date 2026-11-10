"""Repo review engine (RFC 0017 I1, #3039).

With ``review.engine`` configured, the claude-native review stage runs the
client's default-branch skill inline as one engine round, instead of the
built-in reviewer roster. A codex review lane runs the same engine through its
``cw-engine.json`` manifest (RFC 0018 A4, #3216): one ``codex exec`` per role
and one aggregation turn, validated by the shared ``validate`` core.

Import discipline. This ``__init__`` re-exports only the light modules
(``constants``, ``mapping``, ``route``). ``cw.review_adjudication`` imports the
light surface, so importing a heavy module here (workspace, convergence,
dev_queue) would create a cycle. The heavy modules (``context``, ``skill``,
``preflight``, ``materialize``, ``record``, ``allow``, ``validate``,
``manifest_source``) are imported by their submodule path, which is public.
``aggregate`` (the codex aggregation output contract, #3216) is light by
import graph but is not re-exported either; import it by submodule path.
``tests/test_review_engine_package.py`` pins these facts.
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
