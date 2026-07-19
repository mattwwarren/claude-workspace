"""Unit tests for cw.reconcile.main_drift.

Main-checkout drift sweep: dirty/ahead/diverged detection, the edge-triggered
latch (one event per drift episode, not per tick), client-badge routing, and
the ``_detect_main_drift_candidates`` guard branches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cw.board import _index_client_badge_events
from cw.config import (
    save_state,
)
from cw.events import read_events
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile import (
    _MAIN_CHECKOUT_DRIFT_REASON,
    _detect_main_drift_candidates,
    reconcile,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_daemon_session_with_worktree,
)


def _mk_live_drift_session(sid: str, wt_path: Path) -> Session:
    """DAEMON+IMPL session with worktree set and a LIVE surface (survives reap)."""
    sess = _mk_daemon_session_with_worktree(sid, SessionStatus.ACTIVE, wt_path)
    sess.surface_ref = "live0001"
    return sess


def _prime_drift_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    dirty: bool,
    ff_safety: str,
) -> None:
    """Wire load_clients, the two git probes, config, and a live daemon roster."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    monkeypatch.setattr(
        "cw.reconcile.core.load_clients",
        lambda: {
            "client-a": ClientConfig(
                name="client-a", workspace_path=tmp_path / "main-checkout"
            )
        },
    )
    monkeypatch.setattr(
        "cw.reconcile.main_drift.is_main_checkout_dirty", lambda _c: dirty
    )
    monkeypatch.setattr(
        "cw.reconcile.main_drift.check_main_ff_safety", lambda _c: ff_safety
    )
    # Live roster: session surface_ref "live0001" stays ACTIVE (not phantom).
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live0001"}],
    )


def _read_drift_events(consumer: str) -> list[Any]:
    """SESSION_NEEDS_ATTENTION events carrying the main-checkout-drift reason.

    Filters on paused_status so an unrelated watchdog attention event (e.g. the
    idle sweep firing on the same long-lived session) is never miscounted.
    """
    return [
        e
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        if e.payload.get("paused_status") == _MAIN_CHECKOUT_DRIFT_REASON
    ]


