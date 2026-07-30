"""Tests for cw.pr_hydrate — PR-state hydration in the serve tick (#929)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    TicketTask,
    WatchedPr,
)
from cw.pr_hydrate import (
    WATCHED_PR_COUNTERPARTY,
    _blocked_attention_state,
    _comment_body_is_blocking,
    _compute_attention_state,
    _derive_pr_state,
    _diff_transitions,
    _has_blocking_comment_review,
    _hydrate_watched_prs,
    _overlay_push_observation,
    _parse_pr_url,
    _repo_slug_mismatch,
    _resolve_repo_slug,
    _resolve_task_by_pr_ref,
    _reviewer_node_login,
    _summarize_status_checks,
    apply_pr_state_observation,
    derive_counterparty,
    hydrate_pr_states,
    observe_pushed_event,
    resolve_and_register_review_request,
)

_URL = "https://github.com/acme/widgets/pull/42"


def _checkrun(status: str, conclusion: str = "", name: str = "check") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "status": status,
        "conclusion": conclusion,
        "name": name,
        "workflowName": "wf",
        "detailsUrl": "https://ci/1",
    }


def _pr_view_payload(**fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "reviewDecision": "",
        "isDraft": False,
        "reviewRequests": [],
        "comments": [],
    }
    payload.update(fields)
    return payload


@pytest.fixture(autouse=True)
def _mock_cached_gh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``cached_gh_login()`` to unresolved for every test in this file.

    Without this, the first of this file's 24 ``hydrate_pr_states(...)``-
    driving tests would shell out to a real ``gh api user`` subprocess (up to
    a 10s timeout, #1195) and leak the process-lifetime cache into every
    later test in the same pytest process. Individual tests override via
    their own ``monkeypatch.setattr("cw.operator_identity.cached_gh_login", ...)``
    — pytest patches stack, so a test-level patch wins (mirrors
    ``conftest.py``'s ``_mock_push_notification`` autouse-with-override
    precedent).
    """
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)


class TestSummarizeStatusChecks:
    def test_empty_rollup_is_ok(self) -> None:
        result = _summarize_status_checks([])
        assert result == {"failing": [], "pending_count": 0, "ok": True}

    def test_failed_checkrun_is_not_ok(self) -> None:
        rollup = [_checkrun("COMPLETED", "FAILURE", name="lint")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is False
        assert result["failing"][0]["name"] == "lint"
        assert result["failing"][0]["conclusion"] == "FAILURE"

    def test_pending_checkrun_does_not_block_ok(self) -> None:
        rollup = [_checkrun("IN_PROGRESS", name="build")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["pending_count"] == 1

    def test_passing_checkrun_is_ok(self) -> None:
        rollup = [_checkrun("COMPLETED", "SUCCESS", name="test")]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["failing"] == []

    def test_legacy_status_context_failure(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "FAILURE", "context": "ci"}]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is False
        assert result["failing"][0]["name"] == "ci"

    def test_legacy_status_context_pending(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "PENDING", "context": "ci"}]
        result = _summarize_status_checks(rollup)
        assert result["ok"] is True
        assert result["pending_count"] == 1


class TestAttentionState:
    def test_row0_draft_returns_none(self) -> None:
        # Draft gates the entire function even with a blocking merge state.
        assert (
            _compute_attention_state(
                ci_ok=False,
                pending_count=0,
                merge_state_status="DIRTY",
                review_decision="CHANGES_REQUESTED",
                is_draft=True,
                reviewer_count=0,
            )
            is None
        )

    def test_row1_merge_blocked_dirty(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="DIRTY",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "merge_blocked"
        )

    def test_row1_merge_blocked_behind(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="BEHIND",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "merge_blocked"
        )

    def test_row1_unknown_merge_state_not_merge_blocked(self) -> None:
        # Ported wiki lesson "mergeStateStatus can read UNKNOWN immediately
        # after push/rebase" (session:826a27f3): GitHub computes
        # mergeStateStatus asynchronously, so a transient UNKNOWN right after
        # activity must NOT be read as merge_blocked. _ROW1_MERGE_BLOCKING_STATES
        # is a strict allow-list ({DIRTY, BEHIND}); UNKNOWN falls through, so a
        # not-yet-computed merge state can never misfire escalate_merge_block.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="UNKNOWN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )

    def test_row2_ci_failing(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=False,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="",
                is_draft=False,
                reviewer_count=1,
            )
            == "ci_failing"
        )

    def test_row3_changes_requested(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="CHANGES_REQUESTED",
                is_draft=False,
                reviewer_count=1,
            )
            == "changes_requested"
        )

    def test_row4_no_reviewer(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=0,
            )
            == "no_reviewer"
        )

    def test_row4_review_required_with_reviewer_is_not_no_reviewer(self) -> None:
        # REVIEW_REQUIRED but a reviewer is assigned -> falls through to default.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=2,
            )
            == "ready_to_approve"
        )

    def test_row0_gates_row4_draft_zero_reviewers(self) -> None:
        # Explicit amendment case: draft + zero reviewers + REVIEW_REQUIRED -> None,
        # NOT no_reviewer (row 0 gates row 4).
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=True,
                reviewer_count=0,
            )
            is None
        )

    def test_same_inputs_non_draft_returns_no_reviewer(self) -> None:
        # Same inputs as the gated case but non-draft -> no_reviewer.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=0,
            )
            == "no_reviewer"
        )

    def test_row5a_blocked_with_pending_checks_is_none(self) -> None:
        # #929 premise round (2026-07-05): BLOCKED + green-so-far CI + a check
        # still running -> waiting on CI, NOT ready_to_approve.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=2,
                merge_state_status="BLOCKED",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=1,
            )
            is None
        )

    def test_row5b_blocked_review_required_ready_to_approve(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="BLOCKED",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )

    def test_row5c_blocked_without_review_requirement_is_none(self) -> None:
        # BLOCKED, nothing pending, no review requirement -> unknown blocker;
        # do not claim the PR is approvable.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="BLOCKED",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
            )
            is None
        )

    def test_row6_default_ready_to_approve(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )

    def test_row2b_blocking_comment_overrides_blocked_ladder(self) -> None:
        # #1195: a blocking-comment-shaped review on a BLOCKED PR (row5b would
        # otherwise read ready_to_approve) still reads changes_requested.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="BLOCKED",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=1,
                has_blocking_comment_review=True,
            )
            == "changes_requested"
        )

    def test_row2b_blocking_comment_overrides_row6_clean_ladder(self) -> None:
        # #1195: the ticket's non-literal case — a clean, fully-approved PR
        # (row6 would otherwise read ready_to_approve) still reads
        # changes_requested when a blocking comment marker is present.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
                has_blocking_comment_review=True,
            )
            == "changes_requested"
        )

    def test_row2b_does_not_override_row0_draft(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                is_draft=True,
                reviewer_count=1,
                has_blocking_comment_review=True,
            )
            is None
        )

    def test_row2b_does_not_override_row1_merge_blocked(self) -> None:
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="DIRTY",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
                has_blocking_comment_review=True,
            )
            == "merge_blocked"
        )

    def test_row2b_default_false_preserves_existing_behavior(self) -> None:
        # Defaulted kwarg — every pre-#1195 call site is unaffected.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                is_draft=False,
                reviewer_count=1,
            )
            == "ready_to_approve"
        )

    def test_row2b_overrides_row4_no_reviewer(self) -> None:
        # #1195 SHOULD_FIX: the docstring claims row 2b overrides rows 4/5/6
        # uniformly -- this proves the row-4 (no_reviewer) case specifically,
        # not just the BLOCKED and clean/ready_to_approve ladders.
        assert (
            _compute_attention_state(
                ci_ok=True,
                pending_count=0,
                merge_state_status="CLEAN",
                review_decision="REVIEW_REQUIRED",
                is_draft=False,
                reviewer_count=0,
                has_blocking_comment_review=True,
            )
            == "changes_requested"
        )


