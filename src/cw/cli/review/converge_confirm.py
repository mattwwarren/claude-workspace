"""The engine-confirm seam of ``cw review converge record-round`` (#3042).

RFC 0017 I3 (D-5, D-6, D-9): the independent confirm round recorded right
after a repo review engine pass is routed in code by
:func:`cw.review_convergence.route_engine_confirm`, never by the coordinator.
This module holds the CLI side of that rule, which ``converge.py`` wires into
``record-round``:

- :func:`identity_options` -- the ``-c/--client`` and ``--ticket`` overrides
  of the cw-context identity, mirroring ``cw review adjudicate``.
- :func:`round_ran_engine` -- whether a ``loop`` payload came from the engine
  alone (``RoundRecord.engine``).
- :func:`prepare_engine_confirm` -- refuses a findings-form confirm payload
  after an engine pass (it writes no round verdict, so an operator override
  could never release a park) and decides the verdict's findings in code.
- :func:`file_confirm_deferrals` -- files each deferred confirm SHOULD_FIX
  through :func:`cw.cli.review.debt.run_debt_filing`, before the ledger save.
- :func:`announce_confirm_park` -- the ``confirm_escalated`` friction line and
  the ``review.confirm_escalated`` event, after the ledger save.

It imports nothing from ``converge.py``, so ``converge.py`` imports it without
a cycle, and it registers no command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import click

from cw.cli.review._debt_session import default_artifact
from cw.cli.review.debt import resolve_identity, run_debt_filing
from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.review_adjudication import Adjudication
from cw.review_convergence import (
    CONFIRM_ENGINE_DEFER_RATIONALE,
    CONFIRM_ENGINE_PARK_RATIONALE,
    DECISION_STOP_CONFIRM_MUST_FIX,
    ConvergenceError,
    confirm_escalated_event_payload,
    decide_verdict,
    engine_pass_precedes,
    finding_identity,
    route_engine_confirm,
)
from cw.review_debt_filing import artifact_debt_entries, debt_entry
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_engine.route import is_non_deferrable_rule

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.review_convergence import (
        ConvergenceLedger,
        FindingIdentity,
        RoundFinding,
        RoundInput,
        RoundReport,
    )
    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

#: Prefix of the stderr line a parked engine-confirm round prints. The line is
#: ``friction: confirm_escalated: <reason> round=<n>``; the prose copies it into
#: ``friction_highlights`` without the ``friction: `` prefix.
FRICTION_CONFIRM_ESCALATED: Final = "friction: confirm_escalated"
#: Prefix of the stderr line when the ``review.confirm_escalated`` event could
#: not be recorded: ``friction: confirm_escalated_event_failed: <exception>``.
FRICTION_CONFIRM_ESCALATED_EVENT_FAILED: Final = (
    "friction: confirm_escalated_event_failed"
)
#: The refusal of a findings-form confirm payload after an engine pass (A11).
ENGINE_CONFIRM_NEEDS_VERDICT: Final = (
    "a confirm round after a repo review engine pass needs a verdict payload "
    "(the confirm backend's verdict); a findings payload writes no round "
    "verdict, so an operator override could never release the park"
)
CLIENT_OPTION_HELP: Final = (
    "Client that owns the queue ticket a confirm round after a repo review "
    "engine pass files its deferred SHOULD_FIX findings against, and names in "
    "the review.confirm_escalated event. Defaults to the client in "
    ".claude/cw-context.json; with neither, debt filing is skipped (friction "
    "debt_filing: skipped_no_context)."
)
TICKET_OPTION_HELP: Final = (
    "Queue ticket a confirm round after a repo review engine pass files its "
    "deferred SHOULD_FIX findings against. Defaults to the ticket_id in "
    ".claude/cw-context.json; see -c."
)

_GATING: Final = frozenset({"MUST_FIX", "SHOULD_FIX"})
_SHOULD_FIX: Final = "SHOULD_FIX"
_DEFER: Final = "defer"


def identity_options[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """The ``-c/--client`` and ``--ticket`` overrides of the cw-context identity."""
    with_ticket = click.option("--ticket", default=None, help=TICKET_OPTION_HELP)(fn)
    return click.option("-c", "--client", default=None, help=CLIENT_OPTION_HELP)(
        with_ticket
    )


def round_ran_engine(payload: RoundInput) -> bool:
    """Whether *payload* is a ``loop`` round the repo review engine alone ran.

    The role set is the verdict's ``agents_run`` roles; only a findings payload
    with no verdict falls back to its ``roster``. A confirm round never counts.
    """
    if payload.kind != "loop":
        return False
    roles = (
        {record.reviewer_role for record in payload.verdict.agents_run}
        if payload.verdict is not None
        else set(payload.roster)
    )
    return roles == {ENGINE_ROLE}


def _engine_confirm(payload: RoundInput, ledger: ConvergenceLedger) -> bool:
    return payload.kind == "confirm" and engine_pass_precedes(ledger)


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


def prepare_engine_confirm(
    payload: RoundInput, ledger: ConvergenceLedger
) -> RoundInput:
    """*payload* decided in code when it is a confirm round after an engine pass.

    Raises :class:`ConvergenceError` (exit 1, nothing recorded) for a
    findings-form payload there. Otherwise every accepted finding goes through
    ``decide_verdict`` with :data:`CONFIRM_ENGINE_DEFER_RATIONALE` (no
    coordinator adjudication runs), and a gating finding with a never-deferrable
    ``coverage.*``/``acceptance.*`` rule is restamped ``operator_actionable``
    with :data:`CONFIRM_ENGINE_PARK_RATIONALE`. Any other payload is returned
    unchanged.
    """
    if not _engine_confirm(payload, ledger):
        return payload
    if payload.verdict is None:
        raise ConvergenceError(ENGINE_CONFIRM_NEEDS_VERDICT)
    decided = decide_verdict(payload.verdict, CONFIRM_ENGINE_DEFER_RATIONALE)
    stamped = decided.model_copy(
        update={"accepted": [_park_stamp(item) for item in decided.accepted]}
    )
    return payload.model_copy(update={"verdict": stamped})


def _accepted_by_identity(
    verdict: ReviewVerdict | None,
) -> dict[FindingIdentity, Finding]:
    """The verdict's gating accepted findings keyed by ledger identity."""
    if verdict is None:
        return {}
    by_identity: dict[FindingIdentity, Finding] = {}
    for item in verdict.accepted:
        finding = item.finding
        if finding.severity not in _GATING:
            continue
        identity = finding_identity(
            finding.severity,
            finding.file,
            finding.summary,
            fingerprint=finding.fingerprint,
        )
        by_identity.setdefault(identity, finding)
    return by_identity


