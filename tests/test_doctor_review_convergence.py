"""Tests for cw.doctor.review_convergence (#2797, opencode lane #2806)."""

from __future__ import annotations

import pytest

from cw.doctor.review_convergence import _check_review_convergence
from cw.executor._clean_review import CLEAN_REVIEW_UNSUPPORTED_BACKEND
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    Stage,
)
from cw.models.client import ReviewConfig
from tests._doctor_helpers import make_client, make_lane

_BLOCKED = "blocked in pre-flight, before the review agent runs"
_EARLIER_STAGES = "earlier stages still run"
_REMEDIATION = (
    "set review.convergence: blocking",
    "claude-native, codex or opencode REVIEW executor",
)
_EGRESS = "Default-on egress"


def _clean(confirm: str | None = None, max_cycles: int | None = None) -> ReviewConfig:
    return ReviewConfig.model_validate(
        {"convergence": "clean", "confirm_backend": confirm, "max_cycles": max_cycles}
    )


def test_empty_clients_returns_no_results() -> None:
    assert _check_review_convergence({}) == []


@pytest.mark.parametrize(
    "backend", [CLAUDE_NATIVE_BACKEND, CODEX_BACKEND, OPENCODE_BACKEND, LOCAL_BACKEND]
)
def test_blocking_client_emits_nothing(backend: str) -> None:
    client = make_client(make_lane("a"), make_lane("b"), backend=backend)
    assert _check_review_convergence({"client-a": client}) == []


def test_clean_claude_native_default_lane_is_ok() -> None:
    client = make_client(backend=CLAUDE_NATIVE_BACKEND, review=_clean())
    (result,) = _check_review_convergence({"client-a": client})
    assert result.name == "review-convergence/client-a/default"
    assert result.ok is True
    assert result.warn is False
    assert result.detail == (
        "effective mode clean: claude-native REVIEW lane writes the convergence"
        " ledger; confirm backend codex (not_independent=False); max_cycles 8."
    )


@pytest.mark.parametrize(
    ("confirm", "fragment"),
    [
        (None, "confirm backend claude (not_independent=False)"),
        ("none", "confirm backend none (not_independent=False)"),
        ("codex", "confirm backend codex (not_independent=True)"),
    ],
    ids=["unset", "none", "codex"],
)
def test_clean_codex_lane_names_the_resolved_confirm_backend(
    confirm: str | None, fragment: str
) -> None:
    client = make_client(backend=CODEX_BACKEND, review=_clean(confirm, max_cycles=3))
    (result,) = _check_review_convergence({"client-a": client})
    assert result.ok is True
    assert result.warn is False
    assert "codex REVIEW lane writes the convergence ledger" in result.detail
    assert fragment in result.detail
    assert "max_cycles 3" in result.detail


def test_none_confirm_says_no_independent_round_runs() -> None:
    client = make_client(backend=CODEX_BACKEND, review=_clean("none"))
    (result,) = _check_review_convergence({"client-a": client})
    assert "no independent confirm round runs" in result.detail


def test_not_independent_confirm_is_called_out() -> None:
    client = make_client(backend=CODEX_BACKEND, review=_clean("codex"))
    (result,) = _check_review_convergence({"client-a": client})
    assert "The confirm round is not independent of the loop." in result.detail


def test_opencode_lane_override_is_ok_with_the_claude_confirm() -> None:
    """#2806: an opencode lane writes the ledger; unset confirm resolves claude."""
    client = make_client(
        make_lane("oc", backend=OPENCODE_BACKEND),
        backend=CLAUDE_NATIVE_BACKEND,
        review=_clean(),
    )
    (result,) = _check_review_convergence({"client-a": client})
    assert result.name == "review-convergence/client-a/oc"
    assert result.ok is True
    assert result.warn is False
    assert "opencode REVIEW lane writes the convergence ledger" in result.detail
    assert "confirm backend claude (not_independent=False)" in result.detail


