"""Tests for cw.codex_review._profile — the lean, cw-owned codex reviewer
profile argv block and its per-session diagnostics artifact (#1711)."""

from __future__ import annotations

import json

import pytest

from cw.codex_review import (
    _CODEX_DEFAULT_ENABLED_FEATURES_0_147_0,
    _CODEX_FEATURES_0_147_0,
    _DISABLED_FEATURES,
    _LEAN_PROFILE_FEATURE_DENYLIST,
    _PROFILE_DIAGNOSTICS_FILENAME,
    _PROFILE_VERSION,
    _SUPPORTED_CODEX_CLI_VERSION,
    _InstructionSource,
    _lean_profile_argv,
    _persist_profile_diagnostics,
    _ProfileDiagnostics,
    _validate_runtime_profile,
)
from cw.config import diagnostics_dir
from tests._codex_review_helpers import _config_override_values

# Captured verbatim identifier/status/default columns from
# ``codex-cli 0.147.0 features list``. Keeping the capture in the contract test
# makes an inventory edit explain which CLI output superseded it.
_FEATURES_LIST_0_147_0 = """\
apply_patch_freeform removed false
apply_patch_streaming_events under development false
apps stable true
apps_mcp_path_override removed false
artifact under development false
auth_elicitation stable true
browser_use stable true
browser_use_external stable true
browser_use_full_cdp_access stable true
chronicle under development false
code_mode under development false
code_mode_buffered_exec under development false
code_mode_host stable true
code_mode_only under development false
codex_git_commit removed false
collaboration_modes removed true
computer_use stable true
concurrent_reasoning_summaries under development false
current_time_reminder under development false
default_mode_request_user_input under development false
deferred_executor under development false
deferred_tool_world_state under development false
elevated_windows_sandbox removed false
enable_fanout removed false
enable_mcp_apps under development false
enable_request_compression stable true
exec_permission_approvals under development false
executed_tool_call_metadata under development false
executor_capability_discovery under development false
experimental_windows_sandbox removed false
external_agent_memory_import under development false
external_migration removed false
fast_mode stable true
goals stable true
guardian_approval stable true
guardianv2 under development false
hooks stable true
image_detail_original removed false
image_generation stable true
image_resize_notice under development false
in_app_browser stable true
in_app_updates stable true
item_ids removed true
js_repl removed false
js_repl_tools_only removed false
local_thread_store_compression under development false
mcp_2026_07_28 under development false
memories stable false
mentions_v2 stable true
multi_agent stable true
multi_agent_mode removed false
multi_agent_v2 stable false
network_proxy experimental false
non_prefixed_mcp_tool_names under development false
personality stable true
plugin_hooks removed false
plugin_sharing stable true
plugins stable true
prevent_idle_sleep experimental false
realtime_conversation under development false
recommended_plugins stable false
remote_compaction_v2 stable true
remote_control removed false
remote_models removed false
remote_plugin stable true
request_permissions_tool under development false
request_rule removed false
resize_all_images removed true
respect_system_proxy under development false
responses_websockets removed false
responses_websockets_v2 removed false
rollout_budget under development false
runtime_metrics under development false
search_tool removed false
secret_auth_storage stable false
shell_snapshot stable true
shell_tool stable true
shell_zsh_fork under development false
skill_env_var_dependency_prompt removed false
skill_mcp_dependency_install stable true
skill_search stable true
sqlite removed true
standalone_web_search under development false
steer removed true
terminal_resize_reflow removed true
terminal_visualization_instructions under development false
token_budget under development false
tool_call_mcp_elicitation stable true
tool_search removed false
tool_search_always_defer_mcp_tools removed true
tool_suggest stable true
tui_app_server removed true
unavailable_dummy_tools removed false
undo removed false
unified_exec stable true
unified_exec_zsh_fork under development false
use_agent_identity under development false
use_legacy_landlock deprecated false
use_linux_sandbox_bwrap removed false
view_image stable true
web_search_cached deprecated false
web_search_request deprecated false
workspace_dependencies stable true
workspace_owner_usage_nudge removed false
"""


def _disabled(argv: list[str]) -> list[str]:
    """Return every ``--disable <feature>`` value in *argv*, in order."""
    return [
        argv[i + 1]
        for i, tok in enumerate(argv)
        if tok == "--disable" and i + 1 < len(argv)
    ]


# ---------------------------------------------------------------------------
# _DISABLED_FEATURES
# ---------------------------------------------------------------------------


class TestDisabledFeatures:
    def test_versioned_inventory_matches_captured_features_list(self) -> None:
        captured_names = tuple(
            line.split()[0] for line in _FEATURES_LIST_0_147_0.splitlines()
        )
        assert captured_names == _CODEX_FEATURES_0_147_0

    def test_disabled_features_are_explicit_denylist(self) -> None:
        assert _DISABLED_FEATURES == _LEAN_PROFILE_FEATURE_DENYLIST
        assert set(_DISABLED_FEATURES) < set(_CODEX_FEATURES_0_147_0)

    def test_captured_defaults_match_full_feature_fixture(self) -> None:
        captured_enabled = frozenset(
            line.split()[0]
            for line in _FEATURES_LIST_0_147_0.splitlines()
            if line.split()[-1] == "true"
        )
        assert captured_enabled == _CODEX_DEFAULT_ENABLED_FEATURES_0_147_0

    def test_mcp_servers_is_not_a_feature(self) -> None:
        # `--disable <feature>` is `-c features.<name>=false` sugar, and MCP
        # servers are not a member of codex's `features` list — they can only
        # be silenced by a direct `-c mcp_servers={}` override.
        assert "mcp_servers" not in _DISABLED_FEATURES
        assert "mcp" not in _DISABLED_FEATURES


