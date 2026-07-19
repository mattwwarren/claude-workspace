"""The request_reviewer recipe: detect + act for no_reviewer PRs (RFC 0010 P4).

Package split (#1315, part 2 of 2). This module owns the ``request_reviewer``
recipe — it reacts to a PR whose ``pr_state`` carries
``attention_state == "no_reviewer"`` by resolving the repo's ``review_strategy``
and requesting the named reviewer via a deferred ``gh`` call.

``_detect_request_reviewer`` produces a :class:`ReviewRecipeCandidate` per
no_reviewer row (write-free; no strategy read in detect — that lives in the act
phase). ``_act_request_reviewer`` re-validates each candidate under
``dev_queue_lock()``, resolves the strategy, emits ``PR_ACTION_TAKEN`` (durably,
BEFORE the gh call), stamps the one-shot ``request_reviewer_fired_at`` latch
(GitHub #1206), and — strictly after the lock releases — requests the reviewer. A
``ci`` mode is a silent policy skip; an unresolvable client / missing handle /
absent pr_url / failed gh call emits ``PR_ACTION_FAILED`` instead.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_NO_REVIEWER,
    _PAYLOAD_KEY_REVIEW_DECISION,
    RECIPE_REQUEST_REVIEWER,
    ReviewRecipeCandidate,
    _clear_ended_episodes,
    _detect_by_attention_state,
    _find_review_task,
    _record_pr_action_taken,
    _review_payload_base,
    _skip_with_anomaly,
)
from cw.review_strategy import MODE_CI, resolve_review_strategy
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask


# RFC 0010 P4 (#1099) — request_reviewer payload keys. Each is consumed by
# exactly one recipe's act phase (resident in this module), so they live here
# rather than in _shared.py.
_PAYLOAD_KEY_REVIEW_STRATEGY_MODE = "review_strategy_mode"
_PAYLOAD_KEY_REVIEWER_HANDLE = "reviewer_handle"


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
