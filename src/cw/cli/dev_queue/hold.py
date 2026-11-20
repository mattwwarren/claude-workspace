"""``cw dev-queue hold``: park a row until a PR merges (GitHub #2321, #3634).

Flow: require exactly one of ``--until-pr-merged`` (``N`` or ``owner/repo#N``)
and ``--until-ticket-merged`` (resolved once, from the store, to that ticket's
recorded PR); refuse inside a dispatch worker; read the row and refuse anything
:func:`cw.dev_queue.hold.check_holdable` refuses, before any gh call; for an
explicit or ticket-sourced ref resolve the client's slug (git only) before any
gh call; verify the PR with one ``gh pr view`` (outside any lock); take the
locked mutation in
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
    parse_github_pull_url,
    record_hold_event,
    resolve_ticket_pr,
)
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError
from cw.gh import (
    _GH_PR_STATE_CLOSED,
    _GH_PR_STATE_MERGED,
    _GH_PR_STATE_OPEN,
    fetch_pr_view,
    github_pr_url,
)
from cw.models import WatchedPr
from cw.models.enums import WATCHED_PR_SOURCE_DEPENDENCY_HOLD
from cw.models.pr_ref import PrRef
from cw.pr_hydrate import _resolve_repo_slug
from cw.ticket_view import ViewSources
from cw.worktree import _git_dir

from ._group import dev_queue
from .show import echo_resulting_row

if TYPE_CHECKING:
    from pathlib import Path

    from cw.dev_queue.hold import HoldResult
    from cw.models import ClientConfig, DevQueueStore, TicketTask

# One field set, one gh call: state decides MERGED/CLOSED/OPEN and url names the repo.
_HOLD_PR_FIELDS: Final = "state,url"

_HOLD_IN_WORKER_MSG: Final = (
    "dev-queue hold is an operator verb: a dispatched worker must not park rows"
)

_EXACTLY_ONE_MSG: Final = (
    "give exactly one of --until-pr-merged or --until-ticket-merged"
)


class _PrRefParam(click.ParamType):
    """``--until-pr-merged``: a same-repo number (``9``) or ``owner/repo#N``."""

    name = "pr_ref"

    def convert(
        self, value: object, param: click.Parameter | None, ctx: click.Context | None
    ) -> PrRef:
        if isinstance(value, PrRef):
            return value
        ref = PrRef.parse(value) if isinstance(value, str) else None
        if ref is None:
            self.fail(
                f"{value!r} is not a PR reference; use a positive number (9)"
                " or owner/repo#N (acme/widgets#9)",
                param,
                ctx,
            )
        return ref


def _unverified(ref: PrRef) -> CwError:
    return CwError(
        f"could not verify PR {ref.label()} (gh returned no usable state);"
        " a hold is never accepted unverified"
    )


def _unresolved_slug(client: str) -> CwError:
    return CwError(
        f"could not resolve the github repo for client {client};"
        " a hold needs the client's origin remote"
    )


def _check_open(view: dict[str, object] | None, ref: PrRef) -> dict[str, object]:
    """Refuse unless gh reported *ref* OPEN; return the view."""
    if view is None:
        raise _unverified(ref)
    pr_state = view.get("state")
    if pr_state == _GH_PR_STATE_MERGED:
        msg = (
            f"PR {ref.label()} is already merged;"
            " requeue instead (`cw dev-queue requeue`)"
        )
        raise CwError(msg)
    if pr_state == _GH_PR_STATE_CLOSED:
        msg = f"PR {ref.label()} is closed unmerged; nothing to wait for"
        raise CwError(msg)
    if pr_state != _GH_PR_STATE_OPEN:
        raise _unverified(ref)
    return view


def _view_pr(view: dict[str, object]) -> tuple[str, int] | None:
    url = view.get("url")
    return parse_github_pull_url(url) if isinstance(url, str) else None


