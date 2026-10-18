"""``cw lane set`` and its ``cw config lane set`` alias (#2697).

The narrow, agent-reachable way to change a lane's ``max_parallel`` and
``attempt_ceiling`` without hand-editing ``clients.yaml``. The whitelist is
deliberately just those two keys (operator resolution R1-R3): the human opt-in
gates (``signoff``, ``finalize_gate``), ``reap_policy``, the guard toggles and
every other lane key are refused before any file access, because the
orchestrator never loosens a gate it is subject to (ADR-0020). ``paused``
stays with ``cw lane pause`` / ``cw lane resume``.

Flow: the worker guard first, then parsing and the whitelist (no I/O), then
one ``clients_lock(bounded=True)`` section that reads the old values, decides
whether anything changes, validates the edited entry as a ``LaneConfig`` and
only then writes. The ``lane.config_changed`` audit event is recorded after
the lock, only when something changed.

Kept out of :mod:`cw.cli.config_cmds` for module size; the ``lane`` group,
its client helpers and the shared audit emitter are imported from there.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import click
from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap

from cw.cli._base import handle_errors
from cw.cli._clients_doc import dump_clients_doc, load_clients_doc, to_plain
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.cli.config_cmds import (
    _emit_lane_config_changed,
    _lane_client_option,
    _render_config_value,
    _resolve_lane_client_and_name,
    config_group,
    lane,
)
from cw.config import clients_file, clients_lock
from cw.exceptions import CwError
from cw.models import LaneConfig

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["config_lane", "lane_set"]

#: The only lane keys ``cw lane set`` writes. Widening this is a deliberate,
#: test-pinned edit (``tests/test_cli_lane_set.py``).
_SETTABLE_KEYS: frozenset[str] = frozenset({"max_parallel", "attempt_ceiling"})
_SUPPORTED = ", ".join(sorted(_SETTABLE_KEYS))
#: Human opt-in gates, refused with a message naming the gate (R2).
_GATE_KEYS: frozenset[str] = frozenset({"signoff", "finalize_gate"})
_ADR_0020 = "docs/adr/0020-the-operator-is-the-orchestrator-session.md"
#: VALUE spellings that remove the key (case-insensitive).
_UNSET_TOKENS: frozenset[str] = frozenset({"null", "~", ""})
_BOOL_TOKENS: dict[str, bool] = {"true": True, "false": False}
_INT_RE = re.compile(r"-?\d+")

_LANE_SET_IN_WORKER_MSG = (
    "Refusing to change lane configuration from inside a dispatch worker"
    " (.claude/cw-context.json reports headless). A worker must not loosen its"
    " own lane's limits. Run `cw lane set` from the orchestrator session."
)


def _refuse_unsettable(key: str) -> None:
    """Raise :class:`CwError` unless *key* is in :data:`_SETTABLE_KEYS`."""
    if key in _SETTABLE_KEYS:
        return
    if key in _GATE_KEYS:
        msg = (
            f"`{key}` is a human opt-in gate ({_ADR_0020}); the orchestrator"
            f" never sets, clears or releases it. Supported: {_SUPPORTED}"
        )
    else:
        msg = f"Unknown or unsettable lane key '{key}'. Supported: {_SUPPORTED}"
        if key == "paused":
            msg += ". Use `cw lane pause` / `cw lane resume` to pause or resume a lane."
    raise CwError(msg)


def _parse_value(raw: str) -> object:
    """Parse one VALUE deterministically (no YAML 1.1 coercion).

    ``null``, ``~`` or empty -> None (unset); ``true``/``false`` -> bool, both
    case-insensitive; an optionally signed run of digits -> int; anything else
    stays the raw string, for the validator to reject.
    """
    text = raw.strip()
    lowered = text.lower()
    if lowered in _UNSET_TOKENS:
        return None
    if lowered in _BOOL_TOKENS:
        return _BOOL_TOKENS[lowered]
    if _INT_RE.fullmatch(text):
        return int(text)
    return text


def _check_max_parallel(value: object, raw: str) -> None:
    """``max_parallel`` must be unset or a positive int (bool excluded).

    ``LaneConfig.max_parallel`` has no range constraint, and 0 would silently
    stall a lane from an agent-reachable command; pausing is the sanctioned
    way to stop dispatch.
    """
    if value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 1
    ):
        return
    msg = (
        f"max_parallel must be a positive integer (got {raw!r}). Use"
        " `cw lane pause` to stop dispatch to a lane, or max_parallel=null to"
        " restore the default."
    )
    raise CwError(msg)


def _parse_assignments(tokens: list[str]) -> dict[str, object]:
    """Parse and whitelist-check every KEY=VALUE token, in argument order."""
    if not tokens:
        msg = f"Pass at least one KEY=VALUE assignment (supported keys: {_SUPPORTED})."
        raise CwError(msg)
    updates: dict[str, object] = {}
    for token in tokens:
        key, _, raw = token.partition("=")
        key = key.strip()
        _refuse_unsettable(key)
        if key in updates:
            msg = f"Key '{key}' is assigned more than once."
            raise CwError(msg)
        value = _parse_value(raw)
        if key == "max_parallel":
            _check_max_parallel(value, raw)
        updates[key] = value
    return updates


def _find_lane_entry(
    doc: CommentedMap, client: str, name: str, clients_path: Path
) -> CommentedMap:
    """The round-trip mapping of lane *name* under *client* in *doc*."""
    clients_map = doc["clients"]
    if not isinstance(clients_map, CommentedMap) or client not in clients_map:
        msg = f"Client '{client}' not found in {clients_path}"
        raise CwError(msg)
    client_entry = clients_map[client]
    lanes_list = (
        client_entry.get("lanes") if isinstance(client_entry, CommentedMap) else None
    )
    if not lanes_list:
        msg = (
            f"Client '{client}' has no lanes declared in {clients_path}."
            " Add one with `cw lane add`."
        )
        raise CwError(msg)
    for entry in lanes_list:
        if isinstance(entry, CommentedMap) and entry.get("name") == name:
            return entry
    msg = f"Lane '{name}' not found for client '{client}'."
    raise CwError(msg)


def _same_value(old: object, new: object) -> bool:
    """Equal and of the same type, so ``false`` never matches ``0``."""
    return type(old) is type(new) and old == new


def _apply_updates(
    entry: CommentedMap, updates: dict[str, object]
) -> dict[str, list[object]]:
    """Apply *updates* to *entry* in place; return ``{key: [old, new]}`` changed."""
    changes: dict[str, list[object]] = {}
    for key, new in updates.items():
        old = to_plain(entry.get(key))
        if _same_value(old, new):
            continue
        changes[key] = [old, new]
        if new is None:
            del entry[key]
        else:
            entry[key] = new
    return changes


def _validate_lane_entry(entry: CommentedMap, client: str, name: str) -> None:
    """Validate the edited *entry* as a ``LaneConfig`` before it is written."""
    try:
        LaneConfig.model_validate(to_plain(entry))
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        msg = "\n".join(
            [f"Invalid settings for lane '{name}' of client '{client}':", *lines]
        )
        raise CwError(msg) from exc


@lane.command("set")
@click.argument("positional", nargs=-1, metavar="[CLIENT] NAME KEY=VALUE...")
@_lane_client_option
@handle_errors
def lane_set(positional: tuple[str, ...], client_option: str | None) -> None:
    """Set max_parallel or attempt_ceiling on a lane.

    Sets max_parallel and/or attempt_ceiling on lane NAME of CLIENT. Pass
    CLIENT NAME KEY=VALUE..., or NAME KEY=VALUE... with -c/--client.

    Several assignments apply together or not at all. KEY=null (or KEY=)
    removes the key so the lane inherits again. max_parallel takes a positive
    integer (use `cw lane pause` to stop dispatch). attempt_ceiling takes a
    positive integer, false (disable the ceiling) or null. Gate keys
    (signoff, finalize_gate), reap_policy and the guard toggles are not
    settable here (ADR-0020); edit clients.yaml by hand.

    clients.yaml comments and layout survive byte-for-byte only when the file
    is in ruamel.yaml's default style (2-space indent, sequence dashes at
    their parent key's indent, lines within 80 columns); any other layout is
    rewritten in that style. Refused inside a dispatch worker.
    """
    refuse_inside_dispatch_worker(_LANE_SET_IN_WORKER_MSG)
    names = tuple(token for token in positional if "=" not in token)
    updates = _parse_assignments([token for token in positional if "=" in token])
    client, name = _resolve_lane_client_and_name(
        positional=names, client_option=client_option
    )
    # bounded=True (#2501): operator command; only the worker guard and
    # argument parsing precede the lock.
    with clients_lock(bounded=True):
        clients_path = clients_file()
        rt, doc = load_clients_doc(clients_path)
        entry = _find_lane_entry(doc, client, name, clients_path)
        changes = _apply_updates(entry, updates)
        if changes:
            _validate_lane_entry(entry, client, name)
            dump_clients_doc(rt, doc, clients_path)

    if not changes:
        click.echo(
            f"Lane '{name}' of client '{client}' already has these values;"
            " nothing written."
        )
        return
    _emit_lane_config_changed(client, name, changes)
    click.echo(f"Lane '{name}' updated for client '{client}':")
    for key, (old, new) in changes.items():
        click.echo(
            f"  {key}: {_render_config_value(old)} -> {_render_config_value(new)}"
        )


@config_group.group("lane", help="Edit dispatch lane settings (same as `cw lane set`).")
def config_lane() -> None:
    """Edit dispatch lane settings (same as `cw lane set`)."""


config_lane.add_command(lane_set)
