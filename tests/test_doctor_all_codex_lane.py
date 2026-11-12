"""Tests for cw.doctor.all_codex_lane (RFC 0018 D1, #3064)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.doctor.all_codex_lane import _check_all_codex_lane
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    Stage,
)
from cw.models.client import ReviewConfig
from tests._doctor_helpers import make_client, make_lane

if TYPE_CHECKING:
    from cw.doctor._shared import CheckResult
    from cw.models import ClientConfig, LaneConfig

_ALL_CODEX: dict[Stage, str] = {
    Stage.PLAN: CODEX_BACKEND,
    Stage.IMPL: CODEX_BACKEND,
    Stage.REVIEW: CODEX_BACKEND,
    Stage.FINALIZE: CODEX_BACKEND,
}
_TRACKER = "tracker-mcp/client-a/default/codex"


def _clean(confirm: str | None = None) -> ReviewConfig:
    return ReviewConfig.model_validate(
        {"convergence": "clean", "confirm_backend": confirm}
    )


def _client(
    executors: dict[Stage, str] | None = None,
    lanes: tuple[LaneConfig, ...] = (),
    review: ReviewConfig | None = None,
) -> ClientConfig:
    """An all-codex client; *executors* overrides individual stages."""
    merged = {**_ALL_CODEX, **(executors or {})}
    return make_client(
        *lanes,
        backend=merged[Stage.REVIEW],
        executors={k: v for k, v in merged.items() if k is not Stage.REVIEW},
        review=review,
    )


def _single(client: ClientConfig) -> CheckResult:
    (result,) = _check_all_codex_lane({"client-a": client})
    return result


def test_empty_clients_returns_no_results() -> None:
    assert _check_all_codex_lane({}) == []


@pytest.mark.parametrize(
    "stage", [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
)
@pytest.mark.parametrize(
    "other", [CLAUDE_NATIVE_BACKEND, OPENCODE_BACKEND, LOCAL_BACKEND]
)
def test_lane_with_a_non_codex_stage_gives_no_result(stage: Stage, other: str) -> None:
    client = _client({stage: other}, review=_clean(None))
    assert _check_all_codex_lane({"client-a": client}) == []


def test_empty_stages_gives_no_result() -> None:
    lane = make_lane("default", stages=[])
    assert _check_all_codex_lane({"client-a": _client(None, (lane,))}) == []


def test_lane_with_harden_gives_no_result() -> None:
    lane = make_lane(
        "default",
        executors={Stage.HARDEN: CODEX_BACKEND},
        stages=[Stage.HARDEN, Stage.REVIEW],
    )
    assert _check_all_codex_lane({"client-a": _client(None, (lane,))}) == []


def test_blocking_all_codex_reports_ok_without_warning() -> None:
    result = _single(_client())
    assert result.name == "all-codex-lane/client-a/default"
    assert result.ok is True
    assert result.warn is False
    assert "no Claude executor is involved" in result.detail
    assert "Blocking mode runs no confirm round" in result.detail
    assert _TRACKER in result.detail


def test_clean_unset_confirm_warns_with_claude_dependency() -> None:
    result = _single(_client(review=_clean(None)))
    assert result.ok is True
    assert result.warn is True
    detail = result.detail
    assert "claude -p" in detail
    assert "claude binary on PATH" in detail
    assert "confirm_backend_unavailable" in detail
    assert "review.confirm_backend: none" in detail
    assert "review.confirm_backend: codex" in detail
    assert "not independent" in detail
    assert _TRACKER in detail


def test_clean_none_is_ok_without_warning() -> None:
    result = _single(_client(review=_clean("none")))
    assert result.ok is True
    assert result.warn is False
    assert "no independent confirm round runs" in result.detail


def test_clean_codex_is_ok_and_flags_not_independent() -> None:
    result = _single(_client(review=_clean("codex")))
    assert result.ok is True
    assert result.warn is False
    assert "not independent" in result.detail


def test_clean_explicit_claude_is_ok_and_notes_deliberate_dependency() -> None:
    result = _single(_client(review=_clean("claude")))
    assert result.ok is True
    assert result.warn is False
    assert "deliberate" in result.detail


def test_clean_without_review_stage_is_ok_and_confirm_does_not_apply() -> None:
    lane = make_lane("default", stages=[Stage.PLAN, Stage.IMPL, Stage.FINALIZE])
    result = _single(_client(None, (lane,), review=_clean(None)))
    assert result.ok is True
    assert result.warn is False
    assert "does not apply" in result.detail


def test_lane_level_override_gives_exactly_one_result() -> None:
    mixed = make_lane("claude-lane", executors={Stage.PLAN: CLAUDE_NATIVE_BACKEND})
    codex_only = make_lane("codex-lane")
    client = _client(None, (mixed, codex_only), review=_clean("none"))
    results = _check_all_codex_lane({"client-a": client})
    assert [r.name for r in results] == ["all-codex-lane/client-a/codex-lane"]


def test_lane_level_stage_override_is_honoured() -> None:
    lane = make_lane("slim", stages=[Stage.PLAN, Stage.IMPL])
    results = _check_all_codex_lane({"client-a": _client(None, (lane,))})
    assert [r.name for r in results] == ["all-codex-lane/client-a/slim"]


def test_detail_carries_names_only_never_raw_config() -> None:
    detail = _single(_client(review=_clean(None))).detail
    assert "/tmp" not in detail
