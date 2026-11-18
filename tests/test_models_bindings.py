"""Tests for ``cw.models.bindings`` and the ``ClientConfig.bindings`` block (#3702).

RFC 0019 R1: the per-role binding block, its validation, the legacy
``tracker_mcp_gate`` migration (warn once per client) and the resolver
accessors every consumer reads through.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cw.cli import main
from cw.models import ClientConfig
from cw.models.bindings import (
    BindingPreflight,
    Bindings,
    Harness,
    McpRealization,
    Role,
    RoleBinding,
    TicketOp,
    TicketsBinding,
    TicketsSystem,
)
from cw.models.client import TrackerMcpGateConfig
from cw.models.orchestrator_config import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR
from tests.test_ticket_boundary_guard import _direct_gh_call_linenos

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOGGER = "cw.models.client"
_LINEAR_MCP: dict[str, object] = {
    "system": "linear",
    "mcp": {"codex": {"server": "linear"}},
}


def _client(**data: object) -> ClientConfig:
    return ClientConfig.model_validate(
        {"name": "acme", "workspace_path": "/tmp/acme", **data}
    )


def _legacy_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "tracker_mcp_gate is deprecated" in record.getMessage()
    ]


class TestRoleSet:
    def test_fields_are_exactly_the_roles(self) -> None:
        assert set(Bindings.model_fields) == {role.value for role in Role}

    def test_unknown_role_is_rejected_by_name(self) -> None:
        with pytest.raises(ValidationError, match="jira"):
            Bindings.model_validate({"jira": _LINEAR_MCP})

    def test_default_has_no_role(self) -> None:
        bindings = Bindings()
        assert bindings.tickets is None
        assert bindings.knowledge is None
        assert bindings.code_host is None
        assert bindings.chat is None

    def test_client_defaults_to_empty_bindings(self) -> None:
        assert _client().bindings == Bindings()


class TestRealizationShape:
    def test_neither_mcp_nor_cli_is_rejected_naming_the_role(self) -> None:
        with pytest.raises(ValidationError, match=r"bindings\.knowledge .*neither"):
            Bindings.model_validate({"knowledge": {"system": "notion"}})

    def test_both_mcp_and_cli_is_rejected_naming_the_role(self) -> None:
        with pytest.raises(ValidationError, match=r"bindings\.tickets .*both"):
            Bindings.model_validate(
                {
                    "tickets": {
                        "system": "github-issues",
                        "cli": "gh",
                        "mcp": {"codex": {"server": "github"}},
                    }
                }
            )

    @pytest.mark.parametrize(
        "body",
        [{"mcp": None}, {"cli": None}, {"mcp": None, "cli": None}, {}],
        ids=["mcp-null", "cli-null", "both-null", "empty"],
    )
    def test_null_or_missing_counts_as_neither(self, body: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match=r"bindings\.chat .*neither"):
            Bindings.model_validate({"chat": {"system": "slack", **body}})

    def test_mcp_null_beside_cli_loads(self) -> None:
        bindings = Bindings.model_validate(
            {"tickets": {"system": "github-issues", "cli": "gh", "mcp": None}}
        )
        assert bindings.tickets is not None
        assert bindings.tickets.cli == "gh"

    def test_cli_null_beside_mcp_loads(self) -> None:
        bindings = Bindings.model_validate({"tickets": {**_LINEAR_MCP, "cli": None}})
        assert bindings.tickets is not None
        assert bindings.tickets.cli is None

    @pytest.mark.parametrize(
        "body",
        [{"mcp": {}}, {"mcp": {}, "cli": "gh", "system": "github-issues"}],
        ids=["alone", "beside-cli"],
    )
    def test_empty_mcp_is_rejected(self, body: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match=r"bindings\.tickets\.mcp is empty"):
            Bindings.model_validate({"tickets": body})

    @pytest.mark.parametrize("harness", ["claude-native", "codex", "opencode"])
    def test_known_harness_keys_load(self, harness: str) -> None:
        realization = (
            {"plugin_id": "linear@acme"}
            if harness == CLAUDE_NATIVE_BACKEND
            else {"server": "linear"}
        )
        bindings = Bindings.model_validate(
            {"knowledge": {"mcp": {harness: realization}}}
        )
        assert bindings.knowledge is not None

    def test_unknown_harness_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cursor"):
            Bindings.model_validate(
                {"knowledge": {"mcp": {"cursor": {"server": "notion"}}}}
            )

    def test_local_harness_is_rejected_with_no_mcp(self) -> None:
        with pytest.raises(ValidationError, match="local backend has no MCP"):
            Bindings.model_validate(
                {"knowledge": {"mcp": {"local": {"server": "notion"}}}}
            )

    def test_server_and_plugin_id_together_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of server or plugin_id"):
            McpRealization.model_validate({"server": "x", "plugin_id": "y"})

    def test_neither_server_nor_plugin_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of server or plugin_id"):
            McpRealization.model_validate({})

    @pytest.mark.parametrize("server", ["", "   "])
    def test_blank_server_is_rejected(self, server: str) -> None:
        with pytest.raises(ValidationError, match="server is blank"):
            McpRealization.model_validate({"server": server})

    def test_plugin_id_off_claude_native_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"plugin_id .*claude-native"):
            Bindings.model_validate(
                {"tickets": {"mcp": {"codex": {"plugin_id": "linear@acme"}}}}
            )

    def test_tools_omitted_is_valid(self) -> None:
        assert McpRealization.model_validate({"server": "linear"}).tools is None

    def test_legacy_blank_plugin_id_stays_loadable(self) -> None:
        assert McpRealization.model_validate({"plugin_id": ""}).plugin_id == ""

    def test_role_binding_instance_skips_the_cli_echo(self) -> None:
        """A pre-built binding is not a dict, so the before-validator skips it."""
        binding = RoleBinding.model_validate({"system": "github", "cli": "gh"})
        assert Bindings(code_host=binding).code_host is binding


class TestTools:
    def test_ticket_ops_are_accepted(self) -> None:
        tools = {op.value: f"linear_{op.value}" for op in TicketOp}
        bindings = Bindings.model_validate(
            {"tickets": {"mcp": {"codex": {"server": "linear", "tools": tools}}}}
        )
        assert bindings.tickets is not None
        assert bindings.tickets.mcp is not None
        realization = bindings.tickets.mcp[Harness.CODEX]
        assert realization.tools is not None
        assert realization.tools[TicketOp.CLOSE] == "linear_close"

    def test_unknown_op_key_is_rejected_by_name(self) -> None:
        with pytest.raises(ValidationError, match="reopen"):
            McpRealization.model_validate(
                {"server": "linear", "tools": {"reopen": "linear_reopen"}}
            )

    @pytest.mark.parametrize("value", ["", "  ", "get issue", "get;rm", "get\n"])
    def test_bad_tool_values_are_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError, match=r"tools\[get\]"):
            McpRealization.model_validate({"server": "linear", "tools": {"get": value}})

    @pytest.mark.parametrize("role", ["knowledge", "chat", "code_host"])
    def test_tools_off_tickets_are_rejected(self, role: str) -> None:
        with pytest.raises(ValidationError, match="defined by an RFC amendment"):
            Bindings.model_validate(
                {
                    role: {
                        "system": "x",
                        "mcp": {"codex": {"server": "s", "tools": {"get": "t"}}},
                    }
                }
            )

    def test_tools_absent_after_migration(self) -> None:
        client = _client(tracker_mcp_gate={"servers": {"codex": "linear"}})
        assert client.bindings.tickets is not None
        assert client.bindings.tickets.mcp is not None
        assert client.bindings.tickets.mcp[Harness.CODEX].tools is None


class TestCli:
    def test_cli_gh_on_tickets_with_github_issues(self) -> None:
        bindings = Bindings.model_validate(
            {"tickets": {"system": "github-issues", "cli": "gh"}}
        )
        assert bindings.tickets is not None
        assert bindings.tickets.cli == "gh"

    def test_cli_gh_on_code_host_with_github(self) -> None:
        bindings = Bindings.model_validate(
            {"code_host": {"system": "github", "cli": "gh"}}
        )
        assert bindings.code_host is not None

    def test_cli_gh_with_linear_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires system 'github-issues'"):
            Bindings.model_validate({"tickets": {"system": "linear", "cli": "gh"}})

    @pytest.mark.parametrize("role", ["tickets", "code_host"])
    def test_cli_gh_without_system_is_rejected(self, role: str) -> None:
        with pytest.raises(ValidationError, match=r"cli: gh requires system"):
            Bindings.model_validate({role: {"cli": "gh"}})

    def test_cli_gh_with_wrong_code_host_system_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires system 'github'"):
            Bindings.model_validate({"code_host": {"system": "gitlab", "cli": "gh"}})

    @pytest.mark.parametrize(
        "value",
        [["gh", "issue"], {"cmd": "gh"}, "gh issue comment", "linear", "gh "],
        ids=["list", "dict", "argv-string", "linear", "trailing-space"],
    )
    @pytest.mark.parametrize("role", ["tickets", "code_host"])
    def test_argv_templates_are_rejected(self, role: str, value: object) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Bindings.model_validate({role: {"system": "github", "cli": value}})
        text = str(excinfo.value)
        assert f"bindings.{role}.cli must be 'gh'" in text
        assert repr(value) in text

    @pytest.mark.parametrize("role", ["knowledge", "chat"])
    def test_cli_on_knowledge_and_chat_is_rejected(self, role: str) -> None:
        with pytest.raises(ValidationError, match=rf"bindings\.{role}\.cli is not"):
            Bindings.model_validate({role: {"system": "github", "cli": "gh"}})

    @pytest.mark.parametrize("field", ["operator_ids", "preflight"])
    @pytest.mark.parametrize("role", ["knowledge", "chat", "code_host"])
    def test_ticket_only_fields_are_unknown_elsewhere(
        self, role: str, field: str
    ) -> None:
        with pytest.raises(ValidationError, match=field):
            Bindings.model_validate(
                {role: {"system": "x", "mcp": {"codex": {"server": "s"}}, field: []}}
            )


class TestOperatorIds:
    def test_default_is_empty(self) -> None:
        assert TicketsBinding.model_validate(_LINEAR_MCP).operator_ids == []

    def test_valid_ids_load(self) -> None:
        binding = TicketsBinding.model_validate(
            {**_LINEAR_MCP, "operator_ids": ["U1", "octocat"]}
        )
        assert binding.operator_ids == ["U1", "octocat"]

    def test_null_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="operator_ids"):
            TicketsBinding.model_validate({**_LINEAR_MCP, "operator_ids": None})

    @pytest.mark.parametrize("entry", ["", "   "])
    def test_blank_entries_are_rejected(self, entry: str) -> None:
        with pytest.raises(ValidationError, match="operator_ids entry"):
            TicketsBinding.model_validate({**_LINEAR_MCP, "operator_ids": [entry]})

    def test_entries_are_preserved_exactly(self) -> None:
        binding = TicketsBinding.model_validate(
            {**_LINEAR_MCP, "operator_ids": ["U1", "u1", "U1"]}
        )
        assert binding.operator_ids == ["U1", "u1", "U1"]

    def test_allowed_beside_cli(self) -> None:
        bindings = Bindings.model_validate(
            {
                "tickets": {
                    "system": "github-issues",
                    "cli": "gh",
                    "operator_ids": ["octocat"],
                }
            }
        )
        assert bindings.tickets is not None
        assert bindings.tickets.operator_ids == ["octocat"]


class TestPreflight:
    def test_default_preflight(self) -> None:
        binding = TicketsBinding.model_validate(_LINEAR_MCP)
        assert binding.preflight == BindingPreflight(
            enabled=False, settings_path=".claude/settings.json"
        )

    def test_settings_path_is_opaque(self) -> None:
        binding = TicketsBinding.model_validate(
            {**_LINEAR_MCP, "preflight": {"settings_path": "../ odd path//x"}}
        )
        assert binding.preflight.settings_path == "../ odd path//x"


class TestSystem:
    @pytest.mark.parametrize("system", ["github-issues", "linear"])
    def test_tickets_systems_accepted(self, system: str) -> None:
        binding = TicketsBinding.model_validate(
            {"system": system, "mcp": {"codex": {"server": "s"}}}
        )
        assert binding.system == system

    def test_unknown_tickets_system_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="jira"):
            TicketsBinding.model_validate(
                {"system": "jira", "mcp": {"codex": {"server": "s"}}}
            )

    def test_system_omitted_is_none(self) -> None:
        binding = TicketsBinding.model_validate({"mcp": {"codex": {"server": "s"}}})
        assert binding.system is None

    def test_values_track_the_tracker_constants(self) -> None:
        assert TicketsSystem.GITHUB_ISSUES == TRACKER_GITHUB_ISSUES
        assert TicketsSystem.LINEAR == TRACKER_LINEAR

    @pytest.mark.parametrize("system", ["", "  "])
    def test_blank_free_form_system_is_rejected(self, system: str) -> None:
        with pytest.raises(ValidationError, match=r"bindings\.code_host\.system"):
            Bindings.model_validate(
                {"code_host": {"system": system, "mcp": {"codex": {"server": "s"}}}}
            )


class TestHarnessConstants:
    def test_members_track_the_backend_constants(self) -> None:
        assert Harness.CLAUDE_NATIVE == CLAUDE_NATIVE_BACKEND
        assert Harness.LOCAL == LOCAL_BACKEND
        assert Harness.CODEX == CODEX_BACKEND
        assert Harness.OPENCODE == OPENCODE_BACKEND


class TestLegacyMigration:
    def test_legacy_dict_migrates_and_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        client = _client(
            tracker_mcp_gate={
                "enabled": True,
                "plugin_id": "linear@acme",
                "settings_path": ".claude/s.json",
            }
        )
        tickets = client.bindings.tickets
        assert tickets is not None
        assert tickets.system is None
        assert tickets.claude_plugin_id == "linear@acme"
        assert tickets.preflight == BindingPreflight(
            enabled=True, settings_path=".claude/s.json"
        )
        assert len(_legacy_warnings(caplog)) == 1

    def test_legacy_instance_migrates(self) -> None:
        client = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            tracker_mcp_gate=TrackerMcpGateConfig(servers={"opencode": "linear"}),
        )
        assert client.tracker_mcp_server(OPENCODE_BACKEND) == "linear"
        assert client.tickets_preflight.enabled is False

    def test_bindings_only_loads_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        client = _client(bindings={"tickets": _LINEAR_MCP})
        assert client.tracker_mcp_server(CODEX_BACKEND) == "linear"
        assert _legacy_warnings(caplog) == []

    def test_neither_has_no_tickets_binding(self) -> None:
        assert _client().bindings.tickets is None

    def test_both_set_is_rejected_naming_both(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _client(
                tracker_mcp_gate={"plugin_id": "linear@acme"},
                bindings={"tickets": _LINEAR_MCP},
            )
        assert "tracker_mcp_gate" in str(excinfo.value)
        assert "bindings.tickets" in str(excinfo.value)

    def test_both_set_with_bindings_instance_is_rejected(self) -> None:
        bindings = Bindings.model_validate({"tickets": _LINEAR_MCP})
        with pytest.raises(ValidationError, match=r"bindings\.tickets"):
            ClientConfig(
                name="acme",
                workspace_path=Path("/tmp/acme"),
                tracker_mcp_gate=TrackerMcpGateConfig(plugin_id="linear@acme"),
                bindings=bindings,
            )

    def test_legacy_null_beside_bindings_tickets_is_fine(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        client = _client(tracker_mcp_gate=None, bindings={"tickets": _LINEAR_MCP})
        assert client.bindings.tickets is not None
        assert _legacy_warnings(caplog) == []

    def test_legacy_merges_into_bindings_without_tickets(self) -> None:
        client = _client(
            tracker_mcp_gate={"plugin_id": "linear@acme"},
            bindings={"code_host": {"system": "github", "cli": "gh"}},
        )
        assert client.bindings.code_host is not None
        assert client.tickets_plugin_id == "linear@acme"

    def test_legacy_merges_into_bindings_instance(self) -> None:
        bindings = Bindings.model_validate(
            {"code_host": {"system": "github", "cli": "gh"}}
        )
        client = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            tracker_mcp_gate=TrackerMcpGateConfig(plugin_id="linear@acme"),
            bindings=bindings,
        )
        assert client.bindings.code_host == bindings.code_host
        assert client.tickets_plugin_id == "linear@acme"

    def test_legacy_beside_malformed_bindings_fails_on_bindings(self) -> None:
        with pytest.raises(ValidationError, match="bindings"):
            _client(tracker_mcp_gate={"plugin_id": "x"}, bindings="tickets")

    def test_invalid_legacy_gate_keeps_its_message(self) -> None:
        with pytest.raises(ValidationError, match="needs plugin_id or servers"):
            _client(tracker_mcp_gate={"enabled": True})

    def test_legacy_dict_is_not_mutated(self) -> None:
        data: dict[str, object] = {
            "name": "acme",
            "workspace_path": "/tmp/acme",
            "tracker_mcp_gate": {"plugin_id": "p", "servers": {"codex": "linear"}},
        }
        snapshot = copy.deepcopy(data)
        ClientConfig.model_validate(data)
        assert data == snapshot

    def test_plugin_and_servers_map_to_three_realizations(self) -> None:
        client = _client(
            tracker_mcp_gate={
                "plugin_id": "linear@acme",
                "servers": {"codex": "linear-c", "opencode": "linear-o"},
            }
        )
        tickets = client.bindings.tickets
        assert tickets is not None
        assert tickets.mcp == {
            Harness.CLAUDE_NATIVE: McpRealization(plugin_id="linear@acme"),
            Harness.CODEX: McpRealization(server="linear-c"),
            Harness.OPENCODE: McpRealization(server="linear-o"),
        }

    def test_servers_only_has_no_claude_native(self) -> None:
        client = _client(tracker_mcp_gate={"servers": {"codex": "linear"}})
        tickets = client.bindings.tickets
        assert tickets is not None
        assert tickets.mcp is not None
        assert Harness.CLAUDE_NATIVE not in tickets.mcp
        assert client.tickets_plugin_id is None

    def test_non_dict_input_is_returned_untouched(self) -> None:
        sentinel = object()
        assert ClientConfig._migrate_tracker_mcp_gate(sentinel) is sentinel


class TestWarnOnce:
    def test_once_per_client_name(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        legacy = {"plugin_id": "linear@acme"}
        _client(tracker_mcp_gate=legacy)
        _client(tracker_mcp_gate=legacy)
        assert len(_legacy_warnings(caplog)) == 1
        ClientConfig.model_validate(
            {"name": "beta", "workspace_path": "/tmp/b", "tracker_mcp_gate": legacy}
        )
        messages = _legacy_warnings(caplog)
        assert len(messages) == 2
        for message, name in zip(messages, ("acme", "beta"), strict=True):
            assert "tracker_mcp_gate" in message
            assert "bindings.tickets" in message
            assert f"client {name}:" in message

    def test_nameless_dict_warns_under_the_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        data = {"tracker_mcp_gate": {"plugin_id": "linear@acme"}}
        ClientConfig._migrate_tracker_mcp_gate(data)
        ClientConfig._migrate_tracker_mcp_gate(data)
        messages = _legacy_warnings(caplog)
        assert len(messages) == 1
        assert "<unnamed>" in messages[0]

    def test_conflict_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with pytest.raises(ValidationError):
            _client(
                tracker_mcp_gate={"plugin_id": "p"},
                bindings={"tickets": _LINEAR_MCP},
            )
        assert _legacy_warnings(caplog) == []


class TestResolverAccessors:
    def _gated(self, *, enabled: bool) -> ClientConfig:
        return _client(
            tracker_mcp_gate={
                "enabled": enabled,
                "plugin_id": "linear@acme",
                "servers": {"codex": "linear-c", "opencode": "linear-o"},
            }
        )

    @pytest.mark.parametrize("enabled", [True, False])
    def test_tracker_mcp_server(self, *, enabled: bool) -> None:
        client = self._gated(enabled=enabled)
        assert client.tracker_mcp_server(CODEX_BACKEND) == "linear-c"
        assert client.tracker_mcp_server(OPENCODE_BACKEND) == "linear-o"
        assert client.tracker_mcp_server(CLAUDE_NATIVE_BACKEND) is None
        assert client.tracker_mcp_server(LOCAL_BACKEND) is None
        assert client.tracker_mcp_server("bogus") is None

    def test_plugin_id_and_preflight(self) -> None:
        client = self._gated(enabled=True)
        assert client.tickets_plugin_id == "linear@acme"
        assert client.tickets_preflight.enabled is True
        assert client.tickets_preflight.settings_path == ".claude/settings.json"

    def test_defaults_without_a_tickets_binding(self) -> None:
        client = _client()
        assert client.tracker_mcp_server(CODEX_BACKEND) is None
        assert client.tickets_plugin_id is None
        assert client.tickets_preflight == BindingPreflight()

    def test_cli_binding_has_no_realization(self) -> None:
        client = _client(bindings={"tickets": {"system": "github-issues", "cli": "gh"}})
        assert client.tracker_mcp_server(CODEX_BACKEND) is None
        assert client.tickets_plugin_id is None


class TestDumpRoundTrip:
    def test_exclude_unset_round_trip(self, caplog: pytest.LogCaptureFixture) -> None:
        original = _client(tracker_mcp_gate={"enabled": True, "plugin_id": "p"})
        caplog.clear()
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        data = original.model_dump(mode="json", exclude_unset=True)
        assert "tracker_mcp_gate" not in data
        assert ClientConfig.model_validate(data) == original
        assert _legacy_warnings(caplog) == []

    @pytest.mark.parametrize(
        "bindings",
        [
            {"tickets": _LINEAR_MCP},
            {"tickets": {"system": "github-issues", "cli": "gh"}},
            {
                "code_host": {"system": "github", "cli": "gh"},
                "knowledge": {
                    "system": "notion",
                    "mcp": {"claude-native": {"plugin_id": "notion@acme"}},
                },
            },
        ],
        ids=["tickets-mcp", "tickets-cli", "code-host-and-knowledge"],
    )
    def test_full_dump_round_trip(self, bindings: dict[str, object]) -> None:
        original = _client(bindings=bindings)
        assert ClientConfig.model_validate(original.model_dump(mode="json")) == original

    def test_full_dump_of_migrated_client_does_not_rewarn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = _client(tracker_mcp_gate={"servers": {"codex": "linear"}})
        caplog.clear()
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        restored = ClientConfig.model_validate(original.model_dump(mode="json"))
        assert restored == original
        assert _legacy_warnings(caplog) == []


class TestSchema:
    def test_json_schema_builds(self) -> None:
        schema = Bindings.model_json_schema()
        assert "tickets" in schema["properties"]

    @pytest.mark.parametrize("fmt", ["tldr", "json"])
    def test_schema_show(self, fmt: str) -> None:
        result = CliRunner().invoke(
            main, ["schema", "show", "bindings", f"--format={fmt}"]
        )
        assert result.exit_code == 0, result.output
        assert "tickets" in result.output
        if fmt == "json":
            assert "properties" in json.loads(result.output)


class TestBoundaryGuard:
    @pytest.mark.parametrize(
        "rel", ["src/cw/models/bindings.py", "src/cw/doctor/bindings.py"]
    )
    def test_no_gh_argv_literal(self, rel: str) -> None:
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert _direct_gh_call_linenos(tree) == []


class TestTicketsToolName:
    """``TicketsBinding.tool_name``: the binding's tool for an op, or None (#3703)."""

    @staticmethod
    def _tickets(mcp: dict[str, object]) -> TicketsBinding:
        bindings = Bindings.model_validate({"tickets": {"mcp": mcp}})
        assert bindings.tickets is not None
        return bindings.tickets

    def test_a_named_tool_is_returned(self) -> None:
        tickets = self._tickets(
            {"codex": {"server": "linear", "tools": {"get": "linear_get_issue"}}}
        )
        assert tickets.tool_name("codex", TicketOp.GET) == "linear_get_issue"

    def test_an_unnamed_op_returns_none(self) -> None:
        tickets = self._tickets(
            {"codex": {"server": "linear", "tools": {"get": "linear_get_issue"}}}
        )
        assert tickets.tool_name("codex", TicketOp.COMMENT) is None

    def test_a_harness_without_a_realization_returns_none(self) -> None:
        tickets = self._tickets(
            {"codex": {"server": "linear", "tools": {"get": "linear_get_issue"}}}
        )
        assert tickets.tool_name("opencode", TicketOp.GET) is None

    def test_a_realization_without_tools_returns_none(self) -> None:
        tickets = self._tickets({"codex": {"server": "linear"}})
        assert tickets.tool_name("codex", TicketOp.GET) is None
