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

Since #2321 the same registration covers every releasable park
(``_is_variant_b_gate_task``): a dependency hold registers under
``source="dependency_hold"``, and a ``prior_pipeline_pr_open`` park whose PR no
other row carries is also watched. A watch retires (``status="dismissed"``)
once its PR is terminal and no non-terminal row still references it (#3654).
A dismissed entry never collides with a new registration.

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

Since #3634 a hold ref may be an explicit ``owner/repo#N``. The own-repo bound
on a worker-supplied explicit ref (D7) is enforced here, not in routing:
routing runs with no git and no subprocess, and ``ClientConfig`` carries no
slug, so routing stamps the ref as given. Here, outside any lock and before
hydration, a worker park's explicit ref is compared with the holder's own
git-resolved slug (case-insensitively). An own-repo ref is normalised to a
same-repo ref and watched with ``cross_repo=False``; a ref to any other repo is
restamped to the no-id ``awaiting_operator`` park through the locked
compare-and-set ``cw.dev_queue.hold.restamp_unvetted_hold_ref``, with one
WARNING and one ``session.needs_attention`` page, and gets no watch. An
operator hold (``session_id is None``, verified with gh at hold time) is
exempt: its explicit ref registers a ``cross_repo=True`` watch for the ref's
repo with no slug resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from cw.config import load_clients
from cw.dev_queue import DEPENDENCY_HOLD_DISPOSITION, load_dev_queue
from cw.dev_queue.crud import register_or_adopt_watched_pr
from cw.dev_queue.hold import restamp_unvetted_hold_ref
from cw.events import record_event
from cw.gh import github_pr_url
from cw.models import OrchestratorEventType, WatchedPr
from cw.models.enums import WATCHED_PR_SOURCE_DEPENDENCY_HOLD
from cw.models.pr_ref import PrRef, PrRefKey, pr_evidence_keys
from cw.pr_hydrate import _resolve_repo_slug
from cw.reconcile.tasks import (
    _client_cwd,
    _is_dangling_client,
    _is_variant_b_gate_task,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

# GitHub #3634 (D7) -- the open-enum ``session.needs_attention`` paused_status
# paged when a worker-supplied explicit hold ref names a repo other than the
# holder's own.
_DEPENDENCY_REF_NOT_OWN_REPO: Final = "dependency_ref_not_own_repo"

# The ``WatchedPr.source`` discriminator for this producer, distinguishing an
# auto-registered park watch from the two operator-driven ones ("webhook",
# "cli") in any future dismiss/report path.
_WATCHED_PR_SOURCE_STALE_DISPATCH_PARK: Final = "stale_dispatch_park"


# GitHub #2321 -- a watch registered for a dependency hold (``cw dev-queue hold``
# or a worker ``dependency_unmerged`` park carrying ``depends_on_pr``).
def _watched_pr_source(
    task: TicketTask,
) -> Literal["stale_dispatch_park", "dependency_hold"]:
    """The ``WatchedPr.source`` a park's watch is registered under (#2321)."""
    if task.disposition == DEPENDENCY_HOLD_DISPOSITION:
        return WATCHED_PR_SOURCE_DEPENDENCY_HOLD
    return _WATCHED_PR_SOURCE_STALE_DISPATCH_PARK


def _is_stale_dispatch_park(task: TicketTask) -> bool:
    """True iff *task* is a releasable park that needs an independent PR watch.

    Since #2321 this covers every releasable park, not only ``stale_dispatch``:
    it delegates to ``cw.reconcile.tasks._is_variant_b_gate_task``, which admits
    the ``merge_gate_blocked``/``prior_pipeline_pr_open`` park (including a PR
    that is not another row's ``pr_url``), the ``stale_dispatch`` park, and the
    dependency hold. The name is kept for the existing callers and tests.
    """
    return _is_variant_b_gate_task(task)


def _unwatched_park_candidates(
    tasks: list[TicketTask], watched_prs: list[WatchedPr]
) -> list[tuple[TicketTask, PrRef]]:
    """``(task, blocking PR ref)`` for every park with no active watch yet.

    The pre-filter key is ``(client, ref.key())`` against each active watch's
    ``pr_evidence_keys`` (#3634), not ``pr_number`` alone: a
    bare PR number is only unambiguous within one client's repo (#1269). A
    ``client is None`` watch (webhook/cli producer) is therefore not a match --
    it could name that number in an entirely different repo. This is purely an
    optimization that skips a pointless ``git`` call before a candidate reaches
    registration (it never skips a worker park's explicit ref, which always
    needs vetting, D7); it is NOT the authority on whether a candidate can safely be
    registered. That authority is ``register_or_adopt_watched_pr``, which
    handles the case this filter cannot see -- a same-repo, same-PR-number
    watch already held by a DIFFERENT client (or by no client at all) --
    explicitly rather than by an unenforced injective client-to-repo
    assumption.
    """
    already_watched: set[tuple[str, PrRefKey]] = {
        (w.client, key)
        for w in watched_prs
        if w.status == "active" and w.client is not None
        for key in pr_evidence_keys(w.repo, w.pr_number, cross_repo=w.cross_repo)
    }
    candidates: list[tuple[TicketTask, PrRef]] = []
    for task in tasks:
        ref = task.blocked_on_pr
        if ref is None or not _is_stale_dispatch_park(task):
            continue
        # D7: a worker park's explicit ref is never deduped away, whatever
        # watch already covers it (e.g. an operator's cross_repo watch on the
        # same o/r#N): it must reach _vet_worker_ref so an off-repo ref is
        # restamped and paged instead of keeping its id and being releasable.
        # Operator holds and bare same-repo refs may still skip when watched.
        unvetted_worker_ref = ref.repo is not None and task.session_id is not None
        if not unvetted_worker_ref and (task.client, ref.key()) in already_watched:
            continue
        candidates.append((task, ref))
    return candidates


def _register(task: TicketTask, repo: str, pr_number: int, *, cross_repo: bool) -> bool:
    """Register one watch; True unless it collided with another client's."""
    outcome = register_or_adopt_watched_pr(
        WatchedPr(
            pr_url=github_pr_url(repo, pr_number),
            repo=repo,
            pr_number=pr_number,
            client=task.client,
            source=_watched_pr_source(task),
            cross_repo=cross_repo,
        )
    )
    return outcome != "collision"


def _page_refused_ref(task: TicketTask, ref: PrRef, own: str) -> None:
    """Log the ONE WARNING and record the ONE page for a refused worker ref (D7)."""
    label = ref.label()
    _log.warning(
        "dependency_ref_not_own_repo: ticket=%s client=%s ref=%s;"
        " no watch registered, row left awaiting_operator",
        task.ticket_id,
        task.client,
        label,
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id,
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _DEPENDENCY_REF_NOT_OWN_REPO,
            "breadcrumbs": (
                f"worker-supplied dependency ref {label} is not in this client's"
                f" repo {own}; a worker may arm an auto-release only for its own"
                f" repo. Hold it with: cw dev-queue hold {task.ticket_id}"
                f" --until-pr-merged {label} -c {task.client}"
            ),
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )


def _vet_worker_ref(task: TicketTask, ref: PrRef, own: str) -> bool:
    """Apply the own-repo bound to a worker park's explicit ref (D7).

    Returns whether a watch was registered. An own-repo ref is normalised to a
    same-repo ref (compare-and-set) and watched with ``cross_repo=False``; any
    other repo is restamped to the no-id ``awaiting_operator`` park and paged.
    A lost compare-and-set (the row changed, or became an operator hold) skips
    the candidate for this tick with no log and no event.
    """
    normalized = ref.normalized(own)
    if normalized.repo is None:
        if not restamp_unvetted_hold_ref(task.ticket_id, task.client, ref, normalized):
            return False
        return _register(task, own, ref.number, cross_repo=False)
    if restamp_unvetted_hold_ref(task.ticket_id, task.client, ref, None):
        _page_refused_ref(task, ref, own)
    return False


def _own_slug(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    repo_by_client: dict[str, str | None],
) -> str | None:
    """The holder's own slug, resolved at most once per client per call."""
    if task.client not in repo_by_client:
        # Path.cwd() preserves _client_cwd's documented ambient-CWD
        # fallback for a single-tenant deployment with no clients.yaml at
        # all; spelled explicitly because _resolve_repo_slug requires a
        # concrete Path.
        git_dir = _client_cwd(task.client, clients) or Path.cwd()
        repo_by_client[task.client] = _resolve_repo_slug(git_dir)
    return repo_by_client[task.client]


def _register_candidate(
    task: TicketTask,
    ref: PrRef,
    clients: dict[str, ClientConfig],
    repo_by_client: dict[str, str | None],
) -> bool:
    """Register (or vet) one candidate; return whether a watch was registered."""
    if ref.repo is not None and task.session_id is None:
        # An operator hold, verified with gh at hold time: watch the ref's
        # repo as given, with no slug resolution.
        return _register(task, ref.repo, ref.number, cross_repo=True)
    own = _own_slug(task, clients, repo_by_client)
    if own is None:
        return False  # indeterminate: retried next tick, never guessed
    if ref.repo is not None:
        return _vet_worker_ref(task, ref, own)
    return _register(task, own, ref.number, cross_repo=False)


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

    Since #3634 an explicit ref is handled per D7 (see the module docstring):
    an operator hold's ref is watched as given, a worker park's ref only when
    it names the holder's own repo.

    Emits no orchestrator event on the common path, matching both existing
    ``WatchedPr`` producers: the operator-facing signal for this mechanism is
    ``release_stale_gated_tasks``'s ``SESSION_REAP_PROPOSED`` at actual
    release/stamp time. The exception is a genuine cross-client registration
    collision, which ``register_or_adopt_watched_pr`` records as
    ``watched_pr.collision`` rather than silently dropping (see its
    docstring), and a refused worker-supplied ref (#3634), which pages
    ``session.needs_attention`` once with ``paused_status``
    ``dependency_ref_not_own_repo``.
    """
    store = load_dev_queue()
    candidates = _unwatched_park_candidates(store.tasks, store.watched_prs)
    if not candidates:
        return []

    clients = load_clients()
    repo_by_client: dict[str, str | None] = {}
    registered: list[str] = []
    for task, ref in candidates:
        if _is_dangling_client(task.client, clients):
            continue
        if _register_candidate(task, ref, clients, repo_by_client):
            registered.append(task.ticket_id)
    return registered
