"""Tests for ``cw.changelog_sync`` (#3190): re-home a hunk a release cut misfiled.

Every scenario is real git (``tests._changelog_cut_helpers.cut_straddle_repo``):
a branch green on the pre-cut base whose ``origin/main`` is the post-cut main.
One test per row of the plan's state/disposition/exit table, plus the CRLF
and mixed-ending cases and the helper-move enforcement test.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw import changelog_sync
from cw.changelog_sync import DISPOSITION_DIRTY_WORKTREE, sync_changelog
from cw.merge_probe import GitFailedError, MergeTreeResult
from tests import _changelog_cut_helpers, _git_sandbox
from tests._changelog_cut_helpers import (
    CL_BASE,
    CL_BRANCH,
    CL_EXPECTED,
    CL_EXPECTED_POPULATED,
    CL_MAIN,
    CL_MAIN_POPULATED,
    GAMMA,
    RELEASED_1_0_0,
    ReleaseMovedRepo,
    commit_files,
    cut_straddle_repo,
    pyproject,
)
from tests._shared.git_repos import git_in, init_bare_origin

if TYPE_CHECKING:
    from collections.abc import Callable

_TESTS = Path(__file__).resolve().parent
_MOVED = frozenset(
    {
        "TICKET",
        "BRANCH",
        "PREAMBLE",
        "RELEASED_1_0_0",
        "RELEASED_1_1_0",
        "CL_BASE",
        "CL_BRANCH",
        "CL_MAIN",
        "CL_EXPECTED",
        "ReleaseMovedRepo",
        "commit_files",
        "build_release_moved_repo",
    }
)


@pytest.fixture
def straddle(tmp_path: Path, make_git_repo: Callable[..., Path]) -> ReleaseMovedRepo:
    return cut_straddle_repo(tmp_path, make_git_repo)


def _head(repo: Path) -> str:
    return git_in(repo, "rev-parse", "HEAD")


def _state(repo: Path) -> tuple[str, str, str]:
    """HEAD, the index and the worktree status: unchanged means nothing written."""
    return (
        _head(repo),
        git_in(repo, "ls-files", "-s"),
        git_in(repo, "status", "--porcelain"),
    )


def _merge_head(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


def _committed_changelog(repo: Path) -> bytes:
    shown = _git_sandbox.run("show", "HEAD:CHANGELOG.md", cwd=str(repo), text=False)
    assert isinstance(shown.stdout, bytes)
    return shown.stdout


def _released_part(text: str) -> str:
    return text[text.index("## [1.1.0]") :]


# --- moved / created-subsection ----------------------------------------------


def test_moved_merges_once_and_rehomes_the_bullet(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    scenario = cut_straddle_repo(tmp_path, make_git_repo, populated_unreleased=True)
    clone = scenario.clone

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == ("moved", None)
    assert result.inserted == (GAMMA,)
    assert result.conflicted_files == ()
    assert result.base_sha == scenario.main_sha
    assert result.head_sha == _head(clone)
    parents = git_in(clone, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [scenario.branch_sha, scenario.main_sha]
    text = _committed_changelog(clone).decode("utf-8")
    assert text == CL_EXPECTED_POPULATED
    assert _released_part(text) == _released_part(CL_MAIN_POPULATED)
    assert git_in(clone, "status", "--porcelain") == ""
    assert not _merge_head(clone)


def test_created_subsection_matches_the_expected_text(
    straddle: ReleaseMovedRepo,
) -> None:
    result = sync_changelog(straddle.clone, "main")

    assert (result.state, result.disposition, result.inserted) == (
        "moved",
        None,
        (GAMMA,),
    )
    assert _committed_changelog(straddle.clone).decode("utf-8") == CL_EXPECTED


def test_a_second_run_is_clean(straddle: ReleaseMovedRepo) -> None:
    first = sync_changelog(straddle.clone, "main")
    assert first.state == "moved"

    second = sync_changelog(straddle.clone, "main")

    assert (second.state, second.head_sha, second.inserted) == (
        "clean",
        first.head_sha,
        (),
    )


def test_a_git_dir_decoy_does_not_redirect_it(
    straddle: ReleaseMovedRepo,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy = make_git_repo("decoy")
    decoy_head = _head(decoy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    result = sync_changelog(straddle.clone, "main")

    monkeypatch.delenv("GIT_DIR")
    assert result.state == "moved"
    assert _head(decoy) == decoy_head
    assert _committed_changelog(straddle.clone).decode("utf-8") == CL_EXPECTED


# --- clean --------------------------------------------------------------------


def test_clean_for_a_branch_that_never_touched_the_changelog(
    straddle: ReleaseMovedRepo,
) -> None:
    clone = straddle.clone
    git_in(clone, "checkout", "-q", "-B", "code-only", straddle.base_sha)
    commit_files(clone, {"src/app.py": "def app():\n    return 3\n"}, "code")
    before = _state(clone)

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition, result.inserted) == ("clean", None, ())
    assert result.head_sha == before[0]
    assert _state(clone) == before


def test_clean_when_origin_base_has_no_changelog(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("client")
    commit_files(repo, {"src/app.py": "A = 1\n"}, "base")
    init_bare_origin(repo)
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"src/app.py": "A = 2\n"}, "feature")
    git_in(repo, "checkout", "-q", "main")
    commit_files(repo, {"src/other.py": "B = 1\n"}, "main moves on")
    git_in(repo, "push", "-q", "origin", "main")
    git_in(repo, "checkout", "-q", "feature")
    before = _state(repo)

    result = sync_changelog(repo, "main")

    assert (result.state, result.disposition) == ("clean", None)
    assert _state(repo) == before


def test_a_dirty_tree_with_nothing_to_do_is_still_clean(
    straddle: ReleaseMovedRepo,
) -> None:
    clone = straddle.clone
    git_in(clone, "checkout", "-q", "-B", "code-only", straddle.base_sha)
    commit_files(clone, {"src/app.py": "def app():\n    return 3\n"}, "code")
    (clone / "src" / "other.py").write_text("OTHER = 9\n", encoding="utf-8")

    assert sync_changelog(clone, "main").state == "clean"


# --- refused --------------------------------------------------------------------


def test_up_to_date_misfiled_is_refused(straddle: ReleaseMovedRepo) -> None:
    clone = straddle.clone
    git_in(clone, "merge", "-q", "--no-edit", "origin/main")
    before = _state(clone)
    assert GAMMA in _released_part((clone / "CHANGELOG.md").read_text("utf-8"))

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == (
        "refused",
        "changelog_released_section_touched",
    )
    assert result.inserted == ()
    assert _state(clone) == before


def test_a_reworded_merge_base_bullet_is_ambiguous(
    straddle: ReleaseMovedRepo,
) -> None:
    clone = straddle.clone
    reworded = CL_BRANCH.replace("- Beta fix (#2).", "- Beta fix, reworded (#2).")
    commit_files(clone, {"CHANGELOG.md": reworded}, "reword beta")
    before = _state(clone)

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == ("refused", "changelog_ambiguous")
    assert "Beta fix" in result.detail
    assert _state(clone) == before


def test_an_edited_released_bullet_is_refused(straddle: ReleaseMovedRepo) -> None:
    clone = straddle.clone
    edited = CL_BRANCH.replace(RELEASED_1_0_0, RELEASED_1_0_0.replace("#0", "#00"))
    commit_files(clone, {"CHANGELOG.md": edited}, "edit 1.0.0")
    before = _state(clone)

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == (
        "refused",
        "changelog_released_section_touched",
    )
    assert _state(clone) == before


def test_a_dirty_worktree_is_refused_when_a_move_is_needed(
    straddle: ReleaseMovedRepo,
) -> None:
    clone = straddle.clone
    (clone / "src" / "other.py").write_text("OTHER = 9\n", encoding="utf-8")
    before = _state(clone)

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == (
        "refused",
        DISPOSITION_DIRTY_WORKTREE,
    )
    assert DISPOSITION_DIRTY_WORKTREE == "dirty_worktree"
    assert _state(clone) == before
    assert not _merge_head(clone)


def test_mixed_line_endings_are_refused_not_normalised(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    mixed = CL_MAIN.replace("# Changelog\n", "# Changelog\r\n", 1)
    scenario = cut_straddle_repo(tmp_path, make_git_repo, main_changelog=mixed)
    before = _state(scenario.clone)

    result = sync_changelog(scenario.clone, "main")

    assert (result.state, result.disposition) == ("refused", "changelog_unparseable")
    assert "mixed line endings" in result.detail
    assert _state(scenario.clone) == before


def test_a_non_utf8_changelog_is_refused(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = changelog_sync.run_git

    def _latin1(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if argv[0] == "show" and kwargs.get("text") is False:
            return subprocess.CompletedProcess(argv, 0, b"\xff\xfe", b"")
        return real(argv, **kwargs)

    monkeypatch.setattr(changelog_sync, "run_git", _latin1)
    before = _state(straddle.clone)

    result = sync_changelog(straddle.clone, "main")

    assert (result.state, result.disposition) == ("refused", "changelog_unparseable")
    assert "UTF-8" in result.detail
    assert _state(straddle.clone) == before


# --- conflict / unavailable ------------------------------------------------------


def test_a_code_conflict_is_conflict(straddle: ReleaseMovedRepo) -> None:
    clone = straddle.clone
    commit_files(clone, {"pyproject.toml": pyproject("1.0.1")}, "bump")
    before = _state(clone)

    result = sync_changelog(clone, "main")

    assert (result.state, result.disposition) == ("conflict", None)
    assert result.conflicted_files == ("pyproject.toml",)
    assert "Step 4c.5" in result.detail
    assert _state(clone) == before
    assert not _merge_head(clone)


def test_an_unresolvable_base_is_unavailable(straddle: ReleaseMovedRepo) -> None:
    before = _state(straddle.clone)

    result = sync_changelog(straddle.clone, "nope")

    assert (result.state, result.base_sha) == ("unavailable", None)
    assert result.detail == "origin/nope does not resolve"
    assert result.head_sha == before[0]
    assert _state(straddle.clone) == before


def test_a_failed_commit_aborts_the_merge(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = changelog_sync.git_bounded

    def _failing_commit(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "commit":
            return subprocess.CompletedProcess(argv, 1, "", "hook said no\n")
        return real(worktree, argv, timeout)

    monkeypatch.setattr(changelog_sync, "git_bounded", _failing_commit)
    before = _state(straddle.clone)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "git commit exited 1: hook said no" in result.detail
    assert not _merge_head(straddle.clone)
    assert _state(straddle.clone) == before


def test_a_failed_abort_is_named_in_the_detail(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = changelog_sync.git_bounded

    def _failing(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "commit":
            msg = "git commit timed out after 120s"
            raise GitFailedError(msg)
        if argv[:2] == ["merge", "--abort"]:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: no merge\n")
        return real(worktree, argv, timeout)

    monkeypatch.setattr(changelog_sync, "git_bounded", _failing)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "git commit timed out after 120s" in result.detail
    assert "git merge --abort failed" in result.detail
    git_in(straddle.clone, "merge", "--abort")


def test_a_merge_tree_error_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unavailable(*_a: object, **_k: object) -> MergeTreeResult:
        return MergeTreeResult("unavailable", None, (), "too old")

    monkeypatch.setattr(changelog_sync, "merge_tree", _unavailable)

    result = sync_changelog(straddle.clone, "main")

    assert (result.state, result.detail) == ("unavailable", "too old")


def test_a_blob_read_timeout_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        cmd = "git show"
        raise subprocess.TimeoutExpired(cmd, 10.0)

    monkeypatch.setattr(changelog_sync, "run_git", _timeout)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "timed out" in result.detail


def test_an_ancestry_error_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = changelog_sync.git_bounded

    def _broken_merge_base(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "merge-base":
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad\n")
        return real(worktree, argv, timeout)

    monkeypatch.setattr(changelog_sync, "git_bounded", _broken_merge_base)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "git merge-base exited 128" in result.detail


# --- line endings ------------------------------------------------------------------


def test_crlf_changelog_is_preserved_byte_for_byte(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    scenario = cut_straddle_repo(tmp_path, make_git_repo, changelog_eol="\r\n")

    result = sync_changelog(scenario.clone, "main")

    assert result.state == "moved"
    committed = _committed_changelog(scenario.clone)
    assert committed == CL_EXPECTED.replace("\n", "\r\n").encode("utf-8")
    assert b"\n" not in committed.replace(b"\r\n", b"")


# --- the helper move (#3190 R7) ----------------------------------------------------


def _moved_names_taken_from_the_repair_helpers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "tests._changelog_repair_helpers"
        ):
            found.extend(alias.name for alias in node.names if alias.name in _MOVED)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_changelog_repair_helpers"
            and node.attr in _MOVED
        ):
            found.append(node.attr)
    return found


def test_moved_builders_are_imported_from_the_cut_helpers_only() -> None:
    offenders = {
        path.name: names
        for path in sorted(_TESTS.glob("*.py"))
        if path.name != "_changelog_repair_helpers.py"
        and (names := _moved_names_taken_from_the_repair_helpers(path))
    }

    assert offenders == {}
    for name in _MOVED:
        assert hasattr(_changelog_cut_helpers, name), name
    assert CL_BASE.startswith("# Changelog")


def _git_bounded_failing(
    monkeypatch: pytest.MonkeyPatch, fails: Callable[[list[str]], bool]
) -> None:
    """Make ``changelog_sync.git_bounded`` exit 128 for argv *fails* matches."""
    real = changelog_sync.git_bounded

    def _fake(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if fails(argv):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: injected\n")
        return real(worktree, argv, timeout)

    monkeypatch.setattr(changelog_sync, "git_bounded", _fake)


def test_an_unreadable_head_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_bounded_failing(
        monkeypatch, lambda argv: argv == ["rev-parse", "--verify", "HEAD"]
    )

    result = sync_changelog(straddle.clone, "main")

    assert (result.state, result.head_sha) == ("unavailable", None)
    assert result.base_sha == straddle.main_sha
    assert result.detail == "HEAD does not resolve"


def test_a_failed_status_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_bounded_failing(monkeypatch, lambda argv: argv[0] == "status")
    before = _state(straddle.clone)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "git status exited 128" in result.detail
    assert _state(straddle.clone) == before


def test_a_blob_read_oserror_is_unavailable(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _oserror(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        msg = "no git"
        raise OSError(msg)

    monkeypatch.setattr(changelog_sync, "run_git", _oserror)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert "could not run: no git" in result.detail


def test_an_unreadable_merge_state_still_aborts(
    straddle: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = changelog_sync.git_bounded

    def _fake(
        worktree: Path, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "commit":
            return subprocess.CompletedProcess(argv, 1, "", "hook said no\n")
        if argv[-1] == "MERGE_HEAD":
            msg = "git rev-parse timed out after 10s"
            raise GitFailedError(msg)
        return real(worktree, argv, timeout)

    monkeypatch.setattr(changelog_sync, "git_bounded", _fake)
    before = _state(straddle.clone)

    result = sync_changelog(straddle.clone, "main")

    assert result.state == "unavailable"
    assert not _merge_head(straddle.clone)
    assert _state(straddle.clone) == before
