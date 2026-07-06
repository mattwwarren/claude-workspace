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
from typing import TYPE_CHECKING, Any

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.gh import _GH_PR_STATE_MERGED, fetch_pr_view
from cw.models import OrchestratorEventType, PrState

if TYPE_CHECKING:
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
_ROW1_MERGE_BLOCKING_STATES: frozenset[str] = frozenset({"DIRTY", "BEHIND"})
# pr.mergeable fires on ENTERING one of GitHub's genuinely-mergeable statuses
# from outside the set — not on merely leaving a blocking status into
# UNKNOWN/DRAFT (operator resolution, #929 premise round 2026-07-05).
_MERGEABLE_STATES: frozenset[str] = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})
_TERMINAL_PR_STATES: frozenset[str] = frozenset({_GH_PR_STATE_MERGED, "CLOSED"})

_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


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
        return None
    if merge_state_status in _ROW1_MERGE_BLOCKING_STATES:  # Row 1
        return "merge_blocked"
    if not ci_ok:  # Row 2
        return "ci_failing"
    if review_decision == "CHANGES_REQUESTED":  # Row 3
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
    """A task is hydratable when it has a PR URL and a non-terminal PR state."""
    if not task.pr_url:
        return False
    return task.pr_state is None or task.pr_state.state not in _TERMINAL_PR_STATES


def _throttled(tasks: list[TicketTask], interval_seconds: int) -> bool:
    """True if the last hydration pass was under *interval_seconds* ago.

    The baseline is ``max(pr_state.hydrated_at)`` across all tasks — no separate
    persisted timer state (consistent with the plan's R6/R10 resolution).
    """
    stamps = [t.pr_state.hydrated_at for t in tasks if t.pr_state is not None]
    if not stamps:
        return False
    elapsed = (datetime.now(UTC) - max(stamps)).total_seconds()
    return elapsed < interval_seconds


def apply_pr_state_observation(
    *, client: str, ticket_id: str, new_state: PrState
) -> None:
    """Persist one observed ``PrState`` under the queue lock, then emit events.

    Extracted from ``_persist_and_emit``'s per-task body (#930) so the poll
    producer (``_persist_and_emit``) and the webhook push producer
    (``observe_pushed_event``) share the exact same persist/diff/emit
    semantics and transition-dedup.

    Transitions are diffed against the task re-read INSIDE ``dev_queue_lock()``
    — not any pre-lock snapshot the caller may hold — so a writer that touched
    this task's ``pr_state`` between observation and this call can't produce a
    stale diff or a duplicate emit. The durable baseline is written first
    (at-most-once emit); events fire OUTSIDE the queue lock so
    ``record_event``'s inbox lock never nests inside ``dev_queue_lock``.

    A ``(client, ticket_id)`` with no matching task is a silent no-op (the
    task may have been cancelled/removed between observation and this call).
    """
    pending_events: list[tuple[OrchestratorEventType, dict[str, object]]] = []

    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.client != client or task.ticket_id != ticket_id:
                continue
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
                    old=task.pr_state, new=new_state, base=base
                )
            task.pr_state = new_state
            break
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


def _resolve_task_by_pr_ref(
    store: DevQueueStore, repo: str, pr_number: int
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
    old: PrState | None, event_type: OrchestratorEventType, payload: dict[str, Any]
) -> PrState:
    """Build the overlay ``PrState`` for one pushed webhook event (#930).

    Starts from *old* (or a fresh ``PrState()`` baseline when there is no
    prior hydration) and overlays only the field(s) implied by *event_type*,
    refreshing ``hydrated_at``. A missing or malformed payload key is a silent
    no-op for that field — the webhook handler must never reject a request
    for an unexpected payload shape, it just skips the mutation and leaves the
    prior value (or the fresh-baseline default) in place.
    """
    base = old if old is not None else PrState()
    updates: dict[str, Any] = {"hydrated_at": datetime.now(UTC)}
    if event_type == OrchestratorEventType.PR_MERGED:
        updates["state"] = "MERGED"
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
    return base.model_copy(update=updates)


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
    PR is not an error), logged at debug level.
    """
    try:
        event_type = OrchestratorEventType("pr." + wire_event_type)
    except ValueError:
        logger.debug(
            "observe_pushed_event: unknown wire_event_type %r", wire_event_type
        )
        return

    store = load_dev_queue()
    task = _resolve_task_by_pr_ref(store, repo, pr_number)
    if task is None:
        logger.debug(
            "observe_pushed_event: no task tracks %s#%d, ignoring push",
            repo,
            pr_number,
        )
        return

    # Why (#930 operator correction #2): COMMENTED reviews are not a
    # merge-gate signal, so they never mutate PrState (only APPROVED/
    # CHANGES_REQUESTED do) -- but the operator still wants an event emitted
    # for every COMMENTED webhook delivery, INCLUDING duplicate/redelivered
    # ones. There is no PrState field change to compare for COMMENTED, so
    # apply_pr_state_observation's diff-based dedup can't apply here (and
    # would wrongly suppress it) -- this path bypasses it entirely and always
    # emits.
    if (
        event_type == OrchestratorEventType.PR_REVIEW_RECEIVED
        and str(payload.get("review_decision", "")).upper() == "COMMENTED"
    ):
        record_event(
            event_type,
            {
                "repo": repo,
                "pr_number": pr_number,
                "ticket_id": task.ticket_id,
                "client": task.client,
                "review_decision": "COMMENTED",
            },
            correlation_id=task.ticket_id,
        )
        return

    new_state = _overlay_push_observation(task.pr_state, event_type, payload)
    apply_pr_state_observation(
        client=task.client, ticket_id=task.ticket_id, new_state=new_state
    )


def hydrate_pr_states(config: OrchestratorConfig) -> None:
    """Serve-tick pass: hydrate PR state on candidate tasks and emit pr.* events.

    Best-effort and throttled: the whole pass is skipped when the last pass ran
    under ``config.pr_hydration_interval_seconds`` ago. Candidate tasks are those
    with a ``pr_url`` and either no ``pr_state`` or a non-terminal one. A transient
    fetch failure for a single task leaves its prior state untouched.
    """
    store = load_dev_queue()
    if _throttled(store.tasks, config.pr_hydration_interval_seconds):
        return
    candidates = [t for t in store.tasks if _is_candidate(t)]
    if not candidates:
        return
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
