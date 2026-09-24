"""Tests for cw.exceptions - exception hierarchy."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cw.exceptions import CwError, WorktreeError

_NY = ZoneInfo("America/New_York")
_KOLKATA = ZoneInfo("Asia/Kolkata")
# 2026-09-20 is a Sunday; 2026-09-21 a Monday; 2026-09-23 a Wednesday.
_SUNDAY_10AM_NY = datetime(2026, 9, 20, 10, 0, tzinfo=_NY)
# Bidirectional-override characters, built via chr() so this source file itself
# stays free of embedded bidi control characters (ruff PLE2502).
_RTL_OVERRIDE = chr(0x202E)
_RTL_EMBEDDING = chr(0x202B)
# Arabic-Indic digit three: int() would happily accept it, so the parser's
# regex must use an explicit [0-9] class rather than \d.
_ARABIC_INDIC_THREE = chr(0x0663)


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

    def test_claim_tier_arming_error_is_cw_error(self) -> None:
        """#2232: a flat CwError subclass, so the daemon's broad catch sees it."""
        from cw.exceptions import ClaimTierArmingError

        assert issubclass(ClaimTierArmingError, CwError)

    def test_claim_tier_arming_message_names_both_settings(self) -> None:
        """The refusal has to say what is wrong AND what to change (#2232)."""
        from cw.exceptions import ClaimTierArmingError

        err = ClaimTierArmingError(
            "disposition_drift_check_enabled resolves to False, but "
            "codex_claim_suppression_enabled arms the claim tier"
        )
        assert "disposition_drift_check_enabled" in str(err)
        assert "codex_claim_suppression_enabled" in str(err)

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


class TestAmbiguousSessionIdentifierError:
    """#2237: an ambiguous session-name match lists every candidate."""

    def test_message_lists_each_candidate_and_ends_with_hint(self) -> None:
        from cw.exceptions import AmbiguousSessionIdentifierError
        from cw.models import SessionStatus
        from tests.conftest import _make_daemon_session

        started_old = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
        started_new = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)
        candidates = [
            _make_daemon_session(
                id="aaaa1111", status=SessionStatus.COMPLETED, started_at=started_old
            ),
            _make_daemon_session(
                id="bbbb2222", status=SessionStatus.ACTIVE, started_at=started_new
            ),
        ]

        err = AmbiguousSessionIdentifierError("client-a/auto-dev/T-1", candidates)

        message = str(err)
        assert "client-a/auto-dev/T-1" in message
        assert f"aaaa1111  completed  started {started_old.isoformat()}" in message
        assert f"bbbb2222  active  started {started_new.isoformat()}" in message
        assert message.endswith("pass an id to choose")
        assert err.identifier == "client-a/auto-dev/T-1"
        assert [s.id for s in err.candidates] == ["aaaa1111", "bbbb2222"]
        assert isinstance(err, CwError)


class TestBranchHeldByWorktreeError:
    """#2034: a foreign worktree squats the requested branch.

    Modeled on TestHookContextConflictError above — a WorktreeError subclass
    that carries the extra field the raise site needs (here, the path of the
    worktree holding the branch) rather than forcing callers to re-parse the
    message.
    """

    def test_is_worktree_error_subclass(self) -> None:
        from cw.exceptions import BranchHeldByWorktreeError

        assert issubclass(BranchHeldByWorktreeError, WorktreeError)

    def test_carries_holder_path(self) -> None:
        from pathlib import Path

        from cw.exceptions import BranchHeldByWorktreeError

        err = BranchHeldByWorktreeError("msg", holder_path=Path("/x"))

        assert err.holder_path == Path("/x")
        assert str(err) == "msg"


class TestWorktreeOccupiedError:
    """#2213 round 5: a live occupant refuses the reuse refresh as a type a
    caller cannot ignore. Modeled on BranchHeldByWorktreeError above.

    It is a WorktreeError so a broad ``except WorktreeError`` still contains it,
    but deliberately NOT a StaleWorktreeError: the dispatch claim path removes a
    stale worktree on that branch, and an occupied one must never be removed.
    """

    def test_is_worktree_error_but_not_stale_worktree_error(self) -> None:
        from cw.exceptions import StaleWorktreeError, WorktreeOccupiedError

        assert issubclass(WorktreeOccupiedError, WorktreeError)
        assert not issubclass(WorktreeOccupiedError, StaleWorktreeError)

    def test_carries_path_and_reason(self) -> None:
        from pathlib import Path

        from cw.exceptions import WorktreeOccupiedError

        err = WorktreeOccupiedError("msg", path=Path("/x"), reason="a live session")

        assert err.path == Path("/x")
        assert err.reason == "a live session"
        assert str(err) == "msg"


