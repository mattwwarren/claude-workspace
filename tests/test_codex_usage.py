"""Tests for ``cw.codex_usage`` (#3063, RFC 0018 C4): the per-run accumulator."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import cw.codex_usage as module
from cw._lock_guard import LockRank, is_rank_held
from cw.codex_review._audit_events import parse_turn_usage
from cw.codex_runner import CodexRunResult, FakeCodexRunner, relabeled
from cw.codex_usage import (
    NO_USAGE,
    RecordingRunner,
    UsageAccumulator,
    price_totals,
)
from cw.models.orchestrator_config.stage import ModelPrice
from cw.models.usage import (
    USAGE_UNKNOWN_MODEL,
    TokenTotals,
    TurnUsage,
    UsageStage,
    UsageState,
    UsageUnavailable,
    UsageUnavailableReason,
)
from tests._review_convergence_helpers import codex_audit_fixture
from tests._tool_fakes import ScriptedCodexRunner, codex_usage_stdout

# Illustrative operator prices, USD per million tokens.
_PRICE = ModelPrice(
    input_usd_per_mtok=1.25, cached_input_usd_per_mtok=0.125, output_usd_per_mtok=10.0
)
_JSON_ARGV = ["codex", "exec", "--json", "--ephemeral", "-m", "gpt-x"]
_LOGGER = "cw.codex_usage"


def _acc(prices: dict[str, ModelPrice] | None = None) -> UsageAccumulator:
    return UsageAccumulator(session_id="sid1", ticket_id="T-1", prices=prices or {})


def _runner(acc: UsageAccumulator, inner: FakeCodexRunner) -> RecordingRunner:
    return acc.recording_runner(UsageStage.PLAN, inner)


class TestPriceFormula:
    def test_capture_numbers(self) -> None:
        # The committed 0.167.0 capture: input 18588 (cached 0), output 37.
        totals = TokenTotals(input_tokens=18588, output_tokens=37, reasoning_tokens=20)

        cost = price_totals(totals, _PRICE)

        assert cost == pytest.approx(18588 * 1.25e-6 + 37 * 10e-6)
        assert cost == pytest.approx(0.023605)

    def test_cached_is_a_subset_of_input(self) -> None:
        totals = TokenTotals(
            input_tokens=18588, cached_input_tokens=10000, output_tokens=37
        )

        assert price_totals(totals, _PRICE) == pytest.approx(
            8588 * 1.25e-6 + 10000 * 0.125e-6 + 37 * 10e-6
        )

    def test_reasoning_is_not_added_and_cache_write_is_unpriced(self) -> None:
        plain = TokenTotals(input_tokens=100, output_tokens=10)
        noisy = TokenTotals(
            input_tokens=100,
            output_tokens=10,
            reasoning_tokens=999,
            cache_write_input_tokens=999,
        )

        assert price_totals(noisy, _PRICE) == price_totals(plain, _PRICE)

    def test_cached_larger_than_input_never_goes_negative(self) -> None:
        totals = TokenTotals(input_tokens=5, cached_input_tokens=9, output_tokens=0)

        assert price_totals(totals, _PRICE) == pytest.approx(9 * 0.125e-6)


class TestSnapshot:
    def test_no_turns_gives_none(self) -> None:
        assert _acc().snapshot() is None

    def test_no_prices_gives_tokens_only_and_state_recorded(self) -> None:
        acc = _acc()
        acc.record_turn(
            UsageStage.PLAN,
            "plan",
            "gpt-x",
            TurnUsage(100, 10, 0, 5, 1),
            completed=True,
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.usage.state is UsageState.RECORDED
        assert snap.cost_breakdown is None
        assert snap.usage.by_stage[UsageStage.PLAN].input_tokens == 100
        assert snap.usage.by_model["gpt-x"].turns == 1
        assert snap.usage.total_tokens == 105
        assert acc.friction_token() is None

    def test_model_without_a_price_entry_gives_tokens_only(self) -> None:
        acc = _acc({"other": _PRICE})
        acc.record_turn(
            UsageStage.IMPL, "impl", "gpt-x", TurnUsage(1, 0, 0, 1, 0), completed=True
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.cost_breakdown is None
        assert snap.usage.state is UsageState.RECORDED

    def test_exact_match_lookup_only(self) -> None:
        acc = _acc({"gpt-x": _PRICE})
        acc.record_turn(
            UsageStage.IMPL,
            "impl",
            "gpt-x-mini",
            TurnUsage(1000, 0, 0, 10, 0),
            completed=True,
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.cost_breakdown is None

    def test_priced_model_gets_a_dollar_figure_keyed_by_model(self) -> None:
        acc = _acc({"gpt-x": _PRICE})
        acc.record_turn(
            UsageStage.PLAN,
            "plan",
            "gpt-x",
            TurnUsage(18588, 0, 0, 37, 20),
            completed=True,
        )
        acc.record_turn(
            UsageStage.FIX, "fix", "gpt-x", TurnUsage(100, 0, 0, 1, 0), completed=True
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.cost_breakdown is not None
        assert snap.cost_breakdown["gpt-x"] == pytest.approx(
            (18588 + 100) * 1.25e-6 + 38 * 10e-6
        )

    def test_unknown_model_is_never_priced(self) -> None:
        acc = _acc({USAGE_UNKNOWN_MODEL: _PRICE})
        acc.record_turn(
            UsageStage.TRACKER_IO,
            "tracker_io",
            USAGE_UNKNOWN_MODEL,
            TurnUsage(10, 0, 0, 1, 0),
            completed=True,
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.cost_breakdown is None
        assert snap.usage.state is UsageState.RECORDED
        assert USAGE_UNKNOWN_MODEL in snap.usage.by_model

    def test_partial_usage_is_partial_with_no_dollars_for_that_model(self) -> None:
        acc = _acc({"gpt-x": _PRICE})
        acc.record_turn(
            UsageStage.PLAN,
            "plan",
            "gpt-x",
            TurnUsage(9, None, None, None, None),
            completed=True,
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.usage.state is UsageState.PARTIAL
        assert snap.cost_breakdown is None
        assert snap.usage.by_model["gpt-x"].input_tokens == 9

    def test_price_configured_but_tokens_none_is_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc({"gpt-x": _PRICE})
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            acc.record_turn(
                UsageStage.PLAN,
                "plan",
                "gpt-x",
                UsageUnavailable(UsageUnavailableReason.NO_USAGE_BLOCK),
                completed=True,
            )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.usage.state is UsageState.UNAVAILABLE
        assert snap.usage.unavailable_reason is UsageUnavailableReason.NO_USAGE_BLOCK
        assert snap.usage.unavailable_turns == 1
        assert snap.usage.total_tokens is None
        assert snap.cost_breakdown is None

    def test_available_and_unavailable_turns_make_a_partial_state(self) -> None:
        acc = _acc()
        acc.record_turn(
            UsageStage.PLAN, "plan", "gpt-x", TurnUsage(5, 0, 0, 1, 0), completed=True
        )
        acc.record_turn(
            UsageStage.FIX,
            "fix",
            "gpt-x",
            UsageUnavailable(UsageUnavailableReason.NO_OUTPUT),
            completed=True,
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.usage.state is UsageState.PARTIAL
        assert snap.usage.unavailable_turns == 1
        assert acc.friction_token() == (
            "usage_unavailable: 1 completed codex turn(s) reported no token usage "
            "(reason=no_output)"
        )

    def test_a_failed_turn_with_a_usage_block_still_records_tokens(self) -> None:
        acc = _acc()
        acc.record_turn(
            UsageStage.FIX, "fix", "gpt-x", TurnUsage(7, 0, 0, 1, 0), completed=False
        )

        snap = acc.snapshot()

        assert snap is not None
        assert snap.usage.by_stage[UsageStage.FIX].turns == 1

    def test_dirty_tracks_records_since_the_last_snapshot(self) -> None:
        acc = _acc()
        assert acc.dirty is False
        acc.record_turn(
            UsageStage.PLAN, "plan", "gpt-x", TurnUsage(1, 0, 0, 1, 0), completed=True
        )
        assert acc.dirty is True
        acc.snapshot()
        assert acc.dirty is False


class TestWarningAndMarker:
    def test_exit_zero_without_usage_warns_once_with_a_friction_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            acc.record_turn(
                UsageStage.FINALIZE,
                "finalize",
                "gpt-x",
                UsageUnavailable(UsageUnavailableReason.NO_OUTPUT),
                completed=True,
            )

        messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        assert messages == [
            "usage_unavailable: role=finalize session=sid1 ticket=T-1 "
            "stage=finalize reason=no_output"
        ]
        token = acc.friction_token()
        assert token is not None
        assert token.startswith("usage_unavailable: ")

    @pytest.mark.parametrize(
        ("parsed", "completed"),
        [
            (UsageUnavailable(UsageUnavailableReason.TURN_FAILED), True),
            (UsageUnavailable(UsageUnavailableReason.NO_OUTPUT), False),
            (TurnUsage(1, 0, 0, 1, 0), True),
        ],
    )
    def test_silent_cases(
        self,
        caplog: pytest.LogCaptureFixture,
        parsed: TurnUsage | UsageUnavailable,
        *,
        completed: bool,
    ) -> None:
        acc = _acc()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            acc.record_turn(
                UsageStage.PLAN, "plan", "gpt-x", parsed, completed=completed
            )

        assert not [r for r in caplog.records if r.name == _LOGGER]
        assert acc.friction_token() is None


class TestRecordingRunner:
    def test_records_the_model_after_dash_m(self) -> None:
        acc = _acc()
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=10, output_tokens=2)
        )

        result = _runner(acc, inner).run(Path("/tmp"), list(_JSON_ARGV), None)

        assert result.stdout == inner.stdout
        snap = acc.snapshot()
        assert snap is not None
        assert list(snap.usage.by_model) == ["gpt-x"]
        assert list(snap.usage.by_stage) == [UsageStage.PLAN]

    def test_no_dash_m_uses_the_unknown_model(self) -> None:
        acc = _acc()
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=10, output_tokens=2)
        )

        _runner(acc, inner).run(Path("/tmp"), ["codex", "exec", "--json"], None)

        snap = acc.snapshot()
        assert snap is not None
        assert list(snap.usage.by_model) == [USAGE_UNKNOWN_MODEL]

    def test_dangling_dash_m_uses_the_unknown_model(self) -> None:
        acc = _acc()
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=10, output_tokens=2)
        )

        _runner(acc, inner).run(Path("/tmp"), ["codex", "exec", "--json", "-m"], None)

        snap = acc.snapshot()
        assert snap is not None
        assert list(snap.usage.by_model) == [USAGE_UNKNOWN_MODEL]

    def test_an_argv_without_json_is_skipped_without_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc()
        inner = FakeCodexRunner(stdout="")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _runner(acc, inner).run(Path("/tmp"), ["codex", "--version"], None)

        assert acc.snapshot() is None
        assert not caplog.records

    @pytest.mark.parametrize(("returncode", "timed_out"), [(1, False), (-1, True)])
    def test_a_failed_turn_with_empty_stdout_is_silent(
        self, caplog: pytest.LogCaptureFixture, returncode: int, timed_out: bool
    ) -> None:
        acc = _acc()
        inner = FakeCodexRunner(returncode=returncode, timed_out=timed_out)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _runner(acc, inner).run(Path("/tmp"), list(_JSON_ARGV), None)

        assert not caplog.records
        assert acc.friction_token() is None

    def test_exit_zero_with_empty_stdout_warns_with_the_default_role(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _runner(acc, FakeCodexRunner()).run(Path("/tmp"), list(_JSON_ARGV), None)

        assert "role=plan " in caplog.records[-1].getMessage()
        assert "ticket=T-1" in caplog.records[-1].getMessage()
        assert acc.friction_token() is not None

    def test_a_relabelled_role_is_used_in_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc()
        runner = relabeled(
            acc.recording_runner(UsageStage.REVIEW_ROLE, FakeCodexRunner()),
            role="security",
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            runner.run(Path("/tmp"), list(_JSON_ARGV), None)

        assert "role=security " in caplog.records[-1].getMessage()
        assert "stage=review_role" in caplog.records[-1].getMessage()

    def test_the_capture_is_priced_end_to_end(self) -> None:
        acc = _acc({"gpt-x": _PRICE})
        inner = FakeCodexRunner(
            stdout=codex_audit_fixture("clean_structured_answer_0_167_0.jsonl")
        )

        _runner(acc, inner).run(Path("/tmp"), list(_JSON_ARGV), None)

        snap = acc.snapshot()
        assert snap is not None
        assert snap.cost_breakdown == pytest.approx({"gpt-x": 0.023605})

    def test_a_scripted_runner_result_is_passed_through_unchanged(self) -> None:
        acc = _acc()
        reply = CodexRunResult(
            returncode=0,
            stdout=codex_usage_stdout(input_tokens=1, output_tokens=1),
            stderr="",
        )
        scripted = ScriptedCodexRunner().add(lambda _argv, _stdin: True, reply)

        result = acc.recording_runner(UsageStage.IMPL, scripted).run(
            Path("/tmp"), list(_JSON_ARGV), None
        )

        assert result is reply
        assert acc.snapshot() is not None


class TestRelabeled:
    def test_no_op_on_a_fake_runner(self) -> None:
        fake = FakeCodexRunner()

        assert relabeled(fake, UsageStage.FIX) is fake
        assert relabeled(fake, role="x") is fake

    def test_no_op_on_a_scripted_runner(self) -> None:
        scripted = ScriptedCodexRunner()

        assert relabeled(scripted, UsageStage.FIX) is scripted

    def test_relabel_does_not_double_record(self) -> None:
        acc = _acc()
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=10, output_tokens=2)
        )
        base = acc.recording_runner(UsageStage.PLAN, inner)

        relabeled(relabeled(base, UsageStage.PLAN_STATION), role="r").run(
            Path("/tmp"), list(_JSON_ARGV), None
        )

        snap = acc.snapshot()
        assert snap is not None
        assert list(snap.usage.by_stage) == [UsageStage.PLAN_STATION]
        assert snap.usage.by_stage[UsageStage.PLAN_STATION].turns == 1
        assert len(inner.calls) == 1

    def test_kind_only_relabel_resets_the_role_to_the_kind_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = _acc()
        base = acc.recording_runner(UsageStage.REVIEW_ROLE, FakeCodexRunner())

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            relabeled(base, UsageStage.ENGINE_AGGREGATE).run(
                Path("/tmp"), list(_JSON_ARGV), None
            )

        assert "role=engine_aggregate " in caplog.records[-1].getMessage()

    def test_role_only_relabel_keeps_the_kind(self) -> None:
        acc = _acc()
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=1, output_tokens=1)
        )
        base = acc.recording_runner(UsageStage.REVIEW_ROLE, inner)

        relabeled(base, role="arch").run(Path("/tmp"), list(_JSON_ARGV), None)

        snap = acc.snapshot()
        assert snap is not None
        assert list(snap.usage.by_stage) == [UsageStage.REVIEW_ROLE]


class TestNoUsage:
    def test_records_nothing_and_warns_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            NO_USAGE.record_turn(
                UsageStage.PLAN,
                "plan",
                "gpt-x",
                UsageUnavailable(UsageUnavailableReason.NO_OUTPUT),
                completed=True,
            )

        assert NO_USAGE.snapshot() is None
        assert NO_USAGE.friction_token() is None
        assert NO_USAGE.dirty is False
        assert not caplog.records

    def test_disabled_accumulator_is_a_fresh_instance(self) -> None:
        assert UsageAccumulator.disabled().snapshot() is None


class TestLockDiscipline:
    def test_parse_and_snapshot_run_without_the_sessions_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        held: list[bool] = []

        def spy_parse(stdout: str) -> TurnUsage | UsageUnavailable:
            held.append(is_rank_held(LockRank.SESSIONS))
            return parse_turn_usage(stdout)

        monkeypatch.setattr(module, "parse_turn_usage", spy_parse)
        acc = _acc({"gpt-x": _PRICE})
        inner = FakeCodexRunner(
            stdout=codex_usage_stdout(input_tokens=10, output_tokens=2)
        )

        _runner(acc, inner).run(Path("/tmp"), list(_JSON_ARGV), None)
        original = UsageAccumulator.snapshot

        def spy_snapshot(self: UsageAccumulator) -> object:
            held.append(is_rank_held(LockRank.SESSIONS))
            return original(self)

        monkeypatch.setattr(UsageAccumulator, "snapshot", spy_snapshot)
        acc.snapshot()

        assert held == [False, False]
