"""Worktree management commands."""

from __future__ import annotations

import click

from cw.cli._base import _complete_client, handle_errors, main
from cw.config import load_clients
from cw.exceptions import CwError
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir, effective_worktree_bases
from cw.worktree_gc import (
    GC_REMOVE_VERDICTS,
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
    GcVerdict.KEEP_CLOSED_PR: "KEEP  ",
    GcVerdict.SKIP_LOCKED: "SKIP  ",
    GcVerdict.SKIP_GH_UNAVAILABLE: "SKIP  ",
    GcVerdict.SKIP_DETACHED: "SKIP  ",
    GcVerdict.SKIP_BARE: "SKIP  ",
    GcVerdict.SKIP_DIRTY: "SKIP  ",
    GcVerdict.SKIP_LIVE: "SKIP  ",
}

_GC_VERDICT_REASON: dict[GcVerdict, str] = {
    GcVerdict.REMOVE_MERGED: "MERGED PR",
    GcVerdict.REMOVE_CLOSED: "CLOSED PR",
    GcVerdict.KEEP_OPEN_PR: "OPEN PR",
    GcVerdict.KEEP_NO_PR: "no PR",
    GcVerdict.KEEP_CLOSED_PR: "CLOSED PR (kept; use --include-closed to remove)",
    GcVerdict.SKIP_LOCKED: "locked",
    GcVerdict.SKIP_GH_UNAVAILABLE: "gh unavailable",
    GcVerdict.SKIP_DETACHED: "detached HEAD",
    GcVerdict.SKIP_BARE: "bare worktree",
    GcVerdict.SKIP_DIRTY: "dirty",
    GcVerdict.SKIP_LIVE: "live session or running task",
}


def _format_gc_result(gc_result: WorktreeGcResult, *, applied: bool) -> str:
    """Format a single GC result line."""
    label = _GC_VERDICT_LABEL[gc_result.verdict]
    branch = gc_result.entry.branch or "(detached)"
    reason = _GC_VERDICT_REASON[gc_result.verdict]

    if gc_result.pr_number is not None:
        reason = f"{reason} #{gc_result.pr_number}"

    if applied and gc_result.verdict in GC_REMOVE_VERDICTS:
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
    if report.capped:
        n_shown = len(report.results)
        lines.append(
            f"run capped at {n_shown} of {report.total_discovered}"
            f" (pass --limit to adjust)"
        )
    if apply:
        n_ok = n_remove - report.removal_failures
        if report.removal_failures:
            lines.append(
                f"{n_ok} removed, {report.removal_failures} failed"
                f", {n_keep} kept, {n_skip} skipped"
                f" (check logs for removal errors)"
            )
        else:
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
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap worktrees processed per client (applied after base filtering).",
)
@handle_errors
def worktree_gc(
    apply: bool,
    include_closed: bool,
    client_name: str | None,
    timeout: int,
    limit: int | None,
) -> None:
    """GC worktrees for squash-merged or closed branches.

    Checks each worktree branch's PR state via the gh CLI and removes
    worktrees where the PR is MERGED (or CLOSED with --include-closed).
    Dry-run by default; pass --apply to act.
    Locked, dirty, and live-session worktrees are always skipped.
    Runs against all configured GitHub-tracked clients by default.
    """
    clients = load_clients()

    if not clients:
        click.echo("No clients configured. Add one to ~/.config/cw/clients.yaml.")
        return

    if client_name is not None:
        client = clients.get(client_name)
        if client is None:
            msg = f"Client {client_name!r} not found. Run 'cw config' to list clients."
            raise CwError(msg)
        selected = {client_name: client}
    else:
        selected = dict(clients)

    any_output = False
    for name, client in selected.items():
        tracker_root = client.repo_path or client.workspace_path
        if resolve_tracker(tracker_root) != TRACKER_GITHUB_ISSUES:
            click.echo(f"[{name}] skipped — not a GitHub-tracked client")
            continue

        git_cwd = _git_dir(client)
        wt_bases = effective_worktree_bases(client)
        report = run_worktree_gc(
            git_cwd,
            apply=apply,
            timeout=timeout,
            include_closed=include_closed,
            worktree_bases=wt_bases,
            limit=limit,
        )

        if len(selected) > 1:
            click.echo(f"[{name}]")
        click.echo(_format_gc_report(report, apply=apply))
        any_output = True

    if not any_output:
        click.echo("No GitHub-tracked clients found.")
