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
from cw.codex_review._audit_events import _as_int, _empty_metrics, _no_token_usage
from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_REVIEW_WORKTREE_MUTATED,
    _is_spawn_error,
)
from cw.codex_review._context import _parse_reviewer_document
from cw.codex_review._roles import (
    _classify_codex_output_failure,
    _persist_codex_role_document,
    _role_budget,
    _role_deadline,
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
    metrics: ReviewerRunMetrics


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


def _usage_metrics(envelope: dict[str, object], duration: float) -> ReviewerRunMetrics:
    """Map the envelope's ``usage`` and ``modelUsage`` onto the run metrics (#3063).

    ``input_tokens`` is the base count plus the cache read and creation parts
    (the Anthropic ``input_tokens`` field excludes both); ``cached_input_tokens``
    is the cache read, so it stays a subset of ``input_tokens``. A missing cache
    part counts as zero; a non-int base count leaves ``input_tokens`` ``None``.
    ``effective_model`` is the sole ``modelUsage`` key, or ``None`` when there are
    several or none. ``cost_usd`` stays logged, never stored (ADR-0021).
    """
    metrics = _empty_metrics(duration)
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        base = _as_int(usage.get("input_tokens"))
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        cache_created = _as_int(usage.get("cache_creation_input_tokens"))
        if base is not None:
            metrics["input_tokens"] = base + (cache_read or 0) + (cache_created or 0)
        metrics["cached_input_tokens"] = cache_read
        metrics["output_tokens"] = _as_int(usage.get("output_tokens"))
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            metrics["reasoning_tokens"] = _as_int(details.get("thinking_tokens"))
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        (model,) = model_usage
        metrics["effective_model"] = model if isinstance(model, str) else None
    return metrics


def _read_envelope(stdout: str, duration: float) -> _Envelope:
    """Unwrap the envelope into the reviewer document text, or a failure category.

    The captured envelope (``tests/_claude_review_helpers.py``) carries the
    document twice: ``structured_output`` (an object, preferred) and
    ``result`` (the same document as a JSON string, the fallback). An
    ``is_error: true`` envelope is a backend-side failure (``runtime_error``,
    which maps to ``CODEX_ERROR``), never a content failure.
    """
    empty = _empty_metrics(duration)
    if not stdout.strip():
        return _Envelope("empty_output", None, None, empty)
    envelope = _json_object(stdout)
    if envelope is None:
        return _Envelope("invalid_json", None, None, empty)
    cost = _cost(envelope)
    metrics = _usage_metrics(envelope, duration)
    if envelope.get("is_error") is True:
        return _Envelope("runtime_error", None, cost, metrics)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return _Envelope(None, json.dumps(structured), cost, metrics)
    result = envelope.get("result")
    if isinstance(result, str):
        return _Envelope(None, result, cost, metrics)
    return _Envelope("missing_output", None, cost, metrics)


def _process_category(result: ClaudeRunResult) -> ExecutorFailureCategory | None:
    """The process-level failure category of *result*, or ``None`` if it exited 0."""
    if result.timed_out:
        return "timeout"
    if _is_spawn_error(result):
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
    # Read before the mutation check, so a mutated role keeps the tokens it spent.
    process_category = _process_category(result)
    envelope = (
        _read_envelope(result.stdout, duration)
        if process_category is None
        else _Envelope(process_category, None, None, _empty_metrics(duration))
    )
    metrics = envelope.metrics
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
    _log.info(
        "claude review role %r finished in %.1fs (total_cost_usd=%s)",
        role,
        duration,
        envelope.cost_usd,
    )
    category = envelope.category
    if category is None and _no_token_usage(metrics):
        _log.warning(
            "claude review role usage absent: role=%r session=%r "
            "(claude -p envelope carries no token usage; output format drift?)",
            role,
            session_id,
        )
    if category is None:
        parsed = _parse_reviewer_document(envelope.document_text)
        if parsed is not None:
            doc, rejected = parsed
            _persist_codex_role_document(
                session_id=session_id, role=role, doc=doc, backend="claude"
            )
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

    Metrics carry duration, the envelope's token counts and its model (#3063);
    the cost field is logged, never persisted (ADR-0021). A G4 mutation stops
    the loop; the roles after it are not run and contribute nothing.
    """
    argv = build_claude_review_argv(
        model=model, schema=json.dumps(ReviewerFindingsDocument.model_json_schema())
    )
    documents: list[ReviewerFindingsDocument] = []
    failures: list[ReviewerRunFailure] = []
    metrics_by_role: dict[str, ReviewerRunMetrics] = {}
    pre_validation_rejected: list[RejectedFinding] = []
    deadline = _role_deadline(wall_clock_budget_seconds)
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
