"""Tests for ``cw.review_engine.materialize`` and ``cw review engine materialize``."""

from __future__ import annotations

import io
import json
import re
import tarfile
from typing import TYPE_CHECKING

import pytest

import cw.review_engine.materialize as materialize_module
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_engine.context import read_engine_context
from cw.review_engine.materialize import (
    EnginePreconditionError,
    MaterializeError,
    _extract_archive,
    check_round_id,
    engine_paths,
    materialize_skill,
)
from tests._review_engine_helpers import (
    ENGINE_RUBRIC_TEXT,
    ENGINE_SKILL_TEXT,
    EngineRepo,
    engine_context_payload,
    invoke,
    make_engine_repo,
    write_engine_context,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

SKILL_FILE = ".claude/skills/repo-review/SKILL.md"
ROUND = "r1"
ARGS_LINE = re.compile(
    r"^cw-engine: schema_version=1 head_sha=[0-9a-f]{40} "
    r"summary_path=/\S+/summary\.json base_dir=/\S+/skill$"
)


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


def _init_round(repo: EngineRepo, round_id: str = ROUND) -> None:
    result = invoke(
        "review",
        "workspace",
        "init",
        "--ticket",
        repo.ticket,
        "--round-id",
        round_id,
        "--role",
        ENGINE_ROLE,
        "--worktree",
        str(repo.worktree),
    )
    assert result.exit_code == 0, result.output


def _materialize_cli(
    repo: EngineRepo, *, round_id: str = ROUND, expect_head: str | None = None
) -> Result:
    return invoke(
        "review",
        "engine",
        "materialize",
        "--round-id",
        round_id,
        "--expect-head",
        expect_head or repo.head,
        "--worktree",
        str(repo.worktree),
    )


def _library(repo: EngineRepo, round_id: str = ROUND, head: str | None = None):
    context = read_engine_context(repo.worktree / ".claude" / "cw-context.json")
    return materialize_skill(
        repo.worktree,
        round_id=round_id,
        expect_head=head or repo.head,
        context=context,
    )


def _failures(repo: EngineRepo) -> list[dict[str, str]]:
    result = invoke(
        "review", "workspace", "reconcile", "--worktree", str(repo.worktree)
    )
    assert result.exit_code == 0, result.output
    failed: list[dict[str, str]] = json.loads(result.output)["failed_reviewers"]
    return failed


def test_materializes_default_branch_copy_not_branch_copy(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "branch edit\n")
    head = git_in(repo.worktree, "rev-parse", "HEAD")

    materialized = _library(repo, head=head)

    assert materialized.skill_md.read_text() == ENGINE_SKILL_TEXT
    assert (repo.worktree / SKILL_FILE).read_text() == "branch edit\n"


def test_command_layout_normalised_to_skill_md(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, layout=".claude/commands")

    materialized = _library(repo)

    assert materialized.skill_md.name == "SKILL.md"
    assert materialized.skill_md.read_text() == ENGINE_SKILL_TEXT
    assert [p.name for p in materialized.skill_dir.iterdir()] == ["SKILL.md"]


def test_symlink_layout_materializes_real_directory(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    )

    materialized = _library(repo)

    assert materialized.skill_md.read_text() == ENGINE_SKILL_TEXT
    rubric = materialized.skill_dir / "refs" / "rubric.md"
    assert rubric.read_text() == ENGINE_RUBRIC_TEXT
    assert not materialized.skill_dir.is_symlink()


def test_returns_abs_paths_and_args_line_exact_format(repo: EngineRepo) -> None:
    _init_round(repo)

    result = _materialize_cli(repo)

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert set(body) == {"skill_dir", "skill_md", "summary_path", "args_line"}
    for key in ("skill_dir", "skill_md", "summary_path"):
        assert body[key].startswith("/")
    assert ARGS_LINE.match(body["args_line"]), body["args_line"]
    assert f"head_sha={repo.head}" in body["args_line"]
    assert f"summary_path={body['summary_path']}" in body["args_line"]
    assert f"base_dir={body['skill_dir']}" in body["args_line"]


def test_deletes_existing_summary_first(repo: EngineRepo) -> None:
    stale = engine_paths(repo.worktree, ROUND).summary_path
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")

    materialized = _library(repo)

    assert not materialized.summary_path.exists()


def test_refuses_dirty_tree_before_dispatch(repo: EngineRepo) -> None:
    _init_round(repo)
    (repo.worktree / "stray.txt").write_text("x", encoding="utf-8")

    result = _materialize_cli(repo)

    assert result.exit_code == 1
    assert "#2087" in result.output
    assert not engine_paths(repo.worktree, ROUND).round_dir.exists()


def test_refuses_modified_tracked_file(repo: EngineRepo) -> None:
    tracked = repo.worktree / "src" / "a.py"
    tracked.write_text("changed\n", encoding="utf-8")

    with pytest.raises(EnginePreconditionError, match="dirty"):
        _library(repo)


def test_expect_head_mismatch_exit_1(repo: EngineRepo) -> None:
    _init_round(repo)

    result = _materialize_cli(repo, expect_head="b" * 40)

    assert result.exit_code == 1
    assert "expected" in result.output


def test_unresolvable_skill_records_engine_unresolvable_failure_exit_3(
    repo: EngineRepo,
) -> None:
    _init_round(repo)
    write_engine_context(repo.worktree, engine_context_payload(skill="ghost"))

    result = _materialize_cli(repo)

    assert result.exit_code == 3, result.output
    body = json.loads(result.output)
    assert body == {"failed": True, "cause": "skill_not_found", "skill": "ghost"}
    failures = _failures(repo)
    assert [(f["role"], f["reason"]) for f in failures] == [
        (ENGINE_ROLE, "engine_unresolvable")
    ]


def test_default_branch_unresolved_records_failure_exit_3(repo: EngineRepo) -> None:
    _init_round(repo)
    write_engine_context(
        repo.worktree,
        engine_context_payload(skill="repo-review", default_branch="nowhere"),
    )

    result = _materialize_cli(repo)

    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["cause"] == "default_branch_unresolved"


def _tar(members: list[tuple[str, bytes | str]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            if isinstance(payload, str):
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "members",
    [
        [("../evil.txt", b"x")],
        [("link", "../../outside")],
    ],
    ids=["traversal", "escaping-symlink"],
)
def test_tar_member_escape_rejected(
    tmp_path: Path, members: list[tuple[str, bytes | str]]
) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()

    with pytest.raises(MaterializeError) as caught:
        _extract_archive(_tar(members), destination)

    assert caught.value.cause == "materialize_failed"
    assert not (tmp_path / "evil.txt").exists()


def test_absolute_tar_member_is_confined_to_destination(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()

    _extract_archive(_tar([("/abs/evil.txt", b"x")]), destination)

    assert (destination / "abs" / "evil.txt").read_bytes() == b"x"


def test_corrupt_archive_is_materialize_failed(tmp_path: Path) -> None:
    with pytest.raises(MaterializeError) as caught:
        _extract_archive(b"not a tar archive at all" * 50, tmp_path)

    assert caught.value.cause == "materialize_failed"


def test_tar_escape_exits_3_through_cli(
    repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_round(repo)
    real_git = materialize_module._git

    def fake_git(worktree: Path, argv: list[str]) -> bytes:
        if argv[0] == "archive":
            return _tar([("../evil.txt", b"x")])
        return real_git(worktree, argv)

    monkeypatch.setattr(materialize_module, "_git", fake_git)

    result = _materialize_cli(repo)

    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["cause"] == "materialize_failed"


@pytest.mark.parametrize("cap", ["MAX_SKILL_FILES", "MAX_SKILL_BYTES"])
def test_size_caps_exceeded_exit_3(
    repo: EngineRepo, monkeypatch: pytest.MonkeyPatch, cap: str
) -> None:
    _init_round(repo)
    monkeypatch.setattr(materialize_module, cap, 1)

    result = _materialize_cli(repo)

    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["cause"] == "materialize_failed"


@pytest.mark.parametrize("round_id", ["../x", "a/b", "", ".hidden", "x y"])
def test_round_id_pattern_rejected(repo: EngineRepo, round_id: str) -> None:
    with pytest.raises(EnginePreconditionError):
        check_round_id(round_id)
    with pytest.raises(EnginePreconditionError):
        engine_paths(repo.worktree, round_id)


def test_round_id_pattern_rejected_through_cli(repo: EngineRepo) -> None:
    _init_round(repo)

    result = _materialize_cli(repo, round_id="../x")

    assert result.exit_code == 1
    assert "safe path segment" in result.output


def test_materialize_without_workspace_round_exit_1(repo: EngineRepo) -> None:
    write_engine_context(repo.worktree, engine_context_payload(skill="ghost"))

    result = _materialize_cli(repo)

    assert result.exit_code == 1
    assert "no active review workspace" in result.output


def test_idempotent_rematerialize_replaces_dir(repo: EngineRepo) -> None:
    first = _library(repo)
    (first.skill_dir / "stray.txt").write_text("leftover", encoding="utf-8")

    second = _library(repo)

    assert second.skill_dir == first.skill_dir
    assert not (second.skill_dir / "stray.txt").exists()
    assert second.skill_md.read_text() == ENGINE_SKILL_TEXT
