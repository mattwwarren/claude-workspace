"""Row mutation for ``cw dev-queue recover-red-pr`` (GitHub #2945, RFC 0016 B2).

The lock-free reads (live PR head, failing required checks, the shared
``synthesize_pr_feedback`` report, the auto-merge disarm) happen above this
layer in ``cw.shipped_pr_recovery`` (``cw.pr_feedback`` imports
``cw.pr_hydrate``, which imports ``cw.dev_queue``, so nothing here may import
it). This module owns only the pure refusal rules and the locked row write:
a COMPLETED shipped row regresses to IMPL through ``_stage_regress`` (which
revokes the review approval, so the pipeline passes REVIEW again) with the
head-pinned CI-failure feedback attached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue._requeue_messages import _requeue_result
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.lifecycle import _stage_regress
from cw.dev_queue.rearm_owed import mark_auto_merge_rearm_owed
from cw.dev_queue.requeue import _refuse_if_live_session
from cw.dev_queue.shipped_red import SHIPPED_DISPOSITION
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import RequeueStateError
from cw.gh import parse_pr_url
from cw.models import CLAUDE_NATIVE_BACKEND, QueueItemStatus, Stage
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import ReviewFeedback, TicketTask


class RedPrRecovery(NamedTuple):
    """The lock-free read bundle the locked row write consumes."""

    pr_url: str
    repo: str
    pr_number: int
    head_sha: str
    checks: tuple[str, ...]
    feedback: ReviewFeedback
    was_armed: bool
    # The lane pipeline and IMPL backend the prepare step resolved (static
    # client config, so the locked step re-checks the row against the same).
    stages: tuple[Stage, ...]
    backend: str
    lane: str


def not_completed_message(task: TicketTask) -> str:
    """The refusal for a row that is not COMPLETED."""
    return (
        f"row is not COMPLETED (status: {task.status.value}); recover-red-pr"
        " only applies to a shipped PR"
    )


def disarmed_row_unchanged_message(pr_url: str, cause: str) -> str:
    """The refusal when the locked write fails after the PR was disarmed."""
    return (
        f"auto-merge on {pr_url} is already disarmed but the row was not"
        f" changed ({cause}); finalize re-arms it after review, so a retry is"
        " safe once the cause is gone"
    )


def red_pr_refusal(
    task: TicketTask, stages: Sequence[Stage], backend: str
) -> str | None:
    """Why *task* cannot be recovered, or None. Pure; shared by both steps.

    REVIEW must be in the pipeline: the "a fix is re-reviewed before it can
    merge" guarantee rests on the recovered commit passing REVIEW. A
    non-claude-native IMPL executor ignores ``review_feedback`` on a
    non-adopted row (#2983), so it is refused.
    """
    checks = (
        (task.status is not QueueItemStatus.COMPLETED, not_completed_message(task)),
        (
            task.disposition != SHIPPED_DISPOSITION,
            f"row disposition is {task.disposition!r}, not 'shipped';"
            " recover-red-pr only applies to a shipped PR",
        ),
        (
            not task.pr_url or parse_pr_url(task.pr_url) is None,
            "row has no parseable GitHub pr_url",
        ),
        (
            Stage.IMPL not in stages,
            "the lane pipeline has no impl stage to recover into",
        ),
        (
            Stage.REVIEW not in stages,
            "the lane pipeline has no review stage, so a recovered commit would"
            " reach finalize without a re-review",
        ),
        (
            backend != CLAUDE_NATIVE_BACKEND,
            f"the impl executor backend is {backend!r}; only claude-native"
            " delivers the recovery feedback (opencode and local ignore it,"
            " see #2983)",
        ),
    )
    return next((message for failed, message in checks if failed), None)


def recover_red_pr_ticket(
    ticket_id: str,
    client_name: str,
    recovery: RedPrRecovery,
    *,
    native_daemon: NativeDaemonClient | None = None,
) -> dict[str, str | bool | int]:
    """Regress the COMPLETED row to IMPL carrying *recovery*'s feedback.

    Runs the live-session guard outside the lock, then re-finds and
    re-validates the row under it. A refusal here happens AFTER the PR was
    disarmed, so its message says so (a retry is safe).
    """
    _refuse_if_live_session(
        ticket_id,
        client_name,
        native_daemon=native_daemon or get_native_daemon_client(),
    )
    # Unbounded: the PR was already disarmed, so a timed-out bounded acquisition
    # here would orphan that side effect (see test_config's bounded allowlist).
    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        cause: str | None = None
        if task.status is not QueueItemStatus.COMPLETED:
            cause = "row is not COMPLETED"
        elif task.pr_url != recovery.pr_url:
            cause = "pr_url changed"
        elif task.lane != recovery.lane:
            cause = "lane changed"
        else:
            cause = red_pr_refusal(task, recovery.stages, recovery.backend)
        if cause is not None:
            if recovery.was_armed and mark_auto_merge_rearm_owed(
                task, expected_pr_url=recovery.pr_url
            ):
                # Disarm succeeded before this locked revalidation refused the
                # row write. Keep the refusal, but persist the obligation when
                # the row still points at that exact PR.
                save_dev_queue(store)
            raise RequeueStateError(
                disarmed_row_unchanged_message(recovery.pr_url, cause)
            )
        from_stage = task.stage
        _stage_regress(task, Stage.IMPL)
        # ``_stage_regress`` clears the PR URL for non-adopted rows; this
        # recovery still owns the shipped PR and must keep it hydratable.
        task.pr_url = recovery.pr_url
        task.review_feedback = recovery.feedback
        task.red_pr_recovery_sha = recovery.feedback.reviewed_sha
        task.pr_state = None
        if recovery.was_armed:
            # The prepare step only returns after GitHub read-back confirms
            # the disarm. Stamp with the validated row regression.
            mark_auto_merge_rearm_owed(task, expected_pr_url=recovery.pr_url)
        save_dev_queue(store)
    return _requeue_result(task, from_stage, regressed=True)
