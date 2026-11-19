"""Tick-side binding reachability preflight (RFC 0019 H2b, #3744).

Replaces the pre-dispatch tracker-MCP gate (#2442, #3055, #3060). Before a
PENDING row is claimed, it answers one question: **does this row's harness
verifiably fail to reach the client's tickets binding for a stage that needs
it?** A "yes" becomes a ``BLOCKED_ON_USER`` park in
:mod:`cw.dispatch.claim.screening`.

Three parts, one owner:

* :func:`assess_binding` is the pure-policy core shared by the tick and
  ``cw doctor``. Which stages need the binding comes from ``STAGE_NEEDS``
  (``Role.TICKETS in needs_for(harness, stage).bindings``); the read is
  :func:`cw.binding_probe.probe_binding`, the single lookup (#3309); the tool
  check is :func:`cw.binding_tools.check_tools`. It decides; it never logs.
* :func:`resolve_binding_hits` is the tick: per-client opt-in
  (``bindings.tickets.preflight.enabled``), returns ``{}`` before any git or
  config read otherwise (D-C3), and owns the warn-once state and every log line.
* :func:`preflight_before_spawn` is the post-claim, pre-worktree codex CLI
  probe park (#1238), through :func:`~cw.dispatch.claim.outcome.park_before_spawn`.

Policy the probe does not encode lives here, the one allowed ``TrackerClass``
site until X1 (#3713): a GitHub tracker needs no MCP and is never read; an
unset tracker fails open with one warning; codex PLAN and FINALIZE on any other
tracker with an unset or invalid ``servers.codex`` is ``binding_unbound`` before
any read (the only fail-closed case). Every other unknown fails open: a missing
branch at debug, anything else with one warning that a clean verdict re-arms.

Every logger is ``cw.dispatch``, as in the modules this replaces.
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
from cw.tracker import TrackerClass, TrackerKind, tracker_kind
from cw.tracker_io._argv import is_valid_name

if TYPE_CHECKING:
    from pathlib import Path

    from cw.dispatch.claim.outcome import _SpawnOutcome
    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")

#: Every stage some harness's ``STAGE_NEEDS`` gives the tickets binding:
#: {PLAN, IMPL, FINALIZE}. The tick scans only these rows; the claim screen
#: re-checks a hit against it under the lock.
BINDING_STAGES: frozenset[Stage] = frozenset(
    stage for (_, stage), needs in STAGE_NEEDS.items() if Role.TICKETS in needs.bindings
)

#: What a codex hit names when ``servers.codex`` itself is unusable. Names the
#: deprecated key, byte-identical to the old gate's hit for parity.
_CODEX_SERVERS_FILE = "clients.yaml tracker_mcp_gate.servers"
_CODEX_SERVER_UNSET = "servers.codex (unset)"
_TRACKER_UNSET = "tracker unset in .claude/project-config.yaml"
_UNBOUND_PLUGIN = (
    "no bindings.tickets.mcp.claude-native.plugin_id names this harness's"
    " tracker plugin"
)


def _unbound_server(backend: str) -> str:
    return f"no bindings.tickets.mcp.{backend}.server names this harness's MCP server"


# Warn-once dedupe for fail-open outcomes: (client, key) -> the reason last
# warned. The key is the branch (claude-native), "<backend>:<config path>" or
# "<backend>:tracker". The dispatch loop re-resolves every tick, so without
# this one unreadable file would warn every 30 seconds. A clean verdict (a hit
# or a reachable read) pops the key, so a regression warns again.
# Process-lifetime state; tests reset it.
_WARNED_FAIL_OPEN: dict[tuple[str, str], str] = {}


class ReachState(StrEnum):
    """Where :func:`assess_binding` ended for one (harness, stage)."""

    #: The stage does not need the tickets binding on this harness.
    NOT_NEEDED = "not_needed"
    #: A GitHub tracker: reached through ``gh``, never read.
    GITHUB = "github"
    #: No resolvable tracker: fail open with a warning.
    TRACKER_UNSET = "tracker_unset"
    #: Nothing names this harness's MCP server or plugin (a park only for codex
    #: PLAN/FINALIZE, see :attr:`Assessment.hit`).
    UNBOUND = "unbound"
    #: The claude-native read had no branch: debug only.
    NO_BRANCH = "no_branch"
    #: A read was attempted and could not resolve: fail open with a warning.
    UNKNOWN = "unknown"
    #: The read verified the binding is reachable.
    REACHABLE = "reachable"
    #: The read verified the binding is absent or disabled.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class BindingHit:
    """What the orchestrator must fix for one parked ticket.

    Supersedes the tracker-MCP gate's hit: the same four values byte for byte
    (``expected`` is the plugin id or the MCP server name), plus the park
    ``reason``, whether ``expected`` is a plugin or a server, and the declared
    tool names a known tool list excludes.
    """

    branch: str
    file_inspected: str
    expected: str
    backend: str = CLAUDE_NATIVE_BACKEND
    reason: PreflightReason = PreflightReason.BINDING_UNREACHABLE
    expected_kind: Literal["plugin", "server"] = "plugin"
    missing_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    """One assessment: its state, an optional park, and the warn-once keys.

    ``warn`` is ``(key, reason)`` for a fail-open warning; ``rearm`` is a key a
    clean verdict clears. ``detail`` is the probe or policy reason, ``source``
    the file inspected, and ``tools`` the tool check of a reachable server.
    """

    state: ReachState
    hit: BindingHit | None = None
    warn: tuple[str, str] | None = None
    rearm: str | None = None
    detail: str = ""
    source: str | None = None
    tools: ToolCheck | None = None


@dataclass
class ReachReader:
    """Per-call memo: one tracker resolution and one read per backend.

    *root* is both the tracker root and the probe root: ``workspace_path`` on
    the tick, ``repo_path or workspace_path`` in doctor.
    """

    client: ClientConfig
    root: Path
    _tracker: TrackerKind | None = None
    _probes: dict[str, BindingProbe] = field(default_factory=dict)
    _tools: dict[str, ToolCheck] = field(default_factory=dict)

    def tracker(self) -> TrackerKind:
        if self._tracker is None:
            self._tracker = tracker_kind(self.root)
        return self._tracker

    def server_probe(self, backend: str) -> BindingProbe:
        if backend not in self._probes:
            self._probes[backend] = probe_binding(backend, self.client, root=self.root)
        return self._probes[backend]

    def tool_check(self, backend: str, server: str) -> ToolCheck:
        if backend not in self._tools:
            self._tools[backend] = check_tools(self.client, backend, server, self.root)
        return self._tools[backend]


def _needs_tickets(backend: str, stage: Stage) -> bool:
    try:
        harness = Harness(backend)
    except ValueError:
        return False
    return Role.TICKETS in needs_for(harness, stage).bindings


def _assess_plugin(reader: ReachReader, branch: str | None) -> Assessment:
    """claude-native: the branch ``enabledPlugins`` read; tracker not consulted."""
    plugin_id = reader.client.tickets_plugin_id
    if plugin_id is None:
        return Assessment(ReachState.UNBOUND, detail=_UNBOUND_PLUGIN)
    if branch is None:
        return Assessment(ReachState.NO_BRANCH, detail="no branch to read")
    probe = probe_binding(CLAUDE_NATIVE_BACKEND, reader.client, branch=branch)
    if probe.cause is ProbeCause.NO_BRANCH:
        return Assessment(ReachState.NO_BRANCH, detail=probe.reason)
    if probe.cause is not ProbeCause.READ:
        return Assessment(
            ReachState.UNKNOWN,
            warn=(branch, probe.reason),
            detail=probe.reason,
            source=probe.source,
        )
    if probe.state is Reach.ENABLED:
        return Assessment(ReachState.REACHABLE, rearm=branch, source=probe.source)
    hit = BindingHit(
        branch=branch,
        file_inspected=reader.client.tickets_preflight.settings_path,
        expected=plugin_id,
    )
    return Assessment(
        ReachState.UNREACHABLE, hit=hit, rearm=branch, source=probe.source
    )


def _tracker_gate(reader: ReachReader, backend: str) -> Assessment | None:
    """GitHub needs no MCP; an unset tracker fails open. ``None``: read on."""
    tracker_class = reader.tracker().tracker_class
    if tracker_class is TrackerClass.GITHUB:
        return Assessment(ReachState.GITHUB, detail="GitHub tracker, reached via gh")
    if tracker_class is TrackerClass.UNSET:
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
    if tools.state is not ToolState.MISSING:
        return Assessment(
            ReachState.REACHABLE, rearm=label, source=probe.source, tools=tools
        )
    hit = BindingHit(
        branch=branch,
        file_inspected=str(probe.source),
        expected=server,
        backend=backend,
        reason=PreflightReason.BINDING_TOOL_MISSING,
        expected_kind="server",
        missing_tools=tools.missing,
    )
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
    if probe.state is Reach.UNKNOWN:
        return Assessment(
            ReachState.UNKNOWN,
            warn=(label, probe.reason),
            detail=probe.reason,
            source=probe.source,
        )
    if probe.state is Reach.ENABLED:
        return _reachable_server(reader, backend, server, branch, probe)
    hit = BindingHit(
        branch=branch,
        file_inspected=str(probe.source),
        expected=server,
        backend=backend,
        expected_kind="server",
    )
    return Assessment(
        ReachState.UNREACHABLE,
        hit=hit,
        rearm=label,
        detail=probe.reason,
        source=probe.source,
    )


def _assess_codex_unbound(
    reader: ReachReader, server: str | None, branch: str
) -> Assessment:
    """codex PLAN/FINALIZE with no usable ``servers.codex`` (R4)."""
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
    detail = _unbound_server(CODEX_BACKEND)
    if server is not None:
        detail += f" (servers.codex {server!r} is not a valid name)"
    return Assessment(ReachState.UNBOUND, hit=hit, detail=detail)


def assess_binding(
    reader: ReachReader, backend: str, stage: Stage, branch: str | None
) -> Assessment:
    """Assess whether *backend* reaches the tickets binding for *stage*.

    Pure policy over the reads :class:`ReachReader` memoizes; no logging.
    *branch* ``None`` (doctor) makes the claude-native read ``NO_BRANCH``.
    """
    if not _needs_tickets(backend, stage):
        return Assessment(ReachState.NOT_NEEDED)
    if backend == CLAUDE_NATIVE_BACKEND:
        return _assess_plugin(reader, branch)
    server = reader.client.tracker_mcp_server(backend)
    row_branch = branch or ""
    if backend == CODEX_BACKEND and (server is None or not is_valid_name(server)):
        return _assess_codex_unbound(reader, server, row_branch)
    if server is None:
        return Assessment(ReachState.UNBOUND, detail=_unbound_server(backend))
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

    Returns ``{}`` immediately, before any git or config read, when the client
    has not opted in (no ``bindings.tickets``, or ``preflight.enabled`` false).
    The result is positive evidence the binding is unreachable, never its
    complement a guarantee it is reachable: every unresolvable step fails open.
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
    """Post-claim, pre-worktree park of a codex row on an unusable CLI (#1238).

    Runs for any stage that resolves to codex, not opt-in. Reads only the
    TTL-cached ``codex --version`` probe. A capable probe clears the park
    streak and returns ``None``; otherwise the claimed RUNNING row parks
    BLOCKED_ON_USER with the probe's diagnosis as its disposition through
    :func:`park_before_spawn` with ``count_streak=True`` (an environmental
    park), so consecutive parks engage the lane breaker.
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
