"""Dispatch-loop sentinel parse-warning dedup (#1394).

The ``warned_blocks`` set is loop-owned and process-lifetime: a malformed
sentinel block warns once per run, not once per tick. These tests pin the
dedup at the parse seam, the cap-and-clear bound, and the bound running
before the tick's reconcile parses.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cw.dispatch.gating.usage_limit import (
    WARNED_BLOCKS_MAX_KEYS,
    _bound_warned_blocks,
)
from tests._reconcile_helpers import _shipped_salvage_payload, _write_salvage_transcript

if TYPE_CHECKING:
    import pytest

_MALFORMED_WARNING = "did not parse as JSON"
_MALFORMED_RECORD = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "<<<AUTO_DEV_RESULT\n{oops\nAUTO_DEV_RESULT>>>",
            }
        ],
    },
}


def _malformed_transcript(tmp_path: Path) -> Path:
    return _write_salvage_transcript(
        Path.home(),
        tmp_path / "wt-1394",
        "claude-1394",
        _shipped_salvage_payload(),
        extra_records=[_MALFORMED_RECORD],
    )


def _warning_count(caplog: pytest.LogCaptureFixture) -> int:
    return sum(
        1
        for record in caplog.records
        if record.levelno == logging.WARNING and _MALFORMED_WARNING in record.message
    )


def test_parse_warning_is_deduped_across_calls_with_shared_set(
    tmp_config_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = _malformed_transcript(tmp_path)
    warned: set[str] = set()
    with caplog.at_level(logging.WARNING, logger="cw.auto_dev_result"):
        first = _parse_sentinel_from_blocks(path, warned_blocks=warned)
        second = _parse_sentinel_from_blocks(path, warned_blocks=warned)

    assert first is not None
    assert second is not None
    assert _warning_count(caplog) == 1
    assert len(warned) == 1


def test_parse_warning_repeats_without_shared_set(
    tmp_config_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = _malformed_transcript(tmp_path)
    with caplog.at_level(logging.WARNING, logger="cw.auto_dev_result"):
        _parse_sentinel_from_blocks(path)
        _parse_sentinel_from_blocks(path)

    assert _warning_count(caplog) == 2


def test_bound_keeps_set_at_exact_cap() -> None:
    warned = {f"k{i}" for i in range(WARNED_BLOCKS_MAX_KEYS)}
    _bound_warned_blocks(warned)
    assert len(warned) == WARNED_BLOCKS_MAX_KEYS


def test_bound_clears_in_place_past_cap() -> None:
    warned = {f"k{i}" for i in range(WARNED_BLOCKS_MAX_KEYS + 1)}
    original = warned
    _bound_warned_blocks(warned)
    assert len(warned) == 0
    assert warned is original


def test_bound_is_noop_for_none_and_small_sets() -> None:
    _bound_warned_blocks(None)
    small: set[str] = {"a", "b"}
    _bound_warned_blocks(small)
    assert small == {"a", "b"}


def test_reconcile_usage_limited_bounds_before_reconcile_and_passes_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.dispatch.gating import _reconcile_usage_limited
    from cw.reconcile import ReconcileReport

    warned = {f"k{i}" for i in range(WARNED_BLOCKS_MAX_KEYS + 1)}
    seen: list[tuple[int, object]] = []

    def _spy_reconcile(**kwargs: object) -> ReconcileReport:
        seen.append((len(warned), kwargs.get("warned_blocks")))
        return ReconcileReport(usage_limited=False)

    monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", _spy_reconcile)

    assert _reconcile_usage_limited(warned_blocks=warned) is False
    assert seen == [(0, warned)]
    assert seen[0][1] is warned
