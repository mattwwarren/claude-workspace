"""Tests for ``tests/_host_probe_guard.py``: host-state probe isolation (#2664, #3078).

The helpers are exercised directly here with their own recorder and test root,
so they are proven independent of the autouse wiring in ``tests/conftest.py``
(``_hide_optional_binaries``, ``_isolate_process_table`` and the teardown
recorder), which ``tests/test_conftest.py`` pins separately.

A "host" binary is one outside the test root. :func:`host_bin` builds one in
a directory from ``tempfile`` (the session's ``TMPDIR``), which is never inside
the pytest basetemp, so the guard sees exactly what an installed ``gh`` looks
like.

The suite's default ``subprocess.run`` fakes for gh and the CI-absent tools
answer before ``Popen``, so every test here restores the real ``run`` first.

The ``os.exec*``/``os.spawn*`` tests install a recording stand-in for the real
``os`` function *before* the guard, so a guard that failed open would call the
stand-in instead of replacing the test process.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import psutil
import pytest

from tests._host_probe_guard import (
    GUARDED_TOOLS,
    LaunchRecorder,
    ToolLaunchBlockedError,
    blocked_binary,
    empty_process_table,
    finish,
    install_binary_launch_guard,
    scoped_process_iter,
)
from tests._tool_fakes import restore_real_run, write_bin_stub

_DENY = frozenset({"alpha", "beta"})
_NO_FORCED: frozenset[str] = frozenset()
_SCRIPT = "#!/bin/sh\nexit 0\n"


class _ShellKwargs(TypedDict):
    shell: bool


# The shell form is what these tests exercise, so it is spelled once here.
_SHELL: _ShellKwargs = {"shell": True}


@pytest.fixture(autouse=True)
def _launches_reach_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite's ``subprocess.run`` tool defaults: these tests need the guard."""
    restore_real_run(monkeypatch)


@pytest.fixture
def host_bin() -> Iterator[Path]:
    """A bin dir OUTSIDE the test root holding a stub for every guarded tool."""
    with tempfile.TemporaryDirectory(prefix="cw-host-bin-") as raw:
        bin_dir = Path(raw)
        for name in (*GUARDED_TOOLS, *_DENY):
            stub = bin_dir / name
            stub.write_text(_SCRIPT)
            stub.chmod(0o755)
        yield bin_dir


@pytest.fixture
def recorder() -> LaunchRecorder:
    return LaunchRecorder()


@pytest.fixture
def guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorder: LaunchRecorder
) -> LaunchRecorder:
    """Install the guard for every real tool, rooted at this test's tmp_path."""
    install_binary_launch_guard(
        monkeypatch, GUARDED_TOOLS, _NO_FORCED, root=tmp_path, recorder=recorder
    )
    return recorder


def _node(*markers: pytest.MarkDecorator) -> SimpleNamespace:
    marks = [m.mark for m in markers]

    def _iter_markers(name: str) -> Iterator[pytest.Mark]:
        return iter([m for m in marks if m.name == name])

    def _closest(name: str) -> pytest.Mark | None:
        return next(_iter_markers(name), None)

    return SimpleNamespace(iter_markers=_iter_markers, get_closest_marker=_closest)


