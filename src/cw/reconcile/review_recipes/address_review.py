"""The address_review recipe: detect + act for changes_requested PRs (RFC 0010).

Package split (#1315, part 1 of 2). This module owns the P1/P2 ``address_review``
recipe — the review-feedback analogue of the gate recipes: it reacts to a PR
whose review came back ``changes_requested`` by dispatching an
``/address-review`` session that mechanically works the requested changes.

**P1 (GitHub #1096):** detect-only. ``_detect_address_review`` produces a
:class:`ReviewRecipeCandidate` for every dev-queue row whose ``pr_state`` carries
``attention_state == "changes_requested"``; it performs no writes.

**P2 (GitHub #1097):** the act phase. ``_act_address_review`` re-validates each
candidate under ``dev_queue_lock()``, emits ``PR_ACTION_TAKEN`` (durably, BEFORE
the spawn), and then — strictly after the lock releases — dispatches an
``/address-review`` session via ``spawn_create_impl``. A dispatch ``CwError`` or
a precondition anomaly (unparseable PR url, unresolvable client, missing
worktree) emits ``PR_ACTION_FAILED`` instead. Emit-before-dispatch is
structural: the event fires inside the lock, every spawn strictly afterward.
GitHub #1206 adds a one-shot ``address_review_fired_at`` latch, stamped inside
this same lock hold, so the dispatch fires exactly once per changes-requested
episode instead of every reconcile tick.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.pr_hydrate import _parse_pr_url, _repo_slug_mismatch
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_CHANGES_REQUESTED,
    _PAYLOAD_KEY_REVIEW_DECISION,
    RECIPE_ADDRESS_REVIEW,
    ReviewRecipeCandidate,
    _clear_ended_episodes,
    _detect_by_attention_state,
    _emit_pr_action_failed,
    _find_review_task,
    _guard_cross_repo_mismatch,
    _record_pr_action_taken,
    _review_payload_base,
    _skip_with_anomaly,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask


_log = logging.getLogger("cw.reconcile.review_recipes")


class _DispatchJob(NamedTuple):
    """Deferred dispatch job built inside dev_queue_lock(), run after release.

    A NamedTuple (not a bare positional tuple) so call sites access fields by
    name — two adjacent same-typed str fields (ticket_id, lane) made a
    positional reorder a silent, type-checker-invisible bug.
    """

    client_cfg: ClientConfig
    worktree: Path
    pr_number: int
    ticket_id: str
    lane: str
    payload_base: dict[str, object]


def _detect_address_review(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect changes_requested PRs for the address_review recipe (RFC 0010 P1)."""
    return _detect_by_attention_state(
        tasks,
        clients=clients,
        config=config,
        attention_state=_ATTENTION_CHANGES_REQUESTED,
        recipe=RECIPE_ADDRESS_REVIEW,
    )


def _prepare_dispatch_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> _DispatchJob | None:
    """Re-validate a re-loaded row under the lock; emit + build its dispatch job.

    Returns ``None`` to skip. Two skip flavours:

    * **Silent** (no event) when the row is stale — ``pr_state`` gone or no
      longer ``changes_requested`` (a concurrent re-review can have moved it on
      between detect and act) — OR already fired this episode
      (``address_review_fired_at`` is not None; not an anomaly, mirrors
      ``_prepare_auto_fix_ci_job``'s already-fired check).
    * **Anomaly** (emits ``PR_ACTION_FAILED`` + a warning) when the PR url is
      unparseable/absent, the client is unresolvable, or the worktree is
      missing — a fail-safe correction, never a silent drop.

    Otherwise records ``PR_ACTION_TAKEN`` (emit-before-dispatch) from the
    RE-LOADED row, stamps the ``address_review_fired_at`` latch to *now*, and
    returns the deferred dispatch job. ``session_id`` is the originating
    candidate's — the event fires before the new spawn exists, so it can't
    carry a session id that doesn't exist yet.
    """
    pr_state = task.pr_state
    if (
        pr_state is None
        or pr_state.attention_state != _ATTENTION_CHANGES_REQUESTED
        or task.address_review_fired_at is not None
    ):
        return None  # stale or already-fired — silent skip
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_ADDRESS_REVIEW,
        {_PAYLOAD_KEY_REVIEW_DECISION: pr_state.review_decision},
    )
    parsed = _parse_pr_url(task.pr_url) if task.pr_url is not None else None
    if task.pr_url is None or parsed is None:
        _skip_with_anomaly(
            payload_base,
            error=f"unparseable or missing pr_url: {task.pr_url!r}",
            ticket_id=task.ticket_id,
        )
        return None
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        _skip_with_anomaly(
            payload_base,
            error=f"client {task.client!r} not resolvable",
            ticket_id=task.ticket_id,
        )
        return None
    wt = task.worktree_path
    # Why fail LOUD on a missing/stale worktree: ported review-monitor lesson
    # (session:8f738500, "Stale git worktrees cause check to fail silently") —
    # review_monitor's git diff/fetch against a deleted worktree failed SILENTLY,
    # so an /address-review dispatch would run against nothing with no signal.
    # Here an absent worktree_path emits a durable PR_ACTION_FAILED correction
    # (never a silent skip). See tests/test_reconcile_review_recipes.py::
    # test_missing_worktree_emits_pr_action_failed.
    if wt is None or not wt.exists():
        _skip_with_anomaly(
            payload_base,
            error=f"worktree_path missing or absent: {wt!r}",
            ticket_id=task.ticket_id,
        )
        return None
    # GitHub #1198 — cross-repo dispatch guard. The worktree's origin remote can
    # resolve to a different repo than the PR's, so dispatching /address-review
    # here would run in the wrong workspace. local-only read, no network — safe
    # under dev_queue_lock; do not add network calls here.
    pr_repo = parsed[0]
    client_repo = _repo_slug_mismatch(pr_repo, wt)
    if client_repo is not None and not _guard_cross_repo_mismatch(
        task,
        payload_base,
        pr_repo=pr_repo,
        client_repo=client_repo,
        location="worktree origin",
    ):
        return None
    _record_pr_action_taken(
        payload_base,
        task.client,
        task.ticket_id,
        RECIPE_ADDRESS_REVIEW,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        lane=task.lane,
    )
    task.address_review_fired_at = now
    return _DispatchJob(
        client_cfg=client_cfg,
        worktree=wt,
        pr_number=parsed[1],
        ticket_id=task.ticket_id,
        lane=task.lane,
        payload_base=payload_base,
    )


