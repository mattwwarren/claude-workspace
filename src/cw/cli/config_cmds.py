"""Configuration commands: ``config`` (+ concurrency) and ``lane`` management."""

from __future__ import annotations

import json
from io import StringIO

import click
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors, main
from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    clients_file,
    clients_lock,
    concurrency_override_lock,
    get_client,
    get_effective_client,
    load_effective_config,
    load_orchestrator_config,
    show_config,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    OCCUPIED_LANE_STATUSES,
    ConcurrencyOverrides,
    LaneConcurrencyOverride,
    OrchestratorEventType,
    QueueItemStatus,
)

# ``source`` field on LANE_PAUSED / LANE_RESUMED events emitted by an operator
# via ``cw lane pause/resume`` (vs. the circuit breaker in dispatch). See #875.
_LANE_PAUSE_SOURCE_OPERATOR = "operator"


@main.group("config", invoke_without_command=True, help="Show or manage configuration.")
@click.pass_context
@handle_errors
def config_group(ctx: click.Context) -> None:
    """Show or manage configuration.

    When invoked without a subcommand, shows the current configuration
    (backward-compatible with the old ``cw config`` command).
    """
    if ctx.invoked_subcommand is None:
        show_config()


@config_group.command("show", help="Show current configuration.")
@handle_errors
def config_show() -> None:
    """Show current configuration (explicit alias for ``cw config``)."""
    show_config()


@config_group.group("concurrency", help="Manage concurrency overrides.")
def config_concurrency() -> None:
    """Manage concurrency overrides (max_parallel_clients, per-client ceilings)."""


@config_concurrency.command("get", help="Show concurrency configuration layers.")
@click.option("--json", "as_json", is_flag=True, default=False)
@handle_errors
def config_concurrency_get(as_json: bool) -> None:
    """Show concurrency configuration: declared, override, and effective layers."""
    declared = load_orchestrator_config()
    overrides = _load_concurrency_overrides()
    effective = load_effective_config()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "declared": {
                        "max_parallel_clients": declared.max_parallel_clients,
                        "default_ceiling": declared.default_ceiling,
                        "per_client_ceiling": declared.per_client_ceiling,
                    },
                    "override": overrides.model_dump(),
                    "effective": {
                        "max_parallel_clients": effective.max_parallel_clients,
                        "default_ceiling": effective.default_ceiling,
                        "per_client_ceiling": effective.per_client_ceiling,
                    },
                },
                indent=2,
            )
        )
    else:
        click.echo("Declared (orchestrator.yaml):")
        click.echo(f"  max_parallel_clients: {declared.max_parallel_clients}")
        click.echo(f"  default_ceiling: {declared.default_ceiling}")
        click.echo(f"  per_client_ceiling: {declared.per_client_ceiling}")
        click.echo("")
        click.echo("Override (concurrency_overrides.json):")
        click.echo(f"  max_parallel_clients: {overrides.max_parallel_clients}")
        click.echo("")
        click.echo("Effective (merged):")
        click.echo(f"  max_parallel_clients: {effective.max_parallel_clients}")
        click.echo(f"  default_ceiling: {effective.default_ceiling}")
        click.echo(f"  per_client_ceiling: {effective.per_client_ceiling}")


_CONCURRENCY_SET_KEYS: frozenset[str] = frozenset({"max_parallel_clients"})


@config_concurrency.command("set", help="Set a concurrency override.")
@click.argument("assignment")
@handle_errors
def config_concurrency_set(assignment: str) -> None:
    """Set a concurrency override.

    ASSIGNMENT must be in ``key=value`` form, e.g. ``max_parallel_clients=4``.
    Supported keys: max_parallel_clients.
    """
    if "=" not in assignment:
        msg = f"Expected key=value, got: {assignment!r}"
        raise CwError(msg)
    key, _, value_str = assignment.partition("=")
    key = key.strip()
    if key not in _CONCURRENCY_SET_KEYS:
        valid = ", ".join(sorted(_CONCURRENCY_SET_KEYS))
        msg = f"Unknown concurrency key {key!r}. Supported: {valid}"
        raise CwError(msg)
    try:
        value = int(value_str.strip())
    except ValueError as exc:
        msg = f"Value for {key!r} must be an integer, got: {value_str!r}"
        raise CwError(msg) from exc

    with concurrency_override_lock():
        current = _load_concurrency_overrides()
        updates = {key: value}
        updated = current.model_copy(update=updates)
        _save_concurrency_overrides(updated)
    click.echo(f"Set {key}={value}")


