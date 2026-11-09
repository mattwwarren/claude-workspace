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
import contextlib
import functools
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest

from cw._git import run_git
from tests import _git_sandbox, _host_probe_guard
from tests._git_sandbox import (
    REPO_LOCATION_VARS,
    GitSandboxViolationError,
    sandbox_env,
    scrub_process_env,
    validate,
)
from tests._session_home import OPT_OUT_ENV_VARS
from tests._tool_fakes import write_bin_stub

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


class _ShellKwargs(TypedDict):
    shell: bool


# The shell form is what the raw-guard tests exercise, so it is spelled once.
_SHELL: _ShellKwargs = {"shell": True}


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

        # A leaked GIT_DIR would point this at ``other/.git``; ``--show-toplevel``
        # would not notice (git takes the cwd as the work tree).
        assert _git_sandbox.git(repo, "rev-parse", "--absolute-git-dir") == str(
            (repo / ".git").resolve()
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
            "import os, tests._git_sandbox; "
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


# --- Raw-git guard (C4) ---------------------------------------------------------

_SRC_FILE = str(_REPO_ROOT / "src" / "cw" / "_git.py")
_SCRIPT_FILE = str(_REPO_ROOT / ".claude" / "scripts" / "gate_key.py")
_TEST_FILE = str(_REPO_ROOT / "tests" / "test_x.py")
_WRAPPER_FILE = str(_REPO_ROOT / "tests" / "_dispatch_lane_helpers.py")
_STDLIB_FILE = subprocess.__file__


def _frames(*filenames: str) -> list[SimpleNamespace]:
    """Synthetic frames, innermost first, carrying only ``f_code.co_filename``."""
    return [SimpleNamespace(f_code=SimpleNamespace(co_filename=f)) for f in filenames]


@pytest.fixture
def guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> _host_probe_guard.LaunchRecorder:
    """A raw-git guard with its own recorder, installed in front of the suite's."""
    recorder = _host_probe_guard.LaunchRecorder()
    _git_sandbox.install_raw_git_guard(monkeypatch, recorder, root=tmp_path)
    return recorder


class TestClassifyOrigin:
    def test_a_src_frame_anywhere_classifies_as_src(self) -> None:
        frames = _frames(_STDLIB_FILE, _TEST_FILE, _SRC_FILE, _TEST_FILE)

        assert _git_sandbox.classify_origin(frames) == "src"

    def test_a_passthrough_wrapper_over_src_is_allowed(self) -> None:
        """Test frame -> wrapper -> src -> subprocess.py, innermost first."""
        frames = _frames(_STDLIB_FILE, _WRAPPER_FILE, _SRC_FILE, _TEST_FILE)

        assert _git_sandbox.classify_origin(frames) == "src"

    def test_a_script_frame_classifies_as_script(self) -> None:
        assert _git_sandbox.classify_origin(_frames(_STDLIB_FILE, _SCRIPT_FILE)) == (
            "script"
        )

    def test_a_pure_test_stack_classifies_as_test(self) -> None:
        frames = _frames(_STDLIB_FILE, _WRAPPER_FILE, _TEST_FILE)

        assert _git_sandbox.classify_origin(frames) == "test"

    def test_cw_is_imported_from_this_checkout(self) -> None:
        """Frame classification needs ``cw`` to run from ``<repo>/src/cw``."""
        import cw

        assert Path(cw.__file__).resolve().is_relative_to(_REPO_ROOT / "src" / "cw")
        _git_sandbox.assert_cw_from_checkout()

    def test_assert_cw_from_checkout_fails_loudly_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cw

        monkeypatch.setattr(cw, "__file__", "/site-packages/cw/__init__.py")

        with pytest.raises(AssertionError, match="site-packages"):
            _git_sandbox.assert_cw_from_checkout()


class TestIsGitLaunch:
    @pytest.mark.parametrize(
        ("args", "kwargs"),
        [
            pytest.param(["git", "status"], {}, id="list"),
            pytest.param(("git",), {}, id="tuple"),
            pytest.param([b"git", b"init"], {}, id="bytes"),
            pytest.param([Path("/usr/bin/git")], {}, id="pathlike"),
            pytest.param("git status | cat", {"shell": True}, id="shell"),
            pytest.param(["x"], {"executable": "/usr/bin/git"}, id="executable"),
            pytest.param(
                "git status",
                {"shell": True, "executable": "/bin/bash"},
                id="shell-with-executable-shell",
            ),
        ],
    )
    def test_git_launch_forms(self, args: object, kwargs: dict[str, object]) -> None:
        assert _git_sandbox.is_git_launch(args, kwargs)

    def test_positional_shell_and_executable_forms(self) -> None:
        """``Popen`` takes ``executable`` and ``shell`` after ``bufsize`` too."""
        shell_rest = (-1, "/bin/bash", None, None, None, None, True, True)
        executable_rest = (-1, "/usr/bin/git")

        assert _git_sandbox.is_git_launch("git status", {}, shell_rest)
        assert _git_sandbox.is_git_launch(["x"], {}, executable_rest)
        assert not _git_sandbox.is_git_launch("echo git", {}, shell_rest)

    @pytest.mark.parametrize(
        ("args", "kwargs"),
        [
            pytest.param(["gitk"], {}, id="other-program"),
            pytest.param("echo git", {"shell": True}, id="git-not-first"),
            pytest.param([], {}, id="empty"),
            pytest.param(None, {}, id="none"),
        ],
    )
    def test_other_launches(self, args: object, kwargs: dict[str, object]) -> None:
        assert not _git_sandbox.is_git_launch(args, kwargs)


class TestRawGitGuard:
    def test_raw_launch_from_test_code_raises(
        self, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        with pytest.raises(_git_sandbox.RawGitLaunchError):
            subprocess.run(["git", "--version"], capture_output=True, check=False)
        with pytest.raises(_git_sandbox.RawGitLaunchError):
            subprocess.run([b"git", b"--version"], capture_output=True, check=False)
        with pytest.raises(_git_sandbox.RawGitLaunchError):
            subprocess.run([Path("git"), "--version"], capture_output=True, check=False)
        with pytest.raises(_git_sandbox.RawGitLaunchError):
            subprocess.run("git --version", **_SHELL, capture_output=True, check=False)

        assert [v.kind for v in guarded.violations()] == ["git"] * 4

    def test_a_shell_launch_with_an_executable_shell_raises(
        self, tmp_path: Path, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        """``executable`` swaps ``/bin/sh``, not the command string it runs.

        ``PATH`` holds only a stub ``git`` so a guard that failed open would
        run it, never the host's git.
        """
        bin_dir = write_bin_stub(tmp_path, "git", "#!/bin/sh\nexit 0\n")
        env = {"PATH": str(bin_dir)}

        with pytest.raises(_git_sandbox.RawGitLaunchError):
            subprocess.run(
                "git --version",
                **_SHELL,
                executable="/bin/bash",
                env=env,
                check=False,
            )

        assert len(guarded.violations()) == 1

    def test_a_non_git_shell_launch_with_an_executable_shell_passes(
        self, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        result = subprocess.run(
            "echo git",
            **_SHELL,
            executable="/bin/bash",
            capture_output=True,
            check=False,
        )

        assert result.stdout == b"git\n"
        assert guarded.violations() == []

    def test_the_runner_passes(
        self, root: Path, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        assert _git_sandbox.run("--version").returncode == 0
        assert guarded.violations() == []

    def test_a_real_src_launch_passes(
        self, repo: Path, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        result = run_git(["rev-parse", "HEAD"], cwd=repo, capture_output=True)

        assert result.returncode == 0
        assert guarded.violations() == []

    @pytest.mark.parametrize("origin", ["src", "script"])
    @pytest.mark.parametrize("name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"])
    def test_a_leaked_repo_var_in_a_src_or_script_launch_is_refused(
        self, origin: str, name: str
    ) -> None:
        detail = _git_sandbox.leaked_env_detail({name: "/x", "PATH": "/bin"}, origin)

        assert detail is not None
        assert name in detail
        assert origin in detail

    def test_a_clean_src_env_and_os_environ_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _git_sandbox.leaked_env_detail({"PATH": "/bin"}, "src") is None
        monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/index")

        assert _git_sandbox.leaked_env_detail(None, "src") is not None

    def test_containment_check_for_src_launches(self, tmp_path: Path) -> None:
        inside = _git_sandbox.outside_root_detail(["git", "status"], tmp_path, tmp_path)
        outside = _git_sandbox.outside_root_detail(
            ["git", "-C", str(_REPO_ROOT), "status"], tmp_path, tmp_path
        )

        assert inside is None
        assert outside is not None

    def test_a_swallowed_raw_launch_is_still_recorded(
        self, guarded: _host_probe_guard.LaunchRecorder
    ) -> None:
        """A broad ``except`` cannot hide it: the record fails teardown."""
        with contextlib.suppress(Exception):
            subprocess.run(["git", "status"], check=False)

        assert len(guarded.violations()) == 1
        with pytest.raises(AssertionError, match="raw git"):
            _host_probe_guard.finish(guarded, _Node())

    @pytest.mark.raw_git_allowed
    def test_raw_git_allowed_marker_exempts_the_suite_guard(self) -> None:
        result = subprocess.run(["git", "--version"], capture_output=True, check=False)

        assert result.returncode == 0

    @pytest.mark.integration
    def test_integration_tests_are_exempt(self) -> None:
        result = subprocess.run(["git", "--version"], capture_output=True, check=False)

        assert result.returncode == 0


class _Node:
    """Minimal ``pytest.Item`` stand-in with no markers."""

    def iter_markers(self, name: str | None = None) -> Iterator[pytest.Mark]:
        del name
        return iter(())


# --- Static scan: no raw git launch sites in tests/ ------------------------------

_LAUNCHERS = frozenset({"run", "Popen", "check_output", "check_call", "call"})
_SUBPROCESS_OWNERS = frozenset({"subprocess", "sp", "_sp"})
_TESTS_DIR = _REPO_ROOT / "tests"

# Files allowed to launch git with a literal argv: the runner itself and this
# file's deliberate-violation tests. Elsewhere only a test decorated
# ``@pytest.mark.raw_git_allowed`` may.
_LAUNCH_ALLOWLIST: frozenset[str] = frozenset(
    {"tests/_git_sandbox.py", "tests/test_git_sandbox.py"}
)

# Non-literal argv launches, each reviewed: (file, enclosing qualname). None
# runs git from test code: a generic CLI runner, the guard's own
# deliberate raw launches, and the live (integration) opencode suite.
_NON_LITERAL_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("tests/test_classify_merge_conflict.py", "_run_cli"),
        (
            "tests/test_conftest.py",
            "TestSuiteWideRawGitGuard.test_raw_git_launch_from_test_code_is_refused",
        ),
        (
            "tests/test_git_ceiling.py",
            "TestInstalledHook.test_env_none_path_is_confined_too",
        ),
        ("tests/test_opencode_contract_live.py", "_run_opencode"),
        (
            "tests/test_pre_push_hook_git_env.py",
            "test_hook_entry_strips_git_env_before_uv",
        ),
        ("tests/test_tool_fakes.py", "_run"),
        (
            "tests/test_worktree_git.py",
            "TestProbeCheckedOutBranch."
            "test_non_utf8_branch_name_decodes_with_replacement_instead_of_raising",
        ),
    }
)

# Every ``["git", ...]`` literal that is argv DATA, not a launch: exception
# constructors, ``CompletedProcess`` fake returns, comparisons against
# recorded argv, ``pytest.param`` values and recorded-call expectations.
# Keyed (file, enclosing qualname, category) -> count, so it survives line
# moves; regenerate an entry only after checking the literal is not a launch.
_GIT_ARGV_DATA_ALLOWLIST: dict[tuple[str, str, str], int] = {
    ("tests/_changelog_repair_helpers.py", "FakeGhHead._pushed", "comparison"): 1,
    ("tests/_changelog_repair_helpers.py", "ScriptedGit.__call__", "argv-data"): 1,
    ("tests/_git_sandbox.py", "run", "argv-data"): 1,
    ("tests/_review_monitor_helpers.py", "FakeCommands.run_git", "argv-data"): 1,
    (
        "tests/test_changelog_repair_ledger_remap.py",
        "test_push_precedes_the_ledger_save_which_precedes_the_arm",
        "comparison",
    ): 1,
    (
        "tests/test_changelog_repair_real_git.py",
        "test_real_repair_of_an_armed_pr_disarms_before_the_push._logged",
        "argv-data",
    ): 1,
    (
        "tests/test_claude_scripts_git_env.py",
        "test_an_unstripped_script_launch_trips_the_raw_git_guard",
        "comparison",
    ): 1,
    (
        "tests/test_cli_dev_queue_status.py",
        "TestCommitHookProbeBounds.test_transient_state_prints_no_block._timeout",
        "exception",
    ): 1,
    (
        "tests/test_cli_review_check_approval.py",
        "test_check_ref_format_exec_failure_reads_as_invalid_branch._boom",
        "exception",
    ): 1,
    (
        "tests/test_cli_review_check_approval.py",
        "test_toplevel_exec_failure_falls_back_to_the_worktree_directory._boom",
        "exception",
    ): 1,
    (
        "tests/test_cli_review_converge.py",
        "TestStaleDefaultRefRefresh.test_a_failed_refresh_keeps_the_stale_verdict",
        "exception",
    ): 1,
    (
        "tests/test_cli_review_converge_delta.py",
        "TestCodexRound.test_no_fetch_is_issued._record",
        "comparison",
    ): 1,
    (
        "tests/test_codex_confirm_feedback.py",
        "test_a_git_failure_warns_and_yields_nothing",
        "exception",
    ): 2,
    (
        "tests/test_codex_fix_loop.py",
        "TestFixInvocation.test_commit_exception_treated_as_fix_failure._boom",
        "exception",
    ): 1,
    (
        "tests/test_codex_fix_loop_commit.py",
        "TestBeginCycle.test_baseline_probe_git_failure_parks_codex_error._boom",
        "exception",
    ): 1,
    (
        "tests/test_codex_fix_loop_commit.py",
        "TestInvocationFailure.test_unmeasurable_tree_keeps_the_transient_park._second_call_fails",
        "exception",
    ): 1,
    (
        "tests/test_codex_fix_loop_divergence.py",
        "TestLoopGeneratedFindings.test_git_failure_returns_empty_list_and_warns",
        "exception",
    ): 1,
    ("tests/test_codex_fix_loop_hook_failure.py", "<module>", "binding"): 1,
    (
        "tests/test_codex_fix_loop_hook_repair.py",
        "TestHookRepairTurn.test_failed_restage_after_repair_timeout_parks_as_git_error._broken_restage",
        "exception",
    ): 1,
    (
        "tests/test_codex_fix_loop_hook_repair.py",
        "TestHookRepairTurn.test_non_hook_git_error_on_the_recommit_parks_codex_error._first_hook_then_git_error",
        "exception",
    ): 1,
    ("tests/test_codex_fix_loop_hook_repair.py", "_failure", "argv-data"): 1,
    (
        "tests/test_codex_fix_loop_push.py",
        "test_push_and_verify_head_raises_on_push_failure",
        "comparison",
    ): 1,
    (
        "tests/test_codex_review_default_branch_config.py",
        "TestReadAndExists.test_a_git_failure_reads_as_absent_without_raising",
        "fake-return",
    ): 1,
    ("tests/test_default_branch_merge_policy_docs.py", "_run_fence", "argv-data"): 1,
    (
        "tests/test_dispatch.py",
        "TestRunDispatchLoopTickFetchGate.test_one_gate_spans_iterations_so_a_backoff_carries_over._hang",
        "comparison",
    ): 1,
    (
        "tests/test_dispatch_branch_freshness.py",
        "TestFailOpen.test_a_git_failure_is_not_a_conflict",
        "exception",
    ): 1,
    (
        "tests/test_dispatch_gating_freshness.py",
        "TestFreshnessGateAutoFF.test_auto_ff_ff_raises_falls_through_to_ticket_needs_sync._boom",
        "exception",
    ): 1,
    ("tests/test_dispatch_gating_freshness.py", "_HungFetch.__call__", "comparison"): 1,
    (
        "tests/test_dispatch_review_approval_gate.py",
        "_timeout_everywhere._timeout",
        "exception",
    ): 1,
    (
        "tests/test_doctor_commit_hook.py",
        "TestCommitHookState.test_git_failure_is_transient_and_names_the_class_only",
        "exception",
    ): 2,
    ("tests/test_doctor_commit_hook.py", "_transient._raise", "exception"): 1,
    (
        "tests/test_doctor_hook_install_python.py",
        "TestNotFlagged.test_git_hooks_dir_failure",
        "exception",
    ): 2,
    (
        "tests/test_git.py",
        "TestGitHooksDir.test_timeout_and_capture_are_forwarded_to_run_git._record",
        "fake-return",
    ): 1,
    (
        "tests/test_git.py",
        "TestGitOutput.test_kwargs_are_forwarded_with_a_clean_env",
        "comparison",
    ): 1,
    (
        "tests/test_git.py",
        "TestRunGit.test_kwargs_are_forwarded_with_a_clean_env",
        "comparison",
    ): 1,
    ("tests/test_git_ceiling.py", "_toplevel_argv", "argv-data"): 1,
    (
        "tests/test_git_sandbox.py",
        "TestIsGitLaunch.test_git_launch_forms",
        "pytest-param",
    ): 3,
    (
        "tests/test_git_sandbox.py",
        "TestRawGitGuard.test_containment_check_for_src_launches",
        "argv-data",
    ): 2,
    (
        "tests/test_git_sandbox.py",
        "TestRawGitGuard.test_raw_launch_from_test_code_raises",
        "argv-data",
    ): 1,
    ("tests/test_git_sandbox.py", "_is_git_literal", "comparison"): 1,
    (
        "tests/test_host_probe_guard.py",
        "TestBlockedBinary.test_everything_else_passes_through",
        "pytest-param",
    ): 1,
    (
        "tests/test_local_runner.py",
        "test_build_aiderignore_fails_open_when_git_ls_files_errors",
        "exception",
    ): 1,
    (
        "tests/test_mechanical_repair.py",
        "test_success_small_tier_pushes_with_lease_and_arms_pinned_to_new_sha",
        "comparison",
    ): 1,
    ("tests/test_mechanical_repair_real_git.py", "_logged_git._logged", "argv-data"): 1,
    (
        "tests/test_mechanical_repair_real_git.py",
        "test_unmerged_paths_raises_the_passed_stop_on_a_git_failure",
        "argv-data",
    ): 1,
    (
        "tests/test_merge_probe.py",
        "TestMergeTree.test_timeout_is_unavailable._timeout",
        "exception",
    ): 1,
    (
        "tests/test_merge_probe.py",
        "TestMergeTree.test_unreadable_output_is_unavailable._garbage",
        "fake-return",
    ): 1,
    (
        "tests/test_merge_probe.py",
        "TestProbeUnavailable.test_merge_base_error_exit._fake",
        "fake-return",
    ): 1,
    (
        "tests/test_merge_probe.py",
        "TestProbeUnavailable.test_merge_tree_error_exit._fake",
        "fake-return",
    ): 1,
    (
        "tests/test_merge_probe.py",
        "TestProbeUnavailable.test_timeout_and_every_call_is_bounded._record",
        "exception",
    ): 1,
    (
        "tests/test_pr_feedback.py",
        "TestConflicts.test_merge_tree_failure_raises",
        "exception",
    ): 1,
    (
        "tests/test_pr_feedback.py",
        "TestConflicts.test_merge_tree_failure_raises",
        "fake-return",
    ): 1,
    ("tests/test_prep_pr_finalize.py", "_with_toplevel._handler", "comparison"): 1,
    (
        "tests/test_prep_pr_finalize.py",
        "test_arm_automerge_subdirectory_repo_path_binds_gh_to_toplevel",
        "comparison",
    ): 1,
    (
        "tests/test_prep_pr_finalize.py",
        "test_cmd_arm_automerge_binds_gh_calls_to_repo_path",
        "argv-data",
    ): 1,
    (
        "tests/test_prep_pr_finalize.py",
        "test_cmd_arm_automerge_defaults_repo_path_to_git_toplevel",
        "argv-data",
    ): 1,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_armed_pr_is_disarmed_right_before_the_push_and_rearmed_pinned",
        "argv-data",
    ): 2,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_armed_pr_is_disarmed_right_before_the_push_and_rearmed_pinned",
        "comparison",
    ): 2,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_crash_after_push_is_post_push_failed_after_cleanup",
        "comparison",
    ): 1,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_pr_closed_or_rebased_onto_another_base_before_the_push_parks",
        "comparison",
    ): 1,
    ("tests/test_reconcile_changelog_repair.py", "test_rearm_failure", "comparison"): 1,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_success_orders_push_taken_fence_arm_then_cleanup",
        "comparison",
    ): 2,
    (
        "tests/test_reconcile_changelog_repair.py",
        "test_third_party_arm_after_push_is_disarmed_when_fence_fails",
        "comparison",
    ): 1,
    (
        "tests/test_reconcile_harvest_synthesis.py",
        "test_capture_local_harvest_facts_timeout_defers_and_warns",
        "exception",
    ): 1,
    (
        "tests/test_reconcile_harvest_synthesis.py",
        "test_capture_timeout_is_not_stored_and_propagates",
        "exception",
    ): 1,
    (
        "tests/test_reconcile_harvest_synthesis.py",
        "test_captured_git_failure_is_replayed_on_lookup",
        "exception",
    ): 1,
    (
        "tests/test_reconcile_harvest_synthesis.py",
        "test_synthesize_harvest_sentinel_parks_captured_git_errors",
        "exception",
    ): 1,
    (
        "tests/test_reconcile_harvest_synthesis.py",
        "test_synthesize_harvest_sentinel_propagates_timeout_and_unavailable",
        "exception",
    ): 1,
    (
        "tests/test_reconcile_local.py",
        "test_captured_git_failure_parks_at_row_entry_marker",
        "exception",
    ): 1,
    (
        "tests/test_review_approval.py",
        "test_full_sha_lookup_exec_failure_is_unreadable_and_transient._fail_full_sha",
        "exception",
    ): 1,
    (
        "tests/test_review_approval.py",
        "test_git_errors_in_the_ancestry_walk_are_unreadable._inject",
        "exception",
    ): 1,
    (
        "tests/test_review_approval.py",
        "test_git_timeout_is_unreadable_and_transient._timeout",
        "exception",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestResolve.test_empty_output_raises",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestResolveDefaultBranchRef.test_an_empty_probe_output_is_unresolved",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestResolveLoopDiffBase.test_a_failing_probe_returns_the_bare_name",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestResolveLoopDiffBase.test_a_merge_base_failure_after_a_good_probe_raises",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestResolveLoopDiffBase.test_a_probe_that_prints_nothing_returns_the_bare_name",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_diff_base.py",
        "TestVerify.test_a_git_failure_checking_ancestry_raises",
        "fake-return",
    ): 1,
    (
        "tests/test_review_convergence_merge_tolerance.py",
        "TestGitStdoutIsBounded.test_a_hung_git_is_unreadable._hang",
        "exception",
    ): 1,
    (
        "tests/test_review_convergence_merge_tolerance.py",
        "TestRealGitShapes.test_a_merge_tree_timeout_is_merge_tree_unavailable._slow",
        "exception",
    ): 1,
    ("tests/test_review_monitor_check.py", "_fake", "argv-data"): 1,
    (
        "tests/test_review_monitor_check.py",
        "test_check_author_new_commit_with_failing_ci",
        "comparison",
    ): 1,
    (
        "tests/test_review_monitor_delta.py",
        "test_apply_code_changes_reports_touched_threads",
        "argv-data",
    ): 2,
    (
        "tests/test_review_monitor_delta.py",
        "test_apply_code_changes_reviewer_with_empty_diff_has_no_delta",
        "argv-data",
    ): 1,
    (
        "tests/test_review_monitor_delta.py",
        "test_detect_touched_threads_diffs_from_delta_base",
        "argv-data",
    ): 2,
    (
        "tests/test_review_monitor_shell.py",
        "test_runner_builds_argv_and_strips_stdout",
        "argv-data",
    ): 2,
    (
        "tests/test_tool_fakes.py",
        "TestInProcessGhFakes.test_ready_gh_scripts_gh_and_delegates_the_rest",
        "argv-data",
    ): 2,
    (
        "tests/test_worktree_freshness.py",
        "TestFetchUnderTheTickGate.test_is_main_behind_origin_reads_not_stale_and_stops_at_the_fetch",
        "argv-data",
    ): 1,
    (
        "tests/test_worktree_git.py",
        "TestProbeCheckedOutBranch.test_non_utf8_branch_name_decodes_with_replacement_instead_of_raising",
        "argv-data",
    ): 2,
    (
        "tests/test_worktree_git.py",
        "TestProbeCheckedOutBranch.test_runs_git_with_c_locale_utf8_replace_and_without_git_env",
        "comparison",
    ): 1,
}


