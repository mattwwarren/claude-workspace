"""Tests for the declared-only ``harness-capabilities`` doctor check (#3708)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import cw.doctor as doctor_pkg
import cw.doctor.capabilities as cap_mod
from cw.doctor._shared import CheckResult, DoctorReport
from cw.doctor.capabilities import _check_harness_capabilities
from cw.doctor.core import _client_config_checks
from cw.models import CODEX_BACKEND, LOCAL_BACKEND, OPENCODE_BACKEND, LaneConfig, Stage
from cw.models.orchestrator_config.constants import CLAUDE_NATIVE_BACKEND
from tests._doctor_helpers import make_client, make_lane

_CAVEAT = "static declaration; auth, env and headless load unverified"
_PIPELINE = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]


def _lane_on(backend: str) -> LaneConfig:
    """A lane that routes every pipeline stage to *backend*.

    The client's review backend still routes REVIEW to codex, so a lane can
    yield more than one result; tests pick theirs by name with ``_result_for``.
    """
    return make_lane(
        "default",
        executors=dict.fromkeys(_PIPELINE, backend),
        stages=_PIPELINE,
    )


def _result_for(backend: str) -> CheckResult:
    client = make_client(_lane_on(backend), name="client-a")
    results = _check_harness_capabilities({"client-a": client})
    name = f"harness-capabilities/client-a/default/{backend}"
    (match,) = [r for r in results if r.name == name]
    return match


def test_one_result_per_client_lane_backend_and_always_ok() -> None:
    client = make_client(_lane_on(CODEX_BACKEND), name="client-a")
    results = _check_harness_capabilities({"client-a": client})
    names = [r.name for r in results]
    assert len(names) == len(set(names))
    assert "harness-capabilities/client-a/default/codex" in names
    assert all(r.ok and not r.warn for r in results)


@pytest.mark.parametrize(
    "backend", [CLAUDE_NATIVE_BACKEND, CODEX_BACKEND, OPENCODE_BACKEND, LOCAL_BACKEND]
)
def test_every_backend_result_is_ok(backend: str) -> None:
    result = _result_for(backend)
    assert result.ok is True
    assert result.warn is False
    assert _CAVEAT in result.detail


def test_detail_is_deterministic_across_runs() -> None:
    first = _result_for(CODEX_BACKEND).detail
    second = _result_for(CODEX_BACKEND).detail
    assert first == second
    assert "sandbox read-only, workspace-write" in first


def test_empty_sandbox_renders_as_harness_managed() -> None:
    assert "sandbox harness-managed" in _result_for(OPENCODE_BACKEND).detail


def test_mixed_lane_yields_one_result_per_backend_in_first_appearance_order() -> None:
    lane = make_lane(
        "default",
        executors={Stage.REVIEW: CODEX_BACKEND, Stage.IMPL: LOCAL_BACKEND},
        stages=[Stage.REVIEW, Stage.IMPL],
    )
    client = make_client(lane, name="client-a")
    results = _check_harness_capabilities({"client-a": client})
    assert [r.name for r in results] == [
        "harness-capabilities/client-a/default/codex",
        "harness-capabilities/client-a/default/local",
    ]


def test_mismatch_note_names_routed_stage_without_severity_change() -> None:
    lane = make_lane(
        "default", executors={Stage.PLAN: LOCAL_BACKEND}, stages=[Stage.PLAN]
    )
    client = make_client(lane, name="client-a")
    (result,) = _check_harness_capabilities({"client-a": client})
    assert "declared, not enforced" not in result.detail
    assert "parks such a spawn as stage_unsupported" in result.detail
    assert "routes plan to local" in result.detail
    assert result.ok is True
    assert result.warn is False


def test_opencode_finalize_carries_the_note() -> None:
    lane = make_lane(
        "default", executors={Stage.FINALIZE: OPENCODE_BACKEND}, stages=[Stage.FINALIZE]
    )
    client = make_client(lane, name="client-a")
    (result,) = _check_harness_capabilities({"client-a": client})
    assert "routes finalize to opencode" in result.detail
    assert result.warn is False


def test_registration_in_client_config_checks() -> None:
    client = make_client(_lane_on(CODEX_BACKEND), name="client-a")
    names = {r.name for r in _client_config_checks({"client-a": client})}
    assert "harness-capabilities/client-a/default/codex" in names


def test_reexported_from_cw_doctor() -> None:
    assert doctor_pkg._check_harness_capabilities is _check_harness_capabilities
    assert "_check_harness_capabilities" in doctor_pkg.__all__


def test_default_doctor_status_is_clean_with_these_results_alone() -> None:
    client = make_client(_lane_on(CODEX_BACKEND), name="client-a")
    report = DoctorReport(
        version="test", checks=_check_harness_capabilities({"client-a": client})
    )
    assert report.clean is True


def test_module_imports_only_shared_from_doctor_package() -> None:
    source = Path(inspect.getfile(cap_mod)).read_text(encoding="utf-8")
    doctor_imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("cw.doctor")
    }
    assert doctor_imports == {"cw.doctor._shared"}