@config_concurrency.command("clear", help="Clear concurrency overrides.")
@click.argument("key", required=False, default=None)
@handle_errors
def config_concurrency_clear(key: str | None) -> None:
    """Clear concurrency overrides.

    Without KEY, clears all overrides.
    With KEY, clears only that specific key (e.g. ``max_parallel_clients``).
    """
    if key is None:
        with concurrency_override_lock():
            _save_concurrency_overrides(ConcurrencyOverrides())
        click.echo("Cleared all concurrency overrides.")
    else:
        if key not in _CONCURRENCY_SET_KEYS:
            valid = ", ".join(sorted(_CONCURRENCY_SET_KEYS))
            msg = f"Unknown concurrency key {key!r}. Supported: {valid}"
            raise CwError(msg)
        with concurrency_override_lock():
            current = _load_concurrency_overrides()
            updated = current.model_copy(update={key: None})
            _save_concurrency_overrides(updated)
        click.echo(f"Cleared override for {key!r}.")


# Legacy ``cw lane <cmd> CLIENT NAME`` form: exactly two positional args.
_LANE_CLIENT_AND_NAME_POSITIONAL_COUNT = 2
_lane_client_option = click.option(
    "--client",
    "-c",
    "client_option",
    default=None,
    help="Client name (alternative to the CLIENT positional).",
)
_lane_client_and_name_argument = click.argument(
    "positional", nargs=-1, metavar="[CLIENT] NAME"
)


def _resolve_lane_client(
    client_positional: str | None, client_option: str | None
) -> str:
    """Resolve a lane subcommand's CLIENT from the positional arg or -c/--client.

    Exactly one form must be given. ``cw lane`` historically took CLIENT
    positionally only, unlike nearly every other client-scoped subcommand
    (#1607).
    """
    if client_positional is not None and client_option is not None:
        msg = "Pass CLIENT either positionally or via -c/--client, not both."
        raise CwError(msg)
    if client_positional is not None:
        return client_positional
    if client_option is not None:
        return client_option
    msg = "Missing CLIENT: pass it positionally or via -c/--client."
    raise CwError(msg)


def _resolve_lane_client_and_name(
    positional: tuple[str, ...], client_option: str | None
) -> tuple[str, str]:
    """Resolve a lane subcommand's (CLIENT, NAME) from positional args and/or -c.

    The legacy two-positional form (``CLIENT NAME``) keeps working. When
    -c/--client is given, exactly one positional (``NAME``) is expected
    instead (#1607).
    """
    if client_option is not None:
        if len(positional) != 1:
            msg = "With -c/--client, pass exactly one positional argument (NAME)."
            raise CwError(msg)
        return client_option, positional[0]
    if len(positional) != _LANE_CLIENT_AND_NAME_POSITIONAL_COUNT:
        msg = "Pass CLIENT NAME positionally, or NAME with -c/--client."
        raise CwError(msg)
    return positional[0], positional[1]


# --- Lane command group ---


@main.group("lane", help="Manage dispatch lanes.")
def lane() -> None:
    """Manage dispatch lanes for clients."""


@lane.command("ls", help="List lanes for a client.")
@click.argument("client", required=False, default=None)
@_lane_client_option
@click.option("--json", "as_json", is_flag=True, default=False)
@handle_errors
def lane_ls(client: str | None, client_option: str | None, as_json: bool) -> None:
    """List declared lanes for CLIENT (positional or -c/--client)."""
    resolved_client = _resolve_lane_client(
        client_positional=client, client_option=client_option
    )
    client_cfg = get_effective_client(resolved_client)
    lanes = client_cfg.effective_lanes
    if as_json:
        click.echo(json.dumps([ln.model_dump() for ln in lanes], indent=2))
    else:
        click.echo(f"{'NAME':<20} {'MAX_PARALLEL':>12} {'PRIORITY':>8} {'PAUSED'}")
        click.echo("-" * 55)
        for ln in lanes:
            click.echo(
                f"{ln.name:<20} {ln.max_parallel:>12} {ln.priority:>8} {ln.paused!s}"
            )


