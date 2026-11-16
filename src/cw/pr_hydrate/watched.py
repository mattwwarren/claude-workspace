"""Counterparty axis, review-request registration and the hydration throttle.

Part of the ``cw.pr_hydrate`` package (#2999). Logs as ``cw.pr_hydrate``,
the pre-split logger name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    register_watched_pr,
    save_dev_queue,
)
from cw.models import TERMINAL_QUEUE_STATUSES, WatchedPr
from cw.pr_hydrate.transitions import _TERMINAL_PR_STATES, _is_watched_candidate

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask

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

# GitHub #3654 — hydration fetches at most this many active watches per pass.
# Why: without a cap every active watch costs one ``gh pr view`` per pass, so
# the pass cost grows with the watch list (which now accrues dismissed entries
# only as garbage, but active ones grow with requests). A module constant
# matches WATCHED_PR_COUNTERPARTY; promote to an OrchestratorConfig field only
# if operators need tuning.
WATCHED_HYDRATION_CAP_PER_PASS: Final[int] = 20

# Sort key sentinel for a watch never hydrated: sorts before every real stamp,
# so new watches are fetched first and the cap becomes a rotation.
_WATCH_NEVER_HYDRATED: Final = datetime.min.replace(tzinfo=UTC)


def _hydrated_at_or_never(watched: WatchedPr) -> datetime:
    """Sort key: the watch's last ``hydrated_at``, or the never-hydrated sentinel."""
    if watched.pr_state is None:
        return _WATCH_NEVER_HYDRATED
    return watched.pr_state.hydrated_at


def select_watched_hydration_batch(watched_prs: list[WatchedPr]) -> list[WatchedPr]:
    """Pick the watches one hydration pass may fetch (#3654).

    Only hydratable watches (active, non-terminal) are considered. Oldest-first
    by ``hydrated_at`` before the slice, so each pass fetches the watches that
    were fetched longest ago; a bare ``[:CAP]`` would refetch the head of the
    list forever.
    """
    candidates = [w for w in watched_prs if _is_watched_candidate(w)]
    candidates.sort(key=_hydrated_at_or_never)
    return candidates[:WATCHED_HYDRATION_CAP_PER_PASS]


def retire_terminal_watched_prs(store: DevQueueStore) -> list[WatchedPr]:
    """Dismiss active watches whose PR is MERGED/CLOSED and unreferenced (#3654).

    A watch is referenced when a non-terminal row of the same client carries
    ``blocked_on_pr`` equal to the watch's PR number. Referenced watches are
    kept so the release pass can still observe the terminal state. Pure with
    respect to persistence: the caller holds ``dev_queue_lock`` and saves.
    Dismissed entries are kept, not deleted (follow-up debt).
    """
    referenced = {
        (task.client, task.blocked_on_pr)
        for task in store.tasks
        if task.status not in TERMINAL_QUEUE_STATUSES and task.blocked_on_pr is not None
    }
    retired: list[WatchedPr] = []
    for watched in store.watched_prs:
        if watched.status != "active":
            continue
        if (
            watched.pr_state is None
            or watched.pr_state.state not in _TERMINAL_PR_STATES
        ):
            continue
        if (
            watched.client is not None
            and (watched.client, watched.pr_number) in referenced
        ):
            continue
        watched.status = "dismissed"
        retired.append(watched)
        logger.info(
            "retired terminal watched PR repo=%s pr_number=%s client=%s "
            "source=%s state=%s",
            watched.repo,
            watched.pr_number,
            watched.client,
            watched.source,
            watched.pr_state.state,
        )
    return retired


def sweep_terminal_watched_prs() -> None:
    """Self-locking sweep: retire terminal unreferenced watches and persist (#3654).

    Saves only when something was retired, so a no-op pass leaves the file
    untouched.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        if retire_terminal_watched_prs(store):
            save_dev_queue(store)


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