class TestBlockingCommentReview:
    """_comment_body_is_blocking / _has_blocking_comment_review (#1195)."""

    def test_must_fix_marker_non_self_authored_is_blocking(self) -> None:
        comments = [{"author": {"login": "someone-else"}, "body": "MUST_FIX: bad"}]
        assert _has_blocking_comment_review(comments, self_login="the-operator") is True

    def test_blocking_marker_non_self_authored_is_blocking(self) -> None:
        comments = [{"author": {"login": "someone-else"}, "body": "BLOCKING issue"}]
        assert _has_blocking_comment_review(comments, self_login="the-operator") is True

    def test_review_heading_prefix_non_self_authored_is_blocking(self) -> None:
        comments = [
            {
                "author": {"login": "someone-else"},
                "body": "## Review: blocking\nData handling defect.",
            }
        ]
        assert _has_blocking_comment_review(comments, self_login="the-operator") is True

    def test_marker_present_self_authored_is_not_blocking(self) -> None:
        comments = [{"author": {"login": "the-operator"}, "body": "MUST_FIX: bad"}]
        assert (
            _has_blocking_comment_review(comments, self_login="the-operator") is False
        )

    def test_no_marker_ordinary_comment_is_not_blocking(self) -> None:
        comments = [{"author": {"login": "someone-else"}, "body": "looks good!"}]
        assert (
            _has_blocking_comment_review(comments, self_login="the-operator") is False
        )

    def test_empty_comments_list_is_not_blocking(self) -> None:
        assert _has_blocking_comment_review([], self_login="the-operator") is False

    def test_empty_comments_list_with_no_self_login_is_not_blocking(self) -> None:
        assert _has_blocking_comment_review([], self_login=None) is False

    def test_unresolved_self_login_fails_closed_not_open(self) -> None:
        # #1195 MUST_FIX: an unresolved identity (self_login=None) must not
        # scan without exclusion -- that would reproduce the exact
        # self-authored re-trigger loop the exclusion exists to prevent.
        comments = [{"author": {"login": "someone-else"}, "body": "MUST_FIX: bad"}]
        assert _has_blocking_comment_review(comments, self_login=None) is False

    def test_malformed_non_dict_entry_is_skipped_not_raised(self) -> None:
        comments = [
            "not a dict",  # type: ignore[list-item]
            {"author": {"login": "someone-else"}, "body": "MUST_FIX: bad"},
        ]
        assert _has_blocking_comment_review(comments, self_login="the-operator") is True

    def test_malformed_non_str_body_is_skipped_not_raised(self) -> None:
        comments = [
            {"author": {"login": "someone-else"}, "body": None},
            {"author": {"login": "someone-else"}, "body": "MUST_FIX: bad"},
        ]
        assert _has_blocking_comment_review(comments, self_login="the-operator") is True

    def test_comment_body_is_blocking_direct(self) -> None:
        assert _comment_body_is_blocking("## Review: nope") is True
        assert _comment_body_is_blocking("plain MUST_FIX text") is True
        assert _comment_body_is_blocking("plain BLOCKING text") is True
        assert _comment_body_is_blocking("looks great, thanks!") is False

    def test_comment_body_is_blocking_requires_word_boundary(self) -> None:
        # #1195 SHOULD_FIX: bare substring containment would false-positive
        # on words that merely contain a marker, e.g. NONBLOCKING/UNBLOCKING.
        assert _comment_body_is_blocking("this change is NONBLOCKING") is False
        assert _comment_body_is_blocking("UNBLOCKING the queue now") is False

    def test_comment_body_is_blocking_boundary_excludes_hyphen(self) -> None:
        # #1195 MUST_FIX (re-review cycle 2): a bare \b boundary treats a
        # hyphen as a boundary too, so "NON-BLOCKING" (a common code-review
        # convention for explicitly marking a comment non-blocking) would
        # still false-positive under a naive \b-only fix.
        assert _comment_body_is_blocking("NON-BLOCKING: consider renaming") is False


class TestDerivePrStateCommentReview:
    """_derive_pr_state (#1195): comments wired end-to-end into attention_state."""

    def test_ticket_repro_fixture_reads_changes_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the ticket's exact BLOCKED + REVIEW_REQUIRED fixture: a
        blocking comment must override what row5b would otherwise read as
        ready_to_approve."""
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(
                reviewDecision="REVIEW_REQUIRED",
                reviewRequests=[{"slug": "a-team"}],
                mergeStateStatus="BLOCKED",
                isDraft=False,
                comments=[
                    {
                        "author": {"login": "someone-else"},
                        "body": "## Review: blocking\nData handling defect.",
                    }
                ],
            ),
        )
        pr_state = _derive_pr_state(_URL, self_login="the-operator")
        assert pr_state is not None
        assert pr_state.attention_state == "changes_requested"

    def test_self_authored_blocking_comment_does_not_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(
                reviewDecision="REVIEW_REQUIRED",
                reviewRequests=[{"slug": "a-team"}],
                mergeStateStatus="BLOCKED",
                comments=[
                    {"author": {"login": "the-operator"}, "body": "MUST_FIX: bad"}
                ],
            ),
        )
        pr_state = _derive_pr_state(_URL, self_login="the-operator")
        assert pr_state is not None
        assert pr_state.attention_state == "ready_to_approve"


class TestPushPollParity:
    """Poll (_compute_attention_state) and push (_overlay_push_observation)
    must agree on attention_state for the same GitHub-facts combo (#1196).

    Fixes the bug where the push overlay never recomputed attention_state,
    leaving it stale after a webhook-driven update.
    """

    def test_poll_and_push_agree_on_merge_blocked(self) -> None:
        poll_answer = _compute_attention_state(
            ci_ok=True,
            pending_count=0,
            merge_state_status="BEHIND",
            review_decision="REVIEW_REQUIRED",
            is_draft=False,
            reviewer_count=1,
        )
        base = _pr_state(
            merge_state_status="BEHIND",
            review_decision="REVIEW_REQUIRED",
            ci_ok=True,
            reviewer_count=1,
            is_draft=False,
        )
        push_answer = _overlay_push_observation(
            base, event_type=OrchestratorEventType.PR_REVIEW_RECEIVED, payload={}
        ).attention_state
        assert poll_answer == push_answer


class TestParsePrUrl:
    def test_parses_owner_repo_and_number(self) -> None:
        assert _parse_pr_url(_URL) == ("acme/widgets", 42)

    def test_returns_none_for_garbage(self) -> None:
        assert _parse_pr_url("not-a-url") is None

    def test_returns_none_for_non_pull_url(self) -> None:
        assert _parse_pr_url("https://github.com/acme/widgets/issues/42") is None


def _init_repo_with_remote(path: Path, remote_url: str | None) -> Path:
    """Build a minimal git repo at *path*, optionally with an ``origin`` remote.

    Raw subprocess (not the shared ``make_git_repo`` fixture): no existing
    fixture sets a remote, and extending the widely-shared factory to add one
    would be a broader-blast-radius change than GitHub #1198 calls for.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], capture_output=True, check=True)
    if remote_url is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote_url],
            capture_output=True,
            check=True,
        )
    return path