class TestBlockedBinary:
    """``blocked_binary`` decides which launches the guard refuses."""

    def test_bare_name_resolving_outside_the_root_is_blocked(
        self, host_bin: Path, tmp_path: Path
    ) -> None:
        env = {"PATH": str(host_bin)}

        assert blocked_binary(["alpha"], _DENY, root=tmp_path, env=env) == "alpha"
        assert blocked_binary(("beta", "-v"), _DENY, root=tmp_path, env=env) == "beta"

    def test_bare_name_resolving_nowhere_is_blocked(self, tmp_path: Path) -> None:
        env = {"PATH": str(tmp_path / "empty")}

        assert blocked_binary(["alpha"], _DENY, root=tmp_path, env=env) == "alpha"

    def test_bare_name_resolving_inside_the_root_is_allowed(
        self, tmp_path: Path
    ) -> None:
        bin_dir = write_bin_stub(tmp_path, "alpha", _SCRIPT)
        env = {"PATH": str(bin_dir)}

        assert blocked_binary(["alpha"], _DENY, root=tmp_path, env=env) is None

    def test_absolute_paths_are_judged_by_location(
        self, host_bin: Path, tmp_path: Path
    ) -> None:
        inside = write_bin_stub(tmp_path, "alpha", _SCRIPT) / "alpha"

        assert blocked_binary([str(inside)], _DENY, root=tmp_path) is None
        assert blocked_binary([str(host_bin / "alpha")], _DENY, root=tmp_path) == (
            "alpha"
        )
        assert blocked_binary(["/usr/bin/beta"], _DENY, root=tmp_path) == "beta"

    def test_relative_path_resolves_against_cwd(self, tmp_path: Path) -> None:
        write_bin_stub(tmp_path, "alpha", _SCRIPT)

        assert blocked_binary(["bin/alpha"], _DENY, root=tmp_path, cwd=tmp_path) is None

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param([b"alpha"], id="bytes-first-element"),
            pytest.param([Path("alpha")], id="pathlike-first-element"),
            pytest.param(b"alpha", id="bytes-program"),
            pytest.param("alpha", id="str-program"),
        ],
    )
    def test_bytes_and_pathlike_argv_are_normalised(
        self, args: object, tmp_path: Path
    ) -> None:
        env = {"PATH": str(tmp_path / "empty")}

        assert blocked_binary(args, _DENY, root=tmp_path, env=env) == "alpha"

    def test_shell_string_is_judged_by_its_first_word(self, tmp_path: Path) -> None:
        env = {"PATH": str(tmp_path / "empty")}

        assert (
            blocked_binary(
                "alpha --x | cat", _DENY, root=tmp_path, env=env, via_shell=True
            )
            == "alpha"
        )
        assert (
            blocked_binary(["beta x"], _DENY, root=tmp_path, env=env, via_shell=True)
            == "beta"
        )
        assert (
            blocked_binary("echo alpha", _DENY, root=tmp_path, env=env, via_shell=True)
            is None
        )
        assert (
            blocked_binary("'unterminated", _DENY, root=tmp_path, via_shell=True)
            is None
        )

    def test_executable_keyword_wins_over_argv(self, tmp_path: Path) -> None:
        env = {"PATH": str(tmp_path / "empty")}

        assert (
            blocked_binary(
                ["harmless"], _DENY, root=tmp_path, env=env, executable="beta"
            )
            == "beta"
        )

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["git", "status"], id="not-guarded"),
            pytest.param([], id="empty-list"),
            pytest.param((), id="empty-tuple"),
            pytest.param(None, id="not-a-sequence"),
            pytest.param([3], id="int-first-element"),
            pytest.param("", id="empty-shell"),
        ],
    )
    def test_everything_else_passes_through(self, args: object, tmp_path: Path) -> None:
        assert blocked_binary(args, _DENY, root=tmp_path, via_shell=args == "") is None


