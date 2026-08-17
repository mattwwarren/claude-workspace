"""Pre-dispatch open-PR gate for PLAN/IMPL-stage PENDING tasks (#1862).

The incident this closes: a dispatch succeeded, pushed a branch, and opened a
PR, but the queue row was never advanced past PLAN/IMPL (the session died
before its sentinel landed, or the sentinel was never harvested). The next tick
sees a PENDING PLAN-stage row and re-claims it — a second worker then plans and
implements a ticket whose work is already sitting in an open, unmerged PR.

This module answers the one question the claim path needs before that happens:
**does this client's PENDING PLAN/IMPL-stage ticket already have an open PR on
its own feature branch?** The claim path (``cw.dispatch.claim``) turns a "yes"
into a ``BLOCKED_ON_USER`` park rather than a claim.

Two hard contracts, both inherited from the sibling gate modules:

* **Fail open, never gate on an unreliable signal.** ``pr_exists_for_branch``
  reports ``None`` for a transient error and ``gh_available=False`` when the
  binary is absent; both resolve to "no open PR" here, and neither is cached.
  A false positive parks a healthy ticket and costs an operator; a false
  negative costs at most one duplicate dispatch, which is the status quo. This
  is the same posture ``branch_freshness.py`` documents.
* **No network calls under ``dev_queue_lock()``.** This runs from
  ``_dispatch_client_lanes`` *before* the per-lane claim loop, outside the
  queue lock, and its result is folded into ``_claim_next_pending`` as a
  precomputed ``frozenset`` — exactly the shape ``gating.py``'s TTL-cached
  ``_resolve_availability`` preflight probe already establishes.

The per-ticket probe is TTL-cached in ``dispatch_state.json`` (see
:class:`~cw.dispatch_state.OpenPrProbeCache`), so in **steady state** a
30-second tick cadence does not re-shell ``gh pr list`` once per PENDING
ticket per tick. That guarantee is steady-state only: on a cold cache (first
tick after deploy, TTL expiry burst, or a large PLAN/IMPL PENDING backlog),
``resolve_stale_pr_ticket_ids`` bounds its own worst-case cost per call via
``_MAX_PROBES_PER_TICK`` below rather than probing an unbounded backlog
serially in one call.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dispatch_state import (
    OpenPrProbeCache,
    load_open_pr_probe_cache,
    save_open_pr_probe_entries,
)
from cw.gh import pr_exists_for_branch
from cw.models import QueueItemStatus, Stage
from cw.reconcile import feature_branch_key
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")

# TTL (seconds) for one ticket's open-PR probe result. Long enough that a
# 30-second dispatch cadence does not re-shell `gh pr list` per PENDING ticket
# per tick; short enough that a PR opened by a sibling session is noticed
# within a few ticks. Internal tuning constant with no external contract --
# safe to adjust without a schema change, since a shape/value drift self-heals
# within one TTL window (see OpenPrProbeCache's docstring).
_OPEN_PR_PROBE_TTL_SECONDS = 300

# Cap on fresh `gh pr list` probes (cache misses/expiries) per
# resolve_stale_pr_ticket_ids call (#1862 perf follow-up). Cache hits are
# unbounded -- they cost no subprocess call -- only new probes are capped.
# A cold cache with more candidates than this is probed incrementally across
# ticks rather than serially in one call: each capped-out candidate is simply
# left unprobed this call (never added to the stale set, so it claims
# normally this tick) and is picked up on a later tick once earlier
# candidates' entries land in the TTL cache. Internal tuning constant, no
# external contract -- 20 * up to 10s (_PR_EXISTS_TIMEOUT) bounds one call to
# a few minutes worst case, well inside the 30s tick cadence's tolerance for
# an occasional slow tick without stalling the sequential per-client loop for
# the many minutes an unbounded backlog could cost.
_MAX_PROBES_PER_TICK = 20

# The stages at which an open PR on this ticket's own branch means the dispatch
# is stale. REVIEW and FINALIZE are deliberately excluded: a ticket at those
# stages legitimately HAS an open PR -- that is the artifact under review --
# so gating there would park the entire healthy tail of the pipeline.
_GATED_STAGES: frozenset[Stage] = frozenset({Stage.PLAN, Stage.IMPL})


def _gated_candidates(
    client_name: str, queue_snapshot: DevQueueStore
) -> list[TicketTask]:
    """PENDING PLAN/IMPL-stage tasks for *client_name*, in snapshot order."""
    return [
        task
        for task in queue_snapshot.tasks
        if task.client == client_name
        and task.status == QueueItemStatus.PENDING
        and task.stage in _GATED_STAGES
    ]


def _probe_open_pr(client: ClientConfig, ticket_id: str) -> bool | None:
    """Probe ``gh`` for an open PR on *ticket_id*'s feature branch.

    Returns ``True``/``False`` for a reliable verdict, or ``None`` when the
    signal is unusable (transient ``gh`` error, or ``gh`` not installed). The
    caller must treat ``None`` as "no open PR" AND must not cache it.

    ``cwd`` is scoped to the client's git dir so a multi-client host cannot
    misattribute a same-numbered ticket's PR from another repo (#1269).
    """
    branch = feature_branch_key(client.name, ticket_id, {client.name: client})
    open_pr, gh_available = pr_exists_for_branch(branch, cwd=_git_dir(client))
    if not gh_available:
        _log.debug(
            "dispatch: open-PR gate skipped for %s/%s — gh binary unavailable",
            client.name,
            ticket_id,
        )
        return None
    return open_pr


def resolve_stale_pr_ticket_ids(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    ttl_seconds: int = _OPEN_PR_PROBE_TTL_SECONDS,
    now: datetime | None = None,
) -> frozenset[str]:
    """Ticket ids whose dispatch is stale because a PR is already open (#1862).

    Scans *queue_snapshot* for *client*'s PENDING PLAN/IMPL-stage tasks and
    returns the subset whose feature branch already has an open PR. Every other
    task -- a later stage, a non-PENDING status, another client's row -- is
    skipped without a probe.

    Each candidate consults the persisted :class:`OpenPrProbeCache` first: an
    entry probed within *ttl_seconds* is reused, otherwise ``gh`` is probed
    fresh and the (reliable) verdict is persisted. Only reliable verdicts are
    written, so a transient failure re-probes next tick rather than latching.

    *now* is injectable purely for deterministic TTL tests; production callers
    omit it.

    Callers must treat the returned set as "positive evidence of an open PR",
    never its complement as a guarantee that no PR exists -- this function
    fails open at every unresolvable step.
    """
    candidates = _gated_candidates(client.name, queue_snapshot)
    if not candidates:
        return frozenset()
    resolved_now = now if now is not None else datetime.now(UTC)
    cache = load_open_pr_probe_cache()
    stale: set[str] = set()
    newly_probed: dict[str, OpenPrProbeCache] = {}
    probes_used = 0
    skipped_cap = 0
    for task in candidates:
        key = f"{client.name}/{task.ticket_id}"
        cached = cache.get(key)
        if (
            cached is not None
            and (resolved_now - cached.probed_at).total_seconds() < ttl_seconds
        ):
            if cached.has_open_pr:
                stale.add(task.ticket_id)
            continue
        if probes_used >= _MAX_PROBES_PER_TICK:
            # Cap reached: leave this candidate unprobed rather than serially
            # fanning out an unbounded number of gh subprocess calls. It
            # claims normally this tick and is reconsidered next tick.
            skipped_cap += 1
            continue
        probes_used += 1
        probed = _probe_open_pr(client, task.ticket_id)
        if probed is None:
            # Unreliable reading: fail open and do NOT cache it, so the next
            # tick re-probes instead of inheriting a persisted false negative.
            continue
        newly_probed[task.ticket_id] = OpenPrProbeCache(
            probed_at=resolved_now, has_open_pr=probed
        )
        if probed:
            stale.add(task.ticket_id)
    save_open_pr_probe_entries(client.name, newly_probed)
    if skipped_cap:
        _log.info(
            "dispatch: open-PR gate hit its per-tick probe cap (%d) for %s; "
            "%d candidate(s) left unprobed this tick, reconsidered next tick",
            _MAX_PROBES_PER_TICK,
            client.name,
            skipped_cap,
        )
    if stale:
        _log.info(
            "dispatch: open-PR gate holds %s task(s) for %s: %s",
            len(stale),
            client.name,
            sorted(stale),
        )
    return frozenset(stale)
