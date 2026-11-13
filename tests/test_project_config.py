"""Tests for ``read_tests_prepare`` (#2179): the typed ``tests.prepare`` reader.

The reader wraps the one shared loader, ``load_project_config_dict``, so these
tests write real ``.claude/project-config.yaml`` files under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.project_config import PrepareConfigError, read_tests_prepare

_GODOT = "godot --headless --path game --import"


def _write_config(root: Path, body: bytes | str) -> Path:
    cfg = root / ".claude" / "project-config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return root


def test_absent_file_returns_none(tmp_path: Path) -> None:
    assert read_tests_prepare(tmp_path) is None


def test_no_tests_block_returns_none(tmp_path: Path) -> None:
    _write_config(tmp_path, "quality_gates: {}\n")
    assert read_tests_prepare(tmp_path) is None


def test_tests_block_null_returns_none(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n")
    assert read_tests_prepare(tmp_path) is None


def test_prepare_absent_in_tests_block_returns_none(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n  other: value\n")
    assert read_tests_prepare(tmp_path) is None


def test_prepare_string_returned_stripped(tmp_path: Path) -> None:
    _write_config(tmp_path, f"tests:\n  prepare: '  {_GODOT}  '\n")
    assert read_tests_prepare(tmp_path) == _GODOT


def test_multiline_block_scalar_preserved(tmp_path: Path) -> None:
    _write_config(tmp_path, "tests:\n  prepare: |\n    echo one\n    echo two\n")
    assert read_tests_prepare(tmp_path) == "echo one\necho two"


@pytest.mark.parametrize("value", ['""', "'   '"])
def test_blank_prepare_returns_none(tmp_path: Path, value: str) -> None:
    _write_config(tmp_path, f"tests:\n  prepare: {value}\n")
    assert read_tests_prepare(tmp_path) is None


@pytest.mark.parametrize(
    "value",
    ["[a]", "7", "true", "{a: 1}"],
    ids=["list", "int", "bool", "mapping"],
)
def test_malformed_prepare_raises(tmp_path: Path, value: str) -> None:
    _write_config(tmp_path, f"tests:\n  prepare: {value}\n")
    with pytest.raises(PrepareConfigError, match=r"tests\.prepare"):
        read_tests_prepare(tmp_path)


@pytest.mark.parametrize("body", ["tests: [a]\n", "tests: x\n"])
def test_tests_not_a_mapping_raises(tmp_path: Path, body: str) -> None:
    _write_config(tmp_path, body)
    with pytest.raises(PrepareConfigError, match="must be a mapping"):
        read_tests_prepare(tmp_path)


def test_invalid_utf8_file_raises_prepare_config_error(tmp_path: Path) -> None:
    _write_config(tmp_path, b"tests:\n  prepare: \xff\n")
    with pytest.raises(PrepareConfigError, match=r"UTF-8"):
        read_tests_prepare(tmp_path)


def test_unparseable_yaml_reads_as_absent(tmp_path: Path) -> None:
    # Pins the shared loader's behaviour: a YAML syntax error is "absent".
    _write_config(tmp_path, "tests: [\n")
    assert read_tests_prepare(tmp_path) is None
