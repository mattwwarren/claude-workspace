"""Tests for ``attention_watch.py``, the orchestrator's wake-on-event watch (#2250).

``.claude/skills/orchestrate-sprint/scripts/attention_watch.py`` replaces the
old Monitor-armed ``attention_monitor.sh``. It follows ``cw event tail`` for
one client, prints the first attention-worthy event plus whatever lands in a
short burst window, then exits -- so a backgrounded ``Bash`` completion is the
wake signal. A per-client resume stamp makes each re-arm lossless.

Two tiers, mirroring the script's own split:

- **Unit tier** loads the script by path (same pattern as
  ``tests/test_preflight.py``) and calls its pure pieces directly: stamp I/O
  (always with an explicit ``stamp_path``), resume dedup, burst draining, and
  rendering. The liveness-bucket and breadcrumb scenarios are ported forward
  from the retired ``test_attention_monitor_sh.py`` (#2004, #1597).
- **Subprocess tier** runs the script for real against the ``_stub_cw`` fake
  from ``tests/conftest.py``, with ``HOME`` redirected into ``tmp_path`` so no
  test can touch the operator's real ``~/.claude-workspace/``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from cw.dispatch import BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
from cw.dispatch.regress_repeat import _FINALIZE_REGRESS_REPEAT_REASON
from cw.models import LivenessBucket, OrchestratorEventType
from tests.conftest import _stub_cw

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "orchestrate-sprint"
    / "scripts"
    / "attention_watch.py"
)

_SUBPROCESS_TIMEOUT_S = 30
# Seeded stamp + event timestamps for the subprocess tier: fixed and in the
# past, so a test never depends on when the script happens to start.
_SEED_STAMP = "2026-01-01T00:00:00Z"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("attention_watch_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules at class creation.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def aw() -> ModuleType:
    return _load()


def _event(
    event_id: str = "ev1",
    created_at: str = "2026-01-01T00:00:05.500000Z",
    event_type: str = "session.needs_attention",
    **payload: object,
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": event_type,
        "created_at": created_at,
        "payload": {"ticket_id": "T-1", "paused_status": "blocked", **payload},
    }


def _liveness_event(
    session_id: str = "sess-abcdef1234567890",
    ticket_id: str = "T-123",
    stage: str = "impl",
    old_bucket: str = "stale_15m",
    new_bucket: str = "stale_30m",
    stale_minutes: float = 32.4,
) -> dict[str, object]:
    return {
        "id": f"lv-{session_id}-{new_bucket}",
        "type": "session.liveness_changed",
        "created_at": "2026-01-01T00:00:05.000000Z",
        "payload": {
            "session_id": session_id,
            "ticket_id": ticket_id,
            "client": "claude-workspace",
            "stage": stage,
            "old_bucket": old_bucket,
            "new_bucket": new_bucket,
            "stale_minutes": stale_minutes,
        },
    }


def _render(aw: ModuleType, events: list[dict[str, object]]) -> list[str]:
    latch: dict[str, str] = {}
    rendered = [aw.fmt(e, latch) for e in events]
    return [line for line in rendered if line is not None]


def _filled_queue(
    events: list[dict[str, object]], *, upstream_exit: bool = False
) -> queue.Queue[str | None]:
    q: queue.Queue[str | None] = queue.Queue()
    for e in events:
        q.put(json.dumps(e) + "\n")
    if upstream_exit:
        q.put(None)
    return q


# ---------------------------------------------------------------------------
# Unit tier: import + constants
# ---------------------------------------------------------------------------


def test_module_imports_cleanly(aw: ModuleType) -> None:
    assert callable(aw.main)


def test_subscribed_types_are_valid_event_types(aw: ModuleType) -> None:
    valid = {t.value for t in OrchestratorEventType}
    assert set(aw.TYPES) <= valid
    assert "session.liveness_changed" in aw.TYPES


def test_blocker_reason_allowlist_matches_routing_constant(aw: ModuleType) -> None:
    """The script cannot import cw, so its hand-copy is pinned here (#1597)."""
    assert aw.BLOCKER_REASON_PAUSED_STATUSES == BREADCRUMB_ELIGIBLE_PAUSED_STATUSES


def test_surfaced_liveness_buckets_are_canonical_bucket_values(aw: ModuleType) -> None:
    """Hand-copied bucket names are pinned to LivenessBucket (#2004, #2250)."""
    assert (
        frozenset({LivenessBucket.STALE_30M.value, LivenessBucket.STALE_45M.value})
        == aw.SURFACED_LIVENESS
    )


def test_finalize_regress_status_matches_canonical_reason(aw: ModuleType) -> None:
    """Hand-copy of the #1717 finalize_regress_repeat reason, pinned (#2250)."""
    assert aw.FINALIZE_REGRESS_REPEAT_PAUSED_STATUS == _FINALIZE_REGRESS_REPEAT_REASON


# ---------------------------------------------------------------------------
# Unit tier: stamp round-trip
# ---------------------------------------------------------------------------


def test_load_stamp_missing_file_defaults_to_now(
    aw: ModuleType, tmp_path: Path
) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    created_at, ids = aw.load_stamp(stamp_path=tmp_path / "missing.json")
    after = datetime.now(UTC)
    parsed = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before <= parsed <= after
    assert ids == set()


def test_load_stamp_corrupt_json_defaults_to_now(
    aw: ModuleType, tmp_path: Path
) -> None:
    stamp = tmp_path / "corrupt.json"
    stamp.write_text("{not json")
    created_at, ids = aw.load_stamp(stamp_path=stamp)
    assert created_at.endswith("Z")
    assert len(created_at) == len("2026-01-01T00:00:00Z")
    assert ids == set()


def test_save_stamp_then_load_round_trips(aw: ModuleType, tmp_path: Path) -> None:
    stamp = tmp_path / "stamp.json"
    aw.save_stamp(stamp_path=stamp, created_at="2026-09-22T01:12:30Z", ids={"b", "a"})
    assert aw.load_stamp(stamp_path=stamp) == ("2026-09-22T01:12:30Z", {"a", "b"})
    assert json.loads(stamp.read_text()) == {
        "created_at": "2026-09-22T01:12:30Z",
        "ids": ["a", "b"],
    }


def test_save_stamp_creates_parent_dir(aw: ModuleType, tmp_path: Path) -> None:
    stamp = tmp_path / "nested" / "dir" / "stamp.json"
    aw.save_stamp(stamp_path=stamp, created_at="2026-09-22T01:12:30Z", ids=set())
    assert stamp.is_file()


def test_save_stamp_failed_replace_keeps_previous_stamp(
    aw: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that dies before the rename leaves the old stamp readable, so a
    re-arm resumes from it instead of falling back to "now" (#2250)."""
    stamp = tmp_path / "stamp.json"
    aw.save_stamp(stamp_path=stamp, created_at="2026-09-22T01:12:30Z", ids={"a"})

    msg = "disk full"

    def _boom(*_args: object) -> None:
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        aw.save_stamp(stamp_path=stamp, created_at="2026-09-22T09:00:00Z", ids={"b"})

    assert aw.load_stamp(stamp_path=stamp) == ("2026-09-22T01:12:30Z", {"a"})


def test_default_stamp_path_uses_client_name(aw: ModuleType) -> None:
    base = Path.home() / ".claude-workspace"
    assert aw.default_stamp_path("acme") == base / "attention-stamp-acme.json"
    assert (
        aw.default_stamp_path("acme", "debt") == base / "attention-stamp-acme-debt.json"
    )


# ---------------------------------------------------------------------------
# Unit tier: resume dedup (the literal #2250 bug)
# ---------------------------------------------------------------------------


def test_same_second_microsecond_event_not_dropped_by_naive_string_compare(
    aw: ModuleType,
) -> None:
    since = "2026-09-22T01:12:30Z"
    created = "2026-09-22T01:12:30.955Z"
    # The pitfall: a naive full-string compare says the event predates since.
    assert created < since
    state = aw.ResumeState(since, set())
    assert state.accept(_event("e1", created)) is True


def test_event_before_since_second_skipped(aw: ModuleType) -> None:
    state = aw.ResumeState("2026-09-22T01:12:30Z", set())
    assert state.accept(_event("e0", "2026-09-22T01:12:29.999Z")) is False


def test_dedup_by_second_and_id(aw: ModuleType) -> None:
    second = "2026-09-22T01:12:30"
    state = aw.ResumeState(second + "Z", {"id1"})
    assert state.accept(_event("id1", second + ".100Z")) is False
    assert state.accept(_event("id2", second + ".200Z")) is True
    assert state.last_created == second + "Z"
    assert state.sec_ids == {"id1", "id2"}


def test_resume_advances_past_delivered_second(aw: ModuleType, tmp_path: Path) -> None:
    stamp = tmp_path / "stamp.json"
    first = aw.ResumeState("2026-09-22T01:12:00Z", set())
    assert first.accept(_event("e1", "2026-09-22T01:12:30.100Z")) is True
    aw.save_stamp(stamp_path=stamp, created_at=first.last_created, ids=first.sec_ids)

    since, seen = aw.load_stamp(stamp_path=stamp)
    assert (since, seen) == ("2026-09-22T01:12:30Z", {"e1"})
    second = aw.ResumeState(since, seen)
    assert second.accept(_event("e1", "2026-09-22T01:12:30.100Z")) is False
    assert second.accept(_event("e2", "2026-09-22T01:12:31.050Z")) is True
    assert second.last_created == "2026-09-22T01:12:31Z"
    assert second.sec_ids == {"e2"}


# ---------------------------------------------------------------------------
# Unit tier: burst-window coalescing and wake reasons
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_burst(aw: ModuleType, monkeypatch: pytest.MonkeyPatch) -> float:
    burst = 0.05
    monkeypatch.setattr(aw, "BURST_S", burst)
    return burst


@pytest.mark.usefixtures("fast_burst")
def test_single_event_no_burst_exits_promptly(aw: ModuleType) -> None:
    state = aw.ResumeState(_SEED_STAMP, set())
    started = time.monotonic()
    wake = aw.drain(_filled_queue([_event()]), state, max_idle_s=60.0)
    assert time.monotonic() - started < 2.0
    assert len(wake.lines) == 1
    assert wake.lines[0].startswith("ATTENTION | 2026-01-01T00:00:05 |")
    assert not wake.upstream_exited
    assert not wake.idle_backstop


@pytest.mark.usefixtures("fast_burst")
def test_cluster_within_burst_window_coalesces_to_one_wake(aw: ModuleType) -> None:
    events = [_event(f"e{i}", f"2026-01-01T00:00:0{i}.1Z") for i in range(1, 4)]
    state = aw.ResumeState(_SEED_STAMP, set())
    wake = aw.drain(_filled_queue(events), state, max_idle_s=60.0)
    assert len(wake.lines) == 3
    assert state.last_created == "2026-01-01T00:00:03Z"
    assert state.sec_ids == {"e3"}


def test_event_after_burst_window_not_included(
    aw: ModuleType, fast_burst: float
) -> None:
    q = _filled_queue([_event("early", "2026-01-01T00:00:01.1Z")])
    late = json.dumps(_event("late", "2026-01-01T00:00:02.1Z")) + "\n"
    timer = threading.Timer(fast_burst * 20, q.put, args=(late,))
    timer.start()
    try:
        state = aw.ResumeState(_SEED_STAMP, set())
        wake = aw.drain(q, state, max_idle_s=60.0)
    finally:
        timer.join()
    assert len(wake.lines) == 1
    assert "late" not in state.seen
    # Unconsumed, so the stamp stays behind it and the next arm replays it.
    assert state.last_created == "2026-01-01T00:00:01Z"


def test_steady_stream_still_ends_burst_on_time(
    aw: ModuleType, fast_burst: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue that never goes empty must not keep the burst open past its
    deadline (#2250 review). A fake clock that advances on every read makes
    the deadline pass mid-backlog, deterministically."""
    ticks = iter(float(i) * fast_burst for i in range(10_000))
    monkeypatch.setattr(aw, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    backlog = 100
    events = [_event(f"s{i}", "2026-01-01T00:00:05.1Z") for i in range(backlog)]
    state = aw.ResumeState(_SEED_STAMP, set())
    wake = aw.drain(_filled_queue(events), state, max_idle_s=60.0)
    assert 0 < len(wake.lines) < backlog


@pytest.mark.usefixtures("fast_burst")
def test_upstream_exit_ends_drain(aw: ModuleType) -> None:
    state = aw.ResumeState(_SEED_STAMP, set())
    wake = aw.drain(_filled_queue([], upstream_exit=True), state, max_idle_s=60.0)
    assert wake.upstream_exited
    assert wake.lines == []


@pytest.mark.usefixtures("fast_burst")
def test_idle_backstop_fires_with_no_events(aw: ModuleType) -> None:
    state = aw.ResumeState(_SEED_STAMP, set())
    wake = aw.drain(_filled_queue([]), state, max_idle_s=0.05)
    assert wake.idle_backstop
    assert wake.lines == ["WATCHER | idle backstop after 0.05s, no events, re-arm"]


@pytest.mark.usefixtures("fast_burst")
def test_suppressed_event_advances_stamp_without_waking(aw: ModuleType) -> None:
    quiet = _liveness_event(new_bucket="live")
    state = aw.ResumeState(_SEED_STAMP, set())
    wake = aw.drain(_filled_queue([quiet]), state, max_idle_s=0.05)
    assert wake.idle_backstop
    assert state.last_created == "2026-01-01T00:00:05Z"
    assert state.sec_ids == {quiet["id"]}


@pytest.mark.usefixtures("fast_burst")
def test_malformed_line_skipped(aw: ModuleType) -> None:
    q: queue.Queue[str | None] = queue.Queue()
    q.put("not json\n")
    q.put(json.dumps(_event()) + "\n")
    wake = aw.drain(q, aw.ResumeState(_SEED_STAMP, set()), max_idle_s=60.0)
    assert len(wake.lines) == 1


# ---------------------------------------------------------------------------
# Unit tier: rendering / filtering (ported from test_attention_monitor_sh.py)
# ---------------------------------------------------------------------------


def test_liveness_live_and_stale_15m_suppressed(aw: ModuleType) -> None:
    assert _render(aw, [_liveness_event(new_bucket="live")]) == []
    assert _render(aw, [_liveness_event(new_bucket="stale_15m")]) == []


def test_liveness_stale_30m_and_45m_surfaced(aw: ModuleType) -> None:
    out30 = _render(aw, [_liveness_event(new_bucket="stale_30m", stale_minutes=32.4)])
    out45 = _render(
        aw,
        [
            _liveness_event(
                session_id="sess-other000000000",
                new_bucket="stale_45m",
                stale_minutes=47.1,
            )
        ],
    )
    assert len(out30) == 1
    assert "#T-123" in out30[0]
    assert "stage=impl" in out30[0]
    assert "stale_30m" in out30[0]
    assert "stale_m=32.4" in out30[0]
    assert len(out45) == 1
    assert "stale_45m" in out45[0]


def test_liveness_per_stage_duration_not_implied(aw: ModuleType) -> None:
    out = _render(aw, [_liveness_event(new_bucket="stale_30m", stale_minutes=34.9)])
    assert len(out) == 1
    assert "34.9" in out[0]
    assert "35" not in out[0].replace("34.9", "")


def test_liveness_repeat_same_bucket_suppressed(aw: ModuleType) -> None:
    sid = "sess-same0000000000"
    out = _render(
        aw,
        [
            _liveness_event(session_id=sid, new_bucket="stale_30m"),
            _liveness_event(session_id=sid, new_bucket="stale_30m"),
        ],
    )
    assert len(out) == 1


def test_liveness_escalation_always_surfaced(aw: ModuleType) -> None:
    sid = "sess-esc00000000000"
    out = _render(
        aw,
        [
            _liveness_event(session_id=sid, new_bucket="stale_30m"),
            _liveness_event(session_id=sid, new_bucket="stale_45m"),
        ],
    )
    assert len(out) == 2
    assert "stale_30m" in out[0]
    assert "stale_45m" in out[1]


def test_liveness_recovery_then_restall_same_bucket_both_surfaced(
    aw: ModuleType,
) -> None:
    sid = "sess-recover0000000"
    out = _render(
        aw,
        [
            _liveness_event(session_id=sid, new_bucket="stale_30m"),
            _liveness_event(session_id=sid, new_bucket="live"),
            _liveness_event(session_id=sid, new_bucket="stale_30m"),
        ],
    )
    assert len(out) == 2


def test_liveness_distinct_sessions_not_cross_suppressed(aw: ModuleType) -> None:
    out = _render(
        aw,
        [
            _liveness_event(session_id="sess-aaaa00000000000"),
            _liveness_event(session_id="sess-bbbb00000000000"),
        ],
    )
    assert len(out) == 2


def test_liveness_malformed_bucket_value_not_crashing(aw: ModuleType) -> None:
    missing: dict[str, object] = {
        "type": "session.liveness_changed",
        "payload": {"session_id": "sess-x", "ticket_id": "T-1"},
    }
    assert _render(aw, [missing]) == []
    assert _render(aw, [_liveness_event(new_bucket="stale_99m")]) == []


def test_needs_attention_rendering_includes_ticket_and_reason(aw: ModuleType) -> None:
    event = _event(
        ticket_id="T-999",
        paused_status="blocked",
        breadcrumbs="some breadcrumb text",
        stage="review",
        attempts=3,
        lane="debt",
        session_id="sess-needsattn00000",
    )
    out = _render(aw, [event])
    assert len(out) == 1
    line = out[0]
    assert line.startswith(
        "ATTENTION | 2026-01-01T00:00:05 | session.needs_attention | #T-999 | blocked |"
    )
    assert "stage=review" in line
    assert "att=3" in line
    assert "lane=debt" in line
    assert "reason=some breadcrumb text" in line
    assert line.endswith("| sess-nee")


def test_breadcrumbs_hidden_for_non_allowlisted_paused_status(aw: ModuleType) -> None:
    event = _event(paused_status="finalize_hold", breadcrumbs="/some/worktree/path")
    out = _render(aw, [event])
    assert len(out) == 1
    assert "reason=" not in out[0]


def test_finalize_regress_repeat_breadcrumbs_surfaced(aw: ModuleType) -> None:
    event = _event(paused_status="finalize_regress_repeat", breadcrumbs="attempts=3")
    assert "reason=attempts=3" in _render(aw, [event])[0]


def test_unknown_type_passthrough(aw: ModuleType) -> None:
    event: dict[str, object] = {
        "type": "operator.escalation",
        "payload": {"ticket_id": "T-77", "reason": "escalated"},
    }
    out = _render(aw, [event])
    assert len(out) == 1
    assert "#T-77" in out[0]
    assert "escalated" in out[0]


# ---------------------------------------------------------------------------
# Subprocess tier: real process against the _stub_cw fake
# ---------------------------------------------------------------------------


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _run_watch(
    tmp_path: Path,
    *args: str,
    cw_bin: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(_home(tmp_path))}
    env.pop("CW_BIN", None)
    env.update(extra_env or {})
    argv = [sys.executable, str(_SCRIPT), *args]
    if cw_bin is not None:
        argv += ["--cw-bin", str(cw_bin)]
    return subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _seed_stamp(
    path: Path, created_at: str = _SEED_STAMP, ids: tuple[str, ...] = ()
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created_at": created_at, "ids": list(ids)}))
    return path


def _invoked_args(bin_dir: Path) -> list[str]:
    return (bin_dir / "cw.args").read_text().splitlines()


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _assert_process_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_watcher_line_on_upstream_exit(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path, exit_code=3)
    stamp = _seed_stamp(tmp_path / "stamp.json")
    result = _run_watch(
        tmp_path, "acme", "--stamp-path", str(stamp), cw_bin=bin_dir / "cw"
    )
    assert result.returncode == 0, result.stderr
    assert "WATCHER | cw event tail exited rc=3" in result.stdout.splitlines()


def test_terminates_upstream_process_on_exit(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path, events=[json.dumps(_event())], block=True)
    stamp = _seed_stamp(tmp_path / "stamp.json")
    result = _run_watch(
        tmp_path, "acme", "--stamp-path", str(stamp), cw_bin=bin_dir / "cw"
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("ATTENTION | 2026-01-01T00:00:05 |")
    _assert_process_gone(int((bin_dir / "cw.pid").read_text()))
    assert json.loads(stamp.read_text()) == {
        "created_at": "2026-01-01T00:00:05Z",
        "ids": ["ev1"],
    }


def test_idle_backstop_exits_and_reports_after_max_idle_seconds(
    tmp_path: Path,
) -> None:
    bin_dir = _stub_cw(tmp_path, block=True)
    stamp = tmp_path / "stamp.json"
    result = _run_watch(
        tmp_path,
        "acme",
        "--stamp-path",
        str(stamp),
        "--max-idle-seconds",
        "0.2",
        cw_bin=bin_dir / "cw",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "WATCHER | idle backstop after 0.2s, no events, re-arm"
    ]
    _assert_process_gone(int((bin_dir / "cw.pid").read_text()))
    # Same lossless contract as an event wake: the stamp is written, so the
    # next arm resumes from here rather than from a fresh "now".
    saved = json.loads(stamp.read_text())
    assert saved["created_at"] == _flag_value(_invoked_args(bin_dir), "--since")


def test_cli_resumes_from_stamp(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    stamp = _seed_stamp(tmp_path / "stamp.json", "2026-03-04T05:06:07Z", ("a",))
    _run_watch(tmp_path, "acme", "--stamp-path", str(stamp), cw_bin=bin_dir / "cw")
    args = _invoked_args(bin_dir)
    assert _flag_value(args, "--since") == "2026-03-04T05:06:07Z"
    assert _flag_value(args, "--client") == "acme"
    for flag in ("event", "tail", "--follow", "--dedup-terminal", "--json"):
        assert flag in args
    subscribed = {args[i + 1] for i, a in enumerate(args) if a == "--type"}
    assert "session.needs_attention" in subscribed
    assert "session.liveness_changed" in subscribed
    assert "--lane" not in args


def test_cli_default_client_is_claude_workspace(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    result = _run_watch(tmp_path, cw_bin=bin_dir / "cw")
    assert result.returncode == 0, result.stderr
    assert _flag_value(_invoked_args(bin_dir), "--client") == "claude-workspace"
    default_stamp = (
        _home(tmp_path) / ".claude-workspace" / "attention-stamp-claude-workspace.json"
    )
    assert default_stamp.is_file()


def test_cli_since_override_bypasses_stamp(tmp_path: Path) -> None:
    replayed = _event("x", "2026-02-01T00:00:00.5Z")
    bin_dir = _stub_cw(tmp_path, events=[json.dumps(replayed)])
    stamp = _seed_stamp(tmp_path / "stamp.json", "2026-06-01T00:00:00Z", ("x",))
    result = _run_watch(
        tmp_path,
        "acme",
        "--stamp-path",
        str(stamp),
        "--since",
        "2026-01-01T00:00:00Z",
        cw_bin=bin_dir / "cw",
    )
    assert result.returncode == 0, result.stderr
    assert _flag_value(_invoked_args(bin_dir), "--since") == "2026-01-01T00:00:00Z"
    assert result.stdout.splitlines()[0].startswith("ATTENTION | 2026-02-01T00:00:00 |")


def test_cli_lane_scopes_event_tail_invocation(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    result = _run_watch(tmp_path, "acme", "debt", cw_bin=bin_dir / "cw")
    assert result.returncode == 0, result.stderr
    assert _flag_value(_invoked_args(bin_dir), "--lane") == "debt"
    lane_stamp = (
        _home(tmp_path) / ".claude-workspace" / "attention-stamp-acme-debt.json"
    )
    assert lane_stamp.is_file()


def test_cli_stamp_path_override_writes_to_given_path(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    custom = tmp_path / "custom-stamp.json"
    result = _run_watch(
        tmp_path, "acme", "--stamp-path", str(custom), cw_bin=bin_dir / "cw"
    )
    assert result.returncode == 0, result.stderr
    assert custom.is_file()
    assert not (_home(tmp_path) / ".claude-workspace").exists()


def test_cli_cw_bin_override_invokes_given_executable(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    stamp = tmp_path / "stamp.json"
    result = _run_watch(
        tmp_path, "acme", "--stamp-path", str(stamp), cw_bin=bin_dir / "cw"
    )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "cw.args").is_file()


def test_cli_cw_bin_env_var_invokes_given_executable(tmp_path: Path) -> None:
    bin_dir = _stub_cw(tmp_path)
    stamp = tmp_path / "stamp.json"
    result = _run_watch(
        tmp_path,
        "acme",
        "--stamp-path",
        str(stamp),
        extra_env={"CW_BIN": str(bin_dir / "cw")},
    )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "cw.args").is_file()
