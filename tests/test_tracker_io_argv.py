"""Tests for cw.tracker_io._argv: the MCP-allowlisted ``codex exec`` argv (#3057)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_review import _exec_policy_argv
from cw.models import SandboxMode
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import (
    TRACKER_COMMENT_TOOLS,
    TrackerIoConfigError,
    TrackerIoReason,
    build_tracker_io_argv,
    comment_tools_for,
)

_SCHEMA = Path("/scratch/schema.json")
_OUT = Path("/scratch/result.json")
_ALLOWLIST = '["create_comment","list_comments"]'


def _argv(
    server: str = "linear",
    tracker: str = TRACKER_LINEAR,
    other_servers: tuple[str, ...] = (),
) -> list[str]:
    return build_tracker_io_argv(
        server=server,
        tracker=tracker,
        schema_path=_SCHEMA,
        output_path=_OUT,
        other_servers=other_servers,
    )


def test_exact_argv() -> None:
    assert _argv(other_servers=("github", "linear", "notion")) == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-c",
        "approval_policy=never",
        "--skip-git-repo-check",
        "--json",
        "--ephemeral",
        "-c",
        "model_reasoning_effort=low",
        "-c",
        f"mcp_servers.linear.enabled_tools={_ALLOWLIST}",
        "-c",
        "mcp_servers.github.enabled=false",
        "-c",
        "mcp_servers.notion.enabled=false",
        "--output-schema",
        str(_SCHEMA),
        "-o",
        str(_OUT),
    ]


def test_user_config_is_not_ignored() -> None:
    """The MCP server definition lives in ~/.codex/config.toml."""
    assert "--ignore-user-config" not in _argv()


def test_exec_policy_is_the_shared_seam() -> None:
    argv = _argv()
    seam = _exec_policy_argv(SandboxMode.READ_ONLY)
    assert (
        argv[2 : 2 + len(seam)]
        == seam
        == [
            "--sandbox",
            "read-only",
            "-c",
            "approval_policy=never",
        ]
    )


def test_only_the_named_server_gets_an_allowlist() -> None:
    argv = _argv(other_servers=("github",))
    overrides = [a for a in argv if "enabled_tools" in a]
    assert overrides == [f"mcp_servers.linear.enabled_tools={_ALLOWLIST}"]


@pytest.mark.parametrize(
    "forbidden", ["get_issue", "update_comment", "delete_comment", "save_"]
)
def test_no_other_tool_name_appears(forbidden: str) -> None:
    assert not any(forbidden in a for a in _argv(other_servers=("github",)))


def test_other_servers_are_disabled_and_the_target_never() -> None:
    argv = _argv(other_servers=("linear", "a", "b", "a"))
    disabled = [a for a in argv if a.endswith(".enabled=false")]
    assert disabled == ["mcp_servers.a.enabled=false", "mcp_servers.b.enabled=false"]


def test_unexpressible_other_server_fails_closed() -> None:
    """A declared name with a dot cannot be left enabled on the turn."""
    with pytest.raises(TrackerIoConfigError) as info:
        _argv(other_servers=("bad.name", "ok"))
    assert info.value.reason is TrackerIoReason.MCP_SERVER_UNNAMED


@pytest.mark.parametrize("server", ["lin.ear", "lin ear", "", "linear\n"])
def test_invalid_server_name_raises(server: str) -> None:
    with pytest.raises(TrackerIoConfigError) as info:
        _argv(server=server)
    assert info.value.reason is TrackerIoReason.MCP_SERVER_UNNAMED


def test_unsupported_tracker_raises() -> None:
    with pytest.raises(TrackerIoConfigError) as info:
        _argv(tracker="jira")
    assert info.value.reason is TrackerIoReason.TRACKER_UNSUPPORTED
    assert "jira" in str(info.value)


def test_comment_tools_for_linear() -> None:
    assert comment_tools_for(TRACKER_LINEAR) == ("create_comment", "list_comments")
    assert dict(TRACKER_COMMENT_TOOLS) == {TRACKER_LINEAR: comment_tools_for("linear")}


def test_build_is_deterministic() -> None:
    assert _argv(other_servers=("x",)) == _argv(other_servers=("x",))
