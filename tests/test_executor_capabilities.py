"""Four-executor conformance matrix for harness capability declarations (#3708).

The stage pre-flight (``cw.stage_preflight``, RFC 0019 H2) reads these
declarations. These tests pin each declaration to the existing predicates and
to the executors' own ``capabilities()`` method, check that each executor's
pre-flight reads its own declaration, and check that no old predicate derives
from the declaration.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import ModuleType

import pytest

import cw.models as models_pkg
from cw.executor import codex as codex_mod
from cw.executor import core as core_mod
from cw.executor import local as local_mod
from cw.executor import opencode as opencode_mod
from cw.executor._capabilities import (
    CLAUDE_NATIVE_CAPABILITIES,
    CODEX_CAPABILITIES,
    LOCAL_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    declared_capabilities,
)
from cw.executor.codex import CODEX_SUPPORTED_STAGES, CodexExecutor
from cw.executor.core import ClaudeNativeExecutor, StageExecutor
from cw.executor.local import LocalExecutor
from cw.executor.opencode import OpencodeExecutor
from cw.models import StageExecutorConfig
from cw.models.bindings import Harness
from cw.models.capabilities import (
    BindingProbeKind,
    RepairEntry,
    ToolNarrowing,
    UsageSource,
)
from cw.models.enums import SandboxMode, Stage
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from cw.opencode_runner import SUPPORTED_STAGES
from cw.review_convergence._backend import clean_mode_supported, engine_supported

_REPO_ROOT = Path(__file__).resolve().parents[1]
# DEFAULT_LOCAL_LIVENESS_BACKEND ("aider") is a liveness default, not a harness.
_BACKENDS = sorted(
    v
    for k, v in vars(models_pkg).items()
    if k.endswith("_BACKEND") and not k.startswith("DEFAULT_") and isinstance(v, str)
)

_EXECUTORS = {
    CLAUDE_NATIVE_BACKEND: (ClaudeNativeExecutor, CLAUDE_NATIVE_CAPABILITIES),
    CODEX_BACKEND: (CodexExecutor, CODEX_CAPABILITIES),
    OPENCODE_BACKEND: (OpencodeExecutor, OPENCODE_CAPABILITIES),
    LOCAL_BACKEND: (LocalExecutor, LOCAL_CAPABILITIES),
}

# The R7 matrix, reproduced cell by cell with its grounds in the executor
# modules' comments and in the plan.
_MATRIX: dict[str, dict[str, object]] = {
    CLAUDE_NATIVE_BACKEND: {
        "stages": frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE}),
        "skills": True,
        "subagents": True,
        "binding_probe": BindingProbeKind.ENABLED_PLUGINS_BRANCH,
        "sandbox_modes": frozenset(),
        "clean_mode_review": True,
        "review_engine": True,
        "repair_entry": RepairEntry.SUPPORTED,
        "tool_narrowing": ToolNarrowing.PER_TOOL,
        "usage_source": UsageSource.SIGNAL_STOP,
    },
    CODEX_BACKEND: {
        "stages": frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE}),
        "skills": False,
        "subagents": False,
        "binding_probe": BindingProbeKind.BACKEND_MCP_CONFIG,
        "sandbox_modes": frozenset(
            {SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE}
        ),
        "clean_mode_review": True,
        "review_engine": True,
        "repair_entry": RepairEntry.SUPPORTED,
        "tool_narrowing": ToolNarrowing.PER_TOOL,
        "usage_source": UsageSource.CODEX_JSON_EVENTS,
    },
    OPENCODE_BACKEND: {
        "stages": frozenset({Stage.PLAN, Stage.IMPL, Stage.REVIEW}),
        "skills": False,
        "subagents": False,
        "binding_probe": BindingProbeKind.BACKEND_MCP_CONFIG,
        "sandbox_modes": frozenset(),
        "clean_mode_review": True,
        "review_engine": False,
        "repair_entry": RepairEntry.BLOCKS_ANY_FEEDBACK,
        "tool_narrowing": ToolNarrowing.NONE,
        "usage_source": UsageSource.UNAVAILABLE,
    },
    LOCAL_BACKEND: {
        "stages": frozenset({Stage.IMPL}),
        "skills": False,
        "subagents": False,
        "binding_probe": BindingProbeKind.NONE,
        "sandbox_modes": frozenset(),
        "clean_mode_review": False,
        "review_engine": False,
        "repair_entry": RepairEntry.BLOCKS_ADOPTED,
        "tool_narrowing": ToolNarrowing.NONE,
        "usage_source": UsageSource.UNAVAILABLE,
    },
}


@pytest.mark.parametrize("backend", _BACKENDS)
def test_declaration_exists_and_names_its_harness(backend: str) -> None:
    caps = declared_capabilities(backend)
    assert caps is not None
    assert caps.harness is Harness(backend)


def test_unknown_or_empty_backend_has_no_declaration() -> None:
    assert declared_capabilities("nope") is None
    assert declared_capabilities("") is None


@pytest.mark.parametrize("backend", _BACKENDS)
def test_declaration_matches_r7_matrix_field_by_field(backend: str) -> None:
    caps = declared_capabilities(backend)
    assert caps is not None
    for field, expected in _MATRIX[backend].items():
        assert getattr(caps, field) == expected, (backend, field)


def test_nobody_declares_harden() -> None:
    for backend in _BACKENDS:
        caps = declared_capabilities(backend)
        assert caps is not None
        assert Stage.HARDEN not in caps.stages


def test_opencode_does_not_declare_finalize() -> None:
    assert Stage.FINALIZE not in OPENCODE_CAPABILITIES.stages


def test_local_declares_impl_only() -> None:
    assert LOCAL_CAPABILITIES.stages == frozenset({Stage.IMPL})


@pytest.mark.parametrize("backend", sorted(_EXECUTORS))
def test_executor_capabilities_is_the_declared_constant(backend: str) -> None:
    cls, constant = _EXECUTORS[backend]
    executor = cls(config=StageExecutorConfig())
    assert executor.capabilities() is constant
    assert executor.capabilities() is declared_capabilities(backend)
    assert isinstance(executor, StageExecutor)


def test_codex_stages_equal_codex_supported_stages() -> None:
    assert CODEX_CAPABILITIES.stages == CODEX_SUPPORTED_STAGES


def test_opencode_stages_equal_prompt_stages_minus_finalize() -> None:
    """opencode has a FINALIZE prompt but no verified run (RFC 0018 B1, #3059)."""
    expected = {Stage(s) for s in SUPPORTED_STAGES} - {Stage.FINALIZE}
    assert OPENCODE_CAPABILITIES.stages == expected


def test_claude_native_stages_have_command_files() -> None:
    commands = _REPO_ROOT / ".claude" / "commands"
    for stage in CLAUDE_NATIVE_CAPABILITIES.stages:
        assert (commands / f"auto-dev-{stage.value}.md").is_file(), stage
    assert not (commands / "auto-dev-harden.md").exists()


@pytest.mark.parametrize("backend", _BACKENDS)
def test_clean_mode_and_engine_match_existing_predicates(backend: str) -> None:
    caps = declared_capabilities(backend)
    assert caps is not None
    assert caps.clean_mode_review == clean_mode_supported(backend)
    assert caps.review_engine == engine_supported(backend)


@pytest.mark.parametrize(
    ("module", "constant"),
    [
        (local_mod, "LOCAL_CAPABILITIES"),
        (opencode_mod, "OPENCODE_CAPABILITIES"),
        (codex_mod, "CODEX_CAPABILITIES"),
    ],
    ids=["local", "opencode", "codex"],
)
def test_each_preflight_reads_its_own_declaration(
    module: ModuleType, constant: str
) -> None:
    """The stage pre-flight parks from the executor's own declaration."""
    source = inspect.getsource(module)
    assert f"park_if_unmet(\n        {constant}," in source


def test_claude_native_is_not_wired_to_the_preflight() -> None:
    """A7: ClaudeNativeExecutor has no park position (#3744 owns the tick)."""
    assert "park_if_unmet" not in inspect.getsource(core_mod)


def test_codex_impl_consumes_review_feedback_directly() -> None:
    import cw.codex_impl._run as run_mod

    source = inspect.getsource(run_mod)
    assert "review_feedback" in source
    assert "verify_review_feedback" in source


@pytest.mark.parametrize(
    "module",
    [
        "cw.review_convergence._backend",
    ],
)
def test_old_predicates_do_not_read_declarations(module: str) -> None:
    import importlib

    source = inspect.getsource(importlib.import_module(module))
    assert "declared_capabilities" not in source
    assert "_capabilities" not in source


@pytest.mark.parametrize(
    "path", ["src/cw/executor/_capabilities.py", "src/cw/models/capabilities.py"]
)
def test_declarations_never_probe_the_host(path: str) -> None:
    tree = ast.parse((_REPO_ROOT / path).read_text(encoding="utf-8"))
    forbidden = {
        "which",
        "aider_available",
        "opencode_available",
        "codex_capability_diagnosis",
        "build_argv",
    }
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & forbidden


def test_declarations_identical_whether_binaries_present_or_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    snapshot: dict[str, object] = {}
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/bin/fake")
    for backend in _BACKENDS:
        snapshot[backend] = declared_capabilities(backend)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    for backend in _BACKENDS:
        assert declared_capabilities(backend) == snapshot[backend]
