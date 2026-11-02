"""Unit tests for cw.reconcile.main_drift.

Main-checkout drift sweep: dirty/ahead/diverged detection, the edge-triggered
latch (one event per drift episode, not per tick), client-badge routing, and
the ``_detect_main_drift_candidates`` guard branches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from cw.board import _index_client_badge_events
from cw.config import (
    save_state,
)
from cw.dispatch_state import load_main_drift_latches, save_main_drift_latches
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
    main_drift,
    reconcile,
)
from cw.reconcile.cancelled_row_probes import new_cancelled_row_probes
from cw.reconcile.main_drift import (
    MAIN_DRIFT_PROBE_MAX_AGE_SECONDS,
    MainDriftProbes,
    _classify_main_drift,
    _ClientDriftStatus,
    capture_main_drift_probes,
    new_main_drift_probes,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import (
    _auto_config,
    _mk_daemon_session_with_worktree,
    probe_sessions_lock_free,
)
from tests.conftest import commit_tracked_file, git_in


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


def _capture_then_detect(
    state: CwState, clients: dict[str, ClientConfig]
) -> list[_ClientDriftStatus]:
    """Run the lockless capture, then the in-lock detect over its store (#2546)."""
    probes = new_main_drift_probes()
    capture_main_drift_probes(state, clients, probes=probes)
    return _detect_main_drift_candidates(state, clients, probes)


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
    assert _capture_then_detect(CwState(sessions=[sess]), clients) == []


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
    assert _capture_then_detect(CwState(sessions=[sess]), clients) == []


def test_detect_main_drift_skips_unknown_client() -> None:
    """A session whose client is absent from the clients dict is skipped."""
    sess = _mk_daemon_session_with_worktree(
        "orphan", SessionStatus.ACTIVE, Path("/tmp/wt-orphan")
    )
    assert _capture_then_detect(CwState(sessions=[sess]), {}) == []


@pytest.mark.parametrize(
    ("target", "exc"),
    [
        ("is_main_checkout_dirty", WorktreeError("git blew up")),
        ("check_main_ff_safety", OSError("git missing")),
    ],
)
def test_detect_main_drift_swallows_git_error(
    monkeypatch: pytest.MonkeyPatch, target: str, exc: Exception
) -> None:
    """A git error during capture is stored as no-drift (fail-safe).

    The checked client still produces one _ClientDriftStatus entry with
    drift_kind=None (not an empty list) — a client that errored this tick is
    indistinguishable from a genuinely clean one, and the act phase needs
    that clean entry to be able to reset a stale latch (#1258).
    """

    def _boom(_c: object) -> object:
        raise exc

    monkeypatch.setattr("cw.reconcile.main_drift.is_main_checkout_dirty", _clean)
    monkeypatch.setattr(f"cw.reconcile.main_drift.{target}", _boom)
    sess = _mk_daemon_session_with_worktree(
        "err", SessionStatus.ACTIVE, Path("/tmp/wt-err")
    )
    probes = new_main_drift_probes()
    capture_main_drift_probes(CwState(sessions=[sess]), _CLIENTS, probes=probes)

    assert probes.lookup("client-a") is None
    result = _detect_main_drift_candidates(CwState(sessions=[sess]), _CLIENTS, probes)
    assert len(result) == 1
    assert result[0].drift_kind is None


def _clean(_c: object) -> bool:
    return False


_CLIENTS = {"client-a": ClientConfig(name="client-a", workspace_path=Path("/tmp/ws"))}


def _live(sid: str = "s1", wt: str = "/tmp/wt-a") -> Session:
    return _mk_daemon_session_with_worktree(sid, SessionStatus.ACTIVE, Path(wt))


def _stub_store(payload: str | None) -> MainDriftProbes:
    probes = new_main_drift_probes()
    probes.capture("client-a", read=lambda _at: payload)
    return probes


@pytest.mark.parametrize(
    ("dirty", "ff_safety", "expected"),
    [
        (True, "equal", "dirty"),
        (False, "ahead", "ahead of origin"),
        (False, "diverged", "diverged from origin"),
        (False, "equal", None),
        (False, "detached", None),
        ("error", "equal", None),
    ],
    ids=["dirty", "ahead", "diverged", "equal", "detached", "error"],
)
def test_captured_value_matches_classify(
    monkeypatch: pytest.MonkeyPatch,
    dirty: bool | str,
    ff_safety: str,
    expected: str | None,
) -> None:
    """Parity matrix: the stored payload is exactly ``_classify_main_drift``."""

    def _dirty(_c: object) -> bool:
        if dirty == "error":
            msg = "git error"
            raise WorktreeError(msg)
        return bool(dirty)

    monkeypatch.setattr("cw.reconcile.main_drift.is_main_checkout_dirty", _dirty)
    monkeypatch.setattr(
        "cw.reconcile.main_drift.check_main_ff_safety", lambda _c: ff_safety
    )
    probes = new_main_drift_probes()

    capture_main_drift_probes(CwState(sessions=[_live()]), _CLIENTS, probes=probes)

    assert probes.lookup("client-a") == expected
    assert _classify_main_drift(_CLIENTS["client-a"]) == expected


def _setup_real_main_checkout(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[bool]]:
    """A real main checkout on ``main`` == origin/main, a live DAEMON session
    elsewhere, and a lock-state spy on the dirty check."""
    repo = make_git_repo("main-co")
    commit_tracked_file(repo, "a.py")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write_clients_yaml(ClientSpec("client-a", repo, default_branch="main"))
    save_state(CwState(sessions=[_mk_live_drift_session("real", tmp_path / "wt")]))
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live0001"}],
    )
    real_dirty = main_drift.is_main_checkout_dirty
    lock_free: list[bool] = []

    def _spy(client: ClientConfig) -> bool:
        lock_free.append(probe_sessions_lock_free())
        return real_dirty(client)

    monkeypatch.setattr("cw.reconcile.main_drift.is_main_checkout_dirty", _spy)
    return repo, lock_free


