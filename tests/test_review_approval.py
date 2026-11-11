"""Tests for the shared large-tier review-approval predicate (#2773).

Real temp git repos: the predicate runs ``rev-parse``, ``merge-base`` and
``rev-list`` against them. The matrix lived in ``test_repair_fence.py`` as the
repair port of Step 4.0 (#2771); it is here once, because the repair path, the
``cw review check-approval`` CLI and the dispatch claim gate all call this one
function.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw import review_approval as ra
from cw.review_approval import (
    APPROVAL_CHANNEL_ABSENT,
    APPROVAL_MISSING,
    APPROVAL_NOT_REQUIRED,
    APPROVAL_RECORDED,
    APPROVAL_STALE,
    APPROVAL_UNREADABLE,
    APPROVAL_UNRESOLVED_TIER,
    ApprovalInputs,
    ApprovalVerdict,
    CommitLookup,
    approval_inputs_from_task,
    evaluate_review_approval,
    render_verdict_line,
    resolve_commit,
    resolve_row_scope_tier,
)
from tests._review_approval_helpers import (
    ApprovalRepo,
    advance_main,
    head,
    make_approval_repo,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests.test_reconcile_gate_recipes import _make_task

if TYPE_CHECKING:
    from collections.abc import Callable


_NO_GIT = "no git"


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> ApprovalRepo:
    return make_approval_repo(make_git_repo_with_origin, "wt-predicate")


def _inputs(**fields: object) -> ApprovalInputs:
    base = ApprovalInputs(
        tier="large", approver_kind="operator", approved_sha=None, default_branch="main"
    )
    return base._replace(**fields)


def _evaluate(
    repo: ApprovalRepo, inputs: ApprovalInputs, **kwargs: Any
) -> ApprovalVerdict:
    kwargs.setdefault("head_sha", head(repo.worktree))
    return evaluate_review_approval(inputs, repo=repo.worktree, **kwargs)


# --- tier resolution --------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_hint", "computed", "expected"),
    [
        ("large", "small", "large"),
        ("small", "large", "large"),
        (None, "small", "small"),
        ("small", None, "small"),
        (None, None, None),
        ("medium", None, None),
        (None, "Small", None),
    ],
)
def test_resolve_row_tier_precedence(
    scope_hint: str | None, computed: str | None, expected: str | None
) -> None:
    task = _make_task(scope_hint=scope_hint, computed_scope_tier=computed)

    assert resolve_row_scope_tier(task) == expected


def test_approval_inputs_from_task_reads_the_row() -> None:
    task = _make_task(
        computed_scope_tier="large",
        review_approver_kind="recipe",
        review_approved_sha="abc1234",
    )

    inputs = approval_inputs_from_task(task, "trunk")

    assert inputs == ApprovalInputs("large", "recipe", "abc1234", "trunk")


def test_approval_inputs_from_task_without_approval() -> None:
    inputs = approval_inputs_from_task(_make_task(scope_hint="large"), "main")

    assert inputs == ApprovalInputs("large", None, None, "main")


# --- the predicate ----------------------------------------------------------


def test_small_tier_is_not_required(repo: ApprovalRepo) -> None:
    verdict = _evaluate(repo, _inputs(tier="small"), head_sha=None)

    assert verdict.token == APPROVAL_NOT_REQUIRED
    assert verdict.passed is True
    assert verdict.transient is False


@pytest.mark.parametrize("tier", [None, "", "medium", "Large", "SMALL"])
def test_unresolved_tier_parks(repo: ApprovalRepo, tier: str | None) -> None:
    verdict = _evaluate(repo, _inputs(tier=tier))

    assert verdict.token == APPROVAL_UNRESOLVED_TIER
    assert verdict.passed is False


@pytest.mark.parametrize("kind", [None, "human", ""])
def test_large_without_approval_is_missing(
    repo: ApprovalRepo, kind: str | None
) -> None:
    verdict = _evaluate(repo, _inputs(approver_kind=kind, approved_sha=repo.approved))

    assert verdict.token == APPROVAL_MISSING
    assert verdict.passed is False
    assert verdict.transient is False


def test_large_with_approval_at_head_passes(repo: ApprovalRepo) -> None:
    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved[:12]))

    assert verdict.token == APPROVAL_RECORDED
    assert verdict.passed is True


def test_large_with_only_default_branch_merge_after_approval_passes(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
    git_in(repo.worktree, "merge", "--no-edit", "origin/main")

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_RECORDED


@pytest.mark.parametrize("sha", ["not-hex", "abc", "Z" * 40, "", None])
def test_unusable_reviewed_sha_is_stale(repo: ApprovalRepo, sha: str | None) -> None:
    verdict = _evaluate(repo, _inputs(approved_sha=sha))

    assert verdict.token == APPROVAL_STALE
    assert verdict.transient is False


def test_unresolvable_reviewed_sha_is_stale(repo: ApprovalRepo) -> None:
    verdict = _evaluate(repo, _inputs(approved_sha="0" * 40))

    assert verdict.token == APPROVAL_STALE
    assert "is not a commit" in verdict.detail
    assert verdict.transient is False


def test_non_ancestor_reviewed_sha_is_stale(repo: ApprovalRepo, tmp_path: Path) -> None:
    main_sha = advance_main(repo.origin, repo.worktree, tmp_path / "side", "m.py")

    verdict = _evaluate(repo, _inputs(approved_sha=main_sha))

    assert verdict.token == APPROVAL_STALE
    assert "not an ancestor" in verdict.detail
    assert verdict.transient is False


def test_new_non_merge_work_after_approval_is_stale(repo: ApprovalRepo) -> None:
    commit_tracked_file(repo.worktree, "more.py")

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_STALE
    assert "non-merge commit" in verdict.detail


def test_merge_bringing_in_a_non_default_parent_is_stale(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    # The side tip is a merge commit (no non-merge new work for the walk to
    # see) that is itself not on origin/main, so only the parent check fires.
    wt = repo.worktree
    main_tip = advance_main(repo.origin, wt, tmp_path / "side", "main_only.py")
    git_in(wt, "checkout", "-b", "side", f"{main_tip}^")
    git_in(wt, "merge", "--no-ff", "--no-edit", main_tip)
    side_tip = git_in(wt, "rev-parse", "HEAD")
    git_in(wt, "checkout", "feature")
    git_in(wt, "merge", "--no-ff", "--no-edit", "side")

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_STALE
    assert f"brought in {side_tip}" in verdict.detail


def test_octopus_merge_with_a_foreign_parent_is_stale(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    wt = repo.worktree
    main_tip = advance_main(repo.origin, wt, tmp_path / "side", "main_only.py")
    git_in(wt, "checkout", "-b", "foreign", repo.approved)
    commit_tracked_file(wt, "foreign.py")
    foreign = git_in(wt, "rev-parse", "HEAD")
    git_in(wt, "checkout", "feature")
    tree = git_in(wt, "rev-parse", f"{repo.approved}^{{tree}}")
    octopus = git_in(
        wt,
        "commit-tree",
        tree,
        "-p",
        repo.approved,
        "-p",
        main_tip,
        "-p",
        foreign,
        "-m",
        "octopus",
    )
    git_in(wt, "reset", "--hard", octopus)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_STALE
    assert foreign in verdict.detail


def test_history_rewrite_is_stale(repo: ApprovalRepo, tmp_path: Path) -> None:
    advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
    git_in(repo.worktree, "rebase", "origin/main")

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_STALE


def test_reviewed_head_ahead_of_head_is_stale(repo: ApprovalRepo) -> None:
    ahead_of = head(repo.worktree)
    commit_tracked_file(repo.worktree, "later.py")
    later = head(repo.worktree)
    git_in(repo.worktree, "reset", "--hard", ahead_of)

    verdict = _evaluate(repo, _inputs(approved_sha=later))

    assert verdict.token == APPROVAL_STALE


def test_missing_default_branch_ref_is_unreadable(repo: ApprovalRepo) -> None:
    verdict = _evaluate(
        repo, _inputs(approved_sha=repo.approved, default_branch="nope")
    )

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is False


def test_missing_repo_dir_is_unreadable(tmp_path: Path) -> None:
    verdict = evaluate_review_approval(
        _inputs(approved_sha="a" * 40),
        repo=tmp_path / "gone",
        head_sha="b" * 40,
    )

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


def test_head_sha_none_is_unreadable_and_not_transient(repo: ApprovalRepo) -> None:
    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved), head_sha=None)

    assert verdict.token == APPROVAL_UNREADABLE
    assert "HEAD is not readable" in verdict.detail
    assert verdict.transient is False


@pytest.mark.parametrize("default_branch", [None, ""])
def test_default_branch_none_is_unreadable(
    repo: ApprovalRepo, default_branch: str | None
) -> None:
    verdict = _evaluate(
        repo, _inputs(approved_sha=repo.approved, default_branch=default_branch)
    )

    assert verdict.token == APPROVAL_UNREADABLE
    assert "origin_main_branch is missing or invalid" in verdict.detail
    assert verdict.transient is False


def test_check_order_is_tier_kind_head_sha_default_branch(
    repo: ApprovalRepo,
) -> None:
    broken = _inputs(approver_kind=None, approved_sha="zz", default_branch=None)

    # kind precedes head: a missing approval is `missing` even with no HEAD.
    assert _evaluate(repo, broken, head_sha=None).token == APPROVAL_MISSING
    # head precedes the sha format check.
    ok_kind = broken._replace(approver_kind="operator")
    assert _evaluate(repo, ok_kind, head_sha=None).token == APPROVAL_UNREADABLE
    # sha format precedes the default branch check.
    assert _evaluate(repo, ok_kind).token == APPROVAL_STALE
    # default branch precedes the origin ref read.
    good_sha = ok_kind._replace(approved_sha=repo.approved)
    assert "origin_main_branch" in _evaluate(repo, good_sha).detail


def test_timeout_is_forwarded_to_run_git(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = ra.run_git

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _spy)

    _evaluate(repo, _inputs(approved_sha=repo.approved), timeout=7.5)

    assert seen
    assert set(seen) == {7.5}


def test_default_timeout_is_the_shared_local_constant(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw._git import LOCAL_GIT_TIMEOUT_SECONDS

    seen: list[object] = []
    real = ra.run_git

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _spy)

    _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert set(seen) == {LOCAL_GIT_TIMEOUT_SECONDS}


# --- transient failures -----------------------------------------------------


def test_git_timeout_is_unreadable_and_transient(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git"], 60)

    monkeypatch.setattr(ra, "run_git", _timeout)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


def test_oserror_is_unreadable_and_transient(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError(_NO_GIT)

    monkeypatch.setattr(ra, "run_git", _boom)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


def test_rev_list_failure_is_unreadable_and_transient(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ra.run_git

    def _fail_rev_list(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "rev-list":
            return subprocess.CompletedProcess(argv, 128, "", "boom")
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _fail_rev_list)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


@pytest.mark.parametrize(
    ("fail_prefix", "outcome"),
    [
        pytest.param(("merge-base",), 128, id="merge-base-error"),
        pytest.param(("merge-base",), None, id="merge-base-timeout"),
        pytest.param(("rev-list", "--merges"), 128, id="merges-error"),
        pytest.param(("rev-list", "--parents"), 128, id="parents-error"),
    ],
)
def test_git_errors_in_the_ancestry_walk_are_unreadable(
    repo: ApprovalRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_prefix: tuple[str, ...],
    outcome: int | None,
) -> None:
    main_tip = advance_main(repo.origin, repo.worktree, tmp_path / "side", "m.py")
    git_in(repo.worktree, "merge", "--no-ff", "--no-edit", main_tip)
    real = ra.run_git

    def _inject(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if tuple(argv[: len(fail_prefix)]) == fail_prefix:
            if outcome is None:
                raise subprocess.TimeoutExpired(["git"], 60)
            return subprocess.CompletedProcess(argv, outcome, "", "boom")
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _inject)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


def test_merge_base_exit_other_than_0_or_1_is_unreadable(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_tracked_file(repo.worktree, "more.py")
    real = ra.run_git

    def _inject(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if tuple(argv[:2]) == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(argv, 128, "", "fatal")
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _inject)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE


# --- resolve_commit ---------------------------------------------------------


def test_resolve_commit_returns_the_full_sha(repo: ApprovalRepo) -> None:
    lookup = resolve_commit(repo.worktree, "HEAD")

    assert lookup == CommitLookup(repo.approved, False)


def test_resolve_commit_unknown_rev_is_none_without_exec_failure(
    repo: ApprovalRepo,
) -> None:
    lookup = resolve_commit(repo.worktree, "refs/heads/nope")

    assert lookup == CommitLookup(None, False)


def test_resolve_commit_exec_failure(tmp_path: Path) -> None:
    lookup = resolve_commit(tmp_path / "gone", "HEAD")

    assert lookup == CommitLookup(None, True)


def test_resolve_commit_forwards_timeout(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = ra.run_git

    def _spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _spy)

    resolve_commit(repo.worktree, "HEAD", timeout=3.0)

    assert seen == [3.0]


# --- verdict rendering ------------------------------------------------------


def test_render_verdict_line_shapes() -> None:
    assert (
        render_verdict_line(ApprovalVerdict(APPROVAL_NOT_REQUIRED, "small"))
        == "REVIEW_APPROVAL_NOT_REQUIRED"
    )
    assert (
        render_verdict_line(ApprovalVerdict(APPROVAL_RECORDED, "ok"))
        == "REVIEW_APPROVAL_RECORDED"
    )
    assert (
        render_verdict_line(ApprovalVerdict(APPROVAL_STALE, "head moved"))
        == "REVIEW_APPROVAL_PARK: stale — head moved"
    )
    assert (
        render_verdict_line(ApprovalVerdict(APPROVAL_CHANNEL_ABSENT, "old schema"))
        == "REVIEW_APPROVAL_PARK: channel_absent — old schema"
    )


def test_channel_absent_is_not_a_pass() -> None:
    assert ApprovalVerdict(APPROVAL_CHANNEL_ABSENT, "x").passed is False


def test_full_sha_lookup_exec_failure_is_unreadable_and_transient(
    repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ra.run_git

    def _fail_full_sha(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if repo.approved in " ".join(argv):
            raise subprocess.TimeoutExpired(["git"], 1)
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _fail_full_sha)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True
    assert "could not run" in verdict.detail


def test_merge_parent_ancestry_error_is_unreadable_and_transient(
    repo: ApprovalRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_tip = advance_main(repo.origin, repo.worktree, tmp_path / "side", "m.py")
    git_in(repo.worktree, "merge", "--no-ff", "--no-edit", main_tip)
    real = ra.run_git
    seen_parents = []

    def _inject(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if tuple(argv[:2]) == ("merge-base", "--is-ancestor") and argv[2] == main_tip:
            seen_parents.append(argv[2])
            return subprocess.CompletedProcess(argv, 128, "", "fatal")
        return real(argv, **kwargs)

    monkeypatch.setattr(ra, "run_git", _inject)

    verdict = _evaluate(repo, _inputs(approved_sha=repo.approved))

    assert seen_parents
    assert verdict.token == APPROVAL_UNREADABLE
    assert verdict.transient is True


# ---------------------------------------------------------------------------
# The review sign-off attestation verdict (#3040, RFC 0017 II1)
# ---------------------------------------------------------------------------


class _ToleranceSpy:
    """A recording ``merge_tolerance_for`` stub."""

    def __init__(self, answer: object = None) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, sha: str) -> Any:
        self.calls.append(sha)
        return self.answer


def _att(attested: str, **overrides: object) -> dict[str, object]:
    from tests._review_attestation_helpers import attestation_for

    raw = attestation_for(attested).model_dump(mode="json")
    raw.update(overrides)
    return raw


def _check(
    repo: ApprovalRepo,
    raw: object,
    *,
    spy: _ToleranceSpy | None = None,
    head_sha: str | None = "HEAD",
    ticket: str | None = "T-1",
    client: str | None = "test-client",
) -> ra.AttestationVerdict:
    resolved = head(repo.worktree) if head_sha == "HEAD" else head_sha
    return ra.evaluate_review_attestation(
        raw,
        expected_ticket=ticket,
        expected_client=client,
        repo=repo.worktree,
        head_sha=resolved,
        merge_tolerance_for=spy if spy is not None else _ToleranceSpy(),
        renumber_patterns=(),
    )


class TestEvaluateReviewAttestation:
    def test_valid_at_same_head_never_asks_for_tolerance(
        self, repo: ApprovalRepo
    ) -> None:
        spy = _ToleranceSpy()
        verdict = _check(repo, _att(repo.approved), spy=spy)
        assert verdict.token == ra.ATTESTATION_VALID
        assert verdict.valid
        assert spy.calls == []
        assert verdict.attestation_head_sha == verdict.head_sha == repo.approved
        assert (verdict.ticket_id, verdict.client) == ("T-1", "test-client")
        assert verdict.reviewer_backend == "claude-native"

    @pytest.mark.parametrize(
        ("raw", "token"),
        [
            (None, ra.INVALID_MISSING),
            ("not-an-object", ra.INVALID_UNREADABLE),
            ({"schema_version": 2}, ra.INVALID_SCHEMA_VERSION),
            ({"schema_version": 1, "head_sha": "x"}, ra.INVALID_MALFORMED),
        ],
    )
    def test_load_level_invalid(
        self, repo: ApprovalRepo, raw: object, token: str
    ) -> None:
        spy = _ToleranceSpy()
        verdict = _check(repo, raw, spy=spy)
        assert verdict.token == token
        assert not verdict.valid
        assert spy.calls == []

    def test_non_hex_head_is_malformed(self, repo: ApprovalRepo) -> None:
        assert _check(repo, _att(repo.approved, head_sha="Z" * 40)).token == (
            ra.INVALID_MALFORMED
        )

    @pytest.mark.parametrize(
        ("kwargs", "token"),
        [
            ({"ticket": "T-2"}, ra.INVALID_WRONG_TICKET),
            ({"ticket": None}, ra.INVALID_WRONG_TICKET),
            ({"client": "other"}, ra.INVALID_WRONG_CLIENT),
            ({"client": None}, ra.INVALID_WRONG_CLIENT),
        ],
    )
    def test_identity_mismatch(
        self, repo: ApprovalRepo, kwargs: dict[str, Any], token: str
    ) -> None:
        spy = _ToleranceSpy()
        verdict = _check(repo, _att(repo.approved), spy=spy, **kwargs)
        assert verdict.token == token
        assert spy.calls == []

    def test_verdict_not_pass(self, repo: ApprovalRepo) -> None:
        verdict = _check(repo, _att(repo.approved, verdict="FAIL"))
        assert verdict.token == ra.INVALID_FAILED_VERDICT
        assert ra.INVALID_FAILED_VERDICT.endswith(":verdict_not_pass")

    def test_head_unreadable(self, repo: ApprovalRepo) -> None:
        assert _check(repo, _att(repo.approved), head_sha=None).token == (
            ra.INVALID_HEAD_UNREADABLE
        )

    def test_not_ancestor(self, repo: ApprovalRepo) -> None:
        spy = _ToleranceSpy()
        verdict = _check(repo, _att("f" * 40), spy=spy)
        assert verdict.token == ra.INVALID_NOT_ANCESTOR
        assert spy.calls == []

    def test_later_code_commit_is_untolerated(self, repo: ApprovalRepo) -> None:
        spy = _ToleranceSpy()
        commit_tracked_file(repo.worktree, "later.py")
        verdict = _check(repo, _att(repo.approved), spy=spy)
        assert verdict.token == ra.INVALID_UNTOLERATED_COMMIT
        assert verdict.offending_commit == head(repo.worktree)
        assert verdict.offending_path == "later.py"
        assert spy.calls == [repo.approved]

    def test_span_too_large(
        self, repo: ApprovalRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw import review_attestation_tolerance as tol

        monkeypatch.setattr(tol, "MAX_TOLERANCE_SPAN_COMMITS", 0)
        commit_tracked_file(repo.worktree, "later.py")
        assert _check(repo, _att(repo.approved)).token == ra.INVALID_SPAN_TOO_LARGE

    def test_wrong_head_never_passes(self, repo: ApprovalRepo) -> None:
        commit_tracked_file(repo.worktree, "later.py")
        attested_later = head(repo.worktree)
        git_in(repo.worktree, "reset", "--hard", repo.approved)
        assert not _check(repo, _att(attested_later)).valid

    def test_approval_ancestry_helpers_are_not_used(
        self, repo: ApprovalRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.review_convergence import MergeTolerance
        from tests._review_approval_helpers import sync_merge

        def _boom(*_a: object, **_k: object) -> object:
            msg = "the approval predicate's ancestry helpers are a separate rule"
            raise AssertionError(msg)

        monkeypatch.setattr(ra, "_merge_parents_on_default", _boom)
        monkeypatch.setattr(ra, "_new_non_merge_work", _boom)
        advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
        sync_merge(repo.worktree)
        verdict = _check(
            repo, _att(repo.approved), spy=_ToleranceSpy(MergeTolerance("main"))
        )
        assert verdict.valid