def _verify_dependency_pr(
    client_cfg: ClientConfig, ref: PrRef, git_dir: Path, slug: str | None
) -> tuple[str, PrRef]:
    """Verify *ref* is an OPEN PR; return ``(watch repo, persisted ref)`` (#2321).

    Exactly one gh call, made outside any lock: the bare number for a same-repo
    ref, the PR url for an explicit one. MERGED, CLOSED, unknown and mismatched
    PRs are all refused, so a hold never waits on a PR that cannot be observed
    to merge. *slug* is the client's already-resolved slug for an explicit or
    ticket-sourced ref and ``None`` for a bare number, which resolves it after
    the gh call (R9). An explicit ref is rebuilt with gh's canonical-case repo.
    """
    if ref.repo is not None:
        target = github_pr_url(ref.repo, ref.number)
        view = _check_open(
            fetch_pr_view(target, fields=_HOLD_PR_FIELDS, cwd=git_dir), ref
        )
        parsed = _view_pr(view)
        if parsed is None:
            raise _unverified(ref)
        if parsed[0].lower() != ref.repo.lower() or parsed[1] != ref.number:
            msg = (
                f"gh resolved PR {ref.label()} to {parsed[0]}#{parsed[1]};"
                " a hold is never accepted on a mismatch"
            )
            raise CwError(msg)
        return parsed[0], PrRef(repo=parsed[0], number=ref.number)
    view = _check_open(
        fetch_pr_view(str(ref.number), fields=_HOLD_PR_FIELDS, cwd=git_dir), ref
    )
    if slug is None:
        slug = _resolve_repo_slug(git_dir)
    if slug is None:
        raise _unresolved_slug(client_cfg.name)
    parsed = _view_pr(view)
    if parsed is None or parsed[0].lower() != slug.lower() or parsed[1] != ref.number:
        msg = (
            f"PR #{ref.number} is not in client {client_cfg.name}'s repo {slug};"
            " name a PR in another repo as owner/repo#N"
        )
        raise CwError(msg)
    return slug, ref


def _resolve_hold_ref(
    task: TicketTask,
    store: DevQueueStore,
    pr_ref: PrRef | None,
    dep_ticket: str | None,
) -> tuple[PrRef, str | None, str | None]:
    """Return ``(ref, via_ticket, slug)`` for the requested hold (#3634).

    A ticket is resolved from *store* (no gh). An explicit or ticket-sourced
    ref resolves the client's slug (git only) before any gh call and is
    normalised to a same-repo ref when it names the client's own repo; a bare
    number resolves nothing here (``slug`` is ``None``).
    """
    via_ticket: str | None = None
    if dep_ticket is not None:
        ref, via_ticket = resolve_ticket_pr(
            store, dep_ticket, task.ticket_id, task.client
        )
    elif pr_ref is not None:
        ref = pr_ref
    else:
        raise click.UsageError(_EXACTLY_ONE_MSG)
    if ref.repo is None:
        return ref, via_ticket, None
    slug = _resolve_repo_slug(_git_dir(get_client(task.client)))
    if slug is None:
        raise _unresolved_slug(task.client)
    return ref.normalized(slug), via_ticket, slug


def _label(pr_number: int, pr_repo: str | None) -> str:
    return PrRef(number=pr_number, repo=pr_repo).label()


