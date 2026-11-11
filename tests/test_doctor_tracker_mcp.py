"""Tests for src/cw/doctor/tracker_mcp.py (#3055, RFC 0018 S3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.doctor import _check_tracker_mcp
from cw.doctor.core import _client_config_checks
from cw.models import CODEX_BACKEND, OPENCODE_BACKEND, Stage
from cw.models.client import ClientConfig, TrackerMcpGateConfig
from tests._backend_mcp_helpers import McpStage, stage_mcp_config
from tests._doctor_helpers import make_client, make_lane
from tests._shared.cli_preflight import _write_tracker_config

if TYPE_CHECKING:
    from pathlib import Path

    from cw.doctor import CheckResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_OPENCODE_PLAN_IMPL = {Stage.PLAN: OPENCODE_BACKEND, Stage.IMPL: OPENCODE_BACKEND}
_CAVEAT_TAIL = "auth, env and headless load unverified"
_CODEX_PLAN = {Stage.PLAN: CODEX_BACKEND}


def _gate(
    *, enabled: bool = True, server: str | None = "linear"
) -> TrackerMcpGateConfig:
    servers = (
        None if server is None else {OPENCODE_BACKEND: server, CODEX_BACKEND: server}
    )
    return TrackerMcpGateConfig(
        enabled=enabled, plugin_id=None if servers else "p@m", servers=servers
    )


def _client(
    tmp_path: Path,
    *,
    tracker: str | None = "linear",
    review_backend: str = "claude-native",
    executors: dict[Stage, str] | None = None,
    gate: TrackerMcpGateConfig | None = None,
) -> ClientConfig:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    if tracker is not None:
        _write_tracker_config(root, tracker)
    return make_client(
        backend=review_backend,
        executors=executors,
        tracker_mcp_gate=gate,
        workspace_path=root,
    )


def _results(client: ClientConfig) -> dict[str, CheckResult]:
    return {r.name: r for r in _check_tracker_mcp({client.name: client})}


def _only(client: ClientConfig) -> CheckResult:
    (result,) = _check_tracker_mcp({client.name: client})
    return result


class TestScope:
    def test_github_tracker_gives_no_result(self, tmp_path: Path) -> None:
        client = _client(
            tmp_path,
            tracker="github-issues",
            review_backend=CODEX_BACKEND,
            executors=_OPENCODE_PLAN_IMPL,
        )

        assert _check_tracker_mcp({client.name: client}) == []

    def test_claude_native_and_local_give_no_result(self, tmp_path: Path) -> None:
        client = _client(tmp_path, executors={Stage.PLAN: "local"})

        assert _check_tracker_mcp({client.name: client}) == []

    def test_opencode_review_only_gives_no_result(self, tmp_path: Path) -> None:
        """opencode REVIEW performs no tracker operations."""
        client = _client(tmp_path, review_backend=OPENCODE_BACKEND)

        assert _check_tracker_mcp({client.name: client}) == []

    def test_one_result_per_lane_and_backend(self, tmp_path: Path) -> None:
        client = _client(
            tmp_path, review_backend=CODEX_BACKEND, executors=_OPENCODE_PLAN_IMPL
        )

        assert sorted(_results(client)) == [
            "tracker-mcp/client-a/default/codex",
            "tracker-mcp/client-a/default/opencode",
        ]

    def test_lane_override_is_resolved(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        _write_tracker_config(root, "linear")
        client = make_client(
            make_lane("oc", executors=_OPENCODE_PLAN_IMPL),
            backend="claude-native",
            workspace_path=root,
        )

        assert list(_results(client)) == ["tracker-mcp/client-a/oc/opencode"]

    def test_registered_in_client_config_checks(self, tmp_path: Path) -> None:
        client = _client(tmp_path, review_backend=CODEX_BACKEND)

        names = [r.name for r in _client_config_checks({client.name: client})]

        assert "tracker-mcp/client-a/default/codex" in names

    def test_root_prefers_repo_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "shared"
        (repo / ".git").mkdir(parents=True)
        _write_tracker_config(repo, "linear")
        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws-without-config",
            repo_path=repo,
            branch="main",
            pipeline=make_client(backend=CODEX_BACKEND).pipeline,
        )

        assert list(_results(client)) == ["tracker-mcp/client-a/default/codex"]


class TestCodex:
    @pytest.mark.parametrize("tracker", ["linear", None])
    @pytest.mark.parametrize("gate", [None, "on"])
    def test_always_warns_with_lost_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tracker: str | None,
        gate: str | None,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "enabled")
        client = _client(
            tmp_path,
            tracker=tracker,
            review_backend=CODEX_BACKEND,
            gate=_gate() if gate else None,
        )

        result = _only(client)

        assert result.ok is True
        assert result.warn is True
        assert "free-text ticket comments" in result.detail
        assert "voided-findings block (#1814)" in result.detail
        assert "marker comments" in result.detail
        assert "settle" not in result.detail
        assert "constraints and finding dispositions still reach" in result.detail
        # #3057: the verdict now posts through the tracker-IO turn.
        assert "verdict comment is not posted" not in result.detail
        assert "tracker-IO turn" in result.detail
        assert "tracker_mcp_gate.servers.codex" in result.detail
        assert f"tracker {tracker or 'unset'}" in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_missing_server_still_only_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, state)
        client = _client(tmp_path, review_backend=CODEX_BACKEND, gate=_gate())

        result = _only(client)

        assert (result.ok, result.warn) == (True, True)
        assert f"server linear is {state}" in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_plan_lane_gate_on_and_missing_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        """#3060: a codex PLAN row would park, so this lane fails like opencode."""
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, state)
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=_gate())

        result = _only(client)

        assert result.ok is False
        assert f"server linear is {state}" in result.detail
        assert "codex PLAN turn (#3060)" in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_plan_lane_gate_off_and_missing_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, state)
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=_gate(enabled=False))

        assert (_only(client).ok, _only(client).warn) == (True, True)

    def test_plan_lane_enabled_still_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "enabled")
        client = _client(tmp_path, executors=_CODEX_PLAN, gate=_gate())

        result = _only(client)

        assert (result.ok, result.warn) == (True, True)
        assert _CAVEAT_TAIL in result.detail


