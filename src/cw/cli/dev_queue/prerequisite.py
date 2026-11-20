"""``cw dev-queue prerequisite``: a verified merge prerequisite (GitHub #3791).

Flow: refuse inside a dispatch worker; refuse an ``owner/repo#N`` ref (same
repo only; cross-repo waits are ``cw dev-queue hold --until-pr-merged``); read
the row. Declare then refuses a stale or unverifiable dispatch loop (its next
save would drop the field, and it lacks the claim gate), refuses a terminal
row, short-circuits a PR already listed before any gh call, verifies the PR
OPEN and in the client's repo with one ``gh pr view`` outside any lock, and
appends the entry under the queue lock
(:func:`cw.dev_queue.merge_prerequisites.declare_prerequisite`). Remove makes
no gh call and passes no loop guard: removal is the fail-open direction.

The verb registers no ``WatchedPr`` and records no event: the claim gate parks
the row and records ``ticket.held``, and the stale-dispatch registrar watches
the PR from that park.
"""

from __future__ import annotations

import getpass
from typing import TYPE_CHECKING, Final

import click

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.config import get_client
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.merge_prerequisites import (
    check_declarable,
    declare_prerequisite,
    has_prerequisite,
    remove_prerequisite,
)
from cw.dev_queue.storage import load_dev_queue
from cw.ticket_view import ViewSources
from cw.worktree import _git_dir

from ._group import dev_queue
from ._loop_warning import require_dispatch_loop_safe
from .hold import PrRefParam, verify_dependency_pr
from .show import echo_resulting_row

if TYPE_CHECKING:
    from cw.dev_queue.merge_prerequisites import PrerequisiteResult
    from cw.models import TicketTask
    from cw.models.pr_ref import PrRef

_IN_WORKER_MSG: Final = (
    "dev-queue prerequisite is an operator verb: a dispatched worker must not gate rows"
)

_CROSS_REPO_MSG: Final = (
    "--pr takes a same-repo PR number; to wait on a PR in another repo use"
    " `cw dev-queue hold --until-pr-merged owner/repo#N`"
)


def _echo_removed(result: PrerequisiteResult) -> None:
    line = f"Removed merge prerequisite PR #{result.pr_number} from {result.ticket_id}"
    if result.released:
        line += "; released the dependency hold (the row is PENDING)"
    elif result.restamped_to is not None:
        line += f"; the hold now waits on PR #{result.restamped_to}"
    click.echo(line)


def _run_remove(task: TicketTask, ref: PrRef) -> None:
    # bounded=True (#2501): operator command; nothing precedes the lock but
    # read-only state. No gh call and no loop guard (removal fails open).
    result = remove_prerequisite(task.ticket_id, task.client, ref, bounded=True)
    _echo_removed(result)


def _echo_unchanged(ticket_id: str, number: int) -> None:
    click.echo(f"{ticket_id} already has merge prerequisite PR #{number} (unchanged).")


def _run_declare(task: TicketTask, ref: PrRef) -> None:
    require_dispatch_loop_safe("a merge prerequisite")
    check_declarable(task)
    if has_prerequisite(task, ref):
        _echo_unchanged(task.ticket_id, ref.number)
        return
    client_cfg = get_client(task.client)
    repo, ref = verify_dependency_pr(client_cfg, ref, _git_dir(client_cfg), None)
    # bounded=True (#2501): operator command; nothing precedes the lock but
    # read-only state, the stale-loop guard and the one gh read above.
    result = declare_prerequisite(
        task.ticket_id,
        task.client,
        ref,
        repo=repo,
        actor=getpass.getuser(),
        bounded=True,
    )
    if result.outcome == "unchanged":  # declared by a racing call meanwhile
        _echo_unchanged(task.ticket_id, ref.number)
        return
    click.echo(
        f"Recorded merge prerequisite PR #{ref.number} for {task.ticket_id} in"
        f" {task.client} ({repo}): no stage dispatches until it merges"
    )


@dev_queue.command(name="prerequisite")
@click.argument("ticket")
@click.option("--client", "-c", "client", required=True, help="Client name")
@click.option(
    "--pr",
    "pr_ref",
    type=PrRefParam(),
    required=True,
    help="Same-repo PR number that must merge before the row dispatches.",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove this PR from the row's prerequisites (no gh call).",
)
@handle_errors
def dev_queue_prerequisite(
    ticket: str, client: str, pr_ref: PrRef, *, remove: bool
) -> None:
    """Hold every stage dispatch of a row until a same-repo PR merges.

    An operator-only verb. The PR is verified OPEN and in the client's repo
    with one gh call at declaration; nothing is derived from review findings,
    comments or the ticket body. The row's status is not changed now: at its
    next claim the row is parked as a dependency_hold on the PR (no attempt
    charged, no lane slot held), and the serve loop releases it when the PR
    merges. A PR that closes unmerged leaves the row held. Cross-repo PRs are
    refused: use `cw dev-queue hold --until-pr-merged owner/repo#N`. --remove
    drops a prerequisite and releases or moves a hold parked on it.
    """
    refuse_inside_dispatch_worker(_IN_WORKER_MSG)
    if pr_ref.repo is not None:
        raise click.UsageError(_CROSS_REPO_MSG)
    task = _find_ticket(load_dev_queue(), ticket, client, warn=False)
    if remove:
        _run_remove(task, pr_ref)
    else:
        _run_declare(task, pr_ref)
    echo_resulting_row(task.ticket_id, task.client, sources=ViewSources())
