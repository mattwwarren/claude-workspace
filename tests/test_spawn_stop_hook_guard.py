"""Tests for the shell guard wrapped around the injected Stop hook (#2226).

Scope: ``_stop_hook_command`` and the behavior of its output under a real
POSIX ``sh``. The ``_write_hook_context`` round-trip (both origins) and the
"context file written alongside the hook" assertion live in
``tests/test_spawn.py::TestWriteHookContext``, which asserts the written
command bakes in the worktree's absolute context path; they are not
duplicated here.

The guard is one existence test on the **absolute** path of the worktree's
``cw-context.json`` — the file the same code writes — in front of
``cw signal-stop``. It reads no environment variable and no ambient cwd, so a
dispatch worker whose cwd has moved (e.g. into a detached gate worktree) can
never be skipped. No test here depends on ``CLAUDE_PROJECT_DIR`` being set.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.spawn import _stop_hook_command

# Representative Stop-hook payload; the guard must pass it through untouched.
_PAYLOAD = '{"cwd":"/nonexistent","hook_event_name":"Stop"}'

# Sentinel exit code for the "fake cw failed" case — proves the shell's exit
# status is the invoked command's, not the guard's.
_FAKE_CW_EXIT = 3


@dataclass(frozen=True)
class _GuardRun:
    """Outcome of running a Stop hook command under a real ``sh``."""

    returncode: int
    invoked: bool
    marker_text: str


def _write_fake_cw(bin_dir: Path, marker: Path, *, exit_code: int) -> None:
    """Install a fake ``cw`` on PATH that records its args and stdin."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "cw"
    script.write_text(
        "#!/bin/sh\n"
        f"printf 'args=%s\\n' \"$*\" >> '{marker}'\n"
        f"printf 'stdin=%s\\n' \"$(cat)\" >> '{marker}'\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _place_context(worktree: Path) -> Path:
    """Create a bare ``.claude/cw-context.json`` under *worktree*.

    The guard only tests file existence, so a literal ``{}`` is enough; going
    through the real writer (``tests/conftest.py::_write_hook_context_file``)
    would also write ``settings.local.json`` and blur what this file isolates.
    """
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text("{}", encoding="utf-8")
    return context_path.resolve()


def _run_command(
    tmp_path: Path,
    command: str,
    *,
    hook_cwd: Path,
    extra_env: dict[str, str] | None = None,
    fake_cw_exit: int = 0,
) -> _GuardRun:
    """Run *command* under ``sh -c`` in *hook_cwd* against a fake ``cw``.

    The environment is only ``PATH`` (plus *extra_env*): nothing ambient
    leaks in, so a guard that secretly read an env var would show up as a
    failure of the "no env var" cases.
    """
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "marker.txt"
    _write_fake_cw(bin_dir, marker, exit_code=fake_cw_exit)
    hook_cwd.mkdir(parents=True, exist_ok=True)

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", **(extra_env or {})}
    proc = subprocess.run(
        ["sh", "-c", command],
        input=_PAYLOAD,
        capture_output=True,
        text=True,
        cwd=hook_cwd,
        env=env,
        check=False,
    )
    return _GuardRun(
        returncode=proc.returncode,
        invoked=marker.exists(),
        marker_text=marker.read_text(encoding="utf-8") if marker.exists() else "",
    )


class TestStopHookCommandString:
    """The generated command's shape."""

    def test_command_is_one_absolute_existence_guard(self, tmp_path: Path) -> None:
        """One absolute `[ -f ... ] || exit 0` in front of `cw signal-stop`."""
        context_path = _place_context(tmp_path / "wt")

        command = _stop_hook_command(context_path)

        assert command == f"[ -f {context_path} ] || exit 0; cw signal-stop"
        assert command.count("[ -f ") == 1
        assert Path(str(context_path)).is_absolute()

    def test_command_reads_no_environment_variable_or_relative_path(
        self, tmp_path: Path
    ) -> None:
        """No `$VAR` and no bare relative `.claude/...` test survives.

        The earlier CLAUDE_PROJECT_DIR / hook-cwd arms were both ambient and
        could skip a dispatch worker; the absolute path is the only test.
        """
        command = _stop_hook_command(_place_context(tmp_path / "wt"))

        assert "$" not in command
        assert "CLAUDE_PROJECT_DIR" not in command
        assert "[ -f .claude" not in command

    def test_command_ends_in_signal_stop_with_exit_zero_short_circuit(
        self, tmp_path: Path
    ) -> None:
        """Skip path is `exit 0` before the interpreter; the call is unchanged."""
        command = _stop_hook_command(_place_context(tmp_path / "wt"))

        assert "|| exit 0; cw signal-stop" in command
        assert command.endswith("cw signal-stop")

    def test_guarded_path_is_the_context_path_argument(self, tmp_path: Path) -> None:
        """The guard tests exactly the path it is given, with the constant's tail."""
        context_path = _place_context(tmp_path / "wt")

        command = _stop_hook_command(context_path)

        assert str(context_path) in command
        assert command.count(HOOK_CONTEXT_RELATIVE_PATH.as_posix()) == 1

    def test_hostile_path_stays_one_shell_word(self, tmp_path: Path) -> None:
        """Spaces, quotes and `$` in the worktree path are quoted, not expanded."""
        worktree = tmp_path / "my wt's $HOME `x`"
        context_path = _place_context(worktree)

        command = _stop_hook_command(context_path)
        run = _run_command(tmp_path, command, hook_cwd=tmp_path / "elsewhere")

        assert run.invoked is True
        assert run.returncode == 0


class TestStopHookGuardExecution:
    """Real ``sh`` execution of the guard against a fake ``cw`` on PATH."""

    def test_context_present_invokes_cw_with_stdin(self, tmp_path: Path) -> None:
        """Context exists → cw runs and the hook payload passes through."""
        command = _stop_hook_command(_place_context(tmp_path / "wt"))

        run = _run_command(tmp_path, command, hook_cwd=tmp_path / "hookcwd")

        assert run.invoked is True
        assert run.returncode == 0
        assert "args=signal-stop" in run.marker_text
        assert f"stdin={_PAYLOAD}" in run.marker_text

    def test_cwd_elsewhere_still_invokes_cw(self, tmp_path: Path) -> None:
        """Hook cwd in a detached gate worktree (no context there) → cw runs.

        The review-round-1 finding: Step 2.5 runs gates inside `$TMPWT`, whose
        cwd holds no `cw-context.json`. The absolute path exists regardless.
        """
        command = _stop_hook_command(_place_context(tmp_path / "wt"))
        gate_wt = tmp_path / "gate-wt"
        assert not (gate_wt / HOOK_CONTEXT_RELATIVE_PATH).exists()

        run = _run_command(tmp_path, command, hook_cwd=gate_wt)

        assert run.invoked is True
        assert run.returncode == 0

    def test_wrong_project_dir_and_wrong_cwd_still_invokes_cw(
        self, tmp_path: Path
    ) -> None:
        """A CLAUDE_PROJECT_DIR pointing at an unrelated dir cannot skip cw.

        Regression pin for the completion-signal loss the ambient guard had:
        the variable is set to a non-cw directory and the cwd lacks the file.
        """
        command = _stop_hook_command(_place_context(tmp_path / "wt"))
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()

        run = _run_command(
            tmp_path,
            command,
            hook_cwd=tmp_path / "gate-wt",
            extra_env={"CLAUDE_PROJECT_DIR": str(unrelated)},
        )

        assert run.invoked is True

    def test_context_absent_exits_zero_without_invoking_cw(
        self, tmp_path: Path
    ) -> None:
        """No context file at the baked path → rc 0, the interpreter never starts."""
        context_path = tmp_path / "wt" / HOOK_CONTEXT_RELATIVE_PATH
        command = _stop_hook_command(context_path)

        run = _run_command(tmp_path, command, hook_cwd=tmp_path / "hookcwd")

        assert run.invoked is False
        assert run.returncode == 0
        assert run.marker_text == ""

    def test_cwd_context_does_not_rescue_an_absent_baked_path(
        self, tmp_path: Path
    ) -> None:
        """The hook is bound to its own worktree, not to whatever cwd holds.

        A context file in the hook's cwd belongs to some other session; a
        hook baked for a worktree whose context is gone must not fire on it.
        """
        command = _stop_hook_command(tmp_path / "gone" / HOOK_CONTEXT_RELATIVE_PATH)
        other = tmp_path / "other"
        _place_context(other)

        run = _run_command(tmp_path, command, hook_cwd=other)

        assert run.invoked is False
        assert run.returncode == 0

    @pytest.mark.parametrize("project_dir", [None, "", "/nonexistent"])
    def test_result_is_independent_of_claude_project_dir(
        self, tmp_path: Path, project_dir: str | None
    ) -> None:
        """Unset, empty or bogus CLAUDE_PROJECT_DIR all give the same outcome."""
        command = _stop_hook_command(_place_context(tmp_path / "wt"))
        env = {} if project_dir is None else {"CLAUDE_PROJECT_DIR": project_dir}

        run = _run_command(
            tmp_path, command, hook_cwd=tmp_path / "hookcwd", extra_env=env
        )

        assert run.invoked is True

    def test_exit_status_passes_through(self, tmp_path: Path) -> None:
        """The invoked command's exit status is the shell's exit status."""
        command = _stop_hook_command(_place_context(tmp_path / "wt"))

        run = _run_command(
            tmp_path,
            command,
            hook_cwd=tmp_path / "hookcwd",
            fake_cw_exit=_FAKE_CW_EXIT,
        )

        assert run.invoked is True
        assert run.returncode == _FAKE_CW_EXIT
