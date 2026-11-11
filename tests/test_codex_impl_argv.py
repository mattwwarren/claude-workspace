"""Tests for cw.codex_impl._argv: the write-capable IMPL turn argv (#3061)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_impl._argv import (
    ImplArgvPolicyError,
    build_impl_argv,
    require_impl_sandbox_argv,
)
from cw.codex_review import _exec_policy_argv
from cw.models import SandboxMode

if TYPE_CHECKING:
    from pathlib import Path


def _argv(
    tmp_path: Path,
    *,
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE,
    model: str | None = "gpt-x",
    reasoning_effort: str | None = "high",
    other_servers: tuple[str, ...] = (),
) -> list[str]:
    return build_impl_argv(
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
        model=model,
        reasoning_effort=reasoning_effort,
        sandbox_mode=mode,
        other_servers=other_servers,
    )


def test_default_impl_argv_literal(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv == [
        "codex",
        "exec",
        *_exec_policy_argv(SandboxMode.WORKSPACE_WRITE),
        "--json",
        "--ephemeral",
        "-c",
        "model_reasoning_effort=high",
        "--output-schema",
        str(tmp_path / "schema.json"),
        "-o",
        str(tmp_path / "out.json"),
        "-m",
        "gpt-x",
    ]
    assert "--sandbox" in argv
    assert "workspace-write" in argv


def test_model_is_omitted_when_none_and_effort_when_none(tmp_path: Path) -> None:
    argv = _argv(tmp_path, model=None, reasoning_effort=None)
    assert "-m" not in argv
    assert not any(a.startswith("model_reasoning_effort") for a in argv)
    assert argv[-2:] == ["-o", str(tmp_path / "out.json")]


@pytest.mark.parametrize("mode", list(SandboxMode))
def test_impl_sandbox_comes_from_the_seam_for_each_legal_mode(
    tmp_path: Path, mode: SandboxMode
) -> None:
    argv = _argv(tmp_path, mode=mode)
    policy = _exec_policy_argv(mode)
    assert argv[2 : 2 + len(policy)] == policy
    assert argv.count("--sandbox") == 1
    require_impl_sandbox_argv(argv, mode)


def test_require_impl_sandbox_argv_refuses_a_wider_or_missing_policy(
    tmp_path: Path,
) -> None:
    argv = _argv(tmp_path, mode=SandboxMode.READ_ONLY)
    with pytest.raises(ImplArgvPolicyError):
        require_impl_sandbox_argv(argv, SandboxMode.WORKSPACE_WRITE)
    doubled = [*_argv(tmp_path), "--sandbox", "read-only"]
    with pytest.raises(ImplArgvPolicyError):
        require_impl_sandbox_argv(doubled, SandboxMode.WORKSPACE_WRITE)
    policy = _exec_policy_argv(SandboxMode.WORKSPACE_WRITE)
    missing = ["codex", "exec", *policy[:2], "--json"]
    with pytest.raises(ImplArgvPolicyError):
        require_impl_sandbox_argv(missing, SandboxMode.WORKSPACE_WRITE)


def test_other_mcp_servers_are_disabled(tmp_path: Path) -> None:
    argv = _argv(tmp_path, other_servers=("linear", "notion"))
    assert "mcp_servers.linear.enabled=false" in argv
    assert "mcp_servers.notion.enabled=false" in argv
    assert not any("enabled_tools" in a or a.endswith(".enabled=true") for a in argv)