class TestToolLaunchGuard:
    """The installed guard: every launch form, per real guarded tool."""

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_bare_name_run_and_popen_are_blocked(
        self,
        tool: str,
        host_bin: Path,
        guard: LaunchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", f"{host_bin}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(ToolLaunchBlockedError) as excinfo:
            subprocess.run([tool, "--version"], check=False)
        with pytest.raises(FileNotFoundError):
            subprocess.Popen([tool])

        assert excinfo.value.errno == errno.ENOENT
        assert excinfo.value.filename == tool
        assert f"launch of '{tool}' blocked" in str(excinfo.value)
        assert "tests/_tool_fakes.py" in str(excinfo.value)
        assert [v.name for v in guard.violations()] == [tool, tool]

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_bytes_and_pathlike_argv_are_blocked(
        self, tool: str, guard: LaunchRecorder
    ) -> None:
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run([os.fsencode(tool)], check=False)
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run([Path(tool)], check=False)
        assert len(guard.violations()) == 2

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_absolute_host_path_is_blocked(
        self, tool: str, guard: LaunchRecorder
    ) -> None:
        host = shutil.which(tool) or f"/usr/bin/{tool}"

        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run([host], check=False)
        assert [v.name for v in guard.violations()] == [tool]

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_shell_string_starting_with_the_tool_is_blocked(
        self, tool: str, guard: LaunchRecorder
    ) -> None:
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run(f"{tool} --version", **_SHELL, check=False)
        assert len(guard.violations()) == 1

    def test_shell_string_with_the_tool_later_is_allowed(
        self, guard: LaunchRecorder
    ) -> None:
        result = subprocess.run("echo gh", **_SHELL, capture_output=True, check=False)

        assert result.stdout == b"gh\n"
        assert guard.violations() == []

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_executable_keyword_is_blocked(
        self, tool: str, guard: LaunchRecorder
    ) -> None:
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run(["innocent"], executable=f"/usr/bin/{tool}", check=False)
        assert len(guard.violations()) == 1

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    def test_os_system_is_blocked(self, tool: str, guard: LaunchRecorder) -> None:
        with pytest.raises(ToolLaunchBlockedError):
            _call_os("system", f"{tool} pr list")
        assert [v.name for v in guard.violations()] == [tool]

    def test_os_system_allows_other_commands(self, guard: LaunchRecorder) -> None:
        assert _call_os("system", "true") == 0
        assert guard.violations() == []


def _call_os(name: str, *args: object, **kwargs: object) -> object:
    """Call ``os.<name>`` as currently bound (i.e. through any installed guard)."""
    function: Callable[..., object] = getattr(os, name)
    return function(*args, **kwargs)


# (os function, positional args before argv-or-command, takes an env arg)
_EXEC_FORMS: list[tuple[str, Callable[[str], tuple[object, ...]]]] = [
    ("execv", lambda tool: (f"/usr/bin/{tool}", [tool])),
    ("execve", lambda tool: (f"/usr/bin/{tool}", [tool], {})),
    ("execvp", lambda tool: (tool, [tool])),
    ("execvpe", lambda tool: (tool, [tool], {"PATH": "/usr/bin"})),
    ("spawnv", lambda tool: (os.P_WAIT, f"/usr/bin/{tool}", [tool])),
    ("spawnve", lambda tool: (os.P_WAIT, f"/usr/bin/{tool}", [tool], {})),
    ("spawnvp", lambda tool: (os.P_WAIT, tool, [tool])),
    ("spawnvpe", lambda tool: (os.P_WAIT, tool, [tool], {"PATH": "/usr/bin"})),
    ("posix_spawn", lambda tool: (f"/usr/bin/{tool}", [tool], {})),
    ("posix_spawnp", lambda tool: (tool, [tool], {"PATH": "/usr/bin"})),
]


class TestOsLaunchForms:
    """``os.exec*``/``os.spawn*``/``os.posix_spawn*`` raise before launching."""

    @staticmethod
    def _stand_in(
        monkeypatch: pytest.MonkeyPatch, name: str
    ) -> list[tuple[object, ...]]:
        reached: list[tuple[object, ...]] = []

        def _record(*args: object, **_kwargs: object) -> int:
            reached.append(args)
            return 0

        monkeypatch.setattr(os, name, _record)
        return reached

    @pytest.mark.parametrize("tool", sorted(GUARDED_TOOLS))
    @pytest.mark.parametrize(
        ("name", "build"), _EXEC_FORMS, ids=[f[0] for f in _EXEC_FORMS]
    )
    def test_os_launch_of_a_guarded_tool_raises_before_launching(
        self,
        name: str,
        build: Callable[[str], tuple[object, ...]],
        tool: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorder: LaunchRecorder,
    ) -> None:
        reached = self._stand_in(monkeypatch, name)
        install_binary_launch_guard(
            monkeypatch, GUARDED_TOOLS, _NO_FORCED, root=tmp_path, recorder=recorder
        )

        with pytest.raises(ToolLaunchBlockedError):
            _call_os(name, *build(tool))

        assert reached == []
        assert [v.name for v in recorder.violations()] == [tool]

    @pytest.mark.parametrize(
        ("name", "build"), _EXEC_FORMS, ids=[f[0] for f in _EXEC_FORMS]
    )
    def test_os_launch_of_another_program_is_forwarded(
        self,
        name: str,
        build: Callable[[str], tuple[object, ...]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorder: LaunchRecorder,
    ) -> None:
        reached = self._stand_in(monkeypatch, name)
        install_binary_launch_guard(
            monkeypatch, GUARDED_TOOLS, _NO_FORCED, root=tmp_path, recorder=recorder
        )

        _call_os(name, *build("true"))

        assert reached == [build("true")]
        assert recorder.violations() == []

    def test_blocked_run_records_exactly_one_violation(
        self, guard: LaunchRecorder
    ) -> None:
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run(["gh"], check=False)
        with pytest.raises(ToolLaunchBlockedError):
            subprocess.run(["gh"], close_fds=False, check=False)

        assert len(guard.violations()) == 2

    def test_allowed_posix_spawn_forwards_every_kwarg_and_runs(
        self, guard: LaunchRecorder
    ) -> None:
        pid = os.posix_spawn(
            sys.executable,
            [
                sys.executable,
                "-c",
                "import os; raise SystemExit(os.getpgid(0) == os.getpid())",
            ],
            os.environ,
            setpgroup=0,
        )
        _pid, status = os.waitpid(pid, 0)

        assert os.waitstatus_to_exitcode(status) == 1
        assert guard.violations() == []

    @pytest.mark.raw_git_allowed  # git is the non-guarded control launch
    def test_allowed_subprocess_launches_still_run(self, guard: LaunchRecorder) -> None:
        interpreter = subprocess.run([sys.executable, "-c", "pass"], check=False)
        git = subprocess.run(["git", "--version"], capture_output=True, check=False)
        unrelated = subprocess.run(["true"], close_fds=False, check=False)

        assert (interpreter.returncode, git.returncode, unrelated.returncode) == (
            0,
            0,
            0,
        )
        assert guard.violations() == []


class TestAllowedAndForcedPresent:
    def test_a_stub_inside_the_root_runs_by_bare_name_and_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: LaunchRecorder
    ) -> None:
        bin_dir = write_bin_stub(tmp_path, "gh", _SCRIPT)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        assert subprocess.run(["gh"], check=False).returncode == 0
        assert subprocess.run([str(bin_dir / "gh")], check=False).returncode == 0
        assert guard.violations() == []

    def test_forced_present_tool_must_still_be_mocked(
        self,
        tmp_path: Path,
        host_bin: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorder: LaunchRecorder,
    ) -> None:
        """``binary_on_path`` forces ``shutil.which`` only; the launch is refused."""
        monkeypatch.setenv("PATH", str(host_bin))
        install_binary_launch_guard(
            monkeypatch,
            GUARDED_TOOLS,
            frozenset({"gh"}),
            root=tmp_path,
            recorder=recorder,
        )

        with pytest.raises(ToolLaunchBlockedError) as excinfo:
            subprocess.run(["gh", "auth", "status"], check=False)

        assert "binary_on_path" in str(excinfo.value)
        assert "mock the launch" in str(excinfo.value)

    def test_guard_chains_onto_the_previous_popen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: LaunchRecorder
    ) -> None:
        seen: list[object] = []
        real_init = subprocess.Popen.__init__

        def _outer(
            self: subprocess.Popen[bytes], args: object, *rest: object, **kw: object
        ) -> None:
            seen.append(args)
            real_init(self, args, *rest, **kw)

        monkeypatch.setattr(subprocess.Popen, "__init__", _outer)
        install_binary_launch_guard(
            monkeypatch, _DENY, _NO_FORCED, root=tmp_path, recorder=recorder
        )

        subprocess.run(["true"], check=False)

        assert seen == [["true"]]


