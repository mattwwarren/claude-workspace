"""Tests for cw.codex_fix_loop_divergence — the fix-loop divergence guard (#2394).

The #1837 admission gate always admits a MUST_FIX anchored in the latest delta,
so a fix loop that keeps inventing new code to address its own findings never
converges and burns every cycle growing the diff. These tests lock in the
loop-wide progress guard: it trips only when the loop has resolved none of the
originally-found MUST_FIX findings for consecutive cycles AND has grown the
diff past a size threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_fix_loop_divergence import (
    _DIVERGENCE_MIN_LINES,
    _DIVERGENCE_PRE_LOOP_FRACTION,
    _DIVERGENCE_STALL_CYCLES,
    DivergenceState,
    emit_divergence_event,
    initial_divergence_state,
    is_diverging,
    net_lines_for_commit,
    record_divergence_cycle,
    render_divergence_report,
)
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop_convergence import _OpenFindingKey

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


def _state(pre_loop_diff_lines: int = 10) -> DivergenceState:
    return initial_divergence_state(
        original_keys=_ORIGINAL,
        pre_loop_diff_lines=pre_loop_diff_lines,
        pre_loop_head_sha=_PRE_LOOP_SHA,
    )


def _step(
    state: DivergenceState,
    *,
    cycle: int,
    pre: frozenset[_OpenFindingKey],
    post: frozenset[_OpenFindingKey],
    lines: int,
) -> DivergenceState:
    return record_divergence_cycle(
        state,
        cycle=cycle,
        pre_open_keys=pre,
        post_open_keys=post,
        net_lines_added=lines,
    )


class TestDivergenceThresholds:
    def test_thresholds_match_the_documented_defaults(self) -> None:
        assert _DIVERGENCE_STALL_CYCLES == 2
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
        assert state.stall_streak == _DIVERGENCE_STALL_CYCLES
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
        assert state.stall_streak == _DIVERGENCE_STALL_CYCLES
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
