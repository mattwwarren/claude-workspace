"""Tick-side binding reachability preflight (RFC 0019 H2b, #3744).

Replaces the tracker-MCP gate (#2442, #3055, #3060): does a PENDING row's
harness verifiably fail to reach the tickets binding for a stage that needs it?
:func:`assess_binding` (shared with ``cw doctor``) decides from ``STAGE_NEEDS``,
:func:`cw.binding_probe.probe_binding` (#3309) and
:func:`cw.binding_tools.check_tools`, and never logs.
:func:`resolve_binding_hits` is the opt-in tick and owns the warn-once state
and every log line. :func:`preflight_before_spawn` is the codex CLI park.

The tracker policy (:func:`_tracker_gate`, one ``resolve_tracker`` read per
reader): a GitHub tracker is never read, an unset one fails open with one
warning, and codex PLAN on any other tracker with an unusable ``servers.codex``
parks ``binding_unbound`` before any read (the only fail-closed case); a missing
branch is debug only and any other unknown warns once. Every logger is
``cw.dispatch``, as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from cw.binding_probe import BindingProbe, ProbeCause, Reach, probe_binding
from cw.binding_tools import ToolCheck, ToolState, check_tools
from cw.dispatch.claim.codex_capability import _cached_codex_capability_diagnosis
from cw.dispatch.claim.outcome import park_before_spawn, reset_park_streak
from cw.executor import resolve_executor_config
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    QueueItemStatus,
    Stage,
    resolve_feature_branch,
)
from cw.models.bindings import Harness, Role
from cw.stage_preflight import STAGE_NEEDS, PreflightReason, needs_for
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.tracker_io._argv import is_valid_name

if TYPE_CHECKING:
    from pathlib import Path

    from cw.dispatch.claim.outcome import _SpawnOutcome
    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")

#: Every stage some harness needs the tickets binding for: {PLAN, IMPL}. The
#: tick scans these rows; the claim screen re-checks under lock.
BINDING_STAGES: frozenset[Stage] = frozenset(
    stage for (_, stage), needs in STAGE_NEEDS.items() if Role.TICKETS in needs.bindings
)

#: A codex unbound hit's strings, byte-identical to the old gate's (parity).
_CODEX_SERVERS_FILE = "clients.yaml tracker_mcp_gate.servers"
_CODEX_SERVER_UNSET = "servers.codex (unset)"
_TRACKER_UNSET = "tracker unset in .claude/project-config.yaml"
_UNBOUND_PLUGIN = "no bindings.tickets.mcp.claude-native.plugin_id names a plugin"
_UNBOUND_SERVER = "no bindings.tickets.mcp.{}.server names this harness's MCP server"


# Warn-once dedupe: (client, key) -> the reason last warned, keyed by branch,
# "<backend>:<config path>" or "<backend>:tracker", so one unreadable file does
# not warn every tick. A clean verdict pops the key. Tests reset it.
_WARNED_FAIL_OPEN: dict[tuple[str, str], str] = {}


class ReachState(StrEnum):
    """Where :func:`assess_binding` ended for one (harness, stage).

    ``UNBOUND`` parks only for codex PLAN; ``NO_BRANCH`` (no
    claude-native branch to read) is debug only; ``UNKNOWN`` (a read that
    could not resolve) and ``TRACKER_UNSET`` fail open with a warning.
    """

    NOT_NEEDED = "not_needed"
    GITHUB = "github"
    TRACKER_UNSET = "tracker_unset"
    UNBOUND = "unbound"
    NO_BRANCH = "no_branch"
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class BindingHit:
    """One park: the old gate hit's four values byte for byte, plus the reason,
    whether ``expected`` is a plugin or a server, and the excluded tool names."""

    branch: str
    file_inspected: str
    expected: str
    backend: str = CLAUDE_NATIVE_BACKEND
    reason: PreflightReason = PreflightReason.BINDING_UNREACHABLE
    expected_kind: Literal["plugin", "server"] = "plugin"
    missing_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    """A state, an optional park, the warn-once ``(key, reason)`` and the key a
    clean verdict re-arms, plus the reason, file inspected and tool check."""

    state: ReachState
    hit: BindingHit | None = None
    warn: tuple[str, str] | None = None
    rearm: str | None = None
    detail: str = ""
    source: str | None = None
    tools: ToolCheck | None = None


@dataclass
class ReachReader:
    """Per-call memo of the tracker system and one read per backend, rooted at
    *root* (``workspace_path`` on the tick, ``repo_path or workspace_path`` in
    doctor). A flag, not a ``None`` sentinel: ``None`` is also a read result."""

    client: ClientConfig
    root: Path
    _system_read: bool = False
    _system: str | None = None
    _probes: dict[str, BindingProbe] = field(default_factory=dict)
    _tools: dict[str, ToolCheck] = field(default_factory=dict)

    def system(self) -> str | None:
        if not self._system_read:
            self._system = resolve_tracker(self.root)
            self._system_read = True
        return self._system

    def server_probe(self, backend: str) -> BindingProbe:
        if backend not in self._probes:
            self._probes[backend] = probe_binding(backend, self.client, root=self.root)
        return self._probes[backend]

    def tool_check(self, backend: str, server: str) -> ToolCheck:
        if backend not in self._tools:
            self._tools[backend] = check_tools(self.client, backend, server, self.root)
        return self._tools[backend]


def _unknown(
    key: str, probe: BindingProbe, tools: ToolCheck | None = None
) -> Assessment:
    """A read that could not resolve: warn once under *key*, fail open."""
    reason = probe.reason
    return Assessment(
        ReachState.UNKNOWN,
        warn=(key, reason),
        detail=reason,
        source=probe.source,
        tools=tools,
    )


def _server_hit(
    branch: str,
    backend: str,
    server: str,
    probe: BindingProbe,
    missing: tuple[str, ...] = (),
) -> BindingHit:
    """A server park; a non-empty *missing* makes it ``binding_tool_missing``."""
    tool = PreflightReason.BINDING_TOOL_MISSING
    reason = tool if missing else PreflightReason.BINDING_UNREACHABLE
    return BindingHit(
        branch=branch,
        file_inspected=str(probe.source),
        expected=server,
        backend=backend,
        reason=reason,
        expected_kind="server",
        missing_tools=missing,
    )


def _needs_tickets(backend: str, stage: Stage) -> bool:
    try:
        harness = Harness(backend)
    except ValueError:
        return False
    return Role.TICKETS in needs_for(harness, stage).bindings


def _assess_plugin(reader: ReachReader, branch: str | None) -> Assessment:
    """claude-native: the branch ``enabledPlugins`` read; tracker not consulted."""
    plugin_id = reader.client.tickets_plugin_id
    tools = reader.tool_check(CLAUDE_NATIVE_BACKEND, plugin_id or "")
    if plugin_id is None:
        return Assessment(ReachState.UNBOUND, detail=_UNBOUND_PLUGIN, tools=tools)
    probe = probe_binding(
        CLAUDE_NATIVE_BACKEND, reader.client, branch=branch, root=reader.root
    )
    # A ``None`` branch always probes NO_BRANCH; the test narrows the type.
    if probe.cause is ProbeCause.NO_BRANCH or branch is None:
        return Assessment(ReachState.NO_BRANCH, detail=probe.reason, tools=tools)
    if probe.cause is not ProbeCause.READ:
        return _unknown(branch, probe, tools)
    if probe.state is Reach.ENABLED:
        return Assessment(
            ReachState.REACHABLE, rearm=branch, source=probe.source, tools=tools
        )
    hit = BindingHit(branch, reader.client.tickets_preflight.settings_path, plugin_id)
    return Assessment(
        ReachState.UNREACHABLE,
        hit=hit,
        rearm=branch,
        source=probe.source,
        tools=tools,
    )


def _tracker_gate(reader: ReachReader, backend: str) -> Assessment | None:
    """GitHub needs no MCP; an unset tracker fails open. ``None``: read on."""
    system = reader.system()
    if system == TRACKER_GITHUB_ISSUES:
        return Assessment(ReachState.GITHUB, detail="GitHub tracker, reached via gh")
    if not system:
        return Assessment(
            ReachState.TRACKER_UNSET,
            warn=(f"{backend}:tracker", _TRACKER_UNSET),
            detail=_TRACKER_UNSET,
        )
    return None


def _reachable_server(
    reader: ReachReader, backend: str, server: str, branch: str, probe: BindingProbe
) -> Assessment:
    """A reachable server: run the tool-name check; a missing name parks."""
    label = f"{backend}:{probe.source}"
    tools = reader.tool_check(backend, server)
    hit = None
    if tools.state is ToolState.MISSING:
        hit = _server_hit(branch, backend, server, probe, tools.missing)
    return Assessment(
        ReachState.REACHABLE, hit=hit, rearm=label, source=probe.source, tools=tools
    )


def _assess_server(
    reader: ReachReader, backend: str, server: str, branch: str
) -> Assessment:
    """codex/opencode: the in-process config read, after the tracker policy."""
    gated = _tracker_gate(reader, backend)
    if gated is not None:
        return gated
    probe = reader.server_probe(backend)
    label = f"{backend}:{probe.source}"
    tools = reader.tool_check(backend, server)
    if probe.state is Reach.UNKNOWN:
        return _unknown(label, probe, tools)
    if probe.state is Reach.ENABLED:
        return _reachable_server(reader, backend, server, branch, probe)
    hit = _server_hit(branch, backend, server, probe)
    return Assessment(
        ReachState.UNREACHABLE,
        hit=hit,
        rearm=label,
        detail=probe.reason,
        source=probe.source,
        tools=tools,
    )


def _assess_codex_unbound(
    reader: ReachReader, server: str | None, branch: str
) -> Assessment:
    """codex PLAN with no usable ``servers.codex`` (R4)."""
    gated = _tracker_gate(reader, CODEX_BACKEND)
    if gated is not None:
        return gated
    hit = BindingHit(
        branch=branch,
        file_inspected=_CODEX_SERVERS_FILE,
        expected=_CODEX_SERVER_UNSET if server is None else server,
        backend=CODEX_BACKEND,
        reason=PreflightReason.BINDING_UNBOUND,
        expected_kind="server",
    )
    detail = _UNBOUND_SERVER.format(CODEX_BACKEND)
    if server is not None:
        detail += f" (servers.codex {server!r} is not a valid name)"
    return Assessment(ReachState.UNBOUND, hit=hit, detail=detail)


def assess_binding(
    reader: ReachReader, backend: str, stage: Stage, branch: str | None
) -> Assessment:
    """Assess *backend*'s tickets reach for *stage*; no logging. *branch*
    ``None`` (doctor) makes the claude-native read ``NO_BRANCH``."""
    if not _needs_tickets(backend, stage):
        return Assessment(ReachState.NOT_NEEDED)
    if backend == CLAUDE_NATIVE_BACKEND:
        return _assess_plugin(reader, branch)
    server = reader.client.tracker_mcp_server(backend)
    row_branch = branch or ""
    if backend == CODEX_BACKEND and (server is None or not is_valid_name(server)):
        return _assess_codex_unbound(reader, server, row_branch)
    if server is None:
        return Assessment(ReachState.UNBOUND, detail=_UNBOUND_SERVER.format(backend))
    return _assess_server(reader, backend, server, row_branch)


def _warn_fail_open_once(client_name: str, key: str, reason: str) -> None:
    """Log one fail-open *reason* for *key*, suppressing per-tick repeats."""
    dedupe = (client_name, key)
    if _WARNED_FAIL_OPEN.get(dedupe) == reason:
        return
    _WARNED_FAIL_OPEN[dedupe] = reason
    _log.warning(
        "dispatch: tracker-MCP gate failing open for %s branch %s: %s; spawn proceeds",
        client_name,
        key,
        reason,
    )


def _log_assessment(
    client: ClientConfig,
    task: TicketTask,
    backend: str,
    branch: str,
    assessment: Assessment,
) -> None:
    """Emit the tick's log line for one assessment and update the dedupe."""
    if assessment.rearm is not None:
        _WARNED_FAIL_OPEN.pop((client.name, assessment.rearm), None)
    if assessment.warn is not None:
        _warn_fail_open_once(client.name, *assessment.warn)
    elif assessment.state is ReachState.NO_BRANCH:
        _log.debug(
            "dispatch: tracker-MCP gate skipped for %s — branch %s not created yet",
            client.name,
            branch,
        )
    elif assessment.state in (ReachState.NOT_NEEDED, ReachState.UNBOUND) and (
        assessment.hit is None
    ):
        _log.debug(
            "dispatch: tracker-MCP gate not applied to %s %s (backend %s)",
            client.name,
            task.ticket_id,
            backend,
        )


