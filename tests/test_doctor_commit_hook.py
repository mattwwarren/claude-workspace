"""Tests for cw.doctor.commit_hook (#2973).

The ``commit-hook/<client>`` check warns when a client's repo declares
commit-stage hooks in ``.pre-commit-config.yaml`` but ``git commit`` would run
nothing: no executable hook, a ``core.hooksPath`` that bypasses the install, a
non-executable hook, or a hook whose recorded ``INSTALL_PYTHON`` is gone.
Hooks are written by hand through ``tests._commit_hook_helpers``; no test runs
``pre-commit`` itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.doctor import _deps, format_report, run_doctor
from cw.doctor.commit_hook import (
    CommitHookState,
    _check_commit_hook,
    commit_hook_state,
    declares_commit_stage_hook,
)
from tests._codex_review_helpers import _install_pre_commit_hook
from tests._commit_hook_helpers import (
    commit_hook_config,
    hook_script,
    install_hook_in,
    install_templated_hook,
    make_hook_client,
    write_commit_hook_config,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_TIMEOUT = 2.0
_DEFAULT_TIMEOUT = 10.0


class TestDeclaresCommitStageHook:
    @pytest.mark.parametrize(
        ("hook_stages", "default_stages", "expected"),
        [
            (None, None, True),
            (None, ["pre-push"], False),
            (["pre-commit"], ["pre-push"], True),
            (["commit"], None, True),
            (None, ["commit"], True),
            (["push"], None, False),
            (["manual"], None, False),
            (["manual", "pre-push"], None, False),
            (["pre-commit", "pre-push"], None, True),
            ([], ["pre-push"], False),
            ([], ["pre-commit"], True),
            (None, [], False),
            (["merge-commit"], None, False),
        ],
    )
    def test_stage_semantics(
        self,
        hook_stages: list[str] | None,
        default_stages: list[str] | None,
        expected: bool,
    ) -> None:
        text = commit_hook_config(
            hook_stages=hook_stages, default_stages=default_stages
        )

        assert declares_commit_stage_hook(text) is expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "repos: []\n",
            "repos: [unclosed\n",
            "- just\n- a list\n",
            "repos: not-a-list\n",
            "repos:\n  - repo: local\n    hooks: nope\n",
            "repos:\n  - repo: local\n    hooks:\n      - just-a-string\n",
            "repos:\n  - not-a-dict\n",
            "repos:\n  - repo: local\n    hooks:\n      - id: d\n"
            "        stages: [1, null]\n",
            "default_stages: not-a-list\nrepos: []\n",
        ],
    )
    def test_malformed_or_empty_input_is_false_without_raising(self, text: str) -> None:
        assert declares_commit_stage_hook(text) is False

    def test_non_string_stage_entries_are_ignored(self) -> None:
        text = (
            "repos:\n  - repo: local\n    hooks:\n      - id: d\n"
            "        stages: [1, pre-commit]\n"
        )

        assert declares_commit_stage_hook(text) is True

    def test_non_list_default_stages_falls_back_to_all_stages(self) -> None:
        text = (
            "default_stages: not-a-list\nrepos:\n  - repo: local\n    hooks:\n"
            "      - id: d\n"
        )

        assert declares_commit_stage_hook(text) is True

    def test_this_repos_config_shape_is_true_and_pre_push_only_variant_false(
        self,
    ) -> None:
        shape = (
            "default_install_hook_types: [pre-commit, pre-push]\n"
            "repos:\n  - repo: https://example.invalid/r\n    rev: v1\n    hooks:\n"
            "      - id: ruff\n      - id: ruff-format\n"
        )
        pre_push_only = shape.replace(
            "id: ruff\n", "id: ruff\n        stages: [pre-push]\n"
        )
        pre_push_only = pre_push_only.replace(
            "id: ruff-format\n", "id: ruff-format\n        stages: [pre-push]\n"
        )

        assert declares_commit_stage_hook(shape) is True
        assert declares_commit_stage_hook(pre_push_only) is False

    def test_this_repos_config_declares_commit_hooks(self) -> None:
        text = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

        assert declares_commit_stage_hook(text) is True


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    """A real repo whose config declares a commit-stage hook."""
    path = make_git_repo("acme")
    write_commit_hook_config(path)
    return path


def _bad(state: CommitHookState | None) -> CommitHookState:
    assert state is not None
    assert state.ok is False
    return state


class TestCommitHookState:
    def test_no_config_is_none(self, make_git_repo: Callable[..., Path]) -> None:
        assert commit_hook_state(make_git_repo("plain")) is None

    def test_only_pre_push_hooks_is_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        path = make_git_repo("pushy")
        write_commit_hook_config(path, hook_stages=["pre-push"])

        assert commit_hook_state(path) is None

    def test_malformed_config_is_none(self, make_git_repo: Callable[..., Path]) -> None:
        path = make_git_repo("broken")
        write_commit_hook_config(path, text="repos: [unclosed\n")

        assert commit_hook_state(path) is None

    def test_config_that_is_a_directory_is_none(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        path = make_git_repo("dirconfig")
        (path / ".pre-commit-config.yaml").mkdir()

        assert commit_hook_state(path) is None

    def test_no_hook_names_script_and_fix(self, repo: Path) -> None:
        state = _bad(commit_hook_state(repo))

        script = repo / ".git" / "hooks" / "pre-commit"
        assert f"no executable pre-commit hook at {script}" in state.detail
        assert "pre-commit install --hook-type pre-commit" in state.detail
        assert f"in {repo}" in state.detail
        assert "core.hooksPath" not in state.detail
        assert state.transient is False

    def test_sample_only_hooks_dir_is_the_same_finding(self, repo: Path) -> None:
        (repo / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n")

        state = _bad(commit_hook_state(repo))

        assert "no executable pre-commit hook" in state.detail
        assert state.transient is False

    def test_hook_that_is_a_directory_counts_as_missing(self, repo: Path) -> None:
        (repo / ".git" / "hooks" / "pre-commit").mkdir()

        assert "no executable pre-commit hook" in _bad(commit_hook_state(repo)).detail

    def test_executable_hook_with_live_install_python_is_healthy(
        self, repo: Path
    ) -> None:
        install_templated_hook(repo, sys.executable)

        state = commit_hook_state(repo)

        script = repo / ".git" / "hooks" / "pre-commit"
        assert state == CommitHookState(
            ok=True, detail=f"pre-commit hook installed at {script}"
        )

    def test_non_executable_hook(self, repo: Path) -> None:
        _install_pre_commit_hook(repo, hook_script(sys.executable))
        script = repo / ".git" / "hooks" / "pre-commit"
        script.chmod(0o644)

        state = _bad(commit_hook_state(repo))

        assert f"{script} exists but is not executable" in state.detail
        assert f"Fix: chmod +x {script}" in state.detail
        assert "core.hooksPath" not in state.detail
        assert state.transient is False

    def test_non_executable_hook_under_hooks_path_appends_a_note(
        self, repo: Path, tmp_path: Path
    ) -> None:
        hooks = tmp_path / "shared-hooks"
        install_hook_in(hooks, hook_script(sys.executable), executable=False)
        git_in(repo, "config", "core.hooksPath", str(hooks))

        state = _bad(commit_hook_state(repo))

        assert "not executable" in state.detail
        assert "core.hooksPath" in state.detail

    def test_hooks_path_relative_to_hookless_dir_names_the_setting(
        self, repo: Path
    ) -> None:
        git_in(repo, "config", "core.hooksPath", "hk")

        state = _bad(commit_hook_state(repo))

        assert "core.hooksPath is set to 'hk'" in state.detail
        assert "file:" in state.detail
        assert str(repo / "hk") in state.detail
        assert "refuses to install while core.hooksPath is set" in state.detail
        assert f"git -C {repo} config --unset core.hooksPath" in state.detail
        assert "pre-commit install --hook-type pre-commit" in state.detail
        assert state.transient is False

    def test_absolute_hooks_path_with_executable_hook_is_healthy(
        self, repo: Path, tmp_path: Path
    ) -> None:
        hooks = tmp_path / "global-hooks"
        script = install_hook_in(hooks, hook_script(sys.executable))
        git_in(repo, "config", "core.hooksPath", str(hooks))

        state = commit_hook_state(repo)

        assert state == CommitHookState(
            ok=True, detail=f"pre-commit hook installed at {script}"
        )

    def test_relative_hooks_path_with_executable_hook_is_healthy(
        self, repo: Path
    ) -> None:
        install_hook_in(repo / "hk", hook_script(sys.executable))
        git_in(repo, "config", "core.hooksPath", "hk")

        state = commit_hook_state(repo)

        assert state is not None
        assert state.ok is True

    def test_stale_install_python_missing_file(self, repo: Path) -> None:
        gone = "/nonexistent/worktree/.venv/bin/python"
        install_templated_hook(repo, gone)
        script = repo / ".git" / "hooks" / "pre-commit"

        state = _bad(commit_hook_state(repo))

        assert f"the pre-commit hook at {script} records INSTALL_PYTHON={gone}" in (
            state.detail
        )
        assert "not an executable file" in state.detail
        assert "`pre-commit` not found" in state.detail
        assert "unless pre-commit is on PATH" in state.detail
        assert "from a live environment in" in state.detail
        assert f"in {repo}" in state.detail
        assert state.transient is False

    def test_stale_install_python_non_executable_file(
        self, repo: Path, tmp_path: Path
    ) -> None:
        fake = tmp_path / "python-not-exec"
        fake.write_text("")
        fake.chmod(0o644)
        install_templated_hook(repo, str(fake))

        assert "INSTALL_PYTHON" in _bad(commit_hook_state(repo)).detail

    def test_quoted_install_python_with_spaces_that_exists_is_healthy(
        self, repo: Path, tmp_path: Path
    ) -> None:
        spaced = tmp_path / "a dir" / "python"
        spaced.parent.mkdir()
        spaced.write_text("#!/bin/sh\n")
        spaced.chmod(0o755)
        install_templated_hook(repo, str(spaced))

        state = commit_hook_state(repo)

        assert state is not None
        assert state.ok is True

    @pytest.mark.parametrize(
        "script",
        [
            "#!/bin/sh\nnpx husky\n",
            "#!/bin/sh\nINSTALL_PYTHON=\nexit 0\n",
            "#!/bin/sh\nINSTALL_PYTHON='unterminated\nexit 0\n",
            "#!/bin/sh\nINSTALL_PYTHON=python3\nexit 0\n",
        ],
        ids=["no-line", "empty-value", "unterminated-quote", "relative-value"],
    )
    def test_unparseable_or_absent_install_python_is_healthy(
        self, repo: Path, script: str
    ) -> None:
        _install_pre_commit_hook(repo, script)

        state = commit_hook_state(repo)

        assert state is not None
        assert state.ok is True

    def test_invalid_utf8_hook_bytes_do_not_raise(self, repo: Path) -> None:
        _install_pre_commit_hook(repo, "#!/bin/sh\n")
        (repo / ".git" / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\n\xff\xfe\n")

        state = commit_hook_state(repo)

        assert state is not None
        assert state.ok is True

    def test_unreadable_hook_is_healthy(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_pre_commit_hook(repo, hook_script("/nonexistent/python"))
        real_read_text = Path.read_text

        def _read_text(
            self: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if self.name == "pre-commit":
                raise PermissionError(self.name)
            return real_read_text(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", _read_text)

        state = commit_hook_state(repo)

        assert state is not None
        assert state.ok is True

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.CalledProcessError(128, ["git"]),
            subprocess.TimeoutExpired(["git"], 1.0),
            OSError("boom"),
        ],
        ids=["called-process-error", "timeout", "os-error"],
    )
    def test_git_failure_is_transient_and_names_the_class_only(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> Path:
            raise exc

        monkeypatch.setattr("cw.doctor.commit_hook.git_hooks_dir", _raise)

        state = _bad(commit_hook_state(repo))

        assert state.transient is True
        assert (
            f"could not resolve the git hooks directory in {repo} "
            f"({type(exc).__name__}); is it a git checkout?"
        ) == state.detail
        assert "boom" not in state.detail

    def test_timeout_is_threaded_to_every_git_call(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cw.doctor.commit_hook as mod

        seen: dict[str, list[float | None]] = {"hooks_dir": [], "run_git": []}
        real_hooks_dir = mod.git_hooks_dir
        real_run_git = mod.run_git

        def _hooks_dir(path: Path, *, timeout: float | None = None) -> Path:
            seen["hooks_dir"].append(timeout)
            return real_hooks_dir(path, timeout=timeout)

        def _run_git(
            argv: Sequence[str],
            *,
            cwd: Path | str | None = None,
            capture_output: bool = False,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
            seen["run_git"].append(timeout)
            return real_run_git(
                argv, cwd=cwd, capture_output=capture_output, timeout=timeout
            )

        monkeypatch.setattr(mod, "git_hooks_dir", _hooks_dir)
        monkeypatch.setattr(mod, "run_git", _run_git)
        git_in(repo, "config", "core.hooksPath", "hk")

        commit_hook_state(repo)
        commit_hook_state(repo, timeout=_PROBE_TIMEOUT)

        assert seen["hooks_dir"] == [_DEFAULT_TIMEOUT, _PROBE_TIMEOUT]
        assert seen["run_git"] == [_DEFAULT_TIMEOUT, _PROBE_TIMEOUT]

    @pytest.mark.parametrize("mode", ["oserror", "timeout", "nonzero"])
    def test_hooks_path_lookup_failure_is_treated_as_unset(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        def _run_git(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if mode == "oserror":
                msg = "no git"
                raise OSError(msg)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(argv, 1.0)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        monkeypatch.setattr("cw.doctor.commit_hook.run_git", _run_git)

        state = _bad(commit_hook_state(repo))

        assert "no executable pre-commit hook" in state.detail
        assert "core.hooksPath" not in state.detail

    def test_core_bare_true_repo_still_gives_the_missing_hook_finding(
        self, repo: Path
    ) -> None:
        git_in(repo, "config", "core.bare", "true")

        state = _bad(commit_hook_state(repo))

        assert "no executable pre-commit hook" in state.detail


def _missing(repo: Path, _mp: pytest.MonkeyPatch) -> None:
    del repo


def _not_executable(repo: Path, _mp: pytest.MonkeyPatch) -> None:
    _install_pre_commit_hook(repo, hook_script(sys.executable))
    (repo / ".git" / "hooks" / "pre-commit").chmod(0o644)


def _hooks_path(repo: Path, _mp: pytest.MonkeyPatch) -> None:
    git_in(repo, "config", "core.hooksPath", "hk")


def _stale(repo: Path, _mp: pytest.MonkeyPatch) -> None:
    install_templated_hook(repo, "/nonexistent/python")


def _transient(repo: Path, mp: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise subprocess.TimeoutExpired(["git"], 1.0)

    del repo
    mp.setattr("cw.doctor.commit_hook.git_hooks_dir", _raise)


class TestCheckCommitHook:
    def test_empty_clients(self) -> None:
        assert _check_commit_hook({}) == []

    @pytest.mark.parametrize(
        "arrange",
        [_missing, _not_executable, _hooks_path, _stale, _transient],
        ids=["missing", "not-executable", "hooks-path", "stale", "transient"],
    )
    def test_every_failure_kind_is_a_warning_never_a_failure(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        arrange: Callable[[Path, pytest.MonkeyPatch], None],
    ) -> None:
        arrange(repo, monkeypatch)

        results = _check_commit_hook({"acme": make_hook_client(repo)})

        assert [r.name for r in results] == ["commit-hook/acme"]
        assert results[0].ok is True
        assert results[0].warn is True
        assert results[0].detail

    def test_healthy_hook_is_ok_without_warning(self, repo: Path) -> None:
        install_templated_hook(repo, sys.executable)

        results = _check_commit_hook({"acme": make_hook_client(repo)})

        assert [(r.name, r.ok, r.warn) for r in results] == [
            ("commit-hook/acme", True, False)
        ]
        assert "pre-commit hook installed at" in results[0].detail

    def test_no_commit_hooks_declared_gives_no_result(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        path = make_git_repo("nohooks")
        write_commit_hook_config(path, hook_stages=["pre-push"])

        assert _check_commit_hook({"acme": make_hook_client(path)}) == []

    def test_worktree_mode_client_uses_repo_path(self, repo: Path) -> None:
        client = make_hook_client(repo, branch="feature")

        results = _check_commit_hook({"acme": client})

        assert [r.name for r in results] == ["commit-hook/acme"]
        assert str(repo / ".git" / "hooks" / "pre-commit") in results[0].detail

    def test_non_existent_workspace_gives_no_result(self, tmp_path: Path) -> None:
        client = make_hook_client(tmp_path / "missing")

        assert _check_commit_hook({"acme": client}) == []


class TestRunDoctorIntegration:
    def test_run_doctor_reports_commit_hook_without_failing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _deps, "load_clients", lambda: {"acme": make_hook_client(repo)}
        )

        report = run_doctor()

        check = next(c for c in report.checks if c.name == "commit-hook/acme")
        assert check.ok is True
        assert check.warn is True
        assert "[WARN] commit-hook/acme" in format_report(report)
        baseline = [c for c in report.checks if c.name != "commit-hook/acme"]
        assert all(c.ok for c in report.checks) == all(c.ok for c in baseline)
