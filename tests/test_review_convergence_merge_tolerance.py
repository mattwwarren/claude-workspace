"""Tests for ``cw.review_convergence._merge_tolerance`` (#2815, #2861).

The default-branch merge tolerance predicate ``verify-tip``, ``finalize``,
``converge init``, ``converge status`` and the REVIEW staleness gate share.
Most tests are hermetic: a :class:`ScriptedMergeGit` answers the predicate's
git calls, and an unscripted call fails the test. One real-git test pins that
the scripted argvs are exactly the ones the predicate issues against a real
repository, so the fake cannot drift from git. ``TestMergeParents`` and
``TestPathListing`` moved here from ``tests/test_cli_review_converge.py`` with
the functions they cover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw._git import git_output, run_git
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    MergeTolerance,
    _merge_tolerance,
    converged_across_tolerated_merge,
    default_merge_only,
    demote_for_rereview,
    tolerance_for_client,
    tolerates_review_move,
)
from tests._review_convergence_helpers import (
    ScriptedMergeGit,
    branch_that_merged_main,
    branch_touching_merge_script,
    confirmed_ledger,
    diff_returns,
    fail_when,
    fresh_ledger,
    make_review_client,
    non_merge_commit_script,
    tolerated_merge_script,
    write_ledger,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger

CLEAN = "c" * 40
HEAD = "d" * 40
DEFAULT = "e" * 40
TOLERANCE = MergeTolerance("main", frozenset({"CHANGELOG.md"}))


@pytest.fixture
def wt(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    """A worktree on ``feature``, pushed to a bare origin."""
    return make_git_repo_with_origin("wt")[0]


def _tolerated(monkeypatch: pytest.MonkeyPatch) -> ScriptedMergeGit:
    return ScriptedMergeGit(tolerated_merge_script(CLEAN, HEAD, DEFAULT)).install(
        monkeypatch
    )


def _deny(monkeypatch: pytest.MonkeyPatch) -> ScriptedMergeGit:
    return ScriptedMergeGit({}).install(monkeypatch)


def _converged(sha: str = CLEAN) -> ConvergenceLedger:
    return confirmed_ledger(sha=sha)


class TestToleranceForClient:
    def test_a_clean_tolerant_client_names_its_branch_and_exempt_paths(
        self, tmp_path: Path
    ) -> None:
        client = make_review_client(
            tmp_path, default_branch="trunk", ignore_paths=["docs/generated.md"]
        )
        assert tolerance_for_client(client) == MergeTolerance(
            "trunk", frozenset({"CHANGELOG.md", "docs/generated.md"})
        )

    def test_the_strict_opt_out_is_none(self, tmp_path: Path) -> None:
        client = make_review_client(tmp_path, default_merge_tolerance=False)
        assert tolerance_for_client(client) is None

    def test_a_blocking_client_is_none(self, tmp_path: Path) -> None:
        client = make_review_client(tmp_path, convergence="blocking")
        assert tolerance_for_client(client) is None


class TestDefaultMergeOnly:
    def test_no_tolerance_is_strict_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)
        assert default_merge_only(tmp_path, CLEAN, HEAD, None) is False
        assert fake.calls == []

    def test_a_scripted_default_branch_merge_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tolerated(monkeypatch)
        assert default_merge_only(tmp_path, CLEAN, HEAD, TOLERANCE) is True

    @pytest.mark.parametrize(
        "script",
        [
            pytest.param(non_merge_commit_script, id="non-merge-commit"),
            pytest.param(branch_touching_merge_script, id="branch-touching-merge"),
        ],
    )
    def test_a_non_tolerated_move_is_strict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: Callable[[str, str, str], dict[tuple[str, ...], Any]],
    ) -> None:
        ScriptedMergeGit(script(CLEAN, HEAD, DEFAULT)).install(monkeypatch)
        assert default_merge_only(tmp_path, CLEAN, HEAD, TOLERANCE) is False

    def test_the_scripted_argvs_are_the_ones_real_git_is_asked(
        self, wt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fidelity guard: the fake's keys equal the argvs issued on a real repo."""
        branch_that_merged_main(wt, tmp_path / "main-clone")
        merge = git_in(wt, "rev-parse", "HEAD~1")
        clean = git_in(wt, "rev-parse", f"{merge}^1")
        parent2 = git_in(wt, "rev-parse", f"{merge}^2")
        default_sha = git_in(wt, "rev-parse", "origin/main")
        merge_base = git_in(wt, "merge-base", clean, default_sha)
        recorded: list[tuple[str, ...]] = []

        def _run(argv: list[str], **kwargs: Any) -> Any:
            recorded.append(tuple(argv))
            return run_git(argv, **kwargs)

        def _output(argv: list[str], **kwargs: Any) -> str:
            recorded.append(tuple(argv))
            return git_output(argv, **kwargs)

        monkeypatch.setattr(_merge_tolerance, "run_git", _run)
        monkeypatch.setattr(_merge_tolerance, "git_output", _output)

        assert default_merge_only(wt, clean, merge, TOLERANCE) is True
        script = tolerated_merge_script(
            clean, merge, default_sha, parent2=parent2, merge_base=merge_base
        )
        assert set(recorded) == set(script)


