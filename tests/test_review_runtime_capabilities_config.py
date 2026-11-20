"""Config layer for runtime capabilities (#3790).

``review.engine.requires_runtime_capabilities`` and
``pipeline.executors.review.runtime_capabilities`` are operator-owned slug
lists. These tests pin the grammar, the cap, the normalisation, the REVIEW-only
rule, the ``extra=forbid`` contract, the untouched ``to_context()`` and the
lane-replaces-client semantics of the resolver.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.executor import resolve_executor_config
from cw.models import (
    ClientConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.models.orchestrator_config.lane import LaneConfig
from cw.models.runtime_capabilities import (
    MAX_RUNTIME_CAPABILITIES,
    MAX_RUNTIME_CAPABILITY_LENGTH,
    normalize_runtime_capabilities,
)


def _engine(caps: list[str]) -> ReviewEngineConfig:
    return ReviewEngineConfig(skill="review-skill", requires_runtime_capabilities=caps)


def _review(caps: list[str]) -> ReviewConfig:
    return ReviewConfig(convergence="clean", engine=_engine(caps))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["jvm", "cuda", "jvm"], ["cuda", "jvm"]),
        (["b", "a"], ["a", "b"]),
        (["gpu-0.2", "x_y"], ["gpu-0.2", "x_y"]),
        ([], []),
    ],
)
def test_valid_slugs_normalise_sorted_and_deduplicated(
    raw: list[str], expected: list[str]
) -> None:
    assert normalize_runtime_capabilities(raw, field="f") == expected
    assert _engine(raw).requires_runtime_capabilities == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "JVM",
        "has space",
        "a/b",
        "..",
        "a..b",
        "-lead",
        ".lead",
        "_lead",
        "x" * (MAX_RUNTIME_CAPABILITY_LENGTH + 1),
    ],
)
def test_invalid_slugs_are_rejected_naming_the_field(bad: str) -> None:
    with pytest.raises(ValidationError, match="requires_runtime_capabilities"):
        _engine([bad])


def test_non_string_slug_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewEngineConfig.model_validate(
            {"skill": "s", "requires_runtime_capabilities": [1]}
        )


def test_cap_allows_exactly_the_maximum() -> None:
    caps = [f"c{i:02d}" for i in range(MAX_RUNTIME_CAPABILITIES)]
    assert _engine(caps).requires_runtime_capabilities == caps


def test_cap_rejects_one_over_naming_the_first_excess_entry() -> None:
    caps = [f"c{i:02d}" for i in range(MAX_RUNTIME_CAPABILITIES + 1)]
    with pytest.raises(ValidationError, match="first excess entry"):
        _engine(caps)


def test_cap_counts_raw_entries_before_deduplication() -> None:
    raw = ["jvm"] * (MAX_RUNTIME_CAPABILITIES + 1)
    with pytest.raises(ValidationError, match="first excess entry"):
        _engine(raw)


def test_engine_model_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ReviewEngineConfig.model_validate(
            {"skill": "s", "requires_runtime_capability": ["jvm"]}
        )


def test_stage_executor_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        StageExecutorConfig.model_validate({"runtime_capability": ["jvm"]})


def test_pipeline_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        StagePipelineConfig.model_validate({"runtime_capabilities": ["jvm"]})


def test_executor_normalises_its_list() -> None:
    cfg = StageExecutorConfig(runtime_capabilities=["jvm", "cuda", "jvm"])
    assert cfg.runtime_capabilities == ["cuda", "jvm"]


def test_executor_list_is_validated() -> None:
    with pytest.raises(ValidationError, match="runtime_capabilities"):
        StageExecutorConfig(runtime_capabilities=["Bad Slug"])


def test_runtime_capabilities_accepted_on_review_stage() -> None:
    pipeline = StagePipelineConfig(
        executors={Stage.REVIEW: StageExecutorConfig(runtime_capabilities=["jvm"])}
    )
    assert pipeline.executors[Stage.REVIEW].runtime_capabilities == ["jvm"]


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.IMPL, Stage.FINALIZE])
def test_runtime_capabilities_rejected_off_review(stage: Stage) -> None:
    with pytest.raises(ValidationError, match="only honored on the review stage"):
        StagePipelineConfig(
            executors={stage: StageExecutorConfig(runtime_capabilities=["jvm"])}
        )


def test_empty_runtime_capabilities_accepted_on_any_stage() -> None:
    pipeline = StagePipelineConfig(
        executors={
            Stage.PLAN: StageExecutorConfig(runtime_capabilities=[]),
            Stage.REVIEW: StageExecutorConfig(runtime_capabilities=[]),
        }
    )
    assert pipeline.executors[Stage.PLAN].runtime_capabilities == []


def test_engine_round_trip_is_stable() -> None:
    cfg = _engine(["jvm", "cuda"])
    again = ReviewEngineConfig.model_validate(cfg.model_dump())
    assert again == cfg


def test_to_context_is_byte_identical_with_and_without_requirements() -> None:
    plain = ReviewConfig(
        convergence="clean",
        engine=ReviewEngineConfig(skill="review-skill"),
    )
    required = _review(["jvm", "cuda"])
    assert plain.to_context() == required.to_context()
    assert "requires_runtime_capabilities" not in str(required.to_context())


def test_client_config_loads_both_keys_from_a_mapping(tmp_path: Path) -> None:
    # Validate from a plain mapping, as clients.yaml does. A model_dump round
    # trip is not used: the dumped claude-native REVIEW executor carries
    # reasoning_effort, which the codex-only validator refuses on reload.
    client = ClientConfig.model_validate(
        {
            "name": "rc-client",
            "workspace_path": str(tmp_path),
            "default_branch": "main",
            "review": {
                "convergence": "clean",
                "engine": {
                    "skill": "review-skill",
                    "requires_runtime_capabilities": ["jvm", "cuda", "jvm"],
                },
            },
            "pipeline": {
                "executors": {"review": {"runtime_capabilities": ["jvm"]}},
            },
        }
    )
    assert client.review.engine is not None
    assert client.review.engine.requires_runtime_capabilities == ["cuda", "jvm"]
    assert client.pipeline.executors[Stage.REVIEW].runtime_capabilities == ["jvm"]


def test_lane_review_executor_replaces_the_client_wholesale(tmp_path: Path) -> None:
    client_pipeline = StagePipelineConfig(
        executors={Stage.REVIEW: StageExecutorConfig(runtime_capabilities=["jvm"])}
    )
    lane = LaneConfig(
        name="plain",
        pipeline=StagePipelineConfig(executors={Stage.REVIEW: StageExecutorConfig()}),
    )
    client = ClientConfig(
        name="rc-client",
        workspace_path=tmp_path,
        default_branch="main",
        pipeline=client_pipeline,
        lanes=[lane],
    )
    task = TicketTask(
        ticket_id="T", client="rc-client", lane="plain", stage=Stage.REVIEW
    )
    assert (
        resolve_executor_config(Stage.REVIEW, task, client).runtime_capabilities == []
    )


def test_lane_without_review_executor_inherits_the_client(tmp_path: Path) -> None:
    client_pipeline = StagePipelineConfig(
        executors={Stage.REVIEW: StageExecutorConfig(runtime_capabilities=["jvm"])}
    )
    lane = LaneConfig(name="inherit")
    client = ClientConfig(
        name="rc-client",
        workspace_path=tmp_path,
        default_branch="main",
        pipeline=client_pipeline,
        lanes=[lane],
    )
    task = TicketTask(
        ticket_id="T", client="rc-client", lane="inherit", stage=Stage.REVIEW
    )
    resolved = resolve_executor_config(Stage.REVIEW, task, client)
    assert resolved.runtime_capabilities == ["jvm"]
