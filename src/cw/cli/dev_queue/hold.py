"""``cw dev-queue hold``: park a row until a same-repo PR merges (GitHub #2321).

Flow: refuse inside a dispatch worker; read the row and refuse anything
:func:`cw.dev_queue.hold.check_holdable` refuses, before any gh call; verify the
PR with one ``gh pr view`` (outside any lock); take the locked mutation in
:func:`cw.dev_queue.hold.hold_ticket`; register the ``WatchedPr`` only after that
lock is released, and only when the row will actually wait. The serve tick
releases the row on merge (``cw.reconcile.tasks.stale_gate``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import click

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.config import get_client, load_state
from cw.dev_queue.crud import _find_ticket, register_or_adopt_watched_pr
from cw.dev_queue.hold import (
    check_holdable,
    hold_ticket,
    is_operator_hold_on,
)
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError
from cw.gh import fetch_pr_view, github_pr_url, parse_pr_url
from cw.models import WatchedPr
from cw.pr_hydrate import _resolve_repo_slug
from cw.ticket_view import ViewSources
from cw.worktree import _git_dir

from ._group import dev_queue
from .show import echo_resulting_row

if TYPE_CHECKING:
    from pathlib import Path

    from cw.dev_queue.hold import HoldResult
    from cw.models import ClientConfig

# One field set, one gh call: state decides MERGED/CLOSED/OPEN and url names the repo.
_HOLD_PR_FIELDS: Final = "state,url"

_HOLD_IN_WORKER_MSG: Final = (
    "dev-queue hold is an operator verb: a dispatched worker must not park rows"
)


def _unverified(pr_number: int) -> CwError:
    return CwError(
        f"could not verify PR #{pr_number} (gh returned no usable state);"
        " a hold is never accepted unverified"
    )


def _verify_dependency_pr(
    client_cfg: ClientConfig, pr_number: int, git_dir: Path
) -> str:
    """Verify *pr_number* is an OPEN PR in the client's repo; return the slug (#2321).

    Exactly one gh call, made outside any lock. MERGED, CLOSED, unknown and
    cross-repo numbers are all refused, so a hold never waits on a PR that cannot
    be observed to merge.
    """
    view = fetch_pr_view(str(pr_number), fields=_HOLD_PR_FIELDS, cwd=git_dir)
    if view is None:
        raise _unverified(pr_number)
    pr_state = view.get("state")
    if pr_state == "MERGED":
        msg = (
            f"PR #{pr_number} is already merged;"
            " requeue instead (`cw dev-queue requeue`)"
        )
        raise CwError(msg)
    if pr_state == "CLOSED":
        msg = f"PR #{pr_number} is closed unmerged; nothing to wait for"
        raise CwError(msg)
    if pr_state != "OPEN":
        raise _unverified(pr_number)
    slug = _resolve_repo_slug(git_dir)
    if slug is None:
        msg = (
            f"could not resolve the github repo for client {client_cfg.name};"
            " a hold needs the client's origin remote"
        )
        raise CwError(msg)
    url = view.get("url")
    parsed = parse_pr_url(url) if isinstance(url, str) else None
    if parsed is None or parsed[0].lower() != slug.lower() or parsed[1] != pr_number:
        msg = (
            f"PR #{pr_number} is not in client {client_cfg.name}'s repo {slug};"
            " --until-pr-merged takes a same-repo PR number only"
        )
        raise CwError(msg)
    return slug


def _echo_hold(result: HoldResult) -> None:
    """Print the one confirmation line for a non-unchanged hold."""
    ticket, client, pr = result.ticket_id, result.client, result.pr_number
    if result.outcome == "updated" and result.converted_from_worker_park:
        click.echo(
            f"{ticket}: updated (converted from worker park);"
            f" now an operator hold on PR #{pr}"
        )
        return
    if result.outcome == "updated":
        click.echo(
            f"{ticket}: updated, hold moved from PR #{result.previous_pr} to PR #{pr}"
        )
        return
    line = f"Held {ticket} in {client} dev-queue until PR #{pr} merges"
    if result.previous_disposition is not None:
        line += (
            f" (replaced disposition {result.previous_disposition},"
            f" blocked_reason {result.previous_blocked_reason})"
        )
    click.echo(line)


def _register_hold_watch(result: HoldResult, repo: str) -> None:
    """Register the hold's watch after the row is held (#2321 R4, A10).

    A collision here is the narrow race: another client registered this repo and
    PR between the in-lock pre-check and now. The row stays held (no rollback);
    the operator releases it with ``requeue``.
    """
    outcome = register_or_adopt_watched_pr(
        WatchedPr(
            pr_url=github_pr_url(repo, result.pr_number),
            repo=repo,
            pr_number=result.pr_number,
            client=result.client,
            source="dependency_hold",
        )
    )
    if outcome == "collision":
        msg = (
            f"{result.ticket_id} IS held on PR #{result.pr_number},"
            " but its merge cannot be observed: another client already watches"
            f" PR #{result.pr_number} in {repo}. Release it with"
            f" `cw dev-queue requeue {result.ticket_id} -c {result.client}`."
        )
        raise CwError(msg)


@dev_queue.command(name="hold")
@click.argument("ticket")
@click.option(
    "--until-pr-merged",
    "pr_number",
    type=click.IntRange(min=1),
    required=True,
    help="Same-repo PR number this row waits on; released when it merges.",
)
@click.option("--client", "-c", "client", required=True, help="Client name")
@handle_errors
def dev_queue_hold(ticket: str, pr_number: int, client: str) -> None:
    """Hold a row until PR N merges.

    Refuses a row that already has a PR or a live session. Stops no session:
    close a live session first (`cw spawn close`). A worker park with the same
    PR is converted to an operator hold. The serve tick releases the row on
    merge; if the PR closes unmerged the row stays held and a
    session.reap_proposed event names it.
    """
    refuse_inside_dispatch_worker(_HOLD_IN_WORKER_MSG)
    task = _find_ticket(load_dev_queue(), ticket, client, warn=False)
    check_holdable(task, load_state())
    if is_operator_hold_on(task, pr_number):
        click.echo(
            f"{task.ticket_id} already held until PR #{pr_number} merges (unchanged)."
        )
        echo_resulting_row(task.ticket_id, task.client, sources=ViewSources())
        return
    client_cfg = get_client(task.client)
    git_dir = _git_dir(client_cfg)
    repo = _verify_dependency_pr(client_cfg, pr_number, git_dir)
    # bounded=True (#2501): operator command; nothing precedes the lock but
    # read-only state and the one gh call above.
    result = hold_ticket(
        task.ticket_id, task.client, pr_number, repo=repo, bounded=True
    )
    if result.outcome != "unchanged":
        _register_hold_watch(result, repo)
    _echo_hold(result)
    echo_resulting_row(result.ticket_id, result.client, sources=ViewSources())
