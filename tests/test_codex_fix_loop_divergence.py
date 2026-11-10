"""Tests for cw.codex_fix_loop.divergence —the fix-loop divergence guard (#2394).

The #1837 admission gate always admits a MUST_FIX anchored in the latest delta,
so a fix loop that keeps inventing new code to address its own findings never
converges and burns every cycle growing the diff. These tests lock in the
loop-wide progress guard: it trips only when the loop has resolved none of the
originally-found MUST_FIX findings for consecutive cycles AND has grown the
diff past a size threshold.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import divergence
from cw.codex_fix_loop.convergence import _open_finding_key
from cw.codex_fix_loop.divergence import (
    _DIVERGENCE_ANCHOR_RATIO,
    _DIVERGENCE_MIN_LINES,
    _DIVERGENCE_PRE_LOOP_FRACTION,
    _DIVERGENCE_RELATIVE_MIN_LINES,
    _DIVERGENCE_STALL_CYCLES,
    DivergenceState,
    attributed_new_finding_count,
    divergence_threshold,
    emit_divergence_event,
    initial_divergence_state,
    is_diverging,
    loop_generated_findings,
    net_lines_for_commit,
    record_divergence_cycle,
    render_divergence_report,
    total_anchor_lines,
)
from cw.codex_review import _parse_unified_diff
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_findings import AcceptedFinding
from tests._codex_review_helpers import _write
from tests.conftest import _make_finding, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop.convergence import _OpenFindingKey

_KEY_A: _OpenFindingKey = ("a.py", "first finding")
_KEY_B: _OpenFindingKey = ("b.py", "second finding")
_KEY_C: _OpenFindingKey = ("c.py", "third finding")
_SELF_INFLICTED: _OpenFindingKey = ("fix1.py", "self-inflicted finding")
_ORIGINAL = frozenset({_KEY_A, _KEY_B, _KEY_C})
_PRE_LOOP_SHA = "abc1234def"
# Big enough per cycle that two cycles clear the floor, small enough that one
# cycle alone does not.
_BIG_CYCLE_LINES = _DIVERGENCE_MIN_LINES // 2 + 10
_TICKET = "T-2394"


# #2633 lowered the default stall count to 1; the legacy sequences below keep
# their two-cycle semantics by passing it explicitly.
_LEGACY_STALL_CYCLES = 2


def _state(
    pre_loop_diff_lines: int = 10, stall_cycles: int = _LEGACY_STALL_CYCLES
) -> DivergenceState:
    return initial_divergence_state(
        original_keys=_ORIGINAL,
        pre_loop_diff_lines=pre_loop_diff_lines,
        pre_loop_head_sha=_PRE_LOOP_SHA,
        stall_cycles=stall_cycles,
    )


def _step(
    state: DivergenceState,
    *,
    cycle: int,
    pre: frozenset[_OpenFindingKey],
    post: frozenset[_OpenFindingKey],
    lines: int,
    loop_attributed: int = 0,
) -> DivergenceState:
    return record_divergence_cycle(
        state,
        cycle=cycle,
        pre_open_keys=pre,
        post_open_keys=post,
        net_lines_added=lines,
        loop_attributed=loop_attributed,
    )


class TestDivergenceThresholds:
    def test_thresholds_match_the_documented_defaults(self) -> None:
        assert _DIVERGENCE_STALL_CYCLES == 1
        assert _DIVERGENCE_MIN_LINES == 150
        assert _DIVERGENCE_PRE_LOOP_FRACTION == 0.5


class TestRecordDivergenceCycle:
    def test_converging_sequence_never_trips(self) -> None:
        # Resolves one original finding every other cycle while the diff grows
        # hard every cycle — the stall streak never reaches the trip count.
        state = _state()
        open_keys = _ORIGINAL
        schedule = [
            (1, frozenset({_KEY_A, _KEY_B})),
            (2, frozenset({_KEY_A, _KEY_B})),
            (3, frozenset({_KEY_A})),
            (4, frozenset({_KEY_A})),
            (5, frozenset()),
        ]
        for cycle, post in schedule:
            state = _step(
                state,
                cycle=cycle,
                pre=open_keys,
                post=post,
                lines=_BIG_CYCLE_LINES,
            )
            open_keys = post
            assert is_diverging(state) is False

        assert state.cumulative_net_lines_added > _DIVERGENCE_MIN_LINES

    def test_diverging_sequence_trips_at_configured_cycle(self) -> None:
        state = _state()
        state = _step(
            state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=_BIG_CYCLE_LINES
        )
        assert state.stall_streak == 1
        assert is_diverging(state) is False

        state = _step(
            state, cycle=2, pre=_ORIGINAL, post=_ORIGINAL, lines=_BIG_CYCLE_LINES
        )
        assert state.stall_streak == _LEGACY_STALL_CYCLES
        assert is_diverging(state) is True

        state = _step(state, cycle=3, pre=_ORIGINAL, post=_ORIGINAL, lines=0)
        assert is_diverging(state) is True

    def test_stall_streak_resets_on_progress(self) -> None:
        state = _state()
        state = _step(
            state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=_BIG_CYCLE_LINES
        )
        after_progress = frozenset({_KEY_A, _KEY_B})
        state = _step(
            state,
            cycle=2,
            pre=_ORIGINAL,
            post=after_progress,
            lines=_BIG_CYCLE_LINES,
        )
        assert state.stall_streak == 0
        assert is_diverging(state) is False

        state = _step(state, cycle=3, pre=after_progress, post=after_progress, lines=1)
        assert state.stall_streak == 1
        # Cumulative lines are already past the floor, but one stalled cycle
        # since the reset is not enough.
        assert state.cumulative_net_lines_added > _DIVERGENCE_MIN_LINES
        assert is_diverging(state) is False

        state = _step(state, cycle=4, pre=after_progress, post=after_progress, lines=1)
        assert state.stall_streak == _LEGACY_STALL_CYCLES
        assert is_diverging(state) is True

    def test_lines_below_floor_never_trips_even_with_full_stall_streak(
        self,
    ) -> None:
        state = _state()
        for cycle in range(1, 6):
            state = _step(state, cycle=cycle, pre=_ORIGINAL, post=_ORIGINAL, lines=10)
        assert state.stall_streak == 5
        assert state.cumulative_net_lines_added == 50
        assert is_diverging(state) is False

    def test_pre_loop_fraction_raises_the_threshold_for_large_diffs(self) -> None:
        # A 1000-line pre-loop diff puts the bar at 500 lines, not the floor.
        state = _state(pre_loop_diff_lines=1000)
        for cycle in (1, 2):
            state = _step(state, cycle=cycle, pre=_ORIGINAL, post=_ORIGINAL, lines=200)
        assert state.cumulative_net_lines_added == 400
        assert is_diverging(state) is False

        state = _step(state, cycle=3, pre=_ORIGINAL, post=_ORIGINAL, lines=101)
        assert is_diverging(state) is True

    def test_resolving_a_self_inflicted_finding_is_not_progress(self) -> None:
        # Only originally-found findings count; a finding the loop introduced
        # and then fixed again leaves the stall streak climbing.
        state = _state()
        pre = _ORIGINAL | {_SELF_INFLICTED}
        state = _step(state, cycle=1, pre=pre, post=_ORIGINAL, lines=0)
        assert state.history[0].originally_resolved == 0
        assert state.stall_streak == 1

    def test_attributed_replacement_of_original_is_not_progress(self) -> None:
        # #2665: the reviewer rewords an original finding; the original key
        # vanishes but its replacement cites the loop's own helper.
        state = _step(
            _state(),
            cycle=1,
            pre=_ORIGINAL,
            post=(_ORIGINAL - {_KEY_A}) | {_SELF_INFLICTED},
            lines=40,
            loop_attributed=1,
        )

        assert state.stall_streak == 1
        assert state.history[0].originally_resolved == 1
        assert state.history[0].loop_attributed == 1

    def test_partial_attribution_still_counts_net_progress(self) -> None:
        state = _step(
            _state(),
            cycle=1,
            pre=_ORIGINAL,
            post=frozenset({_SELF_INFLICTED}),
            lines=40,
            loop_attributed=1,
        )

        assert state.history[0].originally_resolved == 3
        assert state.stall_streak == 0

    def test_zero_attributed_is_unchanged(self) -> None:
        state = _step(
            _state(),
            cycle=1,
            pre=_ORIGINAL,
            post=(_ORIGINAL - {_KEY_A}) | {_SELF_INFLICTED},
            lines=40,
        )

        assert state.stall_streak == 0
        assert state.history[0].loop_attributed == 0

    def test_history_records_every_cycle(self) -> None:
        state = _state()
        state = _step(
            state,
            cycle=1,
            pre=_ORIGINAL,
            post=frozenset({_KEY_A, _SELF_INFLICTED}),
            lines=40,
        )
        state = _step(
            state,
            cycle=2,
            pre=frozenset({_KEY_A, _SELF_INFLICTED}),
            post=frozenset({_KEY_A}),
            lines=15,
        )

        first, second = state.history
        assert first.cycle == 1
        assert first.must_fix_before == 3
        assert first.must_fix_after == 2
        assert first.originally_resolved == 2
        assert first.net_lines_added == 40
        assert first.cumulative_net_lines_added == 40
        assert second.cycle == 2
        assert second.originally_resolved == 0
        assert second.cumulative_net_lines_added == 55


class TestNetLinesForCommit:
    def test_net_lines_for_commit_returns_zero_for_none_sha(
        self, tmp_path: Path
    ) -> None:
        # tmp_path is not a git repo: any git call would raise.
        assert net_lines_for_commit(tmp_path / "not-a-repo", None) == 0

    def test_net_lines_for_commit_reads_real_commit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("wt-numstat")
        (repo / "seed.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        git_in(repo, "add", "seed.py")
        git_in(repo, "commit", "-m", "seed")
        # Rewrite one line (1 removed + 1 added) and add a new 4-line file.
        (repo / "seed.py").write_text("a = 1\nb = 20\nc = 3\n", encoding="utf-8")
        (repo / "grown.py").write_text("w = 1\nx = 2\ny = 3\nz = 4\n", encoding="utf-8")
        git_in(repo, "add", "seed.py", "grown.py")
        git_in(repo, "commit", "-m", "grow")
        sha = git_in(repo, "rev-parse", "HEAD")

        assert net_lines_for_commit(repo, sha) == 6


class TestRenderDivergenceReport:
    def test_render_divergence_report_includes_pre_loop_sha_and_per_cycle_rows(
        self,
    ) -> None:
        state = _state()
        state = _step(state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=90)
        state = _step(
            state,
            cycle=2,
            pre=_ORIGINAL,
            post=_ORIGINAL | {_SELF_INFLICTED},
            lines=80,
        )

        report = render_divergence_report(state)

        assert _PRE_LOOP_SHA in report
        assert "cycle 1: MUST_FIX 3→3, originally-resolved 0, net lines +90" in report
        assert "(cumulative 90)" in report
        assert "cycle 2: MUST_FIX 3→4, originally-resolved 0, net lines +80" in report
        assert "(cumulative 170)" in report


class TestRenderLoopAttributed:
    def test_attributed_suffix_only_when_positive(self) -> None:
        state = _state()
        state = _step(state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=90)
        state = _step(
            state,
            cycle=2,
            pre=_ORIGINAL,
            post=(_ORIGINAL - {_KEY_A}) | {_SELF_INFLICTED},
            lines=80,
            loop_attributed=1,
        )

        lines = render_divergence_report(state).splitlines()

        assert lines[0] == (
            "Fix loop diverging: no net progress on originally-found MUST_FIX "
            "findings for 2 consecutive cycle(s) (resolved originals minus new "
            "findings attributed to the loop's own code) while the diff grew by "
            f"170 line(s). Pre-loop head: {_PRE_LOOP_SHA} (pre-loop diff: "
            "10 line(s))."
        )
        [first] = [line for line in lines if line.startswith("- cycle 1")]
        [second] = [line for line in lines if line.startswith("- cycle 2")]
        assert first.endswith("(cumulative 90)")
        assert second.endswith("(cumulative 170), loop-attributed 1")


class TestEmitDivergenceEvent:
    def test_event_carries_pre_loop_sha_and_per_cycle_breakdown(self) -> None:
        state = _state()
        state = _step(state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=90)
        state = _step(state, cycle=2, pre=_ORIGINAL, post=_ORIGINAL, lines=80)

        emit_divergence_event(state=state, ticket_id=_TICKET)

        events = read_events(
            event_types=[OrchestratorEventType.FIX_LOOP_DIVERGENCE_DETECTED]
        )
        assert len(events) == 1
        event = events[0]
        assert event.correlation_id == _TICKET
        assert event.payload["pre_loop_head_sha"] == _PRE_LOOP_SHA
        assert event.payload["cumulative_net_lines_added"] == 170
        assert event.payload["stall_streak"] == 2
        cycles = event.payload["cycles"]
        assert isinstance(cycles, list)
        assert [c["cycle"] for c in cycles] == [1, 2]
        assert cycles[1]["net_lines_added"] == 80
        assert [c["loop_attributed"] for c in cycles] == [0, 0]
        assert event.payload["loop_generated_findings"] == []
        assert event.payload["threshold"] == 150
        assert event.payload["anchor_lines"] is None

    def test_event_payload_carries_relative_threshold_and_anchor_lines(
        self,
    ) -> None:
        state = _anchored(6)
        state = _step(state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=49)

        emit_divergence_event(state=state, ticket_id=_TICKET)

        [event] = read_events(
            event_types=[OrchestratorEventType.FIX_LOOP_DIVERGENCE_DETECTED]
        )
        assert event.payload["threshold"] == 48
        assert event.payload["anchor_lines"] == 6

    def test_event_payload_carries_loop_generated_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            divergence, "record_event", lambda *_a, **kw: calls.append(kw)
        )
        found = _af("new.py", 3, "loop bug")

        emit_divergence_event(state=_state(), ticket_id=_TICKET, loop_generated=[found])

        [call] = calls
        payload = call["payload"]
        assert isinstance(payload, dict)
        assert payload["loop_generated_findings"] == [
            {"file": "new.py", "line_start": 3, "summary": "loop bug"}
        ]


class TestStallCyclesParameter:
    def test_state_carries_configured_stall_cycles(self) -> None:
        assert _state(stall_cycles=3).stall_cycles == 3

    def test_one_stalled_cycle_trips_when_growth_clears_floor(self) -> None:
        state = _step(
            _state(stall_cycles=1), cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=200
        )
        assert is_diverging(state) is True

    def test_stall_cycles_two_requires_two(self) -> None:
        state = _step(
            _state(stall_cycles=2), cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=200
        )
        assert is_diverging(state) is False

    def test_default_state_uses_module_constant(self) -> None:
        state = initial_divergence_state(
            original_keys=_ORIGINAL, pre_loop_diff_lines=0, pre_loop_head_sha="x"
        )
        assert state.stall_cycles == _DIVERGENCE_STALL_CYCLES


def _af(
    file: str, line: int | None, summary: str, *, no_anchor: bool = False
) -> AcceptedFinding:
    extra: dict[str, object] = {"no_diff_anchor": True} if no_anchor else {}
    return AcceptedFinding(
        finding=_make_finding(
            file=file, line_start=line, line_end=line, summary=summary, **extra
        ),
        reviewers=["Code Quality Reviewer"],
    )


_ORIGINAL_FINDING = _af("base.py", 7, "original finding")


# #2665: pre-loop code a finding may cite without citing the loop.
_UTIL_PRE_LOOP = (
    "def existing_fn():\n    return 0\n\n\ndef edited_fn():\n    return 1\n"
)


@pytest.fixture
def loop_repo(make_git_repo: Callable[..., Path]) -> tuple[Path, DivergenceState]:
    """``base.py`` at the pre-loop head; one fix cycle grows it and adds files."""
    repo = make_git_repo("wt-loopgen")
    _write(repo / "base.py", "".join(f"b{i} = {i}\n" for i in range(1, 6)))
    _write(repo / "util.py", _UTIL_PRE_LOOP)
    git_in(repo, "add", "base.py", "util.py")
    git_in(repo, "commit", "-m", "pre-loop")
    pre = git_in(repo, "rev-parse", "HEAD")
    _write(repo / "base.py", "".join(f"b{i} = {i}\n" for i in range(1, 9)))
    _write(repo / "loop_new.py", "n = 1\nloop_symbol = 2\n")
    _write(repo / "copy.py", (repo / "base.py").read_text())
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "fix cycle 1")
    state = initial_divergence_state(
        original_keys=frozenset({_open_finding_key(_ORIGINAL_FINDING.finding)}),
        pre_loop_diff_lines=5,
        pre_loop_head_sha=pre,
    )
    return repo, state


class TestLoopGeneratedFindings:
    @pytest.mark.parametrize(
        ("finding", "expected"),
        [
            (_af("base.py", 7, "on an added line"), True),
            (_af("base.py", 2, "on an untouched line"), False),
            (_af("loop_new.py", None, "file-level, created file"), True),
            (_af("base.py", None, "file-level, pre-existing file"), False),
            (_af("copy.py", None, "file-level, copied file"), True),
            (_ORIGINAL_FINDING, False),
            (_af("N/A", None, "no diff anchor", no_anchor=True), False),
            (_af("base.py", 2, "untouched line citing `loop_symbol`"), True),
        ],
    )
    def test_classification(
        self,
        loop_repo: tuple[Path, DivergenceState],
        finding: AcceptedFinding,
        expected: bool,
    ) -> None:
        repo, state = loop_repo

        assert (loop_generated_findings(repo, state, [finding]) == [finding]) is (
            expected
        )

    def test_new_file_mode_never_survives_the_parser(
        self, loop_repo: tuple[Path, DivergenceState]
    ) -> None:
        """Why created files come from --diff-filter=A, not the parsed diff."""
        repo, state = loop_repo
        diff = git_in(repo, "diff", "-U0", f"{state.pre_loop_head_sha}..HEAD")
        file_diffs, *_ = _parse_unified_diff(diff)

        assert "new file mode" not in file_diffs["loop_new.py"]
        assert "@@ -0,0 +1" in file_diffs["loop_new.py"]

    def test_git_failure_returns_empty_list_and_warns(
        self,
        loop_repo: tuple[Path, DivergenceState],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo, state = loop_repo
        exc = subprocess.CalledProcessError(128, ["git", "diff"])

        def _boom(*_a: object, **_k: object) -> str:
            raise exc

        monkeypatch.setattr(divergence, "git_output", _boom)
        with caplog.at_level(logging.WARNING, logger="cw.codex_fix_loop.divergence"):
            found = loop_generated_findings(repo, state, [_af("base.py", 7, "x")])

        assert found == []
        [record] = caplog.records
        assert record.args == (state.pre_loop_head_sha, exc)


def _anchored(anchor: int | None, pre_loop: int = 10) -> DivergenceState:
    return initial_divergence_state(
        original_keys=_ORIGINAL,
        pre_loop_diff_lines=pre_loop,
        pre_loop_head_sha=_PRE_LOOP_SHA,
        stall_cycles=1,
        original_anchor_lines=anchor,
    )


def _ranged(
    start: int | None, end: int | None, *, file: str = "a.py"
) -> AcceptedFinding:
    return AcceptedFinding(
        finding=_make_finding(
            file=file, line_start=start, line_end=end, summary="ranged"
        ),
        reviewers=["Code Quality Reviewer"],
    )


class TestRelativeThreshold:
    def test_thresholds_match_relative_defaults(self) -> None:
        assert _DIVERGENCE_ANCHOR_RATIO == 8
        assert _DIVERGENCE_RELATIVE_MIN_LINES == 40

    def test_2590_shaped_sequence_trips_at_cycle_5(self) -> None:
        state = _anchored(6)
        assert divergence_threshold(state) == 48
        expected = [False, False, False, False, True]
        for cycle, (lines, want) in enumerate(
            zip([12, 12, 12, 12, 11], expected, strict=True), 1
        ):
            state = _step(
                state, cycle=cycle, pre=_ORIGINAL, post=_ORIGINAL, lines=lines
            )
            assert is_diverging(state) is want, cycle
        assert state.cumulative_net_lines_added == 59

    def test_2590_sequence_without_anchor_never_trips(self) -> None:
        state = _state(stall_cycles=1)
        for cycle, lines in enumerate([12, 12, 12, 12, 11], 1):
            state = _step(
                state, cycle=cycle, pre=_ORIGINAL, post=_ORIGINAL, lines=lines
            )
            assert is_diverging(state) is False
        assert state.cumulative_net_lines_added == 59

    def test_large_anchor_keeps_absolute_floor_as_upper_bound(self) -> None:
        state = _anchored(30)
        assert divergence_threshold(state) == 150
        for lines, want in [(60, False), (150, False), (151, True)]:
            fresh = _step(
                _anchored(30), cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=lines
            )
            assert is_diverging(fresh) is want, lines

    def test_one_line_anchor_with_fix_and_test_is_safe(self) -> None:
        assert divergence_threshold(_anchored(1)) == 40
        for lines, want in [(25, False), (40, False), (41, True)]:
            fresh = _step(
                _anchored(1), cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=lines
            )
            assert is_diverging(fresh) is want, lines

    def test_relative_lowers_threshold_for_large_pre_loop_diff(self) -> None:
        # Absolute bar for pre_loop=1000 is 500; a 6-line anchor brings it to 48.
        assert divergence_threshold(_anchored(6, pre_loop=1000)) == 48
        assert divergence_threshold(_state(pre_loop_diff_lines=1000)) == 500
        tripped = _step(
            _anchored(6, pre_loop=1000),
            cycle=1,
            pre=_ORIGINAL,
            post=_ORIGINAL,
            lines=49,
        )
        assert is_diverging(tripped) is True
        unanchored = _step(
            _state(pre_loop_diff_lines=1000, stall_cycles=1),
            cycle=1,
            pre=_ORIGINAL,
            post=_ORIGINAL,
            lines=49,
        )
        assert is_diverging(unanchored) is False

    def test_relative_still_needs_the_stall_streak(self) -> None:
        state = _anchored(6)
        state = _step(
            state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL - {_KEY_A}, lines=100
        )
        assert state.stall_streak == 0
        assert is_diverging(state) is False

    def test_unanchored_state_threshold_is_absolute(self) -> None:
        assert divergence_threshold(_state(stall_cycles=1)) == 150

    def test_initial_state_carries_anchor_lines(self) -> None:
        assert _anchored(6).original_anchor_lines == 6
        assert _state().original_anchor_lines is None

    def test_record_cycle_preserves_anchor_lines(self) -> None:
        state = _step(_anchored(6), cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=5)
        assert state.original_anchor_lines == 6


class TestTotalAnchorLines:
    @pytest.mark.parametrize(
        ("findings", "expected"),
        [
            ([_ranged(7, 7)], 1),
            ([_ranged(10, 15)], 6),
            ([_ranged(10, None)], 1),
            ([_ranged(10, 15), _ranged(20, 20)], 7),
            ([], None),
            ([_ranged(None, None)], None),
            ([_ranged(None, None, file="N/A")], None),
            ([_ranged(10, 15), _ranged(None, None)], None),
        ],
    )
    def test_helper_values(
        self, findings: list[AcceptedFinding], expected: int | None
    ) -> None:
        assert total_anchor_lines(findings) == expected


class TestRelativeReport:
    def test_report_first_line_unchanged_and_threshold_line_absolute(self) -> None:
        report = render_divergence_report(_state(stall_cycles=1))
        first = report.splitlines()[0]
        assert first.startswith(
            "Fix loop diverging: no net progress on originally-found MUST_FIX"
        )
        assert report.splitlines()[1] == (
            "Divergence threshold: 150 line(s) of cumulative churn "
            "(absolute: max(150, 0.5 x pre-loop diff))."
        )

    def test_report_threshold_line_names_anchor_when_relative(self) -> None:
        report = render_divergence_report(_anchored(6))
        assert report.splitlines()[1] == (
            "Divergence threshold: 48 line(s) of cumulative churn "
            "(relative to 6 original anchor line(s): max(40, 8 x 6); "
            "the absolute threshold of 150 still caps it)."
        )


class TestRenderLoopGenerated:
    def test_lists_loop_generated_findings(self) -> None:
        state = _state()
        state = _step(state, cycle=1, pre=_ORIGINAL, post=_ORIGINAL, lines=90)

        report = render_divergence_report(state, [_af("new.py", 3, "loop bug")])

        assert "Findings generated by the loop's own code (1 of 3 open)" in report
        assert "- new.py:3 loop bug" in report

    def test_omits_section_when_none(self) -> None:
        assert "generated by the loop" not in render_divergence_report(_state())


_HELPERS = """def _fold_roster_1(rows):
    return rows


