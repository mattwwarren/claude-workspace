"""Hermetic tests for the clean-sha remap proof (#2921).

``ScriptedGit`` stands in for ``run_git`` and freezegun for the clock, so no real
git runs. ``tests/test_repair_remap.py`` covers the same proof against real
repos; here every unprovable variant is scripted, and a kill-check pair proves
the per-commit equality predicate is what stops a changed commit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time

from cw.lane_pause import iso_z, utc_now_seconds
from cw.reconcile.review_recipes import (
    RECIPE_REPAIR_CHANGELOG_CONFLICT,
    changelog_repair,
    repair_git,
)
from cw.repair_disposition import RepairDisposition
from cw.review_convergence import CleanShaRemap
from tests._changelog_repair_helpers import (
    DEFAULT_REF,
    EXPECTED_SHA,
    MB_SHA,
    NEW_SHA,
    OSERROR,
    PR_NUMBER,
    RANGE_DIFF_NOT_EQUAL,
    REBASED_SHA,
    TICKET,
    TIMEOUT,
    GitResult,
    LogEntry,
    ScriptedGit,
    range_diff_equal,
    range_diff_output,
    stub_remap_proof,
)

FROZEN = "2026-11-07T12:00:00Z"
SCRATCH = Path("scratch")
SKIPPED = "changelog_repair_ledger_remap_skipped"
STRAY_PATH = "src/stray.py\0"
CHANGED_PATHS_ARGV = ("diff", "--name-only", "-z", REBASED_SHA, NEW_SHA)

Configure = Callable[[ScriptedGit], None]


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> ScriptedGit:
    return ScriptedGit(log, []).install(monkeypatch)


def _job() -> Any:
    return SimpleNamespace(
        client="acme",
        ticket_id=TICKET,
        spec=SimpleNamespace(recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT),
    )


def _prep() -> Any:
    return SimpleNamespace(
        merge_base=MB_SHA,
        expected_sha=EXPECTED_SHA,
        default_ref=DEFAULT_REF,
        pr_number=PR_NUMBER,
    )


def _prove(rebased: str = REBASED_SHA, new: str = NEW_SHA) -> CleanShaRemap | None:
    return changelog_repair._prove_remap(_job(), _prep(), SCRATCH, rebased, new)


def _series_proof(rebased: str = REBASED_SHA, new: str = NEW_SHA) -> int:
    return repair_git._prove_series_unchanged(
        SCRATCH, MB_SHA, EXPECTED_SHA, DEFAULT_REF, rebased, new
    )


def _verbs(git: ScriptedGit) -> list[str]:
    return [argv[0] for argv in git.stripped()]


# --- the proof holds -----------------------------------------------------------


@pytest.mark.parametrize("pairs", [1, 3])
def test_equal_series_plus_changelog_commit_is_proven(
    git: ScriptedGit, pairs: int
) -> None:
    stub_remap_proof(git, range_diff=range_diff_equal(pairs))

    with freeze_time(FROZEN):
        remap = _prove()

    assert remap == CleanShaRemap(
        at=FROZEN,
        recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
        from_sha=EXPECTED_SHA,
        rebased_sha=REBASED_SHA,
        to_sha=NEW_SHA,
        commits=pairs,
    )


def test_the_audit_stamp_is_the_clock_read_at_proof_time(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    with freeze_time("2031-01-02T03:04:05Z"):
        remap = _prove()
        expected = iso_z(utc_now_seconds())

    assert remap is not None
    assert remap.at == expected == "2031-01-02T03:04:05Z"


def test_range_diff_compares_the_old_series_with_the_rebased_one(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git)

    assert _prove() is not None

    (ranged,) = [argv for argv in git.stripped() if argv[0] == "range-diff"]
    assert ranged[-2:] == (f"{MB_SHA}..{EXPECTED_SHA}", f"{DEFAULT_REF}..{REBASED_SHA}")


def test_no_changelog_commit_is_allowed_and_checks_no_delta(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    with freeze_time(FROZEN):
        remap = _prove(rebased=REBASED_SHA, new=REBASED_SHA)

    assert remap is not None
    assert remap.rebased_sha == remap.to_sha == REBASED_SHA
    assert _verbs(git) == ["range-diff"]


# --- every unprovable variant fails closed --------------------------------------


def _answer(prefix: tuple[str, ...], result: Any) -> Configure:
    def configure(git: ScriptedGit) -> None:
        stub_remap_proof(git)
        git.set(prefix, result)

    return configure


def _stub(**kwargs: Any) -> Configure:
    def configure(git: ScriptedGit) -> None:
        stub_remap_proof(git, **kwargs)

    return configure


def _pairs(*marks: str) -> Configure:
    return _stub(range_diff=GitResult(stdout=range_diff_output(*marks)))


UNPROVABLE = [
    pytest.param(_stub(range_diff=RANGE_DIFF_NOT_EQUAL), id="changed-pair"),
    pytest.param(_pairs("=", "<"), id="dropped-commit"),
    pytest.param(_pairs("=", ">"), id="added-commit"),
    pytest.param(_pairs("<", ">"), id="swapped-commit"),
    pytest.param(_stub(range_diff=GitResult()), id="empty-output"),
    pytest.param(
        _stub(range_diff=GitResult(stdout="not a pair line\n")), id="unparseable-line"
    ),
    pytest.param(
        _stub(range_diff=GitResult(returncode=128, stderr="fatal")), id="nonzero-exit"
    ),
    pytest.param(_stub(range_diff=TIMEOUT), id="timeout"),
    pytest.param(_stub(range_diff=OSERROR), id="oserror"),
    pytest.param(_stub(changed=STRAY_PATH), id="non-changelog-path-changed"),
    pytest.param(
        _stub(changed=f"CHANGELOG.md\0{STRAY_PATH}"), id="stray-with-changelog"
    ),
    pytest.param(_stub(parent="f" * 40), id="parent-mismatch"),
    pytest.param(
        _answer(("rev-parse", f"{NEW_SHA}^"), GitResult(1)), id="parent-read-fails"
    ),
    pytest.param(
        _answer(CHANGED_PATHS_ARGV, GitResult(returncode=2, stderr="boom")),
        id="failing-diff",
    ),
    pytest.param(_answer(CHANGED_PATHS_ARGV, TIMEOUT), id="diff-timeout"),
]


@pytest.mark.parametrize("configure", UNPROVABLE)
def test_unprovable_rebase_returns_none_and_logs_a_skip(
    git: ScriptedGit, configure: Configure, caplog: pytest.LogCaptureFixture
) -> None:
    configure(git)

    with caplog.at_level(logging.INFO):
        remap = _prove()

    assert remap is None
    skips = [r.getMessage() for r in caplog.records if SKIPPED in r.getMessage()]
    assert len(skips) == 1
    assert f"ticket={TICKET}" in skips[0]
    assert f"new_sha={NEW_SHA}" in skips[0]


@pytest.mark.parametrize("configure", UNPROVABLE)
def test_unprovable_rebase_raises_verify_failed_from_the_series_proof(
    git: ScriptedGit, configure: Configure
) -> None:
    configure(git)

    with pytest.raises(repair_git.RepairStopError) as caught:
        _series_proof()

    assert caught.value.disposition == RepairDisposition.VERIFY_FAILED


# --- kill-check pair: the equality predicate is load-bearing ---------------------


def test_non_equal_commit_is_not_provable(git: ScriptedGit) -> None:
    stub_remap_proof(git, range_diff=RANGE_DIFF_NOT_EQUAL)

    assert _prove() is None


def test_equality_check_is_load_bearing(
    git: ScriptedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the predicate forced true the same ``!`` series IS proven.

    Paired with ``test_non_equal_commit_is_not_provable``: together they show
    ``_all_pairs_equal`` alone is what rejects a changed commit, so a mutated
    predicate cannot pass unnoticed.
    """
    stub_remap_proof(git, range_diff=RANGE_DIFF_NOT_EQUAL)
    monkeypatch.setattr(repair_git, "_all_pairs_equal", lambda _marks: True)

    assert _prove() is not None
