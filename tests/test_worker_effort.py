"""Tests for the per-client worker effort resolver (#2350).

The resolver decides whether ``claude --bg`` gets ``--effort <level>``. Explicit
levels always forward; ``inherit`` never forwards; unset forwards ``medium``
only for full ``claude-opus-5-5*`` model ids.
"""

from __future__ import annotations

import logging

import pytest

from cw.models.worker_effort import WorkerEffort
from cw.worker_effort import WORKER_EFFORT_FLAG, resolve_worker_effort_args

_OPUS_PIN = "claude-opus-5-5"
_EXPLICIT_LEVELS = [
    member for member in WorkerEffort if member is not WorkerEffort.INHERIT
]


class TestWorkerEffortEnum:
    def test_flag_spelling_and_level_set_pinned(self) -> None:
        """Flag `--effort` and the closed level set match claude 2.1.323."""
        assert WORKER_EFFORT_FLAG == "--effort"
        assert {m.value for m in WorkerEffort} == {
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "inherit",
        }


class TestResolveWorkerEffortArgs:
    @pytest.mark.parametrize(
        "worker_model",
        [
            _OPUS_PIN,
            "claude-opus-5-5-20260101",
            " Claude-Opus-5-5 ",
        ],
    )
    def test_unset_on_opus_55_full_id_defaults_to_medium(
        self, worker_model: str
    ) -> None:
        assert resolve_worker_effort_args(worker_model, None) == [
            WORKER_EFFORT_FLAG,
            "medium",
        ]

    @pytest.mark.parametrize(
        "worker_model",
        [
            "claude-sonnet-5-5",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-8",
            "opus",
            "claude-opus-5",
            None,
            "",
        ],
    )
    def test_unset_without_default_emits_no_flag(
        self, worker_model: str | None
    ) -> None:
        assert resolve_worker_effort_args(worker_model, None) == []

    @pytest.mark.parametrize("effort", _EXPLICIT_LEVELS, ids=lambda m: m.value)
    @pytest.mark.parametrize(
        "worker_model",
        [None, "claude-sonnet-5", "claude-haiku-4-5-20251001", _OPUS_PIN],
    )
    def test_explicit_level_always_forwards(
        self, worker_model: str | None, effort: WorkerEffort
    ) -> None:
        assert resolve_worker_effort_args(worker_model, effort) == [
            WORKER_EFFORT_FLAG,
            effort.value,
        ]

    def test_explicit_high_beats_opus_default(self) -> None:
        assert resolve_worker_effort_args(_OPUS_PIN, WorkerEffort.HIGH) == [
            WORKER_EFFORT_FLAG,
            "high",
        ]

    @pytest.mark.parametrize("worker_model", [_OPUS_PIN, None, "claude-sonnet-5-5"])
    def test_inherit_never_emits_flag(self, worker_model: str | None) -> None:
        assert resolve_worker_effort_args(worker_model, WorkerEffort.INHERIT) == []


class TestWorkerEffortLog:
    def test_default_logs_one_info_record_with_source_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="cw.worker_effort"):
            resolve_worker_effort_args(_OPUS_PIN, None)
        records = [r for r in caplog.records if r.name == "cw.worker_effort"]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "worker_model=claude-opus-5-5" in message
        assert "level=medium" in message
        assert "source=default" in message

    def test_explicit_logs_source_explicit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="cw.worker_effort"):
            resolve_worker_effort_args("claude-sonnet-5", WorkerEffort.LOW)
        records = [r for r in caplog.records if r.name == "cw.worker_effort"]
        assert len(records) == 1
        assert "source=explicit" in records[0].getMessage()

    @pytest.mark.parametrize(
        ("worker_model", "effort"),
        [
            (_OPUS_PIN, WorkerEffort.INHERIT),
            ("claude-sonnet-5-5", None),
        ],
    )
    def test_empty_outcome_logs_nothing(
        self,
        worker_model: str,
        effort: WorkerEffort | None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="cw.worker_effort"):
            resolve_worker_effort_args(worker_model, effort)
        assert [r for r in caplog.records if r.name == "cw.worker_effort"] == []