@lane.command("add", help="Add a lane to a client.")
@_lane_client_and_name_argument
@_lane_client_option
@click.option("--max-parallel", type=int, default=None)
@click.option("--priority", type=int, default=None)
@handle_errors
def lane_add(
    positional: tuple[str, ...],
    client_option: str | None,
    max_parallel: int | None,
    priority: int | None,
) -> None:
    """Add a lane named NAME to CLIENT (CLIENT NAME, or NAME with -c/--client)."""
    client, name = _resolve_lane_client_and_name(
        positional=positional, client_option=client_option
    )
    effective_max_parallel = max_parallel if max_parallel is not None else 1
    effective_priority = priority if priority is not None else 0

    with clients_lock():
        rt = YAML(typ="rt")
        rt.default_flow_style = False
        clients_path = clients_file()
        content = clients_path.read_text() if clients_path.exists() else "clients:\n"
        doc = rt.load(content)
        if not isinstance(doc, dict) or "clients" not in doc:
            msg = f"{clients_path} has no 'clients:' key."
            raise CwError(msg)
        clients_map = doc["clients"]
        if client not in clients_map:
            msg = f"Client '{client}' not found in {clients_path}"
            raise CwError(msg)
        client_entry = clients_map[client]
        if not isinstance(client_entry, dict):
            client_entry = CommentedMap()
            clients_map[client] = client_entry
        lanes_list = client_entry.get("lanes")
        if lanes_list is None:
            lanes_list = []
            client_entry["lanes"] = lanes_list
        existing_names = [
            ln["name"] if isinstance(ln, dict) else str(ln) for ln in lanes_list
        ]
        if name in existing_names:
            msg = f"Lane '{name}' already exists for client '{client}'."
            raise CwError(msg)
        new_lane: CommentedMap = CommentedMap()
        new_lane["name"] = name
        new_lane["max_parallel"] = effective_max_parallel
        new_lane["priority"] = effective_priority
        lanes_list.append(new_lane)
        buf = StringIO()
        rt.dump(doc, buf)
        atomic_write_text(clients_path, buf.getvalue())

    record_event(
        OrchestratorEventType.LANE_CREATED,
        {
            "client": client,
            "lane": name,
            "max_parallel": effective_max_parallel,
            "priority": effective_priority,
        },
    )
    click.echo(f"Lane '{name}' added to client '{client}'.")


@lane.command("rm", help="Remove a lane from a client.")
@_lane_client_and_name_argument
@_lane_client_option
@handle_errors
def lane_rm(positional: tuple[str, ...], client_option: str | None) -> None:
    """Remove lane NAME from CLIENT (CLIENT NAME, or NAME with -c/--client).

    Fails if any PENDING, RUNNING, BLOCKED_ON_USER, or AWAITING_OPERATOR_SIGNOFF
    tasks are in that lane.
    """
    client, name = _resolve_lane_client_and_name(
        positional=positional, client_option=client_option
    )
    # Composition, not a byte-identical swap: OCCUPIED_LANE_STATUSES alone
    # omits PENDING (never occupies a lane slot), but lane removal must still
    # block on PENDING work waiting in the lane (#990).
    _active_statuses = OCCUPIED_LANE_STATUSES | {QueueItemStatus.PENDING}
    with dev_queue_lock():
        store = load_dev_queue()
        active_in_lane = [
            t
            for t in store.tasks
            if t.client == client and t.lane == name and t.status in _active_statuses
        ]
        if active_in_lane:
            ids = ", ".join(t.ticket_id for t in active_in_lane)
            msg = (
                f"Cannot remove lane '{name}': {len(active_in_lane)} active task(s)"
                f" assigned to it ({ids})."
                " Reassign or cancel them first."
            )
            raise CwError(msg)

        with clients_lock():
            rt = YAML(typ="rt")
            rt.default_flow_style = False
            clients_path = clients_file()
            content = (
                clients_path.read_text() if clients_path.exists() else "clients:\n"
            )
            doc = rt.load(content)
            if not isinstance(doc, dict) or "clients" not in doc:
                msg = f"{clients_path} has no 'clients:' key."
                raise CwError(msg)
            clients_map = doc["clients"]
            if client not in clients_map:
                msg = f"Client '{client}' not found."
                raise CwError(msg)
            client_entry = clients_map[client]
            lanes_list = (
                client_entry.get("lanes") if isinstance(client_entry, dict) else None
            )
            if lanes_list is None:
                msg = f"Client '{client}' has no lanes declared."
                raise CwError(msg)
            new_lanes = [
                ln
                for ln in lanes_list
                if not (isinstance(ln, dict) and ln.get("name") == name)
            ]
            if len(new_lanes) == len(lanes_list):
                msg = f"Lane '{name}' not found for client '{client}'."
                raise CwError(msg)
            client_entry["lanes"] = new_lanes
            buf = StringIO()
            rt.dump(doc, buf)
            atomic_write_text(clients_path, buf.getvalue())

    click.echo(f"Lane '{name}' removed from client '{client}'.")


