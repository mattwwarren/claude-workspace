"""Tests for cw.gh — GitHub CLI helpers."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from cw.gh import (
    add_pr_reviewer,
    branch_exists_on_origin,
    fetch_approved_plan_comment,
    fetch_pr_view,
    pr_exists_for_branch,
    pr_is_merged_for_ticket,
)

if TYPE_CHECKING:
    import pytest


def _make_run_result(returncode: int = 0, stdout: str = "") -> Any:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def _make_issue_result(pr_numbers: list[int]) -> Any:
    refs = [{"number": n} for n in pr_numbers]
    import json

    return _make_run_result(0, json.dumps({"closedByPullRequestsReferences": refs}))


def _make_pr_result(state: str) -> Any:
    import json

    return _make_run_result(0, json.dumps({"state": state}))


class TestPrIsMergedForTicket:
    """Tests for pr_is_merged_for_ticket."""

    def test_merged_pr_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single linked PR with state MERGED → (True, True)."""
        calls: list[list[str]] = []

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            calls.append(args)
            if "issue" in args:
                return _make_issue_result([42])
            return _make_pr_result("MERGED")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is True
        assert gh_available is True

    def test_open_pr_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single linked PR with state OPEN → (False, True)."""

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([42])
            return _make_pr_result("OPEN")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_no_linked_prs_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue with no linked PRs → (False, True)."""
        import json

        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(
                0, json.dumps({"closedByPullRequestsReferences": []})
            ),
        )
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_gh_not_found_returns_none_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh binary absent → (None, False)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("gh")),
        )
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is None
        assert gh_available is False

    def test_timeout_on_issue_returns_none_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TimeoutExpired on issue call → (None, True)."""

        def _raise(*_a: object, **_kw: object) -> None:
            raise subprocess.TimeoutExpired(["gh"], 10)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is None
        assert gh_available is True

    def test_nonzero_exit_on_issue_returns_none_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero returncode from gh issue view → (None, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(1, ""),
        )
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is None
        assert gh_available is True

    def test_malformed_json_issue_returns_none_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON from gh issue view → (None, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "not json"),
        )
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is None
        assert gh_available is True

    def test_multiple_refs_returns_true_on_first_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple linked PRs — returns True as soon as one is MERGED."""
        pr_states = {10: "CLOSED", 11: "MERGED", 12: "OPEN"}

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([10, 11, 12])
            pr_num = int(args[args.index("view") + 1])
            return _make_pr_result(pr_states[pr_num])

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is True
        assert gh_available is True

    def test_pr_view_timeout_skips_that_pr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TimeoutExpired on pr view skips that PR; exhausted → (False, True)."""

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([42])
            raise subprocess.TimeoutExpired(["gh"], 10)

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_closed_pr_is_not_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linked PR with state CLOSED → (False, True)."""

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([42])
            return _make_pr_result("CLOSED")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_ref_without_number_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ref dict with no 'number' key is skipped; exhausted → (False, True)."""
        import json

        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(
                0,
                json.dumps({"closedByPullRequestsReferences": [{"number": None}]}),
            ),
        )
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_nonzero_exit_on_pr_view_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero returncode from gh pr view skips that PR → (False, True)."""

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([42])
            return _make_run_result(1, "")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    def test_malformed_json_pr_view_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON from gh pr view skips that PR → (False, True)."""

        def _fake_run(args: list[str], **_kwargs: object) -> Any:
            if "issue" in args:
                return _make_issue_result([42])
            return _make_run_result(0, "not json")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487")
        assert merged is False
        assert gh_available is True

    # ------------------------------------------------------------------
    # Branch-keyed fallback tests (Linear / issue-link unsupported)
    # ------------------------------------------------------------------

    def test_linear_branch_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """(a) Linear: issue rc!=0, branch path finds merged PR -> (True, True)."""
        calls: list[list[str]] = []

        def _fake_run(args: list[str], **_kw: object) -> Any:
            calls.append(list(args))
            if "issue" in args:
                return _make_run_result(1, "")  # Linear ticket: not a GitHub issue
            # gh pr list --head ... --state merged -> one result
            return _make_run_result(0, json.dumps([{"number": 1}]))

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is True
        assert gh_available is True

    def test_linear_branch_not_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """(b) Linear: issue rc!=0, branch path finds no merged PR -> (False, True)."""

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")
            return _make_run_result(0, json.dumps([]))

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is False
        assert gh_available is True

    def test_github_issue_link_primary_branch_not_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(c) issue-link finds MERGED -> (True, True), branch path NOT called."""
        calls: list[list[str]] = []
        _branch_called_msg = "branch path must not be called when issue-link succeeds"

        def _fake_run(args: list[str], **_kw: object) -> Any:
            calls.append(list(args))
            if "issue" in args:
                return _make_issue_result([42])
            if "list" in args:
                raise AssertionError(_branch_called_msg)
            return _make_pr_result("MERGED")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("487", branch="dev/487")
        assert merged is True
        assert gh_available is True
        # Verify no "pr list" call was made (only "issue" + "pr view")
        assert not any("list" in call for call in calls)

    def test_linear_branch_gh_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """(d) refs is None, branch path FileNotFoundError -> (None, False)."""

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")  # refs=None
            _msg = "gh"
            raise FileNotFoundError(_msg)

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is None
        assert gh_available is False

    def test_linear_branch_transient_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(e) refs is None, branch path rc!=0 (transient) -> (None, True)."""

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")  # refs=None
            return _make_run_result(1, "")  # branch path transient error

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is None
        assert gh_available is True

    def test_linear_branch_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """refs is None, branch path TimeoutExpired -> (None, True)."""

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")  # refs=None
            raise subprocess.TimeoutExpired(["gh"], 10)

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is None
        assert gh_available is True

    def test_linear_branch_malformed_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """refs is None, branch path returns malformed JSON -> (None, True)."""

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")  # refs=None
            return _make_run_result(0, "not json")  # branch path parse fail

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403", branch="dev/GEN-403")
        assert merged is None
        assert gh_available is True

    def test_no_branch_refs_none_returns_none_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(f) refs is None, no branch arg -> (None, True) (regression guard)."""

        _no_branch_msg = "branch path must not be called when branch=None"

        def _fake_run(args: list[str], **_kw: object) -> Any:
            if "issue" in args:
                return _make_run_result(1, "")
            raise AssertionError(_no_branch_msg)

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        merged, gh_available = pr_is_merged_for_ticket("GEN-403")
        assert merged is None
        assert gh_available is True


