"""Tests for ``tests/_git_sandbox.py`` -- the sandboxed git runner (#3078).

The runner is the only way unit-tier test code launches git: it strips every
``GIT_*`` variable, confines discovery and ``HOME`` to the test root, and
refuses lexically any argument that names a path outside it. The raw-git
guard (installed suite-wide by ``tests/conftest.py``) refuses a git launch
from test code that bypasses it.

Each test here binds its own ``tmp_path`` as the root where it needs a
narrower one than the suite's basetemp.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import _git_sandbox
from tests._git_sandbox import (
    REPO_LOCATION_VARS,
    GitSandboxViolationError,
    sandbox_env,
    scrub_process_env,
    validate,
)
from tests._session_home import OPT_OUT_ENV_VARS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    """Bind ``tmp_path`` as the sandbox root for one test, then restore."""
    previous = _git_sandbox.bound_root()
    _git_sandbox.bind(tmp_path)
    yield tmp_path
    if previous is None:
        _git_sandbox.unbind()
    else:
        _git_sandbox.bind(previous)


@pytest.fixture
def repo(root: Path) -> Path:
    path = root / "repo"
    path.mkdir()
    _git_sandbox.run("init", "-q", "-b", "main", cwd=path)
    _git_sandbox.run("commit", "--allow-empty", "-q", "-m", "init", cwd=path)
    return path


class TestSandboxEnv:
    def test_strips_every_git_variable(self, tmp_path: Path) -> None:
        base = {
            "GIT_DIR": "/elsewhere/.git",
            "GIT_SSH_COMMAND": "ssh -i key",
            "GIT_ASKPASS": "/bin/askpass",
            "PATH": "/usr/bin",
        }

        env = sandbox_env(tmp_path, base=base)

        assert {k for k in env if k.startswith("GIT_")} == {
            "GIT_CEILING_DIRECTORIES",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
        }
        assert env["PATH"] == "/usr/bin"

    def test_confines_discovery_config_and_prompts(self, tmp_path: Path) -> None:
        env = sandbox_env(tmp_path, base={})

        assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve())
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_home_under_the_root_is_kept(self, tmp_path: Path) -> None:
        home = tmp_path / "_home"

        assert sandbox_env(tmp_path, base={"HOME": str(home)})["HOME"] == str(home)

    def test_home_outside_the_root_moves_under_it_with_an_identity(
        self, tmp_path: Path
    ) -> None:
        env = sandbox_env(tmp_path, base={"HOME": "/home/operator"})

        home = Path(env["HOME"])
        assert home.is_relative_to(tmp_path)
        assert "user" in (home / ".gitconfig").read_text(encoding="utf-8")

    def test_without_a_ceiling(self, tmp_path: Path) -> None:
        env = sandbox_env(tmp_path, base={}, ceiling=False)

        assert "GIT_CEILING_DIRECTORIES" not in env


class TestRunner:
    def test_poisoned_environment_is_ignored(
        self, repo: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = root / "other"
        other.mkdir()
        _git_sandbox.run("init", "-q", cwd=other)
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_SSH_COMMAND", "false")
        monkeypatch.setenv("GIT_ASKPASS", "false")

        assert _git_sandbox.git(repo, "rev-parse", "--show-toplevel") == str(
            repo.resolve()
        )

    def test_child_env_is_the_sandbox_env(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
        monkeypatch.setenv("HOME", "/home/operator")
        hook = root / "show-env"
        hook.write_text("#!/bin/sh\nenv\n")
        hook.chmod(0o755)

        result = _git_sandbox.run("-c", f"alias.showenv=!{hook}", "showenv")

        env = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        assert "GIT_DIR" not in env
        assert env["GIT_CEILING_DIRECTORIES"] == str(root.resolve())
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert Path(env["HOME"]).is_relative_to(root)

    def test_default_cwd_is_the_root(self, root: Path) -> None:
        origin = root / "origin.git"

        _git_sandbox.run("init", "--bare", "-q", str(origin))

        assert (origin / "HEAD").is_file()

    def test_check_false_returns_the_failure(self, root: Path) -> None:
        result = _git_sandbox.run("rev-parse", "--verify", "nope", check=False)

        assert result.returncode != 0

    def test_check_true_raises(self, root: Path) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            _git_sandbox.run("rev-parse", "--verify", "nope")

    def test_input_and_extra_env_reach_git(self, repo: Path) -> None:
        blob = _git_sandbox.run(
            "hash-object", "--stdin", cwd=repo, stdin="hello\n"
        ).stdout.strip()
        dated = _git_sandbox.run(
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "dated",
            cwd=repo,
            extra_env={"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z"},
        )

        assert dated.returncode == 0
        assert len(blob) == 40
        assert _git_sandbox.git(repo, "log", "-1", "--format=%ad", "--date=short") == (
            "2001-01-01"
        )

    @pytest.mark.parametrize("name", REPO_LOCATION_VARS)
    def test_a_repo_location_var_in_extra_env_is_refused(
        self, root: Path, name: str
    ) -> None:
        with pytest.raises(GitSandboxViolationError, match=name):
            _git_sandbox.run("status", extra_env={name: str(root)})

    def test_unbound_runner_refuses_to_run(self) -> None:
        previous = _git_sandbox.bound_root()
        _git_sandbox.unbind()
        try:
            with pytest.raises(GitSandboxViolationError, match="no sandbox root"):
                _git_sandbox.run("--version")
        finally:
            if previous is not None:
                _git_sandbox.bind(previous)

    def test_unenforced_containment_allows_an_outside_cwd(
        self, root: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        outside = tmp_path_factory.mktemp("outside-root")
        with _git_sandbox.containment(enforced=False):
            result = _git_sandbox.run("--version", cwd=outside)

        assert result.returncode == 0
        with pytest.raises(GitSandboxViolationError):
            _git_sandbox.run("--version", cwd=outside)


class TestValidate:
    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["-C", "/outside", "status"], id="dash-C"),
            pytest.param(["--git-dir=/outside", "status"], id="git-dir-equals"),
            pytest.param(["--git-dir", "/outside", "status"], id="git-dir-separate"),
            pytest.param(["--work-tree=/outside", "status"], id="work-tree-equals"),
            pytest.param(
                ["--work-tree", "/outside", "status"], id="work-tree-separate"
            ),
            pytest.param(["config", "--file", "/outside/cfg", "a.b"], id="file"),
            pytest.param(["clone", str(_REPO_ROOT), "x"], id="positional-abs"),
            pytest.param(["-c", "core.hooksPath=/abs/outside", "status"], id="dash-c"),
            pytest.param(["clone", "file:///outside/repo", "x"], id="file-url"),
            pytest.param(["add", "~/notes"], id="tilde"),
            pytest.param(["add", "../../../escape"], id="dotdot"),
        ],
    )
    def test_paths_outside_the_root_are_refused(
        self, argv: list[str], tmp_path: Path
    ) -> None:
        with pytest.raises(GitSandboxViolationError):
            validate(argv, cwd=tmp_path, root=tmp_path)

    def test_cwd_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GitSandboxViolationError, match="cwd"):
            validate(["status"], cwd=_REPO_ROOT, root=tmp_path)

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["-C", "{root}/repo", "status"], id="dash-C-inside"),
            pytest.param(["--git-dir={root}/repo/.git", "log"], id="git-dir-inside"),
            pytest.param(["clone", "{root}/a", "{root}/b"], id="positional-inside"),
            pytest.param(["clone", "file://{root}/a", "b"], id="file-url-inside"),
            pytest.param(["diff", "--output=/dev/null"], id="dev-null"),
            pytest.param(["diff", "/dev/null", "x"], id="dev-null-positional"),
            pytest.param(["log", "origin/main", "refs/heads/x"], id="refs"),
            pytest.param(["log", "HEAD~1..HEAD"], id="rev-range"),
            pytest.param(["remote", "add", "o", "https://h/r.git"], id="https-url"),
            pytest.param(["-c", "user.name=cw test", "commit"], id="dash-c-plain"),
            pytest.param(["add", "sub/../file"], id="dotdot-inside"),
        ],
    )
    def test_paths_inside_the_root_and_non_paths_are_accepted(
        self, argv: list[str], tmp_path: Path
    ) -> None:
        resolved = [arg.format(root=tmp_path) for arg in argv]

        validate(resolved, cwd=tmp_path, root=tmp_path)

    def test_symlink_inside_the_root_pointing_out_is_not_refused(
        self, tmp_path: Path
    ) -> None:
        """Documented limit: refusal is lexical and cannot see symlinks."""
        link = tmp_path / "link"
        link.symlink_to(_REPO_ROOT)

        validate(["-C", str(link), "status"], cwd=tmp_path, root=tmp_path)

    def test_a_message_that_looks_like_a_path_is_refused(self, tmp_path: Path) -> None:
        """Documented false positive: pass such text with ``-F`` or ``stdin=``."""
        with pytest.raises(GitSandboxViolationError):
            validate(["commit", "-m", "/x"], cwd=tmp_path, root=tmp_path)


class TestScrubProcessEnv:
    def test_removes_only_the_repo_location_vars(self) -> None:
        environ = dict.fromkeys(REPO_LOCATION_VARS, "/x")
        environ |= {
            "GIT_SSH_COMMAND": "ssh",
            "GIT_ASKPASS": "/bin/askpass",
            "GIT_EXEC_PATH": "/usr/lib/git-core",
            "PATH": "/usr/bin",
        }

        removed = scrub_process_env(environ)

        assert sorted(removed) == sorted(REPO_LOCATION_VARS)
        assert environ == {
            "GIT_SSH_COMMAND": "ssh",
            "GIT_ASKPASS": "/bin/askpass",
            "GIT_EXEC_PATH": "/usr/lib/git-core",
            "PATH": "/usr/bin",
        }

    def test_the_six_repo_location_vars(self) -> None:
        assert set(REPO_LOCATION_VARS) == {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_COMMON_DIR",
            "GIT_PREFIX",
        }

    @pytest.mark.parametrize("opt_out", OPT_OUT_ENV_VARS)
    def test_is_a_no_op_for_a_live_run(self, opt_out: str) -> None:
        environ = {"GIT_DIR": "/x", opt_out: "1"}

        assert scrub_process_env(environ) == []
        assert environ == {"GIT_DIR": "/x", opt_out: "1"}

    def test_a_fresh_interpreter_starts_without_a_leaked_git_dir(self) -> None:
        child_env = {
            k: v
            for k, v in os.environ.items()
            if k not in OPT_OUT_ENV_VARS and not k.startswith("GIT_")
        }
        child_env |= dict.fromkeys(REPO_LOCATION_VARS, "/leaked")
        probe = (
            "import os, tests._git_sandbox as g; g.scrub_process_env(); "
            f"print(sorted(set({list(REPO_LOCATION_VARS)!r}) & set(os.environ)))"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_REPO_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "[]"

    def test_conftest_scrubs_before_any_cw_import(self) -> None:
        """Pin the ordering, as ``TestHomeRedirectByConstruction`` does for HOME.

        ``tests._git_sandbox`` scrubs on import, so its import must sit next to
        the ``tests._session_home`` import and ahead of every ``cw`` import.
        """
        tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

        sandbox = modules.index("tests._git_sandbox")
        session_home = modules.index("tests._session_home")
        first_cw = next(
            i
            for i, name in enumerate(modules)
            if name == "cw" or name.startswith("cw.")
        )
        assert abs(sandbox - session_home) == 1
        assert max(sandbox, session_home) < first_cw
