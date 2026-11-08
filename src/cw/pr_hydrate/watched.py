"""Counterparty axis, review-request registration and the hydration throttle.

Part of the ``cw.pr_hydrate`` package (#2999). Logs as ``cw.pr_hydrate``,
the pre-split logger name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from cw.dev_queue import register_watched_pr
from cw.models import WatchedPr

if TYPE_CHECKING:
    from cw.models import TicketTask

logger = logging.getLogger("cw.pr_hydrate")

# RFC 0011 S1 D-S1 — the counterparty axis for a hold's PR: "self" (the
# operator's own work) or "external" (someone else's). Mirrors
# cw.review_strategy.ReviewStrategyMode's shape: a bare module-level Literal
# alias living in the module that owns its derivation function.
Counterparty = Literal["self", "external"]

# RFC 0011 S2 — a watched PR is, by construction, someone else's review
# request, so its counterparty axis is always "external". A parallel module
# constant to the ``derive_counterparty`` function (which stays TicketTask-typed
# and only produces "self" this slice, #1154); this is the "external" producer
# for the watched-PR path. Never persisted on the record — the axis is derivable
# from "it is a WatchedPr" and carrying a redundant field would let the two
# drift.
WATCHED_PR_COUNTERPARTY: Counterparty = "external"


def derive_counterparty(
    task: TicketTask | None, *, operator_login: str | None
) -> Counterparty:
    """Return the counterparty axis (RFC 0011 S1 D-S1) for *task*'s PR.

    ``task is None`` (nothing dispatched yet) and a PR-less task
    (``pr_url is None``) both resolve "self" for the same reason: there is
    no other party's PR to be "external" to. *operator_login* is accepted
    for observability (logged below) and to keep the call site's intent
    explicit; it does not yet affect the branch outcome.
    """
    logger.debug(
        "derive_counterparty: task=%s operator_login=%s",
        task.ticket_id if task is not None else None,
        operator_login,
    )
    # Why: D-S1/D-S2a scope this ticket to always resolving "self" — no PR
    # authored by anyone other than the auto-dev worker (itself always the
    # operator's own gh identity, per _is_candidate/_PR_VIEW_FIELDS above) is
    # reachable through today's candidate-selection pass. "external" has no
    # producer yet; wiring a real author-comparison is a future ticket.
    return "self"


def _reviewer_node_login(node: object) -> str | None:
    """Return a reviewer-request node's user login, or None for a team/bad shape.

    ``gh pr view --json reviewRequests`` returns a heterogeneous list: User
    nodes carry a ``"login"``; Team nodes carry ``"slug"``/``"name"`` and no
    ``"login"``. Absence of a non-empty login means the request targets a team
    (or the shape is unexpected), not an individual — which this slice
    deliberately ignores (RFC 0011 S2 R5, team-targeted -> not registered). The
    shape tolerance keeps a malformed node from raising anywhere on the path.
    """
    if not isinstance(node, dict):
        return None
    login = node.get("login")
    return login if isinstance(login, str) and login else None


def resolve_and_register_review_request(
    *,
    repo: str,
    pr_number: int,
    pr_url: str,
    reviewer_nodes: list[dict[str, Any]],
    operator_login: str | None,
    source: Literal["webhook", "cli"],
    requester_login: str | None,
) -> tuple[bool, str]:
    """Decide whether a review request targets the operator, and register it.

    Shared decision core for both the ``review_requested`` webhook and
    ``cw review register`` (RFC 0011 S2). Returns ``(registered, reason)``:

    - ``operator_login is None`` -> ``(False, "identity_unresolved")`` (R6
      fail-closed: an unresolved identity never registers anything).
    - empty ``reviewer_nodes`` -> ``(False, "no_reviewer")``.
    - operator's login not among the nodes, but a team node is present ->
      ``(False, "team_targeted")`` (R5).
    - operator's login not among the nodes, all individual ->
      ``(False, "not_operator_targeted")``.
    - operator individually requested -> ``register_watched_pr`` decides
      ``(True, "registered")`` on insert or ``(False, "already_registered")``
      on the idempotency dedup (R7).
    """
    if operator_login is None:
        return (False, "identity_unresolved")
    if not reviewer_nodes:
        return (False, "no_reviewer")
    logins = [_reviewer_node_login(node) for node in reviewer_nodes]
    if operator_login not in logins:
        if any(login is None for login in logins):
            return (False, "team_targeted")
        return (False, "not_operator_targeted")
    inserted = register_watched_pr(
        WatchedPr(
            pr_url=pr_url,
            repo=repo,
            pr_number=pr_number,
            requester_login=requester_login,
            source=source,
        )
    )
    return (True, "registered") if inserted else (False, "already_registered")


def _throttled(
    tasks: list[TicketTask], watched_prs: list[WatchedPr], interval_seconds: int
) -> bool:
    """True if the last hydration pass was under *interval_seconds* ago.

    The baseline is ``max(pr_state.hydrated_at)`` across all tasks and watched
    PRs — no separate persisted timer state (consistent with the plan's
    R6/R10 resolution). Watched PRs (RFC 0011 S2) must be sampled too: a
    watched-PR-only store has no tasks, so sampling tasks alone would never
    throttle and ``_hydrate_watched_prs`` would refetch every active watched
    PR via ``gh pr view`` on every call.
    """
    stamps = [t.pr_state.hydrated_at for t in tasks if t.pr_state is not None]
    stamps += [
        t.auto_merge_rearm_pr_state.hydrated_at
        for t in tasks
        if t.auto_merge_rearm_pr_state is not None
    ]
    stamps += [w.pr_state.hydrated_at for w in watched_prs if w.pr_state is not None]
    if not stamps:
        return False
    elapsed = (datetime.now(UTC) - max(stamps)).total_seconds()
    return elapsed < interval_seconds
