"""Tests for cw.codex_review._profile — the lean, cw-owned codex reviewer
profile argv block and its per-session diagnostics artifact (#1711)."""

from __future__ import annotations

import json

import pytest

from cw.codex_review import (
    _DISABLED_FEATURES,
    _PROFILE_DIAGNOSTICS_FILENAME,
    _PROFILE_VERSION,
    _lean_profile_argv,
    _persist_profile_diagnostics,
    _ProfileDiagnostics,
)
from cw.config import diagnostics_dir

# The 11 identifiers `codex features list` enumerates on codex-cli 0.147.0.
# Hardcoded here (not imported from the module under test) so a silent edit to
# _DISABLED_FEATURES fails this test instead of moving with it.
_EXPECTED_FEATURES = (
    "hooks",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "plugins",
    "plugin_sharing",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "personality",
)


def _config_overrides(argv: list[str]) -> list[str]:
    """Return every ``-c <override>`` value in *argv*, in order.

    ``-c key=value`` is two argv tokens, so a bare ``"key=value" in argv``
    membership check would pass on an override that lost its ``-c`` flag.
    """
    return [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "-c" and i + 1 < len(argv)
    ]


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
    def test_exactly_the_eleven_enumerated_features(self) -> None:
        assert tuple(_DISABLED_FEATURES) == _EXPECTED_FEATURES

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
    @pytest.mark.parametrize("effort", [None, "medium", "high"])
    def test_unconditional_flags_always_present(self, effort: str | None) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert "--ignore-user-config" in argv
        assert "--strict-config" in argv
        overrides = _config_overrides(argv)
        assert "project_doc_max_bytes=0" in overrides
        assert "mcp_servers={}" in overrides

    @pytest.mark.parametrize("effort", [None, "medium", "high"])
    def test_all_eleven_features_disabled(self, effort: str | None) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert _disabled(argv) == list(_EXPECTED_FEATURES)

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_reasoning_effort_override_emitted(self, effort: str) -> None:
        argv = _lean_profile_argv(reasoning_effort=effort)
        assert f"model_reasoning_effort={effort}" in _config_overrides(argv)

    def test_no_reasoning_effort_override_when_none(self) -> None:
        argv = _lean_profile_argv(reasoning_effort=None)
        assert not any(
            override.startswith("model_reasoning_effort=")
            for override in _config_overrides(argv)
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
    def test_writes_all_six_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.codex_review._profile._capability._which_codex",
            lambda: "/usr/bin/codex",
        )
        monkeypatch.setattr(
            "cw.codex_review._profile._capability._codex_cli_version",
            lambda _resolved: "0.147.0",
        )
        _persist_profile_diagnostics(
            session_id="sess-profile",
            model="gpt-5",
            reasoning_effort="high",
            instruction_sources=["role_spec", "approved_plan"],
        )
        path = diagnostics_dir("sess-profile") / _PROFILE_DIAGNOSTICS_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profile_version"] == _PROFILE_VERSION
        assert data["reasoning_effort"] == "high"
        assert data["effective_model"] == "gpt-5"
        assert data["cli_version"] == "0.147.0"
        # Every candidate tool class is disabled today, so the honest
        # complement is the empty list.
        assert data["enabled_tool_classes"] == []
        assert data["instruction_sources"] == ["role_spec", "approved_plan"]

    def test_empty_instruction_sources_round_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._profile._capability._which_codex", lambda: None
        )
        _persist_profile_diagnostics(
            session_id="sess-empty",
            model=None,
            reasoning_effort=None,
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
        monkeypatch.setattr(
            "cw.codex_review._profile._capability._which_codex", lambda: None
        )

        message = "disk full"

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(message)

        monkeypatch.setattr("pathlib.Path.write_text", _boom)
        with caplog.at_level("WARNING"):
            _persist_profile_diagnostics(
                session_id="sess-boom",
                model=None,
                reasoning_effort="high",
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
            instruction_sources=["ticket_context"],
        )
        assert diag.model_dump(mode="json")["instruction_sources"] == ["ticket_context"]


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