class TestResolveRepoSlug:
    """_resolve_repo_slug parses an ``owner/repo`` slug from origin's URL."""

    def test_ssh_remote(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "ssh", "git@github.com:acme/widgets.git"
        )
        assert _resolve_repo_slug(repo) == "acme/widgets"

    def test_https_remote_with_git_suffix(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "https-git", "https://github.com/acme/widgets.git"
        )
        assert _resolve_repo_slug(repo) == "acme/widgets"

    def test_https_remote_without_git_suffix(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "https", "https://github.com/acme/widgets"
        )
        assert _resolve_repo_slug(repo) == "acme/widgets"

    def test_non_github_remote_returns_none(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "gitlab", "https://gitlab.com/acme/widgets.git"
        )
        assert _resolve_repo_slug(repo) is None

    def test_no_origin_remote_returns_none(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(tmp_path / "no-remote", None)
        assert _resolve_repo_slug(repo) is None

    def test_non_git_directory_returns_none(self, tmp_path: Path) -> None:
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        assert _resolve_repo_slug(bare) is None

    def test_subprocess_oserror_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # git binary missing / unrunnable -> OSError -> None, never raises.
        def _raise(*_a: Any, **_k: Any) -> Any:
            msg = "git: not found"
            raise OSError(msg)

        monkeypatch.setattr("cw.pr_hydrate.subprocess.run", _raise)
        assert _resolve_repo_slug(tmp_path) is None

    def test_subprocess_timeout_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hung `git` process (stale credential prompt, NFS stall) times out
        # -> None, never raises (GitHub #1198 perf-review follow-up).
        def _raise(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr("cw.pr_hydrate.subprocess.run", _raise)
        assert _resolve_repo_slug(tmp_path) is None


class TestRepoSlugMismatch:
    """_repo_slug_mismatch fails open; only a resolvable disagreement is a hit."""

    def test_case_insensitive_match_returns_none(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "case", "https://github.com/ACME/Widgets.git"
        )
        assert _repo_slug_mismatch("acme/widgets", repo) is None

    def test_real_mismatch_returns_resolved_slug(self, tmp_path: Path) -> None:
        repo = _init_repo_with_remote(
            tmp_path / "mismatch", "https://github.com/acme/other-repo.git"
        )
        assert _repo_slug_mismatch("acme/widgets", repo) == "acme/other-repo"

    def test_unresolvable_remote_fails_open(self, tmp_path: Path) -> None:
        # No origin remote -> unresolvable -> None (fail-open), never the pr_repo.
        repo = _init_repo_with_remote(tmp_path / "unresolvable", None)
        assert _repo_slug_mismatch("acme/widgets", repo) is None


def _pr_state(**fields: Any) -> PrState:
    defaults: dict[str, Any] = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "ci_ok": True,
        "review_decision": "",
        "attention_state": "ready_to_approve",
    }
    defaults.update(fields)
    return PrState(**defaults)


_BASE = {
    "repo": "acme/widgets",
    "pr_number": 42,
    "ticket_id": "GEN-9",
    "client": "acme",
}


class TestTransitions:
    def test_merged_transition_emits_base_payload(self) -> None:
        old = _pr_state(state="OPEN")
        new = _pr_state(state="MERGED")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        types = {t for t, _ in events}
        assert OrchestratorEventType.PR_MERGED in types
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_MERGED)
        assert payload == _BASE

    def test_merged_first_seen_emits(self) -> None:
        new = _pr_state(state="MERGED")
        events = _diff_transitions(old=None, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_MERGED in {t for t, _ in events}

    def test_ci_failed_transition_true_to_false(self) -> None:
        old = _pr_state(ci_ok=True)
        new = _pr_state(ci_ok=False, failing_checks=["lint", "test-unit"])
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_CI_FAILED)
        assert payload == {**_BASE, "failing_checks": ["lint", "test-unit"]}

    def test_ci_failed_not_emitted_without_prior_true(self) -> None:
        # First-seen failing (old is None) does not emit ci_failed.
        new = _pr_state(ci_ok=False, failing_checks=["lint"])
        events = _diff_transitions(old=None, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_CI_FAILED not in {t for t, _ in events}

    def test_ci_failed_deduped_when_still_failing(self) -> None:
        old = _pr_state(ci_ok=False, failing_checks=["lint"])
        new = _pr_state(ci_ok=False, failing_checks=["lint"])
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_CI_FAILED not in {t for t, _ in events}

    def test_review_received_on_change(self) -> None:
        old = _pr_state(review_decision="REVIEW_REQUIRED")
        new = _pr_state(review_decision="CHANGES_REQUESTED")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        payload = next(
            p for t, p in events if t == OrchestratorEventType.PR_REVIEW_RECEIVED
        )
        assert payload == {**_BASE, "review_decision": "CHANGES_REQUESTED"}

    def test_review_received_deduped_when_unchanged(self) -> None:
        old = _pr_state(review_decision="APPROVED")
        new = _pr_state(review_decision="APPROVED")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_REVIEW_RECEIVED not in {t for t, _ in events}

    def test_mergeable_transition_into_clean(self) -> None:
        old = _pr_state(merge_state_status="DIRTY")
        new = _pr_state(merge_state_status="CLEAN")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_MERGEABLE)
        # Literal key assertion (not just the value): mergeStateStatus is a
        # deliberate camelCase passthrough of the raw gh field name (R5), unlike
        # every sibling payload key, which is snake_case. Guards against a future
        # "normalize the key" refactor silently breaking bus consumers.
        assert "mergeStateStatus" in payload
        assert "merge_state_status" not in payload
        assert payload == {**_BASE, "mergeStateStatus": "CLEAN"}

    def test_mergeable_not_emitted_when_already_clean(self) -> None:
        old = _pr_state(merge_state_status="CLEAN")
        new = _pr_state(merge_state_status="CLEAN")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_MERGEABLE not in {t for t, _ in events}

    def test_mergeable_not_emitted_leaving_blocked_into_unknown(self) -> None:
        # #929 premise round (2026-07-05): the event fires on ENTERING a
        # genuinely-mergeable status, not on merely leaving a blocking one.
        old = _pr_state(merge_state_status="BLOCKED")
        new = _pr_state(merge_state_status="UNKNOWN")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        assert OrchestratorEventType.PR_MERGEABLE not in {t for t, _ in events}

    def test_mergeable_emitted_entering_unstable(self) -> None:
        # UNSTABLE is in GitHub's mergeable set (failing non-required checks).
        old = _pr_state(merge_state_status="BLOCKED")
        new = _pr_state(merge_state_status="UNSTABLE")
        events = _diff_transitions(old=old, new=new, base=dict(_BASE))
        payload = next(p for t, p in events if t == OrchestratorEventType.PR_MERGEABLE)
        assert payload == {**_BASE, "mergeStateStatus": "UNSTABLE"}


