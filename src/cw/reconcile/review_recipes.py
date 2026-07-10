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

from cw.dev_queue import load_dev_queue
from cw.pr_hydrate import _is_candidate

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig, TicketTask

# The only recipe recognised in P1: react to a changes_requested review by
# dispatching /address-review. Named as a constant so the detect phase and any
# future act phase can't drift via a typo'd string literal.
RECIPE_ADDRESS_REVIEW = "address_review"

# The single attention_state the address-review recipe fires on. A row whose
# PR is at any other attention state (or None, e.g. a draft) is never a
# candidate. See cw.pr_hydrate._compute_attention_state, Row 3.
_ATTENTION_CHANGES_REQUESTED = "changes_requested"


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
    tasks: list[TicketTask], *, config: OrchestratorConfig
) -> list[ReviewRecipeCandidate]:
    """Pure classification phase for the address_review recipe. Zero writes.

    A candidate is produced for every task that is a hydration candidate
    (``_is_candidate``: non-null ``pr_url``, non-terminal ``pr_state``) whose
    ``pr_state.attention_state`` is ``changes_requested``. No task-status filter:
    a changes_requested PR warrants an address-review dispatch regardless of the
    row's queue status. Gates on ``config.review_recipes_enabled`` as its first
    line (dual gating — a direct caller still gets correct gating).
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

    Returns the detected candidates (P1 performs no act phase, so nothing is
    dispatched or mutated).
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    return _detect_address_review(tasks, config=config)
