"""Tests for reading the engine manifest from the default branch (#3216)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.review_engine import manifest_source
from cw.review_engine.manifest_source import (
    ManifestRead,
    ManifestStatus,
    read_default_branch_manifest,
    read_manifest_file,
)
from cw.review_engine.materialize import MaterializeError
from cw.review_engine_manifest_io import (
    EngineManifestError,
    LoadedEngineManifest,
)
from tests._review_engine_helpers import (
    EngineRepo,
    copy_engine_fixture,
    make_engine_repo,
    manifest_directory_engine,
)
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

STUB_DIR = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SKILL = "repo-review"
_MANIFEST_REL = f".claude/skills/{_SKILL}/cw-engine.json"


def _read(
    repo: EngineRepo, *, skill: str = _SKILL, branch: str = "main"
) -> ManifestRead:
    return read_default_branch_manifest(repo.worktree, branch, skill)


def _stub_with(tmp_path: Path, **manifest_overrides: object) -> Path:
    """A copy of the stub engine whose manifest has *overrides* applied."""
    return copy_engine_fixture(
        tmp_path, STUB_DIR, name="stub-copy", **manifest_overrides
    )


def test_manifest_present_is_loaded(make_git_repo: Callable[..., Path]) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    result = _read(repo)

    assert result.status == "loaded"
    assert result.cause == ""
    assert result.detail == ""
    assert result.error is None
    assert isinstance(result.loaded, LoadedEngineManifest)
    assert list(result.loaded.agent_specs) == ["security", "quality"]
    assert "Security role" in result.loaded.agent_specs["security"]
    assert result.loaded.aggregation_prompt.strip()


def test_manifest_through_a_symlinked_skills_dir_is_loaded(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo,
        layout=".claude/skills",
        symlink_to=".agents/skills",
        engine_files=STUB_DIR,
    )

    assert _read(repo).status == "loaded"


def test_manifest_absent(make_git_repo: Callable[..., Path]) -> None:
    repo = make_engine_repo(make_git_repo)

    result = _read(repo)

    assert result.status == "absent"
    assert result.cause == "no_codex_manifest"
    assert _SKILL in result.detail
    assert result.loaded is None
    assert result.error is None


def test_single_file_command_layout_is_absent(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, layout=".claude/commands")

    result = _read(repo)

    assert result.status == "absent"
    assert result.cause == "no_codex_manifest"
    assert "single-file" in result.detail


def test_invalid_manifest(make_git_repo: Callable[..., Path], tmp_path: Path) -> None:
    stub = _stub_with(tmp_path, roles=[])
    repo = make_engine_repo(make_git_repo, engine_files=stub)

    result = _read(repo)

    assert result.status == "invalid"
    assert result.cause == "manifest_invalid"
    assert result.detail == "schema_invalid: roles"
    assert isinstance(result.error, EngineManifestError)
    assert result.loaded is None


def test_invalid_manifest_without_codex_is_codex_not_declared(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    stub = _stub_with(tmp_path, backends=["opencode"])
    repo = make_engine_repo(make_git_repo, engine_files=stub)

    result = _read(repo)

    assert result.status == "invalid"
    assert result.cause == "codex_not_declared"
    assert result.detail == "schema_invalid: backends.0"


def test_invalid_manifest_with_a_missing_referenced_file(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    stub = _stub_with(tmp_path, aggregation_prompt="prompts/nope.md")
    repo = make_engine_repo(make_git_repo, engine_files=stub)

    result = _read(repo)

    assert result.status == "invalid"
    assert result.cause == "manifest_invalid"
    assert result.detail == "path_invalid: aggregation_prompt"


def test_unparseable_manifest_has_a_dash_field_path(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    stub = _stub_with(tmp_path)
    (stub / "cw-engine.json").write_text("{nope", encoding="utf-8")
    repo = make_engine_repo(make_git_repo, engine_files=stub)

    result = _read(repo)

    assert result.status == "invalid"
    assert result.detail == "not_json: -"


def test_skill_not_found_is_unresolvable(make_git_repo: Callable[..., Path]) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    result = _read(repo, skill="no-such-skill")

    assert result.status == "unresolvable"
    assert result.cause == "skill_not_found"
    assert "no-such-skill" in result.detail


def test_default_branch_unresolved_is_unresolvable(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    result = _read(repo, branch="no-such-branch")

    assert result.status == "unresolvable"
    assert result.cause == "default_branch_unresolved"
    assert result.detail == "no-such-branch"


def test_manifest_only_on_the_feature_branch_is_absent(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo)
    manifest = (STUB_DIR / "cw-engine.json").read_text(encoding="utf-8")
    commit_tracked_file(repo.worktree, _MANIFEST_REL, manifest)
    assert git_in(repo.worktree, "branch", "--show-current") == "dev/1"

    result = _read(repo)

    assert result.status == "absent"
    assert result.cause == "no_codex_manifest"


def test_manifest_removed_on_the_feature_branch_is_still_loaded(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    git_in(repo.worktree, "rm", "-q", _MANIFEST_REL)
    git_in(repo.worktree, "commit", "-m", "drop manifest")

    assert _read(repo).status == "loaded"


def test_reads_the_remote_tracking_default_branch(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    git_in(repo.worktree, "branch", "-D", "main")

    assert _read(repo).status == "loaded"


def test_never_fetches_and_leaves_the_worktree_alone(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)
    argvs: list[list[str]] = []
    real = manifest_source.run_git

    def recording(
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        check: bool = False,
        capture_output: bool = False,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argvs.append(list(argv))
        return real(
            argv,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
        )

    before = sorted(p.name for p in repo.worktree.iterdir())
    status = git_in(repo.worktree, "status", "--porcelain")
    monkeypatch.setattr(manifest_source, "run_git", recording)

    assert _read(repo).status == "loaded"

    assert [argv[0] for argv in argvs] == ["archive"]
    assert sorted(p.name for p in repo.worktree.iterdir()) == before
    assert git_in(repo.worktree, "status", "--porcelain") == status
    assert not (repo.worktree / "cw-engine.json").exists()


def test_archive_failure_is_materialize_failed(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    def boom(*_args: object, **_kwargs: object) -> bytes:
        raise subprocess.CalledProcessError(128, "archive")

    monkeypatch.setattr(manifest_source, "_archive", boom)

    result = _read(repo)

    assert result.status == "unresolvable"
    assert result.cause == "materialize_failed"
    assert "\n" not in result.detail
    assert "128" in result.detail


def test_extraction_cap_failure_is_materialize_failed(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    def too_many(_data: bytes, _destination: Path) -> None:
        cause, detail = "materialize_failed", "too many files"
        raise MaterializeError(cause, detail)

    monkeypatch.setattr(manifest_source, "_extract_archive", too_many)

    result = _read(repo)

    assert result.status == "unresolvable"
    assert result.cause == "materialize_failed"
    assert "too many files" in result.detail


def test_os_error_is_materialize_failed(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_engine_repo(make_git_repo, engine_files=STUB_DIR)

    def full(*_args: object, **_kwargs: object) -> bytes:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(manifest_source, "_archive", full)

    assert _read(repo).cause == "materialize_failed"


def test_manifest_that_is_a_directory_is_invalid(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    """#3445: the default-branch read now agrees with round time (was ABSENT)."""
    engine = manifest_directory_engine(tmp_path, STUB_DIR)
    repo = make_engine_repo(make_git_repo, engine_files=engine)

    result = _read(repo)

    assert result.status == "invalid"
    assert result.cause == "manifest_invalid"
    assert result.detail == "unreadable: -"
    assert result.loaded is None
    assert isinstance(result.error, EngineManifestError)