class TestRemoteRefUnresolvedError:
    """#2209: a typed CwError subclass so fix_dispatch can discriminate the
    unresolvable-remote-ref class without matching message text.

    Every other dispatch failure keeps the generic clear-and-revert path; this
    one parks the row BLOCKED_ON_USER instead, and that split is only safe if
    the class is nameable.
    """

    def test_is_cw_error_subclass(self) -> None:
        from cw.exceptions import RemoteRefUnresolvedError

        assert issubclass(RemoteRefUnresolvedError, CwError)

    def test_message_propagates(self) -> None:
        from cw.exceptions import RemoteRefUnresolvedError

        err = RemoteRefUnresolvedError("cannot determine remote ref for dev/2209")

        assert str(err) == "cannot determine remote ref for dev/2209"

    def test_caught_by_base_cw_error(self) -> None:
        from cw.exceptions import RemoteRefUnresolvedError

        msg = "no upstream configured"
        with pytest.raises(CwError, match="no upstream configured"):
            raise RemoteRefUnresolvedError(msg)


class TestUsageLimitError:
    def test_usage_limit_error_is_cw_error(self) -> None:
        from cw.exceptions import UsageLimitError

        assert issubclass(UsageLimitError, CwError)

    def test_usage_limit_error_message_propagates(self) -> None:
        from cw.exceptions import UsageLimitError

        err = UsageLimitError("usage limit active")
        assert "usage limit" in str(err)

    def test_reset_at_defaults_to_none(self) -> None:
        """#1409: every pre-existing raiser keeps the message-only call shape."""
        from cw.exceptions import UsageLimitError

        err = UsageLimitError("usage limit active")
        assert err.reset_at is None

    def test_reset_at_accepted_as_keyword(self) -> None:
        """#1409: keyword-only slot, mirroring BranchHeldByWorktreeError."""
        from cw.exceptions import UsageLimitError

        reset_at = datetime(2026, 9, 20, 19, 45, tzinfo=UTC)
        err = UsageLimitError("usage limit active", reset_at=reset_at)

        assert err.reset_at == reset_at
        assert str(err) == "usage limit active"

    def test_runtime_type_hints_resolve(self) -> None:
        """#1409 review round 1: `datetime` must import at RUNTIME.

        Under ``from __future__ import annotations`` every annotation is a
        string, so a TYPE_CHECKING-only ``datetime`` import makes
        ``typing.get_type_hints()`` raise NameError on anything that mentions
        it — which breaks every runtime introspector (pydantic, dataclasses,
        Click type inference, doc tooling) pointed at this module.
        """
        from typing import get_type_hints

        from cw.exceptions import UsageLimitError, parse_usage_limit_reset

        parser_hints = get_type_hints(parse_usage_limit_reset)
        init_hints = get_type_hints(UsageLimitError.__init__)

        assert parser_hints["now"] is datetime
        assert parser_hints["return"] == datetime | None
        assert init_hints["reset_at"] == datetime | None