def _echo_hold(result: HoldResult) -> None:
    """Print the one confirmation line for a non-unchanged hold."""
    ticket, client = result.ticket_id, result.client
    label = _label(result.pr_number, result.pr_repo)
    via = "" if result.via_ticket is None else f" (via ticket {result.via_ticket})"
    if result.outcome == "updated" and result.converted_from_worker_park:
        click.echo(
            f"{ticket}: updated (converted from worker park);"
            f" now an operator hold on PR {label}{via}"
        )
        return
    if result.outcome == "updated" and result.previous_pr is not None:
        previous = _label(result.previous_pr, result.previous_pr_repo)
        click.echo(
            f"{ticket}: updated, hold moved from PR {previous} to PR {label}{via}"
        )
        return
    line = f"Held {ticket} in {client} dev-queue until PR {label} merges{via}"
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
    the operator releases it with ``requeue``. A watch for an explicit
    cross-repo ref carries ``cross_repo=True`` (#3634).
    """
    cross_repo = result.pr_repo is not None
    outcome = register_or_adopt_watched_pr(
        WatchedPr(
            pr_url=github_pr_url(repo, result.pr_number),
            repo=repo,
            pr_number=result.pr_number,
            client=result.client,
            source=WATCHED_PR_SOURCE_DEPENDENCY_HOLD,
            cross_repo=cross_repo,
        )
    )
    if outcome == "collision":
        label = _label(result.pr_number, result.pr_repo)
        where = f"PR {label}" if cross_repo else f"PR #{result.pr_number} in {repo}"
        msg = (
            f"{result.ticket_id} IS held on PR {label},"
            f" but its merge cannot be observed: another client already watches"
            f" {where}. Release it with"
            f" `cw dev-queue requeue {result.ticket_id} -c {result.client}`."
        )
        raise CwError(msg)


@dev_queue.command(name="hold")
@click.argument("ticket")
@click.option(
    "--until-pr-merged",
    "pr_ref",
    type=_PrRefParam(),
    default=None,
    help=(
        "PR this row waits on: a same-repo number (9) or owner/repo#N."
        " Released when it merges. Give this or --until-ticket-merged."
    ),
)
@click.option(
    "--until-ticket-merged",
    "dep_ticket",
    default=None,
    help=(
        "Ticket in this client whose PR this row waits on. Resolved once, now,"
        " to that ticket's recorded PR, which must be open."
        " Give this or --until-pr-merged."
    ),
)
@click.option("--client", "-c", "client", required=True, help="Client name")
@handle_errors
def dev_queue_hold(
    ticket: str, pr_ref: PrRef | None, dep_ticket: str | None, client: str
) -> None:
    """Hold a row until a PR merges.

    Name the PR with --until-pr-merged (a same-repo number, or owner/repo#N) or
    with --until-ticket-merged (another ticket in this client that already has a
    PR). Give exactly one. Refuses a row that already has a PR or a live session.
    Stops no session: close a live session first (`cw spawn close`). A worker park
    with the same PR is converted to an operator hold. The serve tick releases the
    row on merge, and only the PR's own recorded merged state counts: a COMPLETED
    dependency ticket never releases a hold. If the PR closes unmerged the row
    stays held and a session.reap_proposed event names it.
    """
    if (pr_ref is None) == (dep_ticket is None):
        raise click.UsageError(_EXACTLY_ONE_MSG)
    refuse_inside_dispatch_worker(_HOLD_IN_WORKER_MSG)
    store = load_dev_queue()
    task = _find_ticket(store, ticket, client, warn=False)
    check_holdable(task, load_state())
    ref, via_ticket, slug = _resolve_hold_ref(task, store, pr_ref, dep_ticket)
    if is_operator_hold_on(task, ref):
        click.echo(
            f"{task.ticket_id} already held until PR {ref.label()} merges (unchanged)."
        )
        echo_resulting_row(task.ticket_id, task.client, sources=ViewSources())
        return
    client_cfg = get_client(task.client)
    git_dir = _git_dir(client_cfg)
    repo, ref = _verify_dependency_pr(client_cfg, ref, git_dir, slug)
    # bounded=True (#2501): operator command; nothing precedes the lock but
    # read-only state, the local slug read and the one gh call above.
    result = hold_ticket(
        task.ticket_id,
        task.client,
        ref,
        repo=repo,
        via_ticket=via_ticket,
        bounded=True,
    )
    # State first, then event: the row is already saved; the watch follows; the
    # ``ticket.held`` audit is written last, in ``finally``, so a watch collision
    # (the row stays held) still leaves its audit record. record_hold_event skips
    # the unchanged repeat itself.
    try:
        if result.outcome != "unchanged":
            _register_hold_watch(result, repo)
    finally:
        record_hold_event(result, repo)
    _echo_hold(result)
    echo_resulting_row(result.ticket_id, result.client, sources=ViewSources())
