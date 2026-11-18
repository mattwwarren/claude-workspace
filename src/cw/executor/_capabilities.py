"""Static per-harness capability declarations (RFC 0019 H1, #3708).

Declared, not enforced. These constants are data: no dispatch path, guard or
predicate reads them in H1. ``clean_mode_supported``, ``engine_supported``,
``finalize_block_details``, ``repair_entry_block_details`` and
``CODEX_SUPPORTED_STAGES`` remain the enforcement points until H2 (#3219). The
parity tests in ``tests/test_executor_capabilities.py`` pin each declaration to
those predicates. No availability probe here: no ``shutil.which``, no
``build_argv``, so the declaration cannot change with the host's binaries.
"""

from __future__ import annotations

from cw.models.bindings import Harness
from cw.models.capabilities import (
    BindingProbeKind,
    HarnessCapabilities,
    RepairEntry,
    ToolNarrowing,
    UsageSource,
)
from cw.models.enums import SandboxMode, Stage

# Ground: claude-native runs the full pipeline through the Skill tool, Agent
# subagents and hooks (the `.claude/commands/auto-dev-*.md` files). Its
# repair entry is `supported`: the commands define "Repair entry: no plan"
# and no executor guard calls the predicate. Tool narrowing is the deny-list
# built by `build_disallowed_tools_arg`. Usage is a cost figure from
# `signal_stop` only.
CLAUDE_NATIVE_CAPABILITIES = HarnessCapabilities(
    harness=Harness.CLAUDE_NATIVE,
    stages=frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE}),
    skills=True,
    subagents=True,
    binding_probe=BindingProbeKind.ENABLED_PLUGINS_BRANCH,
    sandbox_modes=frozenset(),
    clean_mode_review=True,
    review_engine=True,
    repair_entry=RepairEntry.SUPPORTED,
    tool_narrowing=ToolNarrowing.PER_TOOL,
    usage_source=UsageSource.SIGNAL_STOP,
)

# Ground: `CODEX_SUPPORTED_STAGES` (codex.py:47-49). Codex IMPL consumes
# `review_feedback` itself (codex_impl/_run.py:196-199, 343, 420, via
# `verify_review_feedback`) and tolerates an adopted no-plan row
# (stage_verification/impl_gates.py:99-100), so its repair entry is
# `supported`, and no codex guard calls the predicate. Sandbox is the two
# `SandboxMode` values codex accepts. Narrowing is per tool
# (`-c mcp_servers.<server>.enabled_tools`); app-addressed bindings are
# per server, which O2 handles. Usage comes from the `--json` events.
CODEX_CAPABILITIES = HarnessCapabilities(
    harness=Harness.CODEX,
    stages=frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE}),
    skills=False,
    subagents=False,
    binding_probe=BindingProbeKind.BACKEND_MCP_CONFIG,
    sandbox_modes=frozenset({SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE}),
    clean_mode_review=True,
    review_engine=True,
    repair_entry=RepairEntry.SUPPORTED,
    tool_narrowing=ToolNarrowing.PER_TOOL,
    usage_source=UsageSource.CODEX_JSON_EVENTS,
)

# Ground: FINALIZE is refused for opencode by `finalize_block_details`
# (_finalize_guard.py:34-42, called at opencode.py:122), so opencode does NOT
# declare FINALIZE
# (H2 must reproduce `finalize_unsupported_backend`). `repair_entry` is
# `blocks_any_feedback` (opencode.py:145-147). Usage is unavailable because
# the backend output is unpinned (`BACKEND_OUTPUT_UNPINNED`).
OPENCODE_CAPABILITIES = HarnessCapabilities(
    harness=Harness.OPENCODE,
    stages=frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW}),
    skills=False,
    subagents=False,
    binding_probe=BindingProbeKind.BACKEND_MCP_CONFIG,
    sandbox_modes=frozenset(),
    clean_mode_review=True,
    review_engine=False,
    repair_entry=RepairEntry.BLOCKS_ANY_FEEDBACK,
    tool_narrowing=ToolNarrowing.NONE,
    usage_source=UsageSource.UNAVAILABLE,
)

# Ground: `_local_preflight` has no stage check (local.py:91-164), so local
# keeps running any stage. Declaring IMPL only is "declared, not enforced"
# (#3219 stays open). `repair_entry` is `blocks_adopted` via the default
# flag of `repair_entry_block_details` (local.py:75). Local has no MCP, so
# no binding read and no tool narrowing.
LOCAL_CAPABILITIES = HarnessCapabilities(
    harness=Harness.LOCAL,
    stages=frozenset({Stage.IMPL}),
    skills=False,
    subagents=False,
    binding_probe=BindingProbeKind.NONE,
    sandbox_modes=frozenset(),
    clean_mode_review=False,
    review_engine=False,
    repair_entry=RepairEntry.BLOCKS_ADOPTED,
    tool_narrowing=ToolNarrowing.NONE,
    usage_source=UsageSource.UNAVAILABLE,
)

_BY_BACKEND: dict[str, HarnessCapabilities] = {
    caps.harness.value: caps
    for caps in (
        CLAUDE_NATIVE_CAPABILITIES,
        CODEX_CAPABILITIES,
        OPENCODE_CAPABILITIES,
        LOCAL_CAPABILITIES,
    )
}


def declared_capabilities(backend: str) -> HarnessCapabilities | None:
    """Return the declaration for *backend*, or ``None`` if unrecognized."""
    return _BY_BACKEND.get(backend)
