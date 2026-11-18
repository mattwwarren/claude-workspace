"""Declared-only ``harness-capabilities`` doctor check (RFC 0019 H1, #3708).

One result per (client, lane, backend): the stages the lane routes to that
backend, next to what the backend declares. Declared, not enforced. The check
never probes the host, never resolves an executor (which raises on an unknown
backend), and is always ``ok=True, warn=False``. A stage routed to a backend
that does not declare it gets a plain-text note, but no severity change, since
H2 owns enforcement (#3219).

Leaf module: imports only ``cw.doctor._shared`` from the doctor package, and
repeats the lane-to-backend walk of ``doctor/tracker_mcp.py`` without its
``_TRACKER_STAGES`` filter (R18: no cross-doctor imports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.executor._capabilities import declared_capabilities
from cw.models import Stage, TicketTask
from cw.models.capabilities import HarnessCapabilities, in_enum_order

if TYPE_CHECKING:
    from collections.abc import Iterable
    from enum import StrEnum

    from cw.models import ClientConfig

_CAVEAT = "declared, not enforced; auth, env and headless load unverified"
_UNKNOWN_BACKEND = "unrecognized backend"


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
        f"; declared, not enforced (H2/#3219): routes {names} to "
        f"{caps.harness.value}, which declares {declared}"
    )


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


def _lane_results(client: ClientConfig, lane: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    for backend, routed in _lane_backends(client, lane).items():
        name = f"harness-capabilities/{client.name}/{lane}/{backend}"
        caps = declared_capabilities(backend)
        results.append(
            CheckResult(
                name=name, ok=True, detail=_detail(backend, routed, caps), warn=False
            )
        )
    return results


def _check_harness_capabilities(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Declared capabilities per (client, lane, backend). Never warns or fails."""
    results: list[CheckResult] = []
    for client in clients.values():
        for lane in client.effective_lanes:
            results.extend(_lane_results(client, lane.name))
    return results
