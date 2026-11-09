"""Tests for ``cw.doctor.review_engine`` (GitHub #3039, RFC 0017 I1).

A ``review.engine`` runs only in the Claude-native review stage. The doctor
warns, once per lane, when a lane's REVIEW executor is another backend.
"""

from __future__ import annotations

from pathlib import Path

from cw.doctor.core import _client_config_checks
from cw.doctor.review_engine import _check_review_engine
from cw.models import (
    ClientConfig,
    LaneConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
)
from cw.models.client import ReviewConfig, ReviewEngineConfig

_SKILL = "repo-review"


def _pipeline(
    backend: str, *, stages: list[Stage] | None = None
) -> StagePipelineConfig:
    fields: dict[str, object] = {
        "executors": {Stage.REVIEW: StageExecutorConfig(backend=backend)}
    }
    if stages is not None:
        fields["stages"] = stages
    return StagePipelineConfig.model_validate(fields)


def _client(
    name: str = "acme",
    *,
    engine: bool = True,
    lanes: list[LaneConfig] | None = None,
    pipeline: StagePipelineConfig | None = None,
) -> ClientConfig:
    fields: dict[str, object] = {
        "name": name,
        "workspace_path": Path("/dev/null"),
        "review": ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill=_SKILL) if engine else None,
        ),
    }
    if lanes is not None:
        fields["lanes"] = lanes
    if pipeline is not None:
        fields["pipeline"] = pipeline
    return ClientConfig.model_validate(fields)


def test_warns_for_codex_review_lane() -> None:
    client = _client(pipeline=_pipeline("codex"))

    results = _check_review_engine({"acme": client})

    assert len(results) == 1
    assert results[0].name == "review-engine/acme/default"
    assert results[0].ok is True
    assert results[0].warn is True
    assert "backend=codex" in results[0].detail


def test_warns_for_opencode_review_lane() -> None:
    client = _client(pipeline=_pipeline("opencode"))

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]
    assert (results[0].ok, results[0].warn) == (True, True)
    assert "backend=opencode" in results[0].detail


def test_warns_for_local_lane() -> None:
    client = _client(pipeline=_pipeline("local"))

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]
    assert (results[0].ok, results[0].warn) == (True, True)
    assert "backend=local" in results[0].detail


def test_claude_native_lane_emits_nothing() -> None:
    assert _check_review_engine({"acme": _client()}) == []
    explicit = _client(pipeline=_pipeline("claude-native"))
    assert _check_review_engine({"acme": explicit}) == []


def test_lane_without_review_stage_emits_nothing() -> None:
    lane = LaneConfig(
        name="build",
        pipeline=_pipeline("codex", stages=[Stage.PLAN, Stage.IMPL, Stage.FINALIZE]),
    )

    assert _check_review_engine({"acme": _client(lanes=[lane])}) == []


def test_engine_absent_emits_nothing() -> None:
    client = _client(engine=False, pipeline=_pipeline("codex"))

    assert _check_review_engine({"acme": client}) == []


def test_empty_clients_emits_nothing() -> None:
    assert _check_review_engine({}) == []


def test_not_gated_on_is_clean() -> None:
    """The model forbids engine without clean, so build the lane around it."""
    client = ClientConfig.model_construct(
        name="acme",
        workspace_path=Path("/dev/null"),
        lanes=[],
        pipeline=_pipeline("codex"),
        review=ReviewConfig.model_construct(
            convergence="blocking", engine=ReviewEngineConfig(skill=_SKILL)
        ),
    )
    assert client.review.is_clean is False

    results = _check_review_engine({"acme": client})

    assert [r.name for r in results] == ["review-engine/acme/default"]


def test_detail_carries_names_only() -> None:
    client = ClientConfig.model_validate(
        {
            "name": "acme",
            "workspace_path": Path("/dev/null"),
            "review": ReviewConfig(
                convergence="clean",
                engine=ReviewEngineConfig(
                    skill=_SKILL, guarded_paths=["scripts/validate_*.py"]
                ),
            ),
            "pipeline": _pipeline("codex"),
        }
    )

    detail = _check_review_engine({"acme": client})[0].detail

    assert detail == (
        "review.engine is set, but lane 'default' runs REVIEW on backend=codex; "
        "the engine runs only in the claude-native review stage, so this lane "
        "runs the built-in reviewer roster instead."
    )
    assert _SKILL not in detail
    assert "scripts/validate_" not in detail
    assert "/" not in detail
    assert "SKILL.md" not in detail


def test_mixed_lanes_one_claude_native_one_codex_yields_one_warning() -> None:
    lanes = [
        LaneConfig(name="native", pipeline=_pipeline("claude-native")),
        LaneConfig(name="slow", pipeline=_pipeline("codex")),
    ]

    results = _check_review_engine({"acme": _client(lanes=lanes)})

    assert [r.name for r in results] == ["review-engine/acme/slow"]


def test_one_warning_per_client_with_engine() -> None:
    clients = {
        "a": _client("a", pipeline=_pipeline("codex")),
        "b": _client("b", engine=False, pipeline=_pipeline("codex")),
        "c": _client("c", pipeline=_pipeline("opencode")),
    }

    results = _check_review_engine(clients)

    assert [r.name for r in results] == [
        "review-engine/a/default",
        "review-engine/c/default",
    ]


def test_registered_in_client_config_checks() -> None:
    client = _client(pipeline=_pipeline("codex"))

    names = [check.name for check in _client_config_checks({"acme": client})]

    assert "review-engine/acme/default" in names
