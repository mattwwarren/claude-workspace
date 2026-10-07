"""Thin ``gh`` / ``git`` subprocess wrappers and the cached GitHub login."""

from __future__ import annotations

import functools
import logging
import subprocess

logger = logging.getLogger(__name__)


def _run_gh(args: list[str], repo: str | None = None) -> str:
    """Run a gh CLI command and return stdout.

    Returns empty string on failure (FileNotFoundError or CalledProcessError).
    If *repo* is provided, adds ``-R repo`` to the command.
    """
    cmd = ["gh", *args]
    if repo is not None:
        cmd = ["gh", "-R", repo, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logger.warning("gh CLI not found. Install it: https://cli.github.com/")
        return ""
    except subprocess.CalledProcessError as e:
        logger.warning("gh command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return ""
    return result.stdout.strip()


def _run_git(args: list[str], cwd: str | None = None) -> str:
    """Run a git command and return stdout.

    Returns empty string on failure (FileNotFoundError or CalledProcessError).
    If *cwd* is provided, adds ``-C cwd`` to the command.
    """
    cmd = ["git", *args]
    if cwd is not None:
        cmd = ["git", "-C", cwd, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logger.warning("git not found in PATH")
        return ""
    except subprocess.CalledProcessError as e:
        logger.warning("git command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return ""
    return result.stdout.strip()


@functools.lru_cache(maxsize=1)
def _get_our_username() -> str:
    """Return the authenticated GitHub username.

    Calls ``gh api user --jq .login`` and returns the result stripped of
    surrounding whitespace.  Returns an empty string if the call fails.
    """
    return _run_gh(["api", "user", "--jq", ".login"]).strip()
