"""Daemon-side review recipes: address-review candidate detection (RFC 0010).

Review recipes are the opt-in automation layer that reacts to a PR whose review
came back ``changes_requested`` by dispatching an ``/address-review`` session to
mechanically work the requested changes — the review-feedback analogue of the
gate recipes (``cw.reconcile.gate_recipes``), which advance an *approval* gate.

**P1 scope (GitHub #1096, this ticket):** detect-only. The pure
``_detect_address_review`` classification phase produces
:class:`ReviewRecipeCandidate`s for every dev-queue row whose ``pr_state``
carries ``attention_state == "changes_requested"``; it performs NO act /
dispatch / event emission / state mutation. The act phase — spawning the
``/address-review`` session and emitting its audit event — is deferred to a
future ticket (P2).

Like the gate recipes, this module gates on its own opt-in master switch
(``OrchestratorConfig.review_recipes_enabled``, default False). Because P1 has
no act phase, ``True`` is inert by construction until P2 ships. The switch is
checked in BOTH ``run_review_recipes`` and ``_detect_address_review`` (dual
gating), mirroring ``gate_recipes._recipe_gate_open``'s rationale: a caller
invoking ``_detect_address_review`` directly (unit tests) still gets correct
gating without threading the master switch through a separate check.

Candidate selection reuses ``cw.pr_hydrate._is_candidate`` — the same
"hydratable PR" predicate the poll pass uses (non-null ``pr_url``, non-terminal
``pr_state``) — so a review recipe never fires on a MERGED/CLOSED PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.config import load_effective_clients
from cw.dev_queue import load_dev_queue
from cw.pr_hydrate import _is_candidate

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

# The only recipe recognised in P1: react to a changes_requested review by
# dispatching /address-review. Named as a constant so the detect phase and any
# future act phase can't drift via a typo'd string literal.
RECIPE_ADDRESS_REVIEW = "address_review"

# The single attention_state the address-review recipe fires on. A row whose
# PR is at any other attention state (or None, e.g. a draft) is never a
# candidate. See cw.pr_hydrate._compute_attention_state, Row 3.
_ATTENTION_CHANGES_REQUESTED = "changes_requested"

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


def run_review_recipes(*, config: OrchestratorConfig) -> list[ReviewRecipeCandidate]:
    """Run all enabled review recipes for one reconcile tick (P1: detect-only).

    No-op (returns ``[]`` immediately) unless ``config.review_recipes_enabled``
    is True. Loads a fresh dev-queue snapshot itself rather than accepting one
    from the caller — by the wiring point in ``_reconcile_locked`` several prior
    sweeps have already mutated and saved the queue, so a caller-supplied
    snapshot would be stale (mirrors ``run_gate_recipes``). No ``load_state()``
    call: candidate ``client``/``lane`` come straight off the task, so no
    ``CwState`` lookup is needed. No ``now`` parameter: P1 has no act phase to
    forward it to.

    Per-lane enablement (RFC 0010 P3) is resolved against effective clients —
    ``load_effective_clients`` so lane pause/override state is honoured, matching
    ``run_gate_recipes``.

    Returns the detected candidates (P1 performs no act phase, so nothing is
    dispatched or mutated).
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    clients = load_effective_clients()
    return _detect_address_review(tasks, clients=clients, config=config)
