"""All-codex lane check for cw doctor (RFC 0018 D1, #3064).

A lane whose resolved stages all run on the ``codex`` backend, every stage in
:data:`cw.executor.codex.CODEX_SUPPORTED_STAGES`, is an all-codex lane. For each
such lane one ``CheckResult`` named ``all-codex-lane/<client>/<lane>`` reports
it. Lanes that are not all-codex produce nothing, so default doctor output for
mixed or claude-native clients is unchanged.

States, evaluated in order:

1. blocking client: ``ok=True, warn=False``. Blocking mode runs no confirm
   round, so no Claude executor is involved.
2. clean client, REVIEW absent from the lane's stages: ``ok=True,
   warn=False``; the confirm round does not apply.
3. clean client, REVIEW present, ``review.confirm_backend`` unset:
   ``ok=True, warn=True``. The clean-mode confirm round defaults to
   ``claude -p``, so the lane needs the ``claude`` binary on PATH and a Claude
   login; the Claude-free opt-outs are named.
4. clean client, REVIEW present, ``review.confirm_backend`` explicit:
   ``ok=True, warn=False``; the note follows the resolved confirm backend
   (:func:`cw.review_convergence.resolve_confirm_backend`).

Every detail carries the tracker pointer clause. Config-only resolution
(``resolve_executor_config``, ``resolve_pipeline_stages``), never
``shutil.which`` and never a ``tracker_kind`` read. Output carries only lane,
client and config key names. Leaf module: no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.executor.codex import CODEX_SUPPORTED_STAGES
from cw.models import CODEX_BACKEND, Stage, TicketTask
from cw.review_convergence import resolve_confirm_backend

if TYPE_CHECKING:
    from cw.models import ClientConfig

_CHECK_NAME = "all-codex-lane"

_TRACKER_POINTER = (
    " GitHub is reached through gh; a non-GitHub tracker needs the codex-side MCP"
    " (`tracker_mcp_gate.servers.codex`) and is reported by"
    " `tracker-mcp/{name}/{lane}/codex`."
)
_BLOCKING = (
    "all-codex lane: every stage runs on codex and no Claude executor is involved."
    " Blocking mode runs no confirm round."
)
_NO_REVIEW = (
    "all-codex lane: every stage runs on codex. The lane has no REVIEW stage, so"
    " the clean-mode confirm round does not apply."
)
_CONFIRM_UNSET = (
    "all-codex lane: every stage runs on codex. The clean-mode confirm round"
    " defaults to claude -p, so this lane needs the claude binary on PATH and a"
    " Claude login; a missing binary parks confirm_backend_unavailable. Claude-free"
    " opt-outs: review.confirm_backend: none (no confirm round, forfeits"
    " independence) or review.confirm_backend: codex (stays on codex, not"
    " independent)."
)
_CONFIRM_NOTE: dict[str, str] = {
    "none": (
        "all-codex lane: every stage runs on codex. review.confirm_backend is none,"
        " so no independent confirm round runs."
    ),
    "codex": (
        "all-codex lane: every stage runs on codex. review.confirm_backend is codex,"
        " so the confirm round stays on codex and is not independent of the loop."
    ),
    "claude": (
        "all-codex lane: every stage runs on codex. review.confirm_backend is claude,"
        " so the Claude dependency of the confirm round is deliberate."
    ),
}


def _all_codex(stages: list[Stage], task: TicketTask, client: ClientConfig) -> bool:
    """True when the lane has stages and every one runs on codex, supported."""
    return bool(stages) and all(
        resolve_executor_config(stage, task, client).backend == CODEX_BACKEND
        and stage in CODEX_SUPPORTED_STAGES
        for stage in stages
    )


def _detail_and_warn(client: ClientConfig, stages: list[Stage]) -> tuple[str, bool]:
    """The detail text for one all-codex lane and whether it warns."""
    if not client.review.is_clean:
        return _BLOCKING, False
    if Stage.REVIEW not in stages:
        return _NO_REVIEW, False
    configured = client.review.confirm_backend
    if configured is None:
        return _CONFIRM_UNSET, True
    resolved = resolve_confirm_backend(configured, CODEX_BACKEND)
    return _CONFIRM_NOTE[resolved.name], False


def _lane_result(name: str, client: ClientConfig, lane: str) -> CheckResult | None:
    """One result for *lane* when it is all-codex, else None."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=name, lane=lane, stage=Stage.REVIEW)
    stages = resolve_pipeline_stages(task, client)
    if not _all_codex(stages, task, client):
        return None
    detail, warn = _detail_and_warn(client, stages)
    return CheckResult(
        f"{_CHECK_NAME}/{name}/{lane}",
        ok=True,
        warn=warn,
        detail=detail + _TRACKER_POINTER.format(name=name, lane=lane),
    )


def _check_all_codex_lane(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Report every all-codex lane of every client, blocking or clean."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        for lane in client.effective_lanes:
            result = _lane_result(name, client, lane.name)
            if result is not None:
                results.append(result)
    return results