def _candidates(client_name: str, queue_snapshot: DevQueueStore) -> list[TicketTask]:
    """PENDING :data:`BINDING_STAGES` rows for *client_name*, in snapshot order."""
    return [
        task
        for task in queue_snapshot.tasks
        if task.client == client_name
        and task.status == QueueItemStatus.PENDING
        and task.stage in BINDING_STAGES
    ]


def resolve_binding_hits(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, BindingHit]:
    """Ticket id -> park for *client*'s PENDING rows that must not spawn.

    ``{}`` before any read unless the client opted in. A hit is positive
    evidence the binding is unreachable; every unresolvable step fails open.
    """
    if not client.tickets_preflight.enabled:
        return {}
    reader = ReachReader(client, client.workspace_path)
    hits: dict[str, BindingHit] = {}
    for task in _candidates(client.name, queue_snapshot):
        backend = resolve_executor_config(task.stage, task, client).backend
        branch = resolve_feature_branch(task.ticket_id, client, task)
        assessment = assess_binding(reader, backend, task.stage, branch)
        _log_assessment(client, task, backend, branch, assessment)
        if assessment.hit is not None:
            hits[task.ticket_id] = assessment.hit
    if hits:
        _log.info(
            "dispatch: tracker-MCP gate holds %s task(s) for %s (expected %s): %s",
            len(hits),
            client.name,
            sorted({hit.expected for hit in hits.values()}),
            sorted(hits),
        )
    return hits


def preflight_before_spawn(
    task: TicketTask, client: ClientConfig
) -> _SpawnOutcome | None:
    """Park a claimed codex row (any stage, not opt-in) on an unusable CLI.

    #1238. Reads the TTL-cached ``codex --version`` probe: capable clears the
    park streak; otherwise an environmental park (``count_streak=True``).
    """
    if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
        return None
    probe = _cached_codex_capability_diagnosis()
    if probe.diagnosis is None:
        reset_park_streak()
        return None
    _log.warning(
        "dispatch: codex capability gate parked %s/%s — %s",
        client.name,
        task.ticket_id,
        probe.detail,
    )
    return park_before_spawn(
        task,
        client,
        disposition=probe.diagnosis,
        breadcrumbs=probe.detail,
        count_streak=True,
    )