class TestConvergedAcrossToleratedMerge:
    def test_a_converged_claude_native_ledger_across_a_tolerated_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tolerated(monkeypatch)
        assert converged_across_tolerated_merge(
            _converged(), worktree=tmp_path, head_sha=HEAD, tolerance=TOLERANCE
        )

    @pytest.mark.parametrize(
        "ledger",
        [
            pytest.param(fresh_ledger(), id="no-clean-round"),
            pytest.param(demote_for_rereview(_converged()), id="reopened"),
            pytest.param(
                _converged().model_copy(update={"loop_backend": "codex"}), id="codex"
            ),
            pytest.param(
                _converged().model_copy(update={"loop_backend": "opencode"}),
                id="opencode",
            ),
            pytest.param(
                _converged().model_copy(update={"loop_backend": "mystery"}),
                id="unknown-backend",
            ),
            pytest.param(_converged(sha=HEAD), id="clean-at-head"),
        ],
    )
    def test_anything_else_is_strict_without_a_git_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ledger: ConvergenceLedger,
    ) -> None:
        fake = _deny(monkeypatch)
        assert not converged_across_tolerated_merge(
            ledger, worktree=tmp_path, head_sha=HEAD, tolerance=TOLERANCE
        )
        assert fake.calls == []

    def test_no_tolerance_is_strict_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)
        assert not converged_across_tolerated_merge(
            _converged(), worktree=tmp_path, head_sha=HEAD, tolerance=None
        )
        assert fake.calls == []

    def test_a_clean_sha_that_is_not_behind_head_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tolerated_merge_script(CLEAN, HEAD, DEFAULT)
        script["merge-base", "--is-ancestor", CLEAN, HEAD] = 1
        ScriptedMergeGit(script).install(monkeypatch)
        assert not converged_across_tolerated_merge(
            _converged(), worktree=tmp_path, head_sha=HEAD, tolerance=TOLERANCE
        )

    @pytest.mark.parametrize("answer", [128, OSError("git vanished")])
    def test_unreadable_git_state_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: int | OSError
    ) -> None:
        script = tolerated_merge_script(CLEAN, HEAD, DEFAULT)
        script["rev-parse", "refs/remotes/origin/main^{commit}"] = answer
        ScriptedMergeGit(script).install(monkeypatch)
        assert not converged_across_tolerated_merge(
            _converged(), worktree=tmp_path, head_sha=HEAD, tolerance=TOLERANCE
        )


