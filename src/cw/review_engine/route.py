"""Mechanical routing of engine-sourced adjudication rows (RFC 0017 R14).

:func:`engine_route` is the one function that decides whether an adjudication
row for an *engine* finding may be applied. It runs before the pure ``_stamp``
seam. Rows are checked top-down; the first matching row wins:

1. ``reject`` is never allowed for an engine finding.
2. A MUST_FIX with a non-blank ``evidence_gap`` may ``defer``. It is held, not
   deferred: ``_stamp`` forces ``operator_actionable`` (#3822).
3. ``coverage.*`` / ``acceptance.*`` rules, or a non-deferrable adjudication,
   refuse ``defer``.
4. A MUST_FIX whose fingerprint an operator deferred may ``defer``.
5. Any other MUST_FIX refuses ``defer``.
6. Everything else is applied as recorded.

Builtin findings are never routed here; the caller filters on ``source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.exceptions import CwError
from cw.models.review_engine_summary import is_non_deferrable_rule
from cw.review_engine.constants import EngineRouteRefusalCode

if TYPE_CHECKING:
    from cw.review_adjudication._models import Adjudication
    from cw.review_findings import Finding

_MUST_FIX = "MUST_FIX"
_DEFER = "defer"

#: Outcomes an engine row may carry after routing.
RoutedOutcomeName = Literal["fix", "defer", "operator_action"]


@dataclass(frozen=True)
class RoutedOutcome:
    """An engine row the router allows, with the outcome it keeps."""

    outcome: RoutedOutcomeName


@dataclass(frozen=True)
class EngineRouteRefusal:
    """An engine row the router refuses, named by file and fingerprint."""

    code: EngineRouteRefusalCode
    file: str
    fingerprint: str


class EngineRouteError(CwError):
    """One or more engine adjudication rows were refused. Stamps nothing."""

    def __init__(self, refusals: list[EngineRouteRefusal]) -> None:
        self.refusals = tuple(refusals)
        lines = [
            f"engine_route: {refusal.code}: {refusal.file}: {refusal.fingerprint}"
            for refusal in refusals
        ]
        super().__init__("\n".join(lines))


def _refuse(code: EngineRouteRefusalCode, finding: Finding) -> EngineRouteRefusal:
    return EngineRouteRefusal(
        code=code,
        file=finding.file,
        fingerprint=finding.fingerprint or "",
    )


def _non_deferrable(finding: Finding, adjudication: Adjudication) -> bool:
    return is_non_deferrable_rule(finding.rule_id) or adjudication.non_deferrable


def _gap_held(finding: Finding) -> bool:
    """A MUST_FIX resting on an unrun check: the stamp holds it (#3822)."""
    return finding.severity == _MUST_FIX and bool((finding.evidence_gap or "").strip())


def engine_route(
    finding: Finding,
    adjudication: Adjudication,
    operator_defers: frozenset[str],
) -> RoutedOutcome | EngineRouteRefusal:
    """Decide whether *adjudication* may be applied to an engine *finding*."""
    outcome = adjudication.outcome
    if outcome == "reject":
        return _refuse(EngineRouteRefusalCode.ENGINE_REJECT_FORBIDDEN, finding)
    if outcome == _DEFER and _gap_held(finding):
        return RoutedOutcome(outcome=outcome)
    if _non_deferrable(finding, adjudication):
        if outcome == _DEFER:
            return _refuse(EngineRouteRefusalCode.ENGINE_NON_DEFERRABLE, finding)
        return RoutedOutcome(outcome=outcome)
    return _must_fix_route(finding, outcome, operator_defers)


def _must_fix_route(
    finding: Finding,
    outcome: RoutedOutcomeName,
    operator_defers: frozenset[str],
) -> RoutedOutcome | EngineRouteRefusal:
    """A MUST_FIX may defer only when an operator deferred its fingerprint."""
    if finding.severity != _MUST_FIX or outcome != _DEFER:
        return RoutedOutcome(outcome=outcome)
    if finding.fingerprint in operator_defers:
        return RoutedOutcome(outcome=outcome)
    return _refuse(
        EngineRouteRefusalCode.ENGINE_MUST_FIX_DEFER_REQUIRES_OPERATOR,
        finding,
    )