class TestFinish:
    """The teardown check fails on any recorded, unexpected violation."""

    @staticmethod
    def _blocked(recorder: LaunchRecorder, tool: str) -> None:
        recorder.record_tool(tool, f"launch of {tool!r} blocked")

    def test_clean_recorder_passes(self, recorder: LaunchRecorder) -> None:
        finish(recorder, _node())

    def test_swallowed_block_fails_the_test(self, recorder: LaunchRecorder) -> None:
        """Code under test caught the OSError; the record still fails teardown."""
        self._blocked(recorder, "gh")

        with pytest.raises(AssertionError, match="launch of 'gh' blocked"):
            finish(recorder, _node())

    def test_tool_launch_expected_exempts_named_tools(
        self, recorder: LaunchRecorder
    ) -> None:
        self._blocked(recorder, "codex")
        self._blocked(recorder, "gh")

        finish(recorder, _node(pytest.mark.tool_launch_expected("codex", "gh")))
        with pytest.raises(AssertionError, match="'gh'"):
            finish(recorder, _node(pytest.mark.tool_launch_expected("codex")))

    def test_bare_tool_launch_expected_exempts_every_tool(
        self, recorder: LaunchRecorder
    ) -> None:
        self._blocked(recorder, "opencode")

        finish(recorder, _node(pytest.mark.tool_launch_expected))

    def test_raw_git_records_need_raw_git_allowed(
        self, recorder: LaunchRecorder
    ) -> None:
        recorder.record_git("raw git launch from test code")

        with pytest.raises(AssertionError, match="raw git"):
            finish(recorder, _node(pytest.mark.tool_launch_expected))
        finish(recorder, _node(pytest.mark.raw_git_allowed))


