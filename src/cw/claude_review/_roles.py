"""Per-role ``claude -p`` execution for the Claude confirm round (#2791).

The Claude sibling of :func:`cw.codex_review._roles.run_codex_roles`: one
``claude -p`` call per selected reviewer role, under one shared wall-clock
deadline, with every failure mapped onto the same ``CODEX_*`` reason
vocabulary the synthesis step and the prose exit tables key on. What differs
is the wire format (a JSON envelope on stdout, not an ``-o`` file plus a JSONL
stream) and the per-role worktree-unchanged check (G4).

G4: each role snapshots :func:`cw.review_convergence.worktree_state` before and
after its ``claude -p`` call. A difference fails the role with
``CODEX_REVIEW_WORKTREE_MUTATED`` and no further role starts. A tree that was
already dirty before the call is not a mutation; the comparison is
before-versus-after. A ``git`` failure while reading the state raises
:class:`~cw.exceptions.CwError` to the caller.

``time`` is imported at module scope (not via ``from``) so tests can drive
``cw.claude_review._roles.time.monotonic``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from cw.claude_review._runner import build_claude_review_argv
from cw.codex_review._audit_events import _empty_metrics
from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    _COMMAND_NOT_FOUND_RETURNCODE,
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_REVIEW_WORKTREE_MUTATED,
)
from cw.codex_review._context import _parse_reviewer_document
from cw.codex_review._roles import (
    _classify_codex_output_failure,
    _persist_codex_role_document,
    _slug,
)
from cw.executor_diagnostics import build_executor_failure, persist_diagnostics_bundle
from cw.review_convergence import worktree_state
from cw.review_findings import (
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    _best_effort_discarded_tally,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.claude_review._runner import ClaudeRunner, ClaudeRunResult
    from cw.executor_diagnostics import ExecutorFailureCategory
    from cw.review_findings import RejectedFinding, ReviewerRunMetrics

_log = logging.getLogger(__name__)

#: How many changed ``git status --porcelain`` lines a mutation report quotes.
_MUTATION_STATUS_LINES = 5
_COMMAND_NOT_FOUND_MARKER = "command not found"


class WorktreeMutation(NamedTuple):
    """A reviewer role changed the worktree (G4): which role, and the evidence."""

    role: str
    before: tuple[str, str]
    after: tuple[str, str]

    def describe(self) -> str:
        """``HEAD a -> b`` or ``git status changed: <first 5 porcelain lines>``."""
        head_before, status_before = self.before
        head_after, status_after = self.after
        if head_before != head_after:
            return f"HEAD {head_before} -> {head_after}"
        changed = sorted(
            set(status_after.splitlines()) ^ set(status_before.splitlines())
        )
        return "git status changed: " + " | ".join(changed[:_MUTATION_STATUS_LINES])


class ClaudeRolesOutcome(NamedTuple):
    """``run_codex_roles``' four results, plus the G4 mutation if one occurred."""

    documents: list[ReviewerFindingsDocument]
    failures: list[ReviewerRunFailure]
    metrics_by_role: dict[str, ReviewerRunMetrics]
    pre_validation_rejected: list[RejectedFinding]
    mutation: WorktreeMutation | None


class _Envelope(NamedTuple):
    """What one ``claude -p --output-format json`` stdout yielded."""

    category: ExecutorFailureCategory | None
    document_text: str | None
    cost_usd: float | None


