"""Seam assertions for the ``cw.models.orchestrator_config`` package split (#2497).

``cw.models.orchestrator_config`` was a single 1313-line module. The split is a
pure move, so the behavioral suite (``tests/test_models.py``) stays as it is;
this file pins the things that suite cannot see:

1. The JSON schemas and default dumps are byte-for-byte unchanged. The golden
   ``tests/fixtures/orchestrator_config_schemas.json`` was captured from the
   unsplit module and is compared as ``json.dumps`` strings, so a reordered
   field, an edited docstring (schema ``description``) or a changed default
   fails here. Validators are invisible to the schema, which is why the
   default-instance dumps are pinned too.
2. The import surface is frozen. ``EXPECTED_EXPORTS`` is hard-coded, not
   re-derived from the package, so a dropped re-export fails loudly, and every
   ``cw.models`` re-export is the same object as the package's.
3. Every validator warning logs as ``cw.models.orchestrator_config``, the
   pre-split logger name operators filter on.
4. The package imports cleanly whichever module loads first.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

import cw.models
import cw.models.orchestrator_config as pkg

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "orchestrator_config_schemas.json"

_PINNED_LOGGER = "cw.models.orchestrator_config"

# The 11 models whose schemas the golden pins. ``ClientConfig`` is not part of
# the package, but it embeds ``LaneConfig`` and ``StagePipelineConfig``, so its
# schema catches a change to either one as it reaches the client config.
_SCHEMA_MODELS = (
    "LaneConcurrencyOverride",
    "ClientConcurrencyOverride",
    "ConcurrencyOverrides",
    "StageExecutorConfig",
    "StagePipelineConfig",
    "LaneConfig",
    "OperatorChannelForward",
    "OrchestratorConfig",
    "HookRule",
    "EventHookRegistry",
    "ClientConfig",
)

# The two models whose serialization-mode schema and default dump are pinned.
_DUMPED_MODELS = ("OrchestratorConfig", "LaneConfig")

# The frozenset-typed fields of a default ``OrchestratorConfig`` dump. A
# frozenset of strings iterates in ``PYTHONHASHSEED`` order, so their JSON lists
# are sorted before comparison; the golden was captured the same way.
_SET_PATHS = (
    ("operator_channel_forward", "event_types"),
    ("operator_channel_forward", "task_transition_statuses"),
)

# The 31 names ``cw.models`` imports from ``cw.models.orchestrator_config``.
_CW_MODELS_REEXPORTS = frozenset(
    {
        "_DEFAULT_OPERATOR_EVENT_TYPES",
        "_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES",
        "_USAGE_LIMIT_BACKOFF_SECONDS",
        "AGENT_SPAWN_LAST_STAMPED_AT_KEY",
        "AGENT_SPAWN_STAMP_KEY",
        "AGENT_SPAWN_UNRESOLVED_COUNT_KEY",
        "BASH_TOOL_NAME",
        "CLAUDE_NATIVE_BACKEND",
        "CODEX_BACKEND",
        "CONTEXT_JSON_RELATIVE_PATH",
        "DEFAULT_DISK_PRESSURE_MIN_FREE_GB",
        "DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION",
        "DEFAULT_DISK_PRESSURE_MIN_FREE_INODES",
        "DEFAULT_GLOBAL_ATTEMPT_CEILING",
        "HOOK_CONTEXT_RELATIVE_PATH",
        "LOCAL_BACKEND",
        "MONITOR_TOOL_NAME",
        "OPENCODE_BACKEND",
        "STAGED_EMIT_RESULT_KEY",
        "WORKER_TMPDIR_RELATIVE_PATH",
        "ClientConcurrencyOverride",
        "ConcurrencyOverrides",
        "EventHookRegistry",
        "HookRule",
        "LaneConcurrencyOverride",
        "LaneConfig",
        "OperatorChannelForward",
        "OrchestratorConfig",
        "StageExecutorConfig",
        "StagePipelineConfig",
        "extract_unresolved_spawn_count",
    }
)

# The complete import surface of ``cw.models.orchestrator_config``: the 31
# ``cw.models`` re-exports plus ``CODEX_TIER_CLAIM_SUPPRESSION``, which
# ``cw.codex_background`` imports from the package directly.
EXPECTED_EXPORTS = _CW_MODELS_REEXPORTS | {"CODEX_TIER_CLAIM_SUPPRESSION"}


def _golden() -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads(
        _GOLDEN_PATH.read_text(encoding="utf-8")
    )
    return data


def _model(name: str) -> type[BaseModel]:
    model = getattr(cw.models, name)
    assert isinstance(model, type)
    assert issubclass(model, BaseModel)
    return model


def _canonical_dump(dump: dict[str, object]) -> dict[str, object]:
    for outer, inner in _SET_PATHS:
        sub = dump.get(outer)
        if isinstance(sub, dict) and isinstance(sub.get(inner), list):
            sub[inner] = sorted(sub[inner])
    return dump


class TestSchemaGolden:
    """The schemas and default dumps match the pre-split capture exactly."""

    def test_golden_layout(self) -> None:
        golden = _golden()

        assert list(golden) == ["validation", "serialization", "default_dumps"]
        assert list(golden["validation"]) == list(_SCHEMA_MODELS)
        assert list(golden["serialization"]) == list(_DUMPED_MODELS)
        assert list(golden["default_dumps"]) == list(_DUMPED_MODELS)

    @pytest.mark.parametrize("name", _SCHEMA_MODELS)
    def test_validation_schema_unchanged(self, name: str) -> None:
        schema = _model(name).model_json_schema()

        assert json.dumps(schema) == json.dumps(_golden()["validation"][name])

    @pytest.mark.parametrize("name", _DUMPED_MODELS)
    def test_serialization_schema_unchanged(self, name: str) -> None:
        schema = _model(name).model_json_schema(mode="serialization")

        assert json.dumps(schema) == json.dumps(_golden()["serialization"][name])

    def test_orchestrator_config_default_dump_unchanged(self) -> None:
        dump = _canonical_dump(pkg.OrchestratorConfig().model_dump(mode="json"))

        expected = _golden()["default_dumps"]["OrchestratorConfig"]
        assert json.dumps(dump) == json.dumps(expected)

    def test_lane_config_default_dump_unchanged(self) -> None:
        # ``name`` is the one required field; every other value is a default.
        dump = _canonical_dump(pkg.LaneConfig(name="default").model_dump(mode="json"))

        assert json.dumps(dump) == json.dumps(_golden()["default_dumps"]["LaneConfig"])

    def test_default_dump_sort_is_needed_and_sufficient(self) -> None:
        """The sorted paths are frozensets; nothing else in the dump is."""
        forward = pkg.OrchestratorConfig().operator_channel_forward

        for _outer, inner in _SET_PATHS:
            assert isinstance(getattr(forward, inner), frozenset)


class TestImportSurface:
    """``from cw.models.orchestrator_config import X`` keeps working for every X."""

    def test_expected_exports_size(self) -> None:
        assert len(_CW_MODELS_REEXPORTS) == 31
        assert len(EXPECTED_EXPORTS) == 32

    def test_all_matches_full_surface(self) -> None:
        assert set(pkg.__all__) == EXPECTED_EXPORTS
        assert len(pkg.__all__) == len(EXPECTED_EXPORTS)

    @pytest.mark.parametrize("name", sorted(EXPECTED_EXPORTS))
    def test_name_is_bound(self, name: str) -> None:
        assert hasattr(pkg, name)

    @pytest.mark.parametrize("name", sorted(_CW_MODELS_REEXPORTS))
    def test_cw_models_reexport_is_the_same_object(self, name: str) -> None:
        assert getattr(cw.models, name) is getattr(pkg, name)

    def test_cw_models_still_reexports_the_31_names(self) -> None:
        assert set(cw.models.__all__) >= _CW_MODELS_REEXPORTS


class TestLoggerNameIsPinned:
    """Every validator warning logs as ``cw.models.orchestrator_config``."""

    def test_constants_leaf_holds_the_pinned_name(self) -> None:
        from cw.models.orchestrator_config import constants

        assert constants._LOGGER_NAME == _PINNED_LOGGER
        assert "_LOGGER_NAME" not in pkg.__all__

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            pytest.param(
                {"event_inbox_retention_bytes": 50, "event_inbox_retention_count": 10},
                "event_inbox_retention_bytes=50",
                id="retention-ratio",
            ),
            pytest.param(
                {"per_client_max_parallel": {"acme": 2}},
                "per_client_max_parallel is deprecated",
                id="legacy-per-client",
            ),
            pytest.param(
                {"default_max_parallel": 3},
                "default_max_parallel is deprecated",
                id="legacy-default",
            ),
            pytest.param(
                {"park_veto_cap": 3},
                "ignoring removed timeout setting",
                id="removed-timeout-key",
            ),
        ],
    )
    def test_validator_record_is_pinned(
        self,
        data: dict[str, object],
        message: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_PINNED_LOGGER):
            pkg.OrchestratorConfig.model_validate(data)

        records = [r for r in caplog.records if message in r.getMessage()]
        assert [r.name for r in records] == [_PINNED_LOGGER]


@pytest.mark.parametrize(
    "first",
    [
        "cw.models.orchestrator_config",
        "cw.models.orchestrator_config.concurrency",
        "cw.models.orchestrator_config.constants",
        "cw.models.orchestrator_config.hooks",
        "cw.models.orchestrator_config.stage",
        "cw.models.client",
        "cw.models",
    ],
)
def test_package_imports_cleanly_whichever_module_loads_first(first: str) -> None:
    """A sibling importing from the package ``__init__`` would close a cycle
    that only fails in ONE import order, invisible to a suite whose conftest
    always warms ``cw.models`` first.

    Each interpreter starts cold and imports *first* before anything else. Uses
    ``sys.executable`` (not a bare ``python3``) per PYTHON-PATTERNS' compiled-
    dependency isolation rule: ``pydantic_core`` is ABI-bound to this venv.
    """
    script = (
        f"import {first}\n"
        "import cw.models\n"
        "from cw.models import OrchestratorConfig\n"
        "OrchestratorConfig()\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)
