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
        msg = "subclass caught by base"
        with pytest.raises(CwError, match="subclass"):
            raise WorktreeError(msg)

    def test_disclaimer_not_accepted_is_cw_error(self) -> None:
        from cw.exceptions import DisclaimerNotAcceptedError

        assert issubclass(DisclaimerNotAcceptedError, CwError)

    def test_disclaimer_message_propagates(self) -> None:
        from cw.exceptions import DisclaimerNotAcceptedError

        err = DisclaimerNotAcceptedError("run interactively first")
        assert "interactively" in str(err)

    def test_duplicated_hunk_error_is_cw_error(self) -> None:
        """#1924: flat CwError subclass so `handle_errors` gives it exit 1."""
        from cw.exceptions import DuplicatedHunkError

        err = DuplicatedHunkError("src/cw/foo.py appears twice")
        assert isinstance(err, CwError)
        assert "src/cw/foo.py appears twice" in str(err)

    def test_placeholder_diff_error_is_cw_error(self) -> None:
        from cw.exceptions import PlaceholderDiffError

        err = PlaceholderDiffError("diff is the literal '<diff here>'")
        assert isinstance(err, CwError)
        assert "<diff here>" in str(err)

    def test_diff_base_mismatch_error_is_cw_error(self) -> None:
        from cw.exceptions import DiffBaseMismatchError

        err = DiffBaseMismatchError("payload diff differs from main...HEAD")
        assert isinstance(err, CwError)
        assert "main...HEAD" in str(err)

    def test_documents_from_read_error_is_cw_error(self) -> None:
        from cw.exceptions import DocumentsFromReadError

        err = DocumentsFromReadError("could not read reviewer-1.json")
        assert isinstance(err, CwError)
        assert "reviewer-1.json" in str(err)


class TestHookContextConflictError:
    """GitHub #1674: the error now carries the id of the conflicting session.

    Only the DAEMON-origin live-session raise site supplies it; the USER-origin
    settings-file raise site keeps the message-only call shape.
    """

    def test_carries_conflicting_session_id_when_provided(self) -> None:
        from cw.exceptions import HookContextConflictError

        err = HookContextConflictError("msg", conflicting_session_id="sess-1")

        assert err.conflicting_session_id == "sess-1"
        assert str(err) == "msg"

    def test_conflicting_session_id_defaults_to_none(self) -> None:
        from cw.exceptions import HookContextConflictError

        err = HookContextConflictError("msg")

        assert err.conflicting_session_id is None


class TestUsageLimitError:
    def test_usage_limit_error_is_cw_error(self) -> None:
        from cw.exceptions import UsageLimitError

        assert issubclass(UsageLimitError, CwError)

    def test_usage_limit_error_message_propagates(self) -> None:
        from cw.exceptions import UsageLimitError

        err = UsageLimitError("usage limit active")
        assert "usage limit" in str(err)


class TestUsageLimitRe:
    def test_matches_session_limit(self) -> None:
        from cw.exceptions import USAGE_LIMIT_RE

        assert USAGE_LIMIT_RE.search("You've hit your session limit · resets 3:45pm")

    def test_matches_weekly_limit(self) -> None:
        from cw.exceptions import USAGE_LIMIT_RE

        assert USAGE_LIMIT_RE.search(
            "You've hit your weekly limit · resets Mon 12:00am"
        )

    def test_matches_opus_limit(self) -> None:
        from cw.exceptions import USAGE_LIMIT_RE

        assert USAGE_LIMIT_RE.search("You've hit your Opus limit · resets 3:45pm")

    def test_no_match_hit_the_wall(self) -> None:
        from cw.exceptions import USAGE_LIMIT_RE

        assert USAGE_LIMIT_RE.search("hit the wall") is None

    def test_no_match_connection_limit(self) -> None:
        from cw.exceptions import USAGE_LIMIT_RE

        assert USAGE_LIMIT_RE.search("connection limit exceeded") is None
