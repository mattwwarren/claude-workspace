"""Tests for cw.dev_queue.gate_backoff — the gate-block retry backoff (#2717)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cw.dev_queue.gate_backoff as gate_backoff_mod
from cw.dev_queue.gate_backoff import (
    GATE_BLOCK_BACKOFF_CAP_SECONDS,
    GATE_BLOCK_BACKOFF_INITIAL_SECONDS,
    clear_gate_block_wait,
    gate_block_page_note,
    gate_wait_released,
    is_gate_block,
    record_gate_block,
    reset_gate_block_unless_gate,
)
from cw.models import Stage, TicketTask
from cw.models.gate_block import GateBlockBackoff
from tests.conftest import _make_ticket_task

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_MGB = "merge_gate_blocked"
_PPPO = "prior_pipeline_pr_open"


def _record(
    task: TicketTask,
    *,
    status: str | None = _MGB,
    blocker_reason: str | None = _PPPO,
    blocking_pr: int | None = 88,
    now: datetime = _NOW,
) -> str:
    return record_gate_block(
        task,
        status=status,
        blocker_reason=blocker_reason,
        blocking_pr=blocking_pr,
        now=now,
    )


def _gate_task(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.IMPL, "stage_base_ref": "abc123"}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


class TestIsGateBlock:
    @pytest.mark.parametrize(
        ("status", "reason", "expected"),
        [
            pytest.param(_MGB, None, True, id="merge_gate_blocked_no_reason"),
            pytest.param(_MGB, "agent_block", True, id="merge_gate_blocked_other"),
            pytest.param("blocked", _PPPO, True, id="blocked_prior_pipeline"),
            pytest.param("blocked", "automerge_not_armed", False, id="automerge"),
            pytest.param("stale_dispatch", "pr_already_open", False, id="stale"),
            pytest.param("blocked", None, False, id="blocked_no_reason"),
            pytest.param(None, None, False, id="none_none"),
        ],
    )
    def test_classification(
        self, status: str | None, reason: str | None, expected: bool
    ) -> None:
        assert is_gate_block(status, reason) is expected


class TestRecordGateBlock:
    def test_first_occurrence(self) -> None:
        task = _gate_task()

        _record(task)

        assert task.gate_block is not None
        assert task.gate_block.count == 1
        assert task.next_eligible_at == _NOW + timedelta(seconds=60)
        assert task.gate_block.until == task.next_eligible_at

    def test_same_fingerprint_escalates(self) -> None:
        task = _gate_task()
        counts: list[int] = []
        delays: list[float] = []

        for _ in range(3):
            _record(task)
            assert task.gate_block is not None
            assert task.next_eligible_at is not None
            counts.append(task.gate_block.count)
            delays.append((task.next_eligible_at - _NOW).total_seconds())
            # A spawn success clears the wait between parks, as in production.
            task.next_eligible_at = None

        assert counts == [1, 2, 3]
        assert delays == [60, 120, 240]

    def test_cap_holds_while_count_rises(self) -> None:
        task = _gate_task()
        note = ""
        for _ in range(8):
            task.next_eligible_at = None
            note = _record(task)

        assert task.gate_block is not None
        assert task.gate_block.count == 8
        assert task.next_eligible_at == _NOW + timedelta(
            seconds=GATE_BLOCK_BACKOFF_CAP_SECONDS
        )
        assert note.startswith("occurrence=8 ")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("blocking_pr", 99, id="blocking_pr"),
            pytest.param("stage_base_ref", "def456", id="stage_base_ref"),
            pytest.param("blocker_reason", None, id="reason"),
            pytest.param("stage", Stage.FINALIZE, id="stage"),
        ],
    )
    def test_changed_fingerprint_resets(self, field: str, value: object) -> None:
        task = _gate_task()
        _record(task)
        _record(task)
        assert task.gate_block is not None
        assert task.gate_block.count == 2

        if field == "blocking_pr":
            assert isinstance(value, int)
            _record(task, blocking_pr=value)
        elif field == "blocker_reason":
            _record(task, blocker_reason=None)
        else:
            setattr(task, field, value)
            _record(task)

        assert task.gate_block.count == 1

    def test_none_parts_give_a_stable_fingerprint(self) -> None:
        task = _gate_task(stage_base_ref=None)

        _record(task, blocking_pr=None)
        _record(task, blocking_pr=None)

        assert task.gate_block is not None
        assert task.gate_block.count == 2

    def test_later_existing_window_is_kept(self) -> None:
        usage_limit = _NOW + timedelta(hours=2)
        task = _gate_task(next_eligible_at=usage_limit)

        note = _record(task)

        assert task.next_eligible_at == usage_limit
        assert task.gate_block is not None
        assert task.gate_block.until == _NOW + timedelta(seconds=60)
        assert note == gate_block_page_note(1, usage_limit)

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param(_NOW + timedelta(seconds=10), id="earlier"),
            pytest.param(_NOW - timedelta(minutes=5), id="expired"),
        ],
    )
    def test_earlier_existing_window_is_overwritten(self, existing: datetime) -> None:
        task = _gate_task(next_eligible_at=existing)

        _record(task)

        assert task.next_eligible_at == _NOW + timedelta(seconds=60)

    def test_other_counters_untouched(self) -> None:
        task = _gate_task(spawn_error_count=2, attempts=5, unproductive_attempts=3)

        _record(task)

        assert task.spawn_error_count == 2
        assert task.attempts == 5
        assert task.unproductive_attempts == 3

    def test_returns_the_exact_note(self) -> None:
        task = _gate_task()

        expected = "occurrence=1 next_eligible_at=2026-07-16T12:01:00+00:00"
        assert _record(task) == expected


class TestGateBlockPageNote:
    def test_exact_string(self) -> None:
        when = datetime(2026, 7, 16, 12, 1, tzinfo=UTC)
        assert (
            gate_block_page_note(1, when)
            == "occurrence=1 next_eligible_at=2026-07-16T12:01:00+00:00"
        )

    def test_microseconds_are_dropped(self) -> None:
        when = datetime(2026, 7, 16, 12, 1, 0, 123456, tzinfo=UTC)
        assert (
            gate_block_page_note(3, when)
            == "occurrence=3 next_eligible_at=2026-07-16T12:01:00+00:00"
        )

    def test_initial_constant(self) -> None:
        assert GATE_BLOCK_BACKOFF_INITIAL_SECONDS == 60


class TestClearGateBlockWait:
    def test_clears_own_wait_and_keeps_count(self) -> None:
        task = _gate_task()
        _record(task)

        clear_gate_block_wait(task)

        assert task.next_eligible_at is None
        assert task.gate_block is not None
        assert task.gate_block.count == 1

    def test_leaves_a_later_usage_limit_window(self) -> None:
        task = _gate_task()
        _record(task)
        later = _NOW + timedelta(hours=3)
        task.next_eligible_at = later

        clear_gate_block_wait(task)

        assert task.next_eligible_at == later

    def test_leaves_a_spawn_error_window_without_gate_block(self) -> None:
        window = _NOW + timedelta(seconds=8)
        task = _gate_task(next_eligible_at=window, spawn_error_count=3)

        clear_gate_block_wait(task)

        assert task.next_eligible_at == window

    def test_noop_after_spawn_success_cleared_the_wait(self) -> None:
        task = _gate_task()
        _record(task)
        task.next_eligible_at = None

        clear_gate_block_wait(task)

        assert task.next_eligible_at is None
        assert task.gate_block is not None


class TestGateWaitReleased:
    def test_disabled_backoff_releases_the_gates_own_wait(self) -> None:
        task = _gate_task()
        _record(task)

        assert gate_wait_released(task, backoff_enabled=False) is True
        # Read-only: the row is untouched.
        assert task.next_eligible_at is not None

    def test_enabled_backoff_releases_nothing(self) -> None:
        task = _gate_task()
        _record(task)

        assert gate_wait_released(task, backoff_enabled=True) is False

    def test_a_later_usage_limit_window_is_not_released(self) -> None:
        task = _gate_task()
        _record(task)
        task.next_eligible_at = _NOW + timedelta(hours=3)

        assert gate_wait_released(task, backoff_enabled=False) is False

    def test_a_spawn_error_window_without_gate_block_is_not_released(self) -> None:
        task = _gate_task(next_eligible_at=_NOW + timedelta(seconds=8))

        assert gate_wait_released(task, backoff_enabled=False) is False


class TestResetGateBlockUnlessGate:
    def test_non_gate_outcome_clears_record_only(self) -> None:
        task = _gate_task()
        _record(task)
        wait = task.next_eligible_at

        reset_gate_block_unless_gate(task, "blocked", "agent_block")

        assert task.gate_block is None
        assert task.next_eligible_at == wait

    def test_gate_outcome_keeps_record(self) -> None:
        task = _gate_task()
        _record(task)

        reset_gate_block_unless_gate(task, _MGB, None)

        assert task.gate_block is not None
        assert task.gate_block.count == 1

    def test_record_model_is_used(self) -> None:
        task = _gate_task()
        _record(task)
        assert isinstance(task.gate_block, GateBlockBackoff)


class TestImportDiscipline:
    _ALLOWED_TOP_LEVEL = frozenset(
        {
            "__future__",
            "cw._util",
            "cw.auto_dev_result",
            "cw.dev_queue.gates",
            "cw.models.gate_block",
            "datetime",
            "typing",
        }
    )
    _FORBIDDEN_PREFIXES = ("cw.queue_rows", "cw.dispatch")

    def _tree(self) -> ast.Module:
        source_file = gate_backoff_mod.__file__
        assert source_file is not None
        return ast.parse(Path(source_file).read_text(encoding="utf-8"))

    @staticmethod
    def _module_names(nodes: list[ast.stmt] | list[ast.AST]) -> set[str]:
        names: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def test_imports_first_in_a_cold_interpreter(self) -> None:
        subprocess.run(
            [sys.executable, "-I", "-c", "import cw.dev_queue.gate_backoff"],
            check=True,
            env=os.environ.copy(),
        )

    def test_top_level_imports_are_leaves(self) -> None:
        top_level = self._module_names(list(self._tree().body))
        assert top_level <= self._ALLOWED_TOP_LEVEL

    def test_never_imports_queue_rows_or_dispatch(self) -> None:
        every = self._module_names(list(ast.walk(self._tree())))
        assert not any(name.startswith(self._FORBIDDEN_PREFIXES) for name in every), (
            every
        )

    def test_reason_literal_matches_queue_rows(self) -> None:
        """queue_rows keeps a deliberately local literal; pin the two equal."""
        import cw.auto_dev_result
        import cw.queue_rows

        assert (
            cw.auto_dev_result.BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN
            == cw.queue_rows._PRIOR_PIPELINE_PR_OPEN_REASON
        )
