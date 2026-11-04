"""Tests for ``cw.review_convergence._delta`` (#2918).

``assess_delta_round`` decides whether a delta confirm round may record a
default-branch merge that overlaps the branch's own files. Every test but one
is hermetic: a default-deny :class:`ScriptedMergeGit` answers git, so an
unscripted call fails the test and ``calls == []`` proves git was never asked.
One real-git test pins that the scripted argvs are the ones real git is asked,
``merge-tree --write-tree`` included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw._git import git_output, run_git
from cw.review_convergence import (
    ConvergenceError,
    MergeTolerance,
    _merge_tolerance,
    assess_delta_round,
    record_delta_round,
    tolerance_for_client,
)
from cw.review_convergence._delta import DELTA_REFUSALS, DeltaSpec
from cw.review_convergence._engine import _clean_decision
from cw.review_convergence._tip import has_independent_confirm
from tests._review_convergence_helpers import (
    BRANCH_FILE,
    MERGE_BASE_SHA,
    NOW,
    OVERLAP_FILE,
    PARENT2_SHA,
    ROSTER,
    ScriptedMergeGit,
    branch_with_overlap_merge,
    delta_ledger,
    delta_merge_script,
    fresh_ledger,
    make_review_client,
    merge_tree_key,
    non_default_parent_script,
    non_merge_commit_script,
    octopus_merge_script,
    record,
    tolerated_merge_script,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger, DeltaAssessment

CLEAN = "c" * 40
HEAD = "d" * 40
DEFAULT = "e" * 40
ON = MergeTolerance("main", frozenset({"CHANGELOG.md"}), delta_confirm=True)
OFF = MergeTolerance("main", frozenset({"CHANGELOG.md"}))


def _assess(
    tmp_path: Path,
    *,
    ledger: ConvergenceLedger | None = None,
    head: str = HEAD,
    tolerance: MergeTolerance | None = ON,
    overlap: tuple[str, ...] | None = None,
) -> DeltaAssessment:
    return assess_delta_round(
        ledger or delta_ledger(clean=CLEAN),
        worktree=tmp_path,
        head_sha=head,
        tolerance=tolerance,
        overlap=overlap,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, script: dict[tuple[str, ...], Any]
) -> ScriptedMergeGit:
    return ScriptedMergeGit(script).install(monkeypatch)


def _merge_tree_called(fake: ScriptedMergeGit) -> bool:
    return any(call[:1] == ["merge-tree"] for call in fake.calls)


def test_the_refusal_vocabulary_is_closed() -> None:
    assert set(DELTA_REFUSALS) == {
        "tolerance_off",
        "delta_disabled",
        "ledger_not_confirmable",
        "not_overlap_merge",
        "not_textual_merge",
    }


class TestOneIndependenceRule:
    """``has_independent_confirm`` is the one rule three consumers share.

    The engine's clean decision, the delta round's recording preconditions and
    the assessment's ledger refusal must agree for every confirm backend and
    independence flag, or ``verify-tip`` would say eligible while
    ``record_delta_round`` refuses (or the reverse).
    """

    @pytest.mark.parametrize("backend", ["codex", "claude", "none"])
    @pytest.mark.parametrize("not_independent", [False, True])
    def test_every_consumer_agrees(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        not_independent: bool,
    ) -> None:
        _install(monkeypatch, delta_merge_script(CLEAN, HEAD, DEFAULT))
        ledger = delta_ledger(clean=CLEAN).model_copy(
            update={
                "confirm_backend": backend,
                "confirm_not_independent": not_independent,
            }
        )
        expected = backend != "none" and not not_independent

        assessed = _assess(tmp_path, ledger=ledger).refusal != "ledger_not_confirmable"
        try:
            record_delta_round(
                ledger,
                backend=backend,
                reviewed_sha=HEAD,
                findings=[],
                roster=ROSTER,
                now=NOW,
            )
            recordable = True
        except ConvergenceError:
            recordable = False
        needs_confirm = _clean_decision(ledger, "loop") == "confirm_required"

        assert has_independent_confirm(ledger) is expected
        assert assessed is expected
        assert recordable is expected
        assert needs_confirm is expected


class TestEligible:
    def test_an_overlap_merge_with_the_flag_on_is_planned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, delta_merge_script(CLEAN, HEAD, DEFAULT))

        assessment = _assess(tmp_path)

        assert assessment.refusal is None
        assert assessment.overlapping_files == (BRANCH_FILE,)
        assert assessment.plan is not None
        assert assessment.plan.spec == DeltaSpec(
            from_sha=CLEAN,
            to_sha=HEAD,
            paths=(BRANCH_FILE,),
            branch_base=MERGE_BASE_SHA,
        )
        assert assessment.plan.merges == (HEAD,)

    def test_a_supplied_overlap_is_reused_not_recomputed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(monkeypatch, delta_merge_script(CLEAN, HEAD, DEFAULT))

        assessment = _assess(tmp_path, overlap=("given.py",))

        assert assessment.overlapping_files == ("given.py",)
        assert assessment.plan is not None
        assert assessment.plan.spec.paths == ("given.py",)
        assert not any(call[:1] == ["diff"] for call in fake.calls)


class TestStrictStaysStrict:
    def test_no_tolerance_is_refused_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill-check: a strict client can never reach the delta path."""
        fake = _install(monkeypatch, {})

        assessment = _assess(tmp_path, tolerance=None)

        assert assessment.refusal == "tolerance_off"
        assert assessment.plan is None
        assert assessment.overlapping_files == ()
        assert fake.calls == []

    def test_the_strict_client_config_reads_as_no_tolerance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(monkeypatch, {})
        client = make_review_client(
            tmp_path, default_merge_tolerance=False, delta_confirm=True
        )

        assessment = _assess(tmp_path, tolerance=tolerance_for_client(client))

        assert assessment.refusal == "tolerance_off"
        assert fake.calls == []

    def test_the_flag_off_is_refused_but_the_overlap_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill-check: removing the ``delta_confirm`` gate fails this test."""
        fake = _install(monkeypatch, delta_merge_script(CLEAN, HEAD, DEFAULT))

        assessment = _assess(tmp_path, tolerance=OFF)

        assert assessment.refusal == "delta_disabled"
        assert assessment.plan is None
        assert assessment.overlapping_files == (BRANCH_FILE,)
        assert not _merge_tree_called(fake)


class TestRefusals:
    @pytest.mark.parametrize(
        ("script", "refusal"),
        [
            pytest.param(octopus_merge_script, "not_textual_merge", id="octopus"),
            pytest.param(
                lambda c, h, d: delta_merge_script(c, h, d, merged_tree="edited"),
                "not_textual_merge",
                id="hand-edited-merge",
            ),
            pytest.param(
                lambda c, h, d: delta_merge_script(c, h, d, merge_tree_rc=1),
                "not_textual_merge",
                id="conflict",
            ),
            pytest.param(
                non_default_parent_script, "not_overlap_merge", id="non-default"
            ),
            pytest.param(
                non_merge_commit_script, "not_overlap_merge", id="non-merge-commit"
            ),
            pytest.param(tolerated_merge_script, "not_overlap_merge", id="no-overlap"),
        ],
    )
    def test_each_refusal_plans_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: Callable[[str, str, str], dict[tuple[str, ...], Any]],
        refusal: str,
    ) -> None:
        _install(monkeypatch, script(CLEAN, HEAD, DEFAULT))

        assessment = _assess(tmp_path)

        assert assessment.plan is None
        assert assessment.refusal == refusal

    def test_a_hand_edit_is_the_textual_check_kill_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing the tree comparison would plan a hand-edited merge."""
        fake = _install(
            monkeypatch, delta_merge_script(CLEAN, HEAD, DEFAULT, merged_tree="x")
        )
        assert _assess(tmp_path).refusal == "not_textual_merge"
        assert merge_tree_key(HEAD) in [tuple(call) for call in fake.calls]

    def test_head_at_the_clean_sha_is_not_an_overlap_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(monkeypatch, {})
        assessment = _assess(tmp_path, head=CLEAN)
        assert assessment.refusal == "not_overlap_merge"
        assert fake.calls == []

    def test_an_open_ledger_is_not_confirmable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(monkeypatch, {})
        ledger, _ = record(fresh_ledger(confirm="codex"), sha=CLEAN)
        assert ledger.status == "awaiting_confirm"

        assessment = _assess(tmp_path, ledger=ledger)

        assert assessment.refusal == "ledger_not_confirmable"
        assert fake.calls == []

    def test_a_supplied_empty_overlap_is_not_an_overlap_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(monkeypatch, {})
        assert _assess(tmp_path, overlap=()).refusal == "not_overlap_merge"
        assert fake.calls == []


