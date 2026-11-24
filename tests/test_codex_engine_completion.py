"""The codex engine round's completion check runs and fails closed (#3885).

Every case drives ``run_engine_pass`` over a real git repo whose default branch
carries a checker (``tests/fixtures/review_engine_checker``) or the stub engine
(no key). Blocked rounds return a verdict-less ``agent_block`` and never record
an event. Shared helpers and the ``engine_repo``, ``checker_tmpdir`` and
``record_event_spy`` fixtures live in ``tests/_shared/engine_round_helpers.py``.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from cw.codex_review import _engine_completion
from cw.codex_review._engine_completion import (
    CompletionResult,
    _checker_env,
    _checker_in_tree,
    _completion_failure,
    _read_result,
    checker_problem,
)
from cw.review_convergence import resolve_default_branch_ref
from cw.review_engine.materialize import MaterializeError
from cw.review_engine.skill import resolve_skill
from tests import _git_sandbox
from tests._shared.engine_round_helpers import (
    ROUND_ID,
    SKILL,
    aggregator_stdins,
    assert_group_dead,
    blocked_details,
    engine_runner,
    make_checker_repo,
    observed_exists,
    read_observed,
    round_dir,
    run_pass,
    tamper_roles,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests._review_engine_helpers import EngineRepo

pytestmark = pytest.mark.platform_sensitive

_NO_RETRY = False
_RETRY = True

_PREAMBLE = """#!/usr/bin/env python3
import json, os, subprocess, sys
def arg(name):
    return sys.argv[sys.argv.index(name) + 1]
EV = arg("--evidence-dir")
WT = arg("--worktree")
HEAD = arg("--head-sha")
RID = arg("--round-id")
ROLES = arg("--roles").split(",")
"""


def _checker(body: str) -> str:
    return _PREAMBLE + body


def _result_script(
    status: str = "complete",
    *,
    head: str = "HEAD",
    rid: str = "RID",
    roles: str = "ROLES",
    extra: str = "",
) -> str:
    """A checker that runs *extra* then writes a result with the given fields."""
    return _checker(
        extra
        + f"""
with open(os.path.join(EV, "result.json"), "w") as fh:
    json.dump({{"schema": 1, "head_sha": {head}, "round_id": {rid},
               "status": {status!r}, "roles_covered": {roles},
               "detail": "x"}}, fh)
