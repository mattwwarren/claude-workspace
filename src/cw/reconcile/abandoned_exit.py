"""Enablement resolution for the Stop-hook abandoned-exit park (GitHub #2135).

The park itself lives in :func:`cw.reconcile._shared._route_stopped_without_sentinel`
and is driven from ``cw signal-stop``. This module owns only the question
"may it fire for this row?", kept separate so the Stop hook can answer it
**before** paying for the transcript scan that produces the evidence.

Shaped on ``cw.reconcile.gate_recipes``' enablement pair
(``resolve_gate_recipe_enabled`` / ``_recipe_gate_open``) deliberately: a
default-off master switch in ``orchestrator.yaml`` plus a per-lane map whose
floor is False is the release-playbook floor for a new state-mutating
auto-actor, and reusing that exact shape means an operator arms this the same
way they arm a gate recipe.

Import discipline: this module sits above ``cw.models`` and ``cw.config`` and
below ``cw.cli``. It imports nothing from ``cw.reconcile._shared``, so
``cw.cli.stop_hook`` can import it through the package re-export with no
cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from cw.config import load_clients, load_orchestrator_config
from cw.exceptions import ConfigValidationError

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

# The single recognised key of the per-lane / per-ticket
# ``park_on_abandoned_exit`` maps. A map rather than a bare bool so the two
# override tiers keep the shape their ``gate_recipes`` / ``review_recipes``
# siblings use; ``cw.models.tasks._validate_park_on_abandoned_exit_keys``
# holds the same literal (models sits below reconcile, so it cannot import
# this one) and rejects anything else at config-load time.
PARK_ON_ABANDONED_EXIT_KEY = "park_on_abandoned_exit"

# Tier-3 hardcoded floor for the per-lane resolver. NOT a config field — it is
# what the ticket and lane tiers fall through to. False because the park
# mutates a dev-queue row with no human in the loop.
_DEFAULT_PARK_ON_ABANDONED_EXIT = False

# Everything a config read can raise. A config we cannot read is treated as
# disabled (fail-closed): the Stop hook's job is to never block claude from
# exiting, and "defer" is the safe answer to every unknown here.
_CONFIG_LOAD_ERRORS = (ConfigValidationError, OSError, yaml.YAMLError)


def resolve_park_on_abandoned_exit_enabled(
    task: TicketTask,
    clients: dict[str, ClientConfig],
) -> bool:
    """Return whether the abandoned-exit park is enabled for *task*.

    3-tier precedence, highest first (mirrors ``resolve_gate_recipe_enabled``):

    1. ``task.park_on_abandoned_exit`` — the ticket-level override, when it
       carries :data:`PARK_ON_ABANDONED_EXIT_KEY`.
    2. ``LaneConfig.park_on_abandoned_exit`` on the task's lane.
    3. :data:`_DEFAULT_PARK_ON_ABANDONED_EXIT` — the hardcoded default-off.

    Robust to a missing client (absent from *clients*) or a missing lane
    (absent from the client's ``effective_lanes``): either falls straight
    through to the default with no exception.
    """
    if (
        task.park_on_abandoned_exit is not None
        and PARK_ON_ABANDONED_EXIT_KEY in task.park_on_abandoned_exit
    ):
        return task.park_on_abandoned_exit[PARK_ON_ABANDONED_EXIT_KEY]
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if (
                lane_cfg.name == task.lane
                and lane_cfg.park_on_abandoned_exit is not None
                and PARK_ON_ABANDONED_EXIT_KEY in lane_cfg.park_on_abandoned_exit
            ):
                return lane_cfg.park_on_abandoned_exit[PARK_ON_ABANDONED_EXIT_KEY]
    return _DEFAULT_PARK_ON_ABANDONED_EXIT


def park_on_abandoned_exit_open(
    config: OrchestratorConfig,
    task: TicketTask,
    clients: dict[str, ClientConfig],
) -> bool:
    """Return whether the park may fire for *task* right now.

    Composes the master switch with the per-lane/per-ticket resolution, so
    every caller shares one gating check instead of drifting copies —
    the same reason ``cw.reconcile.gate_recipes._recipe_gate_open`` exists.
    """
    return config.park_on_abandoned_exit_enabled and (
        resolve_park_on_abandoned_exit_enabled(task, clients)
    )


def load_armed_park_config() -> OrchestratorConfig | None:
    """Return the orchestrator config, or None when the master switch is off.

    The cheapest of the Stop hook's preconditions and therefore the first one
    it runs: one ``orchestrator.yaml`` read, no dev-queue lookup and no
    transcript scan. A config that cannot be read reads as disabled.
    """
    try:
        config = load_orchestrator_config()
    except _CONFIG_LOAD_ERRORS:
        return None
    return config if config.park_on_abandoned_exit_enabled else None


def park_gate_open(config: OrchestratorConfig, task: TicketTask) -> bool:
    """:func:`park_on_abandoned_exit_open` with ``clients.yaml`` loaded here.

    Separate from :func:`load_armed_park_config` so the ``clients.yaml`` read
    only happens once the master switch is on and a candidate row has been
    found. A clients.yaml that cannot be read reads as disabled.
    """
    try:
        clients = load_clients()
    except _CONFIG_LOAD_ERRORS:
        return False
    return park_on_abandoned_exit_open(config, task, clients)