def _dispatch_address_review(job: _DispatchJob) -> str | None:
    """Dispatch one ``/address-review`` session (post-lock); ``ticket_id`` or None.

    Runs strictly AFTER ``dev_queue_lock()`` releases (mirrors
    ``gate_recipes._post_auto_approve_comment`` running post-lock), so the spawn
    never nests the flock. The function-local ``spawn_create_impl`` import breaks
    the ``cw.spawn`` ↔ ``cw.reconcile`` cycle. Passes NO ``task=`` kwarg
    (Resolution 6: no dev-queue correlation). On ``CwError`` emits a durable
    ``PR_ACTION_FAILED`` correction and returns ``None`` — one candidate's
    failure never aborts the loop.
    """
    from cw.spawn import spawn_create_impl

    try:
        spawn_create_impl(
            client=job.client_cfg,
            worktree=job.worktree,
            prompt=f"/address-review {job.pr_number}",
            label=f"address-review-{job.pr_number}",
            headless=True,
            ticket_id=job.ticket_id,
            lane=job.lane,
        )
    except CwError as exc:
        _log.warning(
            "review_recipe_dispatch_failed ticket=%s pr=%s",
            job.ticket_id,
            job.pr_number,
            exc_info=True,
        )
        _emit_pr_action_failed(
            job.payload_base, error=str(exc), ticket_id=job.ticket_id
        )
        return None
    return job.ticket_id


def _clear_address_review_fired(task: TicketTask) -> None:
    task.address_review_fired_at = None


def _act_address_review(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[str]:
    """Act phase: re-validate under lock, emit PR_ACTION_TAKEN, then dispatch.

    Mirrors ``gate_recipes._act_auto_approve_review``'s shape (lock / re-load /
    re-check / emit-before-action / deferred side-effect after lock release).
    Under one ``dev_queue_lock()``:

    1. **Episode-end clear** — scan every freshly-loaded row and clear
       ``address_review_fired_at`` where it is set but the current ``pr_state``
       is None or no longer ``changes_requested`` (the episode ended; this
       re-arms the latch for a genuine future re-entry). Runs regardless of
       whether *candidates* is empty.
    2. **Fire** — for each candidate, ``_prepare_dispatch_job`` re-validates,
       emits ``PR_ACTION_TAKEN``, and stamps the latch.

    Stamping/clearing the latch IS a dev-queue write (GitHub #1206 — a latch
    field, not a status transition; all four review-recipe act phases now
    perform this same kind of write). Every ``spawn_create_impl`` runs strictly
    after the lock releases, so the dispatch never nests the flock
    (guaranteeing no self-deadlock) and ``PR_ACTION_TAKEN`` is always durable
    before its spawn.

    ``clients`` is the caller's snapshot (mirrors ``run_gate_recipes`` loading
    ``load_effective_clients()`` once and threading it down) rather than a
    second ``load_effective_clients()`` read taken inside the lock — clients.yaml
    has no locking relationship to the dev-queue store, so re-reading it there
    only extends the flock's hold time for no consistency benefit.

    Returns the acted ticket_ids (those whose ``/address-review`` dispatch
    succeeded).
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    # Keyed on (ticket_id, client): ticket_id alone is a per-repo GitHub issue
    # number, not globally unique across this multi-tenant system's clients.
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    dispatch_jobs: list[_DispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_episodes(
            store,
            attention_state=_ATTENTION_CHANGES_REQUESTED,
            get_fired_at=lambda t: t.address_review_fired_at,
            clear_fired_at=_clear_address_review_fired,
        )
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_dispatch_job(
                task,
                candidate.session_id,
                clients,
                resolved_now,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
            if job is not None:
                dispatch_jobs.append(job)
                changed = True
        if changed:
            save_dev_queue(store)
    acted: list[str] = []
    for job in dispatch_jobs:
        ticket_id = _dispatch_address_review(job)
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted
