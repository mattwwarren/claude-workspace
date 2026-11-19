"""Standalone tri-state probe of a client's tickets binding (RFC 0019 H1, #3708).

:func:`probe_binding` reports whether a harness can reach the tickets binding:
``ENABLED``, ``ABSENT`` or ``UNKNOWN``, plus whether the binding is bound at
all. H1 adds this module with no callers. H2 owns any fail-closed policy
("unless the stage needs the binding"), so the probe reports the raw state and
decides nothing.

Not a pure file read. The claude-native read runs two local git subprocesses
(``rev-parse --verify`` and ``show <branch>:<path>``) against the client repo.
The codex and opencode reads are in-process file reads through
:func:`cw.backend_mcp_config.read_backend_mcp`.

No observability emissions: no ``log_event``, ``record_event``, ``logging`` or
``warnings`` call, and no lock. The caller owns any logging: each result carries
a closed :class:`ProbeCause`, so a caller tells a missing branch (debug) from an
unreadable read (warn) without parsing ``reason`` (RFC 0019 H2b, #3744). The
pure ``enabledPlugins`` classifier :func:`_plugin_enabled` lives here (#3309).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.backend_mcp_config import McpState, read_backend_mcp
from cw.executor._capabilities import declared_capabilities
from cw.models.capabilities import BindingProbeKind
from cw.worktree import _git_dir, _ref_exists, _run_git

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.client import ClientConfig

_ENABLED_PLUGINS_KEY = "enabledPlugins"
_CLI_GH = "gh"


class Reach(StrEnum):
    """Whether a harness can reach a binding: enabled, absent or unknown."""

    ENABLED = "enabled"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ProbeCause(StrEnum):
    """Why a probe ended where it did (#3744).

    ``READ``: a verdict was read (``ENABLED`` or ``ABSENT``). ``UNBOUND``: the
    client binds nothing this harness can read. ``NO_BRANCH``: the claude-native
    read had no branch to read (none given, or not created yet). ``UNREADABLE``:
    a read was attempted and could not resolve.
    """

    READ = "read"
    UNBOUND = "unbound"
    NO_BRANCH = "no_branch"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class BindingProbe:
    """One probe result.

    ``bound`` says whether the client binds the role for this harness at all;
    ``state`` is the reach verdict and is meaningful only when ``bound``.
    ``cause`` is the closed discriminator a caller branches on.
    """

    state: Reach
    bound: bool
    reason: str
    source: str | None
    cause: ProbeCause = ProbeCause.READ


def _unbound(reason: str) -> BindingProbe:
    return BindingProbe(
        Reach.UNKNOWN,
        bound=False,
        reason=reason,
        source=None,
        cause=ProbeCause.UNBOUND,
    )


def _unreadable(reason: str, source: str | None) -> BindingProbe:
    return BindingProbe(
        Reach.UNKNOWN,
        bound=True,
        reason=reason,
        source=source,
        cause=ProbeCause.UNREADABLE,
    )


def _plugin_enabled(enabled_plugins_raw: object, plugin_id: str) -> bool | None:
    """Exact-match *plugin_id* against an ``enabledPlugins`` value.

    An object (Claude Code's documented ``"<plugin>@<marketplace>": bool``
    shape) resolves to ``False`` when the key is absent, and to its value when
    that value is a boolean; a list of plugin-id strings resolves by
    membership. The complete container must match those shapes. Anything else
    -- including a non-string key, a non-boolean value, or a non-string list
    item -- returns ``None``: unrecognized, so the caller fails open.
    """
    if isinstance(enabled_plugins_raw, dict):
        if not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in enabled_plugins_raw.items()
        ):
            return None
        if plugin_id not in enabled_plugins_raw:
            return False
        return bool(enabled_plugins_raw[plugin_id])
    if isinstance(enabled_plugins_raw, list):
        if not all(isinstance(plugin, str) for plugin in enabled_plugins_raw):
            return None
        return plugin_id in enabled_plugins_raw
    return None


class _NoBranch(str):
    """A :func:`_branch_settings` failure reason meaning the branch is absent."""

    __slots__ = ()


def _parse_settings(text: str, relpath: str, branch: str) -> dict[str, object] | str:
    """Parse *text* as a JSON object, or return why it is not one."""
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError:
        return f"settings file {relpath} on {branch} is not valid JSON"
    if not isinstance(raw, dict):
        return f"settings file {relpath} on {branch} is not a JSON object"
    return raw


def _branch_settings(
    client: ClientConfig, branch: str, relpath: str
) -> dict[str, object] | str:
    """Read *relpath* on *branch* as a JSON object, or return why it could not.

    Returns the parsed object, or a reason string on every failure. Mirrors the
    gate's fail-open reads, but reports each reason instead of swallowing it.
    """
    git_cwd = _git_dir(client)
    try:
        if not _ref_exists(branch, git_cwd):
            return _NoBranch(f"branch {branch} does not exist")
        result = _run_git("show", f"{branch}:{relpath}", cwd=git_cwd, check=False)
    except OSError:
        return "git unavailable"
    except UnicodeDecodeError:
        return f"settings file {relpath} on {branch} is not UTF-8"
    if result.returncode != 0:
        return f"settings file {relpath} missing on {branch}"
    return _parse_settings(result.stdout, relpath, branch)


def _no_branch(reason: str, source: str | None) -> BindingProbe:
    return BindingProbe(
        Reach.UNKNOWN,
        bound=True,
        reason=reason,
        source=source,
        cause=ProbeCause.NO_BRANCH,
    )


def _probe_plugin_branch(client: ClientConfig, branch: str | None) -> BindingProbe:
    plugin_id = client.tickets_plugin_id
    if plugin_id is None:
        return _unbound("claude-native tickets realization has no plugin_id")
    if branch is None:
        return _no_branch("no branch", None)
    relpath = client.tickets_preflight.settings_path
    source = f"{branch}:{relpath}"
    settings = _branch_settings(client, branch, relpath)
    if isinstance(settings, str):
        if isinstance(settings, _NoBranch):
            return _no_branch(str(settings), source)
        return _unreadable(settings, source)
    return _plugin_verdict(settings, plugin_id, relpath, source)


def _plugin_verdict(
    settings: dict[str, object], plugin_id: str, relpath: str, source: str
) -> BindingProbe:
    """Classify a parsed settings object's ``enabledPlugins`` for *plugin_id*."""
    if _ENABLED_PLUGINS_KEY not in settings:
        return _unreadable(f"{_ENABLED_PLUGINS_KEY} key missing from {relpath}", source)
    verdict = _plugin_enabled(settings[_ENABLED_PLUGINS_KEY], plugin_id)
    if verdict is None:
        return _unreadable(f"{_ENABLED_PLUGINS_KEY} shape not recognized", source)
    reach = Reach.ENABLED if verdict else Reach.ABSENT
    return BindingProbe(
        reach, bound=True, reason=f"{plugin_id} {reach.value}", source=source
    )


def _probe_mcp_config(backend: str, client: ClientConfig, root: Path) -> BindingProbe:
    server = client.tracker_mcp_server(backend)
    if server is None:
        return _unbound(f"no tickets mcp server for {backend}")
    reading = read_backend_mcp(backend, server, root)
    source = str(reading.path)
    if reading.state is McpState.ENABLED:
        return BindingProbe(
            Reach.ENABLED, bound=True, reason=reading.reason, source=source
        )
    if reading.state is McpState.ABSENT:
        return BindingProbe(
            Reach.ABSENT, bound=True, reason=reading.reason, source=source
        )
    if reading.state is McpState.DISABLED:
        reason = "disabled: " + reading.reason
        return BindingProbe(Reach.ABSENT, bound=True, reason=reason, source=source)
    return _unreadable(reading.reason, source)


def probe_binding(
    backend: str,
    client: ClientConfig,
    *,
    branch: str | None = None,
    root: Path | None = None,
) -> BindingProbe:
    """Report whether *backend* can reach *client*'s tickets binding.

    *branch* is used only by the claude-native read. *root* defaults to the
    client repo (``repo_path`` or ``workspace_path``), matching the dispatch
    gate; H2 passes ``client.workspace_path``.
    """
    caps = declared_capabilities(backend)
    if caps is None:
        return _unbound(f"unrecognized backend {backend!r}")
    if caps.binding_probe is BindingProbeKind.NONE:
        return _unbound(f"{backend} has no binding read")
    tickets = client.bindings.tickets
    if tickets is None:
        return _unbound("no tickets binding")
    if tickets.cli == _CLI_GH:
        return BindingProbe(
            Reach.ENABLED, bound=True, reason="cli: gh needs no MCP", source=None
        )
    if caps.binding_probe is BindingProbeKind.ENABLED_PLUGINS_BRANCH:
        return _probe_plugin_branch(client, branch)
    base = root if root is not None else (client.repo_path or client.workspace_path)
    return _probe_mcp_config(backend, client, base)
