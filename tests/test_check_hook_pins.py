"""Tests for .claude/scripts/check_hook_pins.py (#2961).

The script asserts every mirrors-mypy `additional_dependencies` entry in
`.pre-commit-config.yaml` is an exact `==` pin equal to its `uv.lock` version.
Fixtures are string literals written to `tmp_path`; the real-repo tests read
the checked-in config and lock.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests._shared.guard_fences import load_guard_script_module, run_guard_script_cli

if TYPE_CHECKING:
    import types

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_hook_pins.py"
_REAL_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_REAL_LOCK = _REPO_ROOT / "uv.lock"
_INDENT = " " * 10

_mod = load_guard_script_module(_SCRIPT, "check_hook_pins")


def _config(*deps: str, mypy: bool = True) -> str:
    """A minimal `.pre-commit-config.yaml` with a mirrors-mypy hook."""
    if not mypy:
        return (
            "repos:\n"
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.15.14\n"
            "    hooks:\n"
            "      - id: ruff\n"
        )
    lines = [
        "repos:",
        "  - repo: https://github.com/pre-commit/mirrors-mypy",
        "    rev: v2.1.0",
        "    hooks:",
        "      - id: mypy",
        "        additional_dependencies:",
    ]
    lines.extend(f"          - {dep}" for dep in deps)
    return "\n".join(lines) + "\n"


def _lock(versions: dict[str, str]) -> str:
    """A minimal `uv.lock` with one `[[package]]` per name."""
    blocks = [
        f'[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in versions.items()
    ]
    return "version = 1\nrevision = 3\n\n" + "\n".join(blocks)


def _run(
    mod: types.ModuleType,
    tmp_path: Path,
    config_text: str,
    lock_text: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    config = tmp_path / ".pre-commit-config.yaml"
    lock = tmp_path / "uv.lock"
    config.write_text(config_text)
    lock.write_text(lock_text)
    code = mod.main(["--config", str(config), "--lock", str(lock)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_matching_pins_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(
        _mod, tmp_path, _config("click==8.3.1"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 0
    assert "OK" in out
    assert err == ""


def test_mismatch_is_caught_with_corrected_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("click==8.3.1"), _lock({"click": "8.5.0"}), capsys
    )
    assert code == 1
    assert "click" in out
    assert "8.3.1" in out
    assert "8.5.0" in out
    assert f"{_INDENT}- click==8.5.0" in out.splitlines()


def test_floor_specifier_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("click>=8.3.1"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[not_exact_pin]" in out
    assert f"{_INDENT}- click==8.3.1" in out.splitlines()


def test_unpinned_name_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("click"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[not_exact_pin]" in out
    assert f"{_INDENT}- click==8.3.1" in out.splitlines()


@pytest.mark.parametrize(
    "spec",
    [
        "click~=8.3",
        "click==8.*",
        "click===8.3.1",
        "click>=1,<2",
        'click==8.3.1; python_version>"3"',
    ],
)
def test_other_specifiers_flagged(
    spec: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config(spec), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[not_exact_pin]" in out


def test_unparseable_name_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("==8.3.1"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[not_exact_pin]" in out
    assert "cannot parse" in out


def test_missing_from_lock_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("ghost==1.0"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[missing_from_lock]" in out
    assert "ghost" in out
    assert "- ghost==" not in out


def test_unpinned_name_missing_from_lock_has_no_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config("ghost>=1.0"), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert "[not_exact_pin]" in out
    assert "ghost" in out
    assert "- ghost==" not in out


@pytest.mark.parametrize(
    ("dep", "lock_name", "version"),
    [
        ("ruamel.yaml==0.19.1", "ruamel-yaml", "0.19.1"),
        ("types-PyYAML==6.0.12.20250915", "types-pyyaml", "6.0.12.20250915"),
        ("Jinja2==3.1.6", "jinja2", "3.1.6"),
    ],
)
def test_name_normalization(
    dep: str,
    lock_name: str,
    version: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(
        _mod, tmp_path, _config(dep), _lock({lock_name: version}), capsys
    )
    assert code == 0, out


def test_corrected_line_preserves_hook_spelling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod,
        tmp_path,
        _config("ruamel.yaml==0.18.0"),
        _lock({"ruamel-yaml": "0.19.1"}),
        capsys,
    )
    assert code == 1
    assert f"{_INDENT}- ruamel.yaml==0.19.1" in out.splitlines()


def test_extras_syntax_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(
        _mod, tmp_path, _config("pkg[extra]==1.0"), _lock({"pkg": "1.0"}), capsys
    )
    assert code == 0

    code, out, _ = _run(
        _mod, tmp_path, _config("pkg[extra]==0.9"), _lock({"pkg": "1.0"}), capsys
    )
    assert code == 1
    assert f"{_INDENT}- pkg[extra]==1.0" in out.splitlines()


def test_multiple_lock_versions_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = (
        'version = 1\n\n[[package]]\nname = "click"\nversion = "8.3.1"\n\n'
        '[[package]]\nname = "click"\nversion = "8.5.0"\n'
    )
    code, out, err = _run(_mod, tmp_path, _config("click==8.3.1"), lock, capsys)
    assert code == 1
    assert out == ""
    assert "8.3.1" in err
    assert "8.5.0" in err


def test_lock_entry_without_version_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = (
        'version = 1\n\n[[package]]\nname = "cw"\nsource = { editable = "." }\n\n'
        '[[package]]\nname = "click"\nversion = "8.3.1"\n'
    )
    code, _, _ = _run(_mod, tmp_path, _config("click==8.3.1"), lock, capsys)
    assert code == 0


def test_multiple_violations_all_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        _mod,
        tmp_path,
        _config("click==8.3.1", "jinja2>=3.0", "ghost==1.0"),
        _lock({"click": "8.5.0", "jinja2": "3.1.6"}),
        capsys,
    )
    assert code == 1
    assert "3 violation(s)" in out
    assert out.index("[drift]") < out.index("[not_exact_pin]")
    assert out.index("[not_exact_pin]") < out.index("[missing_from_lock]")


def test_missing_mypy_hook_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(
        _mod, tmp_path, _config(mypy=False), _lock({"click": "8.3.1"}), capsys
    )
    assert code == 1
    assert out == ""
    assert "mirrors-mypy" in err


def test_mypy_hook_without_additional_dependencies_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = (
        "repos:\n"
        "  - repo: https://github.com/pre-commit/mirrors-mypy.git\n"
        "    rev: v2.1.0\n"
        "    hooks:\n"
        "      - id: mypy\n"
    )
    code, out, err = _run(_mod, tmp_path, config, _lock({"click": "8.3.1"}), capsys)
    assert code == 1
    assert out == ""
    assert "additional_dependencies" in err


_GOOD_LOCK = _lock({"click": "8.3.1"})
_GOOD_CONFIG = _config("click==8.3.1")


@pytest.mark.parametrize(
    ("config_text", "lock_text"),
    [
        pytest.param(None, _GOOD_LOCK, id="missing-config"),
        pytest.param(_GOOD_CONFIG, None, id="missing-lock"),
        pytest.param("repos: [\n", _GOOD_LOCK, id="invalid-yaml"),
        pytest.param("- a\n- b\n", _GOOD_LOCK, id="yaml-not-mapping"),
        pytest.param("repos: nope\n", _GOOD_LOCK, id="repos-not-list"),
        pytest.param(
            _config("click==8.3.1").replace("- click==8.3.1", "- 42"),
            _GOOD_LOCK,
            id="non-string-dependency",
        ),
        pytest.param(_GOOD_CONFIG, "not = [valid", id="invalid-toml"),
        pytest.param(_GOOD_CONFIG, "version = 1\n", id="lock-without-package"),
        pytest.param(_GOOD_CONFIG, "", id="empty-lock"),
    ],
)
def test_malformed_inputs_exit_nonzero(
    config_text: str | None,
    lock_text: str | None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / ".pre-commit-config.yaml"
    lock = tmp_path / "uv.lock"
    if config_text is not None:
        config.write_text(config_text)
    if lock_text is not None:
        lock.write_text(lock_text)
    code = _mod.main(["--config", str(config), "--lock", str(lock)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("check_hook_pins:")


def test_real_repo_pins_match_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    code = _mod.main([])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err


def test_real_repo_mutation_is_caught(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _REAL_LOCK.open("rb") as fh:
        packages = tomllib.load(fh)["package"]
    locked = next(p["version"] for p in packages if p["name"] == "click")
    original = _REAL_CONFIG.read_text()
    assert f"click=={locked}" in original
    mutated = original.replace(f"click=={locked}", "click==0.0.1")
    code, out, _ = _run(_mod, tmp_path, mutated, _REAL_LOCK.read_text(), capsys)
    assert code == 1
    assert f"{_INDENT}- click=={locked}" in out.splitlines()


def test_cli_entry_exit_code(tmp_path: Path) -> None:
    config = tmp_path / ".pre-commit-config.yaml"
    lock = tmp_path / "uv.lock"
    config.write_text(_config("click==8.3.1"))
    lock.write_text(_lock({"click": "8.5.0"}))
    result = run_guard_script_cli(
        _SCRIPT, ["--config", str(config), "--lock", str(lock)]
    )
    assert result.returncode == 1
    assert "click==8.5.0" in result.stdout
