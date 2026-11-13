"""Advisory ``spared-unrecorded-workers`` check for ``cw doctor`` (#3257).

The reconcile leaked-worker sweep (:mod:`cw.reconcile.leaked_workers`) stops an
unrecorded daemon roster worker only when it is evidenced orphaned: older than
the spawn-to-record grace AND homed inside a configured client's cw worktree
base (#2670). Every other old unrecorded worker is spared and stays on the
roster until someone stops it by hand: a non-cw or out-of-base cwd, a removed
client or changed ``worktree_base``, or every such worker while
``clients.yaml`` is unreadable.

This check lists those spared-but-old workers so the operator can see them. It
is read-only and advisory (``ok=True``, never ``ok=False``), and it is NOT a
wedge finding, so no ``--reap`` selector ever acts on it. A hand-launched
non-cw ``claude --bg`` worker also appears; that is intentional and can be
ignored. Workers with no ``startedAt`` cannot be aged and are not listed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.doctor._shared import CheckResult
from cw.native_daemon import get_native_daemon_client
from cw.reconcile.leaked_workers import (
    LEAKED_WORKER_GRACE_SECONDS,
    find_spared_unrecorded_workers,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import CwState
    from cw.models.client import ClientConfig
    from cw.native_daemon import WorkerHome

_CHECK_NAME: Final = "spared-unrecorded-workers"
_MAX_LISTED: Final = 5
_ROSTER_UNREADABLE_DETAIL: Final = (
    "skipped: the daemon roster could not be read, so spared workers are unknown"
)
_NONE_DETAIL: Final = (
    "no unrecorded daemon worker older than "
    f"{LEAKED_WORKER_GRACE_SECONDS} s was spared by the leaked-worker sweep"
)
_REMEDIATION: Final = (
    "The leaked-worker sweep spares these (cwd outside every configured cw "
    "worktree base, a removed client or changed worktree_base, or an unreadable "
    "clients.yaml), so they stay on the roster. Stop one by hand with "
    "`claude stop <short_id>`. A hand-launched non-cw `claude --bg` worker also "
    "appears here; ignore it if it is intentional"
)


def _format_worker(home: WorkerHome) -> str:
    return f"{home.short_id} (cwd {home.cwd}; claude stop {home.short_id})"


def _check_spared_unrecorded_workers(
    state: CwState, clients: Mapping[str, ClientConfig]
) -> CheckResult:
    """One advisory row listing old unrecorded workers the sweep spares.

    OK (no warn) when there are none or the roster is unreadable; WARN when any
    is found, naming each worker's short id, cwd and the ``claude stop`` remedy,
    bounded to the first few with a "+N more" count. *clients* is the configured
    client map; an empty map (``clients.yaml`` unreadable) spares every
    unrecorded worker, so all old ones are listed.
    """
    spared = find_spared_unrecorded_workers(
        state,
        daemon=get_native_daemon_client(),
        now=datetime.now(UTC),
        clients=clients,
    )
    if spared is None:
        return CheckResult(_CHECK_NAME, ok=True, detail=_ROSTER_UNREADABLE_DETAIL)
    if not spared:
        return CheckResult(_CHECK_NAME, ok=True, detail=_NONE_DETAIL)
    ordered = sorted(spared, key=lambda h: h.short_id)
    items = "; ".join(_format_worker(h) for h in ordered[:_MAX_LISTED])
    overflow = len(ordered) - _MAX_LISTED
    if overflow > 0:
        items += f"; +{overflow} more"
    detail = (
        f"{len(ordered)} unrecorded daemon worker(s) older than "
        f"{LEAKED_WORKER_GRACE_SECONDS} s left on the roster: {items}. "
        f"{_REMEDIATION}"
    )
    return CheckResult(_CHECK_NAME, ok=True, warn=True, detail=detail)
