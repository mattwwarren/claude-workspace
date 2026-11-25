"""Tests for the clean-sha tree proof (#2921, #3375).

Two halves. The real-git proof runs against temp repos built by ``git_in``; the
hermetic half stubs ``run_git`` with ``ScriptedGit`` and freezegun for the
clock, so no real git runs there. Refusals are scripted from the shared
CHANGELOG fixture constants (no heading literals live in this file), and a
kill-check pair proves the stray-path predicate is what stops a changed
non-CHANGELOG file.

No test asserts a refusal's disposition: ``_prove_remap`` catches any
``RepairStopError`` and only logs and surfaces its detail.
"""

from __future__ import annotations

import logging
import subprocess
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
    repair_proof,
)
from cw.review_convergence import CleanShaRemap
from tests._changelog_cut_helpers import (
    CL_BASE,
    CL_BASE_CRLF,
    CL_BRANCH,
    CL_BRANCH_BARE_CR,
    CL_BRANCH_CRLF,
    CL_EXPECTED,
    CL_EXPECTED_CRLF,
    CL_MAIN,
    CL_MAIN_CRLF,
    CL_MAIN_SIBLING,
    CL_NEW_BEFORE_UNRELEASED,
    CL_NEW_BLANK_OUTSIDE_SPAN,
    CL_NEW_FOREIGN_HEADING,
    CL_NEW_MAIN_LINE_DELETED,
    CL_NEW_OUT_OF_SPAN,
    CL_NEW_PREAMBLE_ADDITION,
    CL_NEW_RELEASED_HEADING_AT_TAIL,
    CL_NEW_RELEASED_SECTION_CHANGED,
    CL_NEW_UNATTESTED_BULLET,
    CL_REBUILT_BARE_CR,
    CL_REBUILT_SIBLING,
    CL_UNRELEASED_ONLY,
    PREAMBLE,
    TICKET,
    commit_files,
)
from tests._changelog_repair_helpers import (
    DEFAULT_SHA,
    EXPECTED_SHA,
    MB_SHA,
    NEW_SHA,
    OSERROR,
    PR_NUMBER,
    REBASED_SHA,
    TIMEOUT,
    TREE_SHA,
    GitResult,
    LogEntry,
    ScriptedGit,
    proof_tree_calls,
    stub_remap_proof,
    unified0,
)
from tests._shared.git_repos import git_in

pytestmark = pytest.mark.release_cut

FROZEN = "2026-11-07T12:00:00Z"
UNION_ATTRIBUTE = "CHANGELOG.md merge=union\n"
SCRATCH = Path("scratch")
SKIPPED = "changelog_repair_ledger_remap_skipped"
STRAY_PATH = "src/stray.py\0"
CHANGED_PATHS_ARGV = ("diff", "--name-only", "-z", REBASED_SHA, NEW_SHA)
PROOF_MERGE_TREE = ("merge-tree", "--write-tree", "--no-messages")
TREE_DIFF = ("diff", "--no-renames", "--name-only", "-z")
DEFAULT_TO_NEW = ("diff", "-U0", "--no-renames", DEFAULT_SHA, NEW_SHA)
MB_TO_EXPECTED = ("diff", "-U0", "--no-renames", MB_SHA, EXPECTED_SHA)

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
        pr_number=PR_NUMBER,
    )


def _prove(rebased: str = REBASED_SHA, new: str = NEW_SHA) -> CleanShaRemap | str:
    return changelog_repair._prove_remap(
        _job(), _prep(), SCRATCH, rebased, new, DEFAULT_SHA
    )


def _verbs(git: ScriptedGit) -> list[str]:
    return [argv[0] for argv in git.stripped()]


def _refusal(result: CleanShaRemap | str) -> str:
    assert isinstance(result, str)
    return result


# --- the proof holds -----------------------------------------------------------


