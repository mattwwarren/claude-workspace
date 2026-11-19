"""``harness-capabilities`` doctor check (RFC 0019 H1 #3708, H2b #3744).

One result per (client, lane, backend): the stages the lane routes to that
backend, next to what the backend declares, plus whether the backend reaches
the tickets binding. It absorbs the retired ``tracker-mcp`` check (#3055) and
never probes the host beyond the in-process config reads the dispatch tick
makes, never resolves an executor (which raises on an unknown backend).

Severity (R8):

* **FAIL** when a routed stage is undeclared (``first_unmet`` returns
  ``stage_unsupported``; every stage of an unrecognized backend), or when the
  client opted in (``bindings.tickets.preflight.enabled``) and the shared
  :func:`cw.dispatch.claim.preflight_reach.assess_binding` yields a park for a
  routed stage that needs the binding (``binding_unreachable``,
  ``binding_unbound``, ``binding_tool_missing``): the tick would park the row.
* Otherwise ``ok=True, warn=False``. A skipped or unreadable read, and the
  claude-native ``enabledPlugins`` read that needs a ticket branch doctor does
  not have, render ``unknown``, never ``missing`` (#3306).

The binding read is :func:`cw.binding_probe.probe_binding` (#3309), rooted at
``repo_path or workspace_path``. Every reachable, unreachable and unknown
detail keeps the declared-only caveat (#3307).

Leaf module: imports only ``cw.doctor._shared`` from the doctor package (R18:
no cross-doctor imports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.binding_tools import ToolState
from cw.dispatch.claim.preflight_reach import (
    Assessment,
    ReachReader,
    ReachState,
    assess_binding,
)
from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.executor._capabilities import declared_capabilities
from cw.models import Stage, TicketTask
from cw.models.capabilities import HarnessCapabilities, in_enum_order
from cw.stage_preflight import PreflightReason, SpawnFacts, first_unmet

if TYPE_CHECKING:
    from collections.abc import Iterable
    from enum import StrEnum

    from cw.binding_tools import ToolCheck
    from cw.dispatch.claim.preflight_reach import BindingHit
    from cw.models import ClientConfig

_CAVEAT = "static declaration; auth, env and headless load unverified"
_UNKNOWN_BACKEND = "unrecognized backend"
_UNVERIFIED = "auth, env and headless load unverified"
_DECLARED_ONLY = "declaration-only check; " + _UNVERIFIED
_BRANCHLESS = (
    "claude-native reads enabledPlugins on the ticket branch, which doctor does"
    " not have"
)


def _lane_backends(client: ClientConfig, lane: str) -> dict[str, list[Stage]]:
    """Map each backend the lane routes to, to its stages in pipeline order."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(ticket_id="", client=client.name, lane=lane, stage=Stage.PLAN)
    grouped: dict[str, list[Stage]] = {}
    for stage in resolve_pipeline_stages(task, client):
        backend = resolve_executor_config(stage, task, client).backend
        grouped.setdefault(backend, []).append(stage)
    return grouped


def _names(members: Iterable[StrEnum]) -> str:
    """Render an enum-valued iterable as ``a, b`` in enum declaration order."""
    return ", ".join(m.value for m in in_enum_order(members))


def _sandbox(caps: HarnessCapabilities) -> str:
    if not caps.sandbox_modes:
        return "harness-managed"
    return _names(caps.sandbox_modes)


def _mismatch_note(routed: list[Stage], caps: HarnessCapabilities) -> str:
    """Plain-text note for routed stages the backend does not declare."""
    undeclared = [s for s in routed if s not in caps.stages]
    if not undeclared:
        return ""
    names = ", ".join(s.value for s in undeclared)
    declared = _names(caps.stages) or "none"
    return (
        f"; routes {names} to {caps.harness.value}, which declares {declared};"
        " the stage pre-flight parks such a spawn as stage_unsupported on"
        " local, opencode and codex (#3219)"
    )


def _undeclared(routed: list[Stage], caps: HarnessCapabilities) -> bool:
    """True when :func:`first_unmet` parks some routed stage as undeclared."""
    for stage in routed:
        facts = SpawnFacts(
            stage=stage,
            clean_review=False,
            engine_refusal=None,
            feedback=False,
            adopted=False,
        )
        unmet = first_unmet(caps, facts)
        if unmet is not None and unmet.reason is PreflightReason.STAGE_UNSUPPORTED:
            return True
    return False