"""
    )


def _dirty(wt: Path) -> None:
    (wt / "dirty.txt").write_text("x", encoding="utf-8")


def _git(wt: Path, *args: str) -> None:
    _git_sandbox.run(*args, cwd=wt)


def _commit_in(wt: Path, path: str) -> None:
    (wt / path).write_text("m", encoding="utf-8")
    _git(wt, "add", path)
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "move")


# ---- happy path and unchanged without the key ---------------------------


def test_happy_path_passes_and_retains_result(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    checker_tmpdir: tuple[Path, list[str]],
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)
    runner = engine_runner()
    outcome = run_pass(repo, runner)
    assert outcome.result.status != "blocked"
    assert outcome.verdict is not None
    assert len(aggregator_stdins(runner)) == 1
    assert (round_dir(repo) / "completion" / "result.json").exists()


def test_observed_argument_order_cwd_and_tmpdir(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    checker_tmpdir: tuple[Path, list[str]],
) -> None:
    worker, recorded = checker_tmpdir
    repo = make_checker_repo(make_git_repo, tmp_path)
    outcome = run_pass(repo, engine_runner())
    assert outcome.result.status != "blocked"
    observed = read_observed(repo)
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[:8] == [
        "--head-sha",
        repo.head,
        "--round-id",
        ROUND_ID,
        "--worktree",
        str(repo.worktree),
        "--evidence-dir",
        str(round_dir(repo) / "completion"),
    ]
    assert argv[8:] == ["--roles", "security,quality"]
    cwd = str(observed["cwd"])
    assert cwd not in (str(repo.worktree), str(round_dir(repo) / SKILL))
    assert cwd.startswith(str(worker))
    assert observed["tmpdir"] == tempfile.gettempdir()
    assert recorded, "checker scratch dir was never created"
    assert not any(worker.iterdir()), "checker scratch dir not cleaned up"


def test_engine_without_key_runs_no_checker(
    engine_repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    real = _engine_completion.engine_precheck

    def counting(*args: Any) -> Any:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(_engine_completion, "engine_precheck", counting)
    outcome = run_pass(engine_repo, engine_runner())
    assert outcome.result.status != "blocked"
    assert not (round_dir(engine_repo) / "completion").exists()
    assert calls == []


# ---- environment --------------------------------------------------------


def test_checker_env_has_only_path_lang_and_tmpdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("CW_CANARY_SECRET", "x")
    monkeypatch.setenv("AWS_CANARY", "x")
    monkeypatch.setenv("GIT_CANARY", "x")
    env = _checker_env()
    assert set(env) == {"PATH", "LANG", "TMPDIR"}
    assert env["TMPDIR"] == tempfile.gettempdir()


def test_checker_env_omits_unset_path_and_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    env = _checker_env()
    assert "PATH" not in env
    assert "LANG" not in env
    assert "TMPDIR" in env


def test_canary_environment_is_not_inherited(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CW_CANARY_SECRET", "x")
    repo = make_checker_repo(make_git_repo, tmp_path)
    run_pass(repo, engine_runner())
    keys = read_observed(repo)["env_keys"]
    assert isinstance(keys, list)
    # The test sandbox (tests/_git_sandbox) injects GIT_CEILING_DIRECTORIES into
    # every child under pytest; production's explicit env never carries it.
    assert set(keys) - {"GIT_CEILING_DIRECTORIES"} <= {"PATH", "TMPDIR", "LANG"}
    assert "CW_CANARY_SECRET" not in keys


# ---- result contract ----------------------------------------------------


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (_result_script(status="incomplete"), "coverage=PARTIAL"),
        (_result_script(roles="['security']"), "coverage=PARTIAL"),
    ],
    ids=["status-incomplete", "missing-role"],
)
def test_incomplete_coverage_blocks_retryable(
    make_git_repo: Callable[..., Path], tmp_path: Path, body: str, detail: str
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    text = blocked_details(run_pass(repo, engine_runner()), retry=_RETRY)
    assert "engine_coverage_incomplete" in text
    assert detail in text


def test_extra_roles_are_accepted(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    body = _result_script(roles="ROLES + ['extra']")
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    assert run_pass(repo, engine_runner()).result.status != "blocked"


@pytest.mark.parametrize(
    ("body", "text"),
    [
        (_result_script(head="'0' * 40"), "engine_head_mismatch: summary="),
        (_result_script(rid="'stale'"), "summary=stale-round"),
    ],
    ids=["stale-head", "stale-round"],
)
def test_stale_binding_blocks_retryable(
    make_git_repo: Callable[..., Path], tmp_path: Path, body: str, text: str
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_RETRY)
    assert text in details


def test_status_failed_blocks_non_retryable(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    body = _result_script(status="failed")
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: status_failed"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_checker("sys.exit(3)\n"), "checker_exit_nonzero"),
        (_checker("sys.exit(0)\n"), "result_missing"),
        (
            _checker("open(os.path.join(EV, 'result.json'), 'w').write('{}')\n"),
            "result_invalid",
        ),
        (
            _checker("os.symlink('/etc/hostname', os.path.join(EV, 'result.json'))\n"),
            "result_invalid",
        ),
    ],
    ids=["exit-nonzero", "result-missing", "result-invalid", "result-symlink"],
)
def test_checker_failure_rows_block_non_retryable(
    make_git_repo: Callable[..., Path], tmp_path: Path, body: str, reason: str
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == f"engine_completion_check_failed: {reason}"


def test_timeout_kills_group_and_blocks(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.mktemp("timeout")
    pid_file = base / "child.pid"
    body = _checker(
        f"""
