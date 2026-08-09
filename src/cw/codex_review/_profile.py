"""The lean, cw-owned codex reviewer profile: argv block + diagnostics (#1711).

A ``codex exec`` invocation launched from cw must be reproducible across
operator machines. By default it is not: codex reads the operator's
``~/.codex/config.toml``, inlines the repo's ``AGENTS.md``/project doc, starts
whatever MCP servers the operator has configured, and enables an evolving set
of optional feature surfaces. None of that is an input cw chose, yet all of it
changes what a reviewer sees.

This module owns the argv block that closes those channels, shared by both
codex argv builders — ``_roles._build_generic_codex_argv`` (the reviewer path)
and ``codex_fix_loop._build_fix_codex_argv`` (the fix path). Fix invocations
retain project-doc discovery because, unlike reviewer prompts, their prompts do
not inline the repository's complete applicable instructions.

It also owns the per-session ``codex-review-profile.json`` diagnostics artifact
(plus pass-discriminated benchmark variants), answering "what profile did THIS
review actually run under": the resolved model and reasoning effort, the codex
CLI version, which optional tool classes survived the profile, and which
instruction channels actually contributed content to the prompts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cw.codex_review import _capability, _context
from cw.config import diagnostics_dir

_log = logging.getLogger(__name__)

# Bump when the argv block below changes shape, so a diagnostics artifact from
# an older run is not mistaken for one produced by the current profile.
_PROFILE_VERSION = 4

_PROFILE_DIAGNOSTICS_FILENAME = "codex-review-profile.json"


class _LeanProfileDisposition(StrEnum):
    ALLOW = "allow"
    DISABLE = "disable"


@dataclass(frozen=True, kw_only=True)
class _CodexFeatureRecord:
    name: str
    default_enabled: bool
    lean_profile_disposition: _LeanProfileDisposition = _LeanProfileDisposition.ALLOW


# Complete metadata for the CLI version against which this profile was
# verified. A newer Codex feature list must be reviewed and added as a new
# versioned collection instead of being silently treated as covered by this
# one. Inventory, defaults, and lean-profile disables are all derived below.
_CODEX_FEATURE_METADATA_0_147_0: tuple[_CodexFeatureRecord, ...] = (
    _CodexFeatureRecord(name="apply_patch_freeform", default_enabled=False),
    _CodexFeatureRecord(name="apply_patch_streaming_events", default_enabled=False),
    _CodexFeatureRecord(name="apps", default_enabled=True),
    _CodexFeatureRecord(name="apps_mcp_path_override", default_enabled=False),
    _CodexFeatureRecord(name="artifact", default_enabled=False),
    _CodexFeatureRecord(name="auth_elicitation", default_enabled=True),
    _CodexFeatureRecord(
        name="browser_use",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(
        name="browser_use_external",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(
        name="browser_use_full_cdp_access",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="chronicle", default_enabled=False),
    _CodexFeatureRecord(name="code_mode", default_enabled=False),
    _CodexFeatureRecord(name="code_mode_buffered_exec", default_enabled=False),
    _CodexFeatureRecord(name="code_mode_host", default_enabled=True),
    _CodexFeatureRecord(name="code_mode_only", default_enabled=False),
    _CodexFeatureRecord(name="codex_git_commit", default_enabled=False),
    _CodexFeatureRecord(name="collaboration_modes", default_enabled=True),
    _CodexFeatureRecord(
        name="computer_use",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="concurrent_reasoning_summaries", default_enabled=False),
    _CodexFeatureRecord(name="current_time_reminder", default_enabled=False),
    _CodexFeatureRecord(name="default_mode_request_user_input", default_enabled=False),
    _CodexFeatureRecord(name="deferred_executor", default_enabled=False),
    _CodexFeatureRecord(name="deferred_tool_world_state", default_enabled=False),
    _CodexFeatureRecord(name="elevated_windows_sandbox", default_enabled=False),
    _CodexFeatureRecord(name="enable_fanout", default_enabled=False),
    _CodexFeatureRecord(name="enable_mcp_apps", default_enabled=False),
    _CodexFeatureRecord(name="enable_request_compression", default_enabled=True),
    _CodexFeatureRecord(name="exec_permission_approvals", default_enabled=False),
    _CodexFeatureRecord(name="executed_tool_call_metadata", default_enabled=False),
    _CodexFeatureRecord(name="executor_capability_discovery", default_enabled=False),
    _CodexFeatureRecord(name="experimental_windows_sandbox", default_enabled=False),
    _CodexFeatureRecord(name="external_agent_memory_import", default_enabled=False),
    _CodexFeatureRecord(name="external_migration", default_enabled=False),
    _CodexFeatureRecord(name="fast_mode", default_enabled=True),
    _CodexFeatureRecord(name="goals", default_enabled=True),
    _CodexFeatureRecord(name="guardian_approval", default_enabled=True),
    _CodexFeatureRecord(name="guardianv2", default_enabled=False),
    _CodexFeatureRecord(
        name="hooks",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="image_detail_original", default_enabled=False),
    _CodexFeatureRecord(name="image_generation", default_enabled=True),
    _CodexFeatureRecord(name="image_resize_notice", default_enabled=False),
    _CodexFeatureRecord(name="in_app_browser", default_enabled=True),
    _CodexFeatureRecord(name="in_app_updates", default_enabled=True),
    _CodexFeatureRecord(name="item_ids", default_enabled=True),
    _CodexFeatureRecord(name="js_repl", default_enabled=False),
    _CodexFeatureRecord(name="js_repl_tools_only", default_enabled=False),
    _CodexFeatureRecord(name="local_thread_store_compression", default_enabled=False),
    _CodexFeatureRecord(name="mcp_2026_07_28", default_enabled=False),
    _CodexFeatureRecord(
        name="memories",
        default_enabled=False,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="mentions_v2", default_enabled=True),
    _CodexFeatureRecord(
        name="multi_agent",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="multi_agent_mode", default_enabled=False),
    _CodexFeatureRecord(
        name="multi_agent_v2",
        default_enabled=False,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="network_proxy", default_enabled=False),
    _CodexFeatureRecord(name="non_prefixed_mcp_tool_names", default_enabled=False),
    _CodexFeatureRecord(
        name="personality",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="plugin_hooks", default_enabled=False),
    _CodexFeatureRecord(
        name="plugin_sharing",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(
        name="plugins",
        default_enabled=True,
        lean_profile_disposition=_LeanProfileDisposition.DISABLE,
    ),
    _CodexFeatureRecord(name="prevent_idle_sleep", default_enabled=False),
    _CodexFeatureRecord(name="realtime_conversation", default_enabled=False),
    _CodexFeatureRecord(name="recommended_plugins", default_enabled=False),
    _CodexFeatureRecord(name="remote_compaction_v2", default_enabled=True),
    _CodexFeatureRecord(name="remote_control", default_enabled=False),
    _CodexFeatureRecord(name="remote_models", default_enabled=False),
    _CodexFeatureRecord(name="remote_plugin", default_enabled=True),
    _CodexFeatureRecord(name="request_permissions_tool", default_enabled=False),
    _CodexFeatureRecord(name="request_rule", default_enabled=False),
    _CodexFeatureRecord(name="resize_all_images", default_enabled=True),
    _CodexFeatureRecord(name="respect_system_proxy", default_enabled=False),
    _CodexFeatureRecord(name="responses_websockets", default_enabled=False),
    _CodexFeatureRecord(name="responses_websockets_v2", default_enabled=False),
    _CodexFeatureRecord(name="rollout_budget", default_enabled=False),
    _CodexFeatureRecord(name="runtime_metrics", default_enabled=False),
    _CodexFeatureRecord(name="search_tool", default_enabled=False),
    _CodexFeatureRecord(name="secret_auth_storage", default_enabled=False),
    _CodexFeatureRecord(name="shell_snapshot", default_enabled=True),
    _CodexFeatureRecord(name="shell_tool", default_enabled=True),
    _CodexFeatureRecord(name="shell_zsh_fork", default_enabled=False),
    _CodexFeatureRecord(name="skill_env_var_dependency_prompt", default_enabled=False),
    _CodexFeatureRecord(name="skill_mcp_dependency_install", default_enabled=True),
    _CodexFeatureRecord(name="skill_search", default_enabled=True),
    _CodexFeatureRecord(name="sqlite", default_enabled=True),
    _CodexFeatureRecord(name="standalone_web_search", default_enabled=False),
    _CodexFeatureRecord(name="steer", default_enabled=True),
    _CodexFeatureRecord(name="terminal_resize_reflow", default_enabled=True),
    _CodexFeatureRecord(
        name="terminal_visualization_instructions", default_enabled=False
    ),
    _CodexFeatureRecord(name="token_budget", default_enabled=False),
    _CodexFeatureRecord(name="tool_call_mcp_elicitation", default_enabled=True),
    _CodexFeatureRecord(name="tool_search", default_enabled=False),
    _CodexFeatureRecord(
        name="tool_search_always_defer_mcp_tools", default_enabled=True
    ),
    _CodexFeatureRecord(name="tool_suggest", default_enabled=True),
    _CodexFeatureRecord(name="tui_app_server", default_enabled=True),
    _CodexFeatureRecord(name="unavailable_dummy_tools", default_enabled=False),
    _CodexFeatureRecord(name="undo", default_enabled=False),
    _CodexFeatureRecord(name="unified_exec", default_enabled=True),
    _CodexFeatureRecord(name="unified_exec_zsh_fork", default_enabled=False),
    _CodexFeatureRecord(name="use_agent_identity", default_enabled=False),
    _CodexFeatureRecord(name="use_legacy_landlock", default_enabled=False),
    _CodexFeatureRecord(name="use_linux_sandbox_bwrap", default_enabled=False),
    _CodexFeatureRecord(name="view_image", default_enabled=True),
    _CodexFeatureRecord(name="web_search_cached", default_enabled=False),
    _CodexFeatureRecord(name="web_search_request", default_enabled=False),
    _CodexFeatureRecord(name="workspace_dependencies", default_enabled=True),
    _CodexFeatureRecord(name="workspace_owner_usage_nudge", default_enabled=False),
)

_CODEX_FEATURES_0_147_0: tuple[str, ...] = tuple(
    feature.name for feature in _CODEX_FEATURE_METADATA_0_147_0
)
_CODEX_DEFAULT_ENABLED_FEATURES_0_147_0: frozenset[str] = frozenset(
    feature.name
    for feature in _CODEX_FEATURE_METADATA_0_147_0
    if feature.default_enabled
)
_CODEX_FEATURE_METADATA_BY_CLI_VERSION: dict[str, tuple[_CodexFeatureRecord, ...]] = {
    "0.147.0": _CODEX_FEATURE_METADATA_0_147_0
}
# The sole canonical lean-profile denylist; argv is generated from this value.
_DISABLED_FEATURES: tuple[str, ...] = tuple(
    feature.name
    for feature in _CODEX_FEATURE_METADATA_0_147_0
    if feature.lean_profile_disposition is _LeanProfileDisposition.DISABLE
)


def _lean_profile_argv(
    *, reasoning_effort: str | None, disable_project_docs: bool = True
) -> list[str]:
    """Return the lean-profile argv fragment shared by both codex builders.

    ``--ignore-user-config`` drops ``~/.codex/config.toml`` so the operator's
    personal codex setup cannot leak into a cw-owned review.

    ``--strict-config`` makes codex *reject* an unknown ``-c`` key instead of
    ignoring it, which is what makes the overrides below trustworthy rather
    than decorative. Verified against codex-cli 0.147.0: ``-c bogus_key_xyz=1``
    exits 1 with ``Error loading config.toml: unknown configuration field
    `bogus_key_xyz` in -c/--config override``. Every key emitted here was
    live-checked under this flag and accepted (EXIT=0).

    When ``disable_project_docs`` is true, ``-c project_doc_max_bytes=0`` stops
    codex inlining the repo's own ``AGENTS.md``/project doc: cw already inlines
    every instruction the reviewer should see. The fix path passes false
    because its prompt does not contain the complete applicable project
    instructions and must therefore preserve codex's native discovery.

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
        "--strict-config",
        "-c",
        "mcp_servers={}",
    ]
    if disable_project_docs:
        argv += ["-c", "project_doc_max_bytes=0"]
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
    # The versioned inventory used to calculate enabled_tool_classes. Both are
    # None when the probed CLI has no inventory in this module.
    feature_inventory_cli_version: str | None
    enabled_tool_classes: list[str] | None
    # Which prompt-instruction channels actually contributed content, unioned
    # across every role in the pass. None means the caller did not compute
    # provenance; [] means it computed that no channel fired. Vocabulary: role_spec,
    # output_format_supplement, ticket_context, approved_plan, project_rubrics,
    # repo_policy, lint_grounding, sensitive_files.
    instruction_sources: list[_context._InstructionSource] | None


