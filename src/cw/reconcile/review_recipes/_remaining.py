"""Remaining review recipes + the tick entry point (RFC 0010 P4 / #1315 part 1).

Package split (#1315, part 1 of 2). This module holds the three sibling recipes
that part 2 will break out into their own modules — ``auto_fix_ci`` (re-dispatch
a ci_failing PR into auto-dev), ``request_reviewer`` (request a reviewer per the
repo's review_strategy on a no_reviewer PR), and ``escalate_merge_block`` (fire
one durable escalation per merge-blocked episode) — together with their pure
``_detect_*`` wrappers, the stateless ``_detect_repeat_fire_counts`` burst
counter, and ``run_review_recipes`` (the single per-tick detect→act entry point
``cw.reconcile.core`` calls).

``run_review_recipes`` is the package's sole top-level orchestration entry point;
it is the one sanctioned site that imports a sibling recipe module's public
detect/act pair (``address_review``), so the recipe modules themselves stay leaf
modules that only depend on ``_shared``.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from pydantic import ValidationError

from cw.config import load_effective_clients
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.pr_hydrate import _parse_pr_url, _repo_slug_mismatch
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_CI_FAILING,
    _ATTENTION_MERGE_BLOCKED,
    _ATTENTION_NO_REVIEWER,
    _PAYLOAD_KEY_CLIENT,
    _PAYLOAD_KEY_RECIPE,
    _PAYLOAD_KEY_REVIEW_DECISION,
    _PAYLOAD_KEY_TICKET_ID,
    RECIPE_AUTO_FIX_CI,
    RECIPE_ESCALATE_MERGE_BLOCK,
    RECIPE_REQUEST_REVIEWER,
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

# Sanctioned sibling import — used only by run_review_recipes (the package's
# single top-level tick entry point), never by a leaf recipe helper. See the
# "Orchestrator/tick-infra exception" in the #1315 plan: recipe modules depend
# on _shared only; this one entry-point function is the exception.
from cw.reconcile.review_recipes.address_review import (
    _act_address_review,
    _detect_address_review,
)
from cw.review_strategy import MODE_CI, resolve_review_strategy
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask


_log = logging.getLogger("cw.reconcile.review_recipes")

# RFC 0010 P4 (#1099) — request_reviewer / escalate_merge_block / auto_fix_ci
# payload keys. Each is consumed by exactly one recipe's act phase (all resident
# in this module), so they live here rather than in _shared.py.
_PAYLOAD_KEY_REVIEW_STRATEGY_MODE = "review_strategy_mode"
_PAYLOAD_KEY_REVIEWER_HANDLE = "reviewer_handle"
_PAYLOAD_KEY_MERGE_STATE_STATUS = "merge_state_status"
_PAYLOAD_KEY_FAILING_CHECKS = "failing_checks"


def _detect_auto_fix_ci(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect ci_failing PRs for the auto_fix_ci recipe (RFC 0010 P4)."""
    return _detect_by_attention_state(
        tasks,
        clients=clients,
        config=config,
        attention_state=_ATTENTION_CI_FAILING,
        recipe=RECIPE_AUTO_FIX_CI,
    )


