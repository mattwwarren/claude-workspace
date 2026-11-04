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
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    MergeTolerance,
    _merge_tolerance,
    assess_delta_round,
    converged_across_tolerated_merge,
    default_merge_only,
    demote_for_rereview,
    overlapping_default_merge,
    tolerance_for_client,
    tolerance_from_context,
    tolerates_review_move,
)
from tests._review_convergence_helpers import (
    BRANCH_FILE,
    MERGE_BASE_SHA,
    PARENT2_SHA,
    ScriptedMergeGit,
    branch_that_merged_main,
    branch_touching_merge_script,
    confirmed_ledger,
    delta_ledger,
    diff_returns,
    fail_when,
    fresh_ledger,
    make_review_client,
    non_default_parent_script,
    non_merge_commit_script,
    tolerated_merge_script,
    write_ledger,
)
from tests.conftest import add_bare_origin, commit_tracked_file, git_in

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


class TestDeltaConfirmFlag:
    """``MergeTolerance.delta_confirm`` from both readers (#2918)."""

    def test_the_client_flag_is_carried(self, tmp_path: Path) -> None:
        on = tolerance_for_client(make_review_client(tmp_path, delta_confirm=True))
        off = tolerance_for_client(make_review_client(tmp_path))
        assert on is not None
        assert on.delta_confirm is True
        assert off is not None
        assert off.delta_confirm is False

    def test_the_flag_never_widens_a_strict_client(self, tmp_path: Path) -> None:
        client = make_review_client(
            tmp_path, default_merge_tolerance=False, delta_confirm=True
        )
        assert tolerance_for_client(client) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(True, True, id="true"),
            pytest.param(False, False, id="false"),
            pytest.param("true", False, id="string-is-malformed"),
            pytest.param(1, False, id="int-is-malformed"),
            pytest.param(None, False, id="null-is-malformed"),
        ],
    )
    def test_the_context_flag_is_true_only_for_a_literal_true(
        self, value: object, expected: bool
    ) -> None:
        context: dict[str, object] = {
            "world_state_snapshot": {"origin_main_branch": "main"}
        }
        tolerance = tolerance_from_context(
            context, {"default_merge_delta_confirm": value}
        )
        assert tolerance is not None
        assert tolerance.delta_confirm is expected

    def test_an_absent_context_flag_is_off(self) -> None:
        context: dict[str, object] = {
            "world_state_snapshot": {"origin_main_branch": "main"}
        }
        tolerance = tolerance_from_context(context, {})
        assert tolerance is not None
        assert tolerance.delta_confirm is False

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            pytest.param(
                ReviewConfig(convergence="clean", default_merge_delta_confirm=True),
                True,
                id="enabled",
            ),
            pytest.param(ReviewConfig(convergence="clean"), False, id="default"),
        ],
    )
    def test_the_producer_context_round_trips_into_the_reader(
        self, config: ReviewConfig, expected: bool
    ) -> None:
        # No hand-written key: the real ``to_context`` output is the reader's
        # input, so the two sides cannot drift apart unnoticed.
        context: dict[str, object] = {
            "world_state_snapshot": {"origin_main_branch": "main"}
        }
        tolerance = tolerance_from_context(context, config.to_context())
        assert tolerance is not None
        assert tolerance.delta_confirm is expected

    def test_two_argument_construction_is_unchanged(self) -> None:
        assert MergeTolerance("main", frozenset()) == MergeTolerance(
            "main", frozenset(), delta_confirm=False
        )


def _two_file_overlap_script() -> dict[tuple[str, ...], Any]:
    """An overlap merge over two branch files, listed out of order."""
    script = tolerated_merge_script(CLEAN, HEAD, DEFAULT)
    both = "z_feature.py\0a_feature.py\0"
    script["diff", "--name-only", "--no-renames", "-z", MERGE_BASE_SHA, CLEAN] = both
    script["diff", "--name-only", "--no-renames", "-z", CLEAN, HEAD] = both
    script["diff", "--name-only", "--no-renames", "-z", MERGE_BASE_SHA, DEFAULT] = both
    script["diff", "--name-only", "--no-renames", "-z", PARENT2_SHA, HEAD] = both
    return script


