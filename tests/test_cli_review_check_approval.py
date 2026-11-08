"""Tests for ``cw review check-approval`` (#2773), Step 4.0's verdict command.

The CLI adapts ``cw-context.json`` and ``HEAD`` into the shared predicate's
inputs (``cw.review_approval``) and prints exactly one verdict line, exiting 0
for every verdict. Real git repos; the predicate's own matrix lives in
``test_review_approval.py`` and the end-to-end fence matrix in ``test_stage.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review import check_approval as check_mod
from tests._review_approval_helpers import (
    ApprovalRepo,
    approval_task,
    approve_at,
    cw_context,
    head,
    make_approval_repo,
    write_context,
)
from tests.conftest import commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable

_PARK = "REVIEW_APPROVAL_PARK: "
_RECORDED = "REVIEW_APPROVAL_RECORDED"
_NOT_REQUIRED = "REVIEW_APPROVAL_NOT_REQUIRED"


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> ApprovalRepo:
    made = make_approval_repo(make_git_repo_with_origin, "wt-check-approval")
    approve_at(made.worktree, made.approved)
    return made


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, ["review", "check-approval", *args])
    return result.exit_code, result.output.strip()


def _check(worktree: Path, tier: str, *extra: str) -> str:
    code, output = _run("--tier", tier, "--worktree", str(worktree), *extra)
    assert code == 0, output
    assert len(output.splitlines()) == 1, output
    return output


def _assert_park(output: str, token: str) -> None:
    assert output.startswith(f"{_PARK}{token} — "), output


# --- tier branches ----------------------------------------------------------


def test_small_tier_needs_no_context_file(tmp_path: Path) -> None:
    assert _check(tmp_path, "small") == _NOT_REQUIRED


@pytest.mark.parametrize(
    "tier", ["", "Large", "SMALL", "medium", "<resolved scope.tier>"]
)
def test_garbage_tier_is_unresolved(repo: ApprovalRepo, tier: str) -> None:
    _assert_park(_check(repo.worktree, tier), "unresolved_tier")


def test_large_with_valid_approval_is_recorded(repo: ApprovalRepo) -> None:
    assert _check(repo.worktree, "large") == _RECORDED


def test_tier_is_required() -> None:
    code, _ = _run("--worktree", ".")

    assert code == 2


# --- context cases ----------------------------------------------------------


def test_missing_context_is_unreadable(repo: ApprovalRepo) -> None:
    (repo.worktree / ".claude" / "cw-context.json").unlink()

    output = _check(repo.worktree, "large")

    _assert_park(output, "unreadable")
    assert "missing, unreadable or not valid JSON" in output


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", "null"])
def test_invalid_context_is_unreadable(repo: ApprovalRepo, text: str) -> None:
    write_context(repo.worktree, text)

    _assert_park(_check(repo.worktree, "large"), "unreadable")


@pytest.mark.parametrize("schema_version", [13, "14", None, True, 14.0, [14]])
def test_old_or_malformed_schema_is_channel_absent(
    repo: ApprovalRepo, schema_version: object
) -> None:
    task = approval_task(head(repo.worktree))
    write_context(
        repo.worktree,
        cw_context(repo.worktree, task, schema_version=schema_version),
    )

    _assert_park(_check(repo.worktree, "large"), "channel_absent")


def test_no_review_approval_in_context_is_missing(repo: ApprovalRepo) -> None:
    payload = cw_context(repo.worktree, approval_task(head(repo.worktree)))
    payload["queue_metadata"] = {}
    write_context(repo.worktree, payload)

    _assert_park(_check(repo.worktree, "large"), "missing")


def test_unknown_approver_kind_is_missing(repo: ApprovalRepo) -> None:
    payload = cw_context(repo.worktree, approval_task(head(repo.worktree)))
    metadata = payload["queue_metadata"]
    assert isinstance(metadata, dict)
    metadata["review_approval"]["approver_kind"] = "human"
    write_context(repo.worktree, payload)

    _assert_park(_check(repo.worktree, "large"), "missing")


def test_non_object_review_approval_is_missing(repo: ApprovalRepo) -> None:
    payload = cw_context(repo.worktree, approval_task(head(repo.worktree)))
    payload["queue_metadata"] = {"review_approval": "operator"}
    write_context(repo.worktree, payload)

    _assert_park(_check(repo.worktree, "large"), "missing")


def test_unusable_reviewed_sha_is_stale(repo: ApprovalRepo) -> None:
    approve_at(repo.worktree, "not-a-sha")

    _assert_park(_check(repo.worktree, "large"), "stale")


@pytest.mark.parametrize("snapshot", [{}, {"origin_main_branch": ""}, "main", None])
def test_absent_or_empty_default_branch_is_unreadable(
    repo: ApprovalRepo, snapshot: object
) -> None:
    payload = cw_context(repo.worktree, approval_task(head(repo.worktree)))
    payload["world_state_snapshot"] = snapshot
    write_context(repo.worktree, payload)

    output = _check(repo.worktree, "large")

    _assert_park(output, "unreadable")
    assert "origin_main_branch is missing or invalid" in output


@pytest.mark.parametrize("branch", ["bad..name", "has space", "a:b"])
def test_invalid_ref_name_default_branch_is_unreadable(
    repo: ApprovalRepo, branch: str
) -> None:
    payload = cw_context(repo.worktree, approval_task(head(repo.worktree)))
    payload["world_state_snapshot"] = {"origin_main_branch": branch}
    write_context(repo.worktree, payload)

    output = _check(repo.worktree, "large")

    _assert_park(output, "unreadable")
    assert "origin_main_branch is missing or invalid" in output


def test_check_ref_format_exec_failure_reads_as_invalid_branch(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = check_mod.run_git

    def _boom(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if tuple(argv[:1]) == ("check-ref-format",):
            raise subprocess.TimeoutExpired(["git"], 1)
        return real(argv, **kwargs)

    monkeypatch.setattr(check_mod, "run_git", _boom)

    _assert_park(_check(repo.worktree, "large"), "unreadable")


def test_unreadable_head_is_unreadable(tmp_path: Path) -> None:
    # A repository with no commit has no HEAD commit to resolve.
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    approve_at(empty, "a" * 40)

    output = _check(empty, "large")

    _assert_park(output, "unreadable")
    assert "HEAD is not readable" in output


# --- worktree and context roots ---------------------------------------------


def test_worktree_inside_a_subdirectory_uses_the_repository_root(
    repo: ApprovalRepo,
) -> None:
    sub = repo.worktree / "nested" / "dir"
    sub.mkdir(parents=True)

    assert _check(sub, "large") == _RECORDED


def test_worktree_not_in_a_repository_falls_back_to_that_directory(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    write_context(plain, cw_context(plain, approval_task("a" * 40)))

    output = _check(plain, "large")

    # The directory itself was the guard root: the context was found there and
    # the missing repository surfaces as an unreadable HEAD, not a missing file.
    _assert_park(output, "unreadable")
    assert "HEAD is not readable" in output


def test_worktree_path_in_context_overrides_the_root(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    # The context found from --worktree redirects to ``repo.worktree``'s own
    # copy of the context: running from `other` finds nothing at `other`.
    approve_at(repo.worktree, head(repo.worktree))
    write_context(other, cw_context(repo.worktree, approval_task(head(repo.worktree))))

    assert _check(other, "large") == _RECORDED


def test_worktree_path_override_rereads_context_from_the_override_root(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    # `other` names `repo.worktree` as its worktree; the approval in `other`
    # is for a sha the override root does not have, the one in the override
    # root is valid: the override root's copy wins.
    write_context(other, cw_context(repo.worktree, approval_task("0" * 40)))
    approve_at(repo.worktree, head(repo.worktree))

    assert _check(other, "large") == _RECORDED


@pytest.mark.parametrize("worktree_path", ["", 5, None, ["x"]])
def test_unusable_worktree_path_is_ignored(
    repo: ApprovalRepo, worktree_path: object
) -> None:
    task = approval_task(head(repo.worktree))
    write_context(
        repo.worktree, cw_context(repo.worktree, task, worktree_path=worktree_path)
    )

    assert _check(repo.worktree, "large") == _RECORDED


def test_explicit_context_path_is_read(repo: ApprovalRepo, tmp_path: Path) -> None:
    ctx = tmp_path / "ctx.json"
    ctx.write_text(
        json.dumps(
            cw_context(
                repo.worktree,
                approval_task(head(repo.worktree)),
                worktree_path="",
            )
        ),
        encoding="utf-8",
    )
    (repo.worktree / ".claude" / "cw-context.json").unlink()

    assert _check(repo.worktree, "large", "--context", str(ctx)) == _RECORDED


def test_default_worktree_is_the_current_directory(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo.worktree)

    code, output = _run("--tier", "large")

    assert code == 0
    assert output == _RECORDED


# --- approval validity through the CLI --------------------------------------


def test_new_commit_after_approval_is_stale(repo: ApprovalRepo) -> None:
    commit_tracked_file(repo.worktree, "later.py")

    _assert_park(_check(repo.worktree, "large"), "stale")


def test_exit_is_zero_for_a_park(repo: ApprovalRepo) -> None:
    approve_at(repo.worktree, "0" * 40)

    code, output = _run("--tier", "large", "--worktree", str(repo.worktree))

    assert code == 0
    assert output.startswith(_PARK)
