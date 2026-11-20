"""The tickets-binding reach half of ``harness-capabilities`` (RFC 0019 H2b, #3744).

A port of the retired ``tracker-mcp`` doctor check's matrix (#3055, #3060)
onto ``_check_harness_capabilities``. Severity follows R8: FAIL only when the
client opted in and the dispatch tick would park the row; every other state,
including the old check's WARNs, is ``ok`` with the state in the detail. A
skipped or unreadable read renders ``unknown``, never ``missing`` (#3306), and
the reachable, unreachable and unknown branches keep the declared-only caveat
(#3307).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.doctor.capabilities import _check_harness_capabilities
from cw.models import CLAUDE_NATIVE_BACKEND, CODEX_BACKEND, OPENCODE_BACKEND, Stage
from cw.models.bindings import Bindings
from cw.models.client import ClientConfig, TrackerMcpGateConfig
from tests._backend_mcp_helpers import (
    McpStage,
    stage_mcp_config,
    write_project_layer,
)
from tests._doctor_helpers import make_client
from tests._shared.cli_preflight import _write_tracker_config

if TYPE_CHECKING:
    from pathlib import Path

    from cw.doctor import CheckResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_UNVERIFIED = "auth, env and headless load unverified"
_DECLARED_ONLY = f"declaration-only check; {_UNVERIFIED}"
_OPENCODE_PLAN_IMPL = {Stage.PLAN: OPENCODE_BACKEND, Stage.IMPL: OPENCODE_BACKEND}
_CODEX_PLAN = {Stage.PLAN: CODEX_BACKEND}
_CODEX_FINALIZE = {Stage.FINALIZE: CODEX_BACKEND}


def _gate(
    *, enabled: bool = True, server: str | None = "linear", plugin: str | None = None
) -> TrackerMcpGateConfig:
    servers = (
        None if server is None else {OPENCODE_BACKEND: server, CODEX_BACKEND: server}
    )
    plugin_id = plugin if plugin is not None else (None if servers else "p@m")
    return TrackerMcpGateConfig(enabled=enabled, plugin_id=plugin_id, servers=servers)


def _root(tmp_path: Path, tracker: str | None) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    if tracker is not None:
        _write_tracker_config(root, tracker)
    return root


def _client(
    tmp_path: Path,
    *,
    tracker: str | None = "linear",
    review_backend: str = CLAUDE_NATIVE_BACKEND,
    executors: dict[Stage, str] | None = None,
    gate: TrackerMcpGateConfig | None = None,
) -> ClientConfig:
    return make_client(
        backend=review_backend,
        executors=executors,
        tracker_mcp_gate=gate,
        workspace_path=_root(tmp_path, tracker),
    )


def _row(client: ClientConfig, backend: str) -> CheckResult:
    name = f"harness-capabilities/{client.name}/default/{backend}"
    (match,) = [
        r for r in _check_harness_capabilities({client.name: client}) if r.name == name
    ]
    return match


class TestTracker:
    @pytest.mark.parametrize(
        ("backend", "executors"),
        [(CODEX_BACKEND, _CODEX_PLAN), (OPENCODE_BACKEND, _OPENCODE_PLAN_IMPL)],
    )
    def test_github_tracker_is_not_needed_and_never_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        backend: str,
        executors: dict[Stage, str],
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "absent")
        client = _client(
            tmp_path, tracker="github-issues", executors=executors, gate=_gate()
        )

        result = _row(client, backend)

        assert (result.ok, result.warn) == (True, False)
        assert "not needed (GitHub tracker, reached through gh)" in result.detail

    def test_unset_tracker_is_unknown_and_never_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#3306: a skipped read is labelled unknown, never missing."""
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "absent")
        client = _client(tmp_path, tracker=None, executors=_CODEX_PLAN, gate=_gate())

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "unknown (MCP declaration not read: tracker is unset)" in result.detail
        assert "missing" not in result.detail
        assert "unreachable" not in result.detail

    def test_root_prefers_repo_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "absent")
        repo = _root(tmp_path, "github-issues")
        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws-without-config",
            repo_path=repo,
            branch="main",
            tracker_mcp_gate=_gate(),
            pipeline=make_client(executors=_CODEX_PLAN).pipeline,
        )

        assert "GitHub tracker" in _row(client, CODEX_BACKEND).detail


