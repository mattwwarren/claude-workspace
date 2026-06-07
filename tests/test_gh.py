"""Tests for cw.gh — GitHub CLI helpers."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from cw.gh import pr_exists_for_branch, pr_is_merged_for_ticket

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
