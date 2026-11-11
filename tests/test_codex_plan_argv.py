"""Tests for cw.codex_plan._argv: the read-only PLAN turn argv (#3060 R2, R4)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.codex_plan._argv import (
    PLAN_READ_ONLY_ARGV,
    PLAN_TRACKER_READ_TOOLS,
    PlanArgvPolicyError,
    build_plan_argv,
    require_read_only_argv,
)
from cw.codex_review import _exec_policy_argv
from cw.models import SandboxMode
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import TrackerIoConfigError, TrackerIoReason

if TYPE_CHECKING:
    from pathlib import Path


def _argv(
    tmp_path: Path,
    *,
    model: str | None = "gpt-x",
    reasoning_effort: str | None = "high",
    mcp_server: str | None = None,
    tracker: str | None = None,
    other_servers: tuple[str, ...] = (),
) -> list[str]:
    return build_plan_argv(
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
        model=model,
        reasoning_effort=reasoning_effort,
        mcp_server=mcp_server,
        tracker=tracker,
        other_servers=other_servers,
    )


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_policy_constant_is_the_seams_read_only_output() -> None:
    assert list(PLAN_READ_ONLY_ARGV) == _exec_policy_argv(SandboxMode.READ_ONLY)


def test_argv_carries_the_contiguous_policy_after_codex_exec(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv[:2] == ["codex", "exec"]
    assert tuple(argv[2 : 2 + len(PLAN_READ_ONLY_ARGV)]) == PLAN_READ_ONLY_ARGV


def test_argv_carries_schema_output_and_the_audit_stream(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert _value_after(argv, "--output-schema") == str(tmp_path / "schema.json")
    assert _value_after(argv, "-o") == str(tmp_path / "out.json")
    assert "--json" in argv
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" not in argv


def test_model_and_reasoning_pins_come_from_the_caller(tmp_path: Path) -> None:
    argv = _argv(tmp_path, model="gpt-plan", reasoning_effort="medium")
    assert argv[-2:] == ["-m", "gpt-plan"]
    assert "model_reasoning_effort=medium" in argv


def test_no_model_and_no_effort_add_nothing(tmp_path: Path) -> None:
    argv = _argv(tmp_path, model=None, reasoning_effort=None)
    assert "-m" not in argv
    assert not any(a.startswith("model_reasoning_effort") for a in argv)


def test_non_github_enables_only_the_read_tools(tmp_path: Path) -> None:
    argv = _argv(
        tmp_path,
        mcp_server="linear",
        tracker=TRACKER_LINEAR,
        other_servers=("linear", "notion"),
    )
    allowlist = json.dumps(["get_issue", "list_comments"], separators=(",", ":"))
    assert f"mcp_servers.linear.enabled_tools={allowlist}" in argv
    assert "mcp_servers.notion.enabled=false" in argv
    assert "mcp_servers.linear.enabled=false" not in argv
    assert PLAN_TRACKER_READ_TOOLS[TRACKER_LINEAR] == ("get_issue", "list_comments")


def test_github_enables_no_tool_and_disables_every_server(tmp_path: Path) -> None:
    argv = _argv(tmp_path, other_servers=("linear", "notion"))
    assert not any("enabled_tools" in a for a in argv)
    assert "mcp_servers.linear.enabled=false" in argv
    assert "mcp_servers.notion.enabled=false" in argv


def test_require_read_only_accepts_the_built_argv(tmp_path: Path) -> None:
    require_read_only_argv(_argv(tmp_path))


def test_require_read_only_refuses_a_workspace_write_argv() -> None:
    argv = ["codex", "exec", *_exec_policy_argv(SandboxMode.WORKSPACE_WRITE), "-o"]
    with pytest.raises(PlanArgvPolicyError):
        require_read_only_argv(argv)


def test_require_read_only_refuses_a_missing_approval_override() -> None:
    argv = ["codex", "exec", *PLAN_READ_ONLY_ARGV[:2], "--json", "--ephemeral"]
    with pytest.raises(PlanArgvPolicyError):
        require_read_only_argv(argv)


def test_require_read_only_refuses_a_second_sandbox_flag(tmp_path: Path) -> None:
    argv = [*_argv(tmp_path), *_exec_policy_argv(SandboxMode.WORKSPACE_WRITE)[:2]]
    with pytest.raises(PlanArgvPolicyError):
        require_read_only_argv(argv)


def test_invalid_server_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrackerIoConfigError) as excinfo:
        _argv(tmp_path, mcp_server="bad.name", tracker=TRACKER_LINEAR)
    assert excinfo.value.reason is TrackerIoReason.MCP_SERVER_UNNAMED


def test_unsupported_tracker_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TrackerIoConfigError) as excinfo:
        _argv(tmp_path, mcp_server="jira", tracker="jira")
    assert excinfo.value.reason is TrackerIoReason.TRACKER_UNSUPPORTED
