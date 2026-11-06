"""Tests for ``cw.pr_unarm.ensure_pr_unarmed`` (extracted from the recipe engine)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw import pr_unarm
from cw.gh import PrHead
from cw.pr_unarm import UnarmOutcome, ensure_pr_unarmed
from cw.reconcile.review_recipes.repair_git import _T_DISARM, _T_GH
from tests._changelog_repair_helpers import (
    DISARM_NONE,
    DISARM_NONZERO_STILL_ARMED,
    DISARM_NONZERO_UNARMED,
    DISARM_OK,
    DISARM_ZERO_STILL_ARMED,
    FakeGhHead,
    make_head,
)

PR = "https://github.com/acme/widgets/pull/42"


def _fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    armed: bool = False,
    disarm: str = DISARM_OK,
    unreadable_after_disarm: bool = False,
    reads: dict[int, PrHead | None] | None = None,
) -> FakeGhHead:
    return FakeGhHead(
        [],
        armed=armed,
        disarm=disarm,
        unreadable_after_disarm=unreadable_after_disarm,
        reads=reads,
    ).install(monkeypatch)


def test_already_unarmed_is_ok_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, armed=False)

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out == UnarmOutcome(True, False, "auto-merge not armed")
    assert fake.disarm_calls == 0


def test_armed_then_disarmed_then_read_back_unarmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, armed=True, disarm=DISARM_OK)

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out.ok is True
    assert out.was_armed is True
    assert fake.disarm_calls == 1


def test_namespace_seam_orders_fetch_disarm_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, armed=True)

    ensure_pr_unarmed(PR, cwd=tmp_path)

    assert fake.log == [
        ("gh", "fetch_pr_head"),
        ("gh", "disarm"),
        ("gh", "fetch_pr_head"),
    ]


def test_disarm_that_cannot_run_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, armed=True, disarm=DISARM_NONE)

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out.ok is False
    assert out.was_armed is True


def test_unreadable_head_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, reads={0: None})

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out == UnarmOutcome(False, None, "PR head unreadable; armed state unknown")


def test_unreadable_read_back_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, armed=True, unreadable_after_disarm=True)

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out.ok is False
    assert "unreadable" in out.detail


@pytest.mark.parametrize("mode", [DISARM_NONZERO_STILL_ARMED, DISARM_ZERO_STILL_ARMED])
def test_still_armed_read_back_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    _fake(monkeypatch, armed=True, disarm=mode)

    out = ensure_pr_unarmed(PR, cwd=tmp_path)

    assert out.ok is False
    assert "still ARMED" in out.detail


def test_nonzero_disarm_exit_with_unarmed_read_back_is_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, armed=True, disarm=DISARM_NONZERO_UNARMED)

    assert ensure_pr_unarmed(PR, cwd=tmp_path).ok is True


def test_a_supplied_head_is_reused_without_a_first_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, armed=True)

    out = ensure_pr_unarmed(PR, cwd=tmp_path, head=make_head(armed=False))

    assert out.ok is True
    assert fake.log == []


def test_default_timeouts_match_the_repair_engine_constants() -> None:
    assert pr_unarm.DEFAULT_READ_TIMEOUT == _T_GH
    assert pr_unarm.DEFAULT_DISARM_TIMEOUT == _T_DISARM


def test_explicit_timeouts_are_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, armed=False)

    ensure_pr_unarmed(PR, cwd=tmp_path, read_timeout=7)

    assert fake.timeouts == [7]
