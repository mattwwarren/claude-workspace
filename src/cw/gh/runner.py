"""Shared ``gh`` runner and timeouts."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


FETCH_COMMENTS_TIMEOUT = 30


_POST_COMMENT_TIMEOUT_SECONDS = 30


def _run_gh_text(
    args: list[str], timeout: int, cwd: Path | None
) -> tuple[_sp.CompletedProcess[str] | None, bool]:
    """Run ``gh *args`` capturing text; return ``(proc, gh_available)``.

    Policy-free, like :func:`pr_exists_for_branch`: ``(None, False)`` when the
    ``gh`` binary is missing, ``(None, True)`` on a timeout or any other OS
    error, else ``(proc, True)`` with the process returned whatever its exit
    code so the caller can read stdout / stderr.
    """
    try:
        result = _sp.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return None, False
    except (OSError, _sp.TimeoutExpired):
        return None, True
    return result, True


def run_issue_view(
    ticket_id: str,
    repo: str,
    *,
    fields: str,
    timeout: int = FETCH_COMMENTS_TIMEOUT,
    cwd: Path | None = None,
) -> tuple[_sp.CompletedProcess[str] | None, bool]:
    """Run ``gh issue view <ticket_id> -R <repo> --json <fields>`` (#2798).

    Returns ``(proc, gh_available)`` per :func:`_run_gh_text`; the caller
    interprets the exit code, stdout and stderr. Unlike
    :func:`fetch_issue_body` / :func:`fetch_issue_comments` it names the repo
    with ``-R`` and keeps the failure detail, which ``cw preflight`` reports.
    """
    return _run_gh_text(
        ["issue", "view", ticket_id, "-R", repo, "--json", fields], timeout, cwd
    )


def run_pr_list(
    repo: str,
    *,
    fields: str,
    search: str | None = None,
    head: str | None = None,
    state: str | None = None,
    timeout: int = FETCH_COMMENTS_TIMEOUT,
    cwd: Path | None = None,
) -> tuple[_sp.CompletedProcess[str] | None, bool]:
    """Run ``gh pr list -R <repo> [--search/--head/--state] --json <fields>``.

    *search*, *head* and *state* are each added only when not None. Returns
    ``(proc, gh_available)`` per :func:`_run_gh_text` (#2798).
    """
    args = ["pr", "list", "-R", repo]
    for flag, value in (("--search", search), ("--head", head), ("--state", state)):
        if value is not None:
            args += [flag, value]
    return _run_gh_text([*args, "--json", fields], timeout, cwd)


def run_failed_job_log(
    repo: str,
    run_id: str,
    job_id: str,
    *,
    timeout: int = FETCH_COMMENTS_TIMEOUT,
    cwd: Path | None = None,
) -> tuple[_sp.CompletedProcess[str] | None, bool]:
    """Run ``gh run view <run_id> -R <repo> --job <job_id> --log-failed`` (#2944).

    The failed steps' log of one Actions job, read by the PR repair-feedback
    synthesizer (``cw.pr_feedback``). Returns ``(proc, gh_available)`` per
    :func:`_run_gh_text`; the caller interprets the exit code and stdout.
    """
    return _run_gh_text(
        ["run", "view", run_id, "-R", repo, "--job", job_id, "--log-failed"],
        timeout,
        cwd,
    )