class TestReadManifestFile:
    """#3445: the one absent/invalid/loaded reader behind both read sites."""

    def test_a_missing_path_is_absent(self, tmp_path: Path) -> None:
        result = read_manifest_file(tmp_path / "cw-engine.json", _SKILL)

        assert result == ManifestRead(
            status=ManifestStatus.ABSENT,
            cause="no_codex_manifest",
            detail=f"{_SKILL} has no cw-engine.json",
        )

    def test_an_invalid_manifest_carries_reason_and_field_path(
        self, tmp_path: Path
    ) -> None:
        stub = _stub_with(tmp_path, roles=[])

        result = read_manifest_file(stub / "cw-engine.json", _SKILL)

        assert result.status is ManifestStatus.INVALID
        assert result.cause == "manifest_invalid"
        assert result.detail == "schema_invalid: roles"
        assert isinstance(result.error, EngineManifestError)
        assert result.loaded is None

    def test_a_manifest_without_codex_is_codex_not_declared(
        self, tmp_path: Path
    ) -> None:
        stub = _stub_with(tmp_path, backends=["opencode"])

        result = read_manifest_file(stub / "cw-engine.json", _SKILL)

        assert result.status is ManifestStatus.INVALID
        assert result.cause == "codex_not_declared"

    def test_a_valid_manifest_is_loaded(self) -> None:
        result = read_manifest_file(STUB_DIR / "cw-engine.json", _SKILL)

        assert result.status is ManifestStatus.LOADED
        assert (result.cause, result.detail, result.error) == ("", "", None)
        assert isinstance(result.loaded, LoadedEngineManifest)


def test_manifest_read_defaults() -> None:
    result = ManifestRead(status="absent", cause="no_codex_manifest", detail="d")

    assert result.loaded is None
    assert result.error is None
