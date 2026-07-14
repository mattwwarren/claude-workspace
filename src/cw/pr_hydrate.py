"""PR-state hydration for the dispatch serve tick (GitHub #929).

Polls the open PRs referenced by dev-queue tasks, persists merge/CI/review
state on each ``TicketTask.pr_state``, and emits ``pr.*`` bus events on state
transitions. This is the fallback (poll) layer complementing the RFC-0002 SSE
push transport — the emit target is the orchestrator bus (``record_event`` ->
``inbox.jsonl``), consumed by ``retire_merged_prs``.

The CI-summary and attention-state derivation logic is ported from
``.claude/scripts/review_monitor.py`` (``_summarize_status_checks`` and
``_compute_attention_state``), which lives outside ``src/`` and cannot be
imported. See the ticket's Attention-State Decision Table for the precedence
chain re-implemented in ``_compute_attention_state``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    register_watched_pr,
    save_dev_queue,
)
from cw.events import record_event
from cw.gh import _GH_PR_STATE_MERGED, fetch_pr_view
from cw.models import OrchestratorEventType, PrState, WatchedPr

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import DevQueueStore, OrchestratorConfig, TicketTask

logger = logging.getLogger(__name__)

# Ported verbatim from .claude/scripts/review_monitor.py (_summarize_status_checks).
_FAILED_CHECKRUN_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)
_PENDING_CHECKRUN_STATUSES: frozenset[str] = frozenset(
    {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}
)
# Row-1 of the attention-state decision table (merge_blocked). BLOCKED is
# deliberately NOT in this set — it means "waiting on required reviews/checks,"
# not a code problem (see Rows 5a-5c).
# Why: this is a strict ALLOW-list, not a deny-list, so an UNKNOWN merge state
# never reads as merge_blocked. Ported review-monitor lesson (session:826a27f3,
# "mergeStateStatus can read UNKNOWN immediately after push/rebase"): GitHub
# computes mergeStateStatus asynchronously, so a check right after a push/rebase
# can transiently return UNKNOWN before DIRTY/CLEAN is determinable. Keeping the
# set to {DIRTY, BEHIND} means a not-yet-computed state can't misfire the
# escalate_merge_block recipe; the next poll re-hydrates the real value. See
# tests/test_pr_hydrate.py::TestAttentionState::
# test_row1_unknown_merge_state_not_merge_blocked.
_ROW1_MERGE_BLOCKING_STATES: frozenset[str] = frozenset({"DIRTY", "BEHIND"})
# pr.mergeable fires on ENTERING one of GitHub's genuinely-mergeable statuses
# from outside the set — not on merely leaving a blocking status into
# UNKNOWN/DRAFT (operator resolution, #929 premise round 2026-07-05).
_MERGEABLE_STATES: frozenset[str] = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})
_TERMINAL_PR_STATES: frozenset[str] = frozenset({_GH_PR_STATE_MERGED, "CLOSED"})

_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")

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


def _summarize_status_checks(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a ``statusCheckRollup`` list into a ``failing`` / ``pending`` summary.

    Ported verbatim from ``_summarize_status_checks`` in
    ``.claude/scripts/review_monitor.py`` (un-importable — lives outside src/).
    ``ok`` is the sole source of CI truth: in-progress/pending checks never block
    it, only genuine failures do.
    """
    failing: list[dict[str, str]] = []
    pending_count = 0
    for c in rollup:
        typename = c.get("__typename", "")
        if typename == "CheckRun":
            status = (c.get("status") or "").upper()
            conclusion = (c.get("conclusion") or "").upper()
            if status == "COMPLETED" and conclusion in _FAILED_CHECKRUN_CONCLUSIONS:
                failing.append(
                    {
                        "workflow": c.get("workflowName") or "",
                        "name": c.get("name") or "",
                        "conclusion": conclusion,
                        "url": c.get("detailsUrl") or "",
                    }
                )
            elif status in _PENDING_CHECKRUN_STATUSES:
                pending_count += 1
        else:
            state_str = (c.get("state") or "").upper()
            if state_str in ("FAILURE", "ERROR"):
                failing.append(
                    {
                        "workflow": "",
                        "name": c.get("context") or "",
                        "conclusion": state_str,
                        "url": c.get("targetUrl") or "",
                    }
                )
            elif state_str == "PENDING":
                pending_count += 1
    return {"failing": failing, "pending_count": pending_count, "ok": not failing}


