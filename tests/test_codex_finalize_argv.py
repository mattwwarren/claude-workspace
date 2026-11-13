"""Tests for cw.codex_finalize._argv: the read-only FINALIZE turn argv (#3062)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_finalize._argv import (
    FINALIZE_READ_ONLY_ARGV,
    FinalizeArgvPolicyError,
    build_finalize_argv,
    require_read_only_finalize_argv,
)
from cw.codex_review import _exec_policy_argv
from cw.models import SandboxMode
from cw.tracker_io import TrackerIoConfigError

if TYPE_CHECKING:
    from pathlib import Path


def _argv(
    tmp_path: Path,
    *,
    model: str | None = "gpt-x",
    reasoning_effort: str | None = "high",
    other_servers: tuple[str, ...] = (),
) -> list[str]:
    return build_finalize_argv(
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
        model=model,
        reasoning_effort=reasoning_effort,
        other_servers=other_servers,
    )


def test_read_only_policy_is_the_literal_seam_output() -> None:
    assert FINALIZE_READ_ONLY_ARGV == (
        "--sandbox",
        "read-only",
        "-c",
        "approval_policy=never",
    )
    assert list(FINALIZE_READ_ONLY_ARGV) == _exec_policy_argv(SandboxMode.READ_ONLY)


def test_default_finalize_argv_literal(tmp_path: Path) -> None:
    assert _argv(tmp_path, other_servers=("linear",)) == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-c",
        "approval_policy=never",
        "--json",
        "--ephemeral",
        "-c",
        "model_reasoning_effort=high",
        "-c",
        "mcp_servers.linear.enabled=false",
        "--output-schema",
        str(tmp_path / "schema.json"),
        "-o",
        str(tmp_path / "out.json"),
        "-m",
        "gpt-x",
    ]


def test_output_schema_and_output_file_flags_carry_the_paths(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv[argv.index("--output-schema") + 1] == str(tmp_path / "schema.json")
    assert argv[argv.index("-o") + 1] == str(tmp_path / "out.json")


def test_model_and_effort_are_omitted_when_none(tmp_path: Path) -> None:
    argv = _argv(tmp_path, model=None, reasoning_effort=None)
    assert "-m" not in argv
    assert not any(a.startswith("model_reasoning_effort") for a in argv)
    assert argv[-2:] == ["-o", str(tmp_path / "out.json")]


def test_the_turn_never_gets_a_wider_sandbox(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv.count("--sandbox") == 1
    assert "workspace-write" not in argv
    assert "danger-full-access" not in argv


def test_every_declared_mcp_server_is_disabled(tmp_path: Path) -> None:
    argv = _argv(tmp_path, other_servers=("linear", "notion", "github"))
    for name in ("linear", "notion", "github"):
        assert f"mcp_servers.{name}.enabled=false" in argv
    assert not any("enabled_tools" in a or a.endswith(".enabled=true") for a in argv)


def test_an_unaddressable_server_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrackerIoConfigError):
        _argv(tmp_path, other_servers=("bad.name",))


def test_the_built_argv_satisfies_the_policy_check(tmp_path: Path) -> None:
    require_read_only_finalize_argv(_argv(tmp_path, other_servers=("linear",)))


def test_a_wider_mode_is_refused(tmp_path: Path) -> None:
    wider = ["codex", "exec", *_exec_policy_argv(SandboxMode.WORKSPACE_WRITE)]
    wider += _argv(tmp_path)[2 + len(FINALIZE_READ_ONLY_ARGV) :]
    with pytest.raises(FinalizeArgvPolicyError):
        require_read_only_finalize_argv(wider)


def test_a_missing_approval_override_is_refused(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    missing = [*argv[:4], *argv[6:]]
    assert missing[2:4] == ["--sandbox", "read-only"]
    with pytest.raises(FinalizeArgvPolicyError):
        require_read_only_finalize_argv(missing)


def test_a_second_later_sandbox_flag_is_refused(tmp_path: Path) -> None:
    doubled = [*_argv(tmp_path), "--sandbox", "danger-full-access"]
    with pytest.raises(FinalizeArgvPolicyError):
        require_read_only_finalize_argv(doubled)


def test_a_policy_that_does_not_follow_codex_exec_is_refused(tmp_path: Path) -> None:
    shifted = ["codex", "exec", "--json", *_argv(tmp_path)[2:]]
    with pytest.raises(FinalizeArgvPolicyError):
        require_read_only_finalize_argv(shifted)


def test_an_empty_argv_is_refused() -> None:
    with pytest.raises(FinalizeArgvPolicyError):
        require_read_only_finalize_argv([])