@lane.command("pause", help="Pause a lane.")
@_lane_client_and_name_argument
@_lane_client_option
@handle_errors
def lane_pause(positional: tuple[str, ...], client_option: str | None) -> None:
    """Pause lane NAME for CLIENT (stops new dispatches to this lane).

    CLIENT NAME positionally, or NAME with -c/--client.
    """
    client, name = _resolve_lane_client_and_name(
        positional=positional, client_option=client_option
    )
    client_cfg = get_client(client)
    declared_names = [ln.name for ln in client_cfg.effective_lanes]
    if name not in declared_names:
        msg = f"Lane '{name}' is not declared for client '{client}'."
        raise CwError(msg)

    lane_key = f"{client}/{name}"
    with concurrency_override_lock():
        current = _load_concurrency_overrides()
        lane_override = current.lanes.get(lane_key, LaneConcurrencyOverride())
        updated_lane = lane_override.model_copy(update={"paused": True})
        current.lanes[lane_key] = updated_lane
        _save_concurrency_overrides(current)

    record_event(
        OrchestratorEventType.LANE_PAUSED,
        {"client": client, "lane": name, "source": _LANE_PAUSE_SOURCE_OPERATOR},
    )
    click.echo(f"Lane '{name}' paused for client '{client}'.")


@lane.command("resume", help="Resume a paused lane.")
@_lane_client_and_name_argument
@_lane_client_option
@handle_errors
def lane_resume(positional: tuple[str, ...], client_option: str | None) -> None:
    """Resume paused lane NAME for CLIENT (CLIENT NAME, or NAME with -c/--client)."""
    client, name = _resolve_lane_client_and_name(
        positional=positional, client_option=client_option
    )
    client_cfg = get_client(client)
    declared_names = [ln.name for ln in client_cfg.effective_lanes]
    if name not in declared_names:
        msg = f"Lane '{name}' is not declared for client '{client}'."
        raise CwError(msg)

    lane_key = f"{client}/{name}"
    with concurrency_override_lock():
        current = _load_concurrency_overrides()
        lane_override = current.lanes.get(lane_key, LaneConcurrencyOverride())
        # Resume also clears the circuit-breaker counter: resume is the sole
        # recovery path for a tripped lane, so a stale count must not re-trip
        # the breaker on the next spawn error. See GitHub #875. It must
        # likewise clear the lane-starved-notify debounce stamp (#1630): a
        # stale ``lane_starved_notify_next_eligible_at`` left over from the
        # prior episode would otherwise silently suppress the *next* trip's
        # first attention notification until that old window expires.
        updated_lane = lane_override.model_copy(
            update={
                "paused": False,
                "consecutive_spawn_errors": 0,
                "lane_starved_notify_next_eligible_at": None,
            }
        )
        current.lanes[lane_key] = updated_lane
        _save_concurrency_overrides(current)

    record_event(
        OrchestratorEventType.LANE_RESUMED,
        {"client": client, "lane": name, "source": _LANE_PAUSE_SOURCE_OPERATOR},
    )
    click.echo(f"Lane '{name}' resumed for client '{client}'.")