def _deferral(
    finding: RoundFinding,
    source: Finding | None,
    round_number: int,
    now: str,
) -> Adjudication:
    """The debt ``Adjudication`` of one deferred confirm SHOULD_FIX."""
    return Adjudication(
        severity=_SHOULD_FIX,
        file=finding.file,
        line_start=source.line_start if source is not None else None,
        line_end=source.line_end if source is not None else None,
        evidence=source.evidence if source is not None else "",
        summary=finding.summary,
        outcome="defer",
        rationale=CONFIRM_ENGINE_DEFER_RATIONALE,
        round=round_number,
        recorded_at=now,
    )


def file_confirm_deferrals(
    ledger: ConvergenceLedger,
    payload: RoundInput,
    findings: Sequence[RoundFinding],
    client: str | None,
    ticket: str | None,
    *,
    now: str,
) -> None:
    """File the SHOULD_FIX findings a confirm round after an engine pass defers.

    *ledger* is the pre-record ledger, so :func:`route_engine_confirm` sees the
    same inputs the engine did. Silent for any other round and when nothing is
    deferred. A never-deferrable SHOULD_FIX is a park, never a deferral, so it
    is never filed. Filing never raises and never changes the exit code (its
    failures are ``run_debt_filing``'s own friction lines).
    """
    if not _engine_confirm(payload, ledger):
        return
    route = route_engine_confirm(ledger, findings)
    if not route.deferred:
        return
    sources = _accepted_by_identity(payload.verdict)
    round_number = len(ledger.rounds) + 1
    applied = [
        _deferral(
            finding,
            sources.get(
                finding_identity(
                    finding.severity,
                    finding.file,
                    finding.summary,
                    fingerprint=finding.fingerprint,
                )
            ),
            round_number,
            now,
        )
        for finding in route.deferred
    ]
    run_debt_filing(client, ticket, None, applied)


