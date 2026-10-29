"""Tests for cw.executor._clean_review: the clean-mode REVIEW guard (#2797)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw import models as cw_models
from cw.executor import resolve_executor
from cw.executor._clean_review import (
    CLEAN_REVIEW_GUARDED_BACKENDS,
    CLEAN_REVIEW_UNSUPPORTED_BACKEND,
    clean_review_block_details,
)
from cw.models import (
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.client import ReviewConfig
from cw.review_convergence import clean_mode_supported
from cw.review_convergence._backend import _LOOP_FAMILY

_STAGES = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)
# Every public backend-name constant in ``cw.models`` (the ``DEFAULT_`` liveness
# probe is not a dispatch backend). A new ``*_BACKEND`` constant lands here
# automatically, so the coverage test below fails until it is wired or guarded.
_BACKENDS = tuple(
    sorted(
        value
        for name, value in vars(cw_models).items()
        if name.endswith("_BACKEND")
        and not name.startswith("DEFAULT_")
        and isinstance(value, str)
    )
)
_GUARDED = (OPENCODE_BACKEND, LOCAL_BACKEND)
_SEAM = "cw.executor._clean_review.clean_mode_supported"


def _client(convergence: str) -> ClientConfig:
    review = ReviewConfig.model_validate({"convergence": convergence})
    return ClientConfig(name="c", workspace_path=Path("/tmp/x"), review=review)


@pytest.mark.parametrize("convergence", ["blocking", "clean"])
@pytest.mark.parametrize("stage", _STAGES, ids=[s.value for s in _STAGES])
@pytest.mark.parametrize("backend", _BACKENDS)
def test_only_clean_review_on_a_guarded_backend_returns_details(
    convergence: str, stage: Stage, backend: str
) -> None:
    details = clean_review_block_details(_client(convergence), stage, backend)
    expected = convergence == "clean" and stage == Stage.REVIEW and backend in _GUARDED
    assert (details is not None) is expected


@pytest.mark.parametrize("backend", _GUARDED)
def test_details_name_the_backend_the_ways_out_and_the_requeue_caveat(
    backend: str,
) -> None:
    details = clean_review_block_details(_client("clean"), Stage.REVIEW, backend)
    assert details is not None
    assert f"backend={backend}" in details
    assert "review.convergence: clean" in details
    assert "writes no convergence ledger" in details
    assert "review.convergence: blocking" in details
    assert "claude-native" in details
    assert "codex" in details
    assert "cw dev-queue requeue" in details
    assert "will not clear" in details


def test_only_the_opencode_details_cite_2806() -> None:
    client = _client("clean")
    opencode = clean_review_block_details(client, Stage.REVIEW, OPENCODE_BACKEND)
    local = clean_review_block_details(client, Stage.REVIEW, LOCAL_BACKEND)
    assert opencode is not None
    assert local is not None
    assert "#2806" in opencode
    assert "#2806" not in local


@pytest.mark.parametrize("backend", _GUARDED)
def test_a_supported_backend_lifts_the_guard(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """The #2806 seam: once the engine supports a backend, nothing here blocks."""
    monkeypatch.setattr(_SEAM, lambda _backend: True)
    assert clean_review_block_details(_client("clean"), Stage.REVIEW, backend) is None


def test_guarded_backends_are_exactly_opencode_and_local() -> None:
    assert frozenset({OPENCODE_BACKEND, LOCAL_BACKEND}) == CLEAN_REVIEW_GUARDED_BACKENDS


def test_a_wired_backend_lifts_the_guard_through_the_real_loop_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #2806 one-key lift: adding opencode to ``_LOOP_FAMILY`` unblocks it."""
    monkeypatch.setitem(_LOOP_FAMILY, OPENCODE_BACKEND, CODEX_BACKEND)
    client = _client("clean")
    assert clean_mode_supported(OPENCODE_BACKEND) is True
    assert clean_review_block_details(client, Stage.REVIEW, OPENCODE_BACKEND) is None
    assert clean_review_block_details(client, Stage.REVIEW, LOCAL_BACKEND) is not None


@pytest.mark.parametrize("backend", _BACKENDS)
def test_every_dispatchable_backend_is_supported_or_guarded(backend: str) -> None:
    """Every ``*_BACKEND`` constant in ``cw.models`` is either wired into clean mode
    or covered by the pre-flight guard (a backend may legitimately be both once
    wired: ``clean_mode_supported`` short-circuits the guard).

    ``_BACKENDS`` is derived from ``cw.models``, so a new backend constant fails
    here until it is added to ``_LOOP_FAMILY`` or ``CLEAN_REVIEW_GUARDED_BACKENDS``,
    instead of silently skipping the guard and mislabelling it "unrecognized" in
    the doctor.
    """
    assert clean_mode_supported(backend) or backend in CLEAN_REVIEW_GUARDED_BACKENDS


@pytest.mark.parametrize("backend", _BACKENDS)
def test_resolve_executor_accepts_every_backend_the_guard_classifies(
    tmp_path: Path, backend: str
) -> None:
    """Every ``cw.models`` backend constant is one ``resolve_executor`` dispatches."""
    client = ClientConfig(
        name="c",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend=backend)}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="c", stage=Stage.REVIEW)
    resolve_executor(task, client)


def test_reason_literal() -> None:
    assert CLEAN_REVIEW_UNSUPPORTED_BACKEND == "clean_review_unsupported_backend"
