"""Per-lane tracker-MCP reachability check for non-Claude backends (#3055).

RFC 0018 S3. One ``CheckResult`` named ``tracker-mcp/<client>/<lane>/<backend>``
per lane whose pipeline runs a tracker-touching stage on a non-Claude backend
(opencode PLAN/IMPL/FINALIZE; codex PLAN/REVIEW/FINALIZE) for a client whose
tracker is not GitHub. A GitHub tracker needs no MCP and produces no result;
``local`` and ``claude-native`` are not reported here.

Severity:

* codex: a warning naming what its reviews lose on this tracker (free-text
  operator comments, the voided-findings block, marker comments) and that a
  codex PLAN turn reads the ticket only through ``servers.codex`` (#3060).
  When the lane runs PLAN or FINALIZE on codex (the FINALIZE turn posts the PR
  link through the tracker MCP), the gate is on and the server reads
  ``ABSENT``/``DISABLED``, it **fails** like opencode (the row would park).
* opencode, tracker unset: a warning -- the PLAN/IMPL prompt tells the agent
  to block with ``tracker_tool_unavailable``.
* opencode, no ``tracker_mcp_gate.servers.opencode`` named: a warning.
* opencode, server named: the user-layer config is read in process with
  :func:`cw.backend_mcp_config.read_backend_mcp`. ``ENABLED`` is ok (with the
  "auth, env and headless load unverified" caveat); ``UNKNOWN`` warns;
  ``ABSENT``/``DISABLED`` **fails** when the gate is on and the lane runs
  PLAN or IMPL on opencode (the row would park), and warns otherwise. The
  non-ENABLED readings carry a "declaration-only check" caveat. A named server
  whose read was skipped (tracker unset) reports "MCP declaration not read"
  with no caveat, because nothing was read.

Root is ``client.repo_path or client.workspace_path``. Leaf module, no
cross-``doctor`` dependencies; read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.backend_mcp_config import McpConfigReading, McpState, read_backend_mcp
from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.models import CODEX_BACKEND, OPENCODE_BACKEND, Stage, TicketTask
from cw.tracker import TrackerClass, TrackerKind, tracker_kind

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_CHECK_NAME = "tracker-mcp"

# The stages each non-Claude backend performs tracker operations in. opencode
# REVIEW performs none (its prompt says so); codex reads at PLAN (#3060) and
# REVIEW and posts the PR link at FINALIZE.
_TRACKER_STAGES: dict[str, frozenset[Stage]] = {
    OPENCODE_BACKEND: frozenset({Stage.PLAN, Stage.IMPL, Stage.FINALIZE}),
    CODEX_BACKEND: frozenset({Stage.PLAN, Stage.REVIEW, Stage.FINALIZE}),
}
# The stages the dispatch gate parks on (``cw.dispatch.tracker_mcp_gate``).
_GATED_STAGES = frozenset({Stage.PLAN, Stage.IMPL})
# The codex stages the dispatch gate parks on: a codex PLAN turn reads the
# ticket and a codex FINALIZE turn posts the PR link, both through the MCP.
_CODEX_GATED_STAGES = frozenset({Stage.PLAN, Stage.FINALIZE})

_UNVERIFIED = "auth, env and headless load unverified"
_CAVEAT = "declared in {path}; " + _UNVERIFIED
# Non-ENABLED readings only prove what the file says, not what loads.
_DECLARED_ONLY = "declaration-only check; " + _UNVERIFIED
_NOT_READ = "MCP declaration not read: tracker is unset"
_LOST_CONTEXT = (
    "codex reviews on this tracker run without the operator's free-text ticket"
    " comments, the voided-findings block (#1814) and marker comments, because"
    " the daemon cannot read this tracker. The verdict comment posts through"
    " the tracker-IO turn (#3057) only when tracker_mcp_gate.servers.codex"
    " names the tracker's MCP server and it is enabled; otherwise it is"
    " skipped (review.verdict_comment_skipped). Row-carried constraints and"
    " finding dispositions still reach the review. A codex PLAN turn (#3060)"
    " reads the ticket only through the MCP server tracker_mcp_gate.servers.codex"
    " names, and blocks tracker_mcp_unavailable_pre_dispatch without it. A codex"
    " FINALIZE turn posts the PR link through the same server and is gated the"
    " same way."
)
_UNSET_PROMPT = (
    "the opencode PLAN/IMPL prompt tells the agent to block with"
    " tracker_tool_unavailable; set tracking.primary.system in"
    " .claude/project-config.yaml"
)


class _Lane(NamedTuple):
    """One reportable (client, lane, backend) and its tracker-touching stages."""

    client: ClientConfig
    lane: str
    backend: str
    stages: tuple[Stage, ...]


def _lane_backends(client: ClientConfig, lane: str) -> dict[str, tuple[Stage, ...]]:
    """Map each non-Claude backend to the tracker-touching stages it runs."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=client.name, lane=lane, stage=Stage.PLAN)
    grouped: dict[str, list[Stage]] = {}
    for stage in resolve_pipeline_stages(task, client):
        backend = resolve_executor_config(stage, task, client).backend
        if stage in _TRACKER_STAGES.get(backend, frozenset()):
            grouped.setdefault(backend, []).append(stage)
    return {backend: tuple(stages) for backend, stages in grouped.items()}


