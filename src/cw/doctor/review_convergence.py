"""Per-lane effective review-convergence mode check for cw doctor (#2797).

Setup-time visibility into ``review.convergence: clean``. For every lane of a
clean client, one ``CheckResult`` named ``review-convergence/<client>/<lane>``
reports whether that lane's REVIEW executor writes the convergence ledger
finalize Step 4.0b requires. Blocking clients emit nothing, so default doctor
output is unchanged; absence of the check means blocking mode.

States, evaluated in order:

1. the lane's resolved stages omit REVIEW: ``ok=True, warn=True`` (nothing
   writes a ledger, so a ticket reaching FINALIZE parks; doctor-warned only);
2. :func:`cw.review_convergence.clean_mode_supported`: ``ok=True``;
3. a backend the executor pre-flight guard covers
   (:data:`cw.executor._clean_review.CLEAN_REVIEW_GUARDED_BACKENDS`):
   ``ok=False``, and the detail says REVIEW-stage spawns are blocked in
   pre-flight, before the review agent runs, while earlier stages still run;
4. any other (unrecognized) backend string: ``ok=False``, without claiming a
   typed block, because ``resolve_executor`` rejects it before any pre-flight.

Config-only (``resolve_executor_config``, ``resolve_pipeline_stages``), never
``shutil.which``. Output carries only backend, lane and client names and config
key names. No logging, no events. Leaf module -- no cross-``doctor``
dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.executor._clean_review import (
    CLEAN_REVIEW_GUARDED_BACKENDS,
    CLEAN_REVIEW_UNSUPPORTED_BACKEND,
    remediation,
)
from cw.models import Stage, TicketTask
from cw.review_convergence import clean_mode_supported, resolve_confirm_backend

if TYPE_CHECKING:
    from cw.models import ClientConfig

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
_CLIENT_REF = "client '{client}' in clients.yaml"
_GUARDED = (
    "effective mode clean, but lane '{lane}' REVIEW executor is"
    " backend={backend}, whose REVIEW lane writes no convergence ledger:"
    " REVIEW-stage spawns on this lane are blocked in pre-flight, before the"
    f" review agent runs, with reason {CLEAN_REVIEW_UNSUPPORTED_BACKEND};"
    " earlier stages still run. To fix: {remediation}."
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
    detail = _SUPPORTED.format(
        backend=backend,
        confirm=resolved.name,
        not_independent=resolved.not_independent,
        max_cycles=client.review.effective_max_cycles,
        note=note,
    )
    return CheckResult(_result_name(name, task), ok=True, detail=detail)


def _fix(name: str, backend: str) -> str:
    return remediation(_CLIENT_REF.format(client=name), backend)


def _guarded_result(name: str, task: TicketTask, backend: str) -> CheckResult:
    detail = _GUARDED.format(
        lane=task.lane, backend=backend, remediation=_fix(name, backend)
    )
    return CheckResult(_result_name(name, task), ok=False, detail=detail)


def _unrecognized_result(name: str, task: TicketTask, backend: str) -> CheckResult:
    detail = _UNRECOGNIZED.format(
        lane=task.lane, backend=backend, remediation=_fix(name, backend)
    )
    return CheckResult(_result_name(name, task), ok=False, detail=detail)


def _lane_result(name: str, client: ClientConfig, task: TicketTask) -> CheckResult:
    """Classify one clean-client lane into the four documented states."""
    if Stage.REVIEW not in resolve_pipeline_stages(task, client):
        return _no_review_stage_result(name, task)
    backend = _lane_backend(task, client)
    if clean_mode_supported(backend):
        return _supported_result(name, task, client, backend)
    if backend in CLEAN_REVIEW_GUARDED_BACKENDS:
        return _guarded_result(name, task, backend)
    return _unrecognized_result(name, task, backend)


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