class TestCandidateSelection:
    def test_hydrates_task_with_pr_url_and_no_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "OPEN"

    def test_skips_task_without_pr_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(tasks=[TicketTask(ticket_id="GEN-1", client="acme")])
        )
        calls = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []
        assert load_dev_queue().tasks[0].pr_state is None

    def test_skips_terminal_pr_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="MERGED",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        calls = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []

    def test_skips_closed_pr_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLOSED is terminal alongside MERGED — both excluded from re-hydration."""
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="CLOSED",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        calls = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []


class TestDeriveCounterparty:
    """Tests for derive_counterparty (RFC 0011 S1 D-S1)."""

    def test_no_pr_task_is_self(self) -> None:
        assert derive_counterparty(None, operator_login=None) == "self"

    def test_hold_with_no_pr_url_is_self(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme", pr_url=None)
        assert derive_counterparty(task, operator_login=None) == "self"

    def test_auto_dev_candidate_pr_is_self(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)
        assert derive_counterparty(task, operator_login=None) == "self"

    def test_operator_login_argument_does_not_change_result(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)
        with_login = derive_counterparty(task, operator_login="alice")
        without_login = derive_counterparty(task, operator_login=None)
        assert with_login == "self"
        assert without_login == "self"


class TestThrottle:
    def test_second_pass_within_interval_is_throttled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(state="OPEN"),
        )
        config = OrchestratorConfig(pr_hydration_interval_seconds=150)
        with freeze_time("2026-07-04 12:00:00") as frozen:
            hydrate_pr_states(config)
            assert len(calls) == 1
            frozen.tick(delta=timedelta(seconds=60))
            hydrate_pr_states(config)
            assert len(calls) == 1  # throttled — no second fetch
            frozen.tick(delta=timedelta(seconds=120))
            hydrate_pr_states(config)
            assert len(calls) == 2  # interval elapsed — fetched again

    def test_hydrated_at_stamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        with freeze_time("2026-07-04 12:00:00"):
            hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.hydrated_at == datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


class TestSelfLoginResolution:
    """RISK fix (#1195): cached_gh_login() resolved at most once per
    hydrate_pr_states() call, never once per candidate/watched-PR."""

    def test_resolved_once_per_hydrate_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL),
                    TicketTask(
                        ticket_id="GEN-2",
                        client="acme",
                        pr_url="https://github.com/acme/widgets/pull/43",
                    ),
                    TicketTask(
                        ticket_id="GEN-3",
                        client="acme",
                        pr_url="https://github.com/acme/widgets/pull/44",
                    ),
                ],
                watched_prs=[_watched()],
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        calls: list[None] = []

        def _counting_login() -> str | None:
            calls.append(None)
            return "the-operator"

        monkeypatch.setattr("cw.operator_identity.cached_gh_login", _counting_login)
        hydrate_pr_states(OrchestratorConfig())
        assert len(calls) == 1

    def test_not_called_on_empty_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(DevQueueStore(tasks=[], watched_prs=[]))
        calls: list[None] = []

        def _counting_login() -> str | None:
            calls.append(None)
            return "the-operator"

        monkeypatch.setattr("cw.operator_identity.cached_gh_login", _counting_login)
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []

    def test_not_called_when_only_dismissed_watched_prs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #1195 SHOULD_FIX: a dismissed watched PR is never fetched
        # (_hydrate_watched_prs skips it before calling _derive_pr_state), so
        # resolving self_login for it is a wasted call every un-throttled tick.
        save_dev_queue(
            DevQueueStore(tasks=[], watched_prs=[_watched(status="dismissed")])
        )
        calls: list[None] = []

        def _counting_login() -> str | None:
            calls.append(None)
            return "the-operator"

        monkeypatch.setattr("cw.operator_identity.cached_gh_login", _counting_login)
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []

    def test_repo_map_lookup_adds_no_subprocess_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1171: per-repo resolution is a pure dict lookup -- populating
        operator_github_login_by_repo for every candidate repo must not add
        any extra cached_gh_login() (and thus no extra `gh api user`) calls
        beyond the single hoisted resolution #1195 already guarantees."""
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL),
                    TicketTask(
                        ticket_id="GEN-2",
                        client="acme",
                        pr_url="https://github.com/acme/widgets/pull/43",
                    ),
                    TicketTask(
                        ticket_id="GEN-3",
                        client="other",
                        pr_url="https://github.com/other/repo/pull/1",
                    ),
                ],
                watched_prs=[_watched()],
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        calls: list[None] = []

        def _counting_login() -> str | None:
            calls.append(None)
            return "the-operator"

        monkeypatch.setattr("cw.operator_identity.cached_gh_login", _counting_login)
        config = OrchestratorConfig(
            operator_github_login_by_repo={
                "acme/widgets": "override-user",
                "other/repo": "another-override",
            }
        )
        hydrate_pr_states(config)
        assert len(calls) <= 1


class TestTransientFailure:
    def test_fetch_none_leaves_prior_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prior = PrState(
            state="OPEN",
            merge_state_status="CLEAN",
            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1", client="acme", pr_url=_URL, pr_state=prior
                    )
                ]
            )
        )
        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", lambda *_a, **_kw: None)
        hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.hydrated_at == datetime(2000, 1, 1, tzinfo=UTC)


class TestHydrateEmitsEvents:
    def test_merged_emits_pr_merged_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="OPEN",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="MERGED"),
        )
        hydrate_pr_states(OrchestratorConfig())
        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].payload == _BASE | {"ticket_id": "GEN-1"}
        assert events[0].payload["repo"] == "acme/widgets"
        assert events[0].payload["pr_number"] == 42
        assert events[0].correlation_id == "GEN-1"

    def test_repeated_hydration_passes_dedup_through_persisted_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end (R12): two real hydrate_pr_states calls with an unchanged
        fetched state emit the transition exactly once, not once per call.

        Unlike TestTransitions' unit tests (which call _diff_transitions
        directly with hand-built old/new PrState objects), this exercises the
        full persisted-state wiring through _persist_and_emit — the same path
        where round 1 found and fixed a real persist/emit dedup race.
        """
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="OPEN",
                            ci_ok=True,
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(
                state="OPEN", statusCheckRollup=[_checkrun("COMPLETED", "FAILURE")]
            ),
        )
        config = OrchestratorConfig(pr_hydration_interval_seconds=150)
        with freeze_time("2026-07-04 12:00:00") as frozen:
            hydrate_pr_states(config)  # first pass: ci_ok True -> False, emits
            frozen.tick(delta=timedelta(seconds=200))  # clear the throttle
            hydrate_pr_states(config)  # second pass: still False -> False, no-op
        events = read_events(event_types=[OrchestratorEventType.PR_CI_FAILED])
        assert len(events) == 1

    def test_derives_new_fields_from_pr_view(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Poll path stores is_draft/reviewer_count so push can recompute
        attention_state without re-fetching GitHub (#1196)."""
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=PrState(
                            state="OPEN",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(
                isDraft=True, reviewRequests=[{"login": "x"}]
            ),
        )
        hydrate_pr_states(OrchestratorConfig())
        store = load_dev_queue()
        task = store.tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.is_draft is True
        assert task.pr_state.reviewer_count == 1