class TestParseUsageLimitReset:
    """#1409: pull the reset instant out of a spawn-time usage-limit message.

    **The fixtures below are NOT captures of a real ``claude --bg`` spawn-time
    message.** No such capture exists on this host (plan decision P1); they are
    derived from *interactive* Claude transcript wording (``resets 3:45pm``,
    ``resets Mon 12:00am``, ``resets 11pm (America/New_York)``). If the real
    spawn-time text differs, the parser returns None and dispatch keeps today's
    flat ``usage_limit_backoff_seconds`` window.
    """

    @pytest.mark.parametrize(
        ("text", "now", "expected"),
        [
            pytest.param(
                "You've hit your session limit · resets 3:45pm",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 15, 45, tzinfo=_NY),
                id="same-day-future",
            ),
            pytest.param(
                "You've hit your Opus limit · resets 3:45pm",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 15, 45, tzinfo=_NY),
                id="opus-variant",
            ),
            pytest.param(
                "You've hit your weekly limit · resets 11pm (America/New_York)",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 23, 0, tzinfo=_NY),
                id="minute-less-with-zone-annotation",
            ),
            pytest.param(
                "You've hit your session limit · resets 3:40am (America/New_York)",
                datetime(2026, 9, 20, 1, 0, tzinfo=_KOLKATA),
                datetime(2026, 9, 20, 3, 40, tzinfo=_KOLKATA),
                id="trailing-zone-ignored-R1",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Mon 12:00am",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 21, 0, 0, tzinfo=_NY),
                id="weekday-tomorrow",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Mon 12:00am",
                datetime(2026, 9, 23, 10, 0, tzinfo=_NY),
                datetime(2026, 9, 28, 0, 0, tzinfo=_NY),
                id="weekday-next-week",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Mon 5pm",
                datetime(2026, 9, 21, 9, 0, tzinfo=_NY),
                datetime(2026, 9, 21, 17, 0, tzinfo=_NY),
                id="same-weekday-later-today",
            ),
            pytest.param(
                "You've hit your session limit · RESETS 3:45PM",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 15, 45, tzinfo=_NY),
                id="case-insensitive",
            ),
            pytest.param(
                "You've hit your session limit · resets 11:05am\n"
                "You've hit your weekly limit · resets 3:45pm",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 15, 45, tzinfo=_NY),
                id="last-occurrence-wins-R5",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sep 26, 11pm (America/New_York)",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 26, 23, 0, tzinfo=_NY),
                id="month-day-mid-turn-wording-2324",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sept 21 9:30am",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 21, 9, 30, tzinfo=_NY),
                id="month-day-long-month-no-comma",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Jan 2, 5pm",
                datetime(2026, 12, 30, 12, 0, tzinfo=_NY),
                datetime(2027, 1, 2, 17, 0, tzinfo=_NY),
                id="month-day-rolls-into-next-year",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sep 20, 11pm",
                _SUNDAY_10AM_NY,
                datetime(2026, 9, 20, 23, 0, tzinfo=_NY),
                id="month-day-today-later",
            ),
        ],
    )
    def test_resolves_expected_instant(
        self, text: str, now: datetime, expected: datetime
    ) -> None:
        from cw.exceptions import parse_usage_limit_reset

        result = parse_usage_limit_reset(text, now=now)

        assert result == expected

    @pytest.mark.parametrize(
        ("text", "now", "expected"),
        [
            pytest.param(
                "You've hit your weekly limit · resets Sun 10:00am",
                datetime(2026, 3, 7, 10, 0, tzinfo=_NY),
                datetime(2026, 3, 8, 14, 0, tzinfo=UTC),
                id="spring-forward-uses-the-target-date-offset",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sun 1:30am",
                datetime(2026, 10, 31, 10, 0, tzinfo=_NY),
                datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
                id="fall-back-ambiguous-hour-takes-the-later-instant",
            ),
        ],
    )
    def test_dst_transition_resolution(
        self, text: str, now: datetime, expected: datetime
    ) -> None:
        """#1409 review round 1: the offset belongs to the RESET's own date.

        Spring forward (2026-03-08): Saturday is EST (-05:00), Sunday is EDT
        (-04:00), so a Sunday 10:00 reset is 14:00Z — resolving it at
        Saturday's offset would name 15:00Z, an hour LATE, which keeps
        dispatch parked past the real reset.

        Fall back (2026-11-01): 01:30 occurs twice. The parser pins
        ``fold=1``, the SECOND (EST, 06:30Z) occurrence — the conservative
        direction, since the first (05:30Z) would reopen the spawn gate an
        hour before the limit lifts and the re-hit costs a real attempt.
        """
        from cw.exceptions import parse_usage_limit_reset

        assert parse_usage_limit_reset(text, now=now) == expected

    def test_returns_utc_aware_datetime(self) -> None:
        from cw.exceptions import parse_usage_limit_reset

        result = parse_usage_limit_reset(
            "You've hit your session limit · resets 3:45pm", now=_SUNDAY_10AM_NY
        )

        assert result is not None
        assert result.tzinfo is UTC

    @pytest.mark.parametrize(
        ("text", "now"),
        [
            pytest.param(
                "You've hit your session limit · resets 3:45pm",
                datetime(2026, 9, 20, 16, 0, tzinfo=_NY),
                id="same-day-already-passed-R2",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Mon 12:00am",
                datetime(2026, 9, 21, 0, 5, tzinfo=_NY),
                id="weekday-today-already-passed-inverted-R3",
            ),
            pytest.param(
                "You've hit your session limit · resets 3:45pm",
                datetime(2026, 9, 20, 15, 45, tzinfo=_NY),
                id="candidate-exactly-now",
            ),
            pytest.param(
                "You've hit your 5-hour limit",
                _SUNDAY_10AM_NY,
                id="limit-phrase-without-reset-fragment",
            ),
            pytest.param(
                "You've hit your session limit · resets",
                _SUNDAY_10AM_NY,
                id="resets-without-time",
            ),
            pytest.param(
                "You've hit your session limit · resets 25:99pm",
                _SUNDAY_10AM_NY,
                id="hour-and-minute-out-of-range",
            ),
            pytest.param(
                "You've hit your session limit · resets 13pm",
                _SUNDAY_10AM_NY,
                id="hour-out-of-range",
            ),
            pytest.param(
                "You've hit your session limit · resets 0:30am",
                _SUNDAY_10AM_NY,
                id="zero-hour-rejected",
            ),
            pytest.param(
                "You've hit your session limit · resets 3:60pm",
                _SUNDAY_10AM_NY,
                id="minute-out-of-range",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Mon",
                _SUNDAY_10AM_NY,
                id="weekday-without-time",
            ),
            pytest.param(
                "You've hit your session limit · resets Xyz 3pm",
                _SUNDAY_10AM_NY,
                id="unknown-weekday-token",
            ),
            pytest.param(
                f"You've hit your session limit · resets {_ARABIC_INDIC_THREE}pm",
                _SUNDAY_10AM_NY,
                id="non-ascii-digits-rejected",
            ),
            pytest.param(
                "no limit phrase here · resets 3:45pm",
                _SUNDAY_10AM_NY,
                id="no-usage-limit-anchor",
            ),
            pytest.param(
                "You've hit your session limit" + "." * 200 + "resets 3:45pm",
                _SUNDAY_10AM_NY,
                id="fragment-beyond-scan-window",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Feb 30, 3pm",
                _SUNDAY_10AM_NY,
                id="month-day-impossible-date",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Oct 30, 11pm",
                _SUNDAY_10AM_NY,
                id="month-day-beyond-seven-days",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sep 19, 11pm",
                _SUNDAY_10AM_NY,
                id="month-day-already-passed-never-rolls-a-year",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sep 20, 9am",
                _SUNDAY_10AM_NY,
                id="month-day-today-already-passed",
            ),
            pytest.param(
                "You've hit your weekly limit · resets Sep, 11pm",
                _SUNDAY_10AM_NY,
                id="month-without-day",
            ),
        ],
    )
    def test_returns_none(self, text: str, now: datetime) -> None:
        from cw.exceptions import parse_usage_limit_reset

        assert parse_usage_limit_reset(text, now=now) is None

    def test_naive_now_returns_none(self) -> None:
        """The function stays total rather than raising on a naive clock."""
        from cw.exceptions import parse_usage_limit_reset

        naive_now = _SUNDAY_10AM_NY.replace(tzinfo=None)

        assert (
            parse_usage_limit_reset(
                "You've hit your session limit · resets 3:45pm", now=naive_now
            )
            is None
        )

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("x" * 1_048_576, id="1MiB-junk"),
            pytest.param("hit " * 300_000, id="repeated-hit"),
            pytest.param("hit " + "a" * 1_000_000, id="hit-plus-long-run"),
            pytest.param(
                f"You've hit your session limit \x00{_RTL_OVERRIDE}\x07 resets 3:45pm",
                id="control-and-rtl-characters",
            ),
            pytest.param(
                "You've hit your session limit · resets 3:45pm " * 5_000,
                id="many-limit-phrases",
            ),
        ],
    )
    def test_adversarial_input_completes_without_raising(self, text: str) -> None:
        from cw.exceptions import parse_usage_limit_reset

        result = parse_usage_limit_reset(text, now=_SUNDAY_10AM_NY)

        assert result is None or result.tzinfo is not None

    def test_deterministic_fuzz_never_raises_and_stays_in_window(self) -> None:
        """RNG-free fuzz: ``itertools.product``, never ``random`` (S311)."""
        from cw.exceptions import parse_usage_limit_reset

        now = _SUNDAY_10AM_NY
        horizon = now + timedelta(days=7)
        phrases = [
            "You've hit your session limit",
            "You've hit your weekly limit",
            "You've hit your Opus limit",
            "hit your 5-hour limit",
            "no limit phrase here",
        ]
        fragments = [
            "resets 3:45pm",
            "resets Mon 12:00am",
            "resets 11pm (America/New_York)",
            "resets 25:99pm",
            "resets",
            "resets Xyz 3pm",
            "RESETS 3:45PM",
            f"resets {_ARABIC_INDIC_THREE}pm",
            f"resets 3:45pm\x00{_RTL_EMBEDDING}",
        ]
        separators = [" · ", " ", "\n", " \x1b[36m ", "", " -- "]
        truncations = [0, 1, 3, 7, 13, 25, 40, 96, 200]

        for phrase, fragment, sep, cut in itertools.product(
            phrases, fragments, separators, truncations
        ):
            raw = f"{phrase}{sep}{fragment}"
            text = raw[: max(0, len(raw) - cut)]
            result = parse_usage_limit_reset(text, now=now)
            if result is not None:
                assert result.tzinfo is not None
                assert now < result <= horizon


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
