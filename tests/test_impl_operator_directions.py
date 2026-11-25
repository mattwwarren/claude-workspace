"""Tests for cw.stage_verification.impl_operator_directions (#3761).

The rule is a pure function of the row: it needs no worktree, so every test
builds a row and calls it directly.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from cw.context_queue_metadata import OPERATOR_DIRECTIONS_KEY
from cw.models import Stage
from cw.stage_verification.impl_operator_directions import (
    _PREAMBLE_RESERVE_BYTES,
    COUNTS_LINE,
    FOLDED_PHRASE,
    GOVERNS_PHRASE,
    MAX_OPERATOR_DIRECTIONS_BYTES,
    NEVER_DELETED_PHRASE,
    OLDEST_FIRST_PHRASE,
    OMISSION_LINE,
    PINNED_PHRASES,
    OperatorDirections,
    _preamble,
    operator_directions,
    with_operator_directions,
)
from cw.stage_verification.posted_text import TRUNCATION_MARKER
from tests._shared.operator_direction_builders import (
    at,
    make_impl_row,
    make_operator_constraint,
    make_resolution,
)

_PREAMBLE_PHRASES = (
    OLDEST_FIRST_PHRASE,
    GOVERNS_PHRASE,
    NEVER_DELETED_PHRASE,
    FOLDED_PHRASE,
)
_ENTRY_BUDGET = MAX_OPERATOR_DIRECTIONS_BYTES - _PREAMBLE_RESERVE_BYTES


def _big(name: str, size: int) -> str:
    """Entry text that starts with *name* and is *size* ASCII bytes long."""
    return f"{name} " + "x" * size


def _delivered(row_directions: OperatorDirections | None) -> OperatorDirections:
    assert row_directions is not None
    return row_directions


class TestApplicability:
    @pytest.mark.parametrize("stage", [Stage.PLAN, Stage.REVIEW, Stage.FINALIZE])
    def test_non_impl_stage_returns_none(self, stage: Stage) -> None:
        row = make_impl_row(
            resolutions=[make_resolution()],
            constraints=[make_operator_constraint()],
            stage=stage,
        )

        assert operator_directions(row) is None
        assert with_operator_directions({"a": 1}, row)[OPERATOR_DIRECTIONS_KEY] is None

    def test_impl_without_entries_returns_none(self) -> None:
        assert operator_directions(make_impl_row()) is None

    @pytest.mark.parametrize(
        "kind", ["adopted_assumption", "non_goal", "plan_decision"]
    )
    def test_plan_derived_constraints_alone_return_none(self, kind: str) -> None:
        row = make_impl_row(constraints=[make_operator_constraint(kind=kind)])

        assert operator_directions(row) is None

    def test_operator_constraint_is_delivered(self) -> None:
        row = make_impl_row(constraints=[make_operator_constraint("Keep it small.")])

        result = _delivered(operator_directions(row))

        assert "Keep it small." in result.text
        assert (result.resolutions, result.constraints, result.omitted) == (0, 1, 0)

    def test_resolution_alone_is_delivered(self) -> None:
        row = make_impl_row(resolutions=[make_resolution("Use the row.")])

        result = _delivered(operator_directions(row))

        assert "Use the row." in result.text
        assert (result.resolutions, result.constraints, result.omitted) == (1, 0, 0)

    def test_mixed_constraints_deliver_operator_entries_only(self) -> None:
        row = make_impl_row(
            constraints=[
                make_operator_constraint("OPERATOR-TEXT", round_=1),
                make_operator_constraint("PLAN-TEXT", round_=2, kind="non_goal"),
            ]
        )

        result = _delivered(operator_directions(row))

        assert "OPERATOR-TEXT" in result.text
        assert "PLAN-TEXT" not in result.text
        assert result.constraints == 1


class TestOrderingAndSupersession:
    def test_later_resolution_renders_after_earlier_constraint(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution("RES-TEXT", recorded_at=at(5))],
            constraints=[make_operator_constraint("CON-TEXT", recorded_at=at(1))],
        )

        text = _delivered(operator_directions(row)).text

        assert text.index("CON-TEXT") < text.index("RES-TEXT")

    def test_later_constraint_renders_after_earlier_resolution(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution("RES-TEXT", recorded_at=at(1))],
            constraints=[make_operator_constraint("CON-TEXT", recorded_at=at(5))],
        )

        text = _delivered(operator_directions(row)).text

        assert text.index("RES-TEXT") < text.index("CON-TEXT")

    def test_round_order_within_a_source_is_kept(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution("FIRST", round_=1, recorded_at=at(1)),
                make_resolution("SECOND", round_=2, recorded_at=at(2)),
            ]
        )

        text = _delivered(operator_directions(row)).text

        assert text.index("FIRST") < text.index("SECOND")

    def test_timestamp_tie_breaks_resolution_before_constraint(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution("RES-TEXT", recorded_at=at(3))],
            constraints=[make_operator_constraint("CON-TEXT", recorded_at=at(3))],
        )

        text = _delivered(operator_directions(row)).text

        assert text.index("RES-TEXT") < text.index("CON-TEXT")

    def test_timestamp_tie_within_a_source_breaks_by_round(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution("ROUND-TWO", round_=2, recorded_at=at(3)),
                make_resolution("ROUND-ONE", round_=1, recorded_at=at(3)),
            ]
        )

        text = _delivered(operator_directions(row)).text

        assert text.index("ROUND-ONE") < text.index("ROUND-TWO")

    def test_naive_and_aware_timestamps_do_not_raise(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution(
                    "NAIVE", recorded_at=datetime.fromisoformat("2026-11-20T09:00:00")
                )
            ],
            constraints=[make_operator_constraint("AWARE", recorded_at=at(0))],
        )

        result = _delivered(operator_directions(row))

        assert "NAIVE" in result.text
        assert "AWARE" in result.text

    def test_preamble_carries_the_pinned_phrases_and_counts(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution()],
            constraints=[make_operator_constraint(recorded_at=at(1))],
        )

        text = _delivered(operator_directions(row)).text

        for phrase in _PREAMBLE_PHRASES:
            assert phrase in text
        assert COUNTS_LINE.format(resolutions=1, constraints=1) in text

    def test_omission_line_absent_when_nothing_is_omitted(self) -> None:
        row = make_impl_row(resolutions=[make_resolution()])

        text = _delivered(operator_directions(row)).text

        assert OMISSION_LINE.format(omitted=0) not in text
        assert "older entries omitted" not in text

    def test_pinned_phrases_tuple_holds_all_seven(self) -> None:
        assert len(PINNED_PHRASES) == 7
        assert len(set(PINNED_PHRASES)) == 7


class TestRender:
    def test_headers_and_verbatim_text(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution(
                    "Line one.\nLine two.", round_=3, recorded_at=at(0), actor="alice"
                )
            ],
            constraints=[
                make_operator_constraint(
                    "No new deps.", round_=4, recorded_at=at(1), actor="bob"
                )
            ],
        )

        text = _delivered(operator_directions(row)).text

        assert "### Resolution 3 (recorded 2026-11-20T09:00:00Z by alice)" in text
        assert "### Constraint 4 (recorded 2026-11-20T09:01:00Z by bob)" in text
        assert "Line one.\nLine two." in text
        assert "No new deps." in text


class TestCapAndTruncation:
    def test_multibyte_text_stays_within_the_byte_cap(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution("é" * 20_000, round_=n, recorded_at=at(n))
                for n in range(1, 4)
            ]
        )

        text = _delivered(operator_directions(row)).text

        assert len(text.encode("utf-8")) <= MAX_OPERATOR_DIRECTIONS_BYTES

    def test_overflow_drops_the_oldest_whole_entries(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution(_big("ENTRY-A", 25_000), round_=1, recorded_at=at(0)),
                make_resolution(_big("ENTRY-C", 25_000), round_=2, recorded_at=at(2)),
            ],
            constraints=[
                make_operator_constraint(
                    _big("ENTRY-B", 25_000), round_=3, recorded_at=at(1)
                ),
                make_operator_constraint(
                    _big("ENTRY-D", 25_000), round_=4, recorded_at=at(3)
                ),
            ],
        )

        result = _delivered(operator_directions(row))

        assert "ENTRY-A" not in result.text
        assert "ENTRY-B" not in result.text
        assert "ENTRY-C" in result.text
        assert "ENTRY-D" in result.text
        assert result.omitted == 2
        assert OMISSION_LINE.format(omitted=2) in result.text
        assert (result.resolutions, result.constraints) == (1, 1)
        assert result.resolutions + result.constraints + result.omitted == 4
        assert len(result.text.encode("utf-8")) <= MAX_OPERATOR_DIRECTIONS_BYTES

    def test_single_newest_entry_over_budget_is_delivered_cut(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution(_big("HUGE", 200_000), recorded_at=at(0))]
        )

        result = _delivered(operator_directions(row))

        assert result.text.endswith(TRUNCATION_MARKER)
        assert "HUGE" in result.text
        assert (result.resolutions, result.constraints, result.omitted) == (1, 0, 0)
        assert len(result.text.encode("utf-8")) <= MAX_OPERATOR_DIRECTIONS_BYTES

    def test_oversized_older_entry_is_omitted_not_delivered(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution(
                    _big("OVERSIZED-SENTINEL", 200_000), round_=1, recorded_at=at(0)
                ),
                make_resolution("SMALL-NEWEST", round_=2, recorded_at=at(1)),
            ]
        )

        result = _delivered(operator_directions(row))

        assert "OVERSIZED-SENTINEL" not in result.text
        assert "SMALL-NEWEST" in result.text
        assert result.omitted == 1
        assert result.resolutions == 1
        assert len(result.text.encode("utf-8")) <= MAX_OPERATOR_DIRECTIONS_BYTES

    def test_kept_set_is_a_contiguous_newest_suffix(self) -> None:
        row = make_impl_row(
            resolutions=[
                make_resolution("SMALL-OLDEST", round_=1, recorded_at=at(0)),
                make_resolution(
                    _big("OVERSIZED-MIDDLE", 200_000), round_=2, recorded_at=at(1)
                ),
                make_resolution("SMALL-NEWEST", round_=3, recorded_at=at(2)),
            ]
        )

        result = _delivered(operator_directions(row))

        assert "SMALL-NEWEST" in result.text
        assert "SMALL-OLDEST" not in result.text
        assert "OVERSIZED-MIDDLE" not in result.text
        assert result.omitted == 2
        assert result.resolutions == 1

    def test_preamble_always_fits_the_reserve(self) -> None:
        preamble = _preamble(resolutions=10**9, constraints=10**9, omitted=10**6)

        assert len(preamble.encode("utf-8")) + 2 <= _PREAMBLE_RESERVE_BYTES


class TestRedaction:
    def test_secret_shapes_are_absent_from_the_output(self) -> None:
        openai_like = "sk-" + "A" * 24
        github_like = "ghp_" + "b" * 36
        row = make_impl_row(
            resolutions=[make_resolution(f"keep {openai_like} out")],
            constraints=[
                make_operator_constraint(f"also {github_like}", recorded_at=at(1))
            ],
        )

        text = _delivered(operator_directions(row)).text

        assert openai_like not in text
        assert github_like not in text


class TestWire:
    def test_wire_has_exactly_the_four_keys(self) -> None:
        row = make_impl_row(resolutions=[make_resolution()])

        wire = _delivered(operator_directions(row)).wire()

        assert set(wire) == {"text", "resolutions", "constraints", "omitted"}

    def test_with_operator_directions_preserves_other_keys(self) -> None:
        row = make_impl_row(resolutions=[make_resolution()])

        out = with_operator_directions({"keep": "me", "n": 2}, row)

        assert out["keep"] == "me"
        assert out["n"] == 2
        assert isinstance(out[OPERATOR_DIRECTIONS_KEY], dict)

    def test_key_is_always_present_and_null_when_nothing_delivered(self) -> None:
        out = with_operator_directions({}, make_impl_row())

        assert OPERATOR_DIRECTIONS_KEY in out
        assert out[OPERATOR_DIRECTIONS_KEY] is None

    def test_calls_are_deterministic(self) -> None:
        row = make_impl_row(
            resolutions=[make_resolution()],
            constraints=[make_operator_constraint(recorded_at=at(1))],
        )

        first = with_operator_directions({}, row)

        assert with_operator_directions({}, row) == first


class TestPurity:
    def test_rule_takes_only_the_row_and_does_not_mutate_it(self) -> None:
        resolutions = [
            make_resolution("B", round_=2, recorded_at=at(2)),
            make_resolution("A", round_=1, recorded_at=at(1)),
        ]
        constraints = [make_operator_constraint("C", recorded_at=at(0))]
        row = make_impl_row(resolutions=resolutions, constraints=constraints)
        before = row.model_dump()

        operator_directions(row)

        assert row.model_dump() == before
        assert [r.text for r in row.operator_resolutions] == ["B", "A"]
