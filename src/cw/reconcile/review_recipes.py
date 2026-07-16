"""Daemon-side review recipes: address-review candidate detection (RFC 0010).

Review recipes are the opt-in automation layer that reacts to a PR whose review
came back ``changes_requested`` by dispatching an ``/address-review`` session to
mechanically work the requested changes — the review-feedback analogue of the
gate recipes (``cw.reconcile.gate_recipes``), which advance an *approval* gate.

**P1 scope (GitHub #1096):** detect-only. The pure ``_detect_address_review``
classification phase produces :class:`ReviewRecipeCandidate`s for every
dev-queue row whose ``pr_state`` carries
``attention_state == "changes_requested"``; it performs no writes.

**P2 scope (GitHub #1097, this ticket):** the act phase. ``_act_address_review``
re-validates each candidate under ``dev_queue_lock()`` (a consistent READ only —
NO dev-queue mutation, per Resolution 6), emits
:class:`OrchestratorEventType.PR_ACTION_TAKEN` (durably, BEFORE the spawn), and
then — strictly after the lock releases — dispatches an ``/address-review``
session via ``spawn_create_impl``. A dispatch ``CwError`` or a precondition
anomaly (unparseable PR url, unresolvable client, missing worktree) emits
:class:`OrchestratorEventType.PR_ACTION_FAILED` instead. Emit-before-dispatch is
structural: the event fires inside the lock, every spawn strictly afterward.

Like the gate recipes, this module gates on its own opt-in master switch
(``OrchestratorConfig.review_recipes_enabled``, default False). The switch is
checked in BOTH ``run_review_recipes`` and ``_detect_address_review`` (dual
gating), mirroring ``gate_recipes._recipe_gate_open``'s rationale: a caller
invoking ``_detect_address_review`` directly (unit tests) still gets correct
gating without threading the master switch through a separate check.

Candidate selection reuses ``cw.pr_hydrate._is_candidate`` — the same
"hydratable PR" predicate the poll pass uses (non-null ``pr_url``, non-terminal
``pr_state``) — so a review recipe never fires on a MERGED/CLOSED PR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from cw.config import load_effective_clients
from cw.dev_queue import (
    _newest_by_created_at,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.pr_hydrate import _is_candidate, _parse_pr_url
from cw.review_strategy import MODE_CI, resolve_review_strategy

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
        WatchedPr,
    )


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


_log = logging.getLogger(__name__)

# Recipe-name constants — one per attention state the review-recipe layer acts
# on. Named so detect/act phases and the models.py recognized-key set can't
# drift via a typo'd string literal. RFC 0010 P1 shipped address_review; P4
# (#1099) adds the other three.
RECIPE_ADDRESS_REVIEW = "address_review"
RECIPE_AUTO_FIX_CI = "auto_fix_ci"
RECIPE_REQUEST_REVIEWER = "request_reviewer"
RECIPE_ESCALATE_MERGE_BLOCK = "escalate_merge_block"

# The attention_state each recipe fires on (1:1 with a recipe). A row whose PR
# is at any other attention state (or None, e.g. a draft) is never a candidate
# for that recipe. See cw.pr_hydrate._compute_attention_state's decision table.
_ATTENTION_CHANGES_REQUESTED = "changes_requested"  # Row 3 -> address_review
_ATTENTION_CI_FAILING = "ci_failing"  # Row 2 -> auto_fix_ci
_ATTENTION_NO_REVIEWER = "no_reviewer"  # Row 4 -> request_reviewer
_ATTENTION_MERGE_BLOCKED = "merge_blocked"  # Row 1 -> escalate_merge_block

# PR_ACTION_TAKEN / PR_ACTION_FAILED payload keys (RFC 0010 P2) — named once so
# the producer (_act_address_review) and the docs/consumers can't drift via a
# typo'd string literal at one site only (mirrors gate_recipes' _SNAPSHOT_KEY_*).
_PAYLOAD_KEY_CLIENT = "client"
_PAYLOAD_KEY_LANE = "lane"
_PAYLOAD_KEY_RECIPE = "recipe"
_PAYLOAD_KEY_TICKET_ID = "ticket_id"
_PAYLOAD_KEY_PR_URL = "pr_url"
_PAYLOAD_KEY_ATTENTION_STATE = "attention_state"
_PAYLOAD_KEY_SESSION_ID = "session_id"
_PAYLOAD_KEY_EVIDENCE_SNAPSHOT = "evidence_snapshot"
_PAYLOAD_KEY_ERROR = "error"
_PAYLOAD_KEY_REVIEW_DECISION = "review_decision"
# RFC 0010 P4 (#1099) — request_reviewer / escalate_merge_block payload keys.
_PAYLOAD_KEY_REVIEW_STRATEGY_MODE = "review_strategy_mode"
_PAYLOAD_KEY_REVIEWER_HANDLE = "reviewer_handle"
_PAYLOAD_KEY_MERGE_STATE_STATUS = "merge_state_status"
_PAYLOAD_KEY_FAILING_CHECKS = "failing_checks"

# RFC 0010 P3 (#1098) — tier-3 hardcoded fallback for the per-lane resolver.
# Default OFF (mirrors gate_recipes._DEFAULT_GATE_RECIPE_ENABLED): a review
# recipe dispatches an /address-review session with no human in the loop, so
# nothing fires unless an operator opts a lane (or ticket) in. NOT a config
# field — it is the floor the ticket/lane tiers fall through to. Only the P1
# recipe (RECIPE_ADDRESS_REVIEW) exists; no placeholders for unimplemented
# recipe names.
_DEFAULT_REVIEW_RECIPE_ENABLED: dict[str, bool] = {
    RECIPE_ADDRESS_REVIEW: False,
    RECIPE_AUTO_FIX_CI: False,
    RECIPE_REQUEST_REVIEWER: False,
    RECIPE_ESCALATE_MERGE_BLOCK: False,
}


def resolve_review_recipe_enabled(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    recipe_name: str,
) -> bool:
    """Return whether *recipe_name* is enabled for *task*, per RFC 0010 P3.

    3-tier precedence, highest first (mirrors
    gate_recipes.resolve_gate_recipe_enabled exactly):

    1. ``task.review_recipes`` — ticket-level override wins when it names the
       recipe.
    2. ``LaneConfig.review_recipes`` on the task's lane — the per-lane map.
    3. ``_DEFAULT_REVIEW_RECIPE_ENABLED`` — the hardcoded default-off floor.

    Robust to a missing client (absent from *clients*) or a missing lane
    (absent from the client's ``effective_lanes``): either falls straight
    through to the default with no exception.
    """
    if task.review_recipes is not None and recipe_name in task.review_recipes:
        return task.review_recipes[recipe_name]
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if (
                lane_cfg.name == task.lane
                and lane_cfg.review_recipes is not None
                and recipe_name in lane_cfg.review_recipes
            ):
                return lane_cfg.review_recipes[recipe_name]
    # .get(..., False): a recipe_name outside _DEFAULT_REVIEW_RECIPE_ENABLED
    # falls through to the safe default instead of raising KeyError, matching
    # this function's documented no-exception robustness guarantee for every
    # other unresolved input.
    return _DEFAULT_REVIEW_RECIPE_ENABLED.get(recipe_name, False)


def resolve_outbound_consent_allowed(
    pr_url: str,
    *,
    config: OrchestratorConfig,
    watched_prs: list[WatchedPr],
) -> bool:
    """Two-party consent gate for outbound acting toward another's PR.

    RFC 0011 B2, #1159.

    Party 1 (operator): ``config.review_recipes_enabled`` — the existing
    review-recipes master switch (RFC 0010 P3), reused as a flat bool only;
    this function does NOT thread a TicketTask/LaneConfig through
    ``resolve_review_recipe_enabled`` (that 3-tier resolver is a distinct,
    ticket-scoped concept — see its docstring at :135).

    Party 2 (target): an active ``WatchedPr`` for *pr_url* — every persisted
    ``WatchedPr`` already passed ``resolve_and_register_review_request``'s
    individual-vs-team filter (RFC 0011 S2 R5/R7, ``cw.pr_hydrate:363``)
    before being inserted, so an existing ``status == "active"`` match IS
    the channel-opening action; no further individual-vs-team re-derivation
    happens here.

    Reads only — never creates, mutates, or dismisses a ``WatchedPr``, so
    "no path initiates outbound" holds definitionally: this function cannot
    be the origin of a channel, only a check against one that already
    exists.

    Own-authored PRs are out of scope for this predicate entirely — the
    existing TicketTask-typed act phase (``_act_address_review`` et al.)
    does not call this function, so those PRs bypass the gate by
    non-modification (RFC 0011 B2 Acceptance; regression coverage is the
    existing test_reconcile_review_recipes.py suite passing unmodified).

    No re-review re-engagement detection (RFC 0011 B3, #1163, Wave 2) — this
    checks ``status == "active"`` existence only.
    """
    if not config.review_recipes_enabled:
        return False
    return any(
        watched.pr_url == pr_url and watched.status == "active"
        for watched in watched_prs
    )


@dataclass(frozen=True)
class ReviewRecipeCandidate:
    """Classification result from a review recipe's detect phase (RFC 0010 P1).

    Shape mirrors :class:`cw.reconcile.gate_recipes.GateRecipeCandidate` (ticket
    identity + lane + recipe + evidence + session) with two review-specific
    fields: ``attention_state`` (the derived PR signal that licensed the fire)
    and ``pr_url`` (the target the future act phase dispatches against).
    ``session_id`` is Optional — a changes_requested PR whose owning session has
    already exited is still a valid candidate, so detection must not require a
    live session.
    """

    ticket_id: str
    client: str
    lane: str
    recipe: str
    attention_state: str
    pr_url: str
    evidence: dict[str, object]
    session_id: str | None


def _detect_by_attention_state(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    attention_state: str,
    recipe: str,
) -> list[ReviewRecipeCandidate]:
    """Shared pure classification phase for every review recipe. Zero writes.

    A candidate is produced for every task that is a hydration candidate
    (``_is_candidate``: non-null ``pr_url``, non-terminal ``pr_state``) whose
    ``pr_state.attention_state`` equals *attention_state* AND for which *recipe*
    is enabled under the 3-tier per-lane/per-ticket precedence
    (``resolve_review_recipe_enabled``, RFC 0010 P3). No task-status filter — a
    PR at the recipe's attention state warrants action regardless of the row's
    queue status. Gates on ``config.review_recipes_enabled`` as its first line
    (dual gating — a direct caller still gets correct gating); the per-task
    enablement check sits inside the loop because it is per-task, not global.
    Each of the four ``_detect_*`` recipes is a one-line wrapper over this,
    swapping only the ``(attention_state, recipe)`` pair — the routing is 1:1,
    so a single row can never match two recipes (see the routing test).
    """
    if not config.review_recipes_enabled:
        return []
    candidates: list[ReviewRecipeCandidate] = []
    for task in tasks:
        if not _is_candidate(task):
            continue
        if task.pr_state is None:
            continue
        if task.pr_state.attention_state != attention_state:
            continue
        if not resolve_review_recipe_enabled(task, clients, recipe):
            continue
        pr_url = task.pr_url
        if pr_url is None:  # pragma: no cover - _is_candidate guarantees non-null
            continue
        candidates.append(
            ReviewRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=recipe,
                attention_state=task.pr_state.attention_state,
                pr_url=pr_url,
                evidence={"review_decision": task.pr_state.review_decision},
                session_id=task.session_id,
            )
        )
    return candidates


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


def _find_review_task(
    store: DevQueueStore, ticket_id: str, client: str
) -> TicketTask | None:
    """Resolve the (ticket_id, client) row this recipe acts on — no status filter.

    Unlike ``gate_recipes._find_blocked_task``, review recipes are NOT
    status-gated: a ``changes_requested`` PR warrants an address-review dispatch
    regardless of the row's queue status (mirrors ``_detect_address_review``,
    which applies no status filter — a status-filtering finder would silently
    drop valid candidates). Reuses ``dev_queue._newest_by_created_at`` for the
    duplicate-row tie-break (same import ``gate_recipes`` uses). Returns ``None``
    (a silent skip for every caller) when the row has vanished between detect
    and act.
    """
    matches = [
        t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
    ]
    if not matches:
        return None
    return _newest_by_created_at(matches)


def _emit_pr_action_failed(
    payload_base: dict[str, object], *, error: str, ticket_id: str
) -> None:
    """Record a durable, operator-forwarded PR_ACTION_FAILED correction."""
    record_event(
        OrchestratorEventType.PR_ACTION_FAILED,
        {**payload_base, _PAYLOAD_KEY_ERROR: error},
        correlation_id=ticket_id,
    )


def _skip_with_anomaly(
    payload_base: dict[str, object], *, error: str, ticket_id: str
) -> None:
    """Log + record PR_ACTION_FAILED for a _prepare_dispatch_job precondition
    anomaly (unparseable pr_url, unresolvable client, missing worktree) — the
    shared log-then-emit pairing so the three anomaly branches can't drift.
    """
    _log.warning("review_recipe_action_failed ticket=%s: %s", ticket_id, error)
    _emit_pr_action_failed(payload_base, error=error, ticket_id=ticket_id)


def _review_payload_base(
    task: TicketTask,
    session_id: str | None,
    recipe: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    """Build the 8-key PR_ACTION_TAKEN/FAILED payload for one review-recipe row.

    Shared by every recipe's act phase so the payload shape can't drift between
    producers. ``attention_state`` is read off the re-loaded ``pr_state`` (the
    caller has already confirmed it is non-None); ``evidence`` is the
    recipe-specific evidence snapshot (review_decision, failing_checks, etc.).
    """
    return {
        _PAYLOAD_KEY_CLIENT: task.client,
        _PAYLOAD_KEY_LANE: task.lane,
        _PAYLOAD_KEY_RECIPE: recipe,
        _PAYLOAD_KEY_TICKET_ID: task.ticket_id,
        _PAYLOAD_KEY_PR_URL: task.pr_url,
        _PAYLOAD_KEY_ATTENTION_STATE: (
            task.pr_state.attention_state if task.pr_state is not None else None
        ),
        _PAYLOAD_KEY_SESSION_ID: session_id,
        _PAYLOAD_KEY_EVIDENCE_SNAPSHOT: evidence,
    }


def _prepare_dispatch_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
) -> _DispatchJob | None:
    """Re-validate a re-loaded row under the lock; emit + build its dispatch job.

    Returns ``None`` to skip. Two skip flavours:

    * **Silent** (no event) when the row is stale — ``pr_state`` gone or no
      longer ``changes_requested`` (a concurrent re-review can have moved it on
      between detect and act).
    * **Anomaly** (emits ``PR_ACTION_FAILED`` + a warning) when the PR url is
      unparseable/absent, the client is unresolvable, or the worktree is
      missing — a fail-safe correction, never a silent drop.

    Otherwise records ``PR_ACTION_TAKEN`` (emit-before-dispatch) from the
    RE-LOADED row and returns the deferred dispatch job. ``session_id`` is the
    originating candidate's — the event fires before the new spawn exists, so it
    can't carry a session id that doesn't exist yet.
    """
    pr_state = task.pr_state
    if pr_state is None or pr_state.attention_state != _ATTENTION_CHANGES_REQUESTED:
        return None  # stale — silent skip (precedent: gate_recipes silent paths)
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
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        payload_base,
        correlation_id=task.ticket_id,
    )
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


def _act_address_review(
    candidates: list[ReviewRecipeCandidate], *, clients: dict[str, ClientConfig]
) -> list[str]:
    """Act phase: re-validate under lock, emit PR_ACTION_TAKEN, then dispatch.

    Mirrors ``gate_recipes._act_auto_approve_review``'s shape (lock / re-load /
    re-check / emit-before-action / deferred side-effect after lock release) but
    performs NO dev-queue mutation (RFC 0010 P2 Resolution 6): the
    ``dev_queue_lock()`` is held only for a consistent READ re-validation. Each
    candidate's row is re-loaded fresh under the lock and re-checked — a
    concurrent re-review can have moved the PR off ``changes_requested`` between
    detect and act. Every ``spawn_create_impl`` runs strictly after the lock
    releases, so the dispatch never nests the flock (guaranteeing no
    self-deadlock) and ``PR_ACTION_TAKEN`` is always durable before its spawn.

    ``clients`` is the caller's snapshot (mirrors ``run_gate_recipes`` loading
    ``load_effective_clients()`` once and threading it down) rather than a
    second ``load_effective_clients()`` read taken inside the lock — clients.yaml
    has no locking relationship to the dev-queue store, so re-reading it there
    only extends the flock's hold time for no consistency benefit.

    Returns the acted ticket_ids (those whose ``/address-review`` dispatch
    succeeded).
    """
    if not candidates:
        return []
    # Keyed on (ticket_id, client): ticket_id alone is a per-repo GitHub issue
    # number, not globally unique across this multi-tenant system's clients.
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    dispatch_jobs: list[_DispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_dispatch_job(task, candidate.session_id, clients)
            if job is not None:
                dispatch_jobs.append(job)
    acted: list[str] = []
    for job in dispatch_jobs:
        ticket_id = _dispatch_address_review(job)
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted


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


def _prepare_auto_fix_ci_job(
    task: TicketTask, session_id: str | None
) -> _RedispatchJob | None:
    """Re-validate a re-loaded row under the lock; emit + build its re-dispatch.

    Silent skip (no event) when the row is stale — ``pr_state`` gone or no
    longer ``ci_failing`` (a concurrent re-run can have moved it on between
    detect and act). Otherwise records ``PR_ACTION_TAKEN`` (emit-before-dispatch)
    and returns the deferred re-dispatch job.
    """
    pr_state = task.pr_state
    if pr_state is None or pr_state.attention_state != _ATTENTION_CI_FAILING:
        return None
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_AUTO_FIX_CI,
        {_PAYLOAD_KEY_FAILING_CHECKS: pr_state.failing_checks},
    )
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        payload_base,
        correlation_id=task.ticket_id,
    )
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


def _act_auto_fix_ci(candidates: list[ReviewRecipeCandidate]) -> list[str]:
    """Act phase for auto_fix_ci: re-validate under lock, emit, then re-dispatch.

    Mirrors ``_act_address_review``'s lock/re-load/emit-before-action/deferred-
    side-effect shape. The re-enqueue + dispatch tick runs strictly after the
    lock releases (``add_ticket`` re-acquires ``dev_queue_lock``, so nesting
    would self-deadlock). Returns the ticket_ids whose re-dispatch succeeded.
    """
    if not candidates:
        return []
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_RedispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_auto_fix_ci_job(task, candidate.session_id)
            if job is not None:
                jobs.append(job)
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


def _prepare_request_reviewer_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
    now: datetime,
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
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        payload_base,
        correlation_id=task.ticket_id,
    )
    task.request_reviewer_fired_at = now
    return _ReviewerJob(
        pr_url=task.pr_url,
        handle=strategy.handle,
        ticket_id=task.ticket_id,
        payload_base=payload_base,
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

    result = add_pr_reviewer(job.pr_url, job.handle)
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


def _act_request_reviewer(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    now: datetime | None = None,
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

    Stamping/clearing the latch IS a dev-queue write (the one exception to the
    "no dev-queue mutation" rule — a latch field, not a status transition),
    saved before the lock releases. The ``add_pr_reviewer`` gh call runs
    strictly after the lock releases. Returns the ticket_ids for which a
    reviewer request actually succeeded (a failed gh call is excluded and
    corrected via ``PR_ACTION_FAILED``).
    """
    from datetime import UTC
    from datetime import datetime as _datetime

    resolved_now = now if now is not None else _datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_ReviewerJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_request_reviewer_episodes(store)
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_request_reviewer_job(
                task, candidate.session_id, clients, resolved_now
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


def _clear_ended_request_reviewer_episodes(store: DevQueueStore) -> bool:
    """Clear the latch on rows whose no-reviewer episode has ended. Returns dirty."""
    changed = False
    for task in store.tasks:
        if task.request_reviewer_fired_at is None:
            continue
        pr_state = task.pr_state
        if pr_state is None or pr_state.attention_state != _ATTENTION_NO_REVIEWER:
            task.request_reviewer_fired_at = None
            changed = True
    return changed


# --- escalate_merge_block act phase (RFC 0010 P4, #1099) -------------------


def _act_escalate_merge_block(
    candidates: list[ReviewRecipeCandidate], *, now: datetime | None = None
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

    Stamping/clearing the latch IS a dev-queue write (the one exception to the
    review layer's "no dev-queue mutation" rule — it is a latch field, not a
    status transition). ``record_event`` nests the inbox lock INSIDE
    ``dev_queue_lock`` (never the reverse), so emitting under the lock is
    deadlock-safe (same ordering as ``cw.reconcile.escalation``).
    """
    from datetime import UTC
    from datetime import datetime as _datetime

    resolved_now = now if now is not None else _datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    acted: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_merge_block_episodes(store)
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            if _fire_escalate_merge_block(task, candidate.session_id, resolved_now):
                acted.append(task.ticket_id)
                changed = True
        if changed:
            save_dev_queue(store)
    return acted


def _clear_ended_merge_block_episodes(store: DevQueueStore) -> bool:
    """Clear the latch on rows whose merge-block episode has ended. Returns dirty."""
    changed = False
    for task in store.tasks:
        if task.escalate_merge_block_fired_at is None:
            continue
        pr_state = task.pr_state
        if pr_state is None or pr_state.attention_state != _ATTENTION_MERGE_BLOCKED:
            task.escalate_merge_block_fired_at = None
            changed = True
    return changed


def _fire_escalate_merge_block(
    task: TicketTask, session_id: str | None, now: datetime
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
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        payload_base,
        correlation_id=task.ticket_id,
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
    snapshot; the ``request_reviewer`` and ``escalate_merge_block`` act phases
    each perform a small latch write (the two exceptions to this module's "no
    dev-queue mutation" rule — a one-shot field, not a status transition),
    every other act phase is read-only under its lock. Returns the
    concatenated ticket_ids each recipe reports as acted.
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    clients = load_effective_clients()
    acted: list[str] = []
    acted += _act_address_review(
        _detect_address_review(tasks, clients=clients, config=config),
        clients=clients,
    )
    acted += _act_auto_fix_ci(
        _detect_auto_fix_ci(tasks, clients=clients, config=config)
    )
    acted += _act_request_reviewer(
        _detect_request_reviewer(tasks, clients=clients, config=config),
        clients=clients,
    )
    acted += _act_escalate_merge_block(
        _detect_escalate_merge_block(tasks, clients=clients, config=config)
    )
    return acted
