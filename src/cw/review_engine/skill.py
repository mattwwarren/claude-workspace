"""Resolve the engine skill in the default-branch tree (RFC 0017 R4).

Every lookup reads git objects at one commit (``git ls-tree`` and
``git cat-file``), never the worktree, so a branch that edits the skill cannot
change which skill runs or what it guards. Symlinks are followed inside the
repository only, at most ``MAX_SYMLINK_HOPS`` times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    import subprocess

from cw._git import run_git
from cw.exceptions import CwError
from cw.review_engine.constants import (
    CAUSE_SKILL_NOT_FOUND,
    CAUSE_SKILL_UNREADABLE,
    MAX_SYMLINK_HOPS,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Probe order: the first layout that resolves wins.
SKILL_LAYOUTS: Final = (
    ".claude/skills/{n}/SKILL.md",
    ".agents/skills/{n}/SKILL.md",
    ".claude/commands/{n}.md",
)

_SYMLINK_MODE: Final = "120000"
_COMMAND_LAYOUT: Final = ".claude/commands/{n}.md"


class SkillResolutionError(CwError):
    """The skill cannot be resolved.

    ``cause`` is ``skill_not_found`` or ``skill_unreadable``.
    """

    def __init__(self, cause: str, skill: str) -> None:
        self.cause = cause
        msg = f"engine skill {skill!r} unresolvable: {cause}"
        super().__init__(msg)


def _unreadable(detail: str) -> SkillResolutionError:
    return SkillResolutionError(CAUSE_SKILL_UNREADABLE, detail)


@dataclass(frozen=True)
class ResolvedSkill:
    """Where the skill lives at the commit.

    ``real_path`` is the skill directory for a directory layout, or the command
    file for ``is_file``. ``probe_path`` is the layout path that matched.
    """

    layout: str
    probe_path: str
    real_path: str
    is_file: bool


@dataclass(frozen=True)
class GuardSet:
    """Paths whose touch parks an engine round, decided at the default branch."""

    prefixes: frozenset[str] = field(default_factory=frozenset)
    globs: tuple[str, ...] = ()

    def touched(self, paths: list[str]) -> list[str]:
        """The subset of *paths* the guard covers, in input order."""
        hits: list[str] = []
        for path in paths:
            if self._covers(path) and path not in hits:
                hits.append(path)
        return hits

    def _covers(self, path: str) -> bool:
        for prefix in self.prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return True
        return any(PurePosixPath(path).full_match(glob) for glob in self.globs)


def _ls_tree_entry(
    worktree: Path, commit: str, path: PurePosixPath
) -> tuple[str, str, str] | None:
    """``(mode, type, sha)`` of *path* at *commit*, or ``None`` when absent."""
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        run_git(
            ["ls-tree", "-z", commit, "--", str(path)],
            cwd=worktree,
            capture_output=True,
            check=True,
            text=False,
        ),
    )
    out = result.stdout.decode()
    if not out:
        return None
    meta = out.split("\t", 1)[0].split()
    return meta[0], meta[1], meta[2]


def _read_link(worktree: Path, sha: str) -> str:
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        run_git(
            ["cat-file", "blob", sha],
            cwd=worktree,
            capture_output=True,
            check=True,
            text=False,
        ),
    )
    return result.stdout.decode().strip()


def _normalize(path: str) -> str:
    """Collapse ``.`` and ``..``; raise when the path escapes the repository."""
    out: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not out:
                raise _unreadable(path)
            out.pop()
        else:
            out.append(segment)
    return "/".join(out)


def _real_path(
    worktree: Path, commit: str, rel: PurePosixPath, hops: int
) -> PurePosixPath | None:
    """The symlink-free path of *rel* at *commit*, or ``None`` when absent."""
    resolved = PurePosixPath()
    parts = rel.parts
    for index, part in enumerate(parts):
        candidate = resolved / part
        entry = _ls_tree_entry(worktree, commit, candidate)
        if entry is None:
            return None
        mode, _kind, sha = entry
        if mode == _SYMLINK_MODE:
            if hops >= MAX_SYMLINK_HOPS:
                raise _unreadable(str(rel))
            target = _read_link(worktree, sha)
            if target.startswith("/"):
                raise _unreadable(str(rel))
            joined = PurePosixPath(_normalize(str(resolved / target)))
            return _real_path(
                worktree, commit, joined.joinpath(*parts[index + 1 :]), hops + 1
            )
        resolved = candidate
    return resolved


def resolve_skill(worktree: Path, commit: str, name: str) -> ResolvedSkill:
    """Resolve the skill *name* at *commit* through :data:`SKILL_LAYOUTS`."""
    for layout in SKILL_LAYOUTS:
        probe = layout.format(n=name)
        real = _real_path(worktree, commit, PurePosixPath(probe), 0)
        if real is None:
            continue
        entry = _ls_tree_entry(worktree, commit, real)
        if entry is None or entry[1] != "blob":
            continue
        is_file = layout == _COMMAND_LAYOUT
        real_text = str(real) if is_file else str(real.parent)
        return ResolvedSkill(
            layout=layout,
            probe_path=probe,
            real_path=real_text,
            is_file=is_file,
        )
    raise SkillResolutionError(CAUSE_SKILL_NOT_FOUND, name)


def guard_patterns(
    skill: ResolvedSkill, name: str, guarded: tuple[str, ...] | list[str]
) -> GuardSet:
    """Every probe path, its skill directory, the resolved real path, and the globs."""
    prefixes: set[str] = set()
    for layout in SKILL_LAYOUTS:
        prefixes.add(layout.format(n=name))
        if layout != _COMMAND_LAYOUT:
            # The skill directory itself: a symlink at this path is a probe too.
            prefixes.add(str(PurePosixPath(layout).parents[1] / name))
    prefixes.add(skill.real_path)
    return GuardSet(prefixes=frozenset(prefixes), globs=tuple(guarded))