def announce_confirm_park(
    updated: ConvergenceLedger,
    report: RoundReport,
    client: str | None,
    ticket: str | None,
) -> None:
    """Print the park friction and record ``review.confirm_escalated``.

    Runs after the ledger save and only for ``stop_confirm_must_fix``. The
    event is best-effort: an ``OSError`` prints the event-failed friction line
    and the command still exits 0. Identity is :func:`resolve_identity`
    (cw-context, overridden by ``-c``/``--ticket``), falling back to the
    ledger's ticket and a ``null`` client.
    """
    if report.decision != DECISION_STOP_CONFIRM_MUST_FIX:
        return
    click.echo(
        f"{FRICTION_CONFIRM_ESCALATED}: {report.reason} round={report.round}",
        err=True,
    )
    identity = resolve_identity(client, ticket)
    ticket_id = identity.ticket if identity is not None else updated.ticket_id
    client_name = identity.client if identity is not None else None
    payload = confirm_escalated_event_payload(
        updated, ticket_id=ticket_id, client=client_name
    )
    try:
        record_event(
            OrchestratorEventType.REVIEW_CONFIRM_ESCALATED,
            payload,
            correlation_id=ticket_id,
        )
    except OSError as exc:
        click.echo(
            f"{FRICTION_CONFIRM_ESCALATED_EVENT_FAILED}: {exc.__class__.__name__}",
            err=True,
        )


def file_round_deferrals(
    ledger: ConvergenceLedger,
    payload: RoundInput,
    findings: Sequence[RoundFinding],
    client: str | None,
    ticket: str | None,
    *,
    now: str,
) -> None:
    """File a non-engine record-round's new SHOULD_FIX defers (#3137).

    The opencode lane and the Claude-native record-round stamp SHOULD_FIX as
    ``defer`` on the ledger, and nothing else filed them: ``cw review adjudicate``
    files only what it decides itself. *ledger* is the pre-record ledger, so a
    defer the ledger already settled is skipped. A key already in the session
    artifact is skipped too, so a defer ``adjudicate`` filed keeps its entry.
    Engine confirm rounds stay with :func:`file_confirm_deferrals`. Silent
    outside a cw session unless ``-c``/``--ticket`` is given, in which case the
    filer reports ``skipped_no_session`` loudly. Filing never raises.
    """
    if _engine_confirm(payload, ledger):
        return
    artifact = default_artifact()
    if artifact is None and client is None and ticket is None:
        return
    settled = {entry.identity for entry in ledger.settled}
    fresh = [
        finding
        for finding in findings
        if finding.outcome == _DEFER
        and finding_identity(
            finding.severity,
            finding.file,
            finding.summary,
            fingerprint=finding.fingerprint,
        )
        not in settled
    ]
    if not fresh:
        return
    round_number = len(ledger.rounds) + 1
    built = [_round_deferral(finding, round_number, now) for finding in fresh]
    filed = (
        {entry.key for entry in artifact_debt_entries(artifact)}
        if artifact is not None and artifact.exists()
        else set()
    )
    applied = [item for item in built if debt_entry(item).key not in filed]
    if applied:
        run_debt_filing(client, ticket, None, applied)


def _round_deferral(finding: RoundFinding, round_number: int, now: str) -> Adjudication:
    """The debt ``Adjudication`` of one round-recorded SHOULD_FIX defer.

    Keeps the finding's own rationale (the opencode lane's text, or the
    Claude-native confirm text), unlike the engine path's fixed rationale.
    """
    return Adjudication(
        severity=finding.severity,
        file=finding.file,
        line_start=None,
        line_end=None,
        evidence="",
        summary=finding.summary,
        outcome=_DEFER,
        rationale=finding.rationale,
        round=round_number,
        recorded_at=now,
    )
