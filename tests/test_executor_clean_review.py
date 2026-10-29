"""Tests for cw.executor._clean_review: the clean-mode REVIEW guard (#2797)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.executor._clean_review import (
    CLEAN_REVIEW_GUARDED_BACKENDS,
    CLEAN_REVIEW_UNSUPPORTED_BACKEND,
    clean_review_block_details,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    Stage,
)
from cw.models.client import ReviewConfig

_STAGES = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)
_BACKENDS = (CLAUDE_NATIVE_BACKEND, CODEX_BACKEND, OPENCODE_BACKEND, LOCAL_BACKEND)
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


def test_reason_literal() -> None:
    assert CLEAN_REVIEW_UNSUPPORTED_BACKEND == "clean_review_unsupported_backend"
