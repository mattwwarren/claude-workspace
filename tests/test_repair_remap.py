"""Tests for the clean-sha carry of the repair recipe (#2921, #3375).

Real temp git repos for the tree proof (merge-tree equality plus the confined
CHANGELOG delta); a stubbed ``_git`` for the CHANGELOG-only delta's failure
branches. The fence and the compare-and-set persist are exercised against real
ledger files. Heading-bearing CHANGELOG texts come from
``tests._changelog_cut_helpers`` so each has one source.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time

from cw.reconcile.review_recipes import (
    changelog_repair,
    repair_git,
    repair_ledger,
    repair_proof,
)
from cw.reconcile.review_recipes import repair_engine as engine
from cw.reconcile.review_recipes.repair_fence import (
    FenceSnapshot,
    FenceVerdict,
    evaluate_repair_fence,
)
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    CleanShaRemap,
    ConvergenceLedger,
    load_ledger,
)
from tests._changelog_cut_helpers import (
    CL_BASE,
    CL_BRANCH,
    CL_MAIN_SIBLING,
    CL_NEW_MAIN_LINE_DELETED,
    CL_NEW_PREAMBLE_ADDITION,
    CL_NEW_RELEASED_SECTION_CHANGED,
    CL_REBUILT_SIBLING,
    commit_files,
)
from tests._review_convergence_helpers import TICKET_ID, confirmed_ledger, write_ledger
from tests._shared.git_repos import git_in

OLD = "old-head-sha"
NEW = "new-head-sha"
AT = "2026-11-07T12:00:00Z"
UNION_ATTRIBUTE = "CHANGELOG.md merge=union\n"
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
        base_branch="main",
    )
    return base._replace(**fields)


# --- real-git proof ----------------------------------------------------------


class _Series:
    """A feature branch of two commits, rebased onto an advanced main.

    The first branch commit touches ``CHANGELOG.md`` (adds Gamma); main adds
    ``main_changelog`` and a code file. ``CHANGELOG.md merge=union`` lets the
    rebase finish, whatever it writes: the proof only reads the head the test
    commits on top via :meth:`changelog_commit`.
    """

    def __init__(
        self,
        make_git_repo: Callable[..., Path],
        *,
        main_changelog: str = CL_MAIN_SIBLING,
        branch_changelog: str | None = CL_BRANCH,
    ) -> None:
        self.path = make_git_repo("remap-repo")
        self.base = commit_files(
            self.path,
            {
                "base.py": "x = 1\n",
                "CHANGELOG.md": CL_BASE,
                ".gitattributes": UNION_ATTRIBUTE,
            },
            "base",
        )
        git_in(self.path, "checkout", "-b", "feature")
        files = {"one.py": "x = 1\n"}
        if branch_changelog is not None:
            files["CHANGELOG.md"] = branch_changelog
        self.first = commit_files(self.path, files, "add one")
        commit_files(self.path, {"two.py": "x = 1\n"}, "add two")
        self.expected = self.head()
        git_in(self.path, "checkout", "main")
        self.main = commit_files(
            self.path,
            {"main_only.py": "x = 1\n", "CHANGELOG.md": main_changelog},
            "main moves",
        )
        git_in(self.path, "checkout", "feature")
        git_in(self.path, "rebase", "main")
        self.rebased = self.head()

    def head(self) -> str:
        return git_in(self.path, "rev-parse", "HEAD")

    def changelog_commit(self, text: str = CL_REBUILT_SIBLING) -> str:
        """Commit *text* as CHANGELOG.md (an empty commit when unchanged)."""
        (self.path / "CHANGELOG.md").write_text(text, encoding="utf-8")
        git_in(self.path, "add", "CHANGELOG.md")
        git_in(self.path, "commit", "--allow-empty", "-m", "chore(changelog): rebuild")
        return self.head()

    def prove(self, rebased: str, new: str) -> int:
        return repair_proof.prove_tree_unchanged(
            self.path, self.expected, self.main, rebased, new, self.base
        )

    def refusal(self, rebased: str, new: str) -> str:
        with pytest.raises(repair_git.RepairStopError) as caught:
            self.prove(rebased, new)
        return caught.value.detail


@pytest.fixture
def series(make_git_repo: Callable[..., Path]) -> _Series:
    return _Series(make_git_repo)


def test_changelog_touching_series_is_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    quiet = _Series(make_git_repo, main_changelog=CL_BASE)

    assert quiet.prove(quiet.rebased, quiet.changelog_commit(CL_BRANCH)) == 2


def test_raced_pair_changelog_is_proven(series: _Series) -> None:
    """Real ``-U0`` output for a sibling bullet landed at the same spot."""
    assert series.prove(series.rebased, series.changelog_commit()) == 2


def test_unchanged_series_without_changelog_commit_is_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    plain = _Series(make_git_repo, branch_changelog=None)

    assert plain.prove(plain.rebased, plain.rebased) == 2


def test_squashed_series_with_the_same_tree_is_proven(series: _Series) -> None:
    """Documents what is lost: commit shape. The net tree is what was reviewed."""
    series.changelog_commit()
    git_in(series.path, "reset", "--soft", series.main)
    git_in(series.path, "commit", "-m", "squashed")
    squashed = series.head()

    assert series.prove(squashed, squashed) == 1


def test_rewritten_non_changelog_hunk_is_not_proven(series: _Series) -> None:
    commit_files(series.path, {"two.py": "changed\n"}, "tamper")
    tampered = series.head()

    detail = series.refusal(tampered, series.changelog_commit())

    assert "two.py" in detail


def test_dropped_branch_commit_is_not_proven(series: _Series) -> None:
    git_in(series.path, "checkout", "-b", "dropped", series.main)
    git_in(series.path, "cherry-pick", series.first)
    dropped = series.head()

    detail = series.refusal(dropped, series.changelog_commit())

    assert "two.py" in detail


def test_added_non_changelog_file_is_not_proven(series: _Series) -> None:
    commit_files(series.path, {"extra.py": "x\n"}, "stray")

    detail = series.refusal(series.rebased, series.head())

    assert "extra.py" in detail


def test_non_changelog_file_in_the_changelog_commit_is_not_proven(
    series: _Series,
) -> None:
    commit_files(series.path, {"two.py": "changed\n"}, "tamper")
    tampered = series.head()
    (series.path / "two.py").write_text("x = 1\n", encoding="utf-8")
    git_in(series.path, "add", "two.py")

    new = series.changelog_commit()

    assert "CHANGELOG rebuild also touched" in series.refusal(tampered, new)


def test_conflicting_non_changelog_merge_is_not_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("conflict-repo")
    base = commit_files(repo, {"one.py": "a\n", "CHANGELOG.md": CL_BASE}, "base")
    git_in(repo, "checkout", "-b", "feature")
    expected = commit_files(repo, {"one.py": "b\n"}, "feature edit")
    git_in(repo, "checkout", "main")
    main = commit_files(repo, {"one.py": "c\n"}, "main edit")

    with pytest.raises(repair_git.RepairStopError) as caught:
        repair_proof.prove_tree_unchanged(
            repo, expected, main, expected, expected, base
        )

    assert "conflicts on" in caught.value.detail
    assert "one.py" in caught.value.detail


def test_zero_surviving_branch_commits_is_not_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("redundant-repo")
    base = commit_files(repo, {"base.py": "x\n", "CHANGELOG.md": CL_BASE}, "base")
    git_in(repo, "checkout", "-b", "feature")
    expected = commit_files(repo, {"one.py": "x\n"}, "add one")
    git_in(repo, "checkout", "main")
    main = commit_files(repo, {"one.py": "x\n"}, "main adds the same file")

    with pytest.raises(repair_git.RepairStopError) as caught:
        repair_proof.prove_tree_unchanged(repo, expected, main, main, main, base)

    assert "no branch commit survives" in caught.value.detail


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        pytest.param(
            CL_NEW_RELEASED_SECTION_CHANGED, "released section", id="released"
        ),
        pytest.param(CL_NEW_MAIN_LINE_DELETED, "deleted or modified", id="deleted"),
        pytest.param(CL_NEW_PREAMBLE_ADDITION, "outside", id="preamble"),
        pytest.param(CHANGELOG_BYTES, "released section", id="gutted"),
    ],
)
def test_changelog_hunks_outside_the_confinement_are_not_proven(
    series: _Series, text: str, detail: str
) -> None:
    assert detail in series.refusal(series.rebased, series.changelog_commit(text))


# --- the CHANGELOG-only delta (stubbed _git) ---------------------------------


def _stub_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent: str = "rebased",
    diff: subprocess.CompletedProcess[str] | None = None,
) -> None:
    """Route ``_git`` calls by subcommand; unset answers mean success."""

    def fake(cwd: Path, *argv: str, **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "rev-parse":
            return _done(f"{parent}\n")
        if argv[0] == "diff":
            return diff or _done("")
        unexpected = f"unexpected git call {argv}"
        raise AssertionError(unexpected)

    monkeypatch.setattr(repair_git, "_git", fake)


def _done(out: str, rc: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


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
            _job(), _prep(series), series.path, series.rebased, new, series.main
        )

    assert isinstance(remap, CleanShaRemap)
    assert remap.from_sha == series.expected
    assert remap.rebased_sha == series.rebased
    assert remap.to_sha == new
    assert remap.commits == 2
    assert remap.at == AT


def test_prove_remap_logs_and_returns_the_reason_when_unprovable(
    series: _Series, caplog: pytest.LogCaptureFixture
) -> None:
    commit_files(series.path, {"extra.py": "x\n"}, "stray")

    with caplog.at_level("INFO"):
        remap = changelog_repair._prove_remap(
            _job(),
            _prep(series),
            series.path,
            series.rebased,
            series.head(),
            series.main,
        )

    assert isinstance(remap, str)
    assert "extra.py" in remap
    assert "changelog_repair_ledger_remap_skipped" in caplog.text
