"""Independent PR-state source for ``stale_dispatch`` parks (GitHub #1927).

A ``stale_dispatch`` park (``disposition="stale_dispatch"`` /
``blocked_reason="pr_already_open"``, GitHub #1862 + #1902) is blocked behind
its OWN earlier, un-harvested-sentinel PR. That PR is discovered by a live
``gh pr list --head <branch>`` self-check which never writes a ``pr_url`` onto
any ``TicketTask``, so no store row ever carries it -- and
``release_stale_gated_tasks``'s Variant B cross-reference, which scans task
rows' hydrated ``pr_state``, therefore had nothing to match the park's
``blocked_on_pr`` against. The park could never self-release.

This pass closes that gap by registering the blocking PR as a ``WatchedPr``:
a store-level PR watch, independent of any task row, already hydrated every
serve tick by ``cw.pr_hydrate._hydrate_watched_prs`` (RFC 0011 S2, #1154).
The mechanism is not new -- this is a third producer for it.

Shape mirrors ``cw.dispatch.pr_gate``: a small, single-purpose, tick-scoped
module. Three phases, deliberately ordered so no network/subprocess work ever
runs under ``dev_queue_lock``:

1. cheap in-memory scan of the store for un-watched parks (no lock, no I/O);
2. local ``git remote get-url origin`` slug resolution per candidate
   (outside any lock);
3. the atomic, self-locking ``register_or_adopt_watched_pr`` insert.

The scan is a full rescan of ``store.tasks`` every call, NOT a piggyback on
``release_stale_gated_tasks``'s own walk: a park stamped by #1902's routing
code before this pass existed must still be picked up retroactively (binding
A2). Idempotency comes from ``register_watched_pr``'s existing
``(repo, pr_number, status == "active")`` dedup -- the pre-filter below is
only an optimization that avoids a pointless ``git`` call. The dedup's own
lack of a ``client`` dimension is the reason registration goes through
``register_or_adopt_watched_pr`` rather than ``register_watched_pr`` directly
-- see that function's docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from cw.config import load_clients
from cw.dev_queue import STALE_DISPATCH_DISPOSITION, load_dev_queue
from cw.dev_queue.crud import register_or_adopt_watched_pr
from cw.gh import github_pr_url
from cw.models import WatchedPr
from cw.pr_hydrate import _resolve_repo_slug
from cw.reconcile.tasks import (
    _client_cwd,
    _is_dangling_client,
    _is_variant_b_gate_task,
)

if TYPE_CHECKING:
    from cw.models import TicketTask

# The ``WatchedPr.source`` discriminator for this producer, distinguishing an
# auto-registered park watch from the two operator-driven ones ("webhook",
# "cli") in any future dismiss/report path.
_WATCHED_PR_SOURCE_STALE_DISPATCH_PARK: Final = "stale_dispatch_park"


def _is_stale_dispatch_park(task: TicketTask) -> bool:
    """True iff *task* is a BLOCKED_ON_USER ``stale_dispatch`` park.

    Delegates to ``cw.reconcile.tasks._is_variant_b_gate_task`` rather than
    re-encoding its ``status``/``blocked_on_pr``/``blocked_reason`` checks: it
    already reaches the identical ``stale_dispatch`` result on its non-Variant-B
    branch, and duplicating that logic here would let a future widening of the
    shared predicate (the same shape of change #1902 already made once) update
    one copy and silently strand the other. The added ``disposition`` conjunct
    is what narrows it to just this producer -- ``_is_variant_b_gate_task``
    also admits the ``merge_gate_blocked``/``prior_pipeline_pr_open`` producer,
    whose blocking PR belongs to a DIFFERENT ticket that already carries it as
    a ``pr_url`` on its own store row, so only THIS producer's PR is
    unreachable and needs a watch.
    """
    return (
        _is_variant_b_gate_task(task) and task.disposition == STALE_DISPATCH_DISPOSITION
    )


def _unwatched_park_candidates(
    tasks: list[TicketTask], watched_prs: list[WatchedPr]
) -> list[tuple[TicketTask, int]]:
    """``(task, blocking PR number)`` for every park with no active watch yet.

    The pre-filter key is ``(client, pr_number)``, not ``pr_number`` alone: a
    bare PR number is only unambiguous within one client's repo (#1269). A
    ``client is None`` watch (webhook/cli producer) is therefore not a match --
    it could name that number in an entirely different repo. This is purely an
    optimization that skips a pointless ``git`` call before a candidate reaches
    registration; it is NOT the authority on whether a candidate can safely be
    registered. That authority is ``register_or_adopt_watched_pr``, which
    handles the case this filter cannot see -- a same-repo, same-PR-number
    watch already held by a DIFFERENT client (or by no client at all) --
    explicitly rather than by an unenforced injective client-to-repo
    assumption.
    """
    already_watched = {
        (w.client, w.pr_number)
        for w in watched_prs
        if w.status == "active" and w.client is not None
    }
    candidates: list[tuple[TicketTask, int]] = []
    for task in tasks:
        pr_number = task.blocked_on_pr
        if pr_number is None or not _is_stale_dispatch_park(task):
            continue
        if (task.client, pr_number) in already_watched:
            continue
        candidates.append((task, pr_number))
    return candidates


def register_stale_dispatch_watched_prs() -> list[str]:
    """Register a ``WatchedPr`` for each un-watched ``stale_dispatch`` park.

    Returns the ticket_ids for which a fresh watch was inserted (a park whose
    watch already existed contributes nothing, so a steady-state tick returns
    an empty list and makes no ``git`` call at all).

    Best-effort throughout, matching every sibling gh/git-touching reconcile
    pass: an unresolvable ``origin`` remote skips that candidate for this tick
    with no failure cached across ticks, so the next tick retries. A dangling
    client (present on the task but absent from a populated ``clients.yaml``)
    is skipped rather than resolved against the ambient CWD -- that is config
    drift, not single-tenant mode, and resolving it could attribute a
    same-numbered PR from the wrong repo (#1269). Within a single call, each
    distinct client's remote is resolved at most once: candidates for the
    same client (most likely on the retroactive backfill tick, which can
    surface many at once) share one cached result rather than each spawning
    its own ``git`` subprocess.

    Emits no orchestrator event on the common path, matching both existing
    ``WatchedPr`` producers: the operator-facing signal for this mechanism is
    ``release_stale_gated_tasks``'s ``SESSION_REAP_PROPOSED`` at actual
    release/stamp time. The exception is a genuine cross-client registration
    collision, which ``register_or_adopt_watched_pr`` records as
    ``watched_pr.collision`` rather than silently dropping (see its
    docstring).
    """
    store = load_dev_queue()
    candidates = _unwatched_park_candidates(store.tasks, store.watched_prs)
    if not candidates:
        return []

    clients = load_clients()
    repo_by_client: dict[str, str | None] = {}
    registered: list[str] = []
    for task, pr_number in candidates:
        if _is_dangling_client(task.client, clients):
            continue
        if task.client not in repo_by_client:
            # Path.cwd() preserves _client_cwd's documented ambient-CWD
            # fallback for a single-tenant deployment with no clients.yaml at
            # all; spelled explicitly because _resolve_repo_slug requires a
            # concrete Path.
            git_dir = _client_cwd(task.client, clients) or Path.cwd()
            repo_by_client[task.client] = _resolve_repo_slug(git_dir)
        repo = repo_by_client[task.client]
        if repo is None:
            continue
        outcome = register_or_adopt_watched_pr(
            WatchedPr(
                pr_url=github_pr_url(repo, pr_number),
                repo=repo,
                pr_number=pr_number,
                client=task.client,
                source=_WATCHED_PR_SOURCE_STALE_DISPATCH_PARK,
            )
        )
        if outcome != "collision":
            registered.append(task.ticket_id)
    return registered
