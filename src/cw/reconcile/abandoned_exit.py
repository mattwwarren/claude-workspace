"""Enablement resolution for the Stop-hook abandoned-exit park (GitHub #2135).

The park itself lives in :func:`cw.reconcile._shared._route_stopped_without_sentinel`
and is driven from ``cw signal-stop``. This module owns only the question
"may it fire for this row?", kept separate so the Stop hook can answer it
**before** reading the worker's recorded park marker or the transcript.

The answer is fail-closed end to end: any failure to read or interpret the
orchestrator or client config, an unknown client, a lane the client never
declared, and an absent lane entry all resolve to "disabled". An automatic row
mutation is least safe exactly when the config is broken, and the Stop hook
must never raise out of ``claude`` exiting.

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
from cw.models import PARK_ON_ABANDONED_EXIT_KEY

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

logger = logging.getLogger(__name__)

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


# Memo of the resolved park config, keyed by ``(client, lane)``; ``None``
# records "disabled" (master switch off, unreadable config, unknown client, or
# a lane the client never declared). A Stop hook is a short-lived ``cw
# signal-stop`` process, so a module-level cache cannot go stale in any way
# that matters -- its job is to guarantee at most one config resolution, and at
# most one WARNING, per (client, lane) per process. The lane joins the key
# because the undeclared-lane WARNING names it: keying on the client alone
# would report only the first lane a process saw.
_PARK_CONFIG_CACHE: dict[tuple[str, str], _ParkConfig | None] = {}


def _lane_declared(client_cfg: ClientConfig, lane: str) -> bool:
    """Whether *lane* is one the client declares in ``clients.yaml``.

    ``lane_names`` synthesizes the implicit ``default`` lane for a client that
    declares none, so a shipped-default install is not gated out by this.
    """
    return lane in client_cfg.lane_names


def resolve_park_on_abandoned_exit_enabled(
    task: TicketTask,
    clients: dict[str, ClientConfig],
) -> bool:
    """Return whether the abandoned-exit park is enabled for *task*.

    A declared-lane gate runs AHEAD of all three tiers: an unknown client, or a
    lane the client's ``clients.yaml`` never declares, returns the off floor
    immediately. Without that gate the ticket tier -- which is read first --
    returned True for a row riding a lane nobody had armed, the one path by
    which an operator who configured nothing could still get an automatic row
    mutation.

    Then 3-tier precedence, highest first (mirrors
    ``resolve_gate_recipe_enabled``):

    1. ``task.park_on_abandoned_exit`` — the ticket-level override, when it
       carries :data:`PARK_ON_ABANDONED_EXIT_KEY`.
    2. ``LaneConfig.park_on_abandoned_exit`` on the task's lane.
    3. :data:`_DEFAULT_PARK_ON_ABANDONED_EXIT` — the hardcoded default-off.

    A lane declared but carrying no park map still falls through the tiers to
    the default with no exception.
    """
    client_cfg = clients.get(task.client)
    if client_cfg is None or not _lane_declared(client_cfg, task.lane):
        return _DEFAULT_PARK_ON_ABANDONED_EXIT
    if (
        task.park_on_abandoned_exit is not None
        and PARK_ON_ABANDONED_EXIT_KEY in task.park_on_abandoned_exit
    ):
        return task.park_on_abandoned_exit[PARK_ON_ABANDONED_EXIT_KEY]
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


def _load_park_config(client: str, lane: str) -> _ParkConfig | None:
    """Load the park config for *client*/*lane*; ``None`` means disabled.

    ``orchestrator.yaml`` is read first and ``clients.yaml`` only when the
    master switch is on, so a shipped-default (switch off) install pays for a
    single config read. Any load failure, a *client* absent from
    ``clients.yaml``, and a *lane* that client never declares are each logged
    once at WARNING -- the names and the error class only, never a traceback --
    and read as disabled.
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
    client_cfg = clients.get(client)
    if client_cfg is None:
        logger.warning(
            "abandoned-exit park disabled for client %s: not in clients.yaml", client
        )
        return None
    if not _lane_declared(client_cfg, lane):
        logger.warning(
            "abandoned-exit park disabled for client %s: lane %s not declared"
            " in clients.yaml",
            client,
            lane,
        )
        return None
    return _ParkConfig(config, clients)


def park_gate_open(task: TicketTask) -> bool:
    """Return whether the park may fire for *task*, fail-closed and memoized.

    The single entry point the Stop hook uses. It runs only after the hook's
    cheaper preconditions (headless DAEMON session, empty ``background_tasks``,
    a RUNNING dev-queue row) have held, and before the marker read and the
    transcript guard. The config is resolved once per (client, lane) per
    process (:data:`_PARK_CONFIG_CACHE`); every failure path returns False
    rather than raising.
    """
    cache_key = (task.client, task.lane)
    if cache_key not in _PARK_CONFIG_CACHE:
        _PARK_CONFIG_CACHE[cache_key] = _load_park_config(task.client, task.lane)
    park_config = _PARK_CONFIG_CACHE[cache_key]
    if park_config is None:
        return False
    return park_on_abandoned_exit_open(park_config.config, task, park_config.clients)


def clear_park_config_cache() -> None:
    """Drop the memoized park config (for tests; a real process is short-lived)."""
    _PARK_CONFIG_CACHE.clear()