class TestToleratesReviewMove:
    def test_a_ledger_converged_at_the_reviewed_sha_across_a_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, _converged())
        _tolerated(monkeypatch)
        client = make_review_client(tmp_path)
        assert tolerates_review_move(client, tmp_path, CLEAN, HEAD) is True

    def test_a_missing_ledger_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert tolerates_review_move(client, tmp_path, CLEAN, HEAD) is False
        assert fake.calls == []

    @pytest.mark.parametrize("raw", [b"\xff\xfe", b"{not json", b"{}"])
    def test_a_corrupt_ledger_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
    ) -> None:
        path = tmp_path / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert tolerates_review_move(client, tmp_path, CLEAN, HEAD) is False
        assert fake.calls == []

    def test_a_ledger_clean_at_another_sha_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, _converged(sha="f" * 40))
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert tolerates_review_move(client, tmp_path, CLEAN, HEAD) is False
        assert fake.calls == []

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"convergence": "blocking"}, id="blocking"),
            pytest.param({"default_merge_tolerance": False}, id="strict-opt-out"),
        ],
    )
    def test_a_client_without_the_tolerance_is_strict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        overrides: dict[str, Any],
    ) -> None:
        write_ledger(tmp_path, _converged())
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path, **overrides)
        assert tolerates_review_move(client, tmp_path, CLEAN, HEAD) is False
        assert fake.calls == []

    def test_no_client_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, _converged())
        fake = _deny(monkeypatch)
        assert tolerates_review_move(None, tmp_path, CLEAN, HEAD) is False
        assert fake.calls == []


class TestMergeParents:
    """``_merge_parents``: the one parse of a merge's extra parents (#2815)."""

    @staticmethod
    def _listing(monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
        monkeypatch.setattr(
            _merge_tolerance, "git_stdout_or_none", lambda _wt, _argv: text
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("", [], id="no-merge"),
            pytest.param("m p1 p2\n", [("m", ["p2"])], id="two-parent"),
            pytest.param(
                "m p1 p2 p3\nn q1 q2\n",
                [("m", ["p2", "p3"]), ("n", ["q2"])],
                id="octopus-and-plain",
            ),
        ],
    )
    def test_extra_parents_are_every_parent_after_the_first(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        text: str,
        expected: list[tuple[str, list[str]]],
    ) -> None:
        self._listing(monkeypatch, text)
        assert _merge_tolerance._merge_parents(wt, "a..b") == expected

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(None, id="git-failed"),
            pytest.param("m p1\n", id="one-parent"),
            pytest.param("m\n", id="no-parent"),
            pytest.param("m p1 p2\n\n", id="blank-line"),
        ],
    )
    def test_an_unreadable_listing_is_none(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch, text: str | None
    ) -> None:
        self._listing(monkeypatch, text)
        assert _merge_tolerance._merge_parents(wt, "a..b") is None


class TestPathListing:
    """``_paths``: the ``git diff --name-only --no-renames -z`` path set (#2815)."""

    def test_renames_and_non_ascii_names_are_listed_verbatim(self, wt: Path) -> None:
        """Deferred premise: both rename sides, an unquoted non-ASCII name."""
        base = git_in(wt, "rev-parse", "HEAD")
        git_in(wt, "mv", "feature.py", "renamed.py")
        git_in(wt, "commit", "-m", "rename")
        commit_tracked_file(wt, "café.py")
        assert _merge_tolerance._paths(wt, base, "HEAD") == {
            "feature.py",
            "renamed.py",
            "café.py",
        }

    def test_identical_trees_are_the_empty_set(self, wt: Path) -> None:
        assert _merge_tolerance._paths(wt, "HEAD", "HEAD") == set()

    def test_empty_output_is_the_empty_set(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        diff_returns(monkeypatch, "")
        assert _merge_tolerance._paths(wt, "HEAD", "HEAD") == set()

    @pytest.mark.parametrize("text", ["a\0\0b\0", "a\0b", "\0"])
    def test_an_oddly_shaped_listing_is_none(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch, text: str
    ) -> None:
        diff_returns(monkeypatch, text)
        assert _merge_tolerance._paths(wt, "HEAD", "HEAD") is None

    @pytest.mark.parametrize("how", ["oserror", "rc128", "unicode"])
    def test_a_failing_listing_is_none(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch, how: str
    ) -> None:
        fail_when(monkeypatch, ["diff"], how)
        assert _merge_tolerance._paths(wt, "HEAD", "HEAD") is None
