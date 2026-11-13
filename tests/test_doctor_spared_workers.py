"""Tests for the ``spared-unrecorded-workers`` doctor advisory (#3257).

The ``run_doctor()`` registration case lives in ``tests/test_doctor.py``: it
needs that file's module-private ``_stub_claude_version_ok`` (the same split as
``tests/test_doctor_worker_tmpdir.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.doctor.spared_workers import _MAX_LISTED, _check_spared_unrecorded_workers
from cw.models import CwState, SessionStatus
from cw.models.client import ClientConfig
from cw.reconcile.leaked_workers import LEAKED_WORKER_GRACE_SECONDS
from tests._reconcile_helpers import ms_of
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import FakeNativeDaemonClient

_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_OLD_MS = ms_of(_NOW) - (LEAKED_WORKER_GRACE_SECONDS + 1) * 1000
_NAME = "spared-unrecorded-workers"


@pytest.fixture(autouse=True)
def daemon(
    monkeypatch: pytest.MonkeyPatch, mock_native_daemon: FakeNativeDaemonClient
) -> FakeNativeDaemonClient:
    """Install the fake daemon at the doctor module's binding."""
    monkeypatch.setattr(
        "cw.doctor.spared_workers.get_native_daemon_client",
        lambda: mock_native_daemon,
    )
    return mock_native_daemon


def _clients(tmp_path: Path) -> dict[str, ClientConfig]:
    return {
        "client-a": ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
    }


@freeze_time(_NOW)
def test_none_spared_is_ok_without_warn(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    daemon.seed_live_worker(tmp_path / "worktrees" / "dev-1", started_at_ms=_OLD_MS)

    result = _check_spared_unrecorded_workers(CwState(), _clients(tmp_path))

    assert result.name == _NAME
    assert result.ok is True
    assert result.warn is False


@freeze_time(_NOW)
def test_one_spared_warns_with_short_id_cwd_and_remedy(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    cwd = tmp_path / "elsewhere"
    sid = daemon.seed_live_worker(cwd, started_at_ms=_OLD_MS)

    result = _check_spared_unrecorded_workers(CwState(), _clients(tmp_path))

    assert result.ok is True
    assert result.warn is True
    assert sid in result.detail
    assert str(cwd) in result.detail
    assert f"claude stop {sid}" in result.detail
    assert "claude stop <short_id>" in result.detail
    assert "non-cw `claude --bg`" in result.detail


@freeze_time(_NOW)
def test_recorded_worker_is_not_reported(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    sid = daemon.seed_live_worker(tmp_path / "elsewhere", started_at_ms=_OLD_MS)
    state = CwState(
        sessions=[
            _make_daemon_session(id="s1", status=SessionStatus.ACTIVE, surface_ref=sid)
        ]
    )

    result = _check_spared_unrecorded_workers(state, _clients(tmp_path))

    assert result.warn is False


def test_roster_unreadable_is_ok_with_skipped_note(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    daemon.roster_unreadable = True

    result = _check_spared_unrecorded_workers(CwState(), _clients(tmp_path))

    assert result.ok is True
    assert result.warn is False
    assert result.detail.startswith("skipped:")


@freeze_time(_NOW)
def test_many_spared_are_bounded_with_overflow_count(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    extra = 3
    sids = [
        daemon.seed_live_worker(tmp_path / f"out-{i}", started_at_ms=_OLD_MS)
        for i in range(_MAX_LISTED + extra)
    ]

    result = _check_spared_unrecorded_workers(CwState(), _clients(tmp_path))

    assert result.warn is True
    assert result.detail.startswith(f"{len(sids)} unrecorded daemon worker(s)")
    assert f"+{extra} more" in result.detail
    listed = [s for s in sids if f"claude stop {s})" in result.detail]
    assert listed == sorted(sids)[:_MAX_LISTED]


@freeze_time(_NOW)
def test_unreadable_clients_degrade_lists_in_base_workers(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    """An empty client map (clients.yaml unreadable) spares every old worker."""
    sid = daemon.seed_live_worker(
        tmp_path / "worktrees" / "dev-1", started_at_ms=_OLD_MS
    )

    result = _check_spared_unrecorded_workers(CwState(), {})

    assert result.warn is True
    assert f"claude stop {sid}" in result.detail
