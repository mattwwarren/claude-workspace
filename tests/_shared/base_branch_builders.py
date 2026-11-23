"""Builders for the RFC 0020 base-row tests (S3a / S3b).

One home for the row-with-a-base fixtures that ``test_review_base_branch_sites``,
``test_base_branch_sites`` and ``test_codex_base_branch_sites`` all need, so no
test file grows a private copy. Plain helpers only: no ``@pytest.fixture`` and
no fake classes (those live in ``tests/_tool_fakes.py``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NamedTuple

import pytest

from cw.models import ClientConfig
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
    from tests._worktree_helpers import FakeClock

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
    **overrides: object,
) -> TicketTask:
    """One ticket row carrying *base* as its ``base_branch`` (``None`` = null row).

    *overrides* are forwarded to the row model (``status=``, ``pr_state=``, ...).
    """
    return _make_ticket_task(
        ticket_id=ticket_id, client=client, base_branch=base, **overrides
    )


def client_for_repo(repo: Path, **extra: object) -> ClientConfig:
    """A client whose ``repo_path`` is *repo*, on ``main``; *extra* overrides fields."""
    return ClientConfig.model_validate(
        {
            "name": "acme",
            "repo_path": str(repo),
            "branch": "main",
            "default_branch": "main",
            **extra,
        }
    )


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


def epic_repo_with_origin(
    make_git_repo: Callable[..., Path],
    base: str = "epic/x",
    *,
    files: Sequence[tuple[str, int]] = (("big.py", 50), ("uv.lock", 40)),
    deleting: Sequence[tuple[str, int]] = (),
) -> tuple[Path, Path]:
    """A repo whose *base* carries sized files beyond the diverged ``main`` (#3783).

    Layers on :func:`add_diverged_base_origin`: the diverged ``main`` is kept on
    purpose, so a two-dot diff would count ``main_work.py`` and a three-dot diff
    would not. Each ``(relpath, n)`` in *files* adds an ``n``-line file to *base*.
    Each ``(relpath, n)`` in *deleting* is an ``n``-line file committed on ``main``
    before the branches diverge (so it is on the merge-base) and ``git rm``-ed on
    *base*. Returns ``(repo, origin)`` with *repo* back on ``main``.
    """
    repo = make_git_repo("acme")
    for relpath, line_count in deleting:
        commit_tracked_file(repo, relpath, "x = 1\n" * line_count)
    origin = add_diverged_base_origin(repo, base)
    git_in(repo, "checkout", base)
    for relpath, _ in deleting:
        git_in(repo, "rm", "-q", relpath)
        git_in(repo, "commit", "-q", "-m", f"delete {relpath} on the epic")
    for relpath, line_count in files:
        commit_tracked_file(repo, relpath, "x = 1\n" * line_count)
    git_in(repo, "push", "origin", base)
    git_in(repo, "checkout", "main")
    return repo, origin


def patch_git_bounded(
    monkeypatch: pytest.MonkeyPatch,
    *modules: ModuleType,
    clock: FakeClock | None = None,
    step: float = 0.0,
    fail_verbs: Sequence[str] = (),
) -> list[tuple[list[str], float]]:
    """Record every ``git_bounded`` call in *modules* and run the real git.

    Returns the live list of ``(argv, timeout)`` pairs. Each call advances
    *clock* (when given) by *step*, and a call whose ``argv[0]`` is in
    *fail_verbs* raises ``GitFailedError`` instead of running git. The real
    function is captured before patching, so only the recording is new.
    """
    from cw.merge_probe import GitFailedError, git_bounded

    calls: list[tuple[list[str], float]] = []

    def _recording(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), timeout))
        if clock is not None:
            clock.advance(step)
        if argv and argv[0] in fail_verbs:
            msg = f"git {argv[0]} timed out after {timeout:g}s"
            raise GitFailedError(msg)
        return git_bounded(worktree, argv, timeout)

    for module in modules:
        monkeypatch.setattr(module, "git_bounded", _recording)
    return calls
