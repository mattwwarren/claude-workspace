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
from cw.gh import fetch_pr_view
from cw.models import OrchestratorEventType, PrState

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig, TicketTask

logger = logging.getLogger(__name__)

# Ported verbatim from .claude/scripts/review_monitor.py (_summarize_status_checks).
_FAILED_CHECKRUN_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)
_PENDING_CHECKRUN_STATUSES: frozenset[str] = frozenset(
    {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}
)
_BLOCKING_MERGE_STATES: frozenset[str] = frozenset({"DIRTY", "BEHIND", "BLOCKED"})
_TERMINAL_PR_STATES: frozenset[str] = frozenset({"MERGED", "CLOSED"})

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
      5/6. BLOCKED / healthy default                 -> ready_to_approve
    """
    if is_draft:  # Row 0 — drafts never enter an escalation path.
        return None
    if merge_state_status in ("DIRTY", "BEHIND"):  # Row 1
        return "merge_blocked"
    if not ci_ok:  # Row 2
        return "ci_failing"
    if review_decision == "CHANGES_REQUESTED":  # Row 3
        return "changes_requested"
    if review_decision == "REVIEW_REQUIRED" and reviewer_count == 0:  # Row 4
        return "no_reviewer"
    # Rows 5 (BLOCKED — waiting on required reviews) and 6 (healthy default)
    # both resolve to ready_to_approve.
    return "ready_to_approve"


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
    old: PrState | None,
    new: PrState,
    *,
    base: dict[str, object],
) -> list[tuple[OrchestratorEventType, dict[str, object]]]:
    """Return ``(event_type, payload)`` pairs for each old->new transition (R5).

    Dedup rule: value-change events (ci_failed/review_received/mergeable) require
    a prior persisted baseline (``old is not None``); ``pr.merged`` fires on the
    first observation of a MERGED state so a re-discovered merge still retires.
    """
    events: list[tuple[OrchestratorEventType, dict[str, object]]] = []
    old_state = old.state if old is not None else ""
    if new.state == "MERGED" and old_state != "MERGED":
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
        and old.merge_state_status in _BLOCKING_MERGE_STATES
        and new.merge_state_status not in _BLOCKING_MERGE_STATES
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


def _persist_and_emit(derived: list[tuple[TicketTask, PrState]]) -> None:
    """Persist new states under the queue lock, then emit events (persist-first).

    Transitions are computed against each task's pre-persist state; the durable
    baseline is written first (at-most-once emit), then events fire OUTSIDE the
    queue lock so ``record_event``'s inbox lock never nests inside it.
    """
    pending_events: list[tuple[str, OrchestratorEventType, dict[str, object]]] = []
    new_by_key: dict[tuple[str, str], PrState] = {}
    for task, new_state in derived:
        new_by_key[(task.client, task.ticket_id)] = new_state
        parsed = _parse_pr_url(task.pr_url or "")
        if parsed is None:
            continue
        repo, pr_number = parsed
        base: dict[str, object] = {
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": task.ticket_id,
            "client": task.client,
        }
        transitions = _diff_transitions(task.pr_state, new_state, base=base)
        for event_type, payload in transitions:
            pending_events.append((task.ticket_id, event_type, payload))

    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            fresh = new_by_key.get((task.client, task.ticket_id))
            if fresh is not None:
                task.pr_state = fresh
        save_dev_queue(store)

    for ticket_id, event_type, payload in pending_events:
        record_event(event_type, payload, correlation_id=ticket_id)


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
