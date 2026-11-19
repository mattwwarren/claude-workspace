"""Per-lane effective review-convergence mode check for cw doctor (#2797, #2806).

Setup-time visibility into ``review.convergence: clean``. For every lane of a
clean client, one ``CheckResult`` named ``review-convergence/<client>/<lane>``
reports whether that lane's REVIEW executor writes the convergence ledger
finalize Step 4.0b requires. Blocking clients emit nothing, so default doctor
output is unchanged; absence of the check means blocking mode.

States, evaluated in order:

1. the lane's resolved stages omit REVIEW: ``ok=True, warn=True`` (nothing
   writes a ledger, so a ticket reaching FINALIZE parks; doctor-warned only);
2. the backend's capability declaration
   (:func:`cw.executor._capabilities.declared_capabilities`) declares REVIEW
   and ``clean_mode_review`` (claude-native, codex, opencode): ``ok=True``; an
   opencode lane with ``review.confirm_backend`` unset also carries the
   default-on Claude egress note (#2806);
3. any other declared backend (``local``): ``ok=False``, and the detail names
   the reason the stage pre-flight (:func:`cw.stage_preflight.first_unmet`)
   parks a clean REVIEW spawn with, before the review agent runs. For
   ``local`` that is ``stage_unsupported``: it declares IMPL only (#3219);
4. a backend with no declaration (an unrecognized string): ``ok=False``,
   without claiming a typed block, because ``resolve_executor`` rejects it
   before any pre-flight.

Config-only (``resolve_executor_config``, ``resolve_pipeline_stages``), never
``shutil.which``. Output carries only backend, lane and client names and config
key names. No logging, no events. Leaf module -- no cross-``doctor``
dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.executor._capabilities import declared_capabilities
from cw.models import OPENCODE_BACKEND, Stage, TicketTask
from cw.review_convergence import resolve_confirm_backend
from cw.stage_preflight import PreflightReason, SpawnFacts, first_unmet
from cw.stage_preflight.details import remediation

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.models.capabilities import HarnessCapabilities

_CHECK_NAME = "review-convergence"

_NO_REVIEW_STAGE = (
    "effective mode clean, but lane '{lane}' has no REVIEW stage in its"
    " pipeline, so nothing writes the convergence ledger and a ticket reaching"
    " FINALIZE parks at Step 4.0b. Add review to the lane's pipeline.stages, or"
    " set review.convergence: blocking for client '{client}' in clients.yaml."
)
_SUPPORTED = (
    "effective mode clean: {backend} REVIEW lane writes the convergence ledger;"
    " confirm backend {confirm} (not_independent={not_independent});"
    " max_cycles {max_cycles}.{note}"
)
_CONFIRM_NONE_NOTE = (
    " review.confirm_backend is none, so no independent confirm round runs."
)
_NOT_INDEPENDENT_NOTE = " The confirm round is not independent of the loop."
_OPENCODE_EGRESS_NOTE = (
    " Default-on egress: review.confirm_backend is unset, so an opencode lane"
    " sends each confirm round's diff to Claude. confirm_backend: codex is not"
    " an opt-out (it sends to Codex); only review.confirm_backend: none avoids"
    " egress, and it forfeits the independent round. Tickets parked on"
    f" {PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND.value} begin this"
    " egress on their next"
    " requeue."
)
_CLIENT_REF = "client '{client}' in clients.yaml"
_GUARDED = (
    "effective mode clean, but lane '{lane}' REVIEW executor is"
    " backend={backend}, {why}: REVIEW-stage spawns on this lane are blocked"
    " in pre-flight, before the review agent runs, with reason {reason}."
    " To fix: {remediation}."
)
_WHY = {
    PreflightReason.STAGE_UNSUPPORTED: "which does not declare the REVIEW stage",
    PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND: (
        "whose REVIEW lane writes no convergence ledger"
    ),
}
#: The clean REVIEW spawn the guarded state words (no engine, no feedback).
_CLEAN_REVIEW_FACTS = SpawnFacts(
    stage=Stage.REVIEW,
    clean_review=True,
    engine_refusal=None,
    feedback=False,
    adopted=False,
)
_UNRECOGNIZED = (
    "effective mode clean, but lane '{lane}' REVIEW executor is"
    " backend={backend}, which cannot honor clean mode (it writes no"
    " convergence ledger) and is not an executor backend cw recognizes, so"
    " dispatch cannot resolve it (unknown executor backend)."
    " To fix: {remediation}."
)


def _lane_backend(task: TicketTask, client: ClientConfig) -> str:
    """The lane's effective REVIEW backend, from config only."""
    return resolve_executor_config(Stage.REVIEW, task, client).backend