def test_opencode_lane_with_unset_confirm_carries_the_egress_note() -> None:
    client = make_client(backend=OPENCODE_BACKEND, review=_clean())
    (result,) = _check_review_convergence({"client-a": client})
    assert _EGRESS in result.detail
    assert "sends each confirm round's diff to Claude" in result.detail
    assert "confirm_backend: codex is not an opt-out" in result.detail
    assert "review.confirm_backend: none" in result.detail
    assert "forfeits the independent round" in result.detail
    assert "begin this egress on their next requeue" in result.detail


@pytest.mark.parametrize("confirm", ["none", "claude", "codex"])
def test_opencode_lane_with_an_explicit_confirm_has_no_egress_note(
    confirm: str,
) -> None:
    client = make_client(backend=OPENCODE_BACKEND, review=_clean(confirm))
    (result,) = _check_review_convergence({"client-a": client})
    assert result.ok is True
    assert _EGRESS not in result.detail
    assert f"confirm backend {confirm} (not_independent=False)" in result.detail


@pytest.mark.parametrize("backend", [CLAUDE_NATIVE_BACKEND, CODEX_BACKEND])
def test_only_an_opencode_lane_gets_the_egress_note(backend: str) -> None:
    client = make_client(backend=backend, review=_clean())
    (result,) = _check_review_convergence({"client-a": client})
    assert _EGRESS not in result.detail


def test_local_lane_fails_with_the_guard_reason() -> None:
    client = make_client(backend=LOCAL_BACKEND, review=_clean())
    (result,) = _check_review_convergence({"client-a": client})
    assert result.ok is False
    assert "backend=local" in result.detail
    assert _BLOCKED in result.detail
    assert _EARLIER_STAGES in result.detail
    assert CLEAN_REVIEW_UNSUPPORTED_BACKEND in result.detail
    assert "#2806" not in result.detail
    for fragment in _REMEDIATION:
        assert fragment in result.detail


def test_unrecognized_backend_fails_without_claiming_a_typed_block() -> None:
    client = make_client(backend="bogus", review=_clean())
    (result,) = _check_review_convergence({"client-a": client})
    assert result.ok is False
    assert "backend=bogus" in result.detail
    assert "dispatch cannot resolve it" in result.detail
    assert "unknown executor backend" in result.detail
    assert CLEAN_REVIEW_UNSUPPORTED_BACKEND not in result.detail
    assert _BLOCKED not in result.detail
    assert _EARLIER_STAGES not in result.detail
    for fragment in _REMEDIATION:
        assert fragment in result.detail


def test_lane_level_claude_native_override_only_the_other_lane_fails() -> None:
    client = make_client(
        make_lane("native", backend=CLAUDE_NATIVE_BACKEND),
        make_lane("plain"),
        backend=LOCAL_BACKEND,
        review=_clean(),
    )
    results = _check_review_convergence({"client-a": client})
    assert [(r.name, r.ok) for r in results] == [
        ("review-convergence/client-a/native", True),
        ("review-convergence/client-a/plain", False),
    ]


def test_lane_without_a_review_stage_warns() -> None:
    client = make_client(
        make_lane("no-review", stages=[Stage.PLAN, Stage.IMPL, Stage.FINALIZE]),
        backend=CLAUDE_NATIVE_BACKEND,
        review=_clean(),
    )
    (result,) = _check_review_convergence({"client-a": client})
    assert result.ok is True
    assert result.warn is True
    assert "no REVIEW stage" in result.detail
    assert "nothing writes the convergence ledger" in result.detail


def test_lanes_and_clients_listed_in_declared_order() -> None:
    clean = make_client(
        make_lane("a"),
        make_lane("b", backend=LOCAL_BACKEND),
        make_lane("c", backend=CODEX_BACKEND),
        backend=CLAUDE_NATIVE_BACKEND,
        review=_clean(),
    )
    blocking = make_client(name="plain", backend=OPENCODE_BACKEND)
    results = _check_review_convergence({"plain": blocking, "client-a": clean})
    assert [(r.name, r.ok) for r in results] == [
        ("review-convergence/client-a/a", True),
        ("review-convergence/client-a/b", False),
        ("review-convergence/client-a/c", True),
    ]
