"""Tests for ``cw.models.usage`` (#3063, RFC 0018 C4)."""

from __future__ import annotations

from cw.models import Session
from cw.models.usage import (
    SessionUsage,
    TokenTotals,
    TurnUsage,
    UsageSnapshot,
    UsageStage,
    UsageState,
    UsageUnavailable,
    UsageUnavailableReason,
)
from tests._shared.session_seeding import _make_daemon_session


class TestSessionUsageTotalTokens:
    def test_none_when_only_unavailable_turns_exist(self) -> None:
        usage = SessionUsage(
            state=UsageState.UNAVAILABLE,
            unavailable_turns=2,
            unavailable_reason=UsageUnavailableReason.NO_USAGE_BLOCK,
        )

        assert usage.total_tokens is None

    def test_sums_input_and_output_over_by_model(self) -> None:
        usage = SessionUsage(
            state=UsageState.RECORDED,
            by_model={
                "a": TokenTotals(
                    input_tokens=100,
                    cached_input_tokens=40,
                    output_tokens=10,
                    reasoning_tokens=5,
                    cache_write_input_tokens=7,
                    turns=1,
                ),
                "b": TokenTotals(input_tokens=1, output_tokens=2, turns=1),
            },
        )

        # cached is a subset of input, reasoning a subset of output, and
        # cache-write is excluded.
        assert usage.total_tokens == 113


class TestEnumsAndRoundTrip:
    def test_json_round_trip_keeps_enum_values_as_strings(self) -> None:
        usage = SessionUsage(
            state=UsageState.PARTIAL,
            by_stage={UsageStage.FIX: TokenTotals(input_tokens=3, turns=1)},
            by_model={"m": TokenTotals(input_tokens=3, turns=1)},
            unavailable_turns=1,
            unavailable_reason=UsageUnavailableReason.TURN_FAILED,
        )

        dumped = usage.model_dump(mode="json")

        assert dumped["state"] == "partial"
        assert dumped["unavailable_reason"] == "turn_failed"
        assert list(dumped["by_stage"]) == ["fix"]
        assert SessionUsage.model_validate(dumped) == usage

    def test_stage_values_are_the_documented_set(self) -> None:
        assert {stage.value for stage in UsageStage} == {
            "plan",
            "plan_station",
            "impl",
            "finalize",
            "review_role",
            "engine_aggregate",
            "fix",
            "tracker_io",
            "session",
        }


class TestSessionDefaults:
    def test_session_without_a_usage_key_loads_none(self) -> None:
        raw = _make_daemon_session().model_dump(mode="json")
        raw.pop("usage", None)

        assert Session.model_validate(raw).usage is None

    def test_session_usage_round_trips_through_json(self) -> None:
        usage = SessionUsage(
            state=UsageState.RECORDED,
            by_model={"m": TokenTotals(input_tokens=1, output_tokens=2, turns=1)},
        )
        session = _make_daemon_session(usage=usage)

        assert Session.model_validate(session.model_dump(mode="json")).usage == usage


_NONE_FIELDS = {
    "input_tokens": None,
    "cached_input_tokens": None,
    "cache_write_input_tokens": None,
    "output_tokens": None,
    "reasoning_tokens": None,
}


class TestTurnUsage:
    def test_absent_when_the_four_reported_fields_are_none(self) -> None:
        assert TurnUsage(**_NONE_FIELDS).absent is True

    def test_not_absent_with_any_reported_field(self) -> None:
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            assert TurnUsage(**{**_NONE_FIELDS, field: 1}).absent is False

    def test_cache_write_alone_does_not_count_as_usage(self) -> None:
        assert TurnUsage(**{**_NONE_FIELDS, "cache_write_input_tokens": 9}).absent


def test_snapshot_and_unavailable_are_plain_tuples() -> None:
    snapshot = UsageSnapshot(SessionUsage(state=UsageState.RECORDED), None)
    assert snapshot.cost_breakdown is None
    assert (
        UsageUnavailable(UsageUnavailableReason.NO_OUTPUT).reason.value == "no_output"
    )
