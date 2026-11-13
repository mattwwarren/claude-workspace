"""Tests for the ``cw tests-prepare ROOT`` command (#2179).

The command prints the ``tests.prepare`` command from ROOT's project config, or
nothing. Exit contract: 0 (value or absent), 1 (malformed or non-UTF-8, message
on stderr, empty stdout), 2 (ROOT missing, Click usage error).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner, Result

from cw.cli import main

_GODOT = "godot --headless --path game --import"


def _write_config(root: Path, body: bytes | str) -> None:
    cfg = root / ".claude" / "project-config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))


def _invoke(root: Path) -> Result:
    return CliRunner().invoke(main, ["tests-prepare", str(root)])


def test_absent_file_prints_nothing(tmp_path: Path) -> None:
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_absent_key_prints_nothing(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n  other: x\n")
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_present_prints_command_and_newline(tmp_path: Path) -> None:
    _write_config(tmp_path, f"tests:\n  prepare: '  {_GODOT}  '\n")
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stdout == _GODOT + "\n"


def test_blank_prints_nothing(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n  prepare: '   '\n")
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_malformed_value_exits_1_on_stderr(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n  prepare: 7\n")
    result = _invoke(tmp_path)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "tests.prepare" in result.stderr


def test_invalid_utf8_exits_1(tmp_path: Path) -> None:
    _write_config(tmp_path, b"tests:\n  prepare: \xff\n")
    result = _invoke(tmp_path)
    assert result.exit_code == 1
    assert result.stdout == ""


def test_nonexistent_root_exits_2(tmp_path: Path) -> None:
    result = _invoke(tmp_path / "missing")
    assert result.exit_code == 2


def test_help_lists_command_and_absent_contract() -> None:
    top = CliRunner().invoke(main, ["--help"])
    assert "tests-prepare" in top.output
    sub = CliRunner().invoke(main, ["tests-prepare", "--help"])
    assert sub.exit_code == 0
    assert "nothing when it is unset" in sub.output
