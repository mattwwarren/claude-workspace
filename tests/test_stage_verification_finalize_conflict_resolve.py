"""Tests for ``cw.stage_verification.finalize_conflict_resolve`` (#3542).

Real git with a bare origin (``fresh_finalize_repo``), the real
``classify_merge_conflict.py`` copied into the per-test HOME
(``install_real_guard_script``), and GitHub's mergeability from the shared
``gh`` fixture. The post-push re-read is scripted by wrapping the module's
``push_ahead``, as the #3861 repair tests do.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from cw.stage_verification import finalize_conflict_resolve as resolve
from cw.stage_verification.finalize_ci_evidence import CiContext
from cw.stage_verification.finalize_gates import (
    FinalizeGateRun,
    gate_scope,
    run_finalize_quality_gates,
)
from cw.stage_verification.finalize_result import (
    AGENT_BLOCK,
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    PUSH_AUTH_FAILED,
    FinalizeBlock,
)
from tests._codex_review_helpers import (
    fresh_finalize_repo,
    install_home_prep_pr_state,
    merge_view,
)
from tests._git_sandbox import run as git_run
from tests._shared.git_repos import (
    commit_tracked_file,
    dirty_tracked_file,
    git_in,
    push_commit_to_origin,
)
from tests._shared.scope_grant_builders import install_real_guard_script

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests._tool_fakes import FakeGhCli

_BRANCH = "dev/T-9"
_URL = "https://github.com/o/r/pull/9"
_DOC = "CHANGELOG-notes.md"
_PARK = FinalizeBlock(
    MERGE_CONFLICT_POST_PUSH,
    f"{_URL} is not mergeable after the push (mergeable=CONFLICTING, "
    "mergeStateStatus=DIRTY)",
    retry_eligible=True,
)
_MERGEABLE = merge_view("MERGEABLE", "CLEAN")


class Repo:
    """A FINALIZE worktree on a bare origin plus the side clone that races it."""

    def __init__(self, worktree: Path, origin: Path, side: Path) -> None:
        self.worktree = worktree
        self.origin = origin
        self.side = side

    def head(self) -> str:
        return git_in(self.worktree, "rev-parse", "HEAD")

    def origin_tip(self) -> str:
        return git_in(self.worktree, "ls-remote", "origin", f"refs/heads/{_BRANCH}")[
            :40
        ]

    def mid_merge(self) -> bool:
        done = git_run(
            "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=self.worktree, check=False
        )
        return done.returncode == 0

    def ours_and_theirs(self, name: str, ours: str, theirs: str) -> None:
        """Commit *name* on the branch and push it; push a rival to origin/main."""
        commit_tracked_file(self.worktree, name, ours)
        git_in(self.worktree, "push", "origin", _BRANCH)
        push_commit_to_origin(self.origin, "main", self.side, name, theirs)

    def base_adds(self, name: str, content: str = "base\n") -> None:
        push_commit_to_origin(self.origin, "main", self.side, name, content)

    def hook(self, name: str, body: str = "exit 1") -> None:
        hooks = self.worktree / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        path = hooks / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)


@pytest.fixture
def repo(make_git_repo: Callable[..., Path], tmp_path: Path) -> Repo:
    worktree, origin = fresh_finalize_repo(make_git_repo, "resolve-wt", _BRANCH)
    exclude = worktree / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".cw/\n", encoding="utf-8")
    return Repo(worktree, origin, tmp_path / "side")


def _never_gates() -> FinalizeGateRun:
    msg = "the gates must not run"
    raise AssertionError(msg)


def _green() -> FinalizeGateRun:
    return FinalizeGateRun(None, ["resolve_path_gates: passed"])


def _resolve(
    repo: Repo,
    *,
    run_gates: Callable[[], FinalizeGateRun] = _green,
    ignore: tuple[str, ...] = (),
    base: str = "main",
    head: str | None = None,
) -> resolve.ResolveOutcome:
    return resolve.resolve_post_push_conflict(
        repo.worktree,
        park=_PARK,
        base_branch=base,
        branch=_BRANCH,
        head_sha=head if head is not None else repo.head(),
        pr_url=_URL,
        ignore_paths=ignore,
        run_gates=run_gates,
    )


def _reads_after_push(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGhCli, view: str | None
) -> None:
    real_push = resolve.push_ahead

    def _push_then_read(worktree: Path, *, branch: str, head_sha: str) -> object:
        block = real_push(worktree, branch=branch, head_sha=head_sha)
        if block is None:
            if view is None:
                gh.reply("pr", "view", _URL, returncode=1)
            else:
                gh.reply("pr", "view", _URL, stdout=view)
        return block

    monkeypatch.setattr(resolve, "push_ahead", _push_then_read)


def _parents(repo: Repo) -> list[str]:
    return git_in(repo.worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]


# -- the happy paths ---------------------------------------------------------


def test_a_clean_merge_is_pushed_without_gates(
    repo: Repo, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.base_adds("other.py")
    _reads_after_push(monkeypatch, gh, _MERGEABLE)
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is None
    assert outcome.friction == []
    assert outcome.head == repo.head()
    assert _parents(repo)[0] == pre
    assert repo.origin_tip() == repo.head()
    assert not (repo.worktree / resolve.CONFLICTED_FILES_RELATIVE).exists()


def test_a_safe_conflict_is_resolved_gated_pushed_and_confirmed(
    repo: Repo, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    _reads_after_push(monkeypatch, gh, _MERGEABLE)
    pre = repo.head()
    gate_calls: list[str] = []

    def _gates() -> FinalizeGateRun:
        gate_calls.append(repo.head())
        return _green()

    outcome = _resolve(repo, run_gates=_gates)
    assert outcome.block is None
    assert outcome.friction == [resolve.SEMANTIC_RESOLVED]
    assert outcome.head == repo.head() == repo.origin_tip()
    assert gate_calls == [repo.head()]
    assert len(_parents(repo)) == 2
    assert _parents(repo)[0] == pre
    body = git_in(repo.worktree, "log", "-1", "--format=%B")
    assert body.startswith("Merge origin/main (semantic auto-resolve: doc_append=1)")
    assert resolve.AUTO_RESOLVED_TRAILER in body
    text = (repo.worktree / _DOC).read_text(encoding="utf-8")
    assert "ours" in text
    assert "theirs" in text
    listed = (repo.worktree / resolve.CONFLICTED_FILES_RELATIVE).read_text()
    assert listed == f"{_DOC}\n"


@pytest.mark.parametrize(
    ("classifier", "token"),
    [(True, resolve.SEMANTIC_UNVERIFIED), (False, resolve.POST_PUSH_UNVERIFIED)],
)
def test_an_unknown_recheck_proceeds_unverified(
    repo: Repo,
    gh: FakeGhCli,
    monkeypatch: pytest.MonkeyPatch,
    classifier: bool,
    token: str,
) -> None:
    if classifier:
        install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
        repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    else:
        repo.base_adds("other.py")
    _reads_after_push(monkeypatch, gh, merge_view("UNKNOWN", "UNKNOWN"))
    outcome = _resolve(repo)
    assert outcome.block is None
    assert outcome.friction == [token]
    assert resolve.SEMANTIC_RESOLVED not in outcome.friction


# -- refusals before or at the classifier -------------------------------------


def _assert_untouched(repo: Repo, pre: str, origin_tip: str) -> None:
    assert repo.head() == pre
    assert not repo.mid_merge()
    assert repo.origin_tip() == origin_tip
    assert git_in(repo.worktree, "status", "--porcelain", "--untracked-files=no") == ""


def test_an_unsafe_conflict_is_refused_and_aborted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs("clash.py", "mine = 1\n", "theirs = 2\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_never_gates)
    block = outcome.block
    assert block is not None
    assert block.reason == MERGE_CONFLICT_POST_PUSH
    assert block.retry_eligible is True
    assert block.details.startswith(f"{_PARK.details}; semantic auto-resolve attempted")
    assert "— refused: " in block.details
    assert block.details.endswith("; conflicted files: clash.py")
    assert outcome.head is None
    _assert_untouched(repo, pre, tip)


def test_an_absent_classifier_parks_with_its_friction(repo: Repo) -> None:
    repo.ours_and_theirs("clash.py", "mine = 1\n", "theirs = 2\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert (
        "semantic auto-resolve not attempted: classify_merge_conflict.py is absent"
        in outcome.block.details
    )
    assert outcome.friction == [resolve.CLASSIFIER_ABSENT_FRICTION]
    _assert_untouched(repo, pre, tip)


def test_a_stale_classifier_marker_is_agent_block(repo: Repo) -> None:
    script = install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            "# cw-script-version: 1", "# cw-script-version: 0"
        ),
        encoding="utf-8",
    )
    repo.ours_and_theirs("clash.py", "mine = 1\n", "theirs = 2\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.reason == AGENT_BLOCK
    assert outcome.block.details == (
        f"Step 4c.5: HEADLESS BLOCK — classify_merge_conflict.py at {script} — "
        "missing/stale cw-script-version marker (need >= 1)"
    )
    _assert_untouched(repo, pre, tip)


def test_ignore_paths_match_exactly_not_as_globs(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    outcome = _resolve(repo, run_gates=_red, ignore=("*.md",))
    assert outcome.block is not None
    assert "must be regenerated" not in outcome.block.details
    assert "resolved conflicts (doc_append=1)" in outcome.block.details


def test_a_conflict_in_an_ignored_path_is_never_resolved(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs("uv.lock", "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_never_gates, ignore=("other.lock", "uv.lock"))
    assert outcome.block is not None
    assert (
        "semantic auto-resolve not attempted: uv.lock must be regenerated, not "
        "hand-resolved" in outcome.block.details
    )
    assert outcome.friction == []
    _assert_untouched(repo, pre, tip)


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (OSError("no interpreter"), "— did not finish: no interpreter"),
        (subprocess.TimeoutExpired("python", 120), "— did not finish: "),
        (None, "— unreadable result: not json"),
    ],
)
def test_a_classifier_that_cannot_answer_aborts_and_parks(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException | None,
    fragment: str,
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    real_run = subprocess.run
    seen: list[list[str]] = []

    def _classifier(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and resolve.CLASSIFIER_SCRIPT_NAME in str(argv[1:2]):
            seen.append(list(argv))
            assert kwargs["cwd"] == repo.worktree
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, 0, "not json", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(resolve.subprocess, "run", _classifier)
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert fragment in outcome.block.details
    assert seen[0][2:] == [
        "resolve",
        "--conflicted-files",
        str(repo.worktree / resolve.CONFLICTED_FILES_RELATIVE),
        "--json",
    ]
    _assert_untouched(repo, pre, tip)


# -- preconditions -----------------------------------------------------------


def test_a_failed_fetch_is_not_attempted(repo: Repo) -> None:
    outcome = _resolve(repo, run_gates=_never_gates, base="no-such-base")
    assert outcome.block is not None
    assert outcome.block.details.startswith(
        f"{_PARK.details}; semantic auto-resolve not attempted: git fetch origin "
        "no-such-base failed"
    )


def test_a_dirty_tracked_tree_is_not_attempted(repo: Repo) -> None:
    dirty_tracked_file(repo.worktree, "tol.py")
    repo.base_adds("other.py")
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert "not attempted: the worktree has tracked modifications" in (
        outcome.block.details
    )
    assert not repo.mid_merge()


def test_a_moved_head_is_not_attempted(repo: Repo) -> None:
    outcome = _resolve(repo, run_gates=_never_gates, head="b" * 40)
    assert outcome.block is not None
    assert f"is not the PR head {'b' * 40}" in outcome.block.details


def test_an_ignored_local_file_base_adds_blocks_the_merge(repo: Repo) -> None:
    (repo.worktree / ".gitignore").write_text("local.env\n", encoding="utf-8")
    git_in(repo.worktree, "add", ".gitignore")
    git_in(repo.worktree, "commit", "-m", "ignore")
    git_in(repo.worktree, "push", "origin", _BRANCH)
    (repo.worktree / "local.env").write_text("mine\n", encoding="utf-8")
    repo.base_adds("local.env", "theirs\n")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert "merging origin/main would write over local.env," in outcome.block.details
    assert (repo.worktree / "local.env").read_text(encoding="utf-8") == "mine\n"
    assert repo.head() == pre


# -- the merge itself fails ----------------------------------------------------


def test_a_hook_rejecting_a_clean_merge_is_aborted(repo: Repo) -> None:
    repo.base_adds("other.py")
    repo.hook("pre-merge-commit")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert (
        "semantic auto-resolve not attempted: git merge failed with no conflicted "
        "files: " in outcome.block.details
    )
    assert "; conflicted files:" not in outcome.block.details
    assert not repo.mid_merge()
    assert repo.head() == pre
    assert not (repo.worktree / resolve.CONFLICTED_FILES_RELATIVE).exists()


def test_an_untracked_path_the_merge_would_overwrite_is_not_aborted(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file appears after the guard looked, so git itself refuses the merge.
    monkeypatch.setattr(resolve, "_untracked_clobbers", lambda *_a: [])
    repo.base_adds("unrelated.py")
    (repo.side / "pkg").mkdir()
    repo.base_adds("pkg/mod.py")
    (repo.worktree / "pkg").write_text("a local file where base has a dir\n")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert "git merge failed with no conflicted files" in outcome.block.details
    assert not repo.mid_merge()
    assert repo.head() == pre


def test_a_failed_abort_is_named(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.base_adds("other.py")
    repo.hook("pre-merge-commit")
    real_git = resolve._git

    def _no_abort(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args == ["merge", "--abort"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: cannot abort")
        return real_git(worktree, args)

    monkeypatch.setattr(resolve, "_git", _no_abort)
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.details.endswith(
        "; git merge --abort failed (fatal: cannot abort): the worktree is "
        'mid-merge, run "git merge --abort" by hand'
    )
    assert repo.mid_merge()


def test_a_hook_rejected_resolution_commit_is_aborted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    repo.hook("pre-commit", "echo hook-says-no >&2; exit 1")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert "semantic auto-resolve attempted — commit failed: " in outcome.block.details
    assert "hook-says-no" in outcome.block.details
    _assert_untouched(repo, pre, tip)


# -- gates and the revert ------------------------------------------------------


def _red(gate: str = "lint", output: str = "boom-tail") -> FinalizeGateRun:
    return FinalizeGateRun(
        FinalizeBlock(AGENT_BLOCK, "red"), failed_gate=gate, failed_output=output
    )


def test_a_red_gate_after_the_merge_is_reverted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(repo, run_gates=_red)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert outcome.block.retry_eligible is True
    assert outcome.block.details == (
        f"{_PARK.details}; semantic auto-resolve resolved conflicts (doc_append=1)"
        f" but gate lint failed: boom-tail; reverted; conflicted files: {_DOC}"
    )
    assert outcome.head is None
    _assert_untouched(repo, pre, tip)


def test_gates_that_could_not_run_are_named_and_reverted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    outcome = _resolve(
        repo, run_gates=lambda: FinalizeGateRun(FinalizeBlock(AGENT_BLOCK, "no sh"))
    )
    assert outcome.block is not None
    assert "but the quality gates could not run: no sh; reverted" in (
        outcome.block.details
    )
    _assert_untouched(repo, pre, tip)


def test_an_untracked_tolerated_file_survives_the_revert(
    repo: Repo, tmp_path: Path
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    (repo.worktree / "tol.txt").write_text("operator\n", encoding="utf-8")
    install_home_prep_pr_state(
        tmp_path / "state", ("lint", "echo gate-red-xyz; exit 3", 5.0)
    )
    pre = repo.head()

    def _real_gates() -> FinalizeGateRun:
        return run_finalize_quality_gates(
            repo.worktree,
            scope=gate_scope("T-9", "acme", "s-1"),
            path="resolve",
            ci=CiContext(base="main", head=pre),
            tolerated=frozenset({"tol.txt"}),
        )

    outcome = _resolve(repo, run_gates=_real_gates)
    assert outcome.block is not None
    assert "gate lint failed: " in outcome.block.details
    assert "gate-red-xyz" in outcome.block.details
    assert "; reverted" in outcome.block.details
    assert repo.head() == pre
    assert (repo.worktree / "tol.txt").read_text(encoding="utf-8") == "operator\n"


def test_a_gate_that_moved_head_is_not_reverted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre = repo.head()

    merged: list[str] = []

    def _moving_gate() -> FinalizeGateRun:
        merged.append(repo.head())
        commit_tracked_file(repo.worktree, "gate-wrote.py")
        return _red()

    outcome = _resolve(repo, run_gates=_moving_gate)
    moved = repo.head()
    assert moved != pre
    block = outcome.block
    assert block is not None
    assert block.retry_eligible is False
    assert "; reverted" not in block.details
    assert f"NOT reverted (HEAD^1 is not the pre-merge commit {pre})" in block.details
    assert moved != merged[0]
    assert (
        f"the merge commit {merged[0]} is left in the local worktree only "
        f"(HEAD is now {moved})" in block.details
    )
    assert (
        f"Confirm {_BRANCH} is checked out in {repo.worktree} and inspect it"
        in block.details
    )
    assert f'run "git -C {repo.worktree} reset --hard {pre}" by hand' in block.details
    assert "commits above the merge would be lost" in block.details


def test_a_failed_reset_is_not_reverted(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre = repo.head()
    real_git = resolve._git

    def _no_reset(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["reset", "--hard"]:
            return subprocess.CompletedProcess(args, 1, "", "reset-refused")
        return real_git(worktree, args)

    monkeypatch.setattr(resolve, "_git", _no_reset)
    outcome = _resolve(repo, run_gates=_red)
    block = outcome.block
    assert block is not None
    assert block.retry_eligible is False
    assert f"git reset --hard {pre} failed: reset-refused" in block.details
    assert "; reverted" not in block.details
    assert repo.head() != pre


def test_a_branch_switch_is_not_reverted(repo: Repo) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")

    merged: list[str] = []

    def _switching_gate() -> FinalizeGateRun:
        merged.append(repo.head())
        git_in(repo.worktree, "checkout", "-q", "-b", "elsewhere", "HEAD^1")
        return _red()

    outcome = _resolve(repo, run_gates=_switching_gate)
    assert outcome.block is not None
    assert f"no longer has {_BRANCH!r} checked out" in outcome.block.details
    assert outcome.block.retry_eligible is False
    assert f"the merge commit {merged[0]} is left" in outcome.block.details
    assert f"(HEAD is now {repo.head()})" in outcome.block.details


# -- push and re-check -----------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "classifier", "site", "merge"),
    [
        (
            PUSH_AUTH_FAILED,
            True,
            "semantic-resolve push",
            "semantic auto-resolve merge",
        ),
        (IMPL_NOT_PUSHED, False, "merge push", "origin/main merge"),
    ],
)
def test_a_push_failure_keeps_its_reason_and_names_the_local_merge(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    classifier: bool,
    site: str,
    merge: str,
) -> None:
    if classifier:
        install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
        repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    else:
        repo.base_adds("other.py")
    pushed = FinalizeBlock(reason, "git push failed: denied", retry_eligible=True)
    monkeypatch.setattr(resolve, "push_ahead", lambda *_a, **_k: pushed)
    outcome = _resolve(repo)
    block = outcome.block
    assert block is not None
    assert block.reason == reason
    assert block.retry_eligible is False
    assert block.details.startswith(f"Step 4c.5 {site}: git push failed: denied; ")
    assert f"the {merge} commit {repo.head()} exists only in the local" in (
        block.details
    )
    assert outcome.head is None


def test_still_dirty_after_the_push_parks_with_the_merge_head(
    repo: Repo, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    _reads_after_push(monkeypatch, gh, merge_view("CONFLICTING", "DIRTY"))
    outcome = _resolve(repo)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert outcome.block.details.endswith(
        "; still not mergeable after one merge of origin/main; conflicted files: "
        f"{_DOC}"
    )
    assert outcome.head == repo.head() == repo.origin_tip()


def test_an_unreadable_recheck_parks_operator_unavailable(
    repo: Repo, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.base_adds("other.py")
    _reads_after_push(monkeypatch, gh, None)
    outcome = _resolve(repo)
    assert outcome.block is not None
    assert outcome.block.reason == OPERATOR_UNAVAILABLE
    assert outcome.head == repo.head()
    assert outcome.friction == []


# -- helper edges ----------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        '{"safe": false}',
        '{"safe": true, "resolved_files": [], "categories": {}}',
        '{"safe": true, "resolved_files": ["a"], "categories": []}',
        '{"safe": true, "resolved_files": [""], "categories": {}}',
        '{"safe": true, "resolved_files": [3], "categories": {}}',
    ],
)
def test_an_unusable_classifier_result_is_unreadable(stdout: str) -> None:
    assert resolve._parse_resolved(stdout) is None


def test_a_readable_classifier_result() -> None:
    parsed = resolve._parse_resolved(
        '{"safe": true, "resolved_files": ["a.md"], '
        '"categories": {"doc_append": 1, "import_union": 2}}'
    )
    assert parsed == resolve._Resolved(("a.md",), "doc_append=1, import_union=2")


def test_an_unreadable_head_is_not_attempted(tmp_path: Path) -> None:
    assert resolve._local_head(tmp_path / "missing") == ""


def test_an_unlistable_base_diff_is_not_attempted(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.base_adds("other.py")
    real = resolve.git_output

    def _no_diff(args: list[str], **kwargs: Any) -> str:
        if args[0] == "diff":
            raise subprocess.CalledProcessError(128, "git diff")
        return real(args, **kwargs)

    monkeypatch.setattr(resolve, "git_output", _no_diff)
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert "not attempted: the paths origin/main changes could not be listed" in (
        outcome.block.details
    )


@pytest.mark.parametrize("failure", ["add", "unmerged"])
def test_a_staging_failure_aborts_before_the_commit(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    real_git = resolve._git

    def _no_add(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "add":
            return subprocess.CompletedProcess(args, 1, "", "add-refused")
        return real_git(worktree, args)

    if failure == "add":
        monkeypatch.setattr(resolve, "_git", _no_add)
        fragment = "commit failed: add-refused"
    else:
        real_unmerged = resolve.unmerged_paths
        calls: list[int] = []

        def _ghost(worktree: Path) -> tuple[str, ...]:
            calls.append(1)
            return real_unmerged(worktree) if len(calls) == 1 else ("ghost.md",)

        monkeypatch.setattr(resolve, "unmerged_paths", _ghost)
        fragment = "commit failed: still unmerged: ghost.md"
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert fragment in outcome.block.details
    _assert_untouched(repo, pre, tip)


# -- hook-running calls --------------------------------------------------------


def test_the_hook_running_calls_have_no_kill_timer(
    repo: Repo, gh: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    _reads_after_push(monkeypatch, gh, _MERGEABLE)
    real_run_git = resolve.run_git
    seen: dict[str, object] = {}

    def _record(argv: list[str], **kwargs: Any) -> Any:
        seen[argv[0]] = kwargs.get("timeout")
        return real_run_git(argv, **kwargs)

    monkeypatch.setattr(resolve, "run_git", _record)
    assert _resolve(repo).block is None
    assert seen["merge"] is None
    assert seen["commit"] is None
    assert seen["add"] is not None


def _raise_after(
    monkeypatch: pytest.MonkeyPatch, matches: Callable[[list[str]], bool]
) -> None:
    """Make the matching ``run_git`` call run, then die as a killed child does."""
    real_run_git = resolve.run_git

    def _die(argv: list[str], **kwargs: Any) -> Any:
        if not matches(argv):
            return real_run_git(argv, **kwargs)
        if argv[0] != "commit":
            real_run_git(argv, **kwargs)
        raise subprocess.TimeoutExpired(["git", *argv], 60)

    monkeypatch.setattr(resolve, "run_git", _die)


def test_a_commit_that_raises_aborts_and_parks(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    _raise_after(monkeypatch, lambda argv: argv[0] == "commit")
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert "semantic auto-resolve attempted — commit failed: " in outcome.block.details
    _assert_untouched(repo, pre, tip)


def test_a_merge_that_raises_aborts_and_parks(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    repo.ours_and_theirs(_DOC, "ours\n", "theirs\n")
    pre, tip = repo.head(), repo.origin_tip()
    _raise_after(monkeypatch, lambda argv: argv[0] == "merge" and "--abort" not in argv)
    outcome = _resolve(repo, run_gates=_never_gates)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert "git merge did not finish: " in outcome.block.details
    _assert_untouched(repo, pre, tip)


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("add", "commit failed: "),
        ("--abort", "git merge --abort failed ("),
        ("reset", "NOT reverted ("),
    ],
)
def test_a_git_call_that_raises_is_parked_not_raised(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, command: str, fragment: str
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    name = "clash.py" if command == "--abort" else _DOC
    repo.ours_and_theirs(name, "ours = 1\n", "theirs = 2\n")
    real_run_git = resolve.run_git

    def _broken(argv: list[str], **kwargs: Any) -> Any:
        if command in argv[:2]:
            msg = "git vanished"
            raise OSError(msg)
        return real_run_git(argv, **kwargs)

    monkeypatch.setattr(resolve, "run_git", _broken)
    outcome = _resolve(repo, run_gates=_red)
    assert outcome.block is not None
    assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
    assert fragment in outcome.block.details
    assert "git vanished" in outcome.block.details


# -- the clobber guard ---------------------------------------------------------


def _ignore_and_push(repo: Repo, pattern: str) -> None:
    commit_tracked_file(repo.worktree, ".gitignore", f"{pattern}\n")
    git_in(repo.worktree, "push", "origin", _BRANCH)


def _assert_refused(repo: Repo, outcome: resolve.ResolveOutcome, name: str) -> None:
    assert outcome.block is not None
    assert (
        f"not attempted: merging origin/main would write over {name}, untracked or "
        "ignored in the worktree" in outcome.block.details
    )
    assert not repo.mid_merge()


def test_a_locally_untracked_file_base_modifies_blocks_the_merge(repo: Repo) -> None:
    commit_tracked_file(repo.worktree, "local.env", "shared\n")
    git_in(repo.worktree, "push", "origin", f"{_BRANCH}:main")
    git_in(repo.worktree, "rm", "--cached", "local.env")
    git_in(repo.worktree, "commit", "-m", "untrack")
    _ignore_and_push(repo, "local.env")
    (repo.worktree / "local.env").write_text("mine\n", encoding="utf-8")
    repo.base_adds("local.env", "theirs\n")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    _assert_refused(repo, outcome, "local.env")
    assert (repo.worktree / "local.env").read_text(encoding="utf-8") == "mine\n"
    assert repo.head() == pre


def test_a_non_ascii_ignored_file_base_adds_blocks_the_merge(repo: Repo) -> None:
    name = "café.env"
    _ignore_and_push(repo, "*.env")
    (repo.worktree / name).write_text("mine\n", encoding="utf-8")
    repo.base_adds(name, "theirs\n")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    _assert_refused(repo, outcome, name)
    assert (repo.worktree / name).read_text(encoding="utf-8") == "mine\n"
    assert repo.head() == pre


def test_an_ignored_file_where_base_needs_a_directory_blocks_the_merge(
    repo: Repo,
) -> None:
    _ignore_and_push(repo, "foo")
    (repo.worktree / "foo").write_text("mine\n", encoding="utf-8")
    repo.base_adds("other.py")
    (repo.side / "foo").mkdir()
    repo.base_adds("foo/bar", "theirs\n")
    pre = repo.head()
    outcome = _resolve(repo, run_gates=_never_gates)
    _assert_refused(repo, outcome, "foo")
    assert (repo.worktree / "foo").read_text(encoding="utf-8") == "mine\n"
    assert repo.head() == pre


@pytest.mark.skipif(sys.platform == "darwin", reason="APFS rejects non-UTF-8")
def test_a_non_utf8_path_is_listed_and_escaped(repo: Repo) -> None:
    name = os.fsdecode(b"\xff.env")
    _ignore_and_push(repo, "*.env")
    (repo.worktree / name).write_text("mine\n", encoding="utf-8")
    repo.base_adds(name, "theirs\n")
    outcome = _resolve(repo, run_gates=_never_gates)
    _assert_refused(repo, outcome, "\\udcff.env")
    assert outcome.block is not None
    outcome.block.details.encode("utf-8")
