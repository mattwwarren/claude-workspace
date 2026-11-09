"""Materialize the engine skill from the default branch (RFC 0017 R4, R6).

Copies the skill directory from a git commit into the worktree's round
directory, so the session can read it inline. The copy comes from git objects,
never the worktree. Extraction is bounded and uses the tarfile ``data`` filter.
A dirty tree or a moved HEAD refuses before anything is written, because the
engine round must describe exactly the commit it was pinned to (#2087).
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cw._git import run_git
from cw.exceptions import CwError
from cw.models.review_engine_summary import REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION
from cw.review_convergence import resolve_default_branch_ref
from cw.review_engine.constants import (
    ENGINE_ARGS_PREFIX,
    ENGINE_SKILL_SUBDIR,
    ENGINE_STATE_RELATIVE_DIR,
    ENGINE_SUMMARY_FILENAME,
    MAX_SKILL_BYTES,
    MAX_SKILL_FILES,
    ROUND_ID_RE,
)
from cw.review_engine.skill import (
    ResolvedSkill,
    SkillResolutionError,
    resolve_skill,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_engine.context import EngineContext

_CAUSE_MATERIALIZE_FAILED = "materialize_failed"
_CAUSE_DEFAULT_UNRESOLVED = "default_branch_unresolved"
_TOO_MANY_FILES = "too many files"
_TOO_MANY_BYTES = "too many bytes"


class EnginePreconditionError(CwError):
    """A precondition failed before anything was written (exit 1, a fault)."""


class MaterializeError(CwError):
    """The skill could not be materialized. ``cause`` names why (exit 3)."""

    def __init__(self, cause: str, detail: str) -> None:
        self.cause = cause
        super().__init__(f"engine skill unresolvable: {cause} ({detail})")


@dataclass(frozen=True)
class EnginePaths:
    """Where one round's skill copy and summary live, under the worktree."""

    round_dir: Path
    skill_dir: Path
    summary_path: Path


@dataclass(frozen=True)
class MaterializedSkill:
    """What ``materialize`` prints: the paths and the args line for the prose."""

    skill_dir: Path
    skill_md: Path
    summary_path: Path
    args_line: str


def check_round_id(round_id: str) -> None:
    """Refuse a round id that is not a single safe path segment."""
    if re.fullmatch(ROUND_ID_RE, round_id) is None:
        msg = f"round id {round_id!r} is not a safe path segment"
        raise EnginePreconditionError(msg)


def engine_paths(worktree: Path, round_id: str) -> EnginePaths:
    """The round directory, skill directory and summary path for *round_id*."""
    check_round_id(round_id)
    round_dir = worktree / ENGINE_STATE_RELATIVE_DIR / round_id
    return EnginePaths(
        round_dir=round_dir,
        skill_dir=round_dir / ENGINE_SKILL_SUBDIR,
        summary_path=round_dir / ENGINE_SUMMARY_FILENAME,
    )


def _git(worktree: Path, argv: list[str]) -> bytes:
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        run_git(
            argv,
            cwd=worktree,
            capture_output=True,
            check=True,
            text=False,
        ),
    )
    return result.stdout


def _git_text(worktree: Path, argv: list[str]) -> str:
    return _git(worktree, argv).decode()


def _require_precondition(worktree: Path, expect_head: str) -> None:
    head = _git_text(worktree, ["rev-parse", "HEAD"]).strip()
    if head != expect_head:
        msg = (
            f"HEAD is {head[:12]}, expected {expect_head[:12]}; refusing to materialize"
        )
        raise EnginePreconditionError(msg)
    dirty = _git_text(worktree, ["status", "--porcelain"]).strip()
    stat = _git_text(worktree, ["diff", "HEAD", "--stat"]).strip()
    if dirty or stat:
        msg = (
            "worktree is dirty; the engine round needs a clean tree "
            "(git status and git diff HEAD --stat must be empty, #2087)"
        )
        raise EnginePreconditionError(msg)


def _extract_archive(data: bytes, destination: Path) -> None:
    """Extract a ``git archive`` tar into *destination* under the caps."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            files = [m for m in members if m.isfile()]
            if len(files) > MAX_SKILL_FILES:
                raise MaterializeError(_CAUSE_MATERIALIZE_FAILED, _TOO_MANY_FILES)
            if sum(m.size for m in files) > MAX_SKILL_BYTES:
                raise MaterializeError(_CAUSE_MATERIALIZE_FAILED, _TOO_MANY_BYTES)
            archive.extractall(destination, filter="data")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise MaterializeError(_CAUSE_MATERIALIZE_FAILED, str(exc)) from exc


def _write_skill(
    worktree: Path, commit: str, skill: ResolvedSkill, paths: EnginePaths
) -> None:
    if paths.skill_dir.exists() or paths.skill_dir.is_symlink():
        shutil.rmtree(paths.skill_dir)
    paths.skill_dir.mkdir(parents=True)
    if skill.is_file:
        data = _git(worktree, ["show", f"{commit}:{skill.real_path}"])
        (paths.skill_dir / "SKILL.md").write_bytes(data)
        return
    data = _git(worktree, ["archive", "--format=tar", f"{commit}:{skill.real_path}"])
    _extract_archive(data, paths.skill_dir)


def materialize_skill(
    worktree: Path,
    *,
    round_id: str,
    expect_head: str,
    context: EngineContext,
) -> MaterializedSkill:
    """Copy the default-branch skill into the round directory; build the args line."""
    paths = engine_paths(worktree, round_id)
    _require_precondition(worktree, expect_head)
    ref = resolve_default_branch_ref(worktree, context.default_branch)
    if ref is None:
        raise MaterializeError(_CAUSE_DEFAULT_UNRESOLVED, context.default_branch)
    try:
        skill = resolve_skill(worktree, ref.commit, context.skill)
    except SkillResolutionError as exc:
        raise MaterializeError(exc.cause, context.skill) from exc
    paths.round_dir.mkdir(parents=True, exist_ok=True)
    if paths.summary_path.exists():
        paths.summary_path.unlink()
    try:
        _write_skill(worktree, ref.commit, skill, paths)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MaterializeError(_CAUSE_MATERIALIZE_FAILED, str(exc)) from exc
    skill_md = (paths.skill_dir / "SKILL.md").resolve()
    summary = paths.summary_path.resolve()
    skill_dir = paths.skill_dir.resolve()
    args_line = (
        f"{ENGINE_ARGS_PREFIX} schema_version={REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION} "
        f"head_sha={expect_head} summary_path={summary} base_dir={skill_dir}"
    )
    return MaterializedSkill(
        skill_dir=skill_dir,
        skill_md=skill_md,
        summary_path=summary,
        args_line=args_line,
    )
