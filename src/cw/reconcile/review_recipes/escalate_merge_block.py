"""The escalate_merge_block recipe: detect + act for merge_blocked PRs (RFC 0010 P4).

Package split (#1315, part 2 of 2). This module owns the ``escalate_merge_block``
recipe — it reacts to a PR whose ``pr_state`` carries
``attention_state == "merge_blocked"`` by firing one durable escalation per
merge-blocked episode. The event IS the escalation — there is no dispatch or
spawn — so this module needs no deferred-job NamedTuple.

``_detect_escalate_merge_block`` produces a :class:`ReviewRecipeCandidate` per
merge_blocked row (write-free). ``_act_escalate_merge_block`` re-validates each
candidate under ``dev_queue_lock()``, emits ``PR_ACTION_TAKEN``, and stamps the
one-shot ``escalate_merge_block_fired_at`` latch (GitHub #1206) so the escalation
fires exactly once per episode.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_MERGE_BLOCKED,
    RECIPE_ESCALATE_MERGE_BLOCK,
    ReviewRecipeCandidate,
    _clear_ended_episodes,
    _detect_by_attention_state,
    _find_review_task,
    _record_pr_action_taken,
    _review_payload_base,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask


# RFC 0010 P4 (#1099) — escalate_merge_block payload key. Consumed by exactly one
# recipe's act phase (resident in this module), so it lives here rather than in
# _shared.py.
_PAYLOAD_KEY_MERGE_STATE_STATUS = "merge_state_status"


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
