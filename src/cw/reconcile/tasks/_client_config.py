"""Per-client config resolvers shared by the reconcile task submodules.

Leaf module: imports no sibling submodule of ``cw.reconcile.tasks``, so every
concern module can depend on it without a cycle (#2631).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, ReapPolicy


def _client_cwd(client_name: str, clients: dict[str, ClientConfig]) -> Path | None:
    """Resolve the gh cwd for *client_name*, or None if no config is found.

    None preserves today's ambient-CWD gh behavior for single-tenant
    deployments with no clients.yaml at all (see #1269 plan Adopted
    Assumptions and TestCompleteTimedOutMergedTasks's no-clients.yaml
    coverage). Callers must check :func:`_is_dangling_client` first and
    skip rather than call this when *clients* is populated but missing an
    entry for *client_name* -- that is config drift, not single-tenant mode,
    and an unscoped gh call there risks misattributing a same-numbered
    ticket from a different repo as merged (GitHub #1269).
    """
    client_cfg = clients.get(client_name)
    return _git_dir(client_cfg) if client_cfg is not None else None


def _is_dangling_client(client_name: str, clients: dict[str, ClientConfig]) -> bool:
    """True when *clients* is populated but has no entry for *client_name*.

    Distinguishes "no clients.yaml at all" (single-tenant, safe ambient-cwd
    fallback via :func:`_client_cwd`) from "client removed/renamed from an
    otherwise-populated clients.yaml" (drift), which must not fall back to
    an unscoped gh call (GitHub #1269).
    """
    return bool(clients) and client_name not in clients


def _resolve_task_policy(
    client: str,
    lane: str,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> ReapPolicy:
    """Resolve reap_policy for a task's client/lane without a ReapCandidate."""
    client_cfg = clients.get(client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == lane and lane_cfg.reap_policy is not None:
                return lane_cfg.reap_policy
    return config.reap_policy