class TestOpencode:
    def _client(
        self, tmp_path: Path, gate: TrackerMcpGateConfig | None, tracker: str | None
    ) -> ClientConfig:
        return _client(
            tmp_path, tracker=tracker, executors=_OPENCODE_PLAN_IMPL, gate=gate
        )

    def test_unset_tracker_warns_about_the_blocking_prompt(
        self, tmp_path: Path
    ) -> None:
        result = _only(self._client(tmp_path, _gate(), None))

        assert (result.ok, result.warn) == (True, True)
        assert "tracker_tool_unavailable" in result.detail
        assert "tracker unset" in result.detail

    def test_no_server_named_warns(self, tmp_path: Path) -> None:
        result = _only(self._client(tmp_path, _gate(server=None), "linear"))

        assert (result.ok, result.warn) == (True, True)
        assert "server none named" in result.detail
        assert "no tracker_mcp_gate.servers entry" in result.detail

    def test_no_gate_warns(self, tmp_path: Path) -> None:
        result = _only(self._client(tmp_path, None, "linear"))

        assert (result.ok, result.warn) == (True, True)

    def test_enabled_is_ok_with_the_caveat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "enabled")

        result = _only(self._client(tmp_path, _gate(), "linear"))

        assert (result.ok, result.warn) == (True, False)
        assert f"declared in {path}; {_CAVEAT_TAIL}" in result.detail
        for part in (
            "lane default",
            "stage plan,impl",
            "backend opencode",
            "tracker linear",
            "server linear",
        ):
            assert part in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_gate_on_and_missing_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, state)

        result = _only(self._client(tmp_path, _gate(), "linear"))

        assert result.ok is False
        assert f"server linear is {state} in {path}" in result.detail

    @pytest.mark.parametrize("state", ["absent", "disabled"])
    def test_gate_off_and_missing_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, state)

        result = _only(self._client(tmp_path, _gate(enabled=False), "linear"))

        assert (result.ok, result.warn) == (True, True)

    def test_finalize_only_lane_missing_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The gate parks only PLAN/IMPL; a FINALIZE-only lane cannot park."""
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = _client(
            tmp_path, executors={Stage.FINALIZE: OPENCODE_BACKEND}, gate=_gate()
        )

        result = _only(client)

        assert (result.ok, result.warn) == (True, True)
        assert "stage finalize" in result.detail

    @pytest.mark.parametrize("state", ["missing", "malformed"])
    def test_unknown_reading_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, state)

        result = _only(self._client(tmp_path, _gate(), "linear"))

        assert (result.ok, result.warn) == (True, True)
        assert "MCP declaration unverified" in result.detail

    def test_probe_found_layer_warns_not_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = self._client(tmp_path, _gate(), "linear")
        (client.workspace_path / "opencode.json").write_text("{}", encoding="utf-8")

        result = _only(client)

        assert (result.ok, result.warn) == (True, True)
        assert "not parsed" in result.detail
