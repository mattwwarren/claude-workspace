"""The review-recipes tick entry point + the stateless repeat-fire counter.

Package split (#1315, part 2 of 2). This module holds ``run_review_recipes`` —
the single per-tick detect->act entry point ``cw.reconcile.core`` calls — and the
stateless ``_detect_repeat_fire_counts`` burst counter it consumes once per tick.

``run_review_recipes`` is the package's sole top-level orchestration entry point;
it is the one sanctioned site that imports the sibling recipe modules' public
detect/act pairs (``address_review``, ``auto_fix_ci``, ``request_reviewer``,
``escalate_merge_block``), so the recipe modules themselves stay leaf modules that
only depend on ``_shared``.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.config import load_effective_clients
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.reconcile.review_recipes._shared import (
    _PAYLOAD_KEY_CLIENT,
    _PAYLOAD_KEY_RECIPE,
    _PAYLOAD_KEY_TICKET_ID,
)

# Sanctioned sibling imports — used only by run_review_recipes (the package's
# single top-level tick entry point), never by a leaf recipe helper. See the
# "Orchestrator/tick-infra exception" in the #1315 plan: recipe modules depend
# on _shared only; this one entry-point function is the exception.
from cw.reconcile.review_recipes.address_review import (
    _act_address_review,
    _detect_address_review,
)
from cw.reconcile.review_recipes.auto_fix_ci import (
    _act_auto_fix_ci,
    _detect_auto_fix_ci,
)
from cw.reconcile.review_recipes.escalate_merge_block import (
    _act_escalate_merge_block,
    _detect_escalate_merge_block,
)
from cw.reconcile.review_recipes.request_reviewer import (
    _act_request_reviewer,
    _detect_request_reviewer,
)

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig


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
