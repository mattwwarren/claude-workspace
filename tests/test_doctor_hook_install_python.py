"""Tests for the ``hook-install-python/<client>`` doctor check (#3076).

A hook that ``pre-commit install`` wrote from inside a dispatch worker records
``INSTALL_PYTHON=<worktree>/.venv/bin/python``. The hooks directory is shared
by every worktree of the repository, so every worktree's git then runs that
worker's interpreter, and the hook breaks once the worktree is cleaned up. The
check warns (advisory, ``ok=True, warn=True``) when any shared hook's
interpreter lies inside one of the client's dispatch-worktree bases, as
:func:`cw.worktree.effective_worktree_bases` defines them. The check has a
second reason (#3077): the interpreter is missing or not an executable file.
A ``pre-commit`` hook whose missing interpreter ``commit-hook/<client>`` already
reports as stale is not repeated here; every other case is reported here.
Hooks are written by hand through ``tests._commit_hook_helpers``; no test runs
``pre-commit``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw._git import git_hooks_dir
from cw.doctor import _deps, format_report, run_doctor
from cw.doctor.commit_hook import _check_commit_hook, _check_hook_install_python
from cw.worktree import effective_worktree_bases
from tests._commit_hook_helpers import (
    hook_script,
    install_hook_in,
    make_hook_client,
    write_commit_hook_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_NAME = "hook-install-python/acme"
_WORKER_PYTHON = Path("dev-1") / ".venv" / "bin" / "python"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    """A real repo whose config declares a commit-stage hook."""
    path = make_git_repo("acme")
    write_commit_hook_config(path)
    return path


def _install(repo: Path, install_python: str, *, name: str = "pre-commit") -> Path:
    return install_hook_in(git_hooks_dir(repo), hook_script(install_python), name=name)


class TestFlagged:
    @pytest.mark.parametrize("idx", [0, 1])
    def test_interpreter_inside_a_worktree_base_is_flagged(
        self, repo: Path, idx: int
    ) -> None:
        client = make_hook_client(repo)
        base = sorted(effective_worktree_bases(client))[idx]
        python = str(base / _WORKER_PYTHON)
        script = _install(repo, python)

        results = _check_hook_install_python({"acme": client})

        assert [(r.name, r.ok, r.warn) for r in results] == [(_NAME, True, True)]
        detail = results[0].detail
        assert str(script) in detail
        assert python in detail
        assert str(base) in detail
        assert f"rm {script}" in detail
        assert "pre-commit install --hook-type pre-commit" in detail

    def test_explicit_base_replaces_the_default_bases(
        self, repo: Path, tmp_path: Path
    ) -> None:
        explicit = tmp_path / "wt"
        client = make_hook_client(repo, worktree_base=explicit)
        assert effective_worktree_bases(client) == frozenset({explicit})
        sibling = repo.parent / ".worktrees" / repo.name
        flagged = _install(repo, str(explicit / _WORKER_PYTHON))
        live = sibling / _WORKER_PYTHON
        live.parent.mkdir(parents=True, exist_ok=True)
        live.symlink_to(sys.executable)
        unflagged = _install(repo, str(live), name="pre-push")

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(flagged) in results[0].detail
        assert str(unflagged) not in results[0].detail

    def test_pre_push_hook_is_flagged_without_commit_hooks_declared(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        path = make_git_repo("acme")
        client = make_hook_client(path)
        base = next(iter(effective_worktree_bases(client)))
        script = _install(path, str(base / _WORKER_PYTHON), name="pre-push")

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(script) in results[0].detail

    def test_venv_symlink_to_system_python_is_still_flagged(self, repo: Path) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        python = base / _WORKER_PYTHON
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        _install(repo, str(python))

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(python) in results[0].detail

    def test_several_flagged_hooks_give_one_result(self, repo: Path) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        first = _install(repo, str(base / _WORKER_PYTHON))
        second = _install(repo, str(base / _WORKER_PYTHON), name="pre-push")

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(first) in results[0].detail
        assert str(second) in results[0].detail

    @pytest.mark.parametrize("name", ["pre-push", "post-commit"])
    def test_non_pre_commit_missing_interpreter_is_flagged(
        self, repo: Path, tmp_path: Path, name: str
    ) -> None:
        python = str(tmp_path / "gone" / "bin" / "python")
        script = _install(repo, python, name=name)

        results = _check_hook_install_python({"acme": make_hook_client(repo)})

        assert [(r.name, r.ok, r.warn) for r in results] == [(_NAME, True, True)]
        detail = results[0].detail
        assert str(script) in detail
        assert python in detail
        assert "not an executable file" in detail
        assert f"rm {script}" in detail
        assert "pre-commit install --hook-type pre-commit" in detail
        assert "dispatch worktree base" not in detail

    def test_non_executable_interpreter_is_flagged(
        self, repo: Path, tmp_path: Path
    ) -> None:
        python = tmp_path / "not-executable"
        python.write_text("", encoding="utf-8")
        python.chmod(0o644)
        script = _install(repo, str(python), name="commit-msg")

        results = _check_hook_install_python({"acme": make_hook_client(repo)})

        assert [r.name for r in results] == [_NAME]
        assert str(script) in results[0].detail
        assert "not an executable file" in results[0].detail

    def test_worktree_and_missing_interpreter_reports_once(self, repo: Path) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        python = str(base / _WORKER_PYTHON)
        _install(repo, python, name="pre-push")

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        detail = results[0].detail
        assert detail.count(f"records INSTALL_PYTHON={python}") == 1
        assert "dispatch worktree base" in detail
        assert "not an executable file" in detail

    def test_pre_commit_in_base_with_commit_config_reports_base_only(
        self, repo: Path
    ) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        python = str(base / _WORKER_PYTHON)
        _install(repo, python)

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(base) in results[0].detail
        assert "not an executable file" not in results[0].detail
        commit = _check_commit_hook({"acme": client})
        assert [(r.name, r.warn) for r in commit] == [("commit-hook/acme", True)]
        assert python in commit[0].detail

    def test_non_executable_pre_commit_with_missing_interpreter_is_still_flagged(
        self, repo: Path
    ) -> None:
        python = str(Path("/nonexistent-cw-3077") / "bin" / "python")
        script = install_hook_in(
            git_hooks_dir(repo), hook_script(python), executable=False
        )
        client = make_hook_client(repo)

        results = _check_hook_install_python({"acme": client})

        assert [r.name for r in results] == [_NAME]
        assert str(script) in results[0].detail
        assert "not an executable file" in results[0].detail
        commit = _check_commit_hook({"acme": client})
        assert [(r.name, r.warn) for r in commit] == [("commit-hook/acme", True)]
        assert "is not executable" in commit[0].detail
        assert "INSTALL_PYTHON" not in commit[0].detail

    @pytest.mark.parametrize(
        "config",
        ["no-config", "manual-only-config", "malformed-config", "unreadable-config"],
    )
    def test_pre_commit_missing_interpreter_flagged_when_commit_check_does_not_own_it(
        self, make_git_repo: Callable[..., Path], tmp_path: Path, config: str
    ) -> None:
        path = make_git_repo("acme")
        if config == "manual-only-config":
            write_commit_hook_config(path, hook_stages=["manual"])
        elif config == "malformed-config":
            write_commit_hook_config(path, text="repos: [")
        elif config == "unreadable-config":
            (path / ".pre-commit-config.yaml").mkdir()
        client = make_hook_client(path)
        python = str(tmp_path / "gone" / "bin" / "python")
        script = _install(path, python)

        results = _check_hook_install_python({"acme": client})

        assert [(r.name, r.ok, r.warn) for r in results] == [(_NAME, True, True)]
        assert str(script) in results[0].detail
        assert "not an executable file" in results[0].detail
        assert _check_commit_hook({"acme": client}) == []


class TestNotFlagged:
    def test_pre_commit_missing_interpreter_left_to_commit_hook(
        self, repo: Path, tmp_path: Path
    ) -> None:
        python = str(tmp_path / "gone" / "python")
        _install(repo, python)
        client = make_hook_client(repo)

        assert _check_hook_install_python({"acme": client}) == []
        commit = _check_commit_hook({"acme": client})
        assert [(r.name, r.warn) for r in commit] == [("commit-hook/acme", True)]
        assert f"INSTALL_PYTHON={python}" in commit[0].detail

    def test_interpreter_outside_every_base(self, repo: Path) -> None:
        _install(repo, sys.executable)
        _install(repo, sys.executable, name="pre-push")

        assert _check_hook_install_python({"acme": make_hook_client(repo)}) == []

    @pytest.mark.parametrize(
        "script",
        ["#!/bin/sh\nINSTALL_PYTHON=python3\n", "#!/bin/sh\nINSTALL_PYTHON=\n"],
        ids=["relative", "empty"],
    )
    def test_relative_or_empty_install_python(self, repo: Path, script: str) -> None:
        install_hook_in(git_hooks_dir(repo), script)

        assert _check_hook_install_python({"acme": make_hook_client(repo)}) == []

    def test_no_hooks(self, repo: Path) -> None:
        assert _check_hook_install_python({"acme": make_hook_client(repo)}) == []

    def test_empty_clients(self) -> None:
        assert _check_hook_install_python({}) == []

    def test_sample_files_and_directories_are_skipped(self, repo: Path) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        _install(repo, str(base / _WORKER_PYTHON), name="pre-commit.sample")
        (git_hooks_dir(repo) / "pre-push").mkdir()

        assert _check_hook_install_python({"acme": client}) == []

    def test_missing_hooks_dir(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hooks_dir(*_args: object, **_kwargs: object) -> Path:
            return tmp_path / "no-such-hooks"

        monkeypatch.setattr("cw.doctor.commit_hook.git_hooks_dir", _hooks_dir)

        assert _check_hook_install_python({"acme": make_hook_client(repo)}) == []

    def test_unreadable_hook(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        _install(repo, str(base / _WORKER_PYTHON))
        real_read_text = Path.read_text

        def _read_text(
            self: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if self.name == "pre-commit":
                raise PermissionError(self.name)
            return real_read_text(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", _read_text)

        assert _check_hook_install_python({"acme": client}) == []

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.CalledProcessError(128, ["git"]),
            subprocess.TimeoutExpired(["git"], 1.0),
            OSError("boom"),
        ],
        ids=["called-process-error", "timeout", "os-error"],
    )
    def test_git_hooks_dir_failure(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> Path:
            raise exc

        monkeypatch.setattr("cw.doctor.commit_hook.git_hooks_dir", _raise)
        client = make_hook_client(repo)

        assert _check_hook_install_python({"acme": client}) == []
        commit = _check_commit_hook({"acme": client})
        assert [(r.name, r.warn) for r in commit] == [("commit-hook/acme", True)]
        assert "could not resolve the git hooks directory" in commit[0].detail


class TestRunDoctorIntegration:
    def test_run_doctor_reports_the_check_without_failing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make_hook_client(repo)
        base = next(iter(effective_worktree_bases(client)))
        _install(repo, str(base / _WORKER_PYTHON), name="pre-push")
        monkeypatch.setattr(_deps, "load_clients", lambda: {"acme": client})

        report = run_doctor()

        check = next(c for c in report.checks if c.name == _NAME)
        assert check.ok is True
        assert check.warn is True
        assert f"[WARN] {_NAME}" in format_report(report)
        baseline = [c for c in report.checks if c.name != _NAME]
        assert all(c.ok for c in report.checks) == all(c.ok for c in baseline)

    def test_run_doctor_reports_a_missing_interpreter_without_failing(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make_hook_client(repo)
        _install(repo, str(tmp_path / "gone" / "bin" / "python"), name="pre-push")
        monkeypatch.setattr(_deps, "load_clients", lambda: {"acme": client})

        report = run_doctor()

        check = next(c for c in report.checks if c.name == _NAME)
        assert check.ok is True
        assert check.warn is True
        assert f"[WARN] {_NAME}" in format_report(report)
        baseline = [c for c in report.checks if c.name != _NAME]
        assert all(c.ok for c in report.checks) == all(c.ok for c in baseline)
