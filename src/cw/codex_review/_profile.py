"""The lean, cw-owned codex reviewer profile: argv block + diagnostics (#1711).

A ``codex exec`` invocation launched from cw must be reproducible across
operator machines. By default it is not: codex reads the operator's
``~/.codex/config.toml``, inlines the repo's ``AGENTS.md``/project doc, starts
whatever MCP servers the operator has configured, and enables an evolving set
of optional feature surfaces. None of that is an input cw chose, yet all of it
changes what a reviewer sees.

This module owns the one argv block that closes those channels, shared verbatim
by both codex argv builders — ``_roles._build_generic_codex_argv`` (the
reviewer path) and ``codex_fix_loop._build_fix_codex_argv`` (the fix path) — so
the two cannot drift onto different profiles for the same run.

It also owns ``codex-review-profile.json``, the per-session diagnostics
artifact answering "what profile did THIS review actually run under": the
resolved model and reasoning effort, the codex CLI version, which optional tool
classes survived the profile, and which instruction channels actually
contributed content to the prompts.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict

from cw.codex_review import _capability, _context
from cw.config import diagnostics_dir

_log = logging.getLogger(__name__)

# Bump when the argv block below changes shape, so a diagnostics artifact from
# an older run is not mistaken for one produced by the current profile.
_PROFILE_VERSION = 2

_PROFILE_DIAGNOSTICS_FILENAME = "codex-review-profile.json"

_SUPPORTED_CODEX_CLI_VERSION = "0.147.0"
_UNSUPPORTED_CODEX_CLI_VERSION_ERROR = (
    "unsupported codex CLI version for lean reviewer profile: expected {expected}, "
    "got {actual}"
)


class _LeanProfileDisposition(StrEnum):
    ALLOW = "allow"
    DISABLE = "disable"


class _CodexFeatureRecord(NamedTuple):
    name: str
    default_enabled: bool
    lean_profile_disposition: _LeanProfileDisposition


# Complete metadata for the CLI version against which this profile was
# verified. A newer Codex feature list must be reviewed and added as a new
# versioned collection instead of being silently treated as covered by this
# one. Inventory, defaults, and lean-profile disables are all derived below.
_CODEX_FEATURE_METADATA_0_147_0: tuple[_CodexFeatureRecord, ...] = (
    _CodexFeatureRecord("apply_patch_freeform", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "apply_patch_streaming_events", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("apps", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("apps_mcp_path_override", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("artifact", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("auth_elicitation", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("browser_use", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("browser_use_external", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord(
        "browser_use_full_cdp_access", True, _LeanProfileDisposition.DISABLE
    ),
    _CodexFeatureRecord("chronicle", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("code_mode", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "code_mode_buffered_exec", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("code_mode_host", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("code_mode_only", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("codex_git_commit", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("collaboration_modes", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("computer_use", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord(
        "concurrent_reasoning_summaries", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("current_time_reminder", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "default_mode_request_user_input", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("deferred_executor", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "deferred_tool_world_state", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "elevated_windows_sandbox", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("enable_fanout", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("enable_mcp_apps", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "enable_request_compression", True, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "exec_permission_approvals", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "executed_tool_call_metadata", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "executor_capability_discovery", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "experimental_windows_sandbox", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "external_agent_memory_import", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("external_migration", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("fast_mode", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("goals", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("guardian_approval", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("guardianv2", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("hooks", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("image_detail_original", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("image_generation", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("image_resize_notice", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("in_app_browser", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("in_app_updates", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("item_ids", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("js_repl", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("js_repl_tools_only", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "local_thread_store_compression", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("mcp_2026_07_28", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("memories", False, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("mentions_v2", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("multi_agent", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("multi_agent_mode", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("multi_agent_v2", False, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("network_proxy", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "non_prefixed_mcp_tool_names", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("personality", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("plugin_hooks", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("plugin_sharing", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("plugins", True, _LeanProfileDisposition.DISABLE),
    _CodexFeatureRecord("prevent_idle_sleep", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("realtime_conversation", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("recommended_plugins", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("remote_compaction_v2", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("remote_control", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("remote_models", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("remote_plugin", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "request_permissions_tool", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("request_rule", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("resize_all_images", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("respect_system_proxy", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("responses_websockets", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "responses_websockets_v2", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("rollout_budget", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("runtime_metrics", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("search_tool", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("secret_auth_storage", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("shell_snapshot", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("shell_tool", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("shell_zsh_fork", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "skill_env_var_dependency_prompt", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord(
        "skill_mcp_dependency_install", True, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("skill_search", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("sqlite", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("standalone_web_search", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("steer", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("terminal_resize_reflow", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "terminal_visualization_instructions", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("token_budget", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "tool_call_mcp_elicitation", True, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("tool_search", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "tool_search_always_defer_mcp_tools", True, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("tool_suggest", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("tui_app_server", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "unavailable_dummy_tools", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("undo", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("unified_exec", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("unified_exec_zsh_fork", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("use_agent_identity", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("use_legacy_landlock", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "use_linux_sandbox_bwrap", False, _LeanProfileDisposition.ALLOW
    ),
    _CodexFeatureRecord("view_image", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("web_search_cached", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("web_search_request", False, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord("workspace_dependencies", True, _LeanProfileDisposition.ALLOW),
    _CodexFeatureRecord(
        "workspace_owner_usage_nudge", False, _LeanProfileDisposition.ALLOW
    ),
)

_CODEX_FEATURES_0_147_0: tuple[str, ...] = tuple(
    feature.name for feature in _CODEX_FEATURE_METADATA_0_147_0
)
_CODEX_DEFAULT_ENABLED_FEATURES_0_147_0: frozenset[str] = frozenset(
    feature.name
    for feature in _CODEX_FEATURE_METADATA_0_147_0
    if feature.default_enabled
)
# The sole canonical lean-profile denylist; argv is generated from this value.
_DISABLED_FEATURES: tuple[str, ...] = tuple(
    feature.name
    for feature in _CODEX_FEATURE_METADATA_0_147_0
    if feature.lean_profile_disposition is _LeanProfileDisposition.DISABLE
)


def _lean_profile_argv(*, reasoning_effort: str | None) -> list[str]:
    """Return the lean-profile argv fragment shared by both codex builders.

    ``--ignore-user-config`` drops ``~/.codex/config.toml`` so the operator's
    personal codex setup cannot leak into a cw-owned review.

    ``--ignore-rules`` closes the separate execpolicy-rules channel so a local
    rules file cannot change which commands the same cw-owned invocation may
    run.

    ``--strict-config`` makes codex *reject* an unknown ``-c`` key instead of
    ignoring it, which is what makes the overrides below trustworthy rather
    than decorative. Verified against codex-cli 0.147.0: ``-c bogus_key_xyz=1``
    exits 1 with ``Error loading config.toml: unknown configuration field
    `bogus_key_xyz` in -c/--config override``. Every key emitted here was
    live-checked under this flag and accepted (EXIT=0).

    ``-c project_doc_max_bytes=0`` stops codex inlining the repo's own
    ``AGENTS.md``/project doc: cw already inlines every instruction the
    reviewer should see, and a second, unversioned instruction channel is the
    thing this profile exists to close.

    ``-c mcp_servers={}`` is a *separate* override rather than one of the
    ``--disable`` flags because MCP servers are not a codex "feature": the
    identifiers `codex features list` enumerates (see ``_DISABLED_FEATURES``)
    contain no ``mcp_servers``/``mcp`` entry, so ``--disable`` — being
    ``-c features.<name>=false`` sugar — structurally cannot target them. A
    direct config override is the only mechanism that can, and ticket AC3
    names MCP servers among what must be disabled.

    ``-c model_reasoning_effort=<effort>`` is emitted only when *effort* is
    not ``None``; ``None`` means "do not pin it", leaving codex's own default.
    """
    argv = [
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "mcp_servers={}",
    ]
    if reasoning_effort is not None:
        argv += ["-c", f"model_reasoning_effort={reasoning_effort}"]
    for feature in _DISABLED_FEATURES:
        argv += ["--disable", feature]
    return argv


class _ProfileDiagnostics(BaseModel):
    """What profile one review pass actually ran under (#1711 AC5)."""

    model_config = ConfigDict(extra="forbid")

    profile_version: int
    reasoning_effort: str | None
    effective_model: str | None
    cli_version: str | None
    # Features reported enabled by default by the supported CLI, excluding the
    # explicit lean-profile denylist.
    enabled_tool_classes: list[str]
    # Which prompt-instruction channels actually contributed content, unioned
    # across every role in the pass. None means the caller did not compute
    # provenance; [] means it computed that no channel fired. Vocabulary: role_spec,
    # output_format_supplement, ticket_context, approved_plan, project_rubrics,
    # repo_policy, lint_grounding, sensitive_files.
    instruction_sources: list[_context._InstructionSource] | None


def _enabled_tool_classes() -> list[str]:
    """Return supported-CLI defaults left enabled after the denylist."""
    return [
        name
        for name in _CODEX_FEATURES_0_147_0
        if name in _CODEX_DEFAULT_ENABLED_FEATURES_0_147_0
        and name not in _DISABLED_FEATURES
    ]


def _validate_runtime_profile() -> str:
    """Return the supported runtime version or fail before codex is launched."""
    cli_version = _capability.probe_codex_cli_version()
    if cli_version != _SUPPORTED_CODEX_CLI_VERSION:
        message = _UNSUPPORTED_CODEX_CLI_VERSION_ERROR.format(
            expected=_SUPPORTED_CODEX_CLI_VERSION,
            actual=cli_version or "unknown",
        )
        raise RuntimeError(message)
    return cli_version


def _persist_profile_diagnostics(
    *,
    session_id: str,
    model: str | None,
    reasoning_effort: str | None,
    cli_version: str | None,
    instruction_sources: list[_context._InstructionSource] | None,
) -> None:
    """Record this pass's profile under *session_id*'s diagnostics dir.

    Never raises: mirrors ``_capability._persist_capability_diagnostics``'s
    contract — a failed diagnostics write must not take the review down with
    it. Called once per ``run_codex_roles`` invocation (not once per role): the
    profile is a property of the pass, not of any single reviewer.
    """
    diagnostics = _ProfileDiagnostics(
        profile_version=_PROFILE_VERSION,
        reasoning_effort=reasoning_effort,
        effective_model=model,
        cli_version=cli_version,
        enabled_tool_classes=_enabled_tool_classes(),
        instruction_sources=(
            None if instruction_sources is None else list(instruction_sources)
        ),
    )
    _log.info(
        "codex_review_profile: session=%s model=%s reasoning_effort=%s sources=%s",
        session_id,
        diagnostics.effective_model,
        diagnostics.reasoning_effort,
        diagnostics.instruction_sources,
    )
    payload = diagnostics.model_dump(mode="json")
    payload["session_id"] = session_id
    payload["recorded_at"] = datetime.now(UTC).isoformat()
    try:
        target = diagnostics_dir(session_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / _PROFILE_DIAGNOSTICS_FILENAME).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        _log.warning(
            "codex review profile diagnostics write failed for session %s", session_id
        )