def _mcp_clause(server: str | None, reading: McpConfigReading | None) -> str:
    """Describe what the backend config says about the server."""
    if server is None:
        return "no tracker_mcp_gate.servers entry names this backend's MCP server"
    if reading is None:
        # The read is skipped only when the tracker is unset (see _lane_result).
        return _NOT_READ
    if reading.state is McpState.ENABLED:
        return _CAVEAT.format(path=reading.path)
    if reading.state is McpState.UNKNOWN:
        return f"MCP declaration unverified: {reading.reason}; {_DECLARED_ONLY}"
    return (
        f"server {server} is {reading.state.value} in {reading.path}; {_DECLARED_ONLY}"
    )


def _lost_context_clause(backend: str, kind: TrackerKind) -> str:
    if backend == CODEX_BACKEND:
        return _LOST_CONTEXT
    if kind.tracker_class is TrackerClass.UNSET:
        return _UNSET_PROMPT
    return ""


def _severity(
    entry: _Lane, kind: TrackerKind, reading: McpConfigReading | None
) -> tuple[bool, bool]:
    """``(ok, warn)`` for one lane; see the module docstring."""
    if kind.tracker_class is TrackerClass.UNSET:
        return True, True
    gated = _CODEX_GATED_STAGES if entry.backend == CODEX_BACKEND else _GATED_STAGES
    if entry.backend == CODEX_BACKEND and not gated.intersection(entry.stages):
        return True, True
    if reading is None or reading.state is McpState.UNKNOWN:
        return True, True
    if reading.state is McpState.ENABLED:
        return True, entry.backend == CODEX_BACKEND
    gate = entry.client.tracker_mcp_gate
    gate_on = gate is not None and gate.enabled
    if gate_on and gated.intersection(entry.stages):
        return False, False
    return True, True


def _lane_result(entry: _Lane, root: Path, kind: TrackerKind) -> CheckResult:
    server = entry.client.tracker_mcp_server(entry.backend)
    reading = (
        read_backend_mcp(entry.backend, server, root)
        if server is not None and kind.tracker_class is TrackerClass.OTHER
        else None
    )
    ok, warn = _severity(entry, kind, reading)
    stages = ",".join(stage.value for stage in entry.stages)
    clauses = [
        f"lane {entry.lane}, stage {stages}, backend {entry.backend},"
        f" tracker {kind.system or 'unset'}, server {server or 'none named'}",
        _mcp_clause(server, reading),
        _lost_context_clause(entry.backend, kind),
    ]
    return CheckResult(
        f"{_CHECK_NAME}/{entry.client.name}/{entry.lane}/{entry.backend}",
        ok=ok,
        warn=warn,
        detail="; ".join(clause for clause in clauses if clause),
    )


def _client_results(client: ClientConfig) -> list[CheckResult]:
    root = client.repo_path or client.workspace_path
    kind = tracker_kind(root)
    if kind.tracker_class is TrackerClass.GITHUB:
        return []
    return [
        _lane_result(_Lane(client, lane.name, backend, stages), root, kind)
        for lane in client.effective_lanes
        for backend, stages in _lane_backends(client, lane.name).items()
    ]


def _check_tracker_mcp(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Report tracker-MCP reachability for every non-Claude tracker lane."""
    return [result for client in clients.values() for result in _client_results(client)]