# ---------------------------------------------------------------------------
# _lean_profile_argv
# ---------------------------------------------------------------------------


class TestLeanProfileArgv:
    def test_profile_version_tracks_ignore_rules_addition(self) -> None:
        assert _PROFILE_VERSION == 2

    @pytest.mark.parametrize("effort", [None, "medium", "high"])
    def test_unconditional_flags_always_present(self, effort: str | None) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert "--ignore-user-config" in argv
        assert "--ignore-rules" in argv
        assert "--strict-config" in argv
        overrides = _config_override_values(argv)
        assert "project_doc_max_bytes=0" in overrides
        assert "mcp_servers={}" in overrides

    @pytest.mark.parametrize("effort", [None, "medium", "high"])
    def test_all_eleven_features_disabled(self, effort: str | None) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert _disabled(argv) == list(_LEAN_PROFILE_FEATURE_DENYLIST)

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_reasoning_effort_override_emitted(self, effort: str) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert f"model_reasoning_effort={effort}" in _config_override_values(argv)

    def test_no_reasoning_effort_override_when_none(self) -> None:
        argv = _lean_profile_argv(reasoning_effort=None)
        assert not any(
            override.startswith("model_reasoning_effort=")
            for override in _config_override_values(argv)
        )

    def test_every_config_override_is_paired(self) -> None:
        # A trailing bare "-c" would be silently accepted by the membership
        # assertions above but rejected by codex at runtime.
        argv = _lean_profile_argv(reasoning_effort="high")
        assert argv[-1] != "-c"
        assert argv[-1] != "--disable"


# ---------------------------------------------------------------------------
# _ProfileDiagnostics / _persist_profile_diagnostics
# ---------------------------------------------------------------------------


class TestPersistProfileDiagnostics:
    def test_writes_all_six_fields(self) -> None:
        _persist_profile_diagnostics(
            session_id="sess-profile",
            model="gpt-5",
            reasoning_effort="high",
            cli_version="0.147.0",
            instruction_sources=[
                _InstructionSource.ROLE_SPEC,
                _InstructionSource.APPROVED_PLAN,
            ],
        )
        path = diagnostics_dir("sess-profile") / _PROFILE_DIAGNOSTICS_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profile_version"] == _PROFILE_VERSION
        assert data["reasoning_effort"] == "high"
        assert data["effective_model"] == "gpt-5"
        assert data["cli_version"] == "0.147.0"
        assert data["enabled_tool_classes"] == [
            feature
            for feature in _CODEX_FEATURES_0_147_0
            if feature in _CODEX_DEFAULT_ENABLED_FEATURES_0_147_0
            and feature not in _DISABLED_FEATURES
        ]
        assert data["instruction_sources"] == ["role_spec", "approved_plan"]

    def test_empty_instruction_sources_round_trips(self) -> None:
        _persist_profile_diagnostics(
            session_id="sess-empty",
            model=None,
            reasoning_effort=None,
            cli_version=None,
            instruction_sources=[],
        )
        path = diagnostics_dir("sess-empty") / _PROFILE_DIAGNOSTICS_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["instruction_sources"] == []
        assert data["effective_model"] is None
        assert data["reasoning_effort"] is None
        assert data["cli_version"] is None

    def test_never_raises_on_oserror(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        message = "disk full"

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(message)

        monkeypatch.setattr("pathlib.Path.write_text", _boom)
        with caplog.at_level("WARNING"):
            _persist_profile_diagnostics(
                session_id="sess-boom",
                model=None,
                reasoning_effort="high",
                cli_version=None,
                instruction_sources=[],
            )
        assert "profile diagnostics write failed" in caplog.text

    def test_model_is_serializable(self) -> None:
        diag = _ProfileDiagnostics(
            profile_version=_PROFILE_VERSION,
            reasoning_effort="medium",
            effective_model=None,
            cli_version=None,
            enabled_tool_classes=[],
            instruction_sources=[_InstructionSource.TICKET_CONTEXT],
        )
        assert diag.model_dump(mode="json")["instruction_sources"] == ["ticket_context"]


class TestValidateRuntimeProfile:
    def test_accepts_supported_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.codex_review._profile._capability.probe_codex_cli_version",
            lambda: _SUPPORTED_CODEX_CLI_VERSION,
        )
        assert _validate_runtime_profile() == _SUPPORTED_CODEX_CLI_VERSION

    @pytest.mark.parametrize("version", [None, "0.146.0", "0.148.0"])
    def test_rejects_unknown_or_mismatched_version(
        self, monkeypatch: pytest.MonkeyPatch, version: str | None
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._profile._capability.probe_codex_cli_version",
            lambda: version,
        )
        with pytest.raises(RuntimeError, match="unsupported codex CLI version"):
            _validate_runtime_profile()


# ---------------------------------------------------------------------------
# StageExecutorConfig.reasoning_effort (resolution default tier)
# ---------------------------------------------------------------------------


class TestStageExecutorConfigField:
    def test_defaults_to_high(self) -> None:
        from cw.models import StageExecutorConfig

        assert StageExecutorConfig().reasoning_effort == "high"

    def test_explicit_none_is_allowed(self) -> None:
        from cw.models import StageExecutorConfig

        assert StageExecutorConfig(reasoning_effort=None).reasoning_effort is None
