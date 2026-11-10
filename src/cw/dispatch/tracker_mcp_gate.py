"""Pre-dispatch tracker-MCP gate for PLAN/IMPL-stage PENDING tasks (#2442).

The incident this closes (#2415): a worker was spawned onto a ticket branch
whose ``.claude/settings.json`` did not enable the tracker's MCP plugin. The
session could not read the tracker's comments, and only discovered that
mid-run -- after a slot, a worktree, and a session had already been spent.

This module answers the one question the claim path needs before that happens:
**does this client's PENDING PLAN/IMPL-stage ticket branch verifiably lack the
client's configured tracker MCP plugin?** The claim path
(``cw.dispatch.claim``) turns a "yes" into a ``BLOCKED_ON_USER`` park rather
than a claim.

Contracts:

* **Per-client opt-in, default off.** The gate runs only for a client whose
  ``ClientConfig.tracker_mcp_gate`` is set with ``enabled: true``; otherwise
  :func:`resolve_tracker_mcp_gate_hits` returns before any git call. There is
  no fleet-wide toggle -- per-client opt-in is the rollout control.
* **Explicit plugin id, exact match.** ``TrackerMcpGateConfig.plugin_id`` is
  the exact ``enabledPlugins`` key the operator's tracker plugin registers
  under. No tracker->plugin mapping, no ``@``-suffix stripping, no case
  folding.
* **Fail open on anything ambiguous.** No branch yet (a first-time PLAN
  dispatch), no settings file on an existing branch, malformed JSON, a
  non-object top level, no ``enabledPlugins`` key, an unrecognized
  ``enabledPlugins`` shape, or git itself unavailable -- all resolve to "not
  gated", and the spawn proceeds. Only a parseable file whose
  ``enabledPlugins`` (an object of ``"<plugin>@<marketplace>": bool``, or a
  list of plugin ids) names ``plugin_id`` as absent or ``false`` is a hit. A
  false positive parks a healthy ticket and costs an operator; a false
  negative costs at most the mid-run discovery that is today's status quo --
  the same posture ``pr_gate.py`` and ``branch_freshness.py`` document.
* **Local git only, no network, no cache.** The settings file is read with
  ``git show <branch>:<settings_path>`` against the client's own git dir --
  never a worktree checkout, which does not exist yet for a PENDING
  PLAN/IMPL row (``create_worktree`` runs after the claim). A local blob read
  is cheap, so unlike ``pr_gate.py`` there is no TTL cache. It still runs
  outside ``dev_queue_lock()``, once per client per tick, from
  ``cw.dispatch.lanes``.

* **Backend-aware (#3055, RFC 0018 S3).** Each candidate's backend resolves
  per task through ``resolve_executor_config``. The branch/plugin path above
  applies to **claude-native** rows only, and only when ``plugin_id`` is set.
  An **opencode** row is checked against ``servers["opencode"]`` instead: on
  a non-GitHub tracker, ``cw.backend_mcp_config.read_backend_mcp`` reads the
  user-layer ``opencode.json`` in process, and the row parks when the server
  is verifiably absent or disabled. A GitHub tracker needs no MCP and is
  never read; an unset tracker fails open with one warning. Codex is not
  gated in S3 (``cw doctor`` reports it). One tracker resolution and one
  config read per backend per tick.

Non-goal: probing whether the tracker's credential (e.g. an API-key env var)
is present. That is a runtime property of the spawned session's environment,
not of the branch, and is deliberately out of scope here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.backend_mcp_config import McpConfigReading, McpState, read_backend_mcp
from cw.executor import resolve_executor_config
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    OPENCODE_BACKEND,
    QueueItemStatus,
    Stage,
    resolve_feature_branch,
)
from cw.tracker import TrackerClass, TrackerKind, tracker_kind
from cw.worktree import _git_dir, _ref_exists, _run_git

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore, TicketTask
    from cw.models.client import TrackerMcpGateConfig

_log = logging.getLogger("cw.dispatch")

# The ``.claude/settings.json`` key naming which Claude Code plugins a session
# rooted in that checkout loads.
_ENABLED_PLUGINS_KEY = "enabledPlugins"

# The stages whose sessions depend on the tracker MCP before producing any
# artifact (PLAN posts the plan to the tracker; IMPL orientation reads the
# tracker's comments -- the #2415 failure). Mirrors pr_gate's PLAN/IMPL scope;
# re-checked at the point of use in cw.dispatch.claim.screening.
_GATED_STAGES: frozenset[Stage] = frozenset({Stage.PLAN, Stage.IMPL})

# Warn-once dedupe for fail-open outcomes: (client, branch) -> the reason last
# warned. The dispatch loop re-resolves every tick, so without this a single
# unreadable settings file would log a warning every 30 seconds. A branch that
# later resolves cleanly (a verdict either way) is dropped from the map, so a
# regression back to a fail-open state warns again. Process-lifetime state;
# tests reset it.
_WARNED_FAIL_OPEN: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class TrackerMcpGateHit:
    """What the orchestrator must fix for one gated ticket (#2442).

    Threaded from the resolver into the park's SESSION_NEEDS_ATTENTION event
    as additive payload fields. ``expected`` is the plugin id for a
    claude-native hit and the MCP server name for a non-Claude hit (#3055);
    ``file_inspected`` is the branch settings path or the backend config path.
    """

    branch: str
    file_inspected: str
    expected: str
    backend: str = CLAUDE_NATIVE_BACKEND


@dataclass
class _GateTick:
    """Per-call memo: one tracker resolution and one config read per backend."""

    client: ClientConfig
    _tracker: TrackerKind | None = None
    readings: dict[str, McpConfigReading] = field(default_factory=dict)

    def tracker(self) -> TrackerKind:
        if self._tracker is None:
            self._tracker = tracker_kind(self.client.workspace_path)
        return self._tracker

    def reading(self, backend: str, server: str) -> McpConfigReading:
        if backend not in self.readings:
            self.readings[backend] = read_backend_mcp(
                backend, server, self.client.workspace_path
            )
        return self.readings[backend]


def _warn_fail_open_once(client_name: str, branch: str, reason: str) -> None:
    """Log one fail-open *reason* for *branch*, suppressing per-tick repeats."""
    key = (client_name, branch)
    if _WARNED_FAIL_OPEN.get(key) == reason:
        return
    _WARNED_FAIL_OPEN[key] = reason
    _log.warning(
        "dispatch: tracker-MCP gate failing open for %s branch %s: %s; spawn proceeds",
        client_name,
        branch,
        reason,
    )


def _read_branch_json(
    client: ClientConfig, branch: str, relpath: str
) -> dict[str, object] | None:
    """Return *relpath* on *branch* parsed as a JSON object, or ``None``.

    ``None`` always means "fail open": the branch does not exist yet (logged at
    debug -- the normal first-dispatch case), git is unavailable, the file is
    absent on an existing branch, or it is not a JSON object. Every case but
    the first is warned once via :func:`_warn_fail_open_once`.
    """
    git_cwd = _git_dir(client)
    try:
        if not _ref_exists(branch, git_cwd):
            _log.debug(
                "dispatch: tracker-MCP gate skipped for %s — branch %s not created yet",
                client.name,
                branch,
            )
            return None
        result = _run_git("show", f"{branch}:{relpath}", cwd=git_cwd, check=False)
    except OSError as exc:
        _warn_fail_open_once(client.name, branch, f"git unavailable ({exc})")
        return None
    if result.returncode != 0:
        _warn_fail_open_once(client.name, branch, f"{relpath} missing on branch")
        return None
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        _warn_fail_open_once(client.name, branch, f"{relpath} is malformed JSON")
        return None
    if not isinstance(parsed, dict):
        _warn_fail_open_once(
            client.name, branch, f"{relpath} top level is not a JSON object"
        )
        return None
    return parsed


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


def _evaluate_branch(
    client: ClientConfig, gate: TrackerMcpGateConfig, branch: str
) -> TrackerMcpGateHit | None:
    """Return a hit iff *branch*'s settings file verifiably lacks the plugin."""
    settings = _read_branch_json(client, branch, gate.settings_path)
    if settings is None:
        return None
    if _ENABLED_PLUGINS_KEY not in settings:
        _warn_fail_open_once(
            client.name,
            branch,
            f"no {_ENABLED_PLUGINS_KEY} key in {gate.settings_path}",
        )
        return None
    plugin_id = gate.plugin_id
    if plugin_id is None:
        return None
    verdict = _plugin_enabled(settings[_ENABLED_PLUGINS_KEY], plugin_id)
    if verdict is None:
        _warn_fail_open_once(
            client.name,
            branch,
            f"unrecognized {_ENABLED_PLUGINS_KEY} shape in {gate.settings_path}",
        )
        return None
    _WARNED_FAIL_OPEN.pop((client.name, branch), None)
    if verdict:
        return None
    return TrackerMcpGateHit(
        branch=branch,
        file_inspected=gate.settings_path,
        expected=plugin_id,
    )


def _evaluate_backend_server(
    client: ClientConfig, backend: str, server: str, branch: str, tick: _GateTick
) -> TrackerMcpGateHit | None:
    """Return a hit iff *backend*'s config verifiably lacks MCP *server* (#3055).

    GITHUB needs no tracker MCP and is never read; UNSET fails open with one
    warning and no read. On any other tracker one read per backend per tick:
    UNKNOWN fails open with one warning, ENABLED re-arms the warning.
    """
    kind = tick.tracker()
    if kind.tracker_class is TrackerClass.GITHUB:
        return None
    if kind.tracker_class is TrackerClass.UNSET:
        _warn_fail_open_once(
            client.name,
            f"{backend}:tracker",
            "tracker unset in .claude/project-config.yaml",
        )
        return None
    reading = tick.reading(backend, server)
    label = f"{backend}:{reading.path}"
    if reading.state is McpState.UNKNOWN:
        _warn_fail_open_once(client.name, label, reading.reason)
        return None
    _WARNED_FAIL_OPEN.pop((client.name, label), None)
    if reading.state is McpState.ENABLED:
        return None
    return TrackerMcpGateHit(
        branch=branch,
        file_inspected=str(reading.path),
        expected=server,
        backend=backend,
    )


def _evaluate_task(
    client: ClientConfig, gate: TrackerMcpGateConfig, task: TicketTask, tick: _GateTick
) -> TrackerMcpGateHit | None:
    """Route one candidate by its resolved backend (A12).

    claude-native: the branch/plugin path, only when ``plugin_id`` is set.
    opencode: the server path, only when ``servers["opencode"]`` is set.
    Codex, ``local`` and anything else are not gated here.
    """
    backend = resolve_executor_config(task.stage, task, client).backend
    branch = resolve_feature_branch(task.ticket_id, client, task)
    if backend == CLAUDE_NATIVE_BACKEND:
        if gate.plugin_id is None:
            return None
        return _evaluate_branch(client, gate, branch)
    server = (gate.servers or {}).get(backend)
    if backend != OPENCODE_BACKEND or server is None:
        _log.debug(
            "dispatch: tracker-MCP gate not applied to %s %s (backend %s)",
            client.name,
            task.ticket_id,
            backend,
        )
        return None
    return _evaluate_backend_server(client, backend, server, branch, tick)


def _gated_candidates(
    client_name: str, queue_snapshot: DevQueueStore
) -> list[TicketTask]:
    """PENDING PLAN/IMPL-stage tasks for *client_name*, in snapshot order."""
    return [
        task
        for task in queue_snapshot.tasks
        if task.client == client_name
        and task.status == QueueItemStatus.PENDING
        and task.stage in _GATED_STAGES
    ]


def resolve_tracker_mcp_gate_hits(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, TrackerMcpGateHit]:
    """Ticket id -> gate hit for *client*'s tickets that must not spawn (#2442).

    Returns ``{}`` immediately -- before any git call -- when the client has
    not opted in (``tracker_mcp_gate`` unset or ``enabled: false``). Otherwise
    scans *queue_snapshot* for the client's PENDING PLAN/IMPL-stage tasks and
    checks each by its resolved backend (see :func:`_evaluate_task`).

    Callers must treat the result as "positive evidence the plugin is
    missing", never its complement as a guarantee the plugin is enabled --
    this function fails open at every unresolvable step.
    """
    gate = client.tracker_mcp_gate
    if gate is None or not gate.enabled:
        return {}
    hits: dict[str, TrackerMcpGateHit] = {}
    tick = _GateTick(client)
    for task in _gated_candidates(client.name, queue_snapshot):
        hit = _evaluate_task(client, gate, task, tick)
        if hit is not None:
            hits[task.ticket_id] = hit
    if hits:
        _log.info(
            "dispatch: tracker-MCP gate holds %s task(s) for %s (expected %s): %s",
            len(hits),
            client.name,
            sorted({hit.expected for hit in hits.values()}),
            sorted(hits),
        )
    return hits