class TestMultiTaskRouting:
    """Two candidates in the same pass route to the correct task, no cross-talk."""

    _URL_A = "https://github.com/acme/widgets/pull/1"
    _URL_B = "https://github.com/acme/gadgets/pull/2"

    def test_two_candidates_fetch_correct_urls_and_persist_to_correct_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(ticket_id="GEN-A", client="acme", pr_url=self._URL_A),
                    TicketTask(ticket_id="GEN-B", client="acme", pr_url=self._URL_B),
                ]
            )
        )

        def _fake_fetch(pr_ref: str, **_kw: object) -> dict[str, Any]:
            if pr_ref == self._URL_A:
                return _pr_view_payload(state="OPEN", reviewDecision="APPROVED")
            if pr_ref == self._URL_B:
                return _pr_view_payload(state="OPEN", mergeStateStatus="DIRTY")
            msg = f"unexpected pr_ref: {pr_ref}"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fake_fetch)
        hydrate_pr_states(OrchestratorConfig())

        tasks_by_id = {t.ticket_id: t for t in load_dev_queue().tasks}
        state_a = tasks_by_id["GEN-A"].pr_state
        state_b = tasks_by_id["GEN-B"].pr_state
        assert state_a is not None
        assert state_b is not None
        assert state_a.review_decision == "APPROVED"
        assert state_a.merge_state_status == "CLEAN"
        assert state_b.merge_state_status == "DIRTY"
        assert state_b.review_decision == ""

    def test_two_candidates_emit_independent_transitions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-A",
                        client="acme",
                        pr_url=self._URL_A,
                        pr_state=PrState(
                            state="OPEN", hydrated_at=datetime(2000, 1, 1, tzinfo=UTC)
                        ),
                    ),
                    TicketTask(
                        ticket_id="GEN-B",
                        client="acme",
                        pr_url=self._URL_B,
                        pr_state=PrState(
                            state="OPEN", hydrated_at=datetime(2000, 1, 1, tzinfo=UTC)
                        ),
                    ),
                ]
            )
        )

        def _fake_fetch(pr_ref: str, **_kw: object) -> dict[str, Any]:
            if pr_ref == self._URL_A:
                return _pr_view_payload(state="MERGED")
            return _pr_view_payload(state="OPEN")

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fake_fetch)
        hydrate_pr_states(OrchestratorConfig())

        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].correlation_id == "GEN-A"
        assert events[0].payload["repo"] == "acme/widgets"

    def test_same_ticket_id_different_client_routes_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The routing key is (client, ticket_id), not ticket_id alone.

        TicketTask.session_id's docstring documents that ticket_id is not
        guaranteed unique across task instances (crashed vs. respawned tasks
        can share one). Confirm a same-ticket_id, different-client pair
        doesn't collapse into one dict entry and cross-contaminate the
        other's persisted state.
        """
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-SHARED", client="acme", pr_url=self._URL_A
                    ),
                    TicketTask(
                        ticket_id="GEN-SHARED", client="widgets-co", pr_url=self._URL_B
                    ),
                ]
            )
        )

        def _fake_fetch(pr_ref: str, **_kw: object) -> dict[str, Any]:
            if pr_ref == self._URL_A:
                return _pr_view_payload(state="OPEN", reviewDecision="APPROVED")
            if pr_ref == self._URL_B:
                return _pr_view_payload(state="OPEN", mergeStateStatus="DIRTY")
            msg = f"unexpected pr_ref: {pr_ref}"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fake_fetch)
        hydrate_pr_states(OrchestratorConfig())

        tasks_by_client = {t.client: t for t in load_dev_queue().tasks}
        state_acme = tasks_by_client["acme"].pr_state
        state_widgets = tasks_by_client["widgets-co"].pr_state
        assert state_acme is not None
        assert state_widgets is not None
        assert state_acme.review_decision == "APPROVED"
        assert state_acme.merge_state_status == "CLEAN"
        assert state_widgets.merge_state_status == "DIRTY"
        assert state_widgets.review_decision == ""


class TestApplyPrStateObservation:
    """apply_pr_state_observation (#930): the shared persist/diff/emit chokepoint
    extracted from _persist_and_emit's per-task body. Both the poll producer
    (_persist_and_emit) and the push producer (observe_pushed_event) route
    through this so they share transition-dedup semantics.
    """

    def test_persists_state_and_emits_transition(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(state="OPEN"),
                    )
                ]
            )
        )
        apply_pr_state_observation(
            client="acme", ticket_id="GEN-1", new_state=_pr_state(state="MERGED")
        )
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "MERGED"
        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].correlation_id == "GEN-1"
        assert events[0].payload == {
            "repo": "acme/widgets",
            "pr_number": 42,
            "ticket_id": "GEN-1",
            "client": "acme",
        }

    def test_no_matching_task_is_noop(self) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        apply_pr_state_observation(
            client="acme", ticket_id="GEN-404", new_state=_pr_state(state="MERGED")
        )
        assert read_events(event_types=[OrchestratorEventType.PR_MERGED]) == []

    def test_dedup_against_persisted_baseline(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(review_decision="APPROVED"),
                    )
                ]
            )
        )
        apply_pr_state_observation(
            client="acme",
            ticket_id="GEN-1",
            new_state=_pr_state(review_decision="APPROVED"),
        )
        assert read_events(event_types=[OrchestratorEventType.PR_REVIEW_RECEIVED]) == []

    def test_unparseable_pr_url_still_persists_but_emits_nothing(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url="not-a-github-url",
                        pr_state=_pr_state(state="OPEN"),
                    )
                ]
            )
        )
        apply_pr_state_observation(
            client="acme", ticket_id="GEN-1", new_state=_pr_state(state="MERGED")
        )
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "MERGED"
        assert read_events(event_types=[OrchestratorEventType.PR_MERGED]) == []


class TestResolveTaskByPrRef:
    def test_finds_matching_task(self) -> None:
        store = DevQueueStore(
            tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
        )
        task = _resolve_task_by_pr_ref(store, repo="acme/widgets", pr_number=42)
        assert task is not None
        assert task.ticket_id == "GEN-1"

    def test_returns_none_when_no_task_matches(self) -> None:
        store = DevQueueStore(
            tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
        )
        assert (
            _resolve_task_by_pr_ref(store, repo="acme/widgets", pr_number=999) is None
        )

    def test_returns_none_for_empty_store(self) -> None:
        empty_store = DevQueueStore(tasks=[])
        assert (
            _resolve_task_by_pr_ref(empty_store, repo="acme/widgets", pr_number=42)
            is None
        )

    def test_returns_first_match_when_multiple_tasks_share_pr(self) -> None:
        store = DevQueueStore(
            tasks=[
                TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL),
                TicketTask(ticket_id="GEN-2", client="acme", pr_url=_URL),
            ]
        )
        task = _resolve_task_by_pr_ref(store, repo="acme/widgets", pr_number=42)
        assert task is not None
        assert task.ticket_id == "GEN-1"


class TestOverlayPushObservation:
    """_overlay_push_observation (#930): pure overlay builder for one pushed
    wire event onto a prior (or absent) PrState baseline.
    """

    def test_merged_sets_state(self) -> None:
        old = _pr_state(state="OPEN")
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_MERGED, payload={}
        )
        assert new.state == "MERGED"
        # Terminal state short-circuits the ladder entirely (#1196).
        assert new.attention_state is None

    def test_ci_failed_sets_ci_ok_false_and_failing_checks(self) -> None:
        old = _pr_state(ci_ok=True, failing_checks=[])
        new = _overlay_push_observation(
            old,
            event_type=OrchestratorEventType.PR_CI_FAILED,
            payload={"failing_checks": ["lint"]},
        )
        assert new.ci_ok is False
        assert new.failing_checks == ["lint"]
        expected = _compute_attention_state(
            ci_ok=False,
            pending_count=0,
            merge_state_status="CLEAN",
            review_decision="",
            is_draft=False,
            reviewer_count=0,
        )
        assert new.attention_state == expected == "ci_failing"

    def test_ci_failed_missing_failing_checks_key_leaves_field_untouched(self) -> None:
        old = _pr_state(ci_ok=True, failing_checks=["stale"])
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_CI_FAILED, payload={}
        )
        assert new.ci_ok is False
        assert new.failing_checks == ["stale"]
        expected = _compute_attention_state(
            ci_ok=False,
            pending_count=0,
            merge_state_status="CLEAN",
            review_decision="",
            is_draft=False,
            reviewer_count=0,
        )
        assert new.attention_state == expected == "ci_failing"

    def test_review_received_sets_review_decision(self) -> None:
        old = _pr_state(review_decision="REVIEW_REQUIRED")
        new = _overlay_push_observation(
            old,
            event_type=OrchestratorEventType.PR_REVIEW_RECEIVED,
            payload={"review_decision": "APPROVED"},
        )
        assert new.review_decision == "APPROVED"
        expected = _compute_attention_state(
            ci_ok=True,
            pending_count=0,
            merge_state_status="CLEAN",
            review_decision="APPROVED",
            is_draft=False,
            reviewer_count=0,
        )
        assert new.attention_state == expected == "ready_to_approve"

    def test_review_received_missing_key_leaves_field_untouched(self) -> None:
        old = _pr_state(review_decision="REVIEW_REQUIRED")
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_REVIEW_RECEIVED, payload={}
        )
        assert new.review_decision == "REVIEW_REQUIRED"
        expected = _compute_attention_state(
            ci_ok=True,
            pending_count=0,
            merge_state_status="CLEAN",
            review_decision="REVIEW_REQUIRED",
            is_draft=False,
            reviewer_count=0,
        )
        assert new.attention_state == expected == "no_reviewer"

    def test_mergeable_sets_merge_state_status(self) -> None:
        old = _pr_state(merge_state_status="BLOCKED")
        new = _overlay_push_observation(
            old,
            event_type=OrchestratorEventType.PR_MERGEABLE,
            payload={"merge_state_status": "CLEAN"},
        )
        assert new.merge_state_status == "CLEAN"
        expected = _compute_attention_state(
            ci_ok=True,
            pending_count=0,
            merge_state_status="CLEAN",
            review_decision="",
            is_draft=False,
            reviewer_count=0,
        )
        assert new.attention_state == expected == "ready_to_approve"

    def test_mergeable_missing_key_leaves_field_untouched(self) -> None:
        old = _pr_state(merge_state_status="BLOCKED")
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_MERGEABLE, payload={}
        )
        assert new.merge_state_status == "BLOCKED"
        expected = _blocked_attention_state(pending_count=0, review_decision="")
        assert new.attention_state == expected is None

    def test_no_prior_baseline_starts_fresh(self) -> None:
        new = _overlay_push_observation(
            None, event_type=OrchestratorEventType.PR_MERGED, payload={}
        )
        assert new.state == "MERGED"
        assert new.ci_ok is True  # PrState() default, untouched
        # Terminal state short-circuits the ladder entirely (#1196).
        assert new.attention_state is None

    def test_overlay_recompute_preserves_blocking_comment_signal(self) -> None:
        """#1195 MUST_FIX: has_blocking_comment_review is not a PrState field
        and no wire payload carries it, but the overlay can infer it was in
        effect (changes_requested with a review_decision that doesn't itself
        explain it) and carry it forward, rather than silently reverting to
        ready_to_approve on an unrelated push event mid-interval.
        """
        old = _pr_state(
            merge_state_status="BLOCKED",
            review_decision="REVIEW_REQUIRED",
            reviewer_count=1,
            attention_state="changes_requested",  # set by a prior poll's comment signal
        )
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_MERGEABLE, payload={}
        )
        assert new.attention_state == "changes_requested"

    def test_overlay_recompute_reverts_when_real_review_decision_explains_prior_state(
        self,
    ) -> None:
        """#1195: the inference must not over-carry. When the prior
        changes_requested was explained by a real review_decision (not the
        comment signal), a push that moves review_decision away from
        CHANGES_REQUESTED must revert normally.
        """
        old = _pr_state(
            merge_state_status="CLEAN",
            review_decision="CHANGES_REQUESTED",
            reviewer_count=1,
            attention_state="changes_requested",
        )
        new = _overlay_push_observation(
            old,
            event_type=OrchestratorEventType.PR_REVIEW_RECEIVED,
            payload={"review_decision": "APPROVED"},
        )
        assert new.attention_state == "ready_to_approve"

    def test_overlay_recompute_masked_row_gap_reverts_on_clearing_push(self) -> None:
        """#1195 accepted residual gap (re-review cycle 2): when the comment
        signal was present at the prior poll but masked by a higher-
        precedence row (row 1, merge_blocked), the inference can't recover
        it -- base.attention_state reads merge_blocked, not
        changes_requested. A push that clears just that masking condition
        still reverts to whatever the base ladder computes, until the next
        full poll re-fetches comments. Fully closing this would require a
        new PrState field, which R2 forbids -- documented, not silently
        incorrect.
        """
        # Prior poll: has_blocking_comment_review was True, but row 1 (DIRTY)
        # masked it, so only "merge_blocked" was persisted.
        masked_ladder_result = _compute_attention_state(
            ci_ok=True,
            pending_count=0,
            merge_state_status="DIRTY",
            review_decision="REVIEW_REQUIRED",
            is_draft=False,
            reviewer_count=1,
            has_blocking_comment_review=True,
        )
        assert masked_ladder_result == "merge_blocked"
        old = _pr_state(
            merge_state_status="DIRTY",
            review_decision="REVIEW_REQUIRED",
            reviewer_count=1,
            attention_state=masked_ladder_result,
        )
        new = _overlay_push_observation(
            old,
            event_type=OrchestratorEventType.PR_MERGEABLE,
            payload={"merge_state_status": "CLEAN"},
        )
        assert new.attention_state == "ready_to_approve"

    def test_carries_forward_untouched_fields(self) -> None:
        old = _pr_state(
            state="OPEN",
            ci_ok=False,
            failing_checks=["x"],
            review_decision="APPROVED",
        )
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_MERGED, payload={}
        )
        assert new.state == "MERGED"
        assert new.ci_ok is False
        assert new.failing_checks == ["x"]
        assert new.review_decision == "APPROVED"
        # Terminal state wins over the ladder even with a stale ci_ok=False.
        assert new.attention_state is None

    def test_hydrated_at_refreshed(self) -> None:
        old = _pr_state(hydrated_at=datetime(2000, 1, 1, tzinfo=UTC))
        with freeze_time("2026-07-06 12:00:00"):
            new = _overlay_push_observation(
                old, event_type=OrchestratorEventType.PR_MERGED, payload={}
            )
        assert new.hydrated_at == datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
        assert new.attention_state is None

    def test_merged_sets_attention_state_none_regardless_of_prior_ladder_inputs(
        self,
    ) -> None:
        old = _pr_state(
            merge_state_status="BEHIND",
            review_decision="REVIEW_REQUIRED",
            ci_ok=False,
            reviewer_count=0,
            is_draft=False,
        )
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_MERGED, payload={}
        )
        assert new.attention_state is None

    def test_push_recomputes_merge_blocked_from_carried_inputs(self) -> None:
        """Ticket's exact repro (#1196): no ladder field changes in the
        payload, but attention_state must still be recomputed from the
        carried baseline instead of going stale."""
        old = _pr_state(
            merge_state_status="BEHIND",
            review_decision="REVIEW_REQUIRED",
            ci_ok=True,
            reviewer_count=1,
            is_draft=False,
            attention_state=None,
        )
        new = _overlay_push_observation(
            old, event_type=OrchestratorEventType.PR_REVIEW_RECEIVED, payload={}
        )
        assert new.attention_state == "merge_blocked"


class TestObservePushedEvent:
    """observe_pushed_event (#930): the public webhook-push entrypoint —
    resolves (repo, pr_number) to a task, builds the overlay, and routes it
    through apply_pr_state_observation (the same chokepoint the poll producer
    uses).
    """

    def test_matching_transition_emits_and_persists(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(state="OPEN"),
                    )
                ]
            )
        )
        observe_pushed_event(
            repo="acme/widgets", pr_number=42, wire_event_type="merged", payload={}
        )
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "MERGED"
        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1

    def test_no_state_change_is_deduped(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(merge_state_status="CLEAN"),
                    )
                ]
            )
        )
        observe_pushed_event(
            repo="acme/widgets",
            pr_number=42,
            wire_event_type="mergeable",
            payload={"merge_state_status": "CLEAN"},
        )
        assert read_events(event_types=[OrchestratorEventType.PR_MERGEABLE]) == []

    def test_unmatched_repo_pr_is_silent_noop(self) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        observe_pushed_event(
            repo="ghost/repo", pr_number=1, wire_event_type="merged", payload={}
        )
        assert read_events(event_types=[OrchestratorEventType.PR_MERGED]) == []

    def test_commented_review_emits_without_mutating_pr_state(self) -> None:
        """Operator correction #2 (#930): COMMENTED reviews are not a
        merge-gate signal, so they mutate NOTHING on PrState — but still emit
        pr.review_received unconditionally.
        """
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(review_decision="APPROVED"),
                    )
                ]
            )
        )
        observe_pushed_event(
            repo="acme/widgets",
            pr_number=42,
            wire_event_type="review_received",
            payload={"review_decision": "COMMENTED"},
        )
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.review_decision == "APPROVED"  # untouched

        events = read_events(event_types=[OrchestratorEventType.PR_REVIEW_RECEIVED])
        assert len(events) == 1
        assert events[0].payload["review_decision"] == "COMMENTED"
        assert events[0].correlation_id == "GEN-1"

    def test_commented_review_redelivery_double_emits(self) -> None:
        """Unlike the other 3 wire event types (which dedup via persisted
        PrState), a redelivered/duplicate COMMENTED webhook double-emits —
        there is no PrState field change to compare, so diff-based dedup
        cannot apply (#930 operator correction #2).
        """
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        for _ in range(2):
            observe_pushed_event(
                repo="acme/widgets",
                pr_number=42,
                wire_event_type="review_received",
                payload={"review_decision": "COMMENTED"},
            )
        events = read_events(event_types=[OrchestratorEventType.PR_REVIEW_RECEIVED])
        assert len(events) == 2

    def test_unknown_wire_event_type_is_noop(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        observe_pushed_event(
            repo="acme/widgets", pr_number=42, wire_event_type="bogus", payload={}
        )
        task = load_dev_queue().tasks[0]
        assert task.pr_state is None

    def test_uses_overlay_not_prebuilt_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """observe_pushed_event must call apply_pr_state_observation with
        overlay=, never a pre-built new_state= — passing new_state would mean
        the overlay was computed from a pre-lock snapshot, reintroducing the
        #930 TOCTOU lost-update bug (a concurrent writer between this
        function's initial task lookup and apply_pr_state_observation's lock
        acquisition would be silently clobbered).
        """
        save_dev_queue(
            DevQueueStore(
                tasks=[TicketTask(ticket_id="GEN-1", client="acme", pr_url=_URL)]
            )
        )
        captured: dict[str, Any] = {}

        def _fake_apply(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr("cw.pr_hydrate.apply_pr_state_observation", _fake_apply)
        observe_pushed_event(
            repo="acme/widgets", pr_number=42, wire_event_type="merged", payload={}
        )
        assert captured["client"] == "acme"
        assert captured["ticket_id"] == "GEN-1"
        assert callable(captured["overlay"])
        assert "new_state" not in captured


class TestApplyPrStateObservationOverlayFreshness:
    """apply_pr_state_observation's *overlay* callable must be invoked with
    the task state re-read INSIDE dev_queue_lock(), never a snapshot the
    caller captured earlier (#930 fix for observe_pushed_event's original
    TOCTOU lost-update bug: the push path used to build its full replacement
    PrState from an unlocked pre-lock read, then persist it unconditionally,
    silently discarding any write that landed in the meantime).
    """

    def test_overlay_sees_freshest_persisted_state(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url=_URL,
                        pr_state=_pr_state(ci_ok=True, review_decision=""),
                    )
                ]
            )
        )

        # Simulate a concurrent writer (e.g. a poll tick, or another webhook
        # delivery) landing a durable change to a DIFFERENT field.
        apply_pr_state_observation(
            client="acme",
            ticket_id="GEN-1",
            new_state=_pr_state(ci_ok=True, review_decision="APPROVED"),
        )

        def _overlay(old: PrState | None) -> PrState:
            assert old is not None
            # If apply_pr_state_observation regressed to invoking overlay()
            # against a pre-lock snapshot instead of the freshly-locked
            # state, this would observe the pre-concurrent-write value
            # ("") instead of "APPROVED".
            assert old.review_decision == "APPROVED", (
                "overlay received a stale baseline, not the freshly-locked "
                "state — the #930 TOCTOU fix has regressed"
            )
            return old.model_copy(update={"ci_ok": False})

        apply_pr_state_observation(client="acme", ticket_id="GEN-1", overlay=_overlay)

        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.ci_ok is False
        assert task.pr_state.review_decision == "APPROVED"

    def test_new_state_and_overlay_both_given_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            apply_pr_state_observation(
                client="acme",
                ticket_id="GEN-1",
                new_state=_pr_state(),
                overlay=lambda _old: _pr_state(),
            )

    def test_neither_new_state_nor_overlay_given_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            apply_pr_state_observation(client="acme", ticket_id="GEN-1")


def _watched(
    pr_number: int = 42,
    *,
    status: str = "active",
    source: str = "cli",
    pr_state: PrState | None = None,
) -> WatchedPr:
    return WatchedPr(
        pr_url=f"https://github.com/acme/widgets/pull/{pr_number}",
        repo="acme/widgets",
        pr_number=pr_number,
        source=source,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        pr_state=pr_state,
    )


class TestWatchedPrCounterpartyConstant:
    def test_watched_pr_counterparty_is_external(self) -> None:
        assert WATCHED_PR_COUNTERPARTY == "external"


class TestReviewerNodeLogin:
    def test_user_node_returns_login(self) -> None:
        assert _reviewer_node_login({"login": "alice"}) == "alice"

    def test_team_node_returns_none(self) -> None:
        assert _reviewer_node_login({"slug": "eng-team", "name": "Eng"}) is None

    def test_non_dict_returns_none(self) -> None:
        assert _reviewer_node_login("nope") is None

    def test_empty_login_returns_none(self) -> None:
        assert _reviewer_node_login({"login": ""}) is None


class TestHydrateWatchedPrs:
    """Watched-PR hydration inside hydrate_pr_states (R9)."""

    def test_hydrate_watched_prs_persists_pr_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[_watched()]))
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        hydrate_pr_states(OrchestratorConfig())
        watched = load_dev_queue().watched_prs[0]
        assert watched.pr_state is not None
        assert watched.pr_state.state == "OPEN"

    def test_hydrate_watched_prs_skips_dismissed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[_watched(status="dismissed")]))
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert calls == []
        assert load_dev_queue().watched_prs[0].pr_state is None

    def test_hydrate_watched_prs_runs_with_zero_task_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[], watched_prs=[_watched()]))
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        hydrate_pr_states(OrchestratorConfig())
        assert load_dev_queue().watched_prs[0].pr_state is not None

    def test_hydrate_watched_prs_transient_fetch_failure_leaves_prior_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prior = PrState(state="OPEN", review_decision="APPROVED")
        save_dev_queue(DevQueueStore(watched_prs=[_watched(pr_state=prior)]))
        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", lambda *_a, **_kw: None)
        hydrate_pr_states(OrchestratorConfig())
        watched = load_dev_queue().watched_prs[0]
        assert watched.pr_state is not None
        assert watched.pr_state.review_decision == "APPROVED"

    def test_hydrate_pr_states_does_not_touch_task_pr_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminal-state task is never re-hydrated when watched PRs exist."""
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        pr_url="https://github.com/acme/widgets/pull/1",
                        pr_state=PrState(
                            state="MERGED",
                            hydrated_at=datetime(2000, 1, 1, tzinfo=UTC),
                        ),
                    )
                ],
                watched_prs=[_watched()],
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: _pr_view_payload(state="OPEN"),
        )
        hydrate_pr_states(OrchestratorConfig())
        store = load_dev_queue()
        assert store.tasks[0].pr_state is not None
        assert store.tasks[0].pr_state.state == "MERGED"
        assert store.watched_prs[0].pr_state is not None

    def test_hydrate_watched_prs_direct_no_op_on_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling the helper with an empty list never fetches anything."""
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(),
        )
        _hydrate_watched_prs([], config=OrchestratorConfig(), self_login=None)
        assert calls == []

    def test_watched_pr_only_store_second_pass_within_interval_is_throttled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store with zero tasks still throttles watched-PR hydration (#1154 fix).

        Regression guard: ``_throttled`` used to sample only
        ``TicketTask.pr_state.hydrated_at``, so a watched-PR-only store (no
        tasks at all) was never throttled and refetched every active watched
        PR via ``gh pr view`` on every call.
        """
        save_dev_queue(DevQueueStore(tasks=[], watched_prs=[_watched()]))
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *a, **_k: calls.append(a) or _pr_view_payload(state="OPEN"),
        )
        config = OrchestratorConfig(pr_hydration_interval_seconds=150)
        with freeze_time("2026-07-04 12:00:00") as frozen:
            hydrate_pr_states(config)
            assert len(calls) == 1
            frozen.tick(delta=timedelta(seconds=60))
            hydrate_pr_states(config)
            assert len(calls) == 1  # throttled — no second fetch
            frozen.tick(delta=timedelta(seconds=120))
            hydrate_pr_states(config)
            assert len(calls) == 2  # interval elapsed — fetched again


