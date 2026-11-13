"""Tests for ``read_tests_prepare`` (#2179): the typed ``tests.prepare`` reader.

The reader wraps the one shared loader, ``load_project_config_dict``, so these
tests write real ``.claude/project-config.yaml`` files under ``tmp_path`` through
the canonical writer in ``tests/_shared/cli_preflight.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.project_config import (
    PrepareConfigError,
    load_project_config_dict,
    read_tests_prepare,
)
from tests._shared.cli_preflight import _write_project_config_yaml

_GODOT = "godot --headless --path game --import"


def test_absent_file_returns_none(tmp_path: Path) -> None:
    assert read_tests_prepare(tmp_path) is None


def test_no_tests_block_returns_none(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "quality_gates: {}\n")
    assert read_tests_prepare(tmp_path) is None


def test_top_level_non_mapping_reads_as_absent(tmp_path: Path) -> None:
    # The shared loader returns None for a non-mapping document; the reader
    # treats that as "no tests block", the same as an absent file.
    _write_project_config_yaml(tmp_path, "- just\n- a list\n")
    assert read_tests_prepare(tmp_path) is None


def test_tests_block_null_returns_none(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "tests:\n")
    assert read_tests_prepare(tmp_path) is None


def test_prepare_absent_in_tests_block_returns_none(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "tests:\n  other: value\n")
    assert read_tests_prepare(tmp_path) is None


def test_prepare_string_returned_stripped(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, f"tests:\n  prepare: '  {_GODOT}  '\n")
    assert read_tests_prepare(tmp_path) == _GODOT


def test_multiline_block_scalar_preserved(tmp_path: Path) -> None:
    _write_project_config_yaml(
        tmp_path, "tests:\n  prepare: |\n    echo one\n    echo two\n"
    )
    assert read_tests_prepare(tmp_path) == "echo one\necho two"


@pytest.mark.parametrize("value", ['""', "'   '"])
def test_blank_prepare_returns_none(tmp_path: Path, value: str) -> None:
    _write_project_config_yaml(tmp_path, f"tests:\n  prepare: {value}\n")
    assert read_tests_prepare(tmp_path) is None


@pytest.mark.parametrize(
    "value",
    ["[a]", "7", "true", "{a: 1}"],
    ids=["list", "int", "bool", "mapping"],
)
def test_malformed_prepare_raises(tmp_path: Path, value: str) -> None:
    _write_project_config_yaml(tmp_path, f"tests:\n  prepare: {value}\n")
    with pytest.raises(PrepareConfigError, match=r"tests\.prepare"):
        read_tests_prepare(tmp_path)


@pytest.mark.parametrize("body", ["tests: [a]\n", "tests: x\n"])
def test_tests_not_a_mapping_raises(tmp_path: Path, body: str) -> None:
    _write_project_config_yaml(tmp_path, body)
    with pytest.raises(PrepareConfigError, match="must be a mapping"):
        read_tests_prepare(tmp_path)


def test_invalid_utf8_file_raises_prepare_config_error(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, b"tests:\n  prepare: \xff\n")
    with pytest.raises(PrepareConfigError, match=r"UTF-8"):
        read_tests_prepare(tmp_path)


def test_unparseable_yaml_reads_as_absent(tmp_path: Path) -> None:
    # The shared loader's established lenient parse-error behavior is retained.
    _write_project_config_yaml(tmp_path, "tests: [\n")
    assert read_tests_prepare(tmp_path) is None


def test_lenient_loader_reads_unreadable_and_unparseable_as_absent(
    tmp_path: Path,
) -> None:
    # Every other consumer keeps the lenient contract: strict=False never raises
    # on an unreadable or unparseable file (#2179 kept it byte-identical).
    unreadable = tmp_path / "unreadable"
    (unreadable / ".claude" / "project-config.yaml").mkdir(parents=True)
    assert load_project_config_dict(unreadable) is None
    unparseable = tmp_path / "unparseable"
    _write_project_config_yaml(unparseable, "tests: [\n")
    assert load_project_config_dict(unparseable) is None


def test_unreadable_config_raises_not_absent(tmp_path: Path) -> None:
    # A directory where the file should be exists but cannot be read as a file
    # (IsADirectoryError, an OSError): a declared key there must not vanish.
    (tmp_path / ".claude" / "project-config.yaml").mkdir(parents=True)
    with pytest.raises(PrepareConfigError, match=r"could not be read or parsed"):
        read_tests_prepare(tmp_path)


def test_dangling_config_symlink_raises_not_absent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / "project-config.yaml").symlink_to(tmp_path / "missing.yaml")
    with pytest.raises(PrepareConfigError, match=r"could not be read or parsed"):
        read_tests_prepare(tmp_path)
