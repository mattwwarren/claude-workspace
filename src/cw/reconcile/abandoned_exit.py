"""Enablement resolution for the Stop-hook abandoned-exit park (GitHub #2135).

The park itself lives in :func:`cw.reconcile._shared._route_stopped_without_sentinel`
and is driven from ``cw signal-stop``. This module owns only the question
"may it fire for this row?", kept separate so the Stop hook can answer it
**before** paying for the transcript scan that produces the evidence.

The answer is fail-closed end to end: any failure to read or interpret the
orchestrator or client config, an unknown client, and an absent lane entry all
resolve to "disabled". An automatic row mutation is least safe exactly when the
config is broken, and the Stop hook must never raise out of ``claude`` exiting.

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

import logging
from typing import TYPE_CHECKING, NamedTuple

import yaml

from cw.config import load_clients, load_orchestrator_config
from cw.exceptions import CwError

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

logger = logging.getLogger(__name__)

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

# Everything a config read can raise: ``CwError`` covers ``ConfigValidationError``
# and the invalid-client-name error ``load_clients`` raises directly;
# ``OSError`` an unreadable or unwritable file; ``ValueError`` (pydantic's
# ``ValidationError`` and ``UnicodeDecodeError`` are both subclasses) malformed
# content; ``yaml.YAMLError`` invalid YAML. A config we cannot read is treated
# as disabled (fail-closed): the Stop hook's job is to never block claude from
# exiting, and "defer" is the safe answer to every unknown here.
_CONFIG_LOAD_ERRORS = (CwError, OSError, ValueError, yaml.YAMLError)


class _ParkConfig(NamedTuple):
    """The two configs the park gate reads, loaded together (#2135)."""

    config: OrchestratorConfig
    clients: dict[str, ClientConfig]


# Memo of the resolved park config, keyed by client name; ``None`` records
# "disabled" (master switch off, unreadable config, or unknown client). A Stop
# hook is a short-lived ``cw signal-stop`` process, so a module-level cache
# cannot go stale in any way that matters -- its job is to guarantee at most
# one config resolution, and at most one WARNING, per client per process.
_PARK_CONFIG_CACHE: dict[str, _ParkConfig | None] = {}


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


def _load_park_config(client: str) -> _ParkConfig | None:
    """Load the park config for *client*; ``None`` means the park is disabled.

    ``orchestrator.yaml`` is read first and ``clients.yaml`` only when the
    master switch is on, so a shipped-default (switch off) install pays for a
    single config read. Any load failure, and a *client* absent from
    ``clients.yaml``, is logged once at WARNING -- the client name and the
    error class only, never a traceback -- and reads as disabled.
    """
    try:
        config = load_orchestrator_config()
        if not config.park_on_abandoned_exit_enabled:
            return None
        clients = load_clients()
    except _CONFIG_LOAD_ERRORS as exc:
        logger.warning(
            "abandoned-exit park disabled for client %s: config unreadable (%s)",
            client,
            type(exc).__name__,
        )
        return None
    if client not in clients:
        logger.warning(
            "abandoned-exit park disabled for client %s: not in clients.yaml", client
        )
        return None
    return _ParkConfig(config, clients)


def park_gate_open(task: TicketTask) -> bool:
    """Return whether the park may fire for *task*, fail-closed and memoized.

    The single entry point the Stop hook uses. It runs only after the hook's
    cheaper preconditions (headless DAEMON session, empty ``background_tasks``,
    a RUNNING dev-queue row) have held, and before the transcript scan. The
    config is resolved once per client per process (:data:`_PARK_CONFIG_CACHE`);
    every failure path returns False rather than raising.
    """
    if task.client not in _PARK_CONFIG_CACHE:
        _PARK_CONFIG_CACHE[task.client] = _load_park_config(task.client)
    park_config = _PARK_CONFIG_CACHE[task.client]
    if park_config is None:
        return False
    return park_on_abandoned_exit_open(park_config.config, task, park_config.clients)


def clear_park_config_cache() -> None:
    """Drop the memoized park config (for tests; a real process is short-lived)."""
    _PARK_CONFIG_CACHE.clear()