class TestHydratePrStatesRepoOverride:
    """Per-repo operator-login override threading (RFC 0011 follow-up, #1171).

    Proves the override login -- not the once-resolved process login -- is
    what ``_derive_pr_state`` actually uses to exclude self-authored blocking
    comments, by making the comment author match one login but not the
    other: a wrong effective login fails to exclude it and the derived
    ``attention_state`` reads ``changes_requested`` instead of
    ``ready_to_approve``.
    """

    _WIDGETS_URL = "https://github.com/acme/widgets/pull/42"
    _OTHER_URL = "https://github.com/other/repo/pull/1"

    def _blocking_comment_payload(self, author_login: str) -> dict[str, Any]:
        return _pr_view_payload(
            reviewDecision="",
            mergeStateStatus="CLEAN",
            comments=[
                {
                    "author": {"login": author_login},
                    "body": "MUST_FIX: needs another pass",
                }
            ],
        )

    def test_task_repo_override_feeds_derive_pr_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1", client="acme", pr_url=self._WIDGETS_URL
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: self._blocking_comment_payload("override-user"),
        )
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: "process-user"
        )
        config = OrchestratorConfig(
            operator_github_login_by_repo={"acme/widgets": "override-user"}
        )
        hydrate_pr_states(config)
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        # Excluded as self only if the override login (not "process-user")
        # was actually threaded into _derive_pr_state's self_login.
        assert task.pr_state.attention_state == "ready_to_approve"

    def test_watched_pr_repo_override_feeds_derive_pr_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[_watched()]))
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: self._blocking_comment_payload("override-user"),
        )
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: "process-user"
        )
        config = OrchestratorConfig(
            operator_github_login_by_repo={"acme/widgets": "override-user"}
        )
        hydrate_pr_states(config)
        watched = load_dev_queue().watched_prs[0]
        assert watched.pr_state is not None
        assert watched.pr_state.attention_state == "ready_to_approve"

    def test_repo_absent_from_map_falls_back_to_once_resolved_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1", client="acme", pr_url=self._WIDGETS_URL
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.pr_hydrate.fetch_pr_view",
            lambda *_a, **_kw: self._blocking_comment_payload("process-user"),
        )
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: "process-user"
        )
        hydrate_pr_states(OrchestratorConfig())
        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.attention_state == "ready_to_approve"

    def test_two_tasks_different_repos_get_different_effective_logins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1", client="acme", pr_url=self._WIDGETS_URL
                    ),
                    TicketTask(
                        ticket_id="GEN-2", client="acme", pr_url=self._OTHER_URL
                    ),
                ]
            )
        )

        def _fetch(pr_url: str, *_a: object, **_kw: object) -> dict[str, Any]:
            if pr_url == self._WIDGETS_URL:
                # override-mapped repo: comment authored by the override login.
                return self._blocking_comment_payload("override-user")
            # unmapped repo: comment authored by the process (fallback) login.
            return self._blocking_comment_payload("process-user")

        monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", _fetch)
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: "process-user"
        )
        config = OrchestratorConfig(
            operator_github_login_by_repo={"acme/widgets": "override-user"}
        )
        hydrate_pr_states(config)
        store = load_dev_queue()
        widgets_task = next(t for t in store.tasks if t.ticket_id == "GEN-1")
        other_task = next(t for t in store.tasks if t.ticket_id == "GEN-2")
        assert widgets_task.pr_state is not None
        assert widgets_task.pr_state.attention_state == "ready_to_approve"
        assert other_task.pr_state is not None
        assert other_task.pr_state.attention_state == "ready_to_approve"


