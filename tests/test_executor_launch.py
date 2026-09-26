"""Tests for cw.executor_launch — shared logged fire-and-forget launcher (#2369)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from cw.executor_launch import _launch_logged_subprocess

_LOG_RELATIVE_PATH = Path(".cw", "tool.log")


def test_launch_logged_subprocess_writes_output_to_log(tmp_path: Path) -> None:
    """Child stdout+stderr land in the given worktree-relative log file."""
    proc = _launch_logged_subprocess(
        tmp_path,
        ["sh", "-c", "echo out; echo err >&2"],
        dict(os.environ),
        _LOG_RELATIVE_PATH,
    )
    proc.wait()

    content = (tmp_path / _LOG_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "out" in content
    assert "err" in content


def test_launch_logged_subprocess_truncates_on_repeat(tmp_path: Path) -> None:
    """A second launch into the same worktree truncates the prior run's log."""
    env = dict(os.environ)
    _launch_logged_subprocess(
        tmp_path, ["sh", "-c", "echo first"], env, _LOG_RELATIVE_PATH
    ).wait()
    _launch_logged_subprocess(
        tmp_path, ["sh", "-c", "echo second"], env, _LOG_RELATIVE_PATH
    ).wait()

    content = (tmp_path / _LOG_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "first" not in content
    assert "second" in content


def test_launch_logged_subprocess_passes_start_new_session(tmp_path: Path) -> None:
    """Popen receives start_new_session=True, cwd=worktree, and the given env."""
    env = {"PATH": "/usr/bin"}
    with patch("cw.executor_launch.subprocess.Popen") as mock_popen:
        _launch_logged_subprocess(tmp_path, ["true"], env, _LOG_RELATIVE_PATH)

    kwargs = mock_popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"] == env
    assert mock_popen.call_args.args == (["true"],)
