"""Worktree management commands."""

from __future__ import annotations

import click

from cw.cli._base import _complete_client, handle_errors, main
from cw.config import load_clients
from cw.exceptions import CwError
from cw.worktree import _git_dir
from cw.worktree_gc import (
    GcVerdict,
    WorktreeGcReport,
    WorktreeGcResult,
    run_worktree_gc,
)

_GC_VERDICT_LABEL: dict[GcVerdict, str] = {
    GcVerdict.REMOVE_MERGED: "REMOVE",
    GcVerdict.REMOVE_CLOSED: "REMOVE",
    GcVerdict.KEEP_OPEN_PR: "KEEP  ",
    GcVerdict.KEEP_NO_PR: "KEEP  ",
    GcVerdict.SKIP_LOCKED: "SKIP  ",
    GcVerdict.SKIP_GH_UNAVAILABLE: "SKIP  ",
    GcVerdict.SKIP_DETACHED: "SKIP  ",
    GcVerdict.SKIP_DIRTY: "SKIP  ",
}

_GC_VERDICT_REASON: dict[GcVerdict, str] = {
    GcVerdict.REMOVE_MERGED: "MERGED PR",
    GcVerdict.REMOVE_CLOSED: "CLOSED PR",
    GcVerdict.KEEP_OPEN_PR: "OPEN PR",
    GcVerdict.KEEP_NO_PR: "no PR",
    GcVerdict.SKIP_LOCKED: "locked",
    GcVerdict.SKIP_GH_UNAVAILABLE: "gh unavailable",
    GcVerdict.SKIP_DETACHED: "detached HEAD",
    GcVerdict.SKIP_DIRTY: "dirty",
}


def _format_gc_result(gc_result: WorktreeGcResult, *, applied: bool) -> str:
    """Format a single GC result line."""
    label = _GC_VERDICT_LABEL[gc_result.verdict]
    branch = gc_result.entry.branch or "(detached)"
    reason = _GC_VERDICT_REASON[gc_result.verdict]

    if gc_result.pr_number is not None:
        reason = f"{reason} #{gc_result.pr_number}"

    if applied and gc_result.verdict.name.startswith("REMOVE_"):
        return f"  removed {branch:<20}  ({reason})"

    return f"  {label}  {branch:<20}  [{reason}]"


def _format_gc_report(report: WorktreeGcReport, *, apply: bool) -> str:
    """Render a human-readable GC report."""
    lines: list[str] = []

    if apply:
        lines.append("cw worktree gc — applying\n")
    else:
        lines.append("cw worktree gc — dry run (pass --apply to remove)\n")

    lines.extend(_format_gc_result(r, applied=apply) for r in report.results)

    n_remove = len(report.to_remove)
    n_keep = len(report.kept)
    n_skip = len(report.skipped)

    lines.append("")
    if apply:
        lines.append(f"{n_remove} removed, {n_keep} kept, {n_skip} skipped")
    else:
        lines.append(f"{n_remove} to remove, {n_keep} to keep, {n_skip} skipped")

    return "\n".join(lines)


@main.group(name="worktree")
def worktree_group() -> None:
    """Worktree management commands."""


@worktree_group.command(name="gc")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Remove worktrees (dry-run by default).",
)
@click.option(
    "--include-closed",
    is_flag=True,
    default=False,
    help="Also remove worktrees for CLOSED (abandoned) PRs.",
)
@click.option(
    "--client",
    "client_name",
    default=None,
    shell_complete=_complete_client,
    help="Limit to a specific client.",
)
@click.option(
    "--timeout",
    type=int,
    default=10,
    show_default=True,
    help="gh CLI timeout in seconds.",
)
@handle_errors
def worktree_gc(
    apply: bool, include_closed: bool, client_name: str | None, timeout: int
) -> None:
    """GC worktrees for squash-merged or closed branches.

    Checks each worktree branch's PR state via the gh CLI and removes
    worktrees where the PR is MERGED. Dry-run by default; pass --apply to act.
    Locked and dirty worktrees are always skipped.

    CLOSED PR worktrees are kept by default (pass --include-closed to remove them).
    """
    clients = load_clients()

    if client_name is not None:
        client = clients.get(client_name)
        if client is None:
            msg = f"Client {client_name!r} not found. Run 'cw config' to list clients."
            raise CwError(msg)
    elif len(clients) == 1:
        client = next(iter(clients.values()))
    else:
        if not clients:
            msg = "No clients configured. Add one to ~/.config/cw/clients.yaml."
            raise CwError(msg)
        names = ", ".join(clients)
        msg = (
            f"Multiple clients configured ({names}). Specify one with --client <name>."
        )
        raise CwError(msg)

    git_cwd = _git_dir(client)
    report = run_worktree_gc(
        git_cwd, apply=apply, timeout=timeout, include_closed=include_closed
    )
    click.echo(_format_gc_report(report, apply=apply))
