"""Shared cross-recipe infrastructure for the review-recipe layer (RFC 0010).

Package split (#1315, part 1 of 2). This module holds the symbols consumed by
two or more recipe modules (``address_review`` / ``auto_fix_ci`` /
``request_reviewer`` / ``escalate_merge_block``): the recipe-name and
attention-state constants, the shared PR_ACTION payload keys, the three
recipe-indexed aggregator dicts, the pure ``_detect_by_attention_state``
classifier, and the shared act-phase helpers (``_find_review_task``,
``_emit_pr_action_failed``, ``_skip_with_anomaly``, ``_guard_cross_repo_mismatch``,
``_review_payload_base``, ``_record_pr_action_taken``, ``_clear_ended_episodes``).
Recipe modules import from here; this module never imports from a recipe module,
so the package's import graph stays acyclic.

Like the gate recipes, this layer gates on its own opt-in master switch
(``OrchestratorConfig.review_recipes_enabled``, default False), checked in BOTH
``run_review_recipes`` and ``_detect_by_attention_state`` (dual gating), mirroring
``gate_recipes._recipe_gate_open``'s rationale: a caller invoking a ``_detect_*``
wrapper directly (unit tests) still gets correct gating without threading the
master switch through a separate check.

Candidate selection reuses ``cw.pr_hydrate._is_candidate`` — the same
"hydratable PR" predicate the poll pass uses (non-null ``pr_url``, non-terminal
``pr_state``) — so a review recipe never fires on a MERGED/CLOSED PR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.dev_queue import _newest_by_created_at
from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.pr_hydrate import PrAttentionState, _is_candidate
from cw.reconcile._shared import _PAUSED_STATUS_KEY

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
        WatchedPr,
    )


_log = logging.getLogger("cw.reconcile.review_recipes")

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
# Only four of PrAttentionState's five members have a recipe here --
# ready_to_approve is a clean PR with nothing to auto-fix, so it has no
# _ATTENTION_* constant (verified: zero `ready_to_approve` hits under
# review_recipes/).
_ATTENTION_CHANGES_REQUESTED: PrAttentionState = (
    "changes_requested"  # Row 3 -> address_review
)
_ATTENTION_CI_FAILING: PrAttentionState = "ci_failing"  # Row 2 -> auto_fix_ci
_ATTENTION_NO_REVIEWER: PrAttentionState = "no_reviewer"  # Row 4 -> request_reviewer
_ATTENTION_MERGE_BLOCKED: PrAttentionState = (
    "merge_blocked"  # Row 1 -> escalate_merge_block
)

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
# RFC 0010 anomaly layer (#1201) — repeat-fire burst SESSION_NEEDS_ATTENTION
# payload keys; the "paused_status" key itself reuses cw.reconcile._shared's
# _PAUSED_STATUS_KEY (the producer/consumer-shared constant every other
# recipe in this package already imports) rather than redeclaring it here.
_PAYLOAD_KEY_REPEAT_FIRE_COUNT = "repeat_fire_count"
_PAYLOAD_KEY_REPEAT_FIRE_WINDOW_MINUTES = "window_minutes"
_REPEAT_FIRE_ATTENTION_REASON = "review_recipe_repeat_fire"

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

# The attention_state each recipe fires on, keyed by recipe name (1:1). The
# canonical map for any consumer that needs to iterate every recipe against its
# trigger state — e.g. the liveness doctor check (#1201). Mirrors the four
# _detect_* wrappers' (attention_state, recipe) pairs without re-deriving them.
RECIPE_ATTENTION_STATES: dict[str, str] = {
    RECIPE_ADDRESS_REVIEW: _ATTENTION_CHANGES_REQUESTED,
    RECIPE_AUTO_FIX_CI: _ATTENTION_CI_FAILING,
    RECIPE_REQUEST_REVIEWER: _ATTENTION_NO_REVIEWER,
    RECIPE_ESCALATE_MERGE_BLOCK: _ATTENTION_MERGE_BLOCKED,
}

# The one-shot ``<recipe>_fired_at`` latch accessor for each recipe, keyed by
# recipe name (#1201). A non-None value is an already-persisted proxy for "this
# recipe fired within the row's current attention_state episode" — the liveness
# check reads it directly instead of replaying events. Promotes the four inline
# lambdas the act phases pass to ``_clear_ended_episodes``; those call sites are
# left untouched (this is a pure additive constant).
RECIPE_FIRED_AT_GETTERS: dict[str, Callable[[TicketTask], datetime | None]] = {
    RECIPE_ADDRESS_REVIEW: lambda t: t.address_review_fired_at,
    RECIPE_AUTO_FIX_CI: lambda t: t.auto_fix_ci_fired_at,
    RECIPE_REQUEST_REVIEWER: lambda t: t.request_reviewer_fired_at,
    RECIPE_ESCALATE_MERGE_BLOCK: lambda t: t.escalate_merge_block_fired_at,
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


def _guard_cross_repo_mismatch(
    task: TicketTask,
    payload_base: dict[str, object],
    *,
    pr_repo: str,
    client_repo: str,
    location: str,
) -> bool:
    """Shared cross-repo dispatch guard body for both recipes (GitHub #1198).

    ``location`` names what ``client_repo`` was resolved from (e.g. "worktree
    origin" or "client workspace origin") for the anomaly error message.
    Returns ``True`` to proceed (override logged) or ``False`` to skip (anomaly
    + ``PR_ACTION_FAILED`` already emitted — caller returns ``None``).
    """
    if task.cross_repo_override:
        _log.warning(
            "review_recipe_repo_mismatch_override ticket=%s pr_repo=%s client_repo=%s",
            task.ticket_id,
            pr_repo,
            client_repo,
        )
        return True
    _skip_with_anomaly(
        payload_base,
        error=(
            f"repo mismatch: pr_url repo {pr_repo!r} != {location} repo {client_repo!r}"
        ),
        ticket_id=task.ticket_id,
    )
    return False


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


def _record_pr_action_taken(
    payload_base: dict[str, object],
    client: str,
    ticket_id: str,
    recipe: str,
    *,
    config: OrchestratorConfig | None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None,
    lane: str,
) -> None:
    """Record PR_ACTION_TAKEN, escalating once on the exact repeat-fire crossing.

    The PR_ACTION_TAKEN event is ALWAYS recorded (unchanged behaviour). When both
    *config* and *repeat_fire_counts* are supplied (the ``run_review_recipes``
    path), the prior in-window count for this ``(client, ticket_id, recipe)``
    plus this fire is compared to ``config.review_recipe_repeat_fire_threshold``:
    a single ``SESSION_NEEDS_ATTENTION`` (``paused_status=review_recipe_repeat_fire``)
    is emitted exactly when the post-increment count EQUALS the threshold — no
    re-fire once past it (each burst crosses the boundary once). A direct
    ``_act_*`` unit-test call passing ``config=None``/``repeat_fire_counts=None``
    records the action with no burst check.
    """
    record_event(
        OrchestratorEventType.PR_ACTION_TAKEN,
        payload_base,
        correlation_id=ticket_id,
    )
    if config is None or repeat_fire_counts is None:
        return
    new_count = repeat_fire_counts.get((client, ticket_id, recipe), 0) + 1
    if new_count != config.review_recipe_repeat_fire_threshold:
        return
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            _PAYLOAD_KEY_CLIENT: client,
            _PAYLOAD_KEY_TICKET_ID: ticket_id,
            _PAYLOAD_KEY_RECIPE: recipe,
            _PAYLOAD_KEY_SESSION_ID: payload_base.get(_PAYLOAD_KEY_SESSION_ID),
            _PAUSED_STATUS_KEY: _REPEAT_FIRE_ATTENTION_REASON,
            _PAYLOAD_KEY_REPEAT_FIRE_COUNT: new_count,
            _PAYLOAD_KEY_REPEAT_FIRE_WINDOW_MINUTES: (
                config.review_recipe_repeat_fire_window_minutes
            ),
            _PAYLOAD_KEY_LANE: lane,
        },
        correlation_id=ticket_id,
    )


def _clear_ended_episodes(
    store: DevQueueStore,
    *,
    attention_state: str,
    get_fired_at: Callable[[TicketTask], datetime | None],
    clear_fired_at: Callable[[TicketTask], None],
) -> bool:
    """Clear a one-shot latch on rows whose *attention_state* episode ended.

    Shared by every review-recipe latch sweep (auto_fix_ci, request_reviewer,
    escalate_merge_block — the third instance triggered this extraction, GitHub
    #1205). A latch is cleared when it is set (not None) but the row's current
    pr_state is None or no longer at *attention_state* — the episode that
    licensed the fire has ended, re-arming the latch for a genuine future
    re-entry. Typed accessor callables (not a string field name) keep every
    read/clear access mypy-checked — TicketTask has no
    extra=forbid/validate_assignment to catch a wrong-but-existing field name
    at runtime. Returns whether any row changed (dirty flag for the caller's
    conditional save_dev_queue).
    """
    changed = False
    for task in store.tasks:
        if get_fired_at(task) is None:
            continue
        pr_state = task.pr_state
        if pr_state is None or pr_state.attention_state != attention_state:
            clear_fired_at(task)
            changed = True
    return changed
