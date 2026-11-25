"""Tests for cw.codex_plan._settle: settlement validation and stubs (#3464)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._post import PARK_HEADER
from cw.codex_plan._result import PlanSettlement
from cw.codex_plan._settle import (
    DEFERRED_STUB,
    ParkState,
    SettleOutcome,
    park_state,
    unresolved_stubs,
    validate_settlements,
)
from cw.codex_plan._tracker import observe_tracker
from cw.gh import AGENT_COMMENT_MARKER
from cw.ticket_snapshot import Purpose, SnapshotContext, bound_comments
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._tool_fakes import plan_sentinel

_PARK_BODY = (
    f"{PARK_HEADER}\n\n1. Which store?\n   - alternatives: (a) redis (b) disk\n"
    f"2. X holds\n\n### How to answer\n\n...\n\n{AGENT_COMMENT_MARKER}"
)


def _park(comment_id: str, created_at: str, body: str = _PARK_BODY) -> object:
    return make_comment(
        comment_id,
        body=body,
        author_id="bot",
        created_at=created_at,
        agent_authored=True,
    )


def _claim(item_id: str, token: str, source: str = "c9") -> PlanSettlement:
    return PlanSettlement(item_id=item_id, token=token, source=source)


def _state(ordinal: int = 1) -> ParkState:
    return ParkState(ordinal=ordinal, newest_body=_PARK_BODY)


def _validate(
    *claims: PlanSettlement,
    park: ParkState | None = None,
    settled: tuple[str, ...] = (),
) -> SettleOutcome:
    return validate_settlements(
        claims,
        park=park or _state(),
        settled=settled,
        comment_ids={"c9"},
        row_rounds={2},
    )


class TestValidate:
    def test_accepts_a_bare_id_at_park_one(self) -> None:
        outcome = _validate(_claim("A1", "ADOPTED"))
        assert outcome.settled_entries() == ("A1: ADOPTED",)
        assert outcome.rejected == ()

    def test_accepts_a_round_qualified_id_at_park_two(self) -> None:
        outcome = _validate(_claim("R2.P2", "CONFIRMED"), park=_state(2))
        assert outcome.settled_entries() == ("R2.P2: CONFIRMED",)

    @pytest.mark.parametrize(
        ("item_id", "ordinal"), [("R1.A1", 1), ("A1", 2), ("R3.A1", 2), ("R2.A1", 1)]
    )
    def test_rejects_a_qualifier_not_equal_to_k(
        self, item_id: str, ordinal: int
    ) -> None:
        outcome = _validate(_claim(item_id, "ADOPTED"), park=_state(ordinal))
        assert outcome.accepted == ()
        assert "round qualifier" in outcome.rejected[0][1]

    def test_rejects_a_token_of_the_wrong_letter(self) -> None:
        outcome = _validate(_claim("A1", "CONFIRMED"))
        assert outcome.rejected[0][1] == "not a settled-line token for this id"

    def test_rejects_a_non_id(self) -> None:
        assert _validate(_claim("Q1", "ADOPTED")).rejected[0][1] == "not an item id"

    def test_rejects_an_alternative_absent_from_the_item(self) -> None:
        assert _validate(_claim("A1", "ALT-b")).accepted
        outcome = _validate(_claim("A1", "ALT-z"))
        assert outcome.rejected[0][1] == "alternative absent from the item"

    def test_rejects_an_already_settled_id(self) -> None:
        outcome = _validate(_claim("A1", "ADOPTED"), settled=("A1: ALT-a",))
        assert outcome.rejected[0][1] == "already settled"

    def test_rejects_a_repeat_within_one_turn(self) -> None:
        outcome = _validate(_claim("A1", "ADOPTED"), _claim("A1", "ALT-a"))
        assert outcome.settled_entries() == ("A1: ADOPTED",)
        assert outcome.rejected[0][1] == "already settled"

    def test_rejects_an_item_absent_from_the_park_comment(self) -> None:
        outcome = _validate(_claim("A7", "ADOPTED"))
        assert outcome.rejected[0][1] == "item absent from the park comment"

    def test_sources(self) -> None:
        assert _validate(_claim("A1", "ADOPTED", "row:2")).accepted
        outcome = _validate(_claim("A1", "ADOPTED", "row:9"))
        assert outcome.rejected[0][1] == "unknown source"
        assert _validate(_claim("A1", "ADOPTED", "x")).rejected[0][1] == (
            "unknown source"
        )

    def test_fail_closed_without_a_park_comment(self) -> None:
        outcome = _validate(
            _claim("A1", "ADOPTED"), park=ParkState(ordinal=0, newest_body=None)
        )
        assert outcome.rejected[0][1] == "no park comment"


class TestParkState:
    def test_reads_raw_comments_by_header_and_agent_marker(self) -> None:
        imitation = make_comment("human", body=_PARK_BODY, created_at="2026-11-30T0")
        snapshot = make_snapshot_from([_park("p1", "2026-11-20T00:00:00Z"), imitation])
        state = park_state(snapshot)
        assert state.ordinal == 1

    def test_park_comments_survive_a_non_github_context(self) -> None:
        snapshot = make_snapshot_from([_park("p1", "2026-11-20T00:00:00Z")])
        mcp = SnapshotContext(github=False, operator_ids=("operator",))
        assert bound_comments(snapshot, mcp, Purpose.OPERATOR_TEXT) == []
        assert bound_comments(snapshot, mcp, Purpose.VOIDED_AND_SETTLE) == []
        assert park_state(snapshot).ordinal == 1

    def test_k_counts_every_park_comment_and_newest_is_last(self) -> None:
        empty = f"{PARK_HEADER}\n\nNo open ambiguities or premises."
        snapshot = make_snapshot_from(
            [
                _park("p1", "2026-11-25T00:00:00Z", empty),
                _park("p2", "2026-11-01T00:00:00Z", empty),
                _park("p3", "2026-11-02T00:00:00Z"),
            ]
        )
        state = park_state(snapshot)
        assert state.ordinal == 3
        assert state.newest_body == _PARK_BODY

    def test_an_equal_created_at_is_tie_broken_by_id(self) -> None:
        same = "2026-11-20T00:00:00Z"
        snapshot = make_snapshot_from(
            [_park("pb", same), _park("pa", same, f"{PARK_HEADER}\n\n1. Only")]
        )
        assert park_state(snapshot).newest_body == _PARK_BODY

    def test_no_snapshot(self) -> None:
        assert park_state(None) == ParkState(ordinal=0, newest_body=None)
        assert park_state(make_snapshot_from([])).ordinal == 0

    def test_item_block_is_the_numbered_line_and_its_sub_lines(self) -> None:
        state = _state()
        assert state.item_block(1) == (
            "1. Which store?\n   - alternatives: (a) redis (b) disk"
        )
        assert state.item_block(2) == "2. X holds\n"
        assert ParkState(ordinal=0, newest_body=None).item_block(1) is None


class TestOutcome:
    def test_friction_lines(self) -> None:
        outcome = _validate(_claim("A1", "ADOPTED"), _claim("A9", "ADOPTED"))
        assert outcome.friction(1) == [
            "plan-stage item settled: A1 → ADOPTED — round 1",
            "plan-stage settlements unmappable: 1 (A9: item absent from the park "
            "comment)",
        ]

    def test_evidence_for_a_comment_a_row_and_a_mixed_round(self) -> None:
        assert _validate().evidence() is None
        comment = _validate(_claim("A1", "ADOPTED"))
        row = _validate(_claim("A1", "ADOPTED", "row:2"))
        mixed = _validate(
            _claim("A1", "ADOPTED", "row:2"),
            _claim("P2", "CONFIRMED"),
            _claim("A1", "ALT-a", "c9"),
        )
        assert comment.evidence() == {"comment_id": "c9", "items": ["A1"]}
        assert row.evidence() == {"comment_id": "row:2", "items": ["A1"]}
        assert mixed.evidence() == {"comment_id": "row:2,c9", "items": ["A1", "P2"]}
        for outcome in (comment, row, mixed):
            AutoDevResult.model_validate(
                {
                    **_sentinel_payload(),
                    "resolution_consumed": True,
                    "resolution_evidence": outcome.evidence(),
                }
            )


def _sentinel_payload() -> dict[str, object]:
    block = extract_block(
        plan_sentinel(
            status="ambiguities_pending_resolution",
            next_actions=["user_resolve_ambiguities"],
            ambiguities=[{"question": "Which?"}],
        )
    )
    assert block is not None
    payload: dict[str, object] = json.loads(block)
    return payload


class TestStubs:
    def test_present(self) -> None:
        plan = f"# P\n\n## Deferred Premises\n\n- X holds — {DEFERRED_STUB}\n"
        assert unresolved_stubs(plan) == [f"- X holds — {DEFERRED_STUB}"]

    def test_resolved_and_outside_the_section(self) -> None:
        plan = (
            f"# P\n\n{DEFERRED_STUB}\n\n## Deferred Premises\n\n- X — check y\n"
            f"\n## Other\n\n{DEFERRED_STUB}\n"
        )
        assert unresolved_stubs(plan) == []

    def test_absent_section(self) -> None:
        assert unresolved_stubs("# P\n") == []


def test_tracker_observation_and_park_state_read_the_same_snapshot() -> None:
    """A park comment is never an operator-authority source."""
    snapshot = make_snapshot_from(
        [_park("p1", "2026-11-20T00:00:00Z"), make_comment("c9")],
        fetched_at=datetime(2026, 11, 21, tzinfo=UTC),
    )
    observation = observe_tracker(snapshot, SnapshotContext(github=True))
    assert [c["id"] for c in observation.comments] == ["c9"]
    assert park_state(snapshot).ordinal == 1
