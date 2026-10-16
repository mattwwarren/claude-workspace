"""Tests for ``cw.dispatch.stale_breadcrumbs`` (#2694).

The ``dispatch_loop_stale`` page's breadcrumb names why the loop is stuck:
the last tick's skip reason, its occupancy and running/cap counts, and the
ceiling-counted sessions no occupied row owns (read now, from state).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cw.dispatch.stale_breadcrumbs import build_stale_breadcrumbs

from cw.config import save_state
from cw.dev_queue import save_dev_queue
from cw.exceptions import CwError
from cw.models import CwState, DevQueueStore, QueueItemStatus
from tests._reconcile_helpers import events_doc_bullet
from tests.conftest import _make_daemon_session, _make_tick_summary, _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_CLIENT = "client-a"
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_OLD_PREFIX = "pending=2 age_s=200"


def _seed_sessions(count: int, *, start: int = 0) -> None:
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id=f"s-{n}",
                    name=f"{_CLIENT}/auto-dev/T-{n}",
                    client=_CLIENT,
                    started_at=_NOW - timedelta(hours=10) + timedelta(minutes=n),
                )
                for n in range(start, start + count)
            ]
        )
    )


@pytest.mark.parametrize("skip", ["cap_full", "lane_cap_blocked", "none"])
def test_names_skip_occupancy_and_running_from_the_tick(
    tmp_config_dir: Path, skip: str
) -> None:
    tick = _make_tick_summary(
        pending=2,
        tick_at=_NOW - timedelta(seconds=200),
        skip_reason=skip,
        occupied=2,
        running=2,
        cap=2,
    )

    text = build_stale_breadcrumbs(_CLIENT, tick, _NOW)

    assert text == (
        f"{_OLD_PREFIX} skip={skip} occupied=2 running=2/2 orphan_sessions=none"
    )


def test_names_orphan_sessions_with_their_ticket(tmp_config_dir: Path) -> None:
    _seed_sessions(2)

    text = build_stale_breadcrumbs(_CLIENT, _make_tick_summary(**_tick_kwargs()), _NOW)

    assert text.startswith(_OLD_PREFIX + " ")
    assert text.endswith("orphan_sessions=s-0 (T-0), s-1 (T-1)")


def test_caps_the_orphan_list_at_five(tmp_config_dir: Path) -> None:
    _seed_sessions(7)

    text = build_stale_breadcrumbs(_CLIENT, _make_tick_summary(**_tick_kwargs()), _NOW)

    assert text.count(" (T-") == 5
    assert text.endswith(" (+2 more)")


def test_a_session_pinned_by_a_blocked_row_is_not_named(
    tmp_config_dir: Path,
) -> None:
    _seed_sessions(1)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-0",
                    client=_CLIENT,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id="s-0",
                )
            ]
        )
    )

    text = build_stale_breadcrumbs(_CLIENT, _make_tick_summary(**_tick_kwargs()), _NOW)

    assert text.endswith("orphan_sessions=none")


@pytest.mark.parametrize(
    "error",
    [OSError("disk gone"), ValueError("bad json"), CwError("unreadable state")],
    ids=["OSError", "ValueError", "CwError"],
)
def test_unreadable_state_degrades_to_the_old_text_with_one_warning(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def _raise() -> CwState:
        raise error

    monkeypatch.setattr("cw.dispatch.stale_breadcrumbs.load_state", _raise)
    caplog.set_level(logging.WARNING, logger="cw.dispatch.stale_breadcrumbs")

    text = build_stale_breadcrumbs(_CLIENT, _make_tick_summary(**_tick_kwargs()), _NOW)

    assert text == _OLD_PREFIX
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"stale_breadcrumbs_degraded: client={_CLIENT}"
        f" error={type(error).__name__}: {error}"
    )


def test_events_doc_describes_the_new_breadcrumb_keys() -> None:
    bullet = events_doc_bullet('- `"dispatch_loop_stale"`')

    for key in ("skip=", "occupied=", "running=", "orphan_sessions="):
        assert key in bullet


def _tick_kwargs() -> dict[str, object]:
    return {"pending": 2, "tick_at": _NOW - timedelta(seconds=200)}