class RosterFold:
    pass


async def fetch_rows():
    return []


_ROSTER_LIMIT = 3
grown_1_0 = 0


def process(items):
    return items


def _helper(item):
    return item


def abc():
    return 1


def __weird__():
    return 2
"""
_UTIL_EDITED = (
    "def existing_fn():\n    return 0\n\n\ndef edited_fn(arg):\n    return 1\n"
)


@pytest.fixture
def citation_repo(
    loop_repo: tuple[Path, DivergenceState],
) -> tuple[Path, DivergenceState]:
    """``loop_repo`` plus a second cycle that adds symbols and edits ``edited_fn``."""
    repo, state = loop_repo
    _write(repo / "helpers.py", _HELPERS)
    _write(repo / "util.py", _UTIL_EDITED)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "fix cycle 2")
    return repo, state


def _cite(**fields: str) -> AcceptedFinding:
    """A new finding on an untouched pre-loop line, text fields from *fields*."""
    kwargs: dict[str, object] = {
        "file": "util.py",
        "line_start": 1,
        "line_end": 1,
        "summary": "unrelated wording",
        "consequence": "unrelated",
        "suggested_fix": "unrelated",
        "evidence": "def existing_fn():",
        **fields,
    }
    return AcceptedFinding(
        finding=_make_finding(**kwargs), reviewers=["Code Quality Reviewer"]
    )


def _count(
    repo: Path,
    state: DivergenceState,
    *findings: AcceptedFinding,
    pre: frozenset[_OpenFindingKey] = frozenset(),
) -> int:
    return attributed_new_finding_count(
        repo,
        state,
        pre_open_keys=pre,
        open_findings={_open_finding_key(f.finding): f for f in findings},
    )


class TestLoopCitation:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"summary": "breaks `_fold_roster_1` for empty rosters"}, 1),
            ({"summary": "breaks `existing_fn` for empty rosters"}, 0),
            ({"summary": "process the batch before the fold"}, 0),
            ({"summary": "the process is wrong"}, 0),
            ({"summary": "_fold_roster_1 never validates its input"}, 1),
            ({"summary": "_helper never validates its input"}, 1),
            ({"evidence": "rows = _fold_roster_1(x)"}, 1),
            ({"evidence": "rows = self._fold_roster_1(x)"}, 1),
            ({"consequence": "`RosterFold` is unused"}, 1),
            ({"suggested_fix": "inline `fetch_rows`"}, 1),
            ({"summary": "`_ROSTER_LIMIT` is a magic number"}, 1),
            ({"summary": "`grown_1_0` is dead"}, 1),
            ({"summary": "`abc` is too short a name"}, 0),
            ({"summary": "`__weird__` is odd"}, 0),
            ({"summary": "`edited_fn` changed signature"}, 0),
            ({"summary": "calls edited_fn(arg) twice"}, 0),
        ],
    )
    def test_name_citation(
        self,
        citation_repo: tuple[Path, DivergenceState],
        fields: dict[str, str],
        expected: int,
    ) -> None:
        repo, state = citation_repo

        assert _count(repo, state, _cite(**fields)) == expected

    def test_loop_added_line_without_a_name_counts(
        self, citation_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, state = citation_repo

        assert _count(repo, state, _af("helpers.py", 1, "something is off")) == 1
        assert _count(repo, state, _af("base.py", 7, "on an added line")) == 1
        assert _count(repo, state, _af("base.py", 2, "on an untouched line")) == 0

    def test_no_diff_anchor_finding_counts_by_name_only(
        self, citation_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, state = citation_repo
        named = _af("N/A", None, "cites `_fold_roster_1`", no_anchor=True)
        bare = _af("N/A", None, "no names here", no_anchor=True)

        assert _count(repo, state, named) == 1
        assert _count(repo, state, bare) == 0

    def test_only_new_non_original_findings_count(
        self, citation_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, state = citation_repo
        persistent = _cite(summary="persistent `_fold_roster_1` finding")
        key = _open_finding_key(persistent.finding)

        assert _count(repo, state, _ORIGINAL_FINDING) == 0
        assert _count(repo, state, persistent, pre=frozenset({key})) == 0
        assert _count(repo, state, persistent) == 1

    def test_no_new_findings_makes_no_git_call(
        self,
        citation_repo: tuple[Path, DivergenceState],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, state = citation_repo

        def _boom(*_a: object, **_k: object) -> str:
            pytest.fail("git must not be called")

        monkeypatch.setattr(divergence, "git_output", _boom)

        assert _count(repo, state, _ORIGINAL_FINDING) == 0
        assert _count(repo, state) == 0

    def test_git_failure_returns_zero_and_warns(
        self,
        citation_repo: tuple[Path, DivergenceState],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo, state = citation_repo
        exc = subprocess.CalledProcessError(128, ["git", "diff"])

        def _boom(*_a: object, **_k: object) -> str:
            raise exc

        monkeypatch.setattr(divergence, "git_output", _boom)
        with caplog.at_level(logging.WARNING, logger="cw.codex_fix_loop.divergence"):
            count = _count(repo, state, _cite(summary="cites `_fold_roster_1`"))

        assert count == 0
        [record] = caplog.records
        assert record.args == (state.pre_loop_head_sha, exc)


_CYCLE_LINES = 90


def _cycle_commit(repo: Path, k: int) -> str:
    """Commit fix cycle *k*: a ~90-line helper ``_fold_roster_k``; return its SHA."""
    body = "".join(f"    step_{k}_{i} = {i}\n" for i in range(_CYCLE_LINES))
    _write(
        repo / f"roster_{k}.py", f"def _fold_roster_{k}(rows):\n{body}    return rows\n"
    )
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", f"fix cycle {k + 1}")
    return git_in(repo, "rev-parse", "HEAD")


# Fingerprints normalise digits, so distinct findings need distinct words.
_WORDS = ("alpha", "beta", "gamma")


def _originals(count: int) -> list[AcceptedFinding]:
    return [_af("base.py", i + 1, f"original {_WORDS[i]}") for i in range(count)]


def _run_cycles(
    repo: Path,
    base: DivergenceState,
    *,
    cites: Callable[[int], str],
    resolves_original: bool,
) -> list[DivergenceState]:
    originals = _originals(3)
    state = base._replace(
        original_keys=frozenset(_open_finding_key(a.finding) for a in originals),
        stall_cycles=2,
    )
    open_now = {_open_finding_key(a.finding): a for a in originals}
    states: list[DivergenceState] = []
    for k in (1, 2):
        sha = _cycle_commit(repo, k)
        pre = frozenset(open_now)
        reworded = _af(
            "base.py", 2, f"reworded {_WORDS[k - 1]} finding, cites `{cites(k)}`"
        )
        post = dict(open_now)
        if resolves_original:
            del post[_open_finding_key(originals[k - 1].finding)]
        post[_open_finding_key(reworded.finding)] = reworded
        attributed = attributed_new_finding_count(
            repo, state, pre_open_keys=pre, open_findings=post
        )
        state = record_divergence_cycle(
            state,
            cycle=k,
            pre_open_keys=pre,
            post_open_keys=frozenset(post),
            net_lines_added=net_lines_for_commit(repo, sha),
            loop_attributed=attributed,
        )
        open_now = post
        states.append(state)
    return states


class TestAcceptance:
    def test_multi_cycle_laundering_by_name_advances_stall_streak_and_parks(
        self, loop_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, base = loop_repo

        first, second = _run_cycles(
            repo,
            base,
            # Cycle 1 cites its own helper; later cycles cite the previous one.
            cites=lambda k: f"_fold_roster_{max(k - 1, 1)}",
            resolves_original=True,
        )

        assert first.stall_streak == 1
        assert is_diverging(first) is False
        assert second.stall_streak == 2
        assert is_diverging(second) is True
        assert [(r.originally_resolved, r.loop_attributed) for r in second.history] == [
            (1, 1),
            (1, 1),
        ]

    def test_genuinely_new_in_scope_defect_is_unaffected(
        self, loop_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, base = loop_repo

        first, second = _run_cycles(
            repo, base, cites=lambda _k: "existing_fn", resolves_original=True
        )

        assert second.stall_streak == 0
        assert is_diverging(second) is False
        assert [r.loop_attributed for r in second.history] == [0, 0]
        assert first.stall_streak == 0

    def test_without_resolved_originals_streak_advances_as_before(
        self, loop_repo: tuple[Path, DivergenceState]
    ) -> None:
        repo, base = loop_repo

        first, second = _run_cycles(
            repo, base, cites=lambda _k: "existing_fn", resolves_original=False
        )

        assert (first.stall_streak, second.stall_streak) == (1, 2)
        assert is_diverging(second) is True
        assert [r.loop_attributed for r in second.history] == [0, 0]