def test_unchanged_tree_is_proven(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    with freeze_time(FROZEN):
        remap = _prove()

    assert remap == CleanShaRemap(
        at=FROZEN,
        recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
        from_sha=EXPECTED_SHA,
        rebased_sha=REBASED_SHA,
        to_sha=NEW_SHA,
        commits=1,
    )


def test_audit_stamp_is_the_clock_at_proof_time(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    with freeze_time("2031-01-02T03:04:05Z"):
        remap = _prove()
        expected = iso_z(utc_now_seconds())

    assert isinstance(remap, CleanShaRemap)
    assert remap.at == expected == "2031-01-02T03:04:05Z"


def test_commits_carried_is_the_rebased_series_length(git: ScriptedGit) -> None:
    stub_remap_proof(git)
    git.set(
        ("rev-list", "--count", f"{DEFAULT_SHA}..{REBASED_SHA}"),
        GitResult(stdout="3\n"),
    )

    remap = _prove()

    assert isinstance(remap, CleanShaRemap)
    assert remap.commits == 3


def test_no_changelog_commit_is_allowed_and_checks_no_delta(git: ScriptedGit) -> None:
    stub_remap_proof(git, new_text=CL_REBUILT_SIBLING)
    git.set(
        ("show", f"{REBASED_SHA}:CHANGELOG.md"), GitResult(stdout=CL_REBUILT_SIBLING)
    )
    git.set(
        ("diff", "-U0", "--no-renames", DEFAULT_SHA, REBASED_SHA),
        GitResult(stdout=unified0(CL_MAIN_SIBLING, CL_REBUILT_SIBLING)),
    )

    result = _prove(rebased=REBASED_SHA, new=REBASED_SHA)

    assert isinstance(result, CleanShaRemap)
    assert result.rebased_sha == result.to_sha == REBASED_SHA
    assert "rev-parse" not in _verbs(git)


def test_changelog_only_conflict_without_the_union_driver_is_allowed(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git)
    git.set(PROOF_MERGE_TREE, GitResult(1, f"{TREE_SHA}\0CHANGELOG.md\0"))

    assert isinstance(_prove(), CleanShaRemap)


def test_raced_pair_rebuild_is_provable(git: ScriptedGit) -> None:
    """A sibling's bullet is already in main; the rebuild filed ours after it."""
    stub_remap_proof(
        git,
        default_text=CL_MAIN_SIBLING,
        attested_text=CL_BRANCH,
        new_text=CL_REBUILT_SIBLING,
    )

    assert isinstance(_prove(), CleanShaRemap)


def test_recreated_subsection_heading_is_provable(git: ScriptedGit) -> None:
    """A release cut emptied main's ``[Unreleased]``; ``### Fixed`` is re-created."""
    stub_remap_proof(
        git, default_text=CL_MAIN, attested_text=CL_BRANCH, new_text=CL_EXPECTED
    )

    assert isinstance(_prove(), CleanShaRemap)


def test_crlf_release_cut_rebuild_is_provable(git: ScriptedGit) -> None:
    """Lines compare verbatim, ``\\r`` included: the CR-bearing heading is carried."""
    stub_remap_proof(
        git,
        default_text=CL_MAIN_CRLF,
        attested_text=CL_BRANCH_CRLF,
        new_text=CL_EXPECTED_CRLF,
    )
    git.set(MB_TO_EXPECTED, GitResult(stdout=unified0(CL_BASE_CRLF, CL_BRANCH_CRLF)))

    assert isinstance(_prove(), CleanShaRemap)


def test_bare_cr_line_the_attested_head_added_is_provable(git: ScriptedGit) -> None:
    stub_remap_proof(git, attested_text=CL_BRANCH_BARE_CR, new_text=CL_REBUILT_BARE_CR)

    assert isinstance(_prove(), CleanShaRemap)


def test_bare_cr_line_the_attested_head_never_added_is_refused(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_REBUILT_BARE_CR)

    detail = _refusal(_prove())

    assert "Extra fix (#9)" in detail
    assert "never added by the reviewed head" in detail


# --- every refusal fails closed ---------------------------------------------------


def _stub(**kwargs: Any) -> Configure:
    def configure(git: ScriptedGit) -> None:
        stub_remap_proof(git, **kwargs)

    return configure


def _answer(prefix: tuple[str, ...], result: Any) -> Configure:
    def configure(git: ScriptedGit) -> None:
        stub_remap_proof(git)
        git.set(prefix, result)

    return configure


REFUSALS = [
    pytest.param(_answer(PROOF_MERGE_TREE, TIMEOUT), "unavailable", id="tree-timeout"),
    pytest.param(_answer(PROOF_MERGE_TREE, OSERROR), "unavailable", id="tree-oserror"),
    pytest.param(
        _answer(PROOF_MERGE_TREE, GitResult(128, "", "fatal")),
        "unavailable",
        id="tree-nonzero-exit",
    ),
    pytest.param(
        _answer(PROOF_MERGE_TREE, GitResult(0, "not-a-tree\0")),
        "unavailable",
        id="tree-unreadable",
    ),
    pytest.param(
        _answer(PROOF_MERGE_TREE, GitResult(1, f"{TREE_SHA}\0one.py\0CHANGELOG.md\0")),
        "one.py",
        id="non-changelog-conflict",
    ),
    pytest.param(_stub(tree_diff=STRAY_PATH), "src/stray.py", id="stray-tree-path"),
    pytest.param(
        _stub(tree_diff=f"CHANGELOG.md\0{STRAY_PATH}"),
        "src/stray.py",
        id="stray-with-changelog",
    ),
    pytest.param(
        _answer(TREE_DIFF, GitResult(2, "", "boom")),
        "git diff --name-only",
        id="tree-diff-fails",
    ),
    pytest.param(
        _stub(new_text=CL_NEW_MAIN_LINE_DELETED),
        "deleted or modified",
        id="line-deleted",
    ),
    pytest.param(
        _stub(new_text=CL_NEW_RELEASED_SECTION_CHANGED),
        "released section",
        id="released-section-changed",
    ),
    pytest.param(
        _answer(DEFAULT_TO_NEW, GitResult(stdout="Binary files differ\n")),
        "unreadable CHANGELOG diff",
        id="unreadable-diff",
    ),
    pytest.param(
        _answer(DEFAULT_TO_NEW, GitResult(2, "", "bad")),
        "git diff -U0",
        id="diff-fails",
    ),
    pytest.param(
        _answer(DEFAULT_TO_NEW, TIMEOUT), "timed out after", id="diff-timeout"
    ),
    pytest.param(_answer(DEFAULT_TO_NEW, OSERROR), "could not run", id="diff-oserror"),
    pytest.param(
        _answer(DEFAULT_TO_NEW, GitResult(stdout=b"+\xff\n")),
        "not UTF-8",
        id="diff-not-utf8",
    ),
    pytest.param(_stub(parent="f" * 40), "not one commit", id="parent-mismatch"),
    pytest.param(
        _answer(("rev-list", "--count"), GitResult(stdout="0\n")),
        "no branch commit survives",
        id="zero-commits",
    ),
    pytest.param(
        _stub(changed=f"CHANGELOG.md\0{STRAY_PATH}"),
        "CHANGELOG rebuild also touched",
        id="rebuild-commit-touches-code",
    ),
    pytest.param(
        _stub(default_text=PREAMBLE),
        "[Unreleased] headings",
        id="default-without-unreleased",
    ),
]


@pytest.mark.parametrize(("configure", "detail"), REFUSALS)
def test_refusal_returns_the_detail_and_logs_one_skip(
    git: ScriptedGit,
    configure: Configure,
    detail: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure(git)

    with caplog.at_level(logging.INFO):
        result = _prove()

    assert detail in _refusal(result)
    skips = [r.getMessage() for r in caplog.records if SKIPPED in r.getMessage()]
    assert len(skips) == 1
    assert f"ticket={TICKET}" in skips[0]
    assert f"new_sha={NEW_SHA}" in skips[0]


# --- CHANGELOG confinement ------------------------------------------------------------


def test_brand_new_released_heading_on_the_merged_side_is_not_provable(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_RELEASED_HEADING_AT_TAIL)

    detail = _refusal(_prove())

    assert "9.9.9" in detail
    assert "never added by the reviewed head" in detail


def test_changelog_preamble_addition_is_not_provable(git: ScriptedGit) -> None:
    stub_remap_proof(git, new_text=CL_NEW_PREAMBLE_ADDITION)

    assert "outside the default branch's [Unreleased] span" in _refusal(_prove())


def test_changelog_line_directly_above_the_unreleased_heading_is_not_provable(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_BEFORE_UNRELEASED)

    assert "outside the default branch's [Unreleased] span" in _refusal(_prove())


def test_changelog_line_absent_from_the_attested_diff_is_not_provable(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_UNATTESTED_BULLET)

    assert "Foreign fix" in _refusal(_prove())


def test_changelog_line_added_after_the_unreleased_span_is_not_provable(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_OUT_OF_SPAN)

    assert not isinstance(_prove(), CleanShaRemap)


def test_foreign_subsection_heading_is_not_provable(git: ScriptedGit) -> None:
    stub_remap_proof(git, new_text=CL_NEW_FOREIGN_HEADING)

    assert "### Removed" in _refusal(_prove())


def test_blank_added_lines_are_exempt_from_provenance_but_not_position(
    git: ScriptedGit,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_BLANK_OUTSIDE_SPAN)

    assert "outside the default branch's [Unreleased] span" in _refusal(_prove())


def test_attested_diff_is_read_from_the_merge_base(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    assert isinstance(_prove(), CleanShaRemap)

    prefixes = [argv[:5] for argv in git.stripped()]
    assert MB_TO_EXPECTED in prefixes
    assert DEFAULT_TO_NEW in prefixes


# --- kill-check pair: the stray-path predicate is load-bearing ------------------


def test_stray_path_refuses_the_carry(git: ScriptedGit) -> None:
    stub_remap_proof(git, tree_diff=STRAY_PATH)

    assert not isinstance(_prove(), CleanShaRemap)


def test_stray_path_flips_the_result(
    git: ScriptedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the predicate emptied the same stray-path series IS proven.

    Paired with ``test_stray_path_refuses_the_carry``: together they show
    ``stray_paths`` alone is what stops a changed non-CHANGELOG file.
    """
    stub_remap_proof(git, tree_diff=STRAY_PATH)
    monkeypatch.setattr(repair_proof, "stray_paths", lambda *_args: [])

    assert isinstance(_prove(), CleanShaRemap)


def test_proof_runs_one_merge_tree_against_the_pinned_default(git: ScriptedGit) -> None:
    stub_remap_proof(git)

    assert isinstance(_prove(), CleanShaRemap)

    (merge_tree,) = proof_tree_calls(git)
    assert merge_tree[-2:] == (DEFAULT_SHA, EXPECTED_SHA)
    assert "merge-tree" in _verbs(git)


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
        base_changelog: str = CL_BASE,
    ) -> None:
        self.path = make_git_repo("remap-repo")
        self.base = commit_files(
            self.path,
            {
                "base.py": "x = 1\n",
                "CHANGELOG.md": base_changelog,
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


def test_crlf_release_cut_rebuild_is_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    """Real git: the re-created ``### Fixed`` keeps its CR, so the exemption fires.

    With ``text=True`` the diff arrived without the CR while the attested blob
    kept it, so the carried-heading exemption never matched and a CRLF changelog
    was refused after every release cut.
    """
    cut = _Series(
        make_git_repo,
        main_changelog=CL_MAIN_CRLF,
        branch_changelog=CL_BRANCH_CRLF,
        base_changelog=CL_BASE_CRLF,
    )

    assert cut.prove(cut.rebased, cut.changelog_commit(CL_EXPECTED_CRLF)) == 2


def test_bare_cr_line_the_reviewed_head_added_is_proven(
    make_git_repo: Callable[..., Path],
) -> None:
    cr = _Series(make_git_repo, branch_changelog=CL_BRANCH_BARE_CR)

    assert cr.prove(cr.rebased, cr.changelog_commit(CL_REBUILT_BARE_CR)) == 2


def test_bare_cr_tail_the_reviewed_head_never_added_is_not_proven(
    series: _Series,
) -> None:
    """The text after a bare CR is part of the line, not a dropped diff fragment."""
    detail = series.refusal(series.rebased, series.changelog_commit(CL_REBUILT_BARE_CR))

    assert "Extra fix (#9)" in detail
    assert "never added by the reviewed head" in detail


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
        pytest.param(CL_NEW_BEFORE_UNRELEASED, "outside", id="above-heading"),
        pytest.param(CL_UNRELEASED_ONLY, "released section", id="gutted"),
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


def _series_prep(series: _Series) -> Any:
    return SimpleNamespace(
        merge_base=series.base,
        expected_sha=series.expected,
        pr_number=7,
    )


def _series_job() -> Any:
    return SimpleNamespace(
        client="client",
        ticket_id=TICKET,
        spec=SimpleNamespace(recipe="repair_changelog_conflict"),
    )


def test_prove_remap_returns_the_audit_entry(series: _Series) -> None:
    new = series.changelog_commit()

    with freeze_time(FROZEN):
        remap = changelog_repair._prove_remap(
            _series_job(),
            _series_prep(series),
            series.path,
            series.rebased,
            new,
            series.main,
        )

    assert isinstance(remap, CleanShaRemap)
    assert remap.from_sha == series.expected
    assert remap.rebased_sha == series.rebased
    assert remap.to_sha == new
    assert remap.commits == 2
    assert remap.at == FROZEN


def test_prove_remap_logs_and_returns_the_reason_when_unprovable(
    series: _Series, caplog: pytest.LogCaptureFixture
) -> None:
    commit_files(series.path, {"extra.py": "x\n"}, "stray")

    with caplog.at_level("INFO"):
        remap = changelog_repair._prove_remap(
            _series_job(),
            _series_prep(series),
            series.path,
            series.rebased,
            series.head(),
            series.main,
        )

    assert isinstance(remap, str)
    assert "extra.py" in remap
    assert "changelog_repair_ledger_remap_skipped" in caplog.text
