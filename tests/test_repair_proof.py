"""Hermetic tests for the clean-sha tree proof (#2921, #3375).

``ScriptedGit`` stands in for ``run_git`` and freezegun for the clock, so no real
git runs. ``tests/test_repair_remap.py`` covers the same proof against real
repos; here every refusal is scripted from the shared CHANGELOG fixture
constants (no heading literals live in this file), and a kill-check pair proves
the stray-path predicate is what stops a changed non-CHANGELOG file.

No test asserts a refusal's disposition: ``_prove_remap`` catches any
``RepairStopError`` and only logs and surfaces its detail.
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
    repair_proof,
)
from cw.review_convergence import CleanShaRemap
from tests._changelog_cut_helpers import (
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
    PREAMBLE,
    TICKET,
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

FROZEN = "2026-11-07T12:00:00Z"
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
