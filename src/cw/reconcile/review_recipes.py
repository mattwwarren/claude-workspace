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
from cw.dev_queue import _newest_by_created_at, dev_queue_lock, load_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.pr_hydrate import _is_candidate, _parse_pr_url

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
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

# The only recipe recognised in P1: react to a changes_requested review by
# dispatching /address-review. Named as a constant so the detect phase and any
# future act phase can't drift via a typo'd string literal.
RECIPE_ADDRESS_REVIEW = "address_review"

# The single attention_state the address-review recipe fires on. A row whose
# PR is at any other attention state (or None, e.g. a draft) is never a
# candidate. See cw.pr_hydrate._compute_attention_state, Row 3.
_ATTENTION_CHANGES_REQUESTED = "changes_requested"

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

# RFC 0010 P3 (#1098) — tier-3 hardcoded fallback for the per-lane resolver.
# Default OFF (mirrors gate_recipes._DEFAULT_GATE_RECIPE_ENABLED): a review
# recipe dispatches an /address-review session with no human in the loop, so
# nothing fires unless an operator opts a lane (or ticket) in. NOT a config
# field — it is the floor the ticket/lane tiers fall through to. Only the P1
# recipe (RECIPE_ADDRESS_REVIEW) exists; no placeholders for unimplemented
# recipe names.
_DEFAULT_REVIEW_RECIPE_ENABLED: dict[str, bool] = {
    RECIPE_ADDRESS_REVIEW: False,
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


def _detect_address_review(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Pure classification phase for the address_review recipe. Zero writes.

    A candidate is produced for every task that is a hydration candidate
    (``_is_candidate``: non-null ``pr_url``, non-terminal ``pr_state``) whose
    ``pr_state.attention_state`` is ``changes_requested`` AND for which the
    address-review recipe is enabled under the 3-tier per-lane/per-ticket
    precedence (``resolve_review_recipe_enabled``, RFC 0010 P3). No task-status
    filter: a changes_requested PR warrants an address-review dispatch
    regardless of the row's queue status. Gates on
    ``config.review_recipes_enabled`` as its first line (dual gating — a direct
    caller still gets correct gating); the per-task enablement check sits inside
    the loop because it is per-task, not global.
    """
    if not config.review_recipes_enabled:
        return []
    candidates: list[ReviewRecipeCandidate] = []
    for task in tasks:
        if not _is_candidate(task):
            continue
        if task.pr_state is None:
            continue
        if task.pr_state.attention_state != _ATTENTION_CHANGES_REQUESTED:
            continue
        if not resolve_review_recipe_enabled(task, clients, RECIPE_ADDRESS_REVIEW):
            continue
        pr_url = task.pr_url
        if pr_url is None:  # pragma: no cover - _is_candidate guarantees non-null
            continue
        candidates.append(
            ReviewRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_ADDRESS_REVIEW,
                attention_state=task.pr_state.attention_state,
                pr_url=pr_url,
                evidence={"review_decision": task.pr_state.review_decision},
                session_id=task.session_id,
            )
        )
    return candidates


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
    payload_base: dict[str, object] = {
        _PAYLOAD_KEY_CLIENT: task.client,
        _PAYLOAD_KEY_LANE: task.lane,
        _PAYLOAD_KEY_RECIPE: RECIPE_ADDRESS_REVIEW,
        _PAYLOAD_KEY_TICKET_ID: task.ticket_id,
        _PAYLOAD_KEY_PR_URL: task.pr_url,
        _PAYLOAD_KEY_ATTENTION_STATE: pr_state.attention_state,
        _PAYLOAD_KEY_SESSION_ID: session_id,
        _PAYLOAD_KEY_EVIDENCE_SNAPSHOT: {
            _PAYLOAD_KEY_REVIEW_DECISION: pr_state.review_decision
        },
    }
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

    P2 (#1097) adds the act phase: detected candidates are dispatched via
    ``_act_address_review``, which spawns an ``/address-review`` session per
    still-valid candidate and emits ``PR_ACTION_TAKEN`` / ``PR_ACTION_FAILED``.
    Returns the list of ticket_ids whose ``/address-review`` dispatch succeeded.
    The act phase performs no dev-queue mutation.
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    clients = load_effective_clients()
    return _act_address_review(
        _detect_address_review(tasks, clients=clients, config=config),
        clients=clients,
    )
