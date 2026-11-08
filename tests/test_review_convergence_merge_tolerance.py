"""Tests for ``cw.review_convergence._merge_tolerance`` (#2815, #2861, #2979).

The tip rule ``verify-tip``, ``finalize``, ``converge init``, ``converge
status`` and the REVIEW staleness gate share: a merge of the default branch
that git itself would write never stales a clean-mode ledger, whatever files it
touches and whatever the loop backend; a conflict resolution, a hand edit, an
octopus, a merge of a non-default commit and a commit of the branch's own do.

The shape cases use real git repositories. A :class:`ScriptedMergeGit` is kept
only for what real git cannot produce on demand (unreadable git, a
``ValueError``, a timeout) and for the "no git call at all" assertions; one
real-git test pins that its scripted argvs are exactly the ones the predicate
issues, so the fake cannot drift from git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw import merge_probe
from cw._git import run_git
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    MergeTolerance,
    _merge_tolerance,
    converged_across_tolerated_merge,
    default_merge_only,
    default_merge_rejection,
    demote_for_rereview,
    is_stale_default_ref_symptom,
    tolerance_for_client,
    tolerance_from_context,
    tolerates_review_move,
)
from tests._review_convergence_helpers import (
    PARENT2_SHA,
    ScriptedMergeGit,
    branch_that_merged_main,
    branch_with_conflict_resolution_merge,
    branch_with_overlap_merge,
    confirmed_ledger,
    fresh_ledger,
    make_review_client,
    merge_origin_commit,
    merge_tree_key,
    non_merge_commit_script,
    side_branch_commit,
    tolerated_merge_script,
    write_ledger,
)
from tests.conftest import (
    add_bare_origin,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.review_convergence import ConvergenceLedger

CLEAN = "c" * 40
HEAD = "d" * 40
DEFAULT = "e" * 40
SPAN = f"{CLEAN}..{HEAD}"
TOLERANCE = MergeTolerance("main")
BACKENDS = ("claude-native", "codex", "opencode")


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


def _converged(
    sha: str = CLEAN, loop_backend: str = "claude-native"
) -> ConvergenceLedger:
    return confirmed_ledger(sha=sha, loop_backend=loop_backend)


def _short(sha: str) -> str:
    return sha[:7]


def _hand_edited_merge(wt: Path, scratch: Path) -> tuple[str, str]:
    """``(clean, merge)``: a clean merge of ``origin/main`` plus a hand edit."""
    clean = git_in(wt, "rev-parse", "HEAD")
    origin = Path(git_in(wt, "remote", "get-url", "origin"))
    push_commit_to_origin(origin, "main", scratch, "main_only.py")
    git_in(wt, "fetch", "origin")
    git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
    (wt / "feature.py").write_text("hand edit in the merge\n", encoding="utf-8")
    git_in(wt, "add", "feature.py")
    git_in(wt, "commit", "--no-edit")
    return clean, git_in(wt, "rev-parse", "HEAD")


class TestStaleDefaultRefSymptom:
    """The stale-ref vocabulary is owned next to the ``REJECT_*`` reasons (#2979)."""

    @pytest.mark.parametrize(
        "reason",
        [
            "second_parent_not_on_default",
            "non_merge_commit",
            "no_default_ref",
        ],
    )
    def test_each_stale_ref_reason_is_a_symptom_with_and_without_a_sha(
        self, reason: str
    ) -> None:
        assert is_stale_default_ref_symptom(reason)
        assert is_stale_default_ref_symptom(f"{reason} abc1234")

    @pytest.mark.parametrize(
        "reason",
        [
            "not_textually_clean abc1234",
            "merge_tree_unavailable abc1234",
            "not_textually_clean",
            "merge_tree_unavailable",
            "tolerance_off",
        ],
    )
    def test_other_reasons_are_not_symptoms(self, reason: str) -> None:
        assert not is_stale_default_ref_symptom(reason)


class TestToleranceReaders:
    def test_a_clean_tolerant_client_names_its_branch(self, tmp_path: Path) -> None:
        client = make_review_client(
            tmp_path, default_branch="trunk", ignore_paths=["docs/generated.md"]
        )
        assert tolerance_for_client(client) == MergeTolerance("trunk")

    def test_the_strict_opt_out_is_none(self, tmp_path: Path) -> None:
        client = make_review_client(tmp_path, default_merge_tolerance=False)
        assert tolerance_for_client(client) is None

    def test_a_blocking_client_is_none(self, tmp_path: Path) -> None:
        client = make_review_client(tmp_path, convergence="blocking")
        assert tolerance_for_client(client) is None

    @pytest.mark.parametrize(
        ("context", "review", "expected"),
        [
            pytest.param(
                {"world_state_snapshot": {"origin_main_branch": "main"}},
                {},
                TOLERANCE,
                id="absent-key-is-tolerant",
            ),
            pytest.param(
                {
                    "world_state_snapshot": {"origin_main_branch": "main"},
                    "merge_gate_ignore_paths": [1],
                },
                {"default_merge_tolerance": True},
                TOLERANCE,
                id="ignore-list-is-not-read",
            ),
            pytest.param(
                {"world_state_snapshot": {"origin_main_branch": "main"}},
                {"default_merge_tolerance": False},
                None,
                id="opt-out",
            ),
            pytest.param(
                {"world_state_snapshot": {"origin_main_branch": "main"}},
                {"default_merge_tolerance": "yes"},
                None,
                id="malformed-flag",
            ),
            pytest.param({}, {}, None, id="no-branch"),
            pytest.param(
                {"world_state_snapshot": {"origin_main_branch": ""}},
                {},
                None,
                id="empty-branch",
            ),
            pytest.param(None, {}, None, id="no-context"),
            pytest.param({}, None, None, id="no-review"),
        ],
    )
    def test_tolerance_from_context(
        self,
        context: dict[str, object] | None,
        review: dict[str, object] | None,
        expected: MergeTolerance | None,
    ) -> None:
        assert tolerance_from_context(context, review) == expected


class TestRealGitShapes:
    """Every merge shape, against a real repository (#2979)."""

    def test_a_clean_disjoint_merge_is_tolerated(
        self, wt: Path, tmp_path: Path
    ) -> None:
        branch_that_merged_main(wt, tmp_path / "scratch")
        merge = git_in(wt, "rev-parse", "HEAD~1")
        clean = git_in(wt, "rev-parse", f"{merge}^1")

        assert default_merge_rejection(wt, clean, merge, TOLERANCE) is None
        assert default_merge_only(wt, clean, merge, TOLERANCE) is True

    def test_a_clean_overlapping_merge_is_tolerated(
        self, wt: Path, tmp_path: Path
    ) -> None:
        """The ticket's case: overlap with the branch's own file does not matter."""
        found = branch_with_overlap_merge(wt, tmp_path / "scratch")

        assert default_merge_rejection(wt, found.clean, found.head, TOLERANCE) is None

    def test_a_conflict_resolution_merge_is_stale(
        self, wt: Path, tmp_path: Path
    ) -> None:
        found = branch_with_conflict_resolution_merge(wt, tmp_path / "scratch")

        assert (
            default_merge_rejection(wt, found.clean, found.head, TOLERANCE)
            == f"not_textually_clean {_short(found.head)}"
        )

    def test_a_hand_edited_clean_merge_is_stale(self, wt: Path, tmp_path: Path) -> None:
        clean, merge = _hand_edited_merge(wt, tmp_path / "scratch")

        assert (
            default_merge_rejection(wt, clean, merge, TOLERANCE)
            == f"not_textually_clean {_short(merge)}"
        )

    def test_a_non_merge_commit_after_the_merge_is_stale(
        self, wt: Path, tmp_path: Path
    ) -> None:
        branch_that_merged_main(wt, tmp_path / "scratch")
        head = git_in(wt, "rev-parse", "HEAD")
        clean = git_in(wt, "rev-parse", "HEAD~1^1")

        assert (
            default_merge_rejection(wt, clean, head, TOLERANCE)
            == f"non_merge_commit {_short(head)}"
        )

    def test_a_merge_of_a_side_branch_commit_is_stale(self, wt: Path) -> None:
        clean = git_in(wt, "rev-parse", "HEAD")
        side_branch_commit(wt)
        git_in(wt, "merge", "--no-edit", "--no-ff", "side")
        merge = git_in(wt, "rev-parse", "HEAD")

        side = git_in(wt, "rev-parse", "side")

        reason = default_merge_rejection(wt, clean, merge, TOLERANCE)

        # The side commit is the branch's own change, found before the merge.
        assert reason == f"non_merge_commit {_short(side)}"

    def test_an_octopus_merge_is_stale(self, wt: Path, tmp_path: Path) -> None:
        clean = git_in(wt, "rev-parse", "HEAD")
        origin = Path(git_in(wt, "remote", "get-url", "origin"))
        push_commit_to_origin(origin, "main", tmp_path / "scratch", "main_only.py")
        git_in(wt, "fetch", "origin")
        side_branch_commit(wt)
        git_in(wt, "merge", "--no-edit", "origin/main", "side")
        merge = git_in(wt, "rev-parse", "HEAD")
        assert len(git_in(wt, "rev-list", "--parents", "-n1", merge).split()) == 4

        # Stale; the scripted case below pins the ``not_two_parent`` reason.
        assert default_merge_rejection(wt, clean, merge, TOLERANCE) is not None

    def test_a_clean_then_conflict_merge_mix_names_the_conflict(
        self, wt: Path, tmp_path: Path
    ) -> None:
        found = branch_with_conflict_resolution_merge(wt, tmp_path / "scratch")
        merge_origin_commit(wt, tmp_path / "scratch2", "main", "later.py")
        head = git_in(wt, "rev-parse", "HEAD")

        assert (
            default_merge_rejection(wt, found.clean, head, TOLERANCE)
            == f"not_textually_clean {_short(found.head)}"
        )

    def test_two_clean_merges_are_tolerated(self, wt: Path, tmp_path: Path) -> None:
        clean = git_in(wt, "rev-parse", "HEAD")
        merge_origin_commit(wt, tmp_path / "one", "main", "one.py")
        merge_origin_commit(wt, tmp_path / "two", "main", "two.py")
        head = git_in(wt, "rev-parse", "HEAD")

        assert default_merge_rejection(wt, clean, head, TOLERANCE) is None

    def test_a_span_within_the_merge_loop_budget_is_tolerated(
        self, wt: Path, tmp_path: Path
    ) -> None:
        clean = git_in(wt, "rev-parse", "HEAD")
        for name in ("one", "two", "three"):
            merge_origin_commit(wt, tmp_path / name, "main", f"{name}.py")
        head = git_in(wt, "rev-parse", "HEAD")
        ticks = iter([0.0, 1.0, 2.0, _merge_tolerance.TIP_CHECK_BUDGET_SECONDS - 1])

        assert (
            default_merge_rejection(
                wt, clean, head, TOLERANCE, clock=lambda: next(ticks)
            )
            is None
        )

    def test_a_spent_budget_stops_probing_and_fails_closed(
        self, wt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clean = git_in(wt, "rev-parse", "HEAD")
        merges: list[str] = []
        for name in ("one", "two", "three"):
            merge_origin_commit(wt, tmp_path / name, "main", f"{name}.py")
            merges.append(git_in(wt, "rev-parse", "HEAD"))
        probed: list[str] = []
        real = _merge_tolerance.merge_tree

        def _recording(worktree: Path, left: str, right: str) -> Any:
            probed.append(left)
            return real(worktree, left, right)

        monkeypatch.setattr(_merge_tolerance, "merge_tree", _recording)
        budget = _merge_tolerance.TIP_CHECK_BUDGET_SECONDS
        # start, then the first merge (in budget), then the second (spent).
        ticks = iter([0.0, 0.0, budget])

        reason = default_merge_rejection(
            wt, clean, merges[-1], TOLERANCE, clock=lambda: next(ticks)
        )

        assert reason == f"merge_tree_unavailable {_short(merges[1])}"
        assert probed == [f"{merges[0]}^1"]

    def test_a_fast_forward_is_not_a_merge(self, wt: Path) -> None:
        clean = git_in(wt, "rev-parse", "HEAD~1")
        head = git_in(wt, "rev-parse", "HEAD")

        assert default_merge_rejection(wt, clean, head, TOLERANCE) == "no_merge"

    def test_a_clean_sha_not_behind_head_is_stale(
        self, wt: Path, tmp_path: Path
    ) -> None:
        side = side_branch_commit(wt)
        head = git_in(wt, "rev-parse", "HEAD")

        assert (
            default_merge_rejection(wt, side, head, TOLERANCE) == "clean_not_ancestor"
        )

    def test_an_unresolvable_default_ref_is_stale(self, wt: Path) -> None:
        head = git_in(wt, "rev-parse", "HEAD")
        tolerance = MergeTolerance("no-such-branch")

        assert default_merge_rejection(wt, head, head, tolerance) == "no_default_ref"

    def test_no_tolerance_is_tolerance_off_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _deny(monkeypatch)

        assert default_merge_rejection(tmp_path, CLEAN, HEAD, None) == "tolerance_off"
        assert default_merge_only(tmp_path, CLEAN, HEAD, None) is False
        assert fake.calls == []

    def test_a_merge_tree_timeout_is_merge_tree_unavailable(
        self, wt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = branch_with_overlap_merge(wt, tmp_path / "scratch")
        real = merge_probe.run_git
        timeouts: list[float | None] = []

        def _slow(
            argv: list[str], *, timeout: float | None = None, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "merge-tree":
                timeouts.append(timeout)
                raise subprocess.TimeoutExpired(["git", *argv], timeout or 0.0)
            return real(argv, timeout=timeout, **kwargs)

        monkeypatch.setattr(merge_probe, "run_git", _slow)

        assert (
            default_merge_rejection(wt, found.clean, found.head, TOLERANCE)
            == f"merge_tree_unavailable {_short(found.head)}"
        )
        assert timeouts == [merge_probe.PROBE_TIMEOUT_SECONDS]


class TestChangelogMerges:
    """The old ``CHANGELOG.md`` exemption is gone; the union driver covers it."""

    @staticmethod
    def _changelog_merge(
        make_git_repo: Callable[..., Path], *, union: bool
    ) -> tuple[Path, str, str]:
        repo = make_git_repo("changelog")
        base = "# Changelog\n\n## [Unreleased]\n\nold line\n"
        if union:
            commit_tracked_file(repo, ".gitattributes", "CHANGELOG.md merge=union\n")
        commit_tracked_file(repo, "CHANGELOG.md", base)
        add_bare_origin(repo)
        git_in(repo, "checkout", "-b", "feature")
        branch_text = "# Changelog\n\n## [Unreleased]\n- branch\n\nold line\n"
        commit_tracked_file(repo, "CHANGELOG.md", branch_text)
        clean = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "checkout", "main")
        main_text = (
            "# Changelog\n\n## [Unreleased]\n- main\n\nold line\n"
            if union
            else "# Changelog\n\n## [Unreleased]\n\nold line\nnew main line\n"
        )
        commit_tracked_file(repo, "CHANGELOG.md", main_text)
        git_in(repo, "push", "origin", "main")
        git_in(repo, "checkout", "feature")
        git_in(repo, "fetch", "origin")
        git_in(repo, "merge", "--no-edit", "origin/main")
        return repo, clean, git_in(repo, "rev-parse", "HEAD")

    @pytest.mark.parametrize("union", [True, False], ids=["union-driver", "ordinary"])
    def test_a_changelog_merge_is_tolerated(
        self, make_git_repo: Callable[..., Path], union: bool
    ) -> None:
        repo, clean, head = self._changelog_merge(make_git_repo, union=union)

        assert default_merge_rejection(repo, clean, head, TOLERANCE) is None


def test_a_rename_of_a_branch_edited_file_on_main_is_tolerated(
    make_git_repo: Callable[..., Path],
) -> None:
    """Real git: main renames a file the branch edited; the merge is clean.

    The merge holds both edits at ``new.py``. Under the file-overlap rule it was
    refused; under the clean-merge rule (#2979) git writes exactly this tree, so
    it is tolerated.
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

    assert default_merge_rejection(repo, clean, head, TOLERANCE) is None


class TestScriptedFidelity:
    def test_a_scripted_default_branch_merge_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tolerated(monkeypatch)
        assert default_merge_only(tmp_path, CLEAN, HEAD, TOLERANCE) is True

    def test_a_scripted_own_commit_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(non_merge_commit_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )
        assert (
            default_merge_rejection(tmp_path, CLEAN, HEAD, TOLERANCE)
            == "non_merge_commit own-com"
        )

    def test_the_scripted_argvs_are_the_ones_real_git_is_asked(
        self, wt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fidelity guard: the fake's keys equal the argvs issued on a real repo."""
        branch_that_merged_main(wt, tmp_path / "main-clone")
        merge = git_in(wt, "rev-parse", "HEAD~1")
        clean = git_in(wt, "rev-parse", f"{merge}^1")
        parent2 = git_in(wt, "rev-parse", f"{merge}^2")
        default_sha = git_in(wt, "rev-parse", "origin/main")
        recorded: list[tuple[str, ...]] = []

        def _run(argv: list[str], **kwargs: Any) -> Any:
            recorded.append(tuple(argv))
            return run_git(argv, **kwargs)

        monkeypatch.setattr(_merge_tolerance, "run_git", _run)
        monkeypatch.setattr(merge_probe, "run_git", _run)

        assert default_merge_only(wt, clean, merge, TOLERANCE) is True
        script = tolerated_merge_script(clean, merge, default_sha, parent2=parent2)
        assert set(recorded) == set(script)

    @pytest.mark.parametrize(
        ("key", "answer", "reason"),
        [
            pytest.param(
                ("rev-parse", "refs/remotes/origin/main^{commit}"),
                128,
                "no_default_ref",
                id="ref-rc128",
            ),
            pytest.param(
                ("rev-parse", "refs/remotes/origin/main^{commit}"),
                OSError("git vanished"),
                "no_default_ref",
                id="ref-oserror",
            ),
            pytest.param(
                (
                    "rev-list",
                    "--reverse",
                    "--first-parent",
                    "--parents",
                    "--merges",
                    SPAN,
                ),
                128,
                "git_unreadable",
                id="merge-listing",
            ),
            pytest.param(
                ("rev-list", "--reverse", "--no-merges", SPAN, f"^{DEFAULT}"),
                128,
                "git_unreadable",
                id="own-listing",
            ),
            pytest.param(
                ("merge-base", "--is-ancestor", PARENT2_SHA, DEFAULT),
                1,
                f"second_parent_not_on_default {HEAD[:7]}",
                id="parent2-not-on-default",
            ),
            pytest.param(
                (
                    "rev-list",
                    "--reverse",
                    "--first-parent",
                    "--parents",
                    "--merges",
                    SPAN,
                ),
                f"{HEAD} {CLEAN} {PARENT2_SHA} octopus-parent\n",
                f"not_two_parent {HEAD[:7]}",
                id="octopus",
            ),
            pytest.param(
                merge_tree_key(HEAD),
                129,
                f"merge_tree_unavailable {HEAD[:7]}",
                id="old-git",
            ),
            pytest.param(
                merge_tree_key(HEAD),
                f"{'b' * 40}\0",
                f"not_textually_clean {HEAD[:7]}",
                id="tree-differs",
            ),
            pytest.param(
                ("rev-parse", f"{HEAD}^{{tree}}"),
                128,
                f"not_textually_clean {HEAD[:7]}",
                id="own-tree-unreadable",
            ),
        ],
    )
    def test_each_scripted_refusal_is_named(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: tuple[str, ...],
        answer: str | int | BaseException,
        reason: str,
    ) -> None:
        script = tolerated_merge_script(CLEAN, HEAD, DEFAULT)
        script[key] = answer
        ScriptedMergeGit(script).install(monkeypatch)

        assert default_merge_rejection(tmp_path, CLEAN, HEAD, TOLERANCE) == reason

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


class TestGitStdoutIsBounded:
    def test_every_call_carries_the_probe_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[float | None] = []

        def _run(argv: list[str], *, timeout: float | None = None, **_kw: Any) -> Any:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "out\n", "")

        monkeypatch.setattr(_merge_tolerance, "run_git", _run)

        assert _merge_tolerance.git_stdout_or_none(tmp_path, ["rev-parse", "x"]) == (
            "out\n"
        )
        assert seen == [merge_probe.PROBE_TIMEOUT_SECONDS]

    def test_a_hung_git_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hang(argv: list[str], **_kw: Any) -> Any:
            raise subprocess.TimeoutExpired(["git", *argv], 10.0)

        monkeypatch.setattr(_merge_tolerance, "run_git", _hang)

        assert _merge_tolerance.git_stdout_or_none(tmp_path, ["rev-list", "x"]) is None
        assert _merge_tolerance.rev_parse_or_none(tmp_path, "x") is None
        assert default_merge_only(tmp_path, CLEAN, HEAD, TOLERANCE) is False
        assert default_merge_rejection(tmp_path, CLEAN, HEAD, TOLERANCE) == (
            "no_default_ref"
        )


class TestConvergedAcrossToleratedMerge:
    @pytest.mark.parametrize("backend", BACKENDS)
    def test_a_converged_ledger_stays_converged_on_an_overlapping_clean_merge(
        self, wt: Path, tmp_path: Path, backend: str
    ) -> None:
        """The rule is backend-independent (#2979): no claude-native allow-list."""
        found = branch_with_overlap_merge(wt, tmp_path / "scratch")
        ledger = _converged(sha=found.clean, loop_backend=backend)

        assert converged_across_tolerated_merge(
            ledger, worktree=wt, head_sha=found.head, tolerance=TOLERANCE
        )

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_a_conflict_resolution_merge_reopens(
        self, wt: Path, tmp_path: Path, backend: str
    ) -> None:
        found = branch_with_conflict_resolution_merge(wt, tmp_path / "scratch")
        ledger = _converged(sha=found.clean, loop_backend=backend)

        assert not converged_across_tolerated_merge(
            ledger, worktree=wt, head_sha=found.head, tolerance=TOLERANCE
        )

    @pytest.mark.parametrize(
        "ledger",
        [
            pytest.param(fresh_ledger(), id="no-clean-round"),
            pytest.param(demote_for_rereview(_converged()), id="reopened"),
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


class TestToleratesReviewMove:
    @pytest.mark.parametrize("backend", BACKENDS)
    def test_an_overlapping_clean_merge_is_tolerated_on_every_backend(
        self, wt: Path, tmp_path: Path, backend: str
    ) -> None:
        found = branch_with_overlap_merge(wt, tmp_path / "scratch")
        write_ledger(wt, _converged(sha=found.clean, loop_backend=backend))
        client = make_review_client(wt)

        assert tolerates_review_move(
            client, wt, reviewed_sha=found.clean, head_sha=found.head
        )

    def test_the_strict_opt_out_refuses_the_clean_merge(
        self, wt: Path, tmp_path: Path
    ) -> None:
        found = branch_with_overlap_merge(wt, tmp_path / "scratch")
        write_ledger(wt, _converged(sha=found.clean))
        client = make_review_client(wt, default_merge_tolerance=False)

        assert not tolerates_review_move(
            client, wt, reviewed_sha=found.clean, head_sha=found.head
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


class TestMergeOutcomes:
    """``classify_merge`` / ``merge_outcomes``: the shared per-merge checks (#2980)."""

    def test_a_resolution_stops_before_the_next_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merge 1 ``not_textually_clean``; merge 2 is never asked about."""
        second = "f" * 40
        script = tolerated_merge_script(CLEAN, HEAD, DEFAULT, merge="m" * 40)
        merges = ("rev-list", "--reverse", "--first-parent", "--parents", "--merges")
        script[(*merges, f"{CLEAN}..{HEAD}")] = (
            f"{'m' * 40} {CLEAN} {PARENT2_SHA}\n{second} {'m' * 40} a b\n"
        )
        script["rev-parse", f"{'m' * 40}^{{tree}}"] = f"{'b' * 40}\n"
        fake = ScriptedMergeGit(script).install(monkeypatch)

        reason = default_merge_rejection(tmp_path, CLEAN, HEAD, TOLERANCE)

        assert reason == f"not_textually_clean {'m' * 7}"
        assert not any(second in " ".join(call) for call in fake.calls)

    def test_the_budget_still_names_merge_tree_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = ScriptedMergeGit(tolerated_merge_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )
        ticks = iter([0.0, _merge_tolerance.TIP_CHECK_BUDGET_SECONDS])
        found = _merge_tolerance.checked_span(tmp_path, CLEAN, HEAD, "main")
        assert not isinstance(found, str)

        outcomes = list(
            _merge_tolerance.merge_outcomes(tmp_path, found, clock=lambda: next(ticks))
        )

        assert outcomes == [f"merge_tree_unavailable {HEAD[:7]}"]
        assert not any(call[0] == "merge-tree" for call in fake.calls)

    def test_a_clean_merge_is_merge_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(tolerated_merge_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )

        outcome = _merge_tolerance.classify_merge(
            tmp_path, HEAD, [PARENT2_SHA], DEFAULT
        )

        assert outcome == _merge_tolerance.MergeClean(HEAD)

    def test_a_real_conflict_is_a_resolution_with_both_trees(
        self, wt: Path, tmp_path: Path
    ) -> None:
        found = branch_with_conflict_resolution_merge(wt, tmp_path / "scratch")
        parent2 = git_in(wt, "rev-parse", f"{found.head}^2")
        default = git_in(wt, "rev-parse", "origin/main")

        outcome = _merge_tolerance.classify_merge(wt, found.head, [parent2], default)

        assert isinstance(outcome, _merge_tolerance.MergeResolution)
        assert outcome.status == "conflict"
        written = merge_probe.merge_tree(wt, f"{found.head}^1", f"{found.head}^2")
        assert outcome.conflicted_tree == written.tree
        assert outcome.resolved_tree == git_in(
            wt, "rev-parse", f"{found.head}^{{tree}}"
        )
        assert outcome.conflicted_tree != outcome.resolved_tree

    def test_checked_span_names_an_own_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ScriptedMergeGit(non_merge_commit_script(CLEAN, HEAD, DEFAULT)).install(
            monkeypatch
        )

        found = _merge_tolerance.checked_span(tmp_path, CLEAN, HEAD, "main")

        assert found == "non_merge_commit own-com"
