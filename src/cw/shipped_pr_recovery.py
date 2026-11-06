"""Lock-free preparation for ``cw dev-queue recover-red-pr`` (GitHub #2945).

Reads the live PR (head, failing required checks), builds head-pinned
CI-failure feedback with the shared :func:`cw.pr_feedback.synthesize_pr_feedback`
and, last, disarms auto-merge with a read-back. It sits above ``cw.dev_queue``
(``cw.pr_feedback`` imports ``cw.pr_hydrate``, which imports ``cw.dev_queue``)
and hands the locked row write (``cw.dev_queue.red_pr_recovery``) one
:class:`~cw.dev_queue.red_pr_recovery.RedPrRecovery` bundle.

Disarm is the last step, so no earlier refusal ever leaves a PR disarmed.
Every refusal raises :class:`~cw.exceptions.RequeueStateError` before any
mutation of the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from cw import gh
from cw.dev_queue.red_pr_recovery import RedPrRecovery, red_pr_refusal
from cw.exceptions import RequeueStateError
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.gh import _GH_PR_STATE_OPEN, parse_pr_url
from cw.models import Stage
from cw.pr_feedback import FeedbackSynthesisError, synthesize_pr_feedback
from cw.pr_unarm import ensure_pr_unarmed
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask


def _refuse(message: str) -> NoReturn:
    raise RequeueStateError(message)


def _live_head_and_checks(pr_url: str, cwd: Path) -> tuple[gh.PrHead, tuple[str, ...]]:
    """The OPEN PR's head and its failing required check names, or a refusal."""
    head = gh.fetch_pr_head(pr_url, cwd=cwd)
    if head is None:
        msg = f"could not read the PR head for {pr_url}; the row is unchanged"
        _refuse(msg)
    if head.state != _GH_PR_STATE_OPEN:
        msg = f"{pr_url} is {head.state}, not OPEN; nothing to recover"
        _refuse(msg)
    required = gh.fetch_required_failing_checks(pr_url, cwd=cwd)
    if required is None:
        msg = f"could not read the required checks for {pr_url}; the row is unchanged"
        _refuse(msg)
    if len(required) == 0:
        msg = f"no required check is failing on {pr_url}; nothing to recover"
        _refuse(msg)
    return head, tuple(c.name for c in required)


def prepare_red_pr_recovery(
    task: TicketTask, client_cfg: ClientConfig
) -> RedPrRecovery:
    """Read, synthesize and disarm; return the bundle the row write consumes."""
    stages = resolve_pipeline_stages(task, client_cfg)
    backend = resolve_executor_config(Stage.IMPL, task, client_cfg).backend
    refusal = red_pr_refusal(task, stages, backend)
    if refusal is not None:
        _refuse(refusal)
    pr_url = task.pr_url or ""
    parsed = parse_pr_url(pr_url)
    if parsed is None:  # pragma: no cover - red_pr_refusal rejects this first
        _refuse("row has no parseable GitHub pr_url")
    repo, number = parsed
    cwd = _git_dir(client_cfg)
    head, checks = _live_head_and_checks(pr_url, cwd)
    try:
        feedback = synthesize_pr_feedback(repo, number, cwd=cwd)
    except FeedbackSynthesisError as exc:
        _refuse(f"could not synthesize CI-failure feedback: {exc}")
    if feedback.reviewed_sha != head.oid:
        msg = (
            f"the PR head moved while reading ({head.oid[:12]} vs"
            f" {feedback.reviewed_sha[:12]}); the row is unchanged, retry"
        )
        _refuse(msg)
    outcome = ensure_pr_unarmed(pr_url, cwd=cwd, head=head)
    if not outcome.ok:
        msg = (
            f"could not verify that auto-merge is disarmed on {pr_url}; the row"
            f" is unchanged. Run gh pr merge {number} --disable-auto and retry"
        )
        _refuse(msg)
    return RedPrRecovery(
        pr_url=pr_url,
        repo=repo,
        pr_number=number,
        head_sha=head.oid,
        checks=checks,
        feedback=feedback,
        was_armed=head.auto_merge_armed,
        stages=tuple(stages),
        backend=backend,
    )