class TestPrExistsForBranch:
    """Tests for pr_exists_for_branch."""

    def test_open_pr_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh pr list returns [{"number": 42}] → (True, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, json.dumps([{"number": 42}])),
        )
        exists, gh_available = pr_exists_for_branch("dev/497")
        assert exists is True
        assert gh_available is True

    def test_no_pr_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh pr list returns [] → (False, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, json.dumps([])),
        )
        exists, gh_available = pr_exists_for_branch("dev/497")
        assert exists is False
        assert gh_available is True

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TimeoutExpired → (None, True)."""

        def _raise(*_a: object, **_kw: object) -> None:
            raise subprocess.TimeoutExpired(["gh"], 10)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        exists, gh_available = pr_exists_for_branch("dev/497")
        assert exists is None
        assert gh_available is True

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-zero returncode → (None, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(1, ""),
        )
        exists, gh_available = pr_exists_for_branch("dev/497")
        assert exists is None
        assert gh_available is True

    def test_gh_absent_returns_none_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FileNotFoundError → (None, False)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("gh")),
        )
        exists, gh_available = pr_exists_for_branch("dev/497")
        assert exists is None
        assert gh_available is False


class TestBranchExistsOnOrigin:
    """Tests for branch_exists_on_origin / _fetch_branch_exists_on_origin."""

    def test_branch_present_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """returncode 0 → (True, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "{}"),
        )
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is True
        assert gh_available is True

    def test_branch_absent_404_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero with HTTP 404 in output → (False, True)."""
        result = _make_run_result(1, "")
        result.stderr = "error: HTTP 404: Not Found"
        monkeypatch.setattr("cw.gh._sp.run", lambda *_a, **_kw: result)
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is False
        assert gh_available is True

    def test_branch_absent_not_found_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero with '"Not Found"' in output → (False, True)."""
        result = _make_run_result(1, '{"message": "Not Found"}')
        result.stderr = ""
        monkeypatch.setattr("cw.gh._sp.run", lambda *_a, **_kw: result)
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is False
        assert gh_available is True

    def test_unknown_nonzero_returns_none_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero with unrecognized output → transient (None, True)."""
        result = _make_run_result(1, "")
        result.stderr = "some other error"
        monkeypatch.setattr("cw.gh._sp.run", lambda *_a, **_kw: result)
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is None
        assert gh_available is True

    def test_file_not_found_returns_none_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FileNotFoundError (gh absent) → (None, False)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("gh")),
        )
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is None
        assert gh_available is False

    def test_os_error_returns_none_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError (transient) → (None, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("pipe error")),
        )
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is None
        assert gh_available is True

    def test_timeout_returns_none_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TimeoutExpired → (None, True)."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("gh", 10)
            ),
        )
        exists, gh_available = branch_exists_on_origin("dev/808")
        assert exists is None
        assert gh_available is True


class TestFetchApprovedPlanComment:
    """Tests for fetch_approved_plan_comment."""

    def _make_comments_result(self, comments: list[dict[str, str]]) -> Any:
        return _make_run_result(0, json.dumps({"comments": comments}))

    def _make_dispatched_run(
        self,
        comments: list[dict[str, Any]],
        identity: str | int | BaseException,
        calls: list[list[str]] | None = None,
    ) -> Any:
        """Fake ``_sp.run`` that routes by which gh subcommand was invoked.

        The comments-fetch call (``"comments" in args``) returns *comments*.
        The identity-fetch call (``args[:3] == ["gh", "api", "user"]``)
        returns/raises *identity*:
        - str: successful login (used as stdout)
        - int: non-zero returncode (gh api user failed)
        - BaseException instance: raised (FileNotFoundError, TimeoutExpired)
        """

        def _fake_run(args: list[str], **_kw: object) -> Any:
            argv = list(args)
            if calls is not None:
                calls.append(argv)
            if "comments" in argv:
                return self._make_comments_result(comments)
            if argv[:3] == ["gh", "api", "user"]:
                if isinstance(identity, BaseException):
                    raise identity
                if isinstance(identity, int):
                    return _make_run_result(identity, "")
                return _make_run_result(0, identity)
            msg = f"unexpected _sp.run args: {argv}"
            raise AssertionError(msg)

        return _fake_run

    def test_returns_latest_comment_with_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Latest comment carrying <!-- plan-spec-reviewed is returned."""
        plan_body = (
            "## Implementation Plan\n\nDo the thing."
            "\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        )
        comments = [
            {"body": "First comment, no marker"},
            {"body": plan_body, "author": {"login": "mattwwarren"}},
        ]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result == plan_body

    def test_returns_latest_when_multiple_plan_comments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple plan comments — latest (last in list) wins."""
        old_plan = "old plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        new_plan = "new plan <!-- plan-spec-reviewed: 2026-01-02 v2 -->"
        comments = [
            {"body": old_plan, "author": {"login": "mattwwarren"}},
            {"body": new_plan, "author": {"login": "mattwwarren"}},
        ]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result == new_plan

    def test_no_matching_comments_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Comments with no plan marker → None."""
        comments = [{"body": "just a regular comment"}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: self._make_comments_result(comments),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_comment_missing_body_key_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A comment dict with no 'body' key at all is skipped, not a crash."""
        comments: list[dict[str, Any]] = [{"id": "no-body-field"}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: self._make_comments_result(comments),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_empty_comments_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue with no comments → None."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: self._make_comments_result([]),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_gh_nonzero_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh exits non-zero → None."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(1, ""),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_gh_not_found_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FileNotFoundError (gh absent) → None."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("gh")),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TimeoutExpired → None."""

        def _raise(*_a: object, **_kw: object) -> None:
            raise subprocess.TimeoutExpired(["gh"], 30)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unparseable JSON response → None."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "not json"),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_passes_ticket_id_to_gh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ticket_id is forwarded as the issue number arg to gh."""
        captured: list[list[str]] = []

        def _fake_run(args: list[str], **_kw: object) -> Any:
            captured.append(list(args))
            return self._make_comments_result([])

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        fetch_approved_plan_comment("42")
        assert len(captured) == 1
        assert "42" in captured[0]

    def test_author_match_returns_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Marker comment authored by the trusted identity is returned."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": {"login": "mattwwarren"}}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result == plan_body

    def test_author_mismatch_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Marker comment authored by someone else is never trusted."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": {"login": "attacker"}}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_author_mismatch_falls_through_to_older_trusted_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An untrusted marker comment is skipped, not scan-terminating."""
        old_trusted = "old plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        new_untrusted = "new plan <!-- plan-spec-reviewed: 2026-01-02 v2 -->"
        comments = [
            {"body": old_trusted, "author": {"login": "mattwwarren"}},
            {"body": new_untrusted, "author": {"login": "attacker"}},
        ]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result == old_trusted

    def test_identity_fetch_failure_returns_none_when_marker_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity resolution failing (non-zero exit) fails closed."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": {"login": "mattwwarren"}}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, 1),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_identity_fetch_not_called_when_no_marker_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No marker-bearing comment → identity lookup is never issued."""
        comments = [{"body": "just a regular comment"}]
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren", calls),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None
        assert len(calls) == 1

    def test_author_field_missing_treated_as_untrusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Marker comment with no author key at all is skipped, not trusted."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_author_field_non_dict_treated_as_untrusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed (non-dict) author field is skipped, not a crash."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": "mattwwarren"}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, "mattwwarren"),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_identity_gh_not_found_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh binary vanishing during the identity call fails closed."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": {"login": "mattwwarren"}}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, FileNotFoundError("gh")),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None

    def test_identity_timeout_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity call timing out fails closed."""
        plan_body = "plan <!-- plan-spec-reviewed: 2026-01-01 v1 -->"
        comments = [{"body": plan_body, "author": {"login": "mattwwarren"}}]
        monkeypatch.setattr(
            "cw.gh._sp.run",
            self._make_dispatched_run(comments, subprocess.TimeoutExpired(["gh"], 30)),
        )
        result = fetch_approved_plan_comment("896")
        assert result is None


_PR_VIEW_FIELDS = (
    "state,mergeable,mergeStateStatus,statusCheckRollup,"
    "reviewDecision,isDraft,reviewRequests"
)


def _make_pr_view_result(**fields: Any) -> Any:
    """Build a gh pr view --json result with permissive defaults (superset of
    _make_pr_result — do NOT overload the narrow single-field helper)."""
    payload: dict[str, Any] = {
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "reviewDecision": "",
        "isDraft": False,
        "reviewRequests": [],
    }
    payload.update(fields)
    return _make_run_result(0, json.dumps(payload))


class TestFetchPrView:
    def test_success_returns_parsed_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_pr_view_result(state="OPEN"),
        )
        result = fetch_pr_view("https://github.com/acme/widgets/pull/42")
        assert result is not None
        assert result["state"] == "OPEN"
        assert result["mergeStateStatus"] == "CLEAN"

    def test_argv_carries_exact_field_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def _fake_run(args: list[str], **_kw: object) -> Any:
            captured.append(list(args))
            return _make_pr_view_result()

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        fetch_pr_view("https://github.com/acme/widgets/pull/42")
        assert len(captured) == 1
        argv = captured[0]
        assert argv[:3] == ["gh", "pr", "view"]
        assert argv[3] == "https://github.com/acme/widgets/pull/42"
        assert "--json" in argv
        assert argv[argv.index("--json") + 1] == _PR_VIEW_FIELDS

    def test_nonzero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(1, ""),
        )
        assert fetch_pr_view("https://github.com/acme/widgets/pull/42") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            raise subprocess.TimeoutExpired(cmd="gh", timeout=15)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        assert fetch_pr_view("https://github.com/acme/widgets/pull/42") is None

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "not json"),
        )
        assert fetch_pr_view("https://github.com/acme/widgets/pull/42") is None

    def test_missing_gh_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            msg = "gh"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        assert fetch_pr_view("https://github.com/acme/widgets/pull/42") is None

    def test_non_dict_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid JSON that isn't an object (e.g. a bare array) must not be
        returned as-is — callers assume a dict and would raise AttributeError
        on .get() otherwise, breaking the "None on ANY failure" contract."""
        monkeypatch.setattr(
            "cw.gh._sp.run",
            lambda *_a, **_kw: _make_run_result(0, "[1, 2, 3]"),
        )
        assert fetch_pr_view("https://github.com/acme/widgets/pull/42") is None


class TestAddPrReviewer:
    """add_pr_reviewer mirrors post_issue_comment: policy-free, None on failure."""

    _PR_URL = "https://github.com/acme/widgets/pull/42"

    def test_success_returns_completed_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = _make_run_result(0, "")
        monkeypatch.setattr("cw.gh._sp.run", lambda *_a, **_kw: sentinel)
        result = add_pr_reviewer(self._PR_URL, "alice")
        assert result is sentinel

    def test_argv_carries_pr_url_and_reviewer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def _fake_run(args: list[str], **_kw: object) -> Any:
            captured.append(list(args))
            return _make_run_result(0, "")

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)
        add_pr_reviewer(self._PR_URL, "org/core")
        assert len(captured) == 1
        argv = captured[0]
        assert argv[:3] == ["gh", "pr", "edit"]
        assert self._PR_URL in argv
        assert argv[argv.index("--add-reviewer") + 1] == "org/core"

    def test_gh_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            msg = "gh"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        assert add_pr_reviewer(self._PR_URL, "alice") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> Any:
            raise subprocess.TimeoutExpired(["gh"], 30)

        monkeypatch.setattr("cw.gh._sp.run", _raise)
        assert add_pr_reviewer(self._PR_URL, "alice") is None
