"""Per-client ``core.bare`` check for ``cw doctor`` (#2973).

``core.bare = true`` in the repo config of a checkout that has a working tree
breaks every working-tree git command (``git status`` fails with "this
operation must be run in a work tree"), so the dispatch loop's auto
fast-forward of ``main`` stalls and the freshness gate trips. A genuine bare
clone also reports a bare repository but has no ``.git`` child, and is not
flagged. Read-only; the finding is advisory (``ok=True, warn=True``) and
names the one-line fix.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Final

from cw._git import run_git
from cw.doctor._shared import CheckResult
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_GIT_TIMEOUT: Final = 10.0


def _is_bare_with_worktree(repo: Path) -> bool:
    """``rev-parse --is-bare-repository`` is true while *repo* has a ``.git``."""
    if not (repo / ".git").exists():
        return False
    try:
        done = run_git(
            ["rev-parse", "--is-bare-repository"],
            cwd=repo,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def _check_repo_bare(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """One ``repo-bare/<client>`` warning per client whose checkout is flagged bare."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        repo = _git_dir(client)
        if not _is_bare_with_worktree(repo):
            continue
        results.append(
            CheckResult(
                f"repo-bare/{name}",
                ok=True,
                warn=True,
                detail=(
                    f"core.bare is true in the repo config of {repo}, but the "
                    "checkout has a working tree (.git is present): git status "
                    "and the dispatch loop's auto fast-forward fail with "
                    '"this operation must be run in a work tree". '
                    f"Fix: git -C {repo} config core.bare false"
                ),
            )
        )
    return results
