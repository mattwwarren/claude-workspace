"""Shared builders for the large-tier review-approval tests (#2773).

One home for the git repos, cw-context writers and the Step 4.0 fence runner
that ``test_review_approval.py``, ``test_cli_review_check_approval.py``,
``test_dispatch_review_approval_gate.py``, ``test_repair_fence.py`` and
``test_stage.py`` all need. Plain functions and classes, no fixtures (the
sibling ``_review_convergence_helpers`` shape): each test module wraps
:func:`make_approval_repo` in its own small fixture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from cw.context_queue_metadata import build_queue_metadata
from cw.models import Stage
from cw.spawn import CW_CONTEXT_SCHEMA_VERSION
from tests.conftest import (
    _bash_fences,
    _clean_git_env,
    _cmd,
    _make_ticket_task,
    git_in,
    push_commit_to_origin,
    substitute_fence_placeholders,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import TicketTask

FINALIZE_DOC = "auto-dev-finalize.md"
STEP40_HEADING = "### Step 4.0: Large-Tier Review Approval Check (headless only)"
STEP4A_HEADING = "### Step 4a: Merge Gate Check"
TIER_PLACEHOLDER = "resolved scope.tier"
CHECK_APPROVAL_CALL = "cw review check-approval"

ShimMode = Literal["real", "old", "silent", "crash"]


class ApprovalRepo(NamedTuple):
    """A feature branch pushed to a bare origin, with its approved head."""

    worktree: Path
    origin: Path
    approved: str


def head(worktree: Path) -> str:
    return git_in(worktree, "rev-parse", "HEAD")


def make_approval_repo(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    name: str = "wt-approval",
    *,
    branch: str = "feature",
) -> ApprovalRepo:
    """A repo on *branch* pushed to a bare origin; ``approved`` is its HEAD."""
    worktree, origin = make_git_repo_with_origin(name, branch=branch)
    return ApprovalRepo(worktree, origin, head(worktree))


def approval_task(sha: str | None, kind: str = "operator") -> TicketTask:
    return _make_ticket_task(
        stage=Stage.FINALIZE,
        review_approved_at=datetime.now(UTC),
        review_approver_kind=kind,
        review_approved_sha=sha,
    )


def cw_context(
    worktree: Path, task: TicketTask, **overrides: object
) -> dict[str, object]:
    """A cw-context payload as spawn writes it, with *task*'s queue metadata."""
    payload: dict[str, object] = {
        "schema_version": CW_CONTEXT_SCHEMA_VERSION,
        "worktree_path": str(worktree),
        "queue_metadata": build_queue_metadata(task),
        "world_state_snapshot": {"origin_main_branch": "main"},
    }
    payload.update(overrides)
    return payload


def write_context(worktree: Path, payload: dict[str, object] | str) -> None:
    path = worktree / ".claude" / "cw-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def approve_at(worktree: Path, sha: str | None, kind: str = "operator") -> None:
    write_context(worktree, cw_context(worktree, approval_task(sha, kind)))


def merge_commit(worktree: Path, *parents: str) -> str:
    """Commit a merge of *parents* (first parent's tree) and move HEAD to it."""
    parent_args = [arg for parent in parents for arg in ("-p", parent)]
    sha = git_in(
        worktree, "commit-tree", f"{parents[0]}^{{tree}}", *parent_args, "-m", "merge"
    )
    git_in(worktree, "reset", "--hard", sha)
    return sha


def advance_main(origin: Path, worktree: Path, side: Path, filename: str) -> str:
    """Land one commit on ``origin/main`` and fetch it into *worktree*."""
    sha = push_commit_to_origin(origin, "main", side, filename)
    git_in(worktree, "fetch", "origin")
    return sha


def sync_merge(worktree: Path) -> None:
    """Finalize's own Step 4c sync merge of the default branch."""
    git_in(worktree, "merge", "--no-ff", "--no-edit", "origin/main")


def step40_section() -> str:
    content = _cmd(FINALIZE_DOC)
    start = content.index(STEP40_HEADING)
    return content[start : content.index(STEP4A_HEADING, start)]


def step40_fence() -> str:
    """The one Step 4.0 bash fence, picked by its ``cw review check-approval`` call."""
    fences = _bash_fences(step40_section())
    assert len(fences) == 1, f"expected one Step 4.0 bash fence, got {len(fences)}"
    assert CHECK_APPROVAL_CALL in fences[0]
    return fences[0]


_SHIM_BODIES: dict[str, str] = {
    "old": "echo 'Usage: cw review [OPTIONS] COMMAND [ARGS]...' >&2\nexit 2\n",
    "silent": "exit 0\n",
    "crash": "echo boom >&2\nexit 1\n",
}


def make_cw_shim(tmp_path: Path, mode: ShimMode) -> Path:
    """A bin dir whose ``cw`` is the real CLI, or an old/silent/crashing stand-in."""
    bin_dir = tmp_path / f"cw-shim-{mode}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f'exec "{sys.executable}" -m cw "$@"\n'
        if mode == "real"
        else _SHIM_BODIES[mode]
    )
    shim = bin_dir / "cw"
    shim.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


_REAL_SHIM_DIRS: list[Path] = []


def real_cw_bin() -> Path:
    """A process-lifetime bin dir holding the ``real`` shim (built once)."""
    if not _REAL_SHIM_DIRS:
        _REAL_SHIM_DIRS.append(
            make_cw_shim(Path(tempfile.mkdtemp(prefix="cw-shim-")), "real")
        )
    return _REAL_SHIM_DIRS[0]


def run_step40_fence(
    worktree: Path,
    tier: str | None,
    *,
    env: dict[str, str] | None = None,
    cw_bin: Path | None = None,
) -> str:
    """Run the Step 4.0 fence from *worktree*; return its one verdict line.

    ``tier=None`` leaves ``<resolved scope.tier>`` unsubstituted, the shape a
    worker that skipped the substitution would run. When *env* is not given,
    *cw_bin* (default: the ``real`` shim dir) is prepended to ``PATH`` on top of
    the clean git env.
    """
    placeholders = {} if tier is None else {TIER_PLACEHOLDER: tier}
    fence = substitute_fence_placeholders(step40_fence(), placeholders)
    bash = shutil.which("bash")
    assert bash is not None
    if env is None:
        shim = cw_bin if cw_bin is not None else real_cw_bin()
        env = _clean_git_env()
        env["PATH"] = f"{shim}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [bash, "-c", fence],
        cwd=worktree,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()