class TestServerRead:
    @pytest.mark.parametrize(
        ("backend", "executors"),
        [
            (CODEX_BACKEND, _CODEX_PLAN),
            (OPENCODE_BACKEND, _OPENCODE_PLAN_IMPL),
        ],
    )
    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_opted_in_and_missing_fails_as_the_tick_would_park(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        backend: str,
        executors: dict[Stage, str],
        state: McpStage,
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, backend, state)
        client = _client(tmp_path, executors=executors, gate=_gate())

        result = _row(client, backend)

        assert (result.ok, result.warn) == (False, False)
        assert f"unreachable (absent or disabled in {path}" in result.detail
        assert _DECLARED_ONLY in result.detail
        assert "parks such a row as binding_unreachable" in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_opted_out_and_missing_is_ok_with_the_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, state)
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=_gate(enabled=False))

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "unreachable" in result.detail
        assert "parks such a row" not in result.detail

    @pytest.mark.parametrize("state", ["absent", "enabled"])
    def test_codex_impl_and_finalize_do_not_need_the_binding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        """RFC 0019 T2 (#3712): codex IMPL and FINALIZE read the ticket snapshot."""
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, state)
        client = _client(
            tmp_path,
            executors={Stage.IMPL: CODEX_BACKEND, Stage.FINALIZE: CODEX_BACKEND},
            gate=_gate(),
        )

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "not needed (no routed stage needs the tickets binding)" in (
            result.detail
        )

    def test_a_review_only_codex_lane_does_not_need_the_binding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "absent")
        client = _client(tmp_path, review_backend=CODEX_BACKEND, gate=_gate())

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "not needed (no routed stage needs the tickets binding)" in (
            result.detail
        )

    def test_enabled_is_reachable_with_the_caveat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "enabled")
        client = _client(tmp_path, executors=_OPENCODE_PLAN_IMPL, gate=_gate())

        result = _row(client, OPENCODE_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert f"reachable (declared in {path}; {_UNVERIFIED})" in result.detail
        assert "tools:" not in result.detail, "no tools declared, no tool line"

    @pytest.mark.parametrize("state", ["missing", "malformed"])
    def test_an_unreadable_config_is_unknown_with_the_caveat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, state)
        client = _client(tmp_path, executors=_OPENCODE_PLAN_IMPL, gate=_gate())

        result = _row(client, OPENCODE_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "tickets binding unknown (" in result.detail
        assert _DECLARED_ONLY in result.detail
        assert "unreachable" not in result.detail

    def test_another_layer_is_unknown_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = _client(tmp_path, executors=_OPENCODE_PLAN_IMPL, gate=_gate())
        write_project_layer(client.workspace_path, OPENCODE_BACKEND)

        result = _row(client, OPENCODE_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "not parsed" in result.detail
        assert "unreachable" not in result.detail


class TestUnbound:
    def test_opencode_without_a_server_is_unbound_and_ok(self, tmp_path: Path) -> None:
        client = _client(
            tmp_path, executors=_OPENCODE_PLAN_IMPL, gate=_gate(server=None)
        )

        result = _row(client, OPENCODE_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "unbound (no bindings.tickets.mcp.opencode.server" in result.detail

    def test_codex_without_a_server_fails_unbound(self, tmp_path: Path) -> None:
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=_gate(server=None))

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (False, False)
        assert "unbound (no bindings.tickets.mcp.codex.server" in result.detail
        assert "parks such a row as binding_unbound" in result.detail

    def test_codex_finalize_without_a_server_is_not_needed(
        self, tmp_path: Path
    ) -> None:
        """RFC 0019 T2 (#3712): a FINALIZE-only codex lane never parks unbound."""
        client = _client(tmp_path, executors=_CODEX_FINALIZE, gate=_gate(server=None))

        result = _row(client, CODEX_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert "tickets binding not needed" in result.detail

    def test_codex_with_an_invalid_server_name_fails_unbound(
        self, tmp_path: Path
    ) -> None:
        gate = TrackerMcpGateConfig(enabled=True, servers={CODEX_BACKEND: "a.b"})
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=gate)

        result = _row(client, CODEX_BACKEND)

        assert result.ok is False
        assert "'a.b' is not a valid name" in result.detail

    def test_codex_unbound_on_a_github_tracker_is_not_needed(
        self, tmp_path: Path
    ) -> None:
        client = _client(
            tmp_path,
            tracker="github-issues",
            executors=_CODEX_PLAN,
            gate=_gate(server=None),
        )

        assert _row(client, CODEX_BACKEND).ok is True


class TestClaudeNative:
    def test_a_bound_plugin_is_unknown_without_a_branch(self, tmp_path: Path) -> None:
        client = _client(tmp_path, gate=_gate(server=None, plugin="linear@acme"))

        result = _row(client, CLAUDE_NATIVE_BACKEND)

        assert (result.ok, result.warn) == (True, False)
        assert (
            "unknown (claude-native reads enabledPlugins on the ticket branch, which"
            " doctor does not have" in result.detail
        )
        assert _DECLARED_ONLY in result.detail

    def test_no_plugin_is_unbound(self, tmp_path: Path) -> None:
        client = _client(tmp_path, gate=_gate())

        result = _row(client, CLAUDE_NATIVE_BACKEND)

        assert "unbound (no bindings.tickets.mcp.claude-native.plugin_id" in (
            result.detail
        )


def _tools_client(tmp_path: Path, tools: dict[str, str]) -> ClientConfig:
    bindings = Bindings.model_validate(
        {
            "tickets": {
                "system": "linear",
                "mcp": {CODEX_BACKEND: {"server": "linear", "tools": tools}},
                "preflight": {"enabled": True},
            }
        }
    )
    return make_client(
        executors=_CODEX_PLAN,
        bindings=bindings,
        workspace_path=_root(tmp_path, "linear"),
    )


def _codex_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "enabled")
    text = path.read_text(encoding="utf-8").replace(
        "[mcp_servers.linear]\n",
        '[mcp_servers.linear]\nenabled_tools = ["get_issue"]\n',
    )
    path.write_text(text, encoding="utf-8")
    return path


class TestTools:
    def test_an_excluded_tool_fails_tool_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = _codex_allowlist(monkeypatch, tmp_path)
        client = _tools_client(tmp_path, {"get": "get_issue", "comment": "save_it"})

        result = _row(client, CODEX_BACKEND)

        assert result.ok is False
        assert f"tools: missing save_it from enabled_tools in {path}" in result.detail
        assert "parks such a row as binding_tool_missing" in result.detail

    def test_allowed_tools_are_checked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = _codex_allowlist(monkeypatch, tmp_path)
        client = _tools_client(tmp_path, {"get": "get_issue"})

        result = _row(client, CODEX_BACKEND)

        assert result.ok is True
        assert f"tools: checked against enabled_tools in {path}" in result.detail

    def test_an_unanswerable_tool_list_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "enabled")
        client = _tools_client(tmp_path, {"get": "get_issue"})
        write_project_layer(client.workspace_path, CODEX_BACKEND)

        result = _row(client, CODEX_BACKEND)

        assert result.ok is True
        assert "tools: unknown (" in result.detail