class TestEmptyProcessTable:
    """``empty_process_table`` is a drop-in for ``psutil.process_iter``."""

    def test_yields_nothing(self) -> None:
        assert list(empty_process_table()) == []

    def test_accepts_attrs_and_ad_value(self) -> None:
        assert list(empty_process_table(["name", "cwd"])) == []
        assert list(empty_process_table(["name", "cwd"], "denied")) == []


class TestScopedProcessIter:
    """``scoped_process_iter`` keeps real kernel entries for owned pids only.

    The autouse ``_isolate_process_table`` fixture has already replaced
    ``psutil.process_iter`` with the empty table, so every positive assertion
    here also proves the helper reads through the real iterator captured at
    import time rather than whatever ``psutil.process_iter`` currently is.
    """

    def test_yields_exactly_the_owned_pid_with_real_attributes(self) -> None:
        scoped = scoped_process_iter({os.getpid()})

        found = list(scoped(["name", "cwd"]))

        assert [process.pid for process in found] == [os.getpid()]
        assert found[0].info["name"] == psutil.Process().name()

    def test_a_pid_absent_from_the_table_is_simply_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stub table keeps the host's live PIDs out of the test: guessing an
        # unused PID (e.g. ``max(pids) + 1``) races the OS handing it to a new
        # process between the snapshot and the check (#2780).
        def _table(
            _attrs: object = None, _ad_value: object = None
        ) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(pid=101, info={"name": "alpha"}),
                SimpleNamespace(pid=202, info={"name": "beta"}),
            ]

        monkeypatch.setattr("tests._host_probe_guard.REAL_PROCESS_ITER", _table)

        assert list(scoped_process_iter({303})(["name"])) == []
        # Positive control: the stub is wired in, so the empty result above
        # comes from the filter and not from an empty table.
        assert [p.pid for p in scoped_process_iter({101})(["name"])] == [101]

    def test_an_empty_scope_yields_nothing(self) -> None:
        assert list(scoped_process_iter(set())(["name"])) == []