def _is_git_literal(node: ast.AST) -> bool:
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value in ("git", b"git")


def _launch_argv(node: ast.Call) -> ast.expr | None:
    """The argv of a ``subprocess`` launch call, or ``None`` if not a launch."""
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = ast.unparse(func.value)
        if func.attr not in _LAUNCHERS or owner not in _SUBPROCESS_OWNERS:
            return None
    elif not (isinstance(func, ast.Name) and func.id == "clean_subprocess_run"):
        return None
    if node.args:
        return node.args[0]
    return next((k.value for k in node.keywords if k.arg == "args"), None)


def _data_category(parent: ast.AST | None) -> str:
    """Why a ``["git", ...]`` literal that is not a launch argv is there."""
    if isinstance(parent, ast.Compare):
        return "comparison"
    if isinstance(parent, ast.Call):
        name = ast.unparse(parent.func).rsplit(".", 1)[-1]
        if name in {"TimeoutExpired", "CalledProcessError", "OSError"}:
            return "exception"
        if name == "CompletedProcess":
            return "fake-return"
        if name == "param":
            return "pytest-param"
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        return "binding"
    return "argv-data"


class _SiteScan(ast.NodeVisitor):
    """Collect launch, non-literal launch and data sites with their qualname."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.raw_allowed: list[bool] = []
        self.parents: dict[int, ast.AST] = {}
        self.launches: list[tuple[str, int]] = []
        self.non_literal: list[str] = []
        self.data: list[tuple[str, str]] = []
        self.launch_argvs: set[int] = set()

    def _qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node)

    def _scoped(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        self.scope.append(node.name)
        self.raw_allowed.append(
            any(
                ast.unparse(d).endswith("mark.raw_git_allowed")
                for d in node.decorator_list
            )
        )
        self._children(node)
        self.raw_allowed.pop()
        self.scope.pop()

    def _children(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.parents[id(child)] = node
            self.visit(child)

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            argv = _launch_argv(node)
            if argv is not None:
                if _is_git_literal(argv):
                    if not any(self.raw_allowed):
                        self.launches.append((self._qualname(), node.lineno))
                    self.launch_argvs.add(id(argv))
                elif not isinstance(
                    argv, (ast.List, ast.Tuple, ast.Constant, ast.JoinedStr)
                ):
                    self.non_literal.append(self._qualname())
        if _is_git_literal(node) and id(node) not in self.launch_argvs:
            parent = self.parents.get(id(node))
            self.data.append((self._qualname(), _data_category(parent)))
        self._children(node)


@functools.cache
def _scan(path: Path) -> _SiteScan:
    scan = _SiteScan()
    scan.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return scan


def _test_files() -> list[tuple[str, Path]]:
    return [
        (path.relative_to(_REPO_ROOT).as_posix(), path)
        for path in sorted(_TESTS_DIR.rglob("*.py"))
    ]


def _data_sites() -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for rel, path in _test_files():
        for qualname, category in _scan(path).data:
            key = (rel, qualname, category)
            counts[key] = counts.get(key, 0) + 1
    return counts


class TestNoRawGitSites:
    """Static half of the raw-git guard: no literal git launch left in tests/."""

    def test_no_literal_git_launch_outside_the_allowlist(self) -> None:
        strays = [
            f"{rel}:{line} ({qualname}) launches git raw; use tests/_git_sandbox.run"
            for rel, path in _test_files()
            if rel not in _LAUNCH_ALLOWLIST
            for qualname, line in _scan(path).launches
        ]

        assert not strays, "\n".join(strays)

    def test_non_literal_launches_are_reviewed(self) -> None:
        found = {
            (rel, qualname)
            for rel, path in _test_files()
            for qualname in _scan(path).non_literal
        }

        assert found == _NON_LITERAL_ALLOWLIST

    def test_git_argv_data_sites_match_the_allowlist(self) -> None:
        """Every non-launch ``["git", ...]`` literal is reviewed argv data.

        The allowlist key is line-independent: (file, enclosing qualname,
        category) with a count. A new literal, or a launch rewritten as data,
        fails here; so does a stale entry.
        """
        assert _data_sites() == _GIT_ARGV_DATA_ALLOWLIST

    def test_the_scan_sees_each_form(self, tmp_path: Path) -> None:
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import subprocess\n"
            "def f(argv):\n"
            "    subprocess.run(['git', 'status'])\n"
            "    subprocess.check_output(args=(b'git', b'log'))\n"
            "    subprocess.run(argv)\n"
            "    raise subprocess.CalledProcessError(1, ['git', 'x'])\n"
            "    assert argv == ['git', 'y']\n"
            "@pytest.mark.raw_git_allowed\n"
            "def g():\n"
            "    subprocess.run(['git', 'status'])\n",
            encoding="utf-8",
        )

        scan = _scan(probe)

        assert scan.launches == [("f", 3), ("f", 4)]
        assert scan.non_literal == ["f"]
        assert scan.data == [("f", "exception"), ("f", "comparison")]
