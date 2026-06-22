"""Monkeypatch-stable indirection for cross-cutting external dependencies.

These callables are each used by more than one reconcile cluster (idle,
stalled, phantom, salvage, tasks, core). Cluster modules invoke them as
``_deps.NAME(...)`` rather than importing them directly, so a single test
patch at ``cw.reconcile._deps.NAME`` intercepts every caller regardless of
which cluster module makes the call. Before the package split these all lived
in the single ``reconcile`` module namespace, where one ``cw.reconcile.NAME``
patch covered every call site; this module preserves that single-patch-point
property.

``checked_out_branch`` is exported without the leading underscore that the
underlying ``cw.worktree`` helper carries so that cluster modules can reference
it as a public attribute (``_deps.checked_out_branch``) without tripping the
private-member-access lint.
"""

from __future__ import annotations

from cw.config import load_effective_clients
from cw.gh import branch_exists_on_origin, pr_is_merged_for_ticket
from cw.native_daemon import get_native_daemon_client, read_supervisor_resume_session_id
from cw.notify import fire_push_notification
from cw.worktree import _checked_out_branch as checked_out_branch

__all__ = [
    "branch_exists_on_origin",
    "checked_out_branch",
    "fire_push_notification",
    "get_native_daemon_client",
    "load_effective_clients",
    "pr_is_merged_for_ticket",
    "read_supervisor_resume_session_id",
]
