"""Routing of the independent confirm round after a repo review engine pass (#3042).

RFC 0017 I3 (D-5, D-6, D-9): once the repo review engine has run a clean
``loop`` round, the engine's verdict is final. The independent confirm round
that follows is never adjudicated by the coordinator and never feeds the fix
loop. :func:`route_engine_confirm` decides it by severity alone, whatever
outcome the findings arrived stamped with, top-down:

1. A finding whose identity is settled (and ``settled_applies``) resurfaces and
   is not new.
2. Any new MUST_FIX, or a SHOULD_FIX whose rule is never deferrable
   (``coverage.*`` / ``acceptance.*``), is park-class: the round parks
   (:attr:`ConfirmAction.PARK`) for the operator.
3. Every other new SHOULD_FIX is deferred to debt
   (:data:`CONFIRM_ENGINE_DEFER_RATIONALE`). With no park-class finding the
   round is :attr:`ConfirmAction.DEFER`, which converges like a clean round.
4. Nothing new is :attr:`ConfirmAction.CLEAN`.

The router is pure and deterministic: the engine (``_apply_round``) calls it
to decide the round, and the CLI calls it on the same pre-record ledger to
learn which SHOULD_FIX findings to file.

:func:`decide_confirm_verdict` is the verdict-side half both lanes share
(#3216): the claude-native ``record-round`` CLI and the codex confirm round
decide the confirm verdict through it, and :func:`verdict_ran_engine` says
whether a loop verdict came from the engine alone.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.review_convergence._findings import (
    Classified,
    decide_verdict,
    settled_applies,
    settled_debt_key,
    settled_index,
)
from cw.review_convergence._identity import FindingIdentity, finding_identity
from cw.review_convergence._models import (
    LEDGER_STATUS_AWAITING_CONFIRM,
    RoundFinding,
    SettledEntry,
)
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_engine.route import is_non_deferrable_rule

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence._models import ConvergenceLedger
    from cw.review_findings import AcceptedFinding, ReviewVerdict

#: Rationale of every SHOULD_FIX the confirm round after an engine pass defers.
#: It is the settled entry's rationale and the filed debt ticket's rationale.
CONFIRM_ENGINE_DEFER_RATIONALE: Final = (
    "SHOULD_FIX from the independent confirm round after a repo review engine "
    "pass; deferred to debt automatically, never re-judged (RFC 0017 D-5, D-9)"
)

#: Rationale stamped on a never-deferrable SHOULD_FIX the same round parks.
CONFIRM_ENGINE_PARK_RATIONALE: Final = (
    "Coverage or acceptance SHOULD_FIX from the independent confirm round after "
    "a repo review engine pass; never deferrable, parked for the operator "
    "(RFC 0017 D-6)"
)


class ConfirmAction(StrEnum):
    """What a confirm round after an engine pass does."""

    PARK = "park"
    DEFER = "defer"
    CLEAN = "clean"


class ConfirmRoute(NamedTuple):
    """The router's answer for one confirm round after an engine pass.

    ``classified`` is what ``_apply_round`` records: no ``new_fix`` ever, the
    park-class findings as ``new_operator`` and the deferred SHOULD_FIX findings
    as ``newly_settled``. ``park_identities`` are the stop identities of a park,
    in finding order and deduplicated; ``deferred`` are the SHOULD_FIX findings
    to file as debt (also deduplicated).
    """

    action: ConfirmAction
    classified: Classified
    park_identities: tuple[FindingIdentity, ...]
    deferred: tuple[RoundFinding, ...]

    @property
    def parks(self) -> bool:
        """True when the round parks for the operator."""
        return self.action is ConfirmAction.PARK


def engine_pass_precedes(ledger: ConvergenceLedger) -> bool:
    """Whether the next confirm round on *ledger* follows an engine pass.

    True only when the ledger is ``awaiting_confirm`` and its last round is a
    clean ``loop`` round the engine ran, recorded at ``awaiting_confirm_sha``.
    A ledger written before #3042 has ``engine=False`` on every round, so it
    never routes here.
    """
    if ledger.status != LEDGER_STATUS_AWAITING_CONFIRM or not ledger.rounds:
        return False
    last = ledger.rounds[-1]
    return (
        last.kind == "loop"
        and last.clean
        and last.engine
        and last.reviewed_sha == ledger.awaiting_confirm_sha
    )


def _is_park_class(finding: RoundFinding) -> bool:
    """A MUST_FIX, or a SHOULD_FIX the operator could never defer (D-6)."""
    return finding.severity == "MUST_FIX" or is_non_deferrable_rule(finding.rule_id)


def _identity_of(finding: RoundFinding) -> FindingIdentity:
    return finding_identity(
        finding.severity,
        finding.file,
        finding.summary,
        fingerprint=finding.fingerprint,
    )


def _action(parked: bool, deferred: bool) -> ConfirmAction:
    if parked:
        return ConfirmAction.PARK
    if deferred:
        return ConfirmAction.DEFER
    return ConfirmAction.CLEAN


def route_engine_confirm(
    ledger: ConvergenceLedger, findings: Sequence[RoundFinding]
) -> ConfirmRoute:
    """Route the confirm round *findings* recorded over an engine pass.

    Severity decides, never the stamped outcome: cw does not reject an engine
    finding, so a MUST_FIX stamped ``reject`` still parks and a SHOULD_FIX
    stamped ``fix`` is still deferred. A settled identity that
    ``settled_applies`` to the re-raise is counted as resurfaced. Duplicate
    identities collapse; an identity raised both park-class and deferrable
    (an engine fingerprint at two severities) parks.
    """
    round_number = len(ledger.rounds) + 1
    settled = settled_index(ledger)
    park: dict[FindingIdentity, RoundFinding] = {}
    defer: dict[FindingIdentity, RoundFinding] = {}
    reseen: dict[FindingIdentity, None] = {}
    resurfaced = 0
    for finding in findings:
        identity = _identity_of(finding)
        entry = settled.get(identity)
        if entry is not None and settled_applies(
            entry, finding.severity, finding.fingerprint, finding.rule_id
        ):
            resurfaced += 1
            reseen[identity] = None
        elif _is_park_class(finding):
            park.setdefault(identity, finding)
        else:
            defer.setdefault(identity, finding)
    deferred = {
        identity: finding for identity, finding in defer.items() if identity not in park
    }
    newly_settled = [
        SettledEntry(
            identity=identity,
            outcome="defer",
            rationale=CONFIRM_ENGINE_DEFER_RATIONALE,
            round=round_number,
            debt_key=settled_debt_key("defer", finding.file, finding.summary),
            last_seen_round=round_number,
        )
        for identity, finding in deferred.items()
    ]
    classified = Classified(
        new_fix=[],
        new_operator=list(park.values()),
        newly_settled=newly_settled,
        resurfaced=resurfaced,
        reseen=list(reseen),
    )
    return ConfirmRoute(
        action=_action(bool(park), bool(deferred)),
        classified=classified,
        park_identities=tuple(park),
        deferred=tuple(deferred.values()),
    )


_GATING: Final = frozenset({"MUST_FIX", "SHOULD_FIX"})


def verdict_ran_engine(verdict: ReviewVerdict) -> bool:
    """Whether *verdict*'s reviewer set is the repo review engine alone."""
    return {record.reviewer_role for record in verdict.agents_run} == {ENGINE_ROLE}


