"""Tickets-binding system check for cw doctor (#3702, RFC 0019 R1).

One ``CheckResult`` named ``bindings/<client>`` per client with a
``bindings.tickets`` block, comparing its ``system`` with the project's
``tracking.primary.system`` (read through ``cw.tracker.resolve_tracker`` at
``repo_path`` or else ``workspace_path``). Equal is ok; different fails,
naming both; either unset is an advisory warning with the fix in the detail.
A client migrated from the deprecated ``tracker_mcp_gate`` has no ``system``
yet, so it warns until ``bindings.tickets.system`` is set. Clients without a
tickets binding emit nothing. Leaf module -- no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.tracker import resolve_tracker

if TYPE_CHECKING:
    from cw.models import ClientConfig

_CHECK_NAME = "bindings"
_UNSET = "unset"
_SET_BINDING_HINT = "set bindings.tickets.system in clients.yaml"
_PIN_PROJECT_HINT = "pin tracking.primary.system in .claude/project-config.yaml"


def _client_result(name: str, client: ClientConfig, bound: str | None) -> CheckResult:
    root = client.repo_path or client.workspace_path
    project = resolve_tracker(root)
    check = f"{_CHECK_NAME}/{name}"
    hints = [
        hint
        for hint, missing in (
            (_SET_BINDING_HINT, not bound),
            (_PIN_PROJECT_HINT, not project),
        )
        if missing
    ]
    if hints:
        return CheckResult(
            check,
            ok=True,
            warn=True,
            detail=(
                f"cannot compare bindings.tickets.system ({bound or _UNSET}) with"
                f" the project tracker ({project or _UNSET}): {'; '.join(hints)}"
            ),
        )
    if bound == project:
        return CheckResult(check, ok=True, detail=f"tickets system {bound}")
    return CheckResult(
        check,
        ok=False,
        detail=(
            f"bindings.tickets.system is {bound} but the project tracker"
            f" (tracking.primary.system) is {project}; make them agree"
        ),
    )


def _check_bindings(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Compare each client's tickets binding system with its project tracker."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        tickets = client.bindings.tickets
        if tickets is None:
            continue
        bound = None if tickets.system is None else str(tickets.system)
        results.append(_client_result(name, client, bound))
    return results
