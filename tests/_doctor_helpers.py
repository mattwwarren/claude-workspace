"""Shared builders and stubs for the doctor check tests.

One home for the lane, client, review-pipeline and ``orchestrator.yaml``
builders that ``test_doctor_codex_fix_loop.py`` (#2542) and
``test_doctor_review_convergence.py`` (#2797) both need, plus the empty
daemon-roster stub (:func:`stub_daemon_roster`) that
``test_doctor_operator_gated_park.py`` and ``test_doctor_scoped_reap.py``
(#2716) both need, so no test file grows its own copy.
"""

from __future__ import annotations

import json
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
from cw.models.client import ReviewConfig, TrackerMcpGateConfig
from cw.native_daemon import FakeNativeDaemonClient

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pytest


def stub_daemon_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    """Point every doctor daemon read at one empty, supervisor-alive roster.

    Mirrors ``tests/test_doctor.py``'s ``_stub_daemon`` plus the readers
    ``run_doctor`` reaches through ``blocked_on_user``, ``orphans`` and
    ``reap``. Shared by ``test_doctor_operator_gated_park.py`` and
    ``test_doctor_scoped_reap.py``. Returns the shared fake daemon so a test
    can ``seed_live_worker`` a live roster entry after the stub is wired.
    """
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(
        json.dumps({"supervisorPid": 12345, "workers": {}}), encoding="utf-8"
    )
    monkeypatch.setattr("cw.doctor.wedge.session_liveness._ROSTER_PATH", roster_path)
    monkeypatch.setattr("cw.doctor.versions._ROSTER_PATH", roster_path)
    daemon = FakeNativeDaemonClient()
    for target in (
        "cw.doctor.wedge.session_liveness",
        "cw.doctor.wedge.blocked_on_user",
        "cw.doctor.wedge.orphans",
        "cw.doctor.wedge.reap",
        "cw.doctor.loop_health",
    ):
        monkeypatch.setattr(f"{target}.get_native_daemon_client", lambda: daemon)
    return daemon


def write_orchestrator_yaml(body: str) -> None:
    """Write *body* to the (test-isolated) ``orchestrator.yaml``."""
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def make_stage_pipeline(
    executors: Mapping[Stage, str | None], stages: Sequence[Stage] | None = None
) -> StagePipelineConfig:
    """A pipeline running each stage in *executors* on its backend (#3055).

    A ``None`` backend means no override for that stage. *stages*, when given,
    is set explicitly, so a lane carrying this pipeline overrides the client's
    stage list.
    """
    configured = {
        stage: StageExecutorConfig(backend=backend)
        for stage, backend in executors.items()
        if backend
    }
    if stages is None:
        return StagePipelineConfig(executors=configured)
    return StagePipelineConfig(executors=configured, stages=list(stages))


def make_review_pipeline(
    backend: str | None, stages: Sequence[Stage] | None = None
) -> StagePipelineConfig:
    """A pipeline whose REVIEW stage runs on *backend* (``None``: no override).

    *stages*, when given, is set explicitly, so a lane carrying this pipeline
    overrides the client's stage list.
    """
    return make_stage_pipeline({Stage.REVIEW: backend}, stages)


def make_lane(
    name: str,
    flag: bool | None = None,
    backend: str | None = None,
    stages: Sequence[Stage] | None = None,
    *,
    executors: Mapping[Stage, str] | None = None,
) -> LaneConfig:
    """A lane with an optional codex fix-loop flag, REVIEW backend and stages.

    *executors* sets other stages' backends (PLAN/IMPL/FINALIZE, #3055);
    *backend* stays the REVIEW backend.
    """
    pipeline = None
    if backend or stages is not None or executors:
        pipeline = make_stage_pipeline(
            {**(executors or {}), Stage.REVIEW: backend}, stages
        )
    return LaneConfig(name=name, codex_fix_loop_enabled=flag, pipeline=pipeline)


def make_client(
    *lanes: LaneConfig,
    backend: str = CODEX_BACKEND,
    review: ReviewConfig | None = None,
    name: str = "client-a",
    executors: Mapping[Stage, str] | None = None,
    tracker_mcp_gate: TrackerMcpGateConfig | None = None,
    workspace_path: Path | None = None,
) -> ClientConfig:
    """A client whose client-level REVIEW stage runs on *backend*.

    *review* ``None`` keeps the default ``ReviewConfig`` (blocking mode).
    *executors* sets other client-level stage backends; *workspace_path*
    defaults to ``/tmp/x`` (doctor tests pass ``tmp_path`` so the tracker and
    the layer probe are real).
    """
    return ClientConfig(
        name=name,
        workspace_path=workspace_path if workspace_path is not None else Path("/tmp/x"),
        lanes=list(lanes),
        pipeline=make_stage_pipeline({**(executors or {}), Stage.REVIEW: backend}),
        review=review if review is not None else ReviewConfig(),
        tracker_mcp_gate=tracker_mcp_gate,
    )