def _json_object(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _cost(envelope: dict[str, object]) -> float | None:
    cost = envelope.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


def _read_envelope(stdout: str) -> _Envelope:
    """Unwrap the envelope into the reviewer document text, or a failure category.

    The captured envelope (``tests/_claude_review_helpers.py``) carries the
    document twice: ``structured_output`` (an object, preferred) and
    ``result`` (the same document as a JSON string, the fallback). An
    ``is_error: true`` envelope is a backend-side failure (``runtime_error``,
    which maps to ``CODEX_ERROR``), never a content failure.
    """
    if not stdout.strip():
        return _Envelope("empty_output", None, None)
    envelope = _json_object(stdout)
    if envelope is None:
        return _Envelope("invalid_json", None, None)
    cost = _cost(envelope)
    if envelope.get("is_error") is True:
        return _Envelope("runtime_error", None, cost)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return _Envelope(None, json.dumps(structured), cost)
    result = envelope.get("result")
    if isinstance(result, str):
        return _Envelope(None, result, cost)
    return _Envelope("missing_output", None, cost)


def _process_category(result: ClaudeRunResult) -> ExecutorFailureCategory | None:
    """The process-level failure category of *result*, or ``None`` if it exited 0."""
    if result.timed_out:
        return "timeout"
    if (
        result.returncode == _COMMAND_NOT_FOUND_RETURNCODE
        and _COMMAND_NOT_FOUND_MARKER in result.stderr
    ):
        return "spawn_error"
    if result.returncode != 0:
        return "nonzero_exit"
    return None


def _persist_claude_role_diagnostics(
    *,
    session_id: str,
    role: str,
    category: ExecutorFailureCategory,
    result: ClaudeRunResult,
    argv: list[str],
    duration_seconds: float,
    document_text: str | None,
) -> None:
    failure = build_executor_failure(
        category=category,
        executor_name="claude",
        session_id=session_id,
        argv=argv,
        stdout_excerpt=result.stdout,
        stderr_excerpt=result.stderr,
        reviewer_role=role,
        duration_seconds=duration_seconds,
        exit_code=result.returncode,
        structured_output_excerpt=document_text,
    )
    persist_diagnostics_bundle(
        session_id=session_id, role_slug=_slug(role), failure=failure
    )


def _role_failure(
    *,
    role: str,
    category: ExecutorFailureCategory,
    result: ClaudeRunResult,
    argv: list[str],
    duration: float,
    document_text: str | None,
    session_id: str,
) -> ReviewerRunFailure:
    """Log, persist the diagnostics bundle, and build the coarse failure."""
    reason = _CATEGORY_TO_REASON[category]
    discarded_count, discarded_severities = (
        _best_effort_discarded_tally(document_text)
        if category == "schema_mismatch"
        else (0, {})
    )
    _log.warning("claude review role %r failed: %s (%s)", role, reason, category)
    _persist_claude_role_diagnostics(
        session_id=session_id,
        role=role,
        category=category,
        result=result,
        argv=argv,
        duration_seconds=duration,
        document_text=document_text,
    )
    return ReviewerRunFailure(
        role=role,
        reason=reason,
        discarded_finding_count=discarded_count,
        discarded_finding_severities=discarded_severities,
    )


def _run_claude_role(
    *,
    runner: ClaudeRunner,
    worktree: Path,
    role: str,
    prompt: str,
    argv: list[str],
    timeout_seconds: int | None,
    session_id: str,
) -> tuple[
    ReviewerFindingsDocument | None,
    ReviewerRunFailure | None,
    ReviewerRunMetrics,
    list[RejectedFinding],
    WorktreeMutation | None,
]:
    """Run one role; exactly one of the document and the failure is set.

    The fifth element is set only when the worktree changed across the call
    (G4); the failure then carries ``CODEX_REVIEW_WORKTREE_MUTATED`` and no
    document is returned even if the envelope held one.
    """
    before = worktree_state(worktree)
    start = time.monotonic()
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    duration = time.monotonic() - start
    after = worktree_state(worktree)
    metrics = _empty_metrics(duration)
    if after != before:
        mutation = WorktreeMutation(role, before, after)
        _log.warning(
            "claude review role mutated the worktree: role=%r session=%r %s",
            role,
            session_id,
            mutation.describe(),
        )
        failure = ReviewerRunFailure(role=role, reason=CODEX_REVIEW_WORKTREE_MUTATED)
        return None, failure, metrics, [], mutation
    process_category = _process_category(result)
    envelope = (
        _read_envelope(result.stdout)
        if process_category is None
        else _Envelope(process_category, None, None)
    )
    _log.info(
        "claude review role %r finished in %.1fs (total_cost_usd=%s)",
        role,
        duration,
        envelope.cost_usd,
    )
    category = envelope.category
    if category is None:
        parsed = _parse_reviewer_document(envelope.document_text)
        if parsed is not None:
            doc, rejected = parsed
            _persist_codex_role_document(session_id=session_id, role=role, doc=doc)
            return doc, None, metrics, rejected, None
        category = _classify_codex_output_failure(envelope.document_text)
    failure = _role_failure(
        role=role,
        category=category,
        result=result,
        argv=argv,
        duration=duration,
        document_text=envelope.document_text,
        session_id=session_id,
    )
    return None, failure, metrics, [], None


class _RoleBudget(NamedTuple):
    exhausted: bool
    timeout_seconds: int | None


def _role_budget(deadline: float | None) -> _RoleBudget:
    """The per-role timeout under *deadline* (``None``: no deadline at all)."""
    if deadline is None:
        return _RoleBudget(exhausted=False, timeout_seconds=None)
    remaining = deadline - time.monotonic()
    if remaining <= _MIN_ROLE_TIMEOUT_SECONDS:
        return _RoleBudget(exhausted=True, timeout_seconds=None)
    return _RoleBudget(
        exhausted=False,
        timeout_seconds=max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS),
    )


def run_claude_roles(
    *,
    runner: ClaudeRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> ClaudeRolesOutcome:
    """Run every role under one shared wall-clock deadline.

    Same deadline rule as ``run_codex_roles``: ``None`` means no deadline;
    otherwise each role gets the remaining budget (never below the floor), and
    a role that cannot get the floor is skipped as ``budget_exhausted``
    without calling the runner. The schema is the plain
    ``ReviewerFindingsDocument`` JSON schema (the OpenAI-strict transform is
    codex-only), passed inline, so no scratch files exist to clean up.

    Metrics carry duration only: the envelope's token and cost fields are
    logged, not persisted. A G4 mutation stops the loop; the roles after it
    are not run and contribute nothing.
    """
    argv = build_claude_review_argv(
        model=model, schema=json.dumps(ReviewerFindingsDocument.model_json_schema())
    )
    documents: list[ReviewerFindingsDocument] = []
    failures: list[ReviewerRunFailure] = []
    metrics_by_role: dict[str, ReviewerRunMetrics] = {}
    pre_validation_rejected: list[RejectedFinding] = []
    deadline: float | None = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    for role in roles:
        budget = _role_budget(deadline)
        if budget.exhausted:
            _log.warning("claude review role %r skipped: budget exhausted", role)
            failures.append(
                ReviewerRunFailure(role=role, reason=CODEX_BUDGET_EXHAUSTED)
            )
            continue
        doc, failure, metrics, rejected, mutation = _run_claude_role(
            runner=runner,
            worktree=worktree,
            role=role,
            prompt=prompts_by_role[role],
            argv=argv,
            timeout_seconds=budget.timeout_seconds,
            session_id=session_id,
        )
        metrics_by_role[role] = metrics
        pre_validation_rejected.extend(rejected)
        if doc is not None:
            documents.append(doc)
        if failure is not None:
            failures.append(failure)
        if mutation is not None:
            return ClaudeRolesOutcome(
                documents, failures, metrics_by_role, pre_validation_rejected, mutation
            )
    return ClaudeRolesOutcome(
        documents, failures, metrics_by_role, pre_validation_rejected, None
    )
