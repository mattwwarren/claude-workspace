"""Per-lane effective codex fix-loop state check for cw doctor (#2542).

Setup-time visibility into whether the ``codex exec --sandbox workspace-write``
fix loop is on for each lane whose review executor is codex, and which source
decided it: the lane's ``codex_fix_loop_enabled`` or the global
``default_codex_fix_loop_enabled`` in ``orchestrator.yaml``. One ``CheckResult``
per codex review lane, always ``ok=True`` (advisory, mirroring
``agent-spec-drift``); ``warn=True`` means the loop is on. Read-only: an absent
``orchestrator.yaml`` is never created. No precedence logic lives here -- the
decision is read straight through ``cw.codex_background._resolve_codex_fix_loop``,
the runtime's own resolver. "Has a codex review executor" is config-only
(``resolve_executor_config``), never ``shutil.which("codex")``. Output carries
only a bool, a source label, config key names, and lane and client names; a
load failure prints the exception class name, never its message. Leaf module --
no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import yaml
from pydantic import ValidationError

from cw.codex_background import _resolve_codex_fix_loop
from cw.config import load_orchestrator_config, orchestrator_config_file
from cw.doctor._shared import CheckResult
from cw.exceptions import CwError
from cw.executor import resolve_executor_config
from cw.models import CODEX_BACKEND, OrchestratorConfig, Stage, TicketTask

if TYPE_CHECKING:
    from cw.codex_background import CodexFixLoopSource
    from cw.models import ClientConfig

# Check name for the per-lane check ("<name>/<client>/<lane>") and the
# single load-failure result.
_CHECK_NAME = "codex-fix-loop"

_GLOBAL_ON = (
    "codex fix loop is ON via global default: default_codex_fix_loop_enabled is"
    " true in orchestrator.yaml, so codex exec --sandbox workspace-write may"
    " commit fixes autonomously on this lane. To change it, set"
    " default_codex_fix_loop_enabled in orchestrator.yaml, or set"
    " codex_fix_loop_enabled on lane '{lane}' of client '{client}' in"
    " clients.yaml."
)
_GLOBAL_OFF = (
    "codex fix loop is OFF via global default: default_codex_fix_loop_enabled is"
    " false or unset in orchestrator.yaml. To turn it on, set"
    " default_codex_fix_loop_enabled: true in orchestrator.yaml, or set"
    " codex_fix_loop_enabled: true on lane '{lane}' of client '{client}' in"
    " clients.yaml."
)
_LANE_ON = (
    "codex fix loop is ON via lane: codex_fix_loop_enabled is true on lane"
    " '{lane}' of client '{client}' in clients.yaml, so codex exec --sandbox"
    " workspace-write may commit fixes autonomously on this lane. To change it,"
    " set codex_fix_loop_enabled to false or remove the key (removing it defers"
    " to default_codex_fix_loop_enabled in orchestrator.yaml)."
)
_LANE_OFF = (
    "codex fix loop is OFF via lane: codex_fix_loop_enabled is false on lane"
    " '{lane}' of client '{client}' in clients.yaml, which overrides"
    " default_codex_fix_loop_enabled in orchestrator.yaml. To change it, set"
    " codex_fix_loop_enabled to true or remove the key (removing it defers to"
    " default_codex_fix_loop_enabled in orchestrator.yaml)."
)
_LOAD_FAILURE = (
    "codex fix loop state unknown: orchestrator.yaml could not be loaded"
    " ({exc_class}), so default_codex_fix_loop_enabled cannot be resolved. See"
    " the orchestrator.yaml check."
)

# Keyed by (source, enabled) so ``_lane_result`` selects a template without branching.
_DETAIL_BY_STATE: dict[tuple[CodexFixLoopSource, bool], str] = {
    ("global default", True): _GLOBAL_ON,
    ("global default", False): _GLOBAL_OFF,
    ("lane", True): _LANE_ON,
    ("lane", False): _LANE_OFF,
}


class _GlobalConfig(NamedTuple):
    """The loaded global config, or the class name of the failure that blocked it."""

    config: OrchestratorConfig | None
    error_class: str


def _codex_review_lanes(
    clients: dict[str, ClientConfig],
) -> list[tuple[str, ClientConfig, TicketTask]]:
    """List each (client name, client, synthetic task) whose lane reviews on codex."""
    lanes: list[tuple[str, ClientConfig, TicketTask]] = []
    for name, client in clients.items():
        for lane in client.effective_lanes:
            # ticket_id="" is the documented "no associated ticket" sentinel.
            task = TicketTask(
                ticket_id="", client=name, lane=lane.name, stage=Stage.REVIEW
            )
            backend = resolve_executor_config(Stage.REVIEW, task, client).backend
            if backend == CODEX_BACKEND:
                lanes.append((name, client, task))
    return lanes


def _load_global_config() -> _GlobalConfig:
    """Load orchestrator.yaml without creating it; report failures by class name."""
    if not orchestrator_config_file().exists():
        return _GlobalConfig(OrchestratorConfig(), "")
    try:
        return _GlobalConfig(load_orchestrator_config(), "")
    except (OSError, yaml.YAMLError, CwError, ValidationError) as exc:
        return _GlobalConfig(None, type(exc).__name__)


def _lane_result(
    name: str, client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> CheckResult:
    """Report the effective fix-loop state and its source for one codex lane."""
    resolution = _resolve_codex_fix_loop(client, task, config)
    template = _DETAIL_BY_STATE[(resolution.source, resolution.enabled)]
    return CheckResult(
        f"{_CHECK_NAME}/{name}/{task.lane}",
        ok=True,
        warn=resolution.enabled,
        detail=template.format(client=name, lane=task.lane),
    )


def _check_codex_fix_loop(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Report the codex fix-loop state for every codex review lane of *clients*."""
    lanes = _codex_review_lanes(clients)
    if not lanes:
        return []
    loaded = _load_global_config()
    if loaded.config is None:
        detail = _LOAD_FAILURE.format(exc_class=loaded.error_class)
        return [CheckResult(_CHECK_NAME, ok=True, detail=detail)]
    config = loaded.config
    return [_lane_result(name, client, task, config) for name, client, task in lanes]
