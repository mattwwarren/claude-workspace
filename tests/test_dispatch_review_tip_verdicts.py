"""The in-lock REVIEW staleness gate reads the installed tip verdict (#2994)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest

from cw.dispatch import review_gates
from cw.dispatch.review_tip_verdicts import (
    TipCheck,
    TipKey,
    installed_tip_verdict,
    precomputed_tip_verdicts,
)

_KEY = TipKey("c", "T-1", "reviewed", "head")


def _plan() -> TipCheck:
    return TipCheck(
        key=_KEY, client=cast("Any", None), worktree=Path("/wt"), task=cast("Any", None)
    )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Pin the planner to a TipCheck and record every inline tip check."""
    seen: dict[str, Any] = {"inline": []}

    def fake_tolerates(
        _c: Any, _w: Path, *, reviewed_sha: str, head_sha: str, task: Any
    ) -> bool:
        seen["inline"].append((reviewed_sha, head_sha))
        return False

    monkeypatch.setattr(review_gates, "plan_review_staleness", lambda *_a: _plan())
    monkeypatch.setattr(review_gates, "tolerates_review_move", fake_tolerates)
    return seen


def _gates(verdict_scope: dict[TipKey, bool] | None) -> bool:
    with precomputed_tip_verdicts(verdict_scope):
        return review_gates._should_gate_for_review_staleness(cast("Any", None), {}, {})


def test_an_installed_true_verdict_does_not_gate_and_runs_no_inline_check(
    gate: dict[str, Any],
) -> None:
    assert _gates({_KEY: True}) is False
    assert gate["inline"] == []


def test_an_installed_false_verdict_gates_and_runs_no_inline_check(
    gate: dict[str, Any],
) -> None:
    assert _gates({_KEY: False}) is True
    assert gate["inline"] == []


def test_a_key_miss_under_a_scope_checks_inline_and_warns(
    gate: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        assert _gates({}) is True  # inline answer: not tolerated, so gate
    assert gate["inline"] == [("reviewed", "head")]
    assert caplog.text.count("no precomputed verdict") == 1


def test_no_scope_runs_the_tip_check_inline_silently(
    gate: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
        assert _gates(None) is True
    assert gate["inline"] == [("reviewed", "head")]
    assert caplog.text == ""


def test_the_verdict_scope_resets_after_the_block(gate: dict[str, Any]) -> None:
    with precomputed_tip_verdicts({_KEY: True}):
        assert installed_tip_verdict(_KEY) is True
    assert installed_tip_verdict(_KEY) is None
