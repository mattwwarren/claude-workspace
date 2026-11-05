"""``cw dev-queue add --adopt-pr`` and the requeue-side adoption inputs (#2943).

Split out of ``crud.py`` to keep it under the ~1000-line ceiling: ``crud``
keeps only the options and one early delegation per command. RFC 0016 order:
every gh read happens here with no lock held, then ``add_ticket`` takes the
dev-queue lock and runs the carried-by-a-live-row check. A refusal writes no
row; a non-green PR writes a parked row.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Literal

import click

from cw._git import run_git
from cw.adopt_pr import build_adopted_task, read_adopted_pr, verify_ticket_exists
from cw.config import get_client
from cw.dev_queue import add_ticket
from cw.dev_queue.adoption import ADOPT_PR_PARK_DISPOSITIONS, AdoptedPrSnapshot
from cw.dispatch.adopted_head_gate import fetch_adopted_branch
from cw.dispatch.claim.events import emit_adoption_attention_event
from cw.events import record_event
from cw.exceptions import AdoptionRefusedError
from cw.models import OrchestratorEventType, Stage, resolve_feature_branch
from cw.pr_hydrate import _parse_pr_url
from cw.worktree import _git_dir

from .show import echo_resulting_row

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.ticket_view import ViewSources

_ADOPT_STAGE = Stage.REVIEW.value
_CAT_FILE_TIMEOUT_SECONDS = 10


def _check_adopt_options(tickets: tuple[str, ...], stage_override: str | None) -> None:
    """``--adopt-pr`` takes exactly one ticket and enters at review only."""
    if len(tickets) != 1:
        msg = "--adopt-pr adopts one PR as one ticket: pass exactly one TICKET."
        raise click.UsageError(msg)
    if stage_override not in (None, _ADOPT_STAGE):
        msg = f"--adopt-pr enters at review; --stage {stage_override} is not allowed."
        raise click.UsageError(msg)


def run_adopt(
    tickets: tuple[str, ...],
    stage_override: str | None,
    client_name: str,
    pr_number: int,
    *,
    priority: int,
    scope_hint: str | None,
    signoff: Literal["operator"] | None,
    hold_finalize: Literal["manual"] | None,
    lane: str,
    sources: ViewSources,
) -> None:
    """Adopt PR *pr_number* as the one ticket for *client_name*; echo the row."""
    _check_adopt_options(tickets, stage_override)
    ticket_id = tickets[0]
    client_cfg = get_client(client_name)
    intake = read_adopted_pr(client_cfg, pr_number)
    note = verify_ticket_exists(client_cfg, ticket_id, pr_number)
    if note is not None:
        click.echo(note, err=True)
    task = build_adopted_task(ticket_id, client_cfg, intake, lane=lane)
    task.priority = priority
    task.scope_hint = scope_hint
    task.signoff = signoff
    task.hold_finalize = hold_finalize
    if not add_ticket(task, bounded=True):
        msg = f"Refusing to adopt PR #{pr_number}: '{ticket_id}' already has a row."
        raise AdoptionRefusedError(msg)
    snapshot = intake.snapshot
    record_event(
        OrchestratorEventType.TICKET_ENQUEUED,
        {
            "ticket_id": ticket_id,
            "client": client_name,
            "priority": priority,
            "stage": task.stage.value,
            "adopted_pr_url": snapshot.pr_url,
            "branch": snapshot.branch,
            "adopted_head_sha": snapshot.head_sha,
        },
    )
    if intake.disposition is not None:
        emit_adoption_attention_event(
            task,
            client_name,
            lane,
            paused_status=intake.disposition,
            branch=snapshot.branch,
            observed_head_sha=snapshot.head_sha,
            commit_range="",
            details=intake.details,
        )
        click.echo(
            f"Adopted {snapshot.pr_url} as {ticket_id} -> {client_name}, parked"
            f" {intake.disposition}: {intake.details}"
        )
    else:
        draft = " (draft; finalize marks it ready)" if snapshot.is_draft else ""
        click.echo(
            f"Adopted {snapshot.pr_url} as {ticket_id} -> {client_name} at review,"
            f" pinned {snapshot.head_sha}{draft}"
        )
    echo_resulting_row(ticket_id, client_name, sources=sources)


def _commit_is_local(repo: Path, sha: str) -> bool:
    """True iff commit *sha* resolves in *repo*; never raises (a non-repo is False)."""
    try:
        completed = run_git(
            ["cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            timeout=_CAT_FILE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _fetch_recheck_objects(
    client_cfg: ClientConfig, row: TicketTask, branch: str
) -> None:
    """Best-effort fetch of the new head's branch and, if absent, the old pin.

    Lets the locked re-pin classify ancestry (#2970): the new head is on the
    branch, but after a force-push the old pin is on no origin branch, so it
    is fetched by SHA. ``fetch_adopted_branch`` hands its second argument to
    ``git fetch origin <ref>``, so a full SHA works (origin must allow it;
    GitHub does). Every failure only logs; ancestry then reads ``unknown`` and
    the requeue is never blocked.
    """
    fetch_adopted_branch(client_cfg, branch)
    old_pin = row.adopted_head_sha
    if old_pin is not None and not _commit_is_local(_git_dir(client_cfg), old_pin):
        fetch_adopted_branch(client_cfg, old_pin)


def adopt_requeue_recheck(
    row: TicketTask | None, *, repin_adopted_head: bool
) -> AdoptedPrSnapshot | None:
    """The lock-free reads ``requeue`` runs for an adopted row (A5, R10).

    With *repin_adopted_head*, fetch the row's branch so the locked ancestry
    check sees a foreign push's objects. For an ``adopt_pr_*`` park, re-read
    the PR and return the fresh snapshot only when it is green; a PR still
    not green fails closed here, before the requeue writes anything. A green
    re-read is followed by a best-effort fetch of the branch and the old pin
    so the re-pin can record ancestry (:func:`_fetch_recheck_objects`).
    """
    if row is None or row.adopted_head_sha is None:
        return None
    client_cfg = get_client(row.client)
    if repin_adopted_head:
        branch = resolve_feature_branch(row.ticket_id, client_cfg, row)
        fetch_adopted_branch(client_cfg, branch)
    if row.disposition not in ADOPT_PR_PARK_DISPOSITIONS or row.adopted_pr_url is None:
        return None
    parsed = _parse_pr_url(row.adopted_pr_url)
    if parsed is None:
        msg = f"Cannot re-check {row.adopted_pr_url}: unparseable PR url."
        raise AdoptionRefusedError(msg)
    intake = read_adopted_pr(client_cfg, parsed[1])
    if intake.disposition is not None:
        msg = (
            f"Ticket '{row.ticket_id}' stays parked: {row.adopted_pr_url} is still"
            f" {intake.disposition} ({intake.details})."
        )
        raise AdoptionRefusedError(msg)
    _fetch_recheck_objects(
        client_cfg, row, resolve_feature_branch(row.ticket_id, client_cfg, row)
    )
    return intake.snapshot