def test_main_drift_dirty_main_emits_attention(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dirty main checkout + worktree elsewhere → 1 SESSION_NEEDS_ATTENTION."""
    wt = tmp_path / "wt-dirty"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-dirty", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")

    reconcile()

    events = _read_drift_events("test-drift-dirty")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["paused_status"] == _MAIN_CHECKOUT_DRIFT_REASON
    assert payload["client"] == "client-a"
    assert payload["crashed"] is False
    assert "dirty" in payload["breadcrumbs"]
    assert str(wt) in payload["breadcrumbs"]


def test_main_drift_ahead_emits_attention(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_main_ff_safety == 'ahead' → SESSION_NEEDS_ATTENTION with 'ahead'."""
    wt = tmp_path / "wt-ahead"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-ahead", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="ahead")

    reconcile()

    events = _read_drift_events("test-drift-ahead")
    assert len(events) == 1
    assert "ahead of origin" in events[0].payload["breadcrumbs"]


def test_main_drift_diverged_emits_attention(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_main_ff_safety == 'diverged' → SESSION_NEEDS_ATTENTION with 'diverged'."""
    wt = tmp_path / "wt-diverged"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-div", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="diverged")

    reconcile()

    events = _read_drift_events("test-drift-diverged")
    assert len(events) == 1
    assert "diverged from origin" in events[0].payload["breadcrumbs"]


def test_main_drift_clean_main_no_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean main (not dirty, ff='equal') → no drift event."""
    wt = tmp_path / "wt-clean"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-clean", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="equal")

    reconcile()

    assert _read_drift_events("test-drift-clean") == []


def test_main_drift_detached_ignored(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ff='detached' is the known adjacent bug (out of scope) → no drift event."""
    wt = tmp_path / "wt-detached"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-det", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="detached")

    reconcile()

    assert _read_drift_events("test-drift-detached") == []


def test_main_drift_fires_once_not_per_tick(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge-triggered latch: drift across two ticks fires once, not per-tick (#1258)."""
    wt = tmp_path / "wt-refire"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-refire", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")

    reconcile()
    reconcile()

    assert len(_read_drift_events("test-drift-refire")) == 1


def test_main_drift_clears_and_refires(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Latch resets silently on clean, re-arms on the next drift episode (#1258)."""
    wt = tmp_path / "wt-clear-refire"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-clear-refire", wt)]))

    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")
    reconcile()
    reconcile()
    assert len(_read_drift_events("test-drift-clear-refire")) == 1

    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="equal")
    reconcile()
    assert len(_read_drift_events("test-drift-clear-refire")) == 1

    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")
    reconcile()
    assert len(_read_drift_events("test-drift-clear-refire")) == 2


def test_main_drift_multi_session_one_event_per_client(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two live sessions on the same client, both dirty → exactly 1 event (#1258)."""
    wt_a = tmp_path / "wt-multi-a"
    wt_b = tmp_path / "wt-multi-b"
    save_state(
        CwState(
            sessions=[
                _mk_live_drift_session("drift-multi-a", wt_a),
                _mk_live_drift_session("drift-multi-b", wt_b),
            ]
        )
    )
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")

    reconcile()

    assert len(_read_drift_events("test-drift-multi")) == 1


def test_main_drift_event_routes_to_client_badge(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client-scoped drift event carries ticket_id=None + real client, routes
    through _index_client_badge_events (not the ticket-row badge path, #1258)."""
    wt = tmp_path / "wt-badge"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-badge", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")

    reconcile()

    events = _read_drift_events("test-drift-badge")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] is None
    assert payload["client"] == "client-a"

    badges = _index_client_badge_events(events, datetime.now(UTC), {"client-a"})
    assert "client-a" in badges


def test_main_drift_non_daemon_session_skipped(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USER-origin session is not a worktree worker → no drift sweep event."""
    wt = tmp_path / "wt-user"
    sess = _mk_live_drift_session("drift-user", wt)
    sess.origin = SessionOrigin.USER
    save_state(CwState(sessions=[sess]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")

    reconcile()

    assert _read_drift_events("test-drift-user") == []


def test_detect_main_drift_skips_worktree_none() -> None:
    """_detect_main_drift_candidates skips a session with worktree_path=None."""
    sess = _mk_daemon_session_with_worktree(
        "no-wt", SessionStatus.ACTIVE, Path("/tmp/x")
    )
    sess.worktree_path = None
    clients = {
        "client-a": ClientConfig(name="client-a", workspace_path=Path("/tmp/ws"))
    }
    assert _detect_main_drift_candidates(CwState(sessions=[sess]), clients) == []


def test_detect_main_drift_skips_backgrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_main_drift_candidates skips a non-live (BACKGROUNDED) session."""
    monkeypatch.setattr(
        "cw.reconcile.main_drift.is_main_checkout_dirty", lambda _c: True
    )
    sess = _mk_daemon_session_with_worktree(
        "bg", SessionStatus.BACKGROUNDED, Path("/tmp/wt-bg")
    )
    clients = {
        "client-a": ClientConfig(name="client-a", workspace_path=Path("/tmp/ws"))
    }
    assert _detect_main_drift_candidates(CwState(sessions=[sess]), clients) == []


def test_detect_main_drift_skips_unknown_client() -> None:
    """A session whose client is absent from the clients dict is skipped."""
    sess = _mk_daemon_session_with_worktree(
        "orphan", SessionStatus.ACTIVE, Path("/tmp/wt-orphan")
    )
    assert _detect_main_drift_candidates(CwState(sessions=[sess]), {}) == []


def test_detect_main_drift_swallows_git_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git error during classification is treated as no-drift (fail-safe).

    The checked client still produces one _ClientDriftStatus entry with
    drift_kind=None (not an empty list) — a client that errored this tick is
    indistinguishable from a genuinely clean one, and the act phase needs
    that clean entry to be able to reset a stale latch (#1258).
    """

    def _boom(_c: object) -> bool:
        msg = "git blew up"
        raise WorktreeError(msg)

    monkeypatch.setattr("cw.reconcile.main_drift.is_main_checkout_dirty", _boom)
    sess = _mk_daemon_session_with_worktree(
        "err", SessionStatus.ACTIVE, Path("/tmp/wt-err")
    )
    clients = {
        "client-a": ClientConfig(name="client-a", workspace_path=Path("/tmp/ws"))
    }
    result = _detect_main_drift_candidates(CwState(sessions=[sess]), clients)
    assert len(result) == 1
    assert result[0].drift_kind is None