def _compute_attention_state(
    *,
    ci_ok: bool,
    pending_count: int,
    merge_state_status: str,
    review_decision: str,
    is_draft: bool,
    reviewer_count: int,
) -> str | None:
    """Derive the operator attention-state via the #929 decision table.

    Precedence chain + unconditional draft-gate ported from
    ``_compute_attention_state`` in ``.claude/scripts/review_monitor.py``. cw
    drops the reference's role/status/unaddressed_count/comment-review inputs
    (no subsystem exists for them). First matching row wins:
      0. is_draft                                    -> None
      1. merge_state_status in (DIRTY, BEHIND)       -> merge_blocked
      2. not ci_ok                                   -> ci_failing
      3. review_decision == CHANGES_REQUESTED        -> changes_requested
      4. review_decision == REVIEW_REQUIRED and no reviewer -> no_reviewer
      5a. BLOCKED and pending checks                 -> None (waiting on CI)
      5b. BLOCKED and review required                -> ready_to_approve
      5c. BLOCKED otherwise                          -> None (unknown blocker)
      6. healthy default                             -> ready_to_approve

    Rows 5a-5c encode the #929 premise-round finding (2026-07-05): BLOCKED can
    co-occur with green CI (a required check that hasn't run yet, or missing
    approvals), so BLOCKED alone must not read as "ready to approve".
    """
    if is_draft:  # Row 0 — drafts never enter an escalation path.
        # Why: unconditional draft-gate ported from review-monitor lesson
        # (session:fc766c55, "Draft PRs must not enter attention/escalation
        # paths"): a draft with green CI and a BLOCKED merge state would
        # otherwise fall through to ready_to_approve and trigger channel bumps /
        # auto-approve. is_draft short-circuits BEFORE every other row so no
        # draft ever produces an attention state. See tests/test_pr_hydrate.py::
        # TestAttentionState::{test_row0_draft_returns_none,
        # test_row0_gates_row4_draft_zero_reviewers}.
        return None
    if merge_state_status in _ROW1_MERGE_BLOCKING_STATES:  # Row 1
        return "merge_blocked"
    if not ci_ok:  # Row 2
        return "ci_failing"
    if review_decision == "CHANGES_REQUESTED":  # Row 3
        # Why: fires on the PR's top-level reviewDecision, ported from review-
        # monitor lesson (session:1a93541b, "changes_requested fires on top-level
        # reviewDecision"): a "Request changes" review with NO inline comments
        # still moves reviewDecision to CHANGES_REQUESTED. cw has no inline-thread
        # subsystem, so this top-level signal is the sole changes_requested
        # trigger for the address_review recipe. See tests/test_pr_hydrate.py::
        # TestAttentionState::test_row3_changes_requested.
        return "changes_requested"
    if review_decision == "REVIEW_REQUIRED" and reviewer_count == 0:  # Row 4
        return "no_reviewer"
    # Rows 5a-5c (BLOCKED) delegate; anything else is Row 6's healthy default.
    return (
        _blocked_attention_state(
            pending_count=pending_count, review_decision=review_decision
        )
        if merge_state_status == "BLOCKED"
        else "ready_to_approve"
    )


def _blocked_attention_state(*, pending_count: int, review_decision: str) -> str | None:
    """Rows 5a-5c: attention state for ``mergeStateStatus == BLOCKED``.

    5a — a required check is still running: waiting on CI, no attention state.
    5b — approvals outstanding: the one genuinely approvable BLOCKED shape.
    5c — blocked for an undetermined reason: don't overclaim approvability.
    """
    if pending_count > 0:  # 5a
        return None
    if review_decision == "REVIEW_REQUIRED":  # 5b
        return "ready_to_approve"
    return None  # 5c