_LEDGER_REFUSALS = [
    pytest.param({"loop_backend": "codex", "confirm": "claude"}, id="codex-loop"),
    pytest.param({"confirm": "none"}, id="confirm-none"),
    pytest.param({"confirm_not_independent": True}, id="not-independent"),
]


class TestLedgerRefusalsAreCheap:
    @pytest.mark.parametrize("kwargs", _LEDGER_REFUSALS)
    def test_no_git_call_and_no_overlap_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        fake = _install(monkeypatch, {})

        assessment = _assess(tmp_path, ledger=delta_ledger(clean=CLEAN, **kwargs))

        assert assessment.refusal == "ledger_not_confirmable"
        assert assessment.overlapping_files == ()
        assert fake.calls == []

    @pytest.mark.parametrize("kwargs", _LEDGER_REFUSALS)
    def test_a_supplied_overlap_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        fake = _install(monkeypatch, {})

        assessment = _assess(
            tmp_path, ledger=delta_ledger(clean=CLEAN, **kwargs), overlap=("a.py",)
        )

        assert assessment.refusal == "ledger_not_confirmable"
        assert assessment.overlapping_files == ("a.py",)
        assert fake.calls == []


def _git_failure(how: str) -> int | BaseException:
    """A scripted answer for *how*: an ``OSError``, rc 128 or a non-UTF-8 read."""
    if how == "oserror":
        return OSError("git vanished")
    if how == "unicode":
        return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    return 128


