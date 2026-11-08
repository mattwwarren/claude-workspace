"""PR-state hydration for the dispatch serve tick (GitHub #929).

Polls the open PRs referenced by dev-queue tasks, persists merge/CI/review
state on each ``TicketTask.pr_state``, and emits ``pr.*`` bus events on state
transitions. This is the fallback (poll) layer complementing the RFC-0002 SSE
push transport — the emit target is the orchestrator bus (``record_event`` ->
``inbox.jsonl``), consumed by ``retire_merged_prs``.

The CI-summary and attention-state derivation logic is ported from
``.claude/scripts/review_monitor_lib/attention.py`` (``_summarize_status_checks``
and ``_compute_attention_state``, run via ``.claude/scripts/review_monitor.py``),
which lives outside ``src/`` and cannot be
imported: every real invocation execs the script directly via its shebang
under a bare system interpreter with none of this project's dependencies
installed, and the script is cross-repo by design (state keyed
``owner/repo``, invoked from outside any single checkout) so there is no
one venv to bind it to (#1598). See the ticket's Attention-State Decision
Table for the precedence chain re-implemented in
``_compute_attention_state``.

Package split (#2999). The flat 1072-line module is now a package. Its
``__init__`` is deliberately NOT a pure re-export shim (the
``cw.dispatch.routing`` shape): the suite monkeypatches
``cw.pr_hydrate.fetch_pr_view``, ``.run_git``, ``._resolve_repo_slug``,
``._derive_pr_state`` and ``.apply_pr_state_observation`` by dotted path,
and a function resolves its free names against the module that defines
it. So every consumer of those names stays defined here:
``_resolve_repo_slug``, ``repo_slug_mismatch``, ``_derive_pr_state``,
``_hydrate_watched_prs``, ``apply_pr_state_observation``,
``_persist_and_emit``, ``_persist_rearm_owed_pr_state``,
``observe_pushed_event`` and ``hydrate_pr_states``. The rest moved to
submodules that import each other by full path, never this ``__init__``:

- ``attention`` — the CI-rollup constants, ``PrAttentionState``,
  ``summarize_status_checks`` and the attention-state decision table.
- ``repo_slug`` — ``_PR_URL_RE``, ``_REMOTE_SLUG_RE``, ``_parse_pr_url`` and
  ``_slug_mismatch``.
- ``transitions`` — ``_MERGEABLE_STATES``, ``_TERMINAL_PR_STATES``,
  ``_diff_transitions`` and ``_is_candidate``.
- ``watched`` — ``Counterparty``, ``WATCHED_PR_COUNTERPARTY``,
  ``derive_counterparty``, the review-request registration and
  ``_throttled``.
- ``push_overlay`` — the webhook overlay helpers behind
  ``observe_pushed_event``.

A test that patches a name a moved function looks up must target the
owning submodule. Every module logs as ``cw.pr_hydrate``.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw._git import run_git
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.rearm_owed import clear_auto_merge_rearm_owed
from cw.events import record_event
from cw.gh import fetch_pr_view
from cw.models import OrchestratorEventType, PrState, WatchedPr
from cw.pr_hydrate.attention import (
    PrAttentionState,
    _blocked_attention_state,
    _comment_body_is_blocking,
    _compute_attention_state,
    _has_blocking_comment_review,
    summarize_status_checks,
)
from cw.pr_hydrate.push_overlay import (
    _REVIEW_DECISION_COMMENTED,
    _emit_commented_review,
    _overlay_push_observation,
    _resolve_task_by_pr_ref,
)
from cw.pr_hydrate.repo_slug import (
    _PR_URL_RE,
    _REMOTE_SLUG_RE,
    _parse_pr_url,
    _slug_mismatch,
)
from cw.pr_hydrate.transitions import (
    _TERMINAL_PR_STATES,
    _diff_transitions,
    _is_candidate,
)
from cw.pr_hydrate.watched import (
    WATCHED_PR_COUNTERPARTY,
    Counterparty,
    _reviewer_node_login,
    _throttled,
    derive_counterparty,
    resolve_and_register_review_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import OrchestratorConfig, TicketTask

logger = logging.getLogger(__name__)


def _resolve_repo_slug(git_dir: Path) -> str | None:
    """Resolve *git_dir*'s ``origin`` remote to a github ``owner/repo`` slug.

    Fail-open (GitHub #1198): returns ``None`` — never raises — on any
    unresolvable case (not a git dir, no origin remote, non-github URL). The
    subprocess shape mirrors ``doctor.py``'s ``_check_wedge_repo_ahead``.
    """
    try:
        result = run_git(
            ["-C", str(git_dir), "remote", "get-url", "origin"],
            capture_output=True,
            check=False,
            # Why: callers run it lockless but on the tick's path (reconcile's
            # review repo-slug pre-pass, #2564); a hung `git` (stale credential
            # prompt, NFS workspace) must not stall every client's tick.
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _REMOTE_SLUG_RE.search(result.stdout.strip())
    if match is None:
        return None
    return match.group(1)


def repo_slug_mismatch(pr_repo: str, git_dir: Path) -> str | None:
    """:func:`_slug_mismatch` against *git_dir*'s live origin slug (runs git)."""
    return _slug_mismatch(pr_repo, _resolve_repo_slug(git_dir))


def _derive_pr_state(pr_url: str, *, self_login: str | None) -> PrState | None:
    """Fetch and derive a fresh ``PrState`` for *pr_url*, or None on failure.

    *self_login* is the operator's own gh login, resolved once by the caller
    (``hydrate_pr_states``, at most once per tick — see #1195 RISK note) and
    threaded down rather than re-resolved per PR, to avoid an unbounded
    per-candidate ``gh api user`` subprocess retry storm on login-resolution
    failure.
    """
    data = fetch_pr_view(pr_url)
    if data is None:
        return None
    rollup = data.get("statusCheckRollup")
    summary = summarize_status_checks(rollup if isinstance(rollup, list) else [])
    ci_ok: bool = summary["ok"]
    state = str(data.get("state") or "OPEN")
    merge_state_status = str(data.get("mergeStateStatus") or "UNKNOWN")
    review_decision = str(data.get("reviewDecision") or "")
    reviewer_count = len(data.get("reviewRequests") or [])
    is_draft = bool(data.get("isDraft", False))
    pending_count = summary["pending_count"]
    comments = data.get("comments")
    has_blocking_comment_review = _has_blocking_comment_review(
        comments if isinstance(comments, list) else [],
        self_login=self_login,
    )
    # Terminal PRs (MERGED/CLOSED) need no operator attention — the decision
    # table is a candidate-selection filter that never runs for them (#929).
    attention_state = (
        None
        if state in _TERMINAL_PR_STATES
        else _compute_attention_state(
            ci_ok=ci_ok,
            pending_count=pending_count,
            merge_state_status=merge_state_status,
            review_decision=review_decision,
            is_draft=is_draft,
            reviewer_count=reviewer_count,
            has_blocking_comment_review=has_blocking_comment_review,
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
        is_draft=is_draft,
        reviewer_count=reviewer_count,
        pending_count=pending_count,
        failing_checks=[str(f["name"]) for f in summary["failing"]],
        hydrated_at=datetime.now(UTC),
    )


def _hydrate_watched_prs(
    watched_prs: list[WatchedPr],
    *,
    config: OrchestratorConfig,
    self_login: str | None,
) -> None:
    """Hydrate ``pr_state`` on each active watched PR, best-effort (RFC 0011 S2).

    Parallel to the ``TicketTask`` hydration loop: reuses ``_derive_pr_state``,
    threading through a per-watched-PR *effective* login rather than
    re-resolving ``self_login`` here. ``self_login`` is the caller-resolved
    (#1195) once-per-tick fallback; ``config.operator_github_login_by_repo``
    (#1171) is consulted per ``watched.repo`` via a pure dict lookup
    (``resolve_operator_login_for_repo``) — no extra ``gh api user`` call. A
    ``dismissed`` watched PR is skipped (never fetched). A transient fetch
    failure (``_derive_pr_state`` -> None) leaves the prior ``pr_state``
    untouched, mirroring the task path's best-effort contract. Each persist
    re-reads the store under ``dev_queue_lock()`` and matches by ``(repo,
    pr_number)`` on an ``active`` record, so a concurrent writer that
    dismissed/removed the entry can't be clobbered.
    """
    from cw.operator_identity import resolve_operator_login_for_repo

    for watched in watched_prs:
        if watched.status != "active":
            continue
        effective_login = resolve_operator_login_for_repo(
            watched.repo, config, fallback=self_login
        )
        new_state = _derive_pr_state(watched.pr_url, self_login=effective_login)
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
    after the queue lock releases, a choice ADR-0019 permits (emitting under
    ``dev_queue_lock`` would be allowed too).

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
            if (
                resolved_state.state in _TERMINAL_PR_STATES
                and task.auto_merge_rearm_pr_url == task.pr_url
            ):
                clear_auto_merge_rearm_owed(task)
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


def _persist_rearm_owed_pr_state(
    *, client: str, ticket_id: str, pr_url: str, new_state: PrState
) -> None:
    """Persist an owed PR observation without overwriting a different `pr_url`.

    Cancellation can clear `TicketTask.pr_url` while the recovery marker keeps
    this PR alive. The separate state snapshot both drives queue-peek output
    and lets `_throttled` avoid fetching it again every dispatch tick.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if (
                task.client != client
                or task.ticket_id != ticket_id
                or task.auto_merge_rearm_pr_url != pr_url
            ):
                continue
            if new_state.state in _TERMINAL_PR_STATES:
                clear_auto_merge_rearm_owed(task)
            else:
                task.auto_merge_rearm_pr_state = new_state
            save_dev_queue(store)
            return


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

    The operator's own gh login (``cached_gh_login()``) is resolved at most
    once per call, outside both hydration loops (#1195), and threaded down as
    the *fallback* to ``_derive_pr_state``/``_hydrate_watched_prs`` — never
    re-resolved per candidate/watched-PR. What each loop actually passes to
    ``_derive_pr_state`` as ``self_login`` is the *effective* login: a per-PR-
    repo override from ``config.operator_github_login_by_repo`` when present
    (RFC 0011 follow-up #1171), else that once-resolved fallback. The #1195
    invariant (at most one ``gh api user`` subprocess call per tick) is
    preserved because the per-repo override lookup is a pure dict read, not a
    subprocess call.
    """
    store = load_dev_queue()
    if _throttled(store.tasks, store.watched_prs, config.pr_hydration_interval_seconds):
        return
    from cw.operator_identity import cached_gh_login, resolve_operator_login_for_repo

    candidates = [t for t in store.tasks if _is_candidate(t)]
    owed_candidates = [
        t
        for t in store.tasks
        if t.auto_merge_rearm_pr_url is not None
        and t.auto_merge_rearm_pr_url != t.pr_url
    ]
    has_active_watched_prs = any(w.status == "active" for w in store.watched_prs)
    self_login = (
        cached_gh_login()
        if candidates or owed_candidates or has_active_watched_prs
        else None
    )
    derived: list[tuple[TicketTask, PrState]] = []
    for task in candidates:
        pr_url = task.pr_url
        if pr_url is None:  # pragma: no cover - _is_candidate guarantees non-null
            continue
        parsed = _parse_pr_url(pr_url)
        effective_login = (
            resolve_operator_login_for_repo(parsed[0], config, fallback=self_login)
            if parsed is not None
            else self_login
        )
        new_state = _derive_pr_state(pr_url, self_login=effective_login)
        if new_state is not None:
            derived.append((task, new_state))
    if derived:
        _persist_and_emit(derived)
    for task in owed_candidates:
        pr_url = task.auto_merge_rearm_pr_url
        if pr_url is None:  # pragma: no cover - candidate predicate guarantees it
            continue
        parsed = _parse_pr_url(pr_url)
        effective_login = (
            resolve_operator_login_for_repo(parsed[0], config, fallback=self_login)
            if parsed is not None
            else self_login
        )
        owed_state = _derive_pr_state(pr_url, self_login=effective_login)
        if owed_state is not None:
            _persist_rearm_owed_pr_state(
                client=task.client,
                ticket_id=task.ticket_id,
                pr_url=pr_url,
                new_state=owed_state,
            )
    # Watched PRs hydrate on the same throttled pass but independently of task
    # candidates (RFC 0011 S2) — the early ``if not candidates: return`` was
    # removed so a watched-PR-only store still hydrates.
    _hydrate_watched_prs(store.watched_prs, config=config, self_login=self_login)


__all__ = [
    "WATCHED_PR_COUNTERPARTY",
    "_PR_URL_RE",
    "_REMOTE_SLUG_RE",
    "Counterparty",
    "PrAttentionState",
    "_blocked_attention_state",
    "_comment_body_is_blocking",
    "_compute_attention_state",
    "_derive_pr_state",
    "_diff_transitions",
    "_has_blocking_comment_review",
    "_hydrate_watched_prs",
    "_is_candidate",
    "_overlay_push_observation",
    "_parse_pr_url",
    "_persist_rearm_owed_pr_state",
    "_resolve_repo_slug",
    "_resolve_task_by_pr_ref",
    "_reviewer_node_login",
    "_slug_mismatch",
    "_throttled",
    "apply_pr_state_observation",
    "derive_counterparty",
    "hydrate_pr_states",
    "observe_pushed_event",
    "repo_slug_mismatch",
    "resolve_and_register_review_request",
    "summarize_status_checks",
]
