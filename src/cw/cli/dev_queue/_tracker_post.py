"""Tracker-posting policy shared by ``resolve --post`` and ``approve --post-marker``.

Both commands mirror a dev-queue decision to a GitHub ticket through ``gh``,
and both owe the operator the same three things: skip the post when the
client's tracker is positively non-GitHub (#2702, #1419), run ``gh`` from the
client's repo, and report the outcome. This module is the one place that
policy lives (#2732).

It never calls ``post_issue_comment`` or ``fetch_issue_comments``: each command
keeps calling its own module-level name, so the dedup (approve only) and the
operator-authorship flag stay with the command that owns them, and tests keep
patching ``cw.cli.dev_queue.<command>.post_issue_comment``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from cw.models import ClientConfig


def _non_github_tracker(client_cfg: ClientConfig) -> str | None:
    """The tracker's name when it is positively known and not GitHub, else None.

    An unresolvable tracker (no config, no ``tracking`` key) is None: fail
    open, matching ``requeue.py``'s gate.
    """
    tracker = resolve_tracker(client_cfg.workspace_path)
    if tracker is not None and tracker != TRACKER_GITHUB_ISSUES:
        return tracker
    return None


def tracker_is_github_or_unknown(client_cfg: ClientConfig) -> bool:
    """True unless the client's tracker is positively non-GitHub."""
    return _non_github_tracker(client_cfg) is None


def github_post_cwd(
    client_cfg: ClientConfig,
    *,
    flag: str,
    noun: str,
    ticket_id: str,
    client: str,
    row_note: str,
) -> Path | None:
    """The ``gh`` working directory when posting may proceed, else None.

    On a known non-GitHub tracker, echoes to stdout that the *noun* is not
    posted to *ticket_id* (*client*), followed by *row_note* (where the
    decision is recorded instead), and returns None.
    """
    tracker = _non_github_tracker(client_cfg)
    if tracker is not None:
        click.echo(
            f"{flag}: tracker is {tracker!r}, not GitHub — the {noun} is not"
            f" posted to {ticket_id} ({client}). {row_note}"
        )
        return None
    return _git_dir(client_cfg)


def report_post_outcome(
    result: subprocess.CompletedProcess[bytes] | None,
    *,
    posted: str,
    failed: str,
) -> bool:
    """Echo *posted* and return True on success; else echo *failed* to stderr.

    Success is a completed ``gh`` run with return code 0; ``None`` (gh could not
    run or timed out) and any other return code are failures.
    """
    if result is not None and result.returncode == 0:
        click.echo(posted)
        return True
    click.echo(failed, err=True)
    return False