def _tool_line(tools: ToolCheck | None) -> str:
    if tools is None or tools.state is ToolState.NOT_DECLARED:
        return ""
    if tools.state is ToolState.UNKNOWN:
        return f"; tools: unknown ({tools.reason})"
    where = f"enabled_tools in {tools.source}"
    if tools.missing:
        return f"; tools: missing {', '.join(tools.missing)} from {where}"
    return f"; tools: checked against {where}"


#: The tickets-binding clause of every state whose text carries no read.
_FIXED_REACH_TEXT: dict[ReachState, str] = {
    ReachState.NOT_NEEDED: "not needed (no routed stage needs the tickets binding)",
    ReachState.GITHUB: "not needed (GitHub tracker, reached through gh)",
    ReachState.TRACKER_UNSET: "unknown (MCP declaration not read: tracker is unset)",
    ReachState.NO_BRANCH: f"unknown ({_BRANCHLESS}; {_DECLARED_ONLY})",
}


def _reach_text(assessment: Assessment) -> str:
    """The tickets-binding clause for one backend's assessment."""
    state = assessment.state
    fixed = _FIXED_REACH_TEXT.get(state)
    if fixed is not None:
        return fixed
    if state is ReachState.UNBOUND:
        return f"unbound ({assessment.detail})"
    if state is ReachState.UNKNOWN:
        return f"unknown ({assessment.detail}; {_DECLARED_ONLY})"
    if state is ReachState.UNREACHABLE:
        return (
            f"unreachable (absent or disabled in {assessment.source}:"
            f" {assessment.detail}; {_DECLARED_ONLY})"
        )
    text = f"reachable (declared in {assessment.source}; {_UNVERIFIED})"
    return text + _tool_line(assessment.tools)


def _assess_lane(
    reader: ReachReader, backend: str, routed: list[Stage]
) -> tuple[Assessment, BindingHit | None]:
    """The backend's assessment over its routed stages, and its park, if any.

    A park on any routed stage is reported; otherwise the first routed stage
    that needs the binding, else the first routed stage.
    """
    assessments = [assess_binding(reader, backend, s, None) for s in routed]
    for assessment in assessments:
        if assessment.hit is not None:
            return assessment, assessment.hit
    needed = [a for a in assessments if a.state is not ReachState.NOT_NEEDED]
    return (needed or assessments)[0], None


def _detail(backend: str, routed: list[Stage], caps: HarnessCapabilities | None) -> str:
    if caps is None:
        return f"{_UNKNOWN_BACKEND} {backend!r}: no declaration; {_CAVEAT}"
    routed_text = ", ".join(s.value for s in in_enum_order(routed))
    parts = [
        f"backend {backend}",
        f"routes {routed_text}",
        f"declares stages {_names(caps.stages) or 'none'}",
        f"skills {'yes' if caps.skills else 'no'}",
        f"subagents {'yes' if caps.subagents else 'no'}",
        f"clean-review {'yes' if caps.clean_mode_review else 'no'}",
        f"engine {'yes' if caps.review_engine else 'no'}",
        f"repair-entry {caps.repair_entry.value}",
        f"sandbox {_sandbox(caps)}",
        f"narrowing {caps.tool_narrowing.value}",
        f"usage {caps.usage_source.value}",
        f"probe {caps.binding_probe.value}",
    ]
    return "; ".join(parts) + _mismatch_note(routed, caps) + f"; {_CAVEAT}"


def _lane_result(
    client: ClientConfig,
    lane: str,
    backend: str,
    routed: list[Stage],
    reader: ReachReader,
) -> CheckResult:
    name = f"harness-capabilities/{client.name}/{lane}/{backend}"
    caps = declared_capabilities(backend)
    detail = _detail(backend, routed, caps)
    if caps is None:
        return CheckResult(name=name, ok=False, detail=detail, warn=False)
    assessment, hit = _assess_lane(reader, backend, routed)
    detail += f"; tickets binding {_reach_text(assessment)}"
    parks = hit is not None and client.tickets_preflight.enabled
    if hit is not None and parks:
        detail += f"; the dispatch tick parks such a row as {hit.reason.value}"
    ok = not (_undeclared(routed, caps) or parks)
    return CheckResult(name=name, ok=ok, detail=detail, warn=False)


def _check_harness_capabilities(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Capabilities and tickets reach per (client, lane, backend). Never warns."""
    results: list[CheckResult] = []
    for client in clients.values():
        reader = ReachReader(client, client.repo_path or client.workspace_path)
        for lane in client.effective_lanes:
            results.extend(
                _lane_result(client, lane.name, backend, routed, reader)
                for backend, routed in _lane_backends(client, lane.name).items()
            )
    return results