class TestResolveAndRegisterReviewRequest:
    """Shared decision function for CLI + webhook review-request registration."""

    _URL = "https://github.com/acme/widgets/pull/42"

    def _resolve(
        self,
        reviewer_nodes: list[dict[str, Any]],
        *,
        operator_login: str | None = "mattwwarren",
        source: str = "webhook",
        requester_login: str | None = None,
    ) -> tuple[bool, str]:
        return resolve_and_register_review_request(
            repo="acme/widgets",
            pr_number=42,
            pr_url=self._URL,
            reviewer_nodes=reviewer_nodes,
            operator_login=operator_login,
            source=source,  # type: ignore[arg-type]
            requester_login=requester_login,
        )

    def test_individual_node_matching_operator_registers(self) -> None:
        result = self._resolve([{"login": "mattwwarren"}])
        assert result == (True, "registered")
        assert len(load_dev_queue().watched_prs) == 1

    def test_team_node_ignored_with_reason(self) -> None:
        result = self._resolve([{"slug": "eng-team", "name": "Engineering"}])
        assert result == (False, "team_targeted")
        assert load_dev_queue().watched_prs == []

    def test_individual_node_not_matching_operator_ignored(self) -> None:
        result = self._resolve([{"login": "someone-else"}])
        assert result == (False, "not_operator_targeted")
        assert load_dev_queue().watched_prs == []

    def test_empty_reviewer_nodes_ignored(self) -> None:
        result = self._resolve([])
        assert result == (False, "no_reviewer")
        assert load_dev_queue().watched_prs == []

    def test_identity_unresolved_fails_closed(self) -> None:
        result = self._resolve([{"login": "mattwwarren"}], operator_login=None)
        assert result == (False, "identity_unresolved")
        assert load_dev_queue().watched_prs == []

    def test_already_registered_idempotent(self) -> None:
        assert self._resolve([{"login": "mattwwarren"}]) == (True, "registered")
        assert self._resolve([{"login": "mattwwarren"}]) == (
            False,
            "already_registered",
        )
        assert len(load_dev_queue().watched_prs) == 1

    def test_source_and_requester_login_persisted(self) -> None:
        self._resolve(
            [{"login": "mattwwarren"}],
            source="cli",
            requester_login="bob",
        )
        watched = load_dev_queue().watched_prs[0]
        assert watched.source == "cli"
        assert watched.requester_login == "bob"
