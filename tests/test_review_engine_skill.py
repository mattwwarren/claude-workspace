"""Tests for ``cw.review_engine.skill``: default-branch skill resolution (R4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.review_engine.skill import (
    SKILL_LAYOUTS,
    GuardSet,
    ResolvedSkill,
    SkillResolutionError,
    guard_patterns,
    resolve_skill,
)
from tests._review_engine_helpers import (
    commit_symlink,
    make_engine_repo,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NAME = "repo-review"


def _resolve(repo: Path, commit: str = "main") -> ResolvedSkill:
    return resolve_skill(repo, git_in(repo, "rev-parse", commit), NAME)


@pytest.mark.parametrize(
    ("layout", "probe", "real", "is_file"),
    [
        (
            ".claude/skills",
            f".claude/skills/{NAME}/SKILL.md",
            f".claude/skills/{NAME}",
            False,
        ),
        (
            ".agents/skills",
            f".agents/skills/{NAME}/SKILL.md",
            f".agents/skills/{NAME}",
            False,
        ),
        (
            ".claude/commands",
            f".claude/commands/{NAME}.md",
            f".claude/commands/{NAME}.md",
            True,
        ),
    ],
)
def test_layout_probe(
    make_git_repo: Callable[..., Path],
    layout: str,
    probe: str,
    real: str,
    is_file: bool,
) -> None:
    repo = make_engine_repo(make_git_repo, layout=layout).worktree

    skill = _resolve(repo)

    assert skill.probe_path == probe
    assert skill.real_path == real
    assert skill.is_file is is_file


def test_layout_precedence_first_in_probe_order_wins(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, layout=".claude/commands").worktree
    git_in(repo, "checkout", "main")
    commit_tracked_file(repo, f".agents/skills/{NAME}/SKILL.md", "agents\n")

    assert _resolve(repo).layout == SKILL_LAYOUTS[1]

    commit_tracked_file(repo, f".claude/skills/{NAME}/SKILL.md", "claude\n")

    assert _resolve(repo).layout == SKILL_LAYOUTS[0]


def test_command_file_resolves_to_kind_file(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, layout=".claude/commands").worktree

    skill = _resolve(repo)

    assert skill.is_file is True
    assert skill.layout == ".claude/commands/{n}.md"


def test_symlinked_skills_dir_resolves_to_real_agents_path(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    ).worktree

    skill = _resolve(repo)

    assert skill.probe_path == f".claude/skills/{NAME}/SKILL.md"
    assert skill.real_path == f".agents/skills/{NAME}"
    # The worktree is never consulted: removing the link there changes nothing.
    (repo / ".claude" / "skills").unlink()
    assert _resolve(repo).real_path == f".agents/skills/{NAME}"


def test_symlink_chain_depth_cap_is_unresolvable(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("chain")
    commit_tracked_file(repo, f"c9/{NAME}/SKILL.md")
    for index in range(9):
        commit_symlink(repo, f"c{index}", f"c{index + 1}")
    commit_symlink(repo, ".claude/skills", "../c0")

    with pytest.raises(SkillResolutionError) as caught:
        _resolve(repo, "HEAD")

    assert caught.value.cause == "skill_unreadable"


def test_symlink_chain_within_cap_resolves(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("short-chain")
    commit_tracked_file(repo, f"c2/{NAME}/SKILL.md")
    commit_symlink(repo, "c1", "c2")
    commit_symlink(repo, "c0", "c1")
    commit_symlink(repo, ".claude/skills", "../c0")

    assert _resolve(repo, "HEAD").real_path == f"c2/{NAME}"


@pytest.mark.parametrize(
    "target", ["../../outside", "/etc"], ids=["escape", "absolute"]
)
def test_symlink_escaping_repo_is_unresolvable(
    make_git_repo: Callable[..., Path], target: str
) -> None:
    repo = make_git_repo("escape")
    commit_symlink(repo, ".claude/skills", target)

    with pytest.raises(SkillResolutionError) as caught:
        _resolve(repo, "HEAD")

    assert caught.value.cause == "skill_unreadable"


def test_missing_skill_raises_skill_not_found(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("empty")

    with pytest.raises(SkillResolutionError) as caught:
        _resolve(repo, "HEAD")

    assert caught.value.cause == "skill_not_found"
    assert NAME in str(caught.value)


def test_dangling_symlink_is_skill_not_found(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("dangling")
    commit_symlink(repo, ".claude/skills", "../nowhere")

    with pytest.raises(SkillResolutionError) as caught:
        _resolve(repo, "HEAD")

    assert caught.value.cause == "skill_not_found"


def test_directory_at_probe_path_is_not_a_skill(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("dir-probe")
    commit_tracked_file(repo, f".claude/skills/{NAME}/SKILL.md/inner.txt")

    with pytest.raises(SkillResolutionError) as caught:
        _resolve(repo, "HEAD")

    assert caught.value.cause == "skill_not_found"


def test_guard_set_contains_all_three_probe_paths_and_real_path(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    ).worktree
    skill = _resolve(repo)

    guard = guard_patterns(skill, NAME, ("docs/**",))

    assert {
        f".claude/skills/{NAME}/SKILL.md",
        f".agents/skills/{NAME}/SKILL.md",
        f".claude/commands/{NAME}.md",
        f".claude/skills/{NAME}",
        f".agents/skills/{NAME}",
        skill.real_path,
    } <= guard.prefixes
    assert guard.globs == ("docs/**",)


def test_guarded_paths_glob_uses_full_match_semantics() -> None:
    guard = GuardSet(globs=("docs/**/*.md", "*.cfg"))

    assert guard.touched(["docs/a.md", "docs/x/y/b.md", "docs/x/c.txt"]) == [
        "docs/a.md",
        "docs/x/y/b.md",
    ]
    assert guard.touched(["top.cfg"]) == ["top.cfg"]
    assert guard.touched(["sub/top.cfg"]) == []


def test_path_matching_skill_dir_prefix_not_sibling_prefix() -> None:
    guard = GuardSet(prefixes=frozenset({f".claude/skills/{NAME}"}))

    assert guard.touched(
        [
            f".claude/skills/{NAME}/SKILL.md",
            f".claude/skills/{NAME}",
            f".claude/skills/{NAME}-x/SKILL.md",
        ]
    ) == [f".claude/skills/{NAME}/SKILL.md", f".claude/skills/{NAME}"]


def test_touched_deduplicates_and_keeps_order() -> None:
    guard = GuardSet(prefixes=frozenset({"a"}))

    assert guard.touched(["a/1", "b", "a/1", "a/0"]) == ["a/1", "a/0"]