def _enabled_tool_classes(
    cli_version: str | None,
) -> tuple[str | None, list[str] | None]:
    """Return the matching inventory version and defaults left enabled."""
    metadata = (
        None
        if cli_version is None
        else _CODEX_FEATURE_METADATA_BY_CLI_VERSION.get(cli_version)
    )
    if metadata is None:
        return None, None
    return cli_version, [
        feature.name
        for feature in metadata
        if feature.default_enabled
        and feature.lean_profile_disposition is not _LeanProfileDisposition.DISABLE
    ]


def _probe_runtime_cli_version() -> str | None:
    """Probe the runtime CLI version for diagnostics without validating it."""
    return _capability.probe_codex_cli_version()


def _persist_profile_diagnostics(
    *,
    session_id: str,
    model: str | None,
    reasoning_effort: str | None,
    cli_version: str | None,
    instruction_sources: list[_context._InstructionSource] | None,
    pass_discriminator: str | None = None,
) -> None:
    """Record this pass's profile under *session_id*'s diagnostics dir.

    Never raises: mirrors ``_capability._persist_capability_diagnostics``'s
    contract — a failed diagnostics write must not take the review down with
    it. Called once per ``run_codex_roles`` invocation (not once per role): the
    profile is a property of the pass, not of any single reviewer. A non-None
    ``pass_discriminator`` preserves multiple passes in the same real session.
    """
    inventory_version, enabled_tool_classes = _enabled_tool_classes(cli_version)
    diagnostics = _ProfileDiagnostics(
        profile_version=_PROFILE_VERSION,
        reasoning_effort=reasoning_effort,
        effective_model=model,
        cli_version=cli_version,
        feature_inventory_cli_version=inventory_version,
        enabled_tool_classes=enabled_tool_classes,
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
        filename = _PROFILE_DIAGNOSTICS_FILENAME
        if pass_discriminator is not None:
            filename = f"codex-review-profile-{pass_discriminator}.json"
        (target / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        _log.warning(
            "codex review profile diagnostics write failed for session %s", session_id
        )