def _result_name(name: str, task: TicketTask) -> str:
    return f"{_CHECK_NAME}/{name}/{task.lane}"


def _no_review_stage_result(name: str, task: TicketTask) -> CheckResult:
    return CheckResult(
        _result_name(name, task),
        ok=True,
        warn=True,
        detail=_NO_REVIEW_STAGE.format(lane=task.lane, client=name),
    )


def _supported_result(
    name: str, task: TicketTask, client: ClientConfig, backend: str
) -> CheckResult:
    resolved = resolve_confirm_backend(client.review.confirm_backend, backend)
    note = ""
    if resolved.name == "none":
        note = _CONFIRM_NONE_NOTE
    elif resolved.not_independent:
        note = _NOT_INDEPENDENT_NOTE
    if backend == OPENCODE_BACKEND and client.review.confirm_backend is None:
        note += _OPENCODE_EGRESS_NOTE
    detail = _SUPPORTED.format(
        backend=backend,
        confirm=resolved.name,
        not_independent=resolved.not_independent,
        max_cycles=client.review.effective_max_cycles,
        note=note,
    )
    return CheckResult(_result_name(name, task), ok=True, detail=detail)


def _fix(name: str) -> str:
    return remediation(_CLIENT_REF.format(client=name))


def _guarded_result(
    name: str, task: TicketTask, backend: str, caps: HarnessCapabilities
) -> CheckResult:
    unmet = first_unmet(caps, _CLEAN_REVIEW_FACTS)
    # A declared backend reaching this state lacks REVIEW or clean mode, so
    # first_unmet parks with one of the two _WHY reasons; the None fallback
    # only keeps the wording total.
    reason = (
        PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND
        if unmet is None
        else unmet.reason
    )
    detail = _GUARDED.format(
        lane=task.lane,
        backend=backend,
        why=_WHY[reason],
        reason=reason.value,
        remediation=_fix(name),
    )
    return CheckResult(_result_name(name, task), ok=False, detail=detail)


def _unrecognized_result(name: str, task: TicketTask, backend: str) -> CheckResult:
    detail = _UNRECOGNIZED.format(
        lane=task.lane, backend=backend, remediation=_fix(name)
    )
    return CheckResult(_result_name(name, task), ok=False, detail=detail)


def _lane_result(name: str, client: ClientConfig, task: TicketTask) -> CheckResult:
    """Classify one clean-client lane into the four documented states."""
    if Stage.REVIEW not in resolve_pipeline_stages(task, client):
        return _no_review_stage_result(name, task)
    backend = _lane_backend(task, client)
    caps = declared_capabilities(backend)
    if caps is None:
        return _unrecognized_result(name, task, backend)
    if Stage.REVIEW in caps.stages and caps.clean_mode_review:
        return _supported_result(name, task, client, backend)
    return _guarded_result(name, task, backend, caps)


def _check_review_convergence(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Report the effective clean-mode support of every lane of each clean client."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        if not client.review.is_clean:
            continue
        for lane in client.effective_lanes:
            # ticket_id="" is the documented "no associated ticket" sentinel.
            task = TicketTask(
                ticket_id="", client=name, lane=lane.name, stage=Stage.REVIEW
            )
            results.append(_lane_result(name, client, task))
    return results