child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
open({str(pid_file)!r}, "w").write(str(child.pid))
import time
time.sleep(60)
"""
    )
    repo = make_checker_repo(make_git_repo, base, body, timeout_seconds=1)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: checker_timeout"
    assert_group_dead(pid_file)


def test_redirected_background_child_is_killed_on_clean_exit(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.mktemp("bgchild")
    pid_file = base / "bg.pid"
    extra = (
        'bg = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL,'
        " stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(bg.pid))\n"
    )
    repo = make_checker_repo(make_git_repo, base, _result_script(extra=extra))
    outcome = run_pass(repo, engine_runner())
    assert outcome.result.status != "blocked"
    assert_group_dead(pid_file)


def test_checker_not_executable_blocks(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path, _result_script(), mode=0o644)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: checker_not_executable"


def test_output_is_never_echoed(
    make_git_repo: Callable[..., Path], tmp_path: Path
) -> None:
    body = _checker("print('SECRET-OUTPUT-xyz')\nsys.exit(4)\n")
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert "SECRET-OUTPUT" not in details


# ---- tree attribution (R17, R4) -----------------------------------------


def test_role_dirtied_tree_blocks_before_checker(
    engine_repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    tamper_roles(
        monkeypatch,
        engine_repo.worktree,
        _dirty,
    )
    details = blocked_details(run_pass(engine_repo, engine_runner()), retry=_NO_RETRY)
    assert details.startswith("engine_worktree_mutated")
    assert not observed_exists(engine_repo)


def test_role_moved_head_blocks_retryable_before_checker(
    make_git_repo: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)
    tamper_roles(monkeypatch, repo.worktree, lambda wt: _commit_in(wt, "moved.txt"))
    details = blocked_details(run_pass(repo, engine_runner()), retry=_RETRY)
    assert details.startswith("engine_head_mismatch")
    assert not observed_exists(repo)


def test_checker_dirtying_tree_is_attributed_to_checker(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    body = _result_script(
        extra="open(os.path.join(WT, 'checker-wrote.txt'), 'w').write('x')\n"
    )
    repo = make_checker_repo(make_git_repo, tmp_path, body)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: worktree_mutated"


def test_checker_moving_head_is_attributed_to_checker(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    extra = (
        "subprocess.run(['git', '-C', WT, '-c', 'user.email=a@b', '-c',"
        " 'user.name=a', 'commit', '--allow-empty', '-m', 'c'], check=True,"
        " capture_output=True)\n"
    )
    repo = make_checker_repo(make_git_repo, tmp_path, _result_script(extra=extra))
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: head_moved"


@pytest.mark.parametrize("call", [0, 1], ids=["pre-run", "post-run"])
def test_precheck_git_error_is_unverifiable(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    call: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)
    real = _engine_completion.engine_precheck
    seen: list[int] = []

    def flaky(worktree: Path, head: str) -> Any:
        seen.append(1)
        if len(seen) - 1 == call:
            raise subprocess.CalledProcessError(1, "precheck")
        return real(worktree, head)

    monkeypatch.setattr(_engine_completion, "engine_precheck", flaky)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: check_unverifiable"
    if call == 0:
        assert not observed_exists(repo)


def test_planted_ancestor_symlink_is_refused(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path_factory.mktemp("symlink")
    repo = make_checker_repo(make_git_repo, base)
    outside = base / "outside"
    (outside / "completion").mkdir(parents=True)
    sentinel = outside / "completion" / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def swap(_wt: Path) -> None:
        rd = round_dir(repo)
        rd.rename(rd.with_name(rd.name + "-moved"))
        rd.symlink_to(outside)

    tamper_roles(monkeypatch, repo.worktree, swap)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: check_unverifiable"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not observed_exists(repo)


def test_overwritten_materialized_copy_is_not_run(
    make_git_repo: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path, _result_script(status="failed"))

    def replace_copy(_wt: Path) -> None:
        copy = round_dir(repo) / "skill" / "bin" / "check-completion"
        copy.write_text(_result_script(), encoding="utf-8")

    tamper_roles(monkeypatch, repo.worktree, replace_copy)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: status_failed"


# ---- launch and extraction ----------------------------------------------


def test_launch_oserror_blocks(
    make_git_repo: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        msg = "no exec"
        raise OSError(msg)

    monkeypatch.setattr(_engine_completion, "run_declared_command", boom)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: checker_launch_failed"


def test_extraction_failure_blocks(
    make_git_repo: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)

    def fail(data: bytes, dest: Path) -> None:
        error = MaterializeError("materialize_failed", "boom")
        raise error

    monkeypatch.setattr(_engine_completion, "_extract_archive", fail)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == "engine_completion_check_failed: extract_failed"


# ---- units --------------------------------------------------------------


def test_missing_checker_in_tree_is_refused(tmp_path: Path) -> None:
    assert _checker_in_tree(tmp_path, "bin/missing") == "checker_missing"


def test_checker_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(target)
    assert _checker_in_tree(tmp_path, "link") == "checker_missing"


def test_completion_result_forbids_extra_keys() -> None:
    raw = (
        b'{"schema": 1, "head_sha": "a", "round_id": "r", "status": "complete",'
        b' "roles_covered": [], "detail": "d", "extra": 1}'
    )
    with pytest.raises(ValidationError):
        CompletionResult.model_validate_json(raw)


def test_completion_result_schema_must_be_one() -> None:
    raw = (
        b'{"schema": 2, "head_sha": "a", "round_id": "r", "status": "complete",'
        b' "roles_covered": [], "detail": "d"}'
    )
    with pytest.raises(ValidationError):
        CompletionResult.model_validate_json(raw)


def test_unknown_completion_reason_is_rejected() -> None:
    message = "unknown completion failure reason"
    with pytest.raises(ValueError, match=message):
        _completion_failure("not_a_reason")


def test_read_result_refuses_oversized(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_bytes(b" " * 70_000)
    failure = _read_result(tmp_path)
    assert not isinstance(failure, CompletionResult)
    assert failure.detail == "engine_completion_check_failed: result_invalid"


def test_checker_problem_reports_missing_file(engine_repo: EngineRepo) -> None:
    ref = resolve_default_branch_ref(engine_repo.worktree, "main")
    assert ref is not None
    resolved = resolve_skill(engine_repo.worktree, ref.commit, SKILL)
    assert (
        checker_problem(engine_repo.worktree, ref.commit, resolved, "bin/none")
        == "checker_missing"
    )


# ---- operating-system failures fail closed ------------------------------

_UNVERIFIABLE = "engine_completion_check_failed: check_unverifiable"


def _deny_scratch_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make creating the checker's scratch directory fail, leaving other temp use."""
    real = tempfile.mkdtemp

    def deny(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("prefix") == "cw-engine-check-":
            msg = "no space left on device"
            raise OSError(msg)
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", deny)


def test_scratch_dir_oserror_blocks(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)
    _deny_scratch_dir(monkeypatch)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == _UNVERIFIABLE
    assert not observed_exists(repo)


def test_evidence_dir_mkdir_oserror_blocks_and_removes_scratch(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    checker_tmpdir: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, recorded = checker_tmpdir
    repo = make_checker_repo(make_git_repo, tmp_path)
    real = Path.mkdir

    def deny(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "completion":
            msg = "read-only file system"
            raise OSError(msg)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == _UNVERIFIABLE
    assert not observed_exists(repo)
    assert recorded, "the scratch dir was never created"
    assert not any(worker.iterdir()), "checker scratch dir not cleaned up"


def test_undeletable_planted_evidence_blocks(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)

    def plant(_wt: Path) -> None:
        evidence = round_dir(repo) / "completion"
        evidence.mkdir()
        (evidence / "planted.json").write_text("{}", encoding="utf-8")

    tamper_roles(monkeypatch, repo.worktree, plant)
    real = _engine_completion.shutil.rmtree

    def deny(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == "completion":
            msg = "operation not permitted"
            raise OSError(msg)
        real(path, *args, **kwargs)

    monkeypatch.setattr(_engine_completion.shutil, "rmtree", deny)
    details = blocked_details(run_pass(repo, engine_runner()), retry=_NO_RETRY)
    assert details == _UNVERIFIABLE
    assert not observed_exists(repo)


def test_checker_problem_scratch_dir_oserror_is_unverifiable(
    engine_repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = resolve_default_branch_ref(engine_repo.worktree, "main")
    assert ref is not None
    resolved = resolve_skill(engine_repo.worktree, ref.commit, SKILL)
    _deny_scratch_dir(monkeypatch)
    assert (
        checker_problem(engine_repo.worktree, ref.commit, resolved, "bin/none")
        == "check_unverifiable"
    )


@pytest.mark.parametrize("kind", ["populated-dir", "symlink"])
def test_planted_evidence_is_replaced_by_an_empty_dir(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    repo = make_checker_repo(make_git_repo, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def plant(_wt: Path) -> None:
        evidence = round_dir(repo) / "completion"
        if kind == "symlink":
            evidence.symlink_to(elsewhere)
        else:
            evidence.mkdir()
            (evidence / "planted.json").write_text("{}", encoding="utf-8")

    tamper_roles(monkeypatch, repo.worktree, plant)
    outcome = run_pass(repo, engine_runner())
    assert outcome.result.status != "blocked"
    assert not (elsewhere / "result.json").exists()
    assert not (round_dir(repo) / "completion" / "planted.json").exists()


def test_checker_under_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    from cw.codex_review._engine_completion import _checker_in_tree

    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "check").write_text("#!/bin/sh\n", encoding="utf-8")
    (outside / "check").chmod(0o755)
    dest.mkdir()
    (dest / "bin").symlink_to(outside)
    assert _checker_in_tree(dest, "bin/check") == "checker_missing"


def test_unreadable_result_is_result_invalid(tmp_path: Path) -> None:
    from cw.codex_review._engine_completion import _read_result

    result = tmp_path / "result.json"
    result.write_text("{}", encoding="utf-8")
    result.chmod(0o000)
    try:
        failure = _read_result(tmp_path)
    finally:
        result.chmod(0o600)
    assert not isinstance(failure, CompletionResult)
    assert failure.detail == "engine_completion_check_failed: result_invalid"
