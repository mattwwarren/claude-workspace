"""Builders for the RFC 0020 base-row tests (S3a / S3b).

One home for the row-with-a-base fixtures that ``test_review_base_branch_sites``,
``test_base_branch_sites`` and ``test_codex_base_branch_sites`` all need, so no
test file grows a private copy. Plain helpers only: no ``@pytest.fixture`` and
no fake classes (those live in ``tests/_tool_fakes.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tests._review_convergence_helpers import merge_origin_commit
from tests._shared.git_repos import (
    add_diverged_base_origin,
    commit_tracked_file,
    git_in,
)
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import TicketTask
    from tests._review_engine_helpers import EngineRepo

ROW_BASE = "epic/x"
CLIENT_DEFAULT = "trunk"
# The trust root for the codex base-row tests: the repo's own ``main``, which is
# what ``add_diverged_base_origin`` diverges ``ROW_BASE`` from.
TRUST_ROOT = "main"

ENGINE_STUB = Path(__file__).resolve().parent.parent / "fixtures" / "review_engine_stub"
ENGINE_SKILL = "repo-review"
ENGINE_SKILL_DIR = f".claude/skills/{ENGINE_SKILL}"
ENGINE_SKILL_MD = f"{ENGINE_SKILL_DIR}/SKILL.md"
EPIC_SKILL_TEXT = "# repo-review\n\nEdited on the epic branch.\n"


def base_row(
    base: str | None = ROW_BASE,
    *,
    ticket_id: str = "T-1",
    client: str = "test-client",
) -> TicketTask:
    """One ticket row carrying *base* as its ``base_branch`` (``None`` = null row)."""
    return _make_ticket_task(ticket_id=ticket_id, client=client, base_branch=base)


def diverged_base_worktree(
    make_git_repo: Callable[..., Path], name: str, base: str = ROW_BASE
) -> Path:
    """A worktree on ``feature``, cut from ``origin/<base>``, diverged from main.

    ``origin/main`` and ``origin/<base>`` each carry a commit the other lacks, so
    a ref measured against the wrong one of them shows. The caller adds whatever
    feature-branch commits its test needs.
    """
    repo = make_git_repo(name)
    add_diverged_base_origin(repo, base)
    git_in(repo, "checkout", "-b", "feature", base)
    return repo


def push_epic_base(worktree: Path, base: str = ROW_BASE) -> str:
    """Push ``main`` to ``origin/<base>`` and return the worktree's HEAD sha."""
    git_in(worktree, "push", "origin", f"main:refs/heads/{base}")
    return git_in(worktree, "rev-parse", "HEAD")


class EpicMerge(NamedTuple):
    """A worktree that merged ``origin/<base>``, with its pre- and post-merge shas."""

    worktree: Path
    clean: str
    head: str


def epic_merged_worktree(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    scratch: Path,
    base: str = ROW_BASE,
) -> EpicMerge:
    """Feature branch that merged ``origin/<base>``, not ``origin/main``.

    ``origin/<base>`` starts at ``main`` and advances out of band; the feature
    branch (one commit past main) merges it, so ``head`` is a clean merge whose
    second parent is on the base branch only.
    """
    wt = make_git_repo_with_origin("wt")[0]
    clean = push_epic_base(wt, base)
    merge_origin_commit(wt, scratch, base, "epic_only.py")
    return EpicMerge(wt, clean, git_in(wt, "rev-parse", "HEAD"))


def push_epic_branch(repo: EngineRepo, *, edit_engine: bool) -> None:
    """``origin/epic/x``: main, plus an edited skill and no manifest if asked."""
    wt = repo.worktree
    git_in(wt, "checkout", "-b", ROW_BASE, "main")
    if edit_engine:
        commit_tracked_file(wt, ENGINE_SKILL_MD, EPIC_SKILL_TEXT)
        git_in(wt, "rm", "-q", f"{ENGINE_SKILL_DIR}/cw-engine.json")
        git_in(wt, "commit", "-q", "-m", "drop the manifest on the epic branch")
    git_in(wt, "push", "-q", "origin", ROW_BASE)
    git_in(wt, "checkout", "dev/1")