def _detect_request_reviewer(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect no_reviewer PRs for the request_reviewer recipe (RFC 0010 P4).

    Pure/config-review-free: no ``resolve_review_strategy`` read here — the
    strategy lookup lives in the act phase, matching the module's "reads only in
    act" framing.
    """
    return _detect_by_attention_state(
        tasks,
        clients=clients,
        config=config,
        attention_state=_ATTENTION_NO_REVIEWER,
        recipe=RECIPE_REQUEST_REVIEWER,
    )


def _detect_escalate_merge_block(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect merge_blocked PRs for the escalate_merge_block recipe (RFC 0010 P4).

    Pure: the one-shot ``escalate_merge_block_fired_at`` latch is NOT consulted
    here (detect stays write-free and stateless) — the act phase re-checks the
    latch against the freshly-loaded row so a re-fire is blocked per episode.
    """
    return _detect_by_attention_state(
        tasks,
        clients=clients,
        config=config,
        attention_state=_ATTENTION_MERGE_BLOCKED,
        recipe=RECIPE_ESCALATE_MERGE_BLOCK,
    )


def _detect_repeat_fire_counts(
    *, config: OrchestratorConfig, now: datetime | None = None
) -> dict[tuple[str, str, str], int]:
    """Count PR_ACTION_TAKEN events per ``(client, ticket_id, recipe)`` in the window.

    Stateless burst detector (#1201): replays PR_ACTION_TAKEN events from the
    inbox and buckets them by ``(client, ticket_id, recipe)`` — ``client`` is
    load-bearing here: ``ticket_id`` alone is a per-repo GitHub issue number, not
    globally unique across this multi-tenant system's clients (same rationale as
    the ``by_key`` dicts in ``_act_address_review`` et al.), so two different
    clients whose numeric issue IDs collide must not share a count. Counts only
    events recorded within ``config.review_recipe_repeat_fire_window_minutes`` of
    *now* (default ``datetime.now(UTC)``) — the window itself is applied via
    ``read_events(since_ts=...)`` rather than a manual post-filter, so events
    outside the window are never even materialized. The act phase compares its
    own about-to-fire event against these counts (``_record_pr_action_taken``) to
    decide whether a repeat-fire burst has crossed the attention threshold.
    Read-only and resilient: a failed inbox read degrades to an empty dict so a
    corrupt inbox never blocks the act phase. Called ONCE per reconcile tick (in
    ``run_review_recipes``), outside every ``dev_queue_lock()``.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    cutoff = resolved_now - timedelta(
        minutes=config.review_recipe_repeat_fire_window_minutes
    )
    try:
        events = read_events(
            event_types=[OrchestratorEventType.PR_ACTION_TAKEN], since_ts=cutoff
        )
    except (OSError, json.JSONDecodeError, ValidationError):
        return {}
    counts: dict[tuple[str, str, str], int] = {}
    for event in events:
        client = event.payload.get(_PAYLOAD_KEY_CLIENT)
        ticket_id = event.payload.get(_PAYLOAD_KEY_TICKET_ID)
        recipe = event.payload.get(_PAYLOAD_KEY_RECIPE)
        if (
            not isinstance(client, str)
            or not isinstance(ticket_id, str)
            or not isinstance(recipe, str)
        ):
            continue
        key = (client, ticket_id, recipe)
        counts[key] = counts.get(key, 0) + 1
    return counts


# --- auto_fix_ci act phase (RFC 0010 P4, #1099) ----------------------------


class _RedispatchJob(NamedTuple):
    """Deferred auto_fix_ci re-dispatch built under the lock, run after release.

    Unlike ``_DispatchJob`` this carries no worktree/PR number — the auto_fix_ci
    recipe re-enqueues the ticket and runs a dispatch tick (coarse re-entry into
    auto-dev, RFC 0010 OQ2), it does not spawn a scoped ``/address-review``.
    """

    client: str
    ticket_id: str
    lane: str
    payload_base: dict[str, object]


def _detect_auto_fix_ci_repo_mismatch(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> tuple[str, str] | None:
    """Return ``(pr_repo, client_repo)`` when the row's client workspace resolves
    to a different github repo than its ``pr_url``, else None (GitHub #1198).

    Fails open: an unparseable ``pr_url``, an unresolvable client, or an
    unresolvable workspace remote all yield None (proceed, no anomaly), so the
    guard introduces no new hard-failure branches beyond the mismatch itself.
    """
    parsed = _parse_pr_url(task.pr_url) if task.pr_url is not None else None
    if parsed is None:
        return None
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        return None
    client_repo = _repo_slug_mismatch(parsed[0], client_cfg.workspace_path)
    if client_repo is None:
        return None
    return parsed[0], client_repo


def _prepare_auto_fix_ci_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> _RedispatchJob | None:
    """Re-validate a re-loaded row under the lock; emit + build its re-dispatch.

    Silent skip (no event) when the row is stale — ``pr_state`` gone or no
    longer ``ci_failing`` (a concurrent re-run can have moved it on between
    detect and act) — OR already fired this episode (``auto_fix_ci_fired_at``
    is not None; not an anomaly, mirrors ``_prepare_request_reviewer_job``'s
    already-fired check). An anomaly skip (emits ``PR_ACTION_FAILED``) when the
    row's client resolves to a different repo than its ``pr_url`` (GitHub
    #1198) — both this check and the already-fired check above must pass for
    the row to fire. Otherwise records ``PR_ACTION_TAKEN``
    (emit-before-dispatch), stamps the ``auto_fix_ci_fired_at`` latch to *now*,
    and returns the deferred re-dispatch job.
    """
    pr_state = task.pr_state
    if (
        pr_state is None
        or pr_state.attention_state != _ATTENTION_CI_FAILING
        or task.auto_fix_ci_fired_at is not None
    ):
        return None
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_AUTO_FIX_CI,
        {_PAYLOAD_KEY_FAILING_CHECKS: pr_state.failing_checks},
    )
    # GitHub #1198 — cross-repo dispatch guard. The client's workspace origin
    # remote can resolve to a different repo than the PR's, so re-dispatching
    # auto-dev here would run in the wrong repo. local-only read, no network —
    # safe under dev_queue_lock; do not add network calls here.
    mismatch = _detect_auto_fix_ci_repo_mismatch(task, clients)
    if mismatch is not None:
        pr_repo, client_repo = mismatch
        if not _guard_cross_repo_mismatch(
            task,
            payload_base,
            pr_repo=pr_repo,
            client_repo=client_repo,
            location="client workspace origin",
        ):
            return None
    _record_pr_action_taken(
        payload_base,
        task.client,
        task.ticket_id,
        RECIPE_AUTO_FIX_CI,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        lane=task.lane,
    )
    task.auto_fix_ci_fired_at = now
    return _RedispatchJob(
        client=task.client,
        ticket_id=task.ticket_id,
        lane=task.lane,
        payload_base=payload_base,
    )


def _dispatch_auto_fix_ci(job: _RedispatchJob) -> str | None:
    """Re-enqueue the ticket then run one dispatch tick (post-lock); id or None.

    Runs strictly AFTER ``dev_queue_lock()`` releases: ``add_ticket``'s own
    internal lock IS ``dev_queue_lock`` (aliased in cw.dev_queue), so calling it
    under our lock would self-deadlock. The function-local imports break the
    ``review_recipes`` -> ``dev_queue``/``dispatch`` import cycle. ``add_ticket``
    raising (``LaneNotFoundError``, a ``CwError``) after ``PR_ACTION_TAKEN`` was
    already emitted -> catch, emit ``PR_ACTION_FAILED`` correction, return None.
    The ``auto_fix_ci_fired_at`` latch stays stamped even when this dispatch
    fails — same posture as ``request_reviewer``: ``PR_ACTION_FAILED`` is the
    visible signal, the latch is NOT rolled back on dispatch failure.
    """
    from cw.dev_queue import add_ticket
    from cw.dispatch import run_dispatch_loop
    from cw.models import TicketTask

    try:
        add_ticket(
            TicketTask(ticket_id=job.ticket_id, client=job.client, lane=job.lane)
        )
        run_dispatch_loop(once=True, client=job.client, emit=None)
    except CwError as exc:
        _log.warning(
            "review_recipe_redispatch_failed ticket=%s",
            job.ticket_id,
            exc_info=True,
        )
        _emit_pr_action_failed(
            job.payload_base, error=str(exc), ticket_id=job.ticket_id
        )
        return None
    return job.ticket_id


def _clear_auto_fix_ci_fired(task: TicketTask) -> None:
    task.auto_fix_ci_fired_at = None


def _act_auto_fix_ci(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[str]:
    """Act phase for auto_fix_ci: re-validate under lock, emit, then re-dispatch.

    Mirrors ``_act_request_reviewer``'s shape. Under one ``dev_queue_lock()``:

    1. **Episode-end clear** — scan every freshly-loaded row and clear
       ``auto_fix_ci_fired_at`` where it is set but the current ``pr_state`` is
       None or no longer ``ci_failing`` (the episode ended; this re-arms the
       latch for a genuine future re-entry). Runs regardless of whether
       *candidates* is empty.
    2. **Fire** — for each candidate, ``_prepare_auto_fix_ci_job`` re-validates
       both the latch and the cross-repo dispatch guard (GitHub #1198;
       ``clients`` is threaded through for the guard), emits
       ``PR_ACTION_TAKEN``, and stamps the latch.

    Stamping/clearing the latch IS a dev-queue write (GitHub #1206: all four
    review-recipe act phases now perform this same kind of write — a latch
    field, not a status transition; none remain read-only), saved before the
    lock releases. The re-enqueue +
    dispatch tick runs strictly after the lock releases (``add_ticket``
    re-acquires ``dev_queue_lock``, so nesting would self-deadlock). Returns
    the ticket_ids whose re-dispatch succeeded.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_RedispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_episodes(
            store,
            attention_state=_ATTENTION_CI_FAILING,
            get_fired_at=lambda t: t.auto_fix_ci_fired_at,
            clear_fired_at=_clear_auto_fix_ci_fired,
        )
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_auto_fix_ci_job(
                task,
                candidate.session_id,
                clients,
                resolved_now,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
            if job is not None:
                jobs.append(job)
                changed = True
        if changed:
            save_dev_queue(store)
    acted: list[str] = []
    for job in jobs:
        ticket_id = _dispatch_auto_fix_ci(job)
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted


# --- request_reviewer act phase (RFC 0010 P4, #1099) -----------------------


class _ReviewerJob(NamedTuple):
    """Deferred request_reviewer gh call built under the lock, run after release."""

    pr_url: str
    handle: str
    ticket_id: str
    payload_base: dict[str, object]
    # Client repo dir the deferred gh call is scoped to (GitHub #1269/#1279).
    # Mirrors _DispatchJob.client_cfg: resolved under the lock, carried across
    # the lock boundary so the post-lock gh call targets the right repo.
    cwd: Path | None


def _prepare_request_reviewer_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> _ReviewerJob | None:
    """Re-validate under the lock, resolve the review strategy, emit + build job.

    Skip flavours:

    * **Silent** (no event) when the row is stale (``pr_state`` gone / no longer
      ``no_reviewer``) OR already fired this episode (``request_reviewer_fired_at``
      is not None — not an anomaly, mirrors ``_fire_escalate_merge_block``'s
      already-fired check) OR the resolved strategy ``mode == "ci"`` — an
      intentional "rely on CI, request no reviewer" policy, not an anomaly.
    * **Anomaly** (``PR_ACTION_FAILED`` + warning) when the client is
      unresolvable, or the strategy names a ``repo_owner``/``reviewer_team`` mode
      whose handle is missing (a configured-but-broken repo), or the PR url is
      absent — a fail-safe correction.

    Otherwise records ``PR_ACTION_TAKEN`` (payload carries the strategy mode +
    reviewer handle), stamps the ``request_reviewer_fired_at`` latch to *now*,
    and returns the deferred gh-call job.
    """
    pr_state = task.pr_state
    if (
        pr_state is None
        or pr_state.attention_state != _ATTENTION_NO_REVIEWER
        or task.request_reviewer_fired_at is not None
    ):
        return None
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_REQUEST_REVIEWER,
        {_PAYLOAD_KEY_REVIEW_DECISION: pr_state.review_decision},
    )
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        _skip_with_anomaly(
            payload_base,
            error=f"client {task.client!r} not resolvable",
            ticket_id=task.ticket_id,
        )
        return None
    strategy = resolve_review_strategy(
        client_cfg.repo_path or client_cfg.workspace_path
    )
    if strategy.mode == MODE_CI:
        return None  # policy: rely on CI — silent skip, not an anomaly
    if strategy.handle is None:
        _skip_with_anomaly(
            payload_base,
            error=f"review_strategy mode {strategy.mode!r} is missing its handle",
            ticket_id=task.ticket_id,
        )
        return None
    if task.pr_url is None:  # pragma: no cover - _is_candidate guarantees non-null
        _skip_with_anomaly(
            payload_base,
            error="pr_url missing",
            ticket_id=task.ticket_id,
        )
        return None
    payload_base[_PAYLOAD_KEY_REVIEW_STRATEGY_MODE] = strategy.mode
    payload_base[_PAYLOAD_KEY_REVIEWER_HANDLE] = strategy.handle
    _record_pr_action_taken(
        payload_base,
        task.client,
        task.ticket_id,
        RECIPE_REQUEST_REVIEWER,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        lane=task.lane,
    )
    task.request_reviewer_fired_at = now
    return _ReviewerJob(
        pr_url=task.pr_url,
        handle=strategy.handle,
        ticket_id=task.ticket_id,
        payload_base=payload_base,
        cwd=_git_dir(client_cfg),
    )


def _dispatch_request_reviewer(job: _ReviewerJob) -> str | None:
    """Request the resolved reviewer on the PR (post-lock). ticket_id or None.

    ``add_pr_reviewer`` never raises (it swallows OSError/timeout, returning
    ``None``), but a swallowed exception or a non-zero ``returncode`` both mean
    the gh call did not actually request the reviewer — mirrors
    ``gate_recipes._post_auto_approve_comment``'s log-on-failure precedent for
    this exact ``CompletedProcess | None`` return shape. A failure here emits a
    ``PR_ACTION_FAILED`` correction so the already-recorded ``PR_ACTION_TAKEN``
    isn't the only, misleadingly-optimistic signal in the audit trail. Imported
    function-locally to keep the ``cw.gh`` dependency off module import.
    """
    from cw.gh import add_pr_reviewer

    result = add_pr_reviewer(job.pr_url, job.handle, cwd=job.cwd)
    if result is None:
        _skip_with_anomaly(
            job.payload_base,
            error="gh call failed (subprocess error or timeout)",
            ticket_id=job.ticket_id,
        )
        return None
    if result.returncode != 0:
        _skip_with_anomaly(
            job.payload_base,
            error=(
                f"gh pr edit --add-reviewer failed rc={result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()}"
            ),
            ticket_id=job.ticket_id,
        )
        return None
    return job.ticket_id


def _clear_request_reviewer_fired(task: TicketTask) -> None:
    task.request_reviewer_fired_at = None


def _act_request_reviewer(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[str]:
    """Act phase for request_reviewer: re-validate + resolve strategy, then gh.

    Mirrors ``_act_address_review``'s lock/re-load/emit-before-action/deferred-
    side-effect shape. Under one ``dev_queue_lock()``:

    1. **Episode-end clear** — scan every freshly-loaded row and clear
       ``request_reviewer_fired_at`` where it is set but the current
       ``pr_state`` is None or no longer ``no_reviewer`` (the episode ended;
       this re-arms the latch for a genuine future re-entry). Runs regardless
       of whether *candidates* is empty.
    2. **Fire** — for each candidate, ``_prepare_request_reviewer_job``
       re-validates, emits ``PR_ACTION_TAKEN``, and stamps the latch.

    Stamping/clearing the latch IS a dev-queue write (GitHub #1206: all four
    review-recipe act phases now perform this same kind of write — a latch
    field, not a status transition; none remain read-only), saved before the
    lock releases. The ``add_pr_reviewer`` gh call runs
    strictly after the lock releases. Returns the ticket_ids for which a
    reviewer request actually succeeded (a failed gh call is excluded and
    corrected via ``PR_ACTION_FAILED``).
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_ReviewerJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_episodes(
            store,
            attention_state=_ATTENTION_NO_REVIEWER,
            get_fired_at=lambda t: t.request_reviewer_fired_at,
            clear_fired_at=_clear_request_reviewer_fired,
        )
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_request_reviewer_job(
                task,
                candidate.session_id,
                clients,
                resolved_now,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
            if job is not None:
                jobs.append(job)
                changed = True
        if changed:
            save_dev_queue(store)
    acted: list[str] = []
    for job in jobs:
        ticket_id = _dispatch_request_reviewer(job)
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted


# --- escalate_merge_block act phase (RFC 0010 P4, #1099) -------------------


def _clear_escalate_merge_block_fired(task: TicketTask) -> None:
    task.escalate_merge_block_fired_at = None


def _act_escalate_merge_block(
    candidates: list[ReviewRecipeCandidate],
    *,
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[str]:
    """Act phase for escalate_merge_block: fire once per merge-blocked episode.

    Under one ``dev_queue_lock()`` (the event IS the escalation — no dispatch or
    spawn):

    1. **Episode-end clear** — scan every freshly-loaded row and clear
       ``escalate_merge_block_fired_at`` where it is set but the current
       ``pr_state`` is None or no longer ``merge_blocked`` (the episode ended;
       this re-arms the latch for a genuine future re-entry). Runs regardless of
       whether *candidates* is empty, so a tick where the row has moved off
       ``merge_blocked`` still clears the latch.
    2. **Fire** — for each candidate, re-validate ``attention_state ==
       merge_blocked`` and ``escalate_merge_block_fired_at is None``; if both
       hold, emit ``PR_ACTION_TAKEN`` and stamp the latch to *now*. An already-
       stamped row is a silent skip (already fired this episode).

    Stamping/clearing the latch IS a dev-queue write (GitHub #1206: all four
    review-recipe act phases now perform this same kind of write — a latch
    field, not a status transition; none remain read-only). ``record_event``
    nests the inbox lock INSIDE
    ``dev_queue_lock`` (never the reverse), so emitting under the lock is
    deadlock-safe (same ordering as ``cw.reconcile.escalation``).
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    acted: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_episodes(
            store,
            attention_state=_ATTENTION_MERGE_BLOCKED,
            get_fired_at=lambda t: t.escalate_merge_block_fired_at,
            clear_fired_at=_clear_escalate_merge_block_fired,
        )
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            if _fire_escalate_merge_block(
                task,
                candidate.session_id,
                resolved_now,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            ):
                acted.append(task.ticket_id)
                changed = True
        if changed:
            save_dev_queue(store)
    return acted


def _fire_escalate_merge_block(
    task: TicketTask,
    session_id: str | None,
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> bool:
    """Emit + stamp the latch for one still-merge-blocked, un-fired row.

    Returns True when it fired (event emitted, latch stamped); False for a stale
    row (moved off merge_blocked) or one already fired this episode.
    """
    pr_state = task.pr_state
    if pr_state is None or pr_state.attention_state != _ATTENTION_MERGE_BLOCKED:
        return False
    if task.escalate_merge_block_fired_at is not None:
        return False
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_ESCALATE_MERGE_BLOCK,
        {_PAYLOAD_KEY_MERGE_STATE_STATUS: pr_state.merge_state_status},
    )
    _record_pr_action_taken(
        payload_base,
        task.client,
        task.ticket_id,
        RECIPE_ESCALATE_MERGE_BLOCK,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        lane=task.lane,
    )
    task.escalate_merge_block_fired_at = now
    return True


def run_review_recipes(*, config: OrchestratorConfig) -> list[str]:
    """Run all enabled review recipes for one reconcile tick (P2: detect → act).

    No-op (returns ``[]`` immediately) unless ``config.review_recipes_enabled``
    is True. Loads a fresh dev-queue snapshot itself rather than accepting one
    from the caller — by the wiring point in ``_reconcile_locked`` several prior
    sweeps have already mutated and saved the queue, so a caller-supplied
    snapshot would be stale (mirrors ``run_gate_recipes``). No ``load_state()``
    call: candidate ``client``/``lane`` come straight off the task, so no
    ``CwState`` lookup is needed. No ``now`` parameter: the act phase performs no
    time-stamped mutation.

    Per-lane enablement (RFC 0010 P3) is resolved against effective clients —
    ``load_effective_clients`` so lane pause/override state is honoured, matching
    ``run_gate_recipes``. Loaded once and threaded into both the detect and act
    phases (mirrors ``run_gate_recipes``) rather than re-read inside the act
    phase's lock.

    P4 (#1099) adds three sibling recipes, each routed by a distinct PR
    attention state (1:1 with a recipe; see the routing test): ``auto_fix_ci``
    (re-dispatches a ci_failing PR into auto-dev), ``request_reviewer`` (requests
    a reviewer per the repo's review_strategy on a no_reviewer PR), and
    ``escalate_merge_block`` (fires one durable escalation per merge-blocked
    episode). Each detect->act pair runs against the same fresh ``tasks``
    snapshot; the ``request_reviewer``, ``escalate_merge_block``,
    ``auto_fix_ci``, and (GitHub #1206) ``address_review`` act phases each
    perform a small latch write (all four act phases now write a one-shot
    latch field, not a status transition — none remain purely read-only under
    their lock). Returns the concatenated ticket_ids each recipe reports as
    acted.
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    clients = load_effective_clients()
    # Compute the repeat-fire burst counts ONCE per tick (#1201), outside every
    # dev_queue_lock() — one read_events replay threaded into all four act
    # phases, mirroring how clients/tasks are loaded once and shared.
    repeat_fire_counts = _detect_repeat_fire_counts(config=config)
    acted: list[str] = []
    acted += _act_address_review(
        _detect_address_review(tasks, clients=clients, config=config),
        clients=clients,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
    acted += _act_auto_fix_ci(
        _detect_auto_fix_ci(tasks, clients=clients, config=config),
        clients=clients,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
    acted += _act_request_reviewer(
        _detect_request_reviewer(tasks, clients=clients, config=config),
        clients=clients,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
    acted += _act_escalate_merge_block(
        _detect_escalate_merge_block(tasks, clients=clients, config=config),
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
    return acted
