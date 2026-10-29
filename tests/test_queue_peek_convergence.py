"""Tests for the clean-mode convergence peek override (#2787, R5).

Covers :func:`cw.queue_peek_convergence.convergence_peek_override`, the
``AGE_REASON_PREFIX`` predicate pinned against the real
:func:`cw.queue_peek.recommend` reason strings, and the thin ``format_row`` hook
that routes through the override before the child-active suffix.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw import queue_peek
from cw.models import Stage
from cw.queue_peek import rows
from cw.queue_peek_convergence import (
    AGE_REASON_PREFIX,
    convergence_peek_override,
    is_age_based_reason,
)
from cw.review_convergence import LEDGER_RELATIVE_PATH
from tests._review_convergence_helpers import (
    ledger_with_rounds,
    round_finding,
    stopped_ledger,
    write_ledger,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundFinding

_NOW = dt.datetime(2026, 10, 27, 12, 0, 0, tzinfo=dt.UTC)
_AGE_PEEK = ("PEEK", "age 50min mature — check stage")
_AGE_STOP = ("STOP", "age 70min exceeded 60-min timeout — stop or hand off")
_IDLE_LIVE = float(queue_peek.IDLE_PEEK_MIN)


def _findings(prefix: str, count: int) -> list[RoundFinding]:
    return [round_finding(summary=f"{prefix} defect {n}") for n in range(count)]


def _falling_ledger() -> ConvergenceLedger:
    """In-progress ledger whose new-finding curve is 3, 2, 1."""
    ledger = ledger_with_rounds(_findings("a", 3), _findings("b", 2), _findings("c", 1))
    assert ledger.status == "in_progress"
    return ledger


def _flat_ledger() -> ConvergenceLedger:
    """In-progress ledger with a flat 2, 2 curve: neither falling nor stalled."""
    ledger = ledger_with_rounds(_findings("a", 2), _findings("b", 2))
    assert ledger.status == "in_progress"
    return ledger


def _stalled_in_progress_ledger() -> ConvergenceLedger:
    """A 3, 3, 3 ledger (``detect_no_progress`` is true) forced back in progress.

    The engine stops on the round that completes the stall, so a genuinely
    in-progress stalled ledger only arises mid-write; the stop fields are
    cleared by hand to reach the override's defensive PEEK branch.
    """
    stopped = stopped_ledger("no_progress", sha="sha-stall")
    return stopped.model_copy(update={"status": "in_progress", "stop_reason": None})


def _review_task(worktree: Path, **overrides: object) -> TicketTask:
    context_path = worktree / ".claude" / "cw-context.json"
    if not context_path.exists():
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            json.dumps(
                {
                    "schema_version": 15,
                    "ticket_id": "T-1",
                    "review": {"convergence": "clean"},
                }
            ),
            encoding="utf-8",
        )
    fields: dict[str, object] = {
        "stage": Stage.REVIEW,
        "worktree_path": worktree,
        "session_id": "sess1234",
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def _override(
    task: TicketTask,
    rec_reason: tuple[str, str],
    idle_min: float | None = 1.0,
) -> tuple[str, str] | None:
    return convergence_peek_override(
        task,
        rec_reason[0],
        rec_reason[1],
        idle_min=idle_min,
        idle_live_max_min=_IDLE_LIVE,
    )


# ---------------------------------------------------------------------------
# Predicate pin
# ---------------------------------------------------------------------------


def _non_age_prefixes() -> tuple[str, ...]:
    return ("idle ", "PR merged", "shipped", "unproductive attempt")


class TestAgePredicate:
    def test_prefix_constant(self) -> None:
        assert AGE_REASON_PREFIX == "age "

    @pytest.mark.parametrize(
        "reason",
        [
            "age 31min in normal range",
            "age 5min — early/healthy",
            "age 50min mature — check stage",
            "age 58min approaches 60-min timeout — stop or hand off",
            "age 90min exceeded 60-min timeout — stop or hand off",
        ],
    )
    def test_age_reasons_match(self, reason: str) -> None:
        assert is_age_based_reason(reason)

    @pytest.mark.parametrize(
        "reason",
        [
            "idle 20min, no PR — likely stuck; peek before stop",
            "PR merged + worker idle 9min — stuck in post-create wait",
            "shipped + PR open — auto-merge CI in progress",
            "unproductive attempt 3 — systemic, not transient",
            "no transcript timestamps — verify session is alive",
            "",
        ],
    )
    def test_non_age_reasons_do_not_match(self, reason: str) -> None:
        assert not is_age_based_reason(reason)

    def test_predicate_agrees_with_real_recommend_reasons(self) -> None:
        """Every reason ``recommend`` really returns is classified correctly.

        The ladder is swept across ages, idle gaps, PR states, sentinels and
        attempt counts. Reasons are split by their independently known
        non-age prefixes; the remainder must all satisfy the predicate and
        the known non-age reasons must not, so a reworded ladder string fails
        here instead of silently escaping the override.
        """
        ages = [5.0, 31.0, 46.0, 56.0, 59.5, 60.0, 90.0]
        idles: list[float | None] = [None, 0.5, 5.0, 8.0, 20.0]
        seen_age: set[str] = set()
        seen_other: set[str] = set()
        for age in ages:
            for idle in idles:
                for pr_state, sentinel, attempts in [
                    (None, None, 0),
                    (None, None, 3),
                    ("MERGED", None, 0),
                    ("OPEN", "shipped", 0),
                ]:
                    _, reason = queue_peek.recommend(
                        age_min=age,
                        idle_min=idle,
                        pr_state=pr_state,
                        sentinel_status=sentinel,
                        unproductive_attempts=attempts,
                    )
                    if reason.startswith(_non_age_prefixes()):
                        seen_other.add(reason)
                        assert not is_age_based_reason(reason), reason
                    else:
                        seen_age.add(reason)
                        assert is_age_based_reason(reason), reason
        # every age arm and every non-age arm was actually exercised
        joined = "\n".join(sorted(seen_age))
        for fragment in (
            "in normal range",
            "early/healthy",
            "mature",
            "approaches",
            "exceeded",
        ):
            assert fragment in joined, fragment
        other = "\n".join(sorted(seen_other))
        for fragment in ("idle ", "PR merged", "shipped", "unproductive attempt"):
            assert fragment in other, fragment

    def test_liveness_rewritten_age_stop_keeps_prefix(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=70.0,
            idle_min=0.5,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=0,
        )
        assert rec == "PEEK"
        assert "verify before stopping" in reason
        assert is_age_based_reason(reason)


# ---------------------------------------------------------------------------
# convergence_peek_override
# ---------------------------------------------------------------------------


class TestConvergencePeekOverride:
    @pytest.mark.parametrize("rec_reason", [_AGE_PEEK, _AGE_STOP])
    def test_falling_curve_turns_age_verdict_into_wait(
        self, tmp_path: Path, rec_reason: tuple[str, str]
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        result = _override(_review_task(tmp_path), rec_reason, idle_min=2.0)
        assert result is not None
        rec, reason = result
        assert rec == "WAIT"
        assert rec_reason[1] in reason
        assert "3→2→1" in reason

    def test_already_wait_age_verdict_is_left_alone_on_a_falling_curve(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        wait = ("WAIT", "age 35min in normal range")
        assert _override(_review_task(tmp_path), wait) is None

    def test_wait_age_verdict_on_a_stalled_ledger_escalates_to_peek(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, _stalled_in_progress_ledger())
        result = _override(
            _review_task(tmp_path), ("WAIT", "age 35min in normal range")
        )
        assert result is not None
        assert result[0] == "PEEK"

    def test_idle_at_threshold_still_overrides(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        result = _override(_review_task(tmp_path), _AGE_STOP, idle_min=_IDLE_LIVE)
        assert result is not None
        assert result[0] == "WAIT"

    def test_age_stop_with_idle_20_is_never_overridden(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        assert _override(_review_task(tmp_path), _AGE_STOP, idle_min=20.0) is None

    def test_age_stop_with_unknown_idle_is_never_overridden(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        assert _override(_review_task(tmp_path), _AGE_STOP, idle_min=None) is None

    def test_no_progress_ledger_gives_peek(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _stalled_in_progress_ledger())
        result = _override(_review_task(tmp_path), _AGE_STOP, idle_min=1.0)
        assert result is not None
        rec, reason = result
        assert rec == "PEEK"
        assert _AGE_STOP[1] in reason
        assert "no progress" in reason

    def test_no_progress_wins_over_a_falling_curve(self, tmp_path: Path) -> None:
        """5, 3, 3, 3 falls overall yet its last window stalls: PEEK, not WAIT."""
        ledger = ledger_with_rounds(
            _findings("a", 5),
            _findings("b", 3),
            _findings("c", 3),
            _findings("d", 3),
        )
        # round four completes the stall window and the engine stops there
        assert ledger.status == "stopped"
        ledger = ledger.model_copy(
            update={"status": "in_progress", "stop_reason": None}
        )
        write_ledger(tmp_path, ledger)
        result = _override(_review_task(tmp_path), _AGE_PEEK)
        assert result is not None
        assert result[0] == "PEEK"

    def test_small_count_tail_after_decline_is_not_peeked(self, tmp_path: Path) -> None:
        """5, 1, 1, 3 is a loop still fixing small items, not a no-progress PEEK."""
        ledger = ledger_with_rounds(
            _findings("a", 5),
            _findings("b", 1),
            _findings("c", 1),
            _findings("d", 3),
            max_cycles=12,
        )
        assert ledger.status == "in_progress"
        write_ledger(tmp_path, ledger)
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_decline_then_plateau_at_one_reads_as_converging(
        self, tmp_path: Path
    ) -> None:
        """5, 3, 1, 1, 1 no longer PEEKs as no progress: it falls to a WAIT."""
        ledger = ledger_with_rounds(
            _findings("a", 5),
            _findings("b", 3),
            _findings("c", 1),
            _findings("d", 1),
            _findings("e", 1),
            max_cycles=12,
        )
        assert ledger.status == "in_progress"
        write_ledger(tmp_path, ledger)
        result = _override(_review_task(tmp_path), _AGE_PEEK)
        assert result is not None
        rec, reason = result
        assert rec == "WAIT"
        assert "5→3→1→1→1" in reason
        assert "no progress" not in reason

    def test_awaiting_confirm_ledger_is_still_in_flight(self, tmp_path: Path) -> None:
        ledger = ledger_with_rounds(_findings("a", 2), [], confirm="codex")
        assert ledger.status == "awaiting_confirm"
        write_ledger(tmp_path, ledger)
        result = _override(_review_task(tmp_path), _AGE_PEEK)
        assert result is not None
        assert result[0] == "WAIT"
        assert "2→0" in result[1]

    def test_flat_curve_gives_no_override(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _flat_ledger())
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_single_round_gives_no_override(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, ledger_with_rounds(_findings("a", 3)))
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_no_rounds_gives_no_override(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, ledger_with_rounds())
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_stopped_ledger_gives_no_override(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, stopped_ledger("max_cycles", sha="sha-max"))
        assert _override(_review_task(tmp_path), _AGE_STOP) is None

    def test_converged_ledger_gives_no_override(self, tmp_path: Path) -> None:
        ledger = ledger_with_rounds(_findings("a", 2), [])
        assert ledger.status == "converged"
        write_ledger(tmp_path, ledger)
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_missing_ledger_gives_no_override(self, tmp_path: Path) -> None:
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    @pytest.mark.parametrize(
        "content",
        ["{not json", '{"schema_version": 1}', ""],
        ids=["malformed", "schema-mismatch", "empty"],
    )
    def test_corrupt_ledger_fails_open(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_non_utf8_ledger_fails_open(self, tmp_path: Path) -> None:
        path = tmp_path / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe\x00bad")
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    def test_unreadable_ledger_path_fails_open(self, tmp_path: Path) -> None:
        """A directory where the file should be raises OSError, not a ValueError."""
        (tmp_path / LEDGER_RELATIVE_PATH).mkdir(parents=True)
        assert _override(_review_task(tmp_path), _AGE_PEEK) is None

    @pytest.mark.parametrize(
        "stage", [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.FINALIZE]
    )
    def test_non_review_stage_gives_no_override(
        self, tmp_path: Path, stage: Stage
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        task = _review_task(tmp_path, stage=stage)
        assert _override(task, _AGE_PEEK) is None

    def test_no_worktree_gives_no_override(self) -> None:
        task = _make_ticket_task(stage=Stage.REVIEW, worktree_path=None)
        assert task.worktree_path is None
        assert _override(task, _AGE_PEEK) is None

    @pytest.mark.parametrize(
        "rec_reason",
        [
            ("STOP-OR-PEEK", "idle 20min, no PR — likely stuck; peek before stop"),
            ("PEEK", "idle 9min, no PR — check for tool denial"),
            ("STOP", "PR merged + worker idle 9min — stuck in post-create wait"),
            ("WAIT", "shipped + PR open — auto-merge CI in progress"),
            ("STOP", "unproductive attempt 3 — systemic, not transient"),
        ],
    )
    def test_non_age_verdicts_are_never_overridden(
        self, tmp_path: Path, rec_reason: tuple[str, str]
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        assert _override(_review_task(tmp_path), rec_reason, idle_min=1.0) is None


# ---------------------------------------------------------------------------
# format_row hook
# ---------------------------------------------------------------------------


def _row_info(
    *, age: float, idle: float, child_idle: float | None = None
) -> dict[str, Any]:
    def _ago(minutes: float) -> str:
        return (_NOW - dt.timedelta(minutes=minutes)).isoformat()

    info: dict[str, Any] = {
        "claim_started_at": _ago(age),
        "first_user_ts": _ago(age),
        "last_asst_ts": _ago(idle),
        "last_sentinel_status": None,
        "last_sentinel_stage": None,
        "last_pr_number": None,
        "signal_source": "transcript",
    }
    if child_idle is not None:
        info["subagent_last_asst_ts"] = _ago(child_idle)
    return info


class TestFormatRowHook:
    def test_falling_curve_row_is_wait_with_curve_text(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        task = _review_task(tmp_path)
        row = queue_peek.format_row(task, _row_info(age=70, idle=2), _NOW)
        assert row["recommend"] == "WAIT"
        assert "3→2→1" in row["reason"]
        assert row["age_min"] == pytest.approx(70.0)

    def test_child_suffix_is_appended_after_the_override(self, tmp_path: Path) -> None:
        """Ordering pin: the override sees the bare reason, the child suffix follows."""
        write_ledger(tmp_path, _falling_ledger())
        task = _review_task(tmp_path)
        info = _row_info(age=70, idle=2, child_idle=1)
        with patch.object(
            rows,
            "convergence_peek_override",
            wraps=rows.convergence_peek_override,
        ) as spy:
            row = queue_peek.format_row(task, info, _NOW)
        assert spy.call_count == 1
        assert "child subagent active" not in spy.call_args.args[2]
        assert row["recommend"] == "WAIT"
        reason: str = row["reason"]
        assert "3→2→1" in reason
        assert reason.endswith("waiting on it, not idle")
        assert reason.index("3→2→1") < reason.index("child subagent active")

    def test_hook_passes_idle_and_peek_threshold(self, tmp_path: Path) -> None:
        task = _review_task(tmp_path)
        with patch.object(rows, "convergence_peek_override", return_value=None) as spy:
            queue_peek.format_row(task, _row_info(age=70, idle=2), _NOW)
        assert spy.call_args.kwargs == {
            "idle_min": pytest.approx(2.0),
            "idle_live_max_min": queue_peek.IDLE_PEEK_MIN,
        }

    def test_blocking_mode_row_is_byte_identical_to_unhooked(
        self, tmp_path: Path
    ) -> None:
        task = _review_task(tmp_path)  # no ledger: blocking mode
        info = _row_info(age=70, idle=2)
        hooked = queue_peek.format_row(task, info, _NOW)
        with patch.object(rows, "convergence_peek_override", return_value=None):
            unhooked = queue_peek.format_row(task, info, _NOW)
        assert hooked == unhooked
        assert hooked["recommend"] == "PEEK"
        assert "3→2→1" not in hooked["reason"]

    def test_non_review_stage_row_ignores_a_ledger_on_disk(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, _falling_ledger())
        task = _review_task(tmp_path, stage=Stage.IMPL)
        row = queue_peek.format_row(task, _row_info(age=70, idle=2), _NOW)
        assert row["recommend"] == "PEEK"
        assert "3→2→1" not in row["reason"]

    def test_stalled_idle_review_stays_flagged(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        task = _review_task(tmp_path)
        row = queue_peek.format_row(task, _row_info(age=70, idle=20), _NOW)
        assert row["recommend"] == "STOP"
        assert "3→2→1" not in row["reason"]

    def test_ledger_for_another_ticket_is_not_trusted(self, tmp_path: Path) -> None:
        ledger = _falling_ledger().model_copy(update={"ticket_id": "T-2"})
        write_ledger(tmp_path, ledger)
        assert _override(_review_task(tmp_path), _AGE_STOP) is None

    def test_non_clean_context_is_not_trusted(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        context_path = tmp_path / ".claude" / "cw-context.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(
            json.dumps({"schema_version": 15, "review": {"convergence": "blocking"}}),
            encoding="utf-8",
        )
        assert _override(_review_task(tmp_path), _AGE_STOP) is None

    def test_invalid_utf8_context_fails_open(self, tmp_path: Path) -> None:
        write_ledger(tmp_path, _falling_ledger())
        context_path = tmp_path / ".claude" / "cw-context.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_bytes(b"\xff\xfe\x00")
        assert _override(_review_task(tmp_path), _AGE_STOP) is None
