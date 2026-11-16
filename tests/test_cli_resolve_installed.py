"""CLI tests for ``cw resolve-installed`` (#3662)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

import cw.installed_resolve as resolver_mod
from cw.cli import main
from cw.installed_resolve import DEFAULT_WAIT_S, WAIT_ENV_VAR


def _invoke(args: list[str], env: dict[str, str] | None = None) -> Result:
    return CliRunner().invoke(main, ["resolve-installed", *args], env=env)


def test_hit_prints_only_the_path(tmp_path: Path) -> None:
    f = tmp_path / "gate_key.py"
    f.write_text("print()\n", encoding="utf-8")
    result = _invoke(["--wait-s", "0", str(f)])
    assert result.exit_code == 0
    assert result.stdout == f"{f}\n"
    assert result.stderr == ""


def test_recovered_hit_prints_path_and_recovery_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "gate_key.py"

    def create(_seconds: float) -> None:
        target.write_text("print()\n", encoding="utf-8")

    monkeypatch.setattr(resolver_mod.time, "sleep", create)
    result = _invoke(["--wait-s", "5", str(target)])
    assert result.exit_code == 0
    assert result.stdout == f"{target}\n"
    assert "recovered" in result.stderr
    assert "first miss" in result.stderr


def test_miss_exits_1_empty_stdout_and_names_candidates(tmp_path: Path) -> None:
    a = tmp_path / "a" / "gate_key.py"
    b = tmp_path / "b" / "gate_key.py"
    result = _invoke(["--wait-s", "0", str(a), str(b)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert str(a) in result.output
    assert str(b) in result.output
    assert "absent" in result.output


def test_miss_reports_dangling_target_and_in_flight_marker(tmp_path: Path) -> None:
    git_dir = tmp_path / "checkout" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("", encoding="utf-8")
    link = tmp_path / "home" / "gate_key.py"
    link.parent.mkdir(parents=True)
    target = tmp_path / "checkout" / "scripts" / "gate_key.py"
    Path(link).symlink_to(target)
    result = _invoke(["--wait-s", "0", str(link)])
    assert result.exit_code == 1
    assert f"dangling -> {target}" in result.output
    assert "index.lock" in result.output


def test_env_var_zero_gives_single_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(resolver_mod.time, "sleep", slept.append)
    result = _invoke([str(tmp_path / "never.py")], env={WAIT_ENV_VAR: "0"})
    assert result.exit_code == 1
    assert slept == []
    assert "1 attempt(s)" in result.output


def test_wait_zero_flag_gives_single_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(resolver_mod.time, "sleep", slept.append)
    result = _invoke(["--wait-s", "0", str(tmp_path / "never.py")])
    assert result.exit_code == 1
    assert slept == []


def test_no_arguments_exits_2() -> None:
    result = CliRunner().invoke(main, ["resolve-installed"])
    assert result.exit_code == 2


def test_negative_wait_exits_2(tmp_path: Path) -> None:
    result = _invoke(["--wait-s", "-1", str(tmp_path / "x.py")])
    assert result.exit_code == 2


def test_wait_default_is_the_module_default() -> None:
    from cw.cli.resolve_installed import resolve_installed

    wait_param = next(p for p in resolve_installed.params if p.name == "wait_s")
    assert wait_param.default == DEFAULT_WAIT_S