def _park_stamp(item: AcceptedFinding) -> AcceptedFinding:
    """*item* restamped ``operator_actionable`` when it can never be deferred."""
    finding = item.finding
    if finding.severity in _GATING and is_non_deferrable_rule(finding.rule_id):
        return item.model_copy(
            update={
                "disposition": "operator_actionable",
                "disposition_detail": CONFIRM_ENGINE_PARK_RATIONALE,
            }
        )
    return item


def decide_confirm_verdict(
    verdict: ReviewVerdict, ledger: ConvergenceLedger, default_rationale: str
) -> ReviewVerdict:
    """The confirm round *verdict* decided in code, for either lane.

    After an engine pass (:func:`engine_pass_precedes` on the pre-record
    *ledger*) every accepted finding goes through ``decide_verdict`` with
    :data:`CONFIRM_ENGINE_DEFER_RATIONALE` and a gating never-deferrable
    ``coverage.*``/``acceptance.*`` finding is restamped
    ``operator_actionable`` with :data:`CONFIRM_ENGINE_PARK_RATIONALE`.
    Otherwise it is ``decide_verdict(verdict, default_rationale)``.
    """
    if not engine_pass_precedes(ledger):
        return decide_verdict(verdict, default_rationale)
    decided = decide_verdict(verdict, CONFIRM_ENGINE_DEFER_RATIONALE)
    return decided.model_copy(
        update={"accepted": [_park_stamp(item) for item in decided.accepted]}
    )