class TestUnreadableGit:
    @pytest.mark.parametrize("how", ["oserror", "rc128", "unicode"])
    @pytest.mark.parametrize(
        "key",
        [
            pytest.param(merge_tree_key(HEAD), id="merge-tree"),
            pytest.param(("rev-parse", f"{HEAD}^{{tree}}"), id="merge-tree-sha"),
            pytest.param(("merge-base", CLEAN, DEFAULT), id="branch-base"),
        ],
    )
    def test_stays_strict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        how: str,
        key: tuple[str, ...],
    ) -> None:
        script = delta_merge_script(CLEAN, HEAD, DEFAULT)
        assert key in script
        script[key] = _git_failure(how)
        fake = _install(monkeypatch, script)

        assessment = _assess(tmp_path)

        assert assessment.plan is None
        assert assessment.refusal in {"not_overlap_merge", "not_textual_merge"}
        assert list(key) in fake.calls


class TestRealGit:
    def test_the_scripted_argvs_are_the_ones_real_git_is_asked(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fidelity guard, ``merge-tree --write-tree`` included."""
        wt = make_git_repo_with_origin("wt")[0]
        merged = branch_with_overlap_merge(wt, tmp_path / "main-clone")
        recorded: list[tuple[str, ...]] = []

        def _run(argv: list[str], **kwargs: Any) -> Any:
            recorded.append(tuple(argv))
            return run_git(argv, **kwargs)

        def _output(argv: list[str], **kwargs: Any) -> str:
            recorded.append(tuple(argv))
            return git_output(argv, **kwargs)

        monkeypatch.setattr(_merge_tolerance, "run_git", _run)
        monkeypatch.setattr(_merge_tolerance, "git_output", _output)

        assessment = assess_delta_round(
            delta_ledger(clean=merged.clean),
            worktree=wt,
            head_sha=merged.head,
            tolerance=ON,
        )

        assert assessment.refusal is None
        assert assessment.overlapping_files == (OVERLAP_FILE,)
        assert assessment.plan is not None
        branch_base = git_in(wt, "merge-base", merged.clean, merged.main_tip)
        assert assessment.plan.spec.branch_base == branch_base
        parent2 = git_in(wt, "rev-parse", f"{merged.head}^2")
        script = delta_merge_script(merged.clean, merged.head, merged.main_tip)
        real = {PARENT2_SHA: parent2, MERGE_BASE_SHA: branch_base}
        expected = {tuple(real.get(part, part) for part in key) for key in script}
        assert set(recorded) == expected


class TestSuppliedOverlapStillReadsTheSpan:
    """With ``overlap`` given, the plan's own git reads still fail closed."""

    def test_an_unreadable_branch_base_is_not_textual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = delta_merge_script(CLEAN, HEAD, DEFAULT)
        script["merge-base", CLEAN, DEFAULT] = 128
        _install(monkeypatch, script)

        assessment = _assess(tmp_path, overlap=(BRANCH_FILE,))

        assert assessment.plan is None
        assert assessment.refusal == "not_textual_merge"

    def test_an_unreadable_span_is_not_textual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            {("rev-parse", "refs/remotes/origin/main^{commit}"): 128},
        )

        assessment = _assess(tmp_path, overlap=(BRANCH_FILE,))

        assert assessment.plan is None
        assert assessment.refusal == "not_textual_merge"
        assert assessment.overlapping_files == (BRANCH_FILE,)
