"""Tests for the shell guard wrapped around the injected Stop hook (#2226).

Scope: the ``STOP_HOOK_COMMAND`` string and its behavior under a real POSIX
``sh``. The ``_write_hook_context`` round-trip (both origins) and the
"context file written alongside the hook" assertion already live in
``tests/test_spawn.py::TestWriteHookContext``; those tests compare against
``STOP_HOOK_COMMAND`` and are not duplicated here.

The guard skips ``cw signal-stop`` — and therefore the whole Python
interpreter start it costs — only when no ``.claude/cw-context.json`` is
reachable from either ``$CLAUDE_PROJECT_DIR`` or the hook process's own cwd.
It fails open when the variable is unset or empty.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from cw.models import HOOK_CONTEXT_RELATIVE_PATH

# Representative Stop-hook payload; the guard must pass it through untouched.
_PAYLOAD = '{"cwd":"/nonexistent","hook_event_name":"Stop"}'

# Sentinel exit code for the "fake cw failed" case — proves the shell's exit
# status is the invoked command's, not the guard's.
_FAKE_CW_EXIT = 3


@dataclass(frozen=True)
class _GuardRun:
    """Outcome of running STOP_HOOK_COMMAND under a real ``sh``."""

    returncode: int
    invoked: bool
    marker_text: str


def _write_fake_cw(bin_dir: Path, marker: Path, *, exit_code: int) -> None:
    """Install a fake ``cw`` on PATH that records its args and stdin."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "cw"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "args=%s\\n" "$*" >> "{marker}"\n'
        f'printf "stdin=%s\\n" "$(cat)" >> "{marker}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _place_context(root: Path) -> None:
    """Create a bare ``.claude/cw-context.json`` under *root*.

    The guard only tests file existence, so a literal ``{}`` is enough; going
    through the real writer (``tests/conftest.py::_write_hook_context_file``)
    would also write ``settings.local.json`` and blur what this file isolates.
    """
    context_path = root / HOOK_CONTEXT_RELATIVE_PATH
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text("{}", encoding="utf-8")


def _run_guard(
    tmp_path: Path,
    *,
    project_env: str | None,
    project_has_context: bool = False,
    cwd_has_context: bool = False,
    fake_cw_exit: int = 0,
) -> _GuardRun:
    """Run STOP_HOOK_COMMAND under ``sh`` with a synthetic hook environment.

    *project_env* is ``None`` to leave ``CLAUDE_PROJECT_DIR`` unset, ``""`` to
    export it empty, or a directory name created under *tmp_path* and exported
    as its absolute path. The hook process always runs in ``tmp_path/hookcwd``.
    """
    from cw.spawn import STOP_HOOK_COMMAND

    bin_dir = tmp_path / "bin"
    marker = tmp_path / "marker.txt"
    _write_fake_cw(bin_dir, marker, exit_code=fake_cw_exit)

    hook_cwd = tmp_path / "hookcwd"
    hook_cwd.mkdir(parents=True, exist_ok=True)
    if cwd_has_context:
        _place_context(hook_cwd)

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if project_env is not None:
        if project_env:
            project_dir = tmp_path / project_env
            project_dir.mkdir(parents=True, exist_ok=True)
            if project_has_context:
                _place_context(project_dir)
            env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        else:
            env["CLAUDE_PROJECT_DIR"] = ""

    proc = subprocess.run(
        ["sh", "-c", STOP_HOOK_COMMAND],
        input=_PAYLOAD,
        capture_output=True,
        text=True,
        cwd=hook_cwd,
        env=env,
        check=False,
    )
    marker_text = marker.read_text(encoding="utf-8") if marker.exists() else ""
    return _GuardRun(
        returncode=proc.returncode, invoked=marker.exists(), marker_text=marker_text
    )


