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

from pydantic import BaseModel, ConfigDict

from cw.codex_review import _capability, _context
from cw.config import diagnostics_dir

_log = logging.getLogger(__name__)

# Bump when the argv block below changes shape, so a diagnostics artifact from
# an older run is not mistaken for one produced by the current profile.
_PROFILE_VERSION = 2

_PROFILE_DIAGNOSTICS_FILENAME = "codex-review-profile.json"

# Complete captured inventory for the CLI version against which this profile
# was verified. The version is part of the name deliberately: a newer Codex
# feature list must be reviewed and added as a new inventory instead of being
# silently treated as covered by this one.
_CODEX_FEATURES_0_147_0: tuple[str, ...] = (
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

# A lean reviewer needs no optional Codex feature. Keep the allowlist explicit
# so both the disable argv and diagnostics are honest complements of the
# versioned inventory rather than two names for the same tuple.
_LEAN_PROFILE_FEATURE_ALLOWLIST: frozenset[str] = frozenset()
_DISABLED_FEATURES: tuple[str, ...] = tuple(
    feature
    for feature in _CODEX_FEATURES_0_147_0
    if feature not in _LEAN_PROFILE_FEATURE_ALLOWLIST
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
    # Versioned-inventory feature classes allowed by this profile. Empty by
    # construction today — see _LEAN_PROFILE_FEATURE_ALLOWLIST.
    enabled_tool_classes: list[str]
    # Which prompt-instruction channels actually contributed content, unioned
    # across every role in the pass. Vocabulary: role_spec,
    # output_format_supplement, ticket_context, approved_plan, project_rubrics,
    # repo_policy, lint_grounding, sensitive_files.
    instruction_sources: list[_context._InstructionSource]


def _enabled_tool_classes() -> list[str]:
    """Return the versioned-inventory complement of the disable argv."""
    return [name for name in _CODEX_FEATURES_0_147_0 if name not in _DISABLED_FEATURES]


def _persist_profile_diagnostics(
    *,
    session_id: str,
    model: str | None,
    reasoning_effort: str | None,
    instruction_sources: list[_context._InstructionSource],
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
        cli_version=_capability.probe_codex_cli_version(),
        enabled_tool_classes=_enabled_tool_classes(),
        instruction_sources=list(instruction_sources),
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
