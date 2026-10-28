"""``cw review confirm`` -- the independent confirmation round (#2787, #2791).

Converge-to-clean review mode (``review.convergence: clean``) does not record a
clean ``confirm`` round on the loop's own say-so: an independent round must
also come back clean. This command runs that round, one full review pass with
no fix loop, over a dev-queue row's worktree and reports the outcome in an
exit-code contract the prose obeys (R19, R26, R29). ``--backend`` picks the
reviewer: ``codex`` (the default, for a Claude-native loop) or ``claude``
(``claude -p`` per role, for a codex loop, #2791). The contract is the same
for both:

- ``0``: the round ran to a verdict. NOT a clean signal by itself (R29):
  ``result_blocker_reason`` decides what the caller may record.
- ``3``: the backend is down and nothing else is. Causes ``binary_missing``,
  ``backend_error`` (a non-zero exit or error envelope from the backend
  itself, no document from any role) and ``crash`` (the review raised
  ``OSError``/``CwError``).
- ``4``: the round ran, or was attempted, on a healthy backend and failed on
  content. Causes ``partial``, ``reviewers_failed``, ``worktree_mutated`` and
  ``discarded_findings`` (a reviewer's document failed structurally while
  claiming MUST_FIX or SHOULD_FIX findings, so the findings are gone).
- ``2``: a Click usage error, including an unknown ``--backend``.
- ``1``: a ``CwError`` (unknown ticket, unresolvable client) or an uncaught
  exception: a cw or invocation fault, never reported as an outage.

The command imposes no deadline of its own on the review (ADR-0014): a slow
round is a queue-peek signal, never a kill and never an outage.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, NoReturn

import click

from cw.cli._base import handle_errors
from cw.config import get_client
from cw.dev_queue import load_dev_queue
from cw.dev_queue.crud import _find_ticket
from cw.exceptions import CwError
from cw.models.orchestrator_config.stage import StageExecutorConfig
from cw.review_convergence import LEDGER_RELATIVE_PATH, load_ledger, worktree_state
from cw.worktree import resolve_task_worktree

from ._group import review

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.models import TicketTask
    from cw.review_findings import CapturedDiff, ReviewVerdict

#: Exit code of a confirm round whose backend is down (R19). A code of its own:
#: exit 1 is shared with ``handle_errors`` and Python's uncaught-exception exit.
CONFIRM_UNAVAILABLE_EXIT_CODE = 3
#: Exit code of a round that ran or was attempted on a healthy backend and
#: failed on content (R26, R29). Never recorded on the ledger.
CONFIRM_ROUND_FAILED_EXIT_CODE = 4

_CODEX = "codex"
_CLAUDE = "claude"
_BACKENDS = (_CODEX, _CLAUDE)
#: ``_format_failures_detail`` appends this pointer after the ``role (reason)``
#: pairs; the reasons are read from the text before it.
_DIAGNOSTICS_MARKER = " [diagnostics:"
_REASON_PATTERN = re.compile(r"\(([a-z_]+)\)")
_CALL_MUTATED_DETAIL = "the worktree HEAD or git status changed during the review call"


class _RoundOutcome(NamedTuple):
    """One round's result and verdict.

    ``backend_down`` is the Claude round's own per-role classification;
    ``None`` on the codex path, which reads it from the failure summary.
    """

    result: AutoDevResult
    verdict: ReviewVerdict | None
    backend_down: bool | None


def _default_reasoning_effort() -> str | None:
    """The codex default effort ("high", #1711), or ``None`` if it is unpinned.

    ``StageExecutorConfig.reasoning_effort`` is ``ReasoningEffort | None``, so
    the ``.value`` read is narrowed explicitly. ``None`` leaves codex's own
    config in force. Read at call time, not import time.
    """
    default = StageExecutorConfig().reasoning_effort
    return default.value if default is not None else None


def _exit_with(payload: dict[str, object], code: int) -> NoReturn:
    """Print *payload* as JSON, then exit *code* (the ``check-voided`` shape)."""
    click.echo(json.dumps(payload, indent=2))
    raise click.exceptions.Exit(code)


def _unavailable(cause: str, detail: str, backend: str) -> NoReturn:
    """Exit 3: the backend is down. The only uncharged class."""
    _exit_with(
        {"available": False, "backend": backend, "cause": cause, "detail": detail},
        CONFIRM_UNAVAILABLE_EXIT_CODE,
    )


def _round_failed(cause: str, detail: str, backend: str) -> NoReturn:
    """Exit 4: a content-level failure on a healthy backend."""
    _exit_with(
        {
            "available": True,
            "backend": backend,
            "round_failed": True,
            "cause": cause,
            "detail": detail,
        },
        CONFIRM_ROUND_FAILED_EXIT_CODE,
    )


def _confirmation_base(*, worktree: Path, ticket_id: str, default_branch: str) -> str:
    """Use the convergence ledger's fork point when one is available.

    The independent round must review the same fork-point-anchored change set
    as the loop. Direct command tests may run without a ledger, so retain the
    explicit/client branch fallback for that non-convergence invocation shape.
    """
    path = worktree / LEDGER_RELATIVE_PATH
    try:
        ledger = load_ledger(path)
    except (OSError, ValueError) as exc:
        msg = f"cannot read the convergence ledger at {path}: {exc}"
        raise CwError(msg) from exc
    if ledger is None:
        return default_branch
    if ledger.ticket_id != ticket_id:
        msg = (
            f"convergence ledger at {path} belongs to ticket "
            f"{ledger.ticket_id!r}, not {ticket_id!r}"
        )
        raise CwError(msg)
    return ledger.fork_point


def _session_id(task: TicketTask) -> str:
    from cw.claude_review import CONFIRM_SESSION_ID_PREFIX

    return f"{CONFIRM_SESSION_ID_PREFIX}{task.session_id or task.ticket_id}"


def _run_round(
    *,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    base_ref: str,
    model: str | None,
    reasoning_effort: str | None,
) -> _RoundOutcome:
    """One codex review pass, no fix loop and no cw-imposed deadline (R20).

    ``cw.codex_review`` and ``RealCodexRunner`` are imported here, not at module
    scope: ``cw.cli.review`` is imported on every ``cw`` invocation, and a
    function-local name is the seam tests monkeypatch. The ``try`` wraps only
    ``run_review``: argument handling, the row and client lookups and the runner
    construction sit outside it, so none of them can be reported as an outage.
    """
    from cw.codex_review import run_review
    from cw.codex_review._diff import _capture_diff
    from cw.codex_runner import RealCodexRunner

    runner = RealCodexRunner()
    # ``run_review`` uses its branch argument both for the review diff and for
    # origin/<branch> scope measurement. Keep those concerns separate here:
    # capture the confirmation diff from the convergence ledger's fork point,
    # while the review pipeline still receives the real client branch name for
    # scope and diagnostics. The shared pipeline has no separate base-ref
    # parameter yet, so this narrow command-boundary adapter supplies it.
    from cw.codex_review._context import core as review_context

    capture_diff = _capture_diff

    def capture_from_base(
        capture_worktree: Path, _ignored_branch: str
    ) -> tuple[CapturedDiff, str, list[str]]:
        return capture_diff(capture_worktree, base_ref)

    review_context.__dict__["_capture_diff"] = capture_from_base
    try:
        result, verdict = run_review(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=None,
            session_id=_session_id(task),
            fix_loop_enabled=False,
        )
    except (OSError, CwError) as exc:
        _unavailable("crash", f"{type(exc).__name__}: {exc}", _CODEX)
    finally:
        review_context.__dict__["_capture_diff"] = capture_diff
    return _RoundOutcome(result, verdict, None)


def _run_claude_round(
    *,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    base_ref: str,
    model: str | None,
) -> _RoundOutcome:
    """One Claude review pass (#2791), diffed from *base_ref*, no deadline.

    Deferred imports for the same reason as :func:`_run_round`, and the same
    narrow ``try``: only ``run_claude_review`` itself can be a ``crash``.
    ``run_claude_review`` takes the diff base directly, so no capture adapter
    is needed, and it classifies ``backend_down`` per role itself.
    """
    from cw.claude_review import RealClaudeRunner, run_claude_review

    runner = RealClaudeRunner()
    try:
        round_ = run_claude_review(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            base_ref=base_ref,
            model=model,
            wall_clock_budget_seconds=None,
            session_id=_session_id(task),
        )
    except (OSError, CwError) as exc:
        _unavailable("crash", f"{type(exc).__name__}: {exc}", _CLAUDE)
    return _RoundOutcome(round_.result, round_.verdict, round_.backend_down)


def _failure_reasons(details: str) -> set[str]:
    """The per-role reasons in a ``role (reason); ...`` failure summary."""
    summary = details.split(_DIAGNOSTICS_MARKER, 1)[0]
    return set(_REASON_PATTERN.findall(summary))


def _fail_without_verdict(
    details: str, *, backend: str, backend_down: bool | None
) -> NoReturn:
    """No reviewer produced a document: split backend-down from content failure.

    Every failed role a backend-side reason (a non-zero exit, a capacity
    ``turn.failed``, an error envelope) means nothing says the backend is up:
    exit 3. Any output that could not be parsed, or any other reason, means
    the backend ran: exit 4. The Claude round hands in its own per-role
    classification; the codex round's is read from the failure summary.
    """
    from cw.codex_review import CODEX_ERROR, CODEX_MODEL_CAPACITY

    if backend_down is None:
        reasons = _failure_reasons(details)
        backend_down = bool(reasons) and reasons <= {CODEX_ERROR, CODEX_MODEL_CAPACITY}
    if backend_down:
        _unavailable("backend_error", details, backend)
    _round_failed("reviewers_failed", details, backend)


def _discard_tally(severities: dict[str, int]) -> str:
    return ", ".join(
        f"{severity} x{count}" for severity, count in sorted(severities.items())
    )


def _discard_detail(verdict: ReviewVerdict) -> str:
    """The ``role (reason): <severity tally>`` pairs of the discarding failures."""
    return "; ".join(
        f"{failure.role} ({failure.reason}): "
        f"{_discard_tally(failure.discarded_finding_severities)}"
        for failure in verdict.run_failures_with_should_fix_discards
    )


def _fail_failed_round(
    reason: str | None, verdict: ReviewVerdict, details: str, backend: str
) -> None:
    """Exit 4 for the two verdict-carrying failures; return for every other verdict.

    A discarded-findings round is exit 4, never 0: an exit 0 would hand the
    caller a verdict that looks clean precisely because the lost findings are
    not in it (R29). ``codex_review_partial`` is reached only when no block
    reason applies, so a failed reviewer plus a MUST_FIX finding carries the
    MUST_FIX reason and stays exit 0.
    """
    from cw.codex_review import (
        CODEX_REVIEW_PARTIAL,
        CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS,
    )

    if reason == CODEX_REVIEW_PARTIAL:
        _round_failed("partial", details, backend)
    if reason == CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS:
        _round_failed("discarded_findings", _discard_detail(verdict), backend)


def _resolve_round_inputs(
    *, ticket_id: str, client: str, worktree: Path, default_branch: str | None
) -> tuple[TicketTask, str]:
    """The row and the configured branch, after the worktree ownership checks.

    Every check here is a ``CwError`` (exit 1), never an outage.
    """
    client_config = get_client(client)
    task = _find_ticket(load_dev_queue(), ticket_id, client)
    expected_worktree = resolve_task_worktree(task, client_config)
    if expected_worktree is None:
        msg = f"Cannot resolve the worktree for ticket '{ticket_id}'"
        raise CwError(msg)
    if worktree.resolve() != expected_worktree.resolve():
        msg = (
            f"Worktree '{worktree}' does not belong to ticket '{ticket_id}'; "
            f"expected '{expected_worktree}'"
        )
        raise CwError(msg)
    return task, default_branch or client_config.default_branch


def _binary_present(backend: str) -> bool:
    if backend == _CLAUDE:
        from cw.claude_review import _which_claude

        return _which_claude() is not None
    return shutil.which(_CODEX) is not None


def _finish_round(
    *,
    backend: str,
    worktree: Path,
    before: tuple[str, str],
    outcome: _RoundOutcome,
) -> None:
    """Map a completed round onto exit 4, exit 3, or the exit-0 verdict print.

    A worktree that changed across the whole call (either backend), or a
    Claude round that reported a per-role mutation (G4), is ``worktree_mutated``.
    """
    from cw.codex_review import CODEX_REVIEW_WORKTREE_MUTATED

    if worktree_state(worktree) != before:
        _round_failed("worktree_mutated", _CALL_MUTATED_DETAIL, backend)
    blocker = outcome.result.blocker
    reason = blocker.reason if blocker is not None else None
    details = blocker.details if blocker is not None else ""
    if reason == CODEX_REVIEW_WORKTREE_MUTATED:
        _round_failed("worktree_mutated", details, backend)
    verdict = outcome.verdict
    if verdict is None:
        _fail_without_verdict(
            details, backend=backend, backend_down=outcome.backend_down
        )
    _fail_failed_round(reason, verdict, details, backend)
    click.echo(
        json.dumps(
            {
                "available": True,
                "backend": backend,
                "reviewed_sha": verdict.reviewed_sha,
                "result_blocker_reason": reason,
                "verdict": verdict.model_dump(mode="json"),
            },
            indent=2,
        )
    )


@review.command(name="confirm")
@click.option(
    "--ticket-id", required=True, help="Ticket whose dev-queue row to review."
)
@click.option("--client", "-c", required=True, help="Client the ticket belongs to.")
@click.option(
    "--worktree",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Worktree holding the branch under review.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Branch the diff is taken against. Default: the client's default branch.",
)
@click.option(
    "--backend",
    type=click.Choice(_BACKENDS),
    default=_CODEX,
    show_default=True,
    help=(
        "Reviewer backend: codex (codex exec per role) or claude (claude -p "
        "per role, read-only tools, the independent confirm for a codex loop)."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Reviewer model. Default: the backend's own configured model.",
)
@click.option(
    "--reasoning-effort",
    default=None,
    help=(
        "Codex reasoning effort. Default: the codex default (high). "
        "Codex only; ignored with --backend claude."
    ),
)
@handle_errors
def review_confirm(
    ticket_id: str,
    client: str,
    worktree: Path,
    default_branch: str | None,
    backend: str,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    """Run one independent review round for TICKET (#2787, #2791).

    Runs a full review pass, with no fix loop, over --worktree on --backend
    (codex by default) and prints JSON on stdout. Exit codes:

    0: the round ran to a verdict; prints {"available": true, "backend":
    <backend>, "reviewed_sha": ..., "result_blocker_reason": ..., "verdict":
    {...}}. A verdict is NOT a clean signal: result_blocker_reason decides what
    may be recorded.

    3: the backend is down (causes binary_missing, backend_error, crash);
    prints {"available": false, "backend": <backend>, "cause": ..., "detail":
    ...}. The only exit that means "backend unavailable".

    4: the round failed on a healthy backend (causes partial,
    reviewers_failed, worktree_mutated, discarded_findings); prints
    {"available": true, "backend": <backend>, "round_failed": true, "cause":
    ..., "detail": ...}. Nothing is recorded.

    2: a usage error (missing or unknown option or backend). 1: a cw error
    (unknown ticket, unresolvable client) or an uncaught exception.

    The command reads the row and posts nothing: no tracker comment, no
    dev-queue status or stage change, no session events. The review runs with
    no deadline imposed by cw.
    """
    task, configured_base = _resolve_round_inputs(
        ticket_id=ticket_id,
        client=client,
        worktree=worktree,
        default_branch=default_branch,
    )
    if not _binary_present(backend):
        _unavailable(
            "binary_missing", f"{backend} executable not found on PATH", backend
        )
    before = worktree_state(worktree)
    confirmation_base = _confirmation_base(
        worktree=worktree,
        ticket_id=ticket_id,
        default_branch=configured_base,
    )
    if backend == _CLAUDE:
        outcome = _run_claude_round(
            task=task,
            worktree=worktree,
            default_branch=configured_base,
            base_ref=confirmation_base,
            model=model,
        )
    else:
        outcome = _run_round(
            task=task,
            worktree=worktree,
            default_branch=configured_base,
            base_ref=confirmation_base,
            model=model,
            reasoning_effort=(
                reasoning_effort
                if reasoning_effort is not None
                else _default_reasoning_effort()
            ),
        )
    _finish_round(backend=backend, worktree=worktree, before=before, outcome=outcome)
