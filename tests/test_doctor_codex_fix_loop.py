"""Tests for cw.doctor.codex_fix_loop (#2542); expected details are literals."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from cw.cli import main
from cw.codex_background import (
    CodexFixLoopResolution,
    _resolve_codex_fix_loop_enabled,
)
from cw.config import orchestrator_config_file
from cw.doctor._shared import CheckResult, DoctorReport
from cw.doctor.codex_fix_loop import _check_codex_fix_loop
from cw.exceptions import CwError
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    LaneConfig,
    OrchestratorConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)

_LOADER = "cw.doctor.codex_fix_loop.load_orchestrator_config"
_SANDBOX = "codex exec --sandbox workspace-write may commit fixes autonomously"
_REMOVE_KEY = (
    "set codex_fix_loop_enabled to {flip} or remove the key (removing it defers"
    " to default_codex_fix_loop_enabled in orchestrator.yaml)."
)
_LANE_KEY = "codex_fix_loop_enabled"
_GLOBAL_KEY = "default_codex_fix_loop_enabled"
_GLOBAL_ON = (
    f"codex fix loop is ON via global default: {_GLOBAL_KEY} is true in"
    f" orchestrator.yaml, so {_SANDBOX} on this lane. To change it, set"
    f" {_GLOBAL_KEY} in orchestrator.yaml, or set {_LANE_KEY} on lane"
    " '{lane}' of client '{client}' in clients.yaml."
)
_GLOBAL_OFF = (
    f"codex fix loop is OFF via global default: {_GLOBAL_KEY} is false or unset"
    f" in orchestrator.yaml. To turn it on, set {_GLOBAL_KEY}: true in"
    f" orchestrator.yaml, or set {_LANE_KEY}: true on lane '{{lane}}' of client"
    " '{client}' in clients.yaml."
)
_LANE_ON = (
    f"codex fix loop is ON via lane: {_LANE_KEY} is true on lane '{{lane}}' of"
    f" client '{{client}}' in clients.yaml, so {_SANDBOX} on this lane. To"
    f" change it, {_REMOVE_KEY.format(flip='false')}"
)
_LANE_OFF = (
    f"codex fix loop is OFF via lane: {_LANE_KEY} is false on lane '{{lane}}' of"
    f" client '{{client}}' in clients.yaml, which overrides {_GLOBAL_KEY} in"
    f" orchestrator.yaml. To change it, {_REMOVE_KEY.format(flip='true')}"
)
_LOAD_FAILURE = (
    "codex fix loop state unknown: orchestrator.yaml could not be loaded"
    " ({exc_class}), so default_codex_fix_loop_enabled cannot be resolved. See"
    " the orchestrator.yaml check."
)
_LEAKY_MESSAGE = "/home/secret/ws/orchestrator.yaml: boom"


def _write_orchestrator_yaml(body: str) -> None:
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _review_pipeline(backend: str) -> StagePipelineConfig:
    executors = {Stage.REVIEW: StageExecutorConfig(backend=backend)}
    return StagePipelineConfig(executors=executors)


def _lane(
    name: str, flag: bool | None = None, backend: str | None = None
) -> LaneConfig:
    pipeline = _review_pipeline(backend) if backend else None
    return LaneConfig(name=name, codex_fix_loop_enabled=flag, pipeline=pipeline)


def _client(*lanes: LaneConfig) -> ClientConfig:
    pipeline = _review_pipeline(CODEX_BACKEND)
    return ClientConfig(
        name="client-a",
        workspace_path=Path("/tmp/x"),
        lanes=list(lanes),
        pipeline=pipeline,
    )


def _raising_loader(exc: BaseException) -> object:
    def _load() -> OrchestratorConfig:
        raise exc

    return _load


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as info:
        OrchestratorConfig.model_validate({"bogus_field": 1})
    return info.value


def test_empty_clients_returns_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_LOADER, _raising_loader(RuntimeError("loader called")))
    assert _check_codex_fix_loop({}) == []


def test_client_without_codex_review_executor_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOADER, _raising_loader(RuntimeError("loader called")))
    client = ClientConfig(name="plain", workspace_path=Path("/tmp/x"))
    assert _check_codex_fix_loop({"plain": client}) == []


def test_lane_level_non_codex_review_overrides_client_codex() -> None:
    client = _client(_lane("a", backend="claude-native"), _lane("b"))
    results = _check_codex_fix_loop({"client-a": client})
    assert [r.name for r in results] == ["codex-fix-loop/client-a/b"]


def test_client_level_codex_without_declared_lanes_lists_default_lane() -> None:
    results = _check_codex_fix_loop({"client-a": _client()})
    assert [r.name for r in results] == ["codex-fix-loop/client-a/default"]


@pytest.mark.parametrize(
    ("lane_flag", "global_value", "template", "enabled"),
    [
        (True, False, _LANE_ON, True),
        (True, True, _LANE_ON, True),
        (False, True, _LANE_OFF, False),
        (False, False, _LANE_OFF, False),
        (None, True, _GLOBAL_ON, True),
        (None, False, _GLOBAL_OFF, False),
    ],
    ids=[
        "lane-on-global-off",
        "lane-on-global-on",
        "lane-off-global-on",
        "lane-off-global-off",
        "global-on",
        "global-off",
    ],
)
def test_detail_matrix(
    lane_flag: bool | None, global_value: bool, template: str, enabled: bool
) -> None:
    _write_orchestrator_yaml(f"{_GLOBAL_KEY}: {str(global_value).lower()}\n")
    client = _client(_lane("trial", lane_flag))
    results = _check_codex_fix_loop({"client-a": client})

    assert len(results) == 1
    result = results[0]
    assert result.name == "codex-fix-loop/client-a/trial"
    assert result.ok is True
    assert result.detail == template.format(lane="trial", client="client-a")
    assert result.warn is enabled
    task = TicketTask(
        ticket_id="T-1", client="client-a", stage=Stage.REVIEW, lane="trial"
    )
    runtime = OrchestratorConfig(default_codex_fix_loop_enabled=global_value)
    assert result.warn is _resolve_codex_fix_loop_enabled(client, task, runtime)


def test_global_key_absent_from_orchestrator_yaml_is_off_via_global_default() -> None:
    _write_orchestrator_yaml("reap_policy: auto\n")
    (result,) = _check_codex_fix_loop({"client-a": _client()})

    assert result.detail == _GLOBAL_OFF.format(lane="default", client="client-a")
    assert result.warn is False


def test_absent_orchestrator_yaml_stays_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOADER, _raising_loader(RuntimeError("loader called")))
    assert not orchestrator_config_file().exists()

    (result,) = _check_codex_fix_loop({"client-a": _client()})

    assert result.detail == _GLOBAL_OFF.format(lane="default", client="client-a")
    assert result.warn is False
    assert not orchestrator_config_file().exists()


@pytest.mark.parametrize(
    ("exc", "exc_class"),
    [
        (OSError(_LEAKY_MESSAGE), "OSError"),
        (yaml.YAMLError(_LEAKY_MESSAGE), "YAMLError"),
        (CwError(_LEAKY_MESSAGE), "CwError"),
        (_validation_error(), "ValidationError"),
    ],
    ids=["oserror", "yaml-error", "cw-error", "validation-error"],
)
def test_load_failure_reports_fixed_message_with_class_name_only(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, exc_class: str
) -> None:
    _write_orchestrator_yaml(f"{_GLOBAL_KEY}: true\n")
    monkeypatch.setattr(_LOADER, _raising_loader(exc))
    results = _check_codex_fix_loop({"client-a": _client()})

    assert len(results) == 1
    result = results[0]
    assert result.name == "codex-fix-loop"
    assert result.ok is True
    assert result.warn is False
    assert result.detail == _LOAD_FAILURE.format(exc_class=exc_class)
    assert "/home/secret" not in result.detail
    assert "boom" not in result.detail


def test_real_invalid_orchestrator_yaml_does_not_leak_path_or_validation_text() -> None:
    _write_orchestrator_yaml("bogus_field: 1\n")
    (result,) = _check_codex_fix_loop({"client-a": _client()})

    assert result.detail == _LOAD_FAILURE.format(exc_class="ConfigValidationError")
    assert str(orchestrator_config_file()) not in result.detail
    assert "bogus_field" not in result.detail


def test_mixed_lanes_and_clients_listed_in_declared_order() -> None:
    _write_orchestrator_yaml(f"{_GLOBAL_KEY}: true\n")
    codex = _client(_lane("a", True), _lane("b"), _lane("c", False))
    plain = ClientConfig(name="plain", workspace_path=Path("/tmp/x"))

    results = _check_codex_fix_loop({"plain": plain, "client-a": codex})

    assert [(r.name, r.warn) for r in results] == [
        ("codex-fix-loop/client-a/a", True),
        ("codex-fix-loop/client-a/b", True),
        ("codex-fix-loop/client-a/c", False),
    ]
    assert [r.detail.split(":")[0] for r in results] == [
        "codex fix loop is ON via lane",
        "codex fix loop is ON via global default",
        "codex fix loop is OFF via lane",
    ]


def test_doctor_goes_through_shared_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_orchestrator_yaml(f"{_GLOBAL_KEY}: false\n")
    monkeypatch.setattr(
        "cw.doctor.codex_fix_loop._resolve_codex_fix_loop",
        lambda *_args: CodexFixLoopResolution(True, "lane"),
    )

    (result,) = _check_codex_fix_loop({"client-a": _client(_lane("trial"))})

    assert result.detail == _LANE_ON.format(lane="trial", client="client-a")


def test_warning_does_not_change_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    check = CheckResult("codex-fix-loop/client-a/x", ok=True, warn=True, detail="d")
    report = DoctorReport(version="0.0.0", checks=[check])
    assert report.ok is True
    assert report.clean is False
    monkeypatch.setattr("cw.cli.maintenance.run_doctor", lambda **_kwargs: report)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "[WARN] codex-fix-loop/" in result.output