def _parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse ``(owner/repo, pr_number)`` from a GitHub PR URL, or None."""
    match = _PR_URL_RE.search(pr_url)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _derive_pr_state(pr_url: str) -> PrState | None:
    """Fetch and derive a fresh ``PrState`` for *pr_url*, or None on failure."""
    data = fetch_pr_view(pr_url)
    if data is None:
        return None
    rollup = data.get("statusCheckRollup")
    summary = _summarize_status_checks(rollup if isinstance(rollup, list) else [])
    ci_ok: bool = summary["ok"]
    state = str(data.get("state") or "OPEN")
    merge_state_status = str(data.get("mergeStateStatus") or "UNKNOWN")
    review_decision = str(data.get("reviewDecision") or "")
    reviewer_count = len(data.get("reviewRequests") or [])
    # Terminal PRs (MERGED/CLOSED) need no operator attention — the decision
    # table is a candidate-selection filter that never runs for them (#929).
    attention_state = (
        None
        if state in _TERMINAL_PR_STATES
        else _compute_attention_state(
            ci_ok=ci_ok,
            pending_count=summary["pending_count"],
            merge_state_status=merge_state_status,
            review_decision=review_decision,
            is_draft=bool(data.get("isDraft", False)),
            reviewer_count=reviewer_count,
        )
    )
    mergeable = data.get("mergeable")
    return PrState(
        state=state,
        mergeable=None if mergeable is None else str(mergeable),
        merge_state_status=merge_state_status,
        ci_ok=ci_ok,
        review_decision=review_decision,
        attention_state=attention_state,
        is_draft=bool(data.get("isDraft", False)),
        reviewer_count=reviewer_count,
        pending_count=summary["pending_count"],
        failing_checks=[str(f["name"]) for f in summary["failing"]],
        hydrated_at=datetime.now(UTC),
    )


def _diff_transitions(
    *,
    old: PrState | None,
    new: PrState,
    base: dict[str, object],
) -> list[tuple[OrchestratorEventType, dict[str, object]]]:
    """Return ``(event_type, payload)`` pairs for each old->new transition (R5).

    ``old``/``new`` are keyword-only: both are ``PrState``-domain values with no
    structural difference at the call site, so an accidental positional swap
    would silently invert every transition's direction with no type error.

    Dedup rule: value-change events (ci_failed/review_received/mergeable) require
    a prior persisted baseline (``old is not None``); ``pr.merged`` fires on the
    first observation of a MERGED state so a re-discovered merge still retires.
    """
    events: list[tuple[OrchestratorEventType, dict[str, object]]] = []
    old_state = old.state if old is not None else ""
    if new.state == _GH_PR_STATE_MERGED and old_state != _GH_PR_STATE_MERGED:
        events.append((OrchestratorEventType.PR_MERGED, dict(base)))
    if old is not None and old.ci_ok and not new.ci_ok:
        events.append(
            (
                OrchestratorEventType.PR_CI_FAILED,
                {**base, "failing_checks": list(new.failing_checks)},
            )
        )
    if old is not None and old.review_decision != new.review_decision:
        events.append(
            (
                OrchestratorEventType.PR_REVIEW_RECEIVED,
                {**base, "review_decision": new.review_decision},
            )
        )
    if (
        old is not None
        and old.merge_state_status not in _MERGEABLE_STATES
        and new.merge_state_status in _MERGEABLE_STATES
    ):
        # mergeStateStatus is a deliberate passthrough (not snake_cased) — it is
        # the raw gh field name, unlike the derived failing_checks/review_decision.
        events.append(
            (
                OrchestratorEventType.PR_MERGEABLE,
                {**base, "mergeStateStatus": new.merge_state_status},
            )
        )
    return events


def _is_candidate(task: TicketTask) -> bool:
    """A task is hydratable when it has a PR URL and a non-terminal PR state.

    Why the terminal-state exclusion: ported review-monitor lesson
    (session:94a665a5, "Abandoned PR auto-completion") — a PR MERGED or CLOSED on
    GitHub needs no further operator attention. Excluding terminal states here
    (the same predicate the review-recipe detect phase reuses) means no review
    recipe ever fires on a merged/abandoned PR, cw's analogue of review_monitor
    auto-completing such PRs out of the monitored queue. See
    tests/test_pr_hydrate.py::TestCandidateSelection::test_skips_closed_pr_state
    and tests/test_reconcile_review_recipes.py::test_closed_pr_never_a_candidate.
    """
    if not task.pr_url:
        return False
    return task.pr_state is None or task.pr_state.state not in _TERMINAL_PR_STATES


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


def _hydrate_watched_prs(watched_prs: list[WatchedPr]) -> None:
    """Hydrate ``pr_state`` on each active watched PR, best-effort (RFC 0011 S2).

    Parallel to the ``TicketTask`` hydration loop: reuses ``_derive_pr_state``
    unmodified. A ``dismissed`` watched PR is skipped (never fetched). A
    transient fetch failure (``_derive_pr_state`` -> None) leaves the prior
    ``pr_state`` untouched, mirroring the task path's best-effort contract. Each
    persist re-reads the store under ``dev_queue_lock()`` and matches by
    ``(repo, pr_number)`` on an ``active`` record, so a concurrent writer that
    dismissed/removed the entry can't be clobbered.
    """
    for watched in watched_prs:
        if watched.status != "active":
            continue
        new_state = _derive_pr_state(watched.pr_url)
        if new_state is None:
            continue
        with dev_queue_lock():
            store = load_dev_queue()
            updated = False
            for persisted in store.watched_prs:
                if (
                    persisted.repo == watched.repo
                    and persisted.pr_number == watched.pr_number
                    and persisted.status == "active"
                ):
                    persisted.pr_state = new_state
                    updated = True
                    break
            if updated:
                save_dev_queue(store)


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
    stamps += [w.pr_state.hydrated_at for w in watched_prs if w.pr_state is not None]
    if not stamps:
        return False
    elapsed = (datetime.now(UTC) - max(stamps)).total_seconds()
    return elapsed < interval_seconds


def apply_pr_state_observation(
    *,
    client: str,
    ticket_id: str,
    new_state: PrState | None = None,
    overlay: Callable[[PrState | None], PrState] | None = None,
) -> None:
    """Persist one observed ``PrState`` under the queue lock, then emit events.

    Extracted from ``_persist_and_emit``'s per-task body (#930) so the poll
    producer (``_persist_and_emit``) and the webhook push producer
    (``observe_pushed_event``) share the exact same persist/diff/emit
    semantics and transition-dedup.

    Exactly one of *new_state* or *overlay* must be given. The poll producer
    passes *new_state* (always a complete state freshly fetched from ``gh``,
    so there is nothing to overlay). The push producer passes *overlay*: a
    callable invoked with the task's ``pr_state`` as re-read INSIDE
    ``dev_queue_lock()`` -- not any pre-lock snapshot the caller may hold --
    so a concurrent writer that touched this task's ``pr_state`` between the
    caller's initial observation and this call can't be silently clobbered
    (#930 fix: the original push path built its overlay from a pre-lock
    snapshot and persisted that stale result unconditionally, discarding any
    write that landed in the meantime).

    Transitions are diffed against that same freshly-locked baseline, so a
    concurrent writer can't produce a stale diff or a duplicate emit either.
    The durable baseline is written first (at-most-once emit); events fire
    OUTSIDE the queue lock so ``record_event``'s inbox lock never nests inside
    ``dev_queue_lock``.

    A ``(client, ticket_id)`` with no matching task is a silent no-op (the
    task may have been cancelled/removed between observation and this call).
    """
    exactly_one_msg = "exactly one of new_state or overlay must be given"
    if (new_state is None) == (overlay is None):
        raise ValueError(exactly_one_msg)

    def _resolve(old: PrState | None) -> PrState:
        if overlay is not None:
            return overlay(old)
        if new_state is not None:
            return new_state
        raise ValueError(exactly_one_msg)  # unreachable given the check above

    pending_events: list[tuple[OrchestratorEventType, dict[str, object]]] = []

    with dev_queue_lock():
        store = load_dev_queue()
        matched = False
        for task in store.tasks:
            if task.client != client or task.ticket_id != ticket_id:
                continue
            matched = True
            resolved_state = _resolve(task.pr_state)
            parsed = _parse_pr_url(task.pr_url or "")
            if parsed is not None:
                repo, pr_number = parsed
                base: dict[str, object] = {
                    "repo": repo,
                    "pr_number": pr_number,
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                }
                pending_events = _diff_transitions(
                    old=task.pr_state, new=resolved_state, base=base
                )
            task.pr_state = resolved_state
            break
        if matched:
            save_dev_queue(store)

    for event_type, payload in pending_events:
        record_event(event_type, payload, correlation_id=ticket_id)


def _persist_and_emit(derived: list[tuple[TicketTask, PrState]]) -> None:
    """Persist each derived state and emit its transitions (poll producer).

    Thin loop over ``apply_pr_state_observation`` — one queue-lock acquisition
    per task rather than one for the whole batch, which is functionally
    equivalent for this best-effort pass (no caller asserts lock-acquisition
    count) and is what lets the push producer share this exact code path.
    """
    for task, new_state in derived:
        apply_pr_state_observation(
            client=task.client, ticket_id=task.ticket_id, new_state=new_state
        )


# Wire-level review_decision value for a comment-only review (#930). Not a
# merge-gate signal -- see the "Why" note on _emit_commented_review below.
_REVIEW_DECISION_COMMENTED = "COMMENTED"


def _resolve_task_by_pr_ref(
    store: DevQueueStore, *, repo: str, pr_number: int
) -> TicketTask | None:
    """Return the dev-queue task whose ``pr_url`` matches ``(repo, pr_number)``.

    Linear scan reusing ``_parse_pr_url`` — no separate index. A ``(repo,
    pr_number)`` with no matching task is not an error (untracked PR); the
    caller no-ops.
    """
    for task in store.tasks:
        if _parse_pr_url(task.pr_url or "") == (repo, pr_number):
            return task
    return None


def _overlay_push_observation(
    old: PrState | None, *, event_type: OrchestratorEventType, payload: dict[str, Any]
) -> PrState:
    """Build the overlay ``PrState`` for one pushed webhook event (#930).

    Starts from *old* (or a fresh ``PrState()`` baseline when there is no
    prior hydration) and overlays only the field(s) implied by *event_type*,
    refreshing ``hydrated_at``. A missing or malformed payload key is a silent
    no-op for that field — the webhook handler must never reject a request
    for an unexpected payload shape, it just skips the mutation and leaves the
    prior value (or the fresh-baseline default) in place.

    Called from inside ``apply_pr_state_observation``'s ``dev_queue_lock()``
    with the freshly-locked task state as *old* — never a pre-lock snapshot —
    so this overlay can't clobber a concurrent writer (#930 fix).

    After the field(s) implied by *event_type* are overlaid, ``attention_state``
    is always recomputed (#1196) — mirroring the poll path (``_derive_pr_state``),
    which always recomputes it via ``_compute_attention_state``. Terminal states
    (MERGED/CLOSED) short-circuit straight to None; otherwise the ladder is
    recomputed from the (possibly just-overlaid) ci_ok/merge_state_status/
    review_decision plus ``is_draft``/``reviewer_count``/``pending_count``,
    which are only ever carried forward from *base* — no wire payload ever
    carries them, so they are never themselves overlaid.
    """
    base = old if old is not None else PrState()
    updates: dict[str, Any] = {"hydrated_at": datetime.now(UTC)}
    if event_type == OrchestratorEventType.PR_MERGED:
        updates["state"] = _GH_PR_STATE_MERGED
    elif event_type == OrchestratorEventType.PR_CI_FAILED:
        updates["ci_ok"] = False
        failing_checks = payload.get("failing_checks")
        if isinstance(failing_checks, list):
            updates["failing_checks"] = [str(f) for f in failing_checks]
    elif event_type == OrchestratorEventType.PR_REVIEW_RECEIVED:
        review_decision = payload.get("review_decision")
        if review_decision is not None:
            updates["review_decision"] = str(review_decision)
    elif event_type == OrchestratorEventType.PR_MERGEABLE:
        merge_state_status = payload.get("merge_state_status")
        if merge_state_status is not None:
            updates["merge_state_status"] = str(merge_state_status)
    merged_state = updates.get("state", base.state)
    if merged_state in _TERMINAL_PR_STATES:
        updates["attention_state"] = None
    else:
        updates["attention_state"] = _compute_attention_state(
            ci_ok=updates.get("ci_ok", base.ci_ok),
            pending_count=base.pending_count,
            merge_state_status=updates.get(
                "merge_state_status", base.merge_state_status
            ),
            review_decision=updates.get("review_decision", base.review_decision),
            is_draft=base.is_draft,
            reviewer_count=base.reviewer_count,
        )
    return base.model_copy(update=updates)


def _emit_commented_review(*, repo: str, pr_number: int, task: TicketTask) -> None:
    """Emit ``pr.review_received`` for a COMMENTED review, unconditionally.

    Why (#930 operator correction #2): COMMENTED reviews are not a
    merge-gate signal, so they never mutate PrState (only APPROVED/
    CHANGES_REQUESTED do) -- but the operator still wants an event emitted
    for every COMMENTED webhook delivery, INCLUDING duplicate/redelivered
    ones. There is no PrState field change to compare for COMMENTED, so
    apply_pr_state_observation's diff-based dedup can't apply here (and
    would wrongly suppress it) -- this path bypasses it entirely and always
    emits.
    """
    record_event(
        OrchestratorEventType.PR_REVIEW_RECEIVED,
        {
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": task.ticket_id,
            "client": task.client,
            "review_decision": _REVIEW_DECISION_COMMENTED,
        },
        correlation_id=task.ticket_id,
    )


def observe_pushed_event(
    *, repo: str, pr_number: int, wire_event_type: str, payload: dict[str, Any]
) -> None:
    """Handle one GitHub webhook push event (#930).

    Routes a pushed ``(repo, pr_number, wire_event_type, payload)``
    observation through the SAME persist/diff/emit path as poll hydration
    (``apply_pr_state_observation``) so push and poll producers share
    transition-dedup and both land in ``dev_queue.json`` (``pr_state``) and
    ``inbox.jsonl`` (``pr.*`` events). *wire_event_type* is the bare suffix
    from ``cw_pr_events_server._VALID_EVENT_TYPES`` (``"ci_failed"``,
    ``"review_received"``, ``"mergeable"``, ``"merged"``); an unrecognized
    suffix is a silent no-op (defensive — the wire contract validates this
    upstream via ``PREventRequest``).

    Resolving ``(repo, pr_number)`` to no task is a silent no-op (an untracked
    PR is not an error), logged at debug level. The initial lookup below is
    used only to obtain the ``(client, ticket_id)`` routing key and to decide
    the COMMENTED bypass; the actual persisted state is always computed from
    the freshly-locked baseline inside ``apply_pr_state_observation`` (#930
    fix), not from this pre-lock snapshot.
    """
    try:
        event_type = OrchestratorEventType("pr." + wire_event_type)
    except ValueError:
        logger.debug(
            "observe_pushed_event: unknown wire_event_type %r", wire_event_type
        )
        return

    store = load_dev_queue()
    task = _resolve_task_by_pr_ref(store, repo=repo, pr_number=pr_number)
    if task is None:
        logger.debug(
            "observe_pushed_event: no task tracks %s#%d, ignoring push",
            repo,
            pr_number,
        )
        return

    if (
        event_type == OrchestratorEventType.PR_REVIEW_RECEIVED
        and str(payload.get("review_decision", "")).upper()
        == _REVIEW_DECISION_COMMENTED
    ):
        _emit_commented_review(repo=repo, pr_number=pr_number, task=task)
        return

    apply_pr_state_observation(
        client=task.client,
        ticket_id=task.ticket_id,
        overlay=lambda old: _overlay_push_observation(
            old, event_type=event_type, payload=payload
        ),
    )


def hydrate_pr_states(config: OrchestratorConfig) -> None:
    """Serve-tick pass: hydrate PR state on candidate tasks and emit pr.* events.

    Best-effort and throttled: the whole pass is skipped when the last pass ran
    under ``config.pr_hydration_interval_seconds`` ago. Candidate tasks are those
    with a ``pr_url`` and either no ``pr_state`` or a non-terminal one. A transient
    fetch failure for a single task leaves its prior state untouched. Active
    ``watched_prs`` (RFC 0011 S2) hydrate on the same pass via
    ``_hydrate_watched_prs``, independently of whether any task is a candidate.
    """
    store = load_dev_queue()
    if _throttled(store.tasks, store.watched_prs, config.pr_hydration_interval_seconds):
        return
    candidates = [t for t in store.tasks if _is_candidate(t)]
    derived: list[tuple[TicketTask, PrState]] = []
    for task in candidates:
        pr_url = task.pr_url
        if pr_url is None:  # pragma: no cover - _is_candidate guarantees non-null
            continue
        new_state = _derive_pr_state(pr_url)
        if new_state is not None:
            derived.append((task, new_state))
    if derived:
        _persist_and_emit(derived)
    # Watched PRs hydrate on the same throttled pass but independently of task
    # candidates (RFC 0011 S2) — the early ``if not candidates: return`` was
    # removed so a watched-PR-only store still hydrates.
    _hydrate_watched_prs(store.watched_prs)