class TestStopHookCommandString:
    """The constant itself, and its agreement with the settings template."""

    def test_stop_command_is_guarded_and_fail_open(self) -> None:
        """Guard keys off CLAUDE_PROJECT_DIR, fails open, ends in signal-stop."""
        from cw.spawn import STOP_HOOK_COMMAND

        relpath = HOOK_CONTEXT_RELATIVE_PATH.as_posix()

        assert "CLAUDE_PROJECT_DIR" in STOP_HOOK_COMMAND
        # Fail-open first arm: an unset/empty variable short-circuits the
        # chain so `cw signal-stop` runs exactly as it did before #2226.
        assert '[ -z "$CLAUDE_PROJECT_DIR" ]' in STOP_HOOK_COMMAND
        # Exactly two existence tests: one under $CLAUDE_PROJECT_DIR, one
        # relative to the hook process's own cwd.
        assert STOP_HOOK_COMMAND.count(relpath) == 2
        assert f'[ -f "$CLAUDE_PROJECT_DIR/{relpath}" ]' in STOP_HOOK_COMMAND
        assert f"[ -f {relpath} ]" in STOP_HOOK_COMMAND
        assert "exit 0" in STOP_HOOK_COMMAND
        assert STOP_HOOK_COMMAND.endswith("cw signal-stop")

    def test_template_stop_command_is_the_constant(self) -> None:
        """The settings template's Stop command IS STOP_HOOK_COMMAND."""
        from cw.spawn import _HOOK_SETTINGS_TEMPLATE, STOP_HOOK_COMMAND

        stop_entries = _HOOK_SETTINGS_TEMPLATE["hooks"]["Stop"]
        assert any(
            entry["hooks"][0]["command"] == STOP_HOOK_COMMAND for entry in stop_entries
        )

    def test_guard_path_matches_hook_context_relative_path(self) -> None:
        """The guarded path is derived from HOOK_CONTEXT_RELATIVE_PATH.

        Pins the two constants together so the guard can never test a path
        the writer does not write.
        """
        from cw.spawn import STOP_HOOK_COMMAND

        assert HOOK_CONTEXT_RELATIVE_PATH.as_posix() == ".claude/cw-context.json"
        assert HOOK_CONTEXT_RELATIVE_PATH.as_posix() in STOP_HOOK_COMMAND


class TestStopHookGuardExecution:
    """Real ``sh`` execution of the guard against a fake ``cw`` on PATH."""

    def test_project_dir_has_context_invokes_cw_with_stdin(
        self, tmp_path: Path
    ) -> None:
        """Context under $CLAUDE_PROJECT_DIR → cw runs, stdin passes through."""
        run = _run_guard(tmp_path, project_env="project", project_has_context=True)

        assert run.invoked is True
        assert run.returncode == 0
        assert "args=signal-stop" in run.marker_text
        assert f"stdin={_PAYLOAD}" in run.marker_text

    def test_unset_variable_fails_open(self, tmp_path: Path) -> None:
        """CLAUDE_PROJECT_DIR unset and no context anywhere → cw still runs."""
        run = _run_guard(tmp_path, project_env=None)

        assert run.invoked is True
        assert run.returncode == 0

    def test_empty_variable_fails_open(self, tmp_path: Path) -> None:
        """CLAUDE_PROJECT_DIR="" and no context anywhere → cw still runs."""
        run = _run_guard(tmp_path, project_env="")

        assert run.invoked is True
        assert run.returncode == 0

    def test_wrong_project_dir_but_cwd_has_context_invokes_cw(
        self, tmp_path: Path
    ) -> None:
        """Variable points elsewhere but the hook cwd holds the context → cw runs.

        This is the hardening arm: the guard must not fail closed just because
        the harness set CLAUDE_PROJECT_DIR to a directory cw does not own.
        """
        run = _run_guard(tmp_path, project_env="elsewhere", cwd_has_context=True)

        assert run.invoked is True
        assert run.returncode == 0

    def test_wrong_project_dir_and_bare_cwd_skips(self, tmp_path: Path) -> None:
        """Variable points elsewhere and the cwd lacks the file → cw is skipped.

        The documented residual: this is also the only combination in which a
        wrong-but-set variable can drop a completion signal.
        """
        run = _run_guard(tmp_path, project_env="elsewhere")

        assert run.invoked is False
        assert run.returncode == 0

    def test_unset_variable_with_cwd_context_invokes_cw(self, tmp_path: Path) -> None:
        """Variable unset but the hook cwd holds the context → cw runs."""
        run = _run_guard(tmp_path, project_env=None, cwd_has_context=True)

        assert run.invoked is True
        assert run.returncode == 0

    def test_project_dir_with_space_is_quoted(self, tmp_path: Path) -> None:
        """A project dir containing a space still resolves (quoting holds)."""
        run = _run_guard(tmp_path, project_env="my project", project_has_context=True)

        assert run.invoked is True
        assert run.returncode == 0

    def test_no_context_anywhere_skips_without_interpreter_start(
        self, tmp_path: Path
    ) -> None:
        """No context under the project dir or the cwd → rc 0, cw never runs."""
        run = _run_guard(tmp_path, project_env="project")

        assert run.invoked is False
        assert run.returncode == 0
        assert run.marker_text == ""

    def test_exit_status_passes_through(self, tmp_path: Path) -> None:
        """The invoked command's exit status is the shell's exit status."""
        run = _run_guard(
            tmp_path,
            project_env="project",
            project_has_context=True,
            fake_cw_exit=_FAKE_CW_EXIT,
        )

        assert run.invoked is True
        assert run.returncode == _FAKE_CW_EXIT
