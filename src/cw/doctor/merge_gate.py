"""Disabled Stage 4a merge gate check for cw doctor (#2851).

``merge_gate_enabled: false`` on a client turns off the Stage 4a merge gate in
``auto-dev-finalize.md``, so a forgotten switch would otherwise hide. One
``CheckResult`` named ``merge-gate/<client>`` per client that disabled it,
``ok=True, warn=True`` (advisory). Clients with the gate on emit nothing, so
default doctor output is unchanged. Config-only; no logging, no events. Leaf
module -- no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult

if TYPE_CHECKING:
    from cw.models import ClientConfig

_CHECK_NAME = "merge-gate"

_DISABLED = (
    "Stage 4a merge gate is DISABLED: merge_gate_enabled is false for client"
    " '{client}' in clients.yaml, so its tickets open PRs even when an open"
    " pipeline PR conflicts with the branch; whichever PR merges second needs a"
    " manual rebase. To restore the gate, set merge_gate_enabled: true or remove"
    " the key."
)


def _check_merge_gate_disabled(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Report each client whose Stage 4a merge gate is switched off."""
    return [
        CheckResult(
            f"{_CHECK_NAME}/{name}",
            ok=True,
            warn=True,
            detail=_DISABLED.format(client=name),
        )
        for name, client in clients.items()
        if not client.merge_gate_enabled
    ]
