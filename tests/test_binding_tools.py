"""Tests for ``cw.binding_tools``: the tickets tool-name check (RFC 0019 H2b, #3744)."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from cw.binding_tools import (
    TOOL_LIST_SOURCES,
    ToolList,
    ToolListSource,
    ToolState,
    check_tools,
    declared_tools,
    missing_tools,
)
from cw.models import ClientConfig
from cw.models.bindings import Bindings
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)

_ROOT = Path("/nonexistent-root")


def _client(tools: dict[str, str] | None, backend: str = CODEX_BACKEND) -> ClientConfig:
    realization: dict[str, object] = {"server": "linear"}
    if tools is not None:
        realization["tools"] = tools
    bindings = Bindings.model_validate(
        {"tickets": {"system": "linear", "mcp": {backend: realization}}}
    )
    return ClientConfig(name="acme", workspace_path=_ROOT, bindings=bindings)


def _known(allowed: tuple[str, ...] | None) -> ToolList:
    return ToolList(known=True, allowed=allowed, source="cfg", reason="r")


def _sources(tool_list: ToolList) -> MappingProxyType[str, ToolListSource | None]:
    def _source(server: str, root: Path) -> ToolList:
        assert server == "linear"
        assert root == _ROOT
        return tool_list

    return MappingProxyType({CODEX_BACKEND: _source})


class TestMissingTools:
    def test_a_name_outside_the_allowlist_is_missing(self) -> None:
        assert missing_tools(["b", "a", "c"], _known(("a",))) == ("b", "c")

    def test_no_allowlist_excludes_nothing(self) -> None:
        assert missing_tools(["a"], _known(None)) == ()

    def test_an_unknown_list_excludes_nothing(self) -> None:
        unknown = ToolList(known=False, allowed=("x",), source="cfg", reason="r")
        assert missing_tools(["a"], unknown) == ()


class TestDeclaredTools:
    def test_values_in_declaration_order_deduplicated(self) -> None:
        client = _client({"get": "get_issue", "comment": "save", "close": "save"})
        assert declared_tools(client, CODEX_BACKEND) == ("get_issue", "save")

    def test_no_tools_map_or_no_realization_declares_nothing(self) -> None:
        assert declared_tools(_client(None), CODEX_BACKEND) == ()
        assert declared_tools(_client({"get": "x"}), OPENCODE_BACKEND) == ()
        bare = ClientConfig(name="acme", workspace_path=_ROOT)
        assert declared_tools(bare, CODEX_BACKEND) == ()


class TestCheckTools:
    def test_not_declared(self) -> None:
        check = check_tools(_client(None), CODEX_BACKEND, "linear", _ROOT)
        assert check.state is ToolState.NOT_DECLARED

    def test_missing(self) -> None:
        check = check_tools(
            _client({"get": "get_issue", "comment": "save"}),
            CODEX_BACKEND,
            "linear",
            _ROOT,
            sources=_sources(_known(("get_issue",))),
        )
        assert check.state is ToolState.MISSING
        assert check.missing == ("save",)
        assert check.source == "cfg"

    def test_ok(self) -> None:
        check = check_tools(
            _client({"get": "get_issue"}),
            CODEX_BACKEND,
            "linear",
            _ROOT,
            sources=_sources(_known(("get_issue", "other"))),
        )
        assert check.state is ToolState.OK
        assert check.missing == ()

    def test_unknown_list_fails_open(self) -> None:
        unknown = ToolList(known=False, allowed=None, source="cfg", reason="why")
        check = check_tools(
            _client({"get": "x"}),
            CODEX_BACKEND,
            "linear",
            _ROOT,
            sources=_sources(unknown),
        )
        assert check.state is ToolState.UNKNOWN
        assert check.reason == "why"

    @pytest.mark.parametrize("backend", [CLAUDE_NATIVE_BACKEND, OPENCODE_BACKEND])
    def test_a_harness_without_a_source_is_unknown(self, backend: str) -> None:
        realization = (
            {"plugin_id": "p@m", "tools": {"get": "x"}}
            if backend == CLAUDE_NATIVE_BACKEND
            else {"server": "linear", "tools": {"get": "x"}}
        )
        bindings = Bindings.model_validate(
            {"tickets": {"system": "linear", "mcp": {backend: realization}}}
        )
        client = ClientConfig(name="acme", workspace_path=_ROOT, bindings=bindings)
        check = check_tools(client, backend, "linear", _ROOT)
        assert check.state is ToolState.UNKNOWN
        assert check.source is None

    def test_codex_source_reads_the_enabled_tools_allowlist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.linear]\nenabled_tools = ["get_issue"]\n', encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr("cw.backend_mcp_config._SYSTEM_LAYERS", {})
        root = tmp_path / "ws"
        (root / ".git").mkdir(parents=True)
        check = check_tools(
            _client({"get": "get_issue", "comment": "save_comment"}),
            CODEX_BACKEND,
            "linear",
            root,
        )
        assert check.state is ToolState.MISSING
        assert check.missing == ("save_comment",)
        assert check.source == str(codex_home / "config.toml")


def test_the_registry_names_every_harness_and_only_codex_has_a_source() -> None:
    assert set(TOOL_LIST_SOURCES) == {
        CODEX_BACKEND,
        CLAUDE_NATIVE_BACKEND,
        OPENCODE_BACKEND,
        LOCAL_BACKEND,
    }
    assert [b for b, s in TOOL_LIST_SOURCES.items() if s is not None] == [CODEX_BACKEND]