class TestOverlappingDefaultMerge:
    """The overlap-only rejection, named (#2918)."""

    def test_an_overlap_merge_names_the_branch_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(branch_touching_merge_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) == (
            BRANCH_FILE,
        )

    def test_the_overlap_is_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(_two_file_overlap_script()).install(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) == (
            "a_feature.py",
            "z_feature.py",
        )

    def test_a_tolerated_merge_overlaps_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tolerated(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) == ()

    def test_an_exempt_overlap_is_not_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(branch_touching_merge_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )
        exempt = MergeTolerance("main", frozenset({BRANCH_FILE}))
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, exempt) == ()

    @pytest.mark.parametrize(
        "script",
        [
            pytest.param(non_merge_commit_script, id="non-merge-commit"),
            pytest.param(non_default_parent_script, id="non-default-parent"),
        ],
    )
    def test_any_other_rejection_is_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: Callable[[str, str, str], dict[tuple[str, ...], Any]],
    ) -> None:
        ScriptedMergeGit(script(CLEAN, HEAD, DEFAULT)).install(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) is None

    def test_landed_outside_moved_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = branch_touching_merge_script(CLEAN, HEAD, DEFAULT)
        script["diff", "--name-only", "--no-renames", "-z", MERGE_BASE_SHA, DEFAULT] = (
            "other.py\0"
        )
        ScriptedMergeGit(script).install(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) is None

    def test_a_hand_edit_outside_the_branch_files_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = branch_touching_merge_script(CLEAN, HEAD, DEFAULT)
        script["diff", "--name-only", "--no-renames", "-z", PARENT2_SHA, HEAD] = (
            f"{BRANCH_FILE}\0hand-edit.py\0"
        )
        ScriptedMergeGit(script).install(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) is None

    @pytest.mark.parametrize(
        "prefix",
        [
            pytest.param(["rev-parse"], id="tracking-ref"),
            pytest.param(["merge-base", CLEAN], id="merge-base"),
            pytest.param(["diff"], id="path-listing"),
        ],
    )
    def test_unreadable_git_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: list[str]
    ) -> None:
        fake = ScriptedMergeGit(branch_touching_merge_script(CLEAN, HEAD, DEFAULT))
        script = fake.script
        for key in [k for k in script if list(k[: len(prefix)]) == prefix]:
            script[key] = 128
        fake.install(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, TOLERANCE) is None

    def test_no_tolerance_makes_no_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)
        assert overlapping_default_merge(tmp_path, CLEAN, HEAD, None) is None
        assert fake.calls == []

    def test_default_merge_only_still_refuses_the_overlap_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The split of ``_untouched`` left ``default_merge_only`` unchanged."""
        fake = ScriptedMergeGit(
            branch_touching_merge_script(CLEAN, HEAD, DEFAULT)
        ).install(monkeypatch)
        assert default_merge_only(tmp_path, CLEAN, HEAD, TOLERANCE) is False
        merged_parent_diff = [
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            PARENT2_SHA,
            HEAD,
        ]
        assert merged_parent_diff not in fake.calls


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

    def test_a_value_error_from_git_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``subprocess.run`` raises ``ValueError`` on an embedded NUL in argv."""

        def _raise(argv: list[str], **_kwargs: Any) -> Any:
            msg = "embedded null byte"
            raise ValueError(msg)

        monkeypatch.setattr(_merge_tolerance, "run_git", _raise)
        assert _merge_tolerance.git_stdout_or_none(tmp_path, ["rev-parse", "x"]) is None
        assert default_merge_only(tmp_path, "a\x00b", HEAD, TOLERANCE) is False


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
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is True
        )

    def test_a_missing_ledger_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
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
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
        assert fake.calls == []

    def test_an_unreadable_ledger_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory at the ledger path makes the read raise ``OSError``."""
        (tmp_path / LEDGER_RELATIVE_PATH).mkdir(parents=True)
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
        assert fake.calls == []

    def test_a_ledger_clean_at_another_sha_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, _converged(sha="f" * 40))
        fake = _deny(monkeypatch)
        client = make_review_client(tmp_path)
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
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
        assert (
            tolerates_review_move(client, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
        assert fake.calls == []

    def test_no_client_is_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, _converged())
        fake = _deny(monkeypatch)
        assert (
            tolerates_review_move(None, tmp_path, reviewed_sha=CLEAN, head_sha=HEAD)
            is False
        )
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


def test_a_rename_of_a_branch_edited_file_on_main_is_not_tolerated(
    make_git_repo: Callable[..., Path],
) -> None:
    """Real git: main renames a file the branch edited; the merge is strict (#2918).

    The merge holds both edits at ``new.py`` and ``old.py`` is gone, so
    ``(B & C) - exempt`` is empty and a path-set comparison alone would call it
    "no overlap". It is refused because ``new.py`` is not in the branch's own
    diff, so the merge differs from main's parent there
    (``_merges_add_only_branch_paths``). Neither the overlap reader nor the
    delta assessment may report it as eligible; removing the
    ``_merges_add_only_branch_paths`` guard in ``_merge_overlap`` turns this red.
    """
    repo = make_git_repo("rename")
    lines = [f"line {n}" for n in range(1, 21)]
    (repo / "old.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    git_in(repo, "add", "old.py")
    git_in(repo, "commit", "-m", "add old.py")
    add_bare_origin(repo)
    git_in(repo, "checkout", "-b", "dev")
    branch_lines = [*lines[:1], "branch 2", *lines[2:]]
    (repo / "old.py").write_text("\n".join(branch_lines) + "\n", encoding="utf-8")
    git_in(repo, "commit", "-am", "branch edits line 2")
    clean = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "main")
    git_in(repo, "mv", "old.py", "new.py")
    main_lines = [*lines[:18], "main 19", *lines[19:]]
    (repo / "new.py").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    git_in(repo, "commit", "-am", "main renames old.py and edits line 19")
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", "dev")
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "--no-edit", "origin/main")
    head = git_in(repo, "rev-parse", "HEAD")
    merged = (repo / "new.py").read_text(encoding="utf-8").splitlines()
    assert not (repo / "old.py").exists()
    assert merged[1] == "branch 2"
    assert merged[18] == "main 19"
    tolerance = MergeTolerance("main", frozenset(), True)

    assert overlapping_default_merge(repo, clean, head, tolerance) is None
    assessment = assess_delta_round(
        delta_ledger(clean=clean), worktree=repo, head_sha=head, tolerance=tolerance
    )
    assert assessment.plan is None
    assert assessment.refusal == "not_overlap_merge"
