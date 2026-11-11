"""Tests for the clean-sha carry of the repair recipe (#2921).

Real temp git repos for the per-commit ``range-diff`` proof and the CHANGELOG-only
delta; a stubbed ``_git`` for the parser's failure branches. The fence and the
compare-and-set persist are exercised against real ledger files.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time

from cw.reconcile.review_recipes import changelog_repair, repair_git, repair_ledger
from cw.reconcile.review_recipes import repair_engine as engine
from cw.reconcile.review_recipes.repair_fence import (
    FenceSnapshot,
    FenceVerdict,
    evaluate_repair_fence,
)
from cw.repair_disposition import RepairDisposition
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    CleanShaRemap,
    ConvergenceLedger,
    load_ledger,
)
from tests._review_convergence_helpers import TICKET_ID, confirmed_ledger, write_ledger
from tests._shared.git_repos import commit_tracked_file, git_in

OLD = "old-head-sha"
NEW = "new-head-sha"
AT = "2026-11-07T12:00:00Z"
CHANGELOG_BYTES = "# Changelog\n\n## [Unreleased]\n"


def _remap(**fields: object) -> CleanShaRemap:
    base: dict[str, object] = {
        "at": AT,
        "recipe": "repair_changelog_conflict",
        "from_sha": OLD,
        "rebased_sha": "rebased-sha",
        "to_sha": NEW,
        "commits": 2,
    }
    base.update(fields)
    return CleanShaRemap.model_validate(base)


def _snapshot(ledger_path: Path | None, **fields: object) -> FenceSnapshot:
    base = FenceSnapshot(
        ticket_id=TICKET_ID,
        tier="small",
        review_approver_kind=None,
        review_approved_sha=None,
        clean_mode=True,
        ledger_path=ledger_path,
        default_branch="main",
    )
    return base._replace(**fields)


# --- real-git proof ----------------------------------------------------------


class _Series:
    """A feature branch of two commits, rebased onto an advanced main."""

    def __init__(self, make_git_repo: Callable[..., Path]) -> None:
        self.path = make_git_repo("remap-repo")
        commit_tracked_file(self.path, "base.py")
        self.base = self.head()
        git_in(self.path, "checkout", "-b", "feature")
        commit_tracked_file(self.path, "one.py")
        commit_tracked_file(self.path, "two.py")
        self.expected = self.head()
        git_in(self.path, "checkout", "main")
        commit_tracked_file(self.path, "main_only.py")
        self.main = self.head()
        git_in(self.path, "checkout", "feature")
        git_in(self.path, "rebase", "main")
        self.rebased = self.head()

    def head(self) -> str:
        return git_in(self.path, "rev-parse", "HEAD")

    def changelog_commit(self) -> str:
        (self.path / "CHANGELOG.md").write_text(CHANGELOG_BYTES, encoding="utf-8")
        git_in(self.path, "add", "CHANGELOG.md")
        git_in(self.path, "commit", "-m", "chore(changelog): rebuild")
        return self.head()

    def prove(self, rebased: str, new: str) -> int:
        return repair_git._prove_series_unchanged(
            self.path, self.base, self.expected, "main", rebased, new
        )


@pytest.fixture
def series(make_git_repo: Callable[..., Path]) -> _Series:
    return _Series(make_git_repo)


def test_equal_series_plus_changelog_commit_is_proven(series: _Series) -> None:
    new = series.changelog_commit()

    assert series.prove(series.rebased, new) == 2


def test_no_changelog_commit_is_proven_with_head_equal_to_rebased(
    series: _Series,
) -> None:
    assert series.prove(series.rebased, series.rebased) == 2


def test_changed_branch_commit_is_not_proven(series: _Series) -> None:
    git_in(series.path, "checkout", "-b", "tampered", series.rebased)
    (series.path / "two.py").write_text("changed\n", encoding="utf-8")
    git_in(series.path, "commit", "-am", "tamper", "--amend")
    tampered = series.head()

    with pytest.raises(repair_git.RepairStopError) as caught:
        series.prove(tampered, tampered)

    assert caught.value.disposition == RepairDisposition.VERIFY_FAILED
    assert "changed across the rebase" in caught.value.detail


def test_non_changelog_commit_on_top_is_not_proven(series: _Series) -> None:
    (series.path / "extra.py").write_text("x\n", encoding="utf-8")
    git_in(series.path, "add", "extra.py")
    git_in(series.path, "commit", "-m", "stray")

    with pytest.raises(repair_git.RepairStopError) as caught:
        series.prove(series.rebased, series.head())

    assert "CHANGELOG rebuild also touched" in caught.value.detail


def test_head_that_is_not_one_commit_on_the_rebased_series_is_not_proven(
    series: _Series,
) -> None:
    with pytest.raises(repair_git.RepairStopError) as caught:
        series.prove(series.rebased, series.main)

    assert "not one commit on the rebased series" in caught.value.detail


# --- parser and predicates (stubbed _git) ------------------------------------


def _stub_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    range_diff: subprocess.CompletedProcess[str] | None = None,
    parent: str = "rebased",
    diff: subprocess.CompletedProcess[str] | None = None,
) -> None:
    """Route ``_git`` calls by subcommand; unset answers mean success."""

    def fake(cwd: Path, *argv: str, **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "range-diff":
            return range_diff or _done("1:  a = 1:  a subj\n")
        if argv[0] == "rev-parse":
            return _done(f"{parent}\n")
        if argv[0] == "diff":
            return diff or _done("")
        unexpected = f"unexpected git call {argv}"
        raise AssertionError(unexpected)

    monkeypatch.setattr(repair_git, "_git", fake)


def _done(out: str, rc: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_range_diff_marks_parse_every_pair_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = (
        "1:  2c9641c = 1:  2c9641c one\n"
        "-:  ------- > 2:  aea5e62 two\n"
        "3:  5fa53bd < -:  ------- three\n"
        "4:  abc1234 ! 4:  def5678 four\n"
    )
    _stub_git(monkeypatch, range_diff=_done(out))

    marks = repair_git._range_diff_marks(Path(), "a..b", "c..d")

    assert marks == ["=", ">", "<", "!"]


@pytest.mark.parametrize(
    ("range_diff", "reason"),
    [
        (_done("", rc=128, err="fatal"), "git range-diff exit 128"),
        (_done("\n   \n"), "printed no commit pairs"),
        (_done("garbage line\n"), "unparseable range-diff line"),
    ],
)
def test_unreadable_range_diff_is_unprovable(
    monkeypatch: pytest.MonkeyPatch,
    range_diff: subprocess.CompletedProcess[str],
    reason: str,
) -> None:
    _stub_git(monkeypatch, range_diff=range_diff)

    with pytest.raises(repair_git.RepairStopError) as caught:
        repair_git._range_diff_marks(Path(), "a..b", "c..d")

    assert caught.value.disposition == RepairDisposition.VERIFY_FAILED
    assert reason in caught.value.detail


def test_all_pairs_equal_requires_every_mark_to_be_equal() -> None:
    assert repair_git._all_pairs_equal(["=", "="]) is True
    assert repair_git._all_pairs_equal(["=", "!"]) is False
    assert repair_git._all_pairs_equal(["<"]) is False


def test_parent_mismatch_is_unprovable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(monkeypatch, parent="someone-else")

    with pytest.raises(repair_git.RepairStopError, match="not one commit"):
        repair_git._changelog_only_delta(Path(), "rebased", "new")


def test_failed_parent_read_is_unprovable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(monkeypatch)

    def failing(cwd: Path, *argv: str, **_: Any) -> subprocess.CompletedProcess[str]:
        return _done("", rc=1, err="bad rev")

    monkeypatch.setattr(repair_git, "_git", failing)

    with pytest.raises(repair_git.RepairStopError, match="not one commit"):
        repair_git._changelog_only_delta(Path(), "rebased", "new")


def test_failed_changed_paths_read_is_unprovable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_git(monkeypatch, diff=_done("", rc=2, err="boom"))

    with pytest.raises(repair_git.RepairStopError, match="git diff --name-only"):
        repair_git._changelog_only_delta(Path(), "rebased", "new")


# --- persist_remap -----------------------------------------------------------


def test_persist_writes_the_remap_when_the_pre_image_matches(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, before)
    after = before.model_copy(update={"last_clean_sha": NEW})

    assert repair_ledger.persist_remap(path, before, after) is None

    assert load_ledger(path) == after


def test_persist_refuses_a_ledger_changed_since_the_fence(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, confirmed_ledger(sha="someone-else"))

    reason = repair_ledger.persist_remap(
        path, before, before.model_copy(update={"last_clean_sha": NEW})
    )

    assert reason is not None
    assert "changed since the fence read it" in reason


def test_persist_refuses_a_vanished_ledger(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)

    reason = repair_ledger.persist_remap(
        tmp_path / LEDGER_RELATIVE_PATH, before, before
    )

    assert reason is not None
    assert "vanished" in reason


def test_persist_refuses_an_unreadable_ledger(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    reason = repair_ledger.persist_remap(
        path, confirmed_ledger(sha=OLD), confirmed_ledger(sha=NEW)
    )

    assert reason is not None
    assert "unreadable before the remap write" in reason


def test_persist_reports_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, before)

    def boom(_path: Path, _ledger: ConvergenceLedger) -> None:
        disk_full = "disk full"
        raise OSError(disk_full)

    monkeypatch.setattr(repair_ledger, "save_ledger", boom)

    reason = repair_ledger.persist_remap(
        path, before, before.model_copy(update={"last_clean_sha": NEW})
    )

    assert reason is not None
    assert "write failed (OSError: disk full)" in reason


# --- fence (in memory) -------------------------------------------------------


def test_fence_applies_the_remap_in_memory_only(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, before)

    verdict = evaluate_repair_fence(
        _snapshot(path),
        repo=tmp_path,
        new_sha=NEW,
        origin_tip=NEW,
        remap=_remap(),
    )

    assert verdict.passed is True
    assert verdict.remapped is not None
    assert verdict.remapped.last_clean_sha == NEW
    assert verdict.pre_image == before
    assert load_ledger(path) == before


def test_fence_refuses_a_remap_whose_source_is_not_the_clean_sha(
    tmp_path: Path,
) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha="tolerated-past"))

    verdict = evaluate_repair_fence(
        _snapshot(path),
        repo=tmp_path,
        new_sha=NEW,
        origin_tip=NEW,
        remap=_remap(),
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "commits_after_clean_round"
    assert verdict.remapped is None
    assert "ledger remap refused" in verdict.detail


def test_fence_ignores_the_remap_outside_clean_mode(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=OLD))

    verdict = evaluate_repair_fence(
        _snapshot(path, clean_mode=False),
        repo=tmp_path,
        new_sha=NEW,
        origin_tip=NEW,
        remap=_remap(),
    )

    assert verdict.passed is True
    assert verdict.remapped is None


def test_failed_approval_does_not_carry_the_remap(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=OLD))

    verdict = evaluate_repair_fence(
        _snapshot(path, tier="large", review_approver_kind=None),
        repo=tmp_path,
        new_sha=NEW,
        origin_tip=NEW,
        remap=_remap(),
    )

    assert verdict.passed is False
    assert verdict.remapped is None
    assert verdict.pre_image is None


def test_remap_that_still_fails_the_tip_is_not_carried(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=OLD))

    verdict = evaluate_repair_fence(
        _snapshot(path),
        repo=tmp_path,
        new_sha="other-head",
        origin_tip="other-head",
        remap=_remap(),
    )

    assert verdict.passed is False
    assert verdict.remapped is None


# --- engine: apply the persist -----------------------------------------------


def _job_and_state(ledger_path: Path, remap: CleanShaRemap) -> tuple[Any, Any, Any]:
    job = SimpleNamespace(
        client="client",
        ticket_id=TICKET_ID,
        fence=SimpleNamespace(ledger_path=ledger_path),
    )
    prep = SimpleNamespace(pr_number=7)
    state = SimpleNamespace(clean_remap=remap)
    return job, prep, state


def test_apply_remap_persists_and_keeps_the_passing_verdict(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, before)
    remap = _remap()
    remapped = before.model_copy(update={"last_clean_sha": NEW})
    verdict = FenceVerdict(True, "not_required", None, "ok", remapped, before)
    job, prep, state = _job_and_state(path, remap)

    result = engine._apply_remap(job, prep, verdict, state)

    assert result.passed is True
    assert load_ledger(path) == remapped


def test_apply_remap_fails_the_fence_when_the_write_is_refused(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=OLD)
    path = write_ledger(tmp_path, confirmed_ledger(sha="moved-on"))
    remap = _remap()
    verdict = FenceVerdict(
        True,
        "not_required",
        None,
        "ok",
        before.model_copy(update={"last_clean_sha": NEW}),
        before,
    )
    job, prep, state = _job_and_state(path, remap)

    result = engine._apply_remap(job, prep, verdict, state)

    assert result.passed is False
    assert result.tip_reason == "commits_after_clean_round"
    assert "ledger remap not written" in result.detail


def test_apply_remap_passes_a_verdict_without_a_remap_through(tmp_path: Path) -> None:
    verdict = FenceVerdict(True, "not_required", None, "ok")
    job, prep, state = _job_and_state(tmp_path / LEDGER_RELATIVE_PATH, _remap())

    assert engine._apply_remap(job, prep, verdict, state) is verdict


# --- changelog_repair: eligibility and proof wrapper -------------------------


@pytest.mark.parametrize(
    ("clean", "has_ledger", "tolerance", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_remap_eligibility_honors_mode_ledger_and_strict_opt_out(
    clean: bool, has_ledger: bool, tolerance: bool, expected: bool
) -> None:
    fence = SimpleNamespace(
        clean_mode=clean,
        ledger_path=Path("ledger") if has_ledger else None,
        default_merge_tolerance=tolerance,
    )

    assert changelog_repair._remap_eligible(SimpleNamespace(fence=fence)) is expected


def _prep(series: _Series) -> Any:
    return SimpleNamespace(
        merge_base=series.base,
        expected_sha=series.expected,
        default_ref="main",
        pr_number=7,
    )


def _job() -> Any:
    return SimpleNamespace(
        client="client",
        ticket_id=TICKET_ID,
        spec=SimpleNamespace(recipe="repair_changelog_conflict"),
    )


def test_prove_remap_returns_the_audit_entry(series: _Series) -> None:
    new = series.changelog_commit()

    with freeze_time(AT):
        remap = changelog_repair._prove_remap(
            _job(), _prep(series), series.path, series.rebased, new
        )

    assert remap is not None
    assert remap.from_sha == series.expected
    assert remap.rebased_sha == series.rebased
    assert remap.to_sha == new
    assert remap.commits == 2
    assert remap.at == AT


def test_prove_remap_logs_and_returns_none_when_unprovable(
    series: _Series, caplog: pytest.LogCaptureFixture
) -> None:
    (series.path / "extra.py").write_text("x\n", encoding="utf-8")
    git_in(series.path, "add", "extra.py")
    git_in(series.path, "commit", "-m", "stray")

    with caplog.at_level("INFO"):
        remap = changelog_repair._prove_remap(
            _job(), _prep(series), series.path, series.rebased, series.head()
        )

    assert remap is None
    assert "changelog_repair_ledger_remap_skipped" in caplog.text
