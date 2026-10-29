"""Shared config builders for the per-lane doctor check tests.

One home for the lane, client, review-pipeline and ``orchestrator.yaml``
builders that ``test_doctor_codex_fix_loop.py`` (#2542) and
``test_doctor_review_convergence.py`` (#2797) both need, so neither test file
grows its own copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cw.config import orchestrator_config_file
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    LaneConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
)
from cw.models.client import ReviewConfig

if TYPE_CHECKING:
    from collections.abc import Sequence


def write_orchestrator_yaml(body: str) -> None:
    """Write *body* to the (test-isolated) ``orchestrator.yaml``."""
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def make_review_pipeline(
    backend: str | None, stages: Sequence[Stage] | None = None
) -> StagePipelineConfig:
    """A pipeline whose REVIEW stage runs on *backend* (``None``: no override).

    *stages*, when given, is set explicitly, so a lane carrying this pipeline
    overrides the client's stage list.
    """
    executors = {Stage.REVIEW: StageExecutorConfig(backend=backend)} if backend else {}
    if stages is None:
        return StagePipelineConfig(executors=executors)
    return StagePipelineConfig(executors=executors, stages=list(stages))


def make_lane(
    name: str,
    flag: bool | None = None,
    backend: str | None = None,
    stages: Sequence[Stage] | None = None,
) -> LaneConfig:
    """A lane with an optional codex fix-loop flag, REVIEW backend and stages."""
    pipeline = (
        make_review_pipeline(backend, stages) if backend or stages is not None else None
    )
    return LaneConfig(name=name, codex_fix_loop_enabled=flag, pipeline=pipeline)


def make_client(
    *lanes: LaneConfig,
    backend: str = CODEX_BACKEND,
    review: ReviewConfig | None = None,
    name: str = "client-a",
) -> ClientConfig:
    """A client whose client-level REVIEW stage runs on *backend*.

    *review* ``None`` keeps the default ``ReviewConfig`` (blocking mode).
    """
    return ClientConfig(
        name=name,
        workspace_path=Path("/tmp/x"),
        lanes=list(lanes),
        pipeline=make_review_pipeline(backend),
        review=review if review is not None else ReviewConfig(),
    )
