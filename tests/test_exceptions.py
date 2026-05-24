"""Tests for cw.exceptions - exception hierarchy."""

from __future__ import annotations

import pytest

from cw.exceptions import CwError, WorktreeError


class TestExceptionHierarchy:
    def test_cw_error_is_exception(self) -> None:
        assert issubclass(CwError, Exception)

    def test_worktree_error_is_cw_error(self) -> None:
        assert issubclass(WorktreeError, CwError)

    def test_message_propagates(self) -> None:
        err = CwError("boom")
        assert str(err) == "boom"

    def test_worktree_message_propagates(self) -> None:
        err = WorktreeError("branch-missing")
        assert str(err) == "branch-missing"

    def test_raise_and_catch_as_cw_error(self) -> None:
        with pytest.raises(CwError, match="subclass"):
            raise WorktreeError("subclass caught by base")

    def test_disclaimer_not_accepted_is_cw_error(self) -> None:
        from cw.exceptions import DisclaimerNotAcceptedError

        assert issubclass(DisclaimerNotAcceptedError, CwError)

    def test_disclaimer_message_propagates(self) -> None:
        from cw.exceptions import DisclaimerNotAcceptedError

        err = DisclaimerNotAcceptedError("run interactively first")
        assert "interactively" in str(err)