def test_real_git_clean_checkout_emits_nothing(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real git, full reconcile(): a clean main checkout emits no event and the
    git ran before sessions_lock (the harness fails any in-lock git)."""
    _repo, lock_free = _setup_real_main_checkout(tmp_path, make_git_repo, monkeypatch)

    reconcile()

    assert lock_free == [True]
    assert _read_drift_events("t-2546-real-clean") == []


def test_real_git_dirty_checkout_emits_one_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same code path as the clean case; only the checkout state differs."""
    repo, lock_free = _setup_real_main_checkout(tmp_path, make_git_repo, monkeypatch)
    (repo / "a.py").write_text("changed", encoding="utf-8")

    reconcile()

    assert lock_free == [True]
    events = _read_drift_events("t-2546-real-dirty")
    assert len(events) == 1
    payload = events[0].payload
    assert payload == {
        "session_id": "",
        "session_name": "",
        "client": "client-a",
        "ticket_id": None,
        "claude_session_id": None,
        "paused_status": _MAIN_CHECKOUT_DRIFT_REASON,
        "breadcrumbs": (
            f"main checkout {repo} is dirty (worktree at {tmp_path / 'wt'})"
        ),
        "crashed": False,
    }


@pytest.mark.parametrize("kind", ["none", "backgrounded", "user"])
def test_detect_without_live_daemon_session_yields_nothing(kind: str) -> None:
    """A captured drift for a client with no live DAEMON session at detect
    time yields no candidate."""
    sessions: list[Session] = []
    if kind == "backgrounded":
        sessions = [
            _mk_daemon_session_with_worktree(
                "bg", SessionStatus.BACKGROUNDED, Path("/tmp/wt")
            )
        ]
    elif kind == "user":
        user = _live()
        user.origin = SessionOrigin.USER
        sessions = [user]

    result = _detect_main_drift_candidates(
        CwState(sessions=sessions), _CLIENTS, _stub_store("dirty")
    )

    assert result == []


def test_reconcile_with_captured_drift_but_no_live_session_emits_nothing(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=True, ff_safety="equal")
    save_state(CwState(sessions=[]))
    store = _stub_store("dirty")
    monkeypatch.setattr(
        "cw.reconcile.core.capture_drift_and_cancelled_probes",
        lambda **_kw: (store, new_cancelled_row_probes()),
    )

    reconcile()

    assert _read_drift_events("t-2546-no-live") == []


def test_latched_client_without_live_session_keeps_its_latch(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="equal")
    save_main_drift_latches({"client-a": True})
    save_state(CwState(sessions=[]))

    reconcile()

    assert load_main_drift_latches() == {"client-a": True}
    assert _read_drift_events("t-2546-latched") == []


def test_missing_capture_defers_without_touching_the_latch(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live client with no usable capture: no candidate, no event, latch kept."""
    wt = tmp_path / "wt-missing"
    save_state(CwState(sessions=[_mk_live_drift_session("drift-missing", wt)]))
    _prime_drift_reconcile(monkeypatch, tmp_path, dirty=False, ff_safety="equal")
    save_main_drift_latches({"client-a": True})
    monkeypatch.setattr(
        "cw.reconcile.core.capture_drift_and_cancelled_probes",
        lambda **_kw: (new_main_drift_probes(), new_cancelled_row_probes()),
    )

    with caplog.at_level(logging.WARNING, "cw.reconcile.main_drift"):
        reconcile()

    assert load_main_drift_latches() == {"client-a": True}
    assert _read_drift_events("t-2546-missing") == []
    assert "main_drift_probe_unavailable client=client-a reason=missing" in caplog.text


@pytest.mark.parametrize("store", ["none", "empty", "stale"])
def test_detect_with_no_usable_capture_yields_nothing(store: str) -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with freeze_time(now) as clock:
        probes: MainDriftProbes | None = None
        if store == "empty":
            probes = new_main_drift_probes()
        elif store == "stale":
            probes = _stub_store("dirty")
            clock.tick(MAIN_DRIFT_PROBE_MAX_AGE_SECONDS)

        result = _detect_main_drift_candidates(
            CwState(sessions=[_live()]), _CLIENTS, probes
        )

    assert result == []


def test_budget_exhausted_capture_stores_nothing_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def _classify(client: ClientConfig) -> str | None:
        calls.append(client.name)
        return "dirty"

    monkeypatch.setattr("cw.reconcile.main_drift._classify_main_drift", _classify)
    clients = {
        **_CLIENTS,
        "client-b": ClientConfig(name="client-b", workspace_path=Path("/tmp/wb")),
    }
    other = _live("s2", "/tmp/wt-b")
    other.client = "client-b"
    probes = new_main_drift_probes(budget_seconds=0.0)

    with caplog.at_level(logging.WARNING, "cw.reconcile.main_drift"):
        capture_main_drift_probes(
            CwState(sessions=[_live(), other]), clients, probes=probes
        )

    assert calls == []
    assert probes.keys == frozenset()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2 of 2" in warnings[0].getMessage()


def test_capture_and_detect_share_the_target_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-session-wins per client, DAEMON only, live only, worktree set,
    client configured: capture probes exactly the clients detect reports."""
    probed: list[str] = []

    def _classify(client: ClientConfig) -> str | None:
        probed.append(client.name)
        return None

    monkeypatch.setattr("cw.reconcile.main_drift._classify_main_drift", _classify)
    first = _live("first", "/tmp/wt-first")
    second = _live("second", "/tmp/wt-second")
    no_wt = _live("no-wt")
    no_wt.client = "client-c"
    no_wt.worktree_path = None
    unknown = _live("unknown")
    unknown.client = "client-unknown"
    user = _live("user")
    user.client = "client-d"
    user.origin = SessionOrigin.USER
    clients = {
        name: ClientConfig(name=name, workspace_path=Path(f"/tmp/{name}"))
        for name in ("client-a", "client-c", "client-d")
    }
    state = CwState(sessions=[no_wt, unknown, user, first, second])
    probes = new_main_drift_probes()

    capture_main_drift_probes(state, clients, probes=probes)
    result = _detect_main_drift_candidates(state, clients, probes)

    assert probed == ["client-a"]
    assert probes.keys == frozenset({"client-a"})
    assert [(c.client, c.sample_worktree_path) for c in result] == [
        ("client-a", Path("/tmp/wt-first"))
    ]
