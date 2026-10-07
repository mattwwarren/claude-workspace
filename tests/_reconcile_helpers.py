"""Shared test helpers for the ``cw.reconcile`` per-submodule test suite.

Cross-category factories, payload builders, and transcript writers used by two
or more of the split ``test_reconcile_*.py`` files. This module has no
``test_`` prefix, so pytest does not collect it (same convention as
``tests/conftest.py``); it is imported explicitly by the test modules that use
each helper.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cw._lock_guard import LockRank, is_rank_held
from cw.config import load_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    ClientConfig,
    LastResultSource,
    OrchestratorConfig,
    OrchestratorEventType,
    PendingFixDispatch,
    QueueItemStatus,
    ReapPolicy,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared import _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.review_recipes import ReviewRecipeCandidate
from cw.reconcile.review_recipes._shared import RepoSlugs, _find_review_task
from cw.reconcile.review_recipes.address_review import (
    _act_address_review,
    _address_review_slug_dir,
)
from cw.reconcile.review_recipes.auto_fix_ci import (
    _act_auto_fix_ci,
    _auto_fix_ci_guard_target,
)
from tests.conftest import (
    _make_daemon_session,
    _write_idle_transcript,
    _write_stop_hook_transcript,
)

# Default daemon surface ref shared by the promoted doctor/reconcile helpers
# below -- the same "fake-short-id" the transcript writers key their filename
# prefix on, so ``_locate_session_transcript``'s surface_ref glob finds them.
_FAKE_SURFACE_REF = "fake-short-id"

# The act phases whose cross-repo guard reads pre-captured repo slugs (#2564);
# ``act_then_dispatch`` captures a ``repo_slugs`` for these when none is passed.
_SLUG_GUARDED_ACTS: tuple[Callable[..., object], ...] = (
    _act_address_review,
    _act_auto_fix_ci,
)


def probe_sessions_lock_free() -> bool:
    """Return True iff this thread does NOT hold ``sessions_lock`` right now.

    Reads the lock guard's per-thread held stack (ADR-0019). Probing by
    re-acquiring the lock would itself be a recorded re-entry.
    """
    return not is_rank_held(LockRank.SESSIONS)


class LockProbeDaemon(FakeNativeDaemonClient):
    """Fake daemon whose ``stop()`` records lock state and on-disk statuses.

    Each stop appends ``("free" | "held", {session_id: status})`` to
    :attr:`probes` -- whether ``sessions_lock`` was free at the call, and every
    session's persisted status at that moment -- then stops as the base fake
    does (#1232).
    """

    def __init__(self) -> None:
        super().__init__()
        self.probes: list[tuple[str, dict[str, SessionStatus]]] = []

    def stop(self, short_id: str) -> None:
        lock = "free" if probe_sessions_lock_free() else "held"
        statuses = {s.id: s.status for s in load_state().sessions}
        self.probes.append((lock, statuses))
        super().stop(short_id)


def call_and_drain[T](
    fn: Callable[..., T],
    *args: object,
    deferred: DeferredReconcileJobs | None = None,
    **kwargs: object,
) -> T:
    """Call an act-phase *fn* with a post-lock sink, then drain the sink.

    Stands in for ``reconcile()``'s own create-then-drain (#1232) for tests
    that drive an act phase directly. Pass *deferred* to own the sink (e.g. to
    assert on what was queued); otherwise a fresh one is built. Returns
    *fn*'s result.
    """
    sink = deferred if deferred is not None else DeferredReconcileJobs()
    result = fn(*args, deferred=sink, **kwargs)
    run_post_lock_jobs(sink)
    return result


def capture_repo_slugs(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
) -> RepoSlugs:
    """Capture, live, every git dir the review recipes' repo guard reads (#2564).

    Stands in for ``reconcile()``'s pre-lock ``capture_review_repo_slugs`` for
    tests that drive ``_act_address_review`` / ``_act_auto_fix_ci`` directly:
    reloads the saved queue, re-finds each candidate's row the way the act
    phase will (``_find_review_task``), and captures both its address_review
    worktree and its auto_fix_ci client workspace through the act phases' own
    guard-dir helpers. Runs git, so call it with no ``sessions_lock`` held.
    """
    store = load_dev_queue()
    slugs = RepoSlugs()
    for candidate in candidates:
        task = _find_review_task(store, candidate.ticket_id, candidate.client)
        if task is None:
            continue
        worktree = _address_review_slug_dir(task)
        if worktree is not None:
            slugs.capture(worktree)
        target = _auto_fix_ci_guard_target(task, clients)
        if target is not None:
            slugs.capture(target[1])
    return slugs


def act_then_dispatch[J](
    act: Callable[..., list[J]],
    dispatch: Callable[[list[J]], list[str]],
    candidates: list[ReviewRecipeCandidate],
    **kwargs: object,
) -> list[str]:
    """Run a review-recipe act phase, then dispatch the jobs it returned.

    The review-recipe act phases only prepare jobs under ``dev_queue_lock()``
    (#1229, #1232); ``reconcile()`` runs them after ``sessions_lock``
    releases. This stands in for that act-then-drain for tests that exercise
    act + dispatch end to end without a full ``reconcile()``: *act* is called
    as ``act(candidates, **kwargs)``, its job list goes to *dispatch*, and the
    acted ticket ids *dispatch* reports are returned.

    For the two acts whose repo guard reads pre-captured slugs (#2564), a
    ``repo_slugs`` the caller did not pass is captured first with
    :func:`capture_repo_slugs`, as ``reconcile()`` does before its lock. An
    explicit ``repo_slugs=`` (an empty ``RepoSlugs()`` included) is used as is.
    """
    if act in _SLUG_GUARDED_ACTS and "repo_slugs" not in kwargs:
        clients = kwargs["clients"]
        assert isinstance(clients, dict)
        kwargs["repo_slugs"] = capture_repo_slugs(candidates, clients=clients)
    return dispatch(act(candidates, **kwargs))


def _mk_session(
    sid: str,
    surface_ref: str | None,
    status: SessionStatus = SessionStatus.ACTIVE,
    started_at: datetime | None = None,
    purpose: SessionPurpose = SessionPurpose.IMPL,
) -> Session:
    return _make_daemon_session(
        id=sid,
        name=f"client-a/{sid}",
        purpose=purpose,
        origin=SessionOrigin.USER,
        status=status,
        worktree_path=None,
        surface_ref=surface_ref,
        started_at=(
            started_at if started_at is not None else datetime(2026, 4, 19, tzinfo=UTC)
        ),
    )


def _mk_daemon_completed_session(sid: str) -> Session:
    """Build a DAEMON COMPLETED session for silent-revert testing."""
    return _make_daemon_session(
        id=sid,
        name=f"client-a/{sid}",
        status=SessionStatus.COMPLETED,
        worktree_path=None,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def _mk_headless_daemon_session(
    sid: str,
    worktree: Path,
    started_at: datetime,
    surface_ref: str | None = "fake-short-id",
) -> Session:
    """Build a headless DAEMON ACTIVE session with a cw-context.json."""
    sess = _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        worktree_path=worktree,
        surface_ref=surface_ref,
        started_at=started_at,
    )
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "' + sid + '"}'
    )
    return sess


def _routed_last_result(**extra: Any) -> dict[str, Any]:
    """A terminal-shaped ``last_result`` a #2458 partial route already consumed.

    The exact shape ``_stamp_sentinel_partial_route_consumed`` leaves behind:
    the staged ``stage_complete`` payload with the consumed marker merged in
    alongside it (#2524). *extra* keys are merged last, so a test can add a
    refusal latch or override the marker's value.
    """
    return {
        **_stage_complete_payload(),
        _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY: True,
        **extra,
    }


def _mk_routed_session(
    sid: str,
    worktree: Path,
    *,
    started_at: datetime | None = None,
    surface_ref: str | None = _FAKE_SURFACE_REF,
    status: SessionStatus = SessionStatus.ACTIVE,
    last_result: dict[str, Any] | None = None,
) -> Session:
    """A headless DAEMON session whose staged result was already routed (#2524).

    Built on :func:`_mk_headless_daemon_session`, so the name is
    ``client-a/auto-dev/<sid>`` and the ticket id resolves to *sid*.
    ``last_result`` defaults to :func:`_routed_last_result`; the source is
    EMIT_CLI, the only path that stamps the consumed marker.
    """
    sess = _mk_headless_daemon_session(
        sid,
        worktree,
        started_at if started_at is not None else datetime(2026, 1, 1, tzinfo=UTC),
        surface_ref=surface_ref,
    )
    sess.status = status
    sess.last_result = last_result if last_result is not None else _routed_last_result()
    sess.last_result_source = LastResultSource.EMIT_CLI
    return sess


def _stamp_transcript_age(
    home: Path,
    worktree: Path,
    *,
    stale_minutes: float,
    now: datetime | None = None,
    surface_ref: str = _FAKE_SURFACE_REF,
) -> Path:
    """Write a transcript for *worktree* whose mtime is *stale_minutes* old.

    Promoted from ``tests/test_doctor.py``'s class-8 ``_stamp_transcript``
    (#2524). The age is measured from *now* (default: the real clock).
    """
    transcript = _write_idle_transcript(
        home, worktree, filename=f"{surface_ref}-sess.jsonl"
    )
    anchor = now if now is not None else datetime.now(UTC)
    ts = (anchor - timedelta(minutes=stale_minutes)).timestamp()
    os.utime(str(transcript), (ts, ts))
    return transcript


def _write_agent_spawn_stamp(
    worktree: Path, *, unresolved_count: int, stamped_at: datetime
) -> None:
    """Write an ``agent_spawn_stamp`` into *worktree*'s cw-context.json.

    Promoted from ``tests/test_doctor.py``'s class-8 ``_write_spawn_stamp``
    (#2524): the same on-disk payload ``cw agent-spawn-pre`` produces.
    """
    (worktree / ".claude").mkdir(parents=True, exist_ok=True)
    payload = {
        "agent_spawn_stamp": {
            "unresolved_count": unresolved_count,
            "last_stamped_at": stamped_at.isoformat(),
        }
    }
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_fake_roster(tmp_path: Path, *, supervisor_pid: int = 12345) -> Path:
    """Write an empty daemon ``roster.json`` under *tmp_path*; return its path."""
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(
        json.dumps({"supervisorPid": supervisor_pid, "workers": {}}),
        encoding="utf-8",
    )
    return roster_path


def _install_fake_daemon_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    surface_ref: str = _FAKE_SURFACE_REF,
) -> tuple[Path, FakeNativeDaemonClient]:
    """Wire a fake daemon (with *surface_ref* live) into every doctor seam.

    Promoted from ``tests/test_doctor.py``'s class-8 ``_setup_common``
    (#2524). Writes an empty roster, points the doctor's roster-path seams at
    it, returns the per-test ``Path.home()`` (already redirected by the
    autouse ``_isolate_home`` fixture, #1756) so transcript lookup finds
    files the test writes, and patches every doctor-side
    ``get_native_daemon_client`` -- including the stranded-routed-result
    class's -- so a doctor run stays hermetic.
    """
    roster_path = _write_fake_roster(tmp_path)
    monkeypatch.setattr("cw.doctor.wedge.session_liveness._ROSTER_PATH", roster_path)
    monkeypatch.setattr("cw.doctor.versions._ROSTER_PATH", roster_path)

    home = Path.home()

    daemon = FakeNativeDaemonClient()
    daemon._live.add(surface_ref)
    for target in (
        "cw.doctor.wedge.blocked_on_user.get_native_daemon_client",
        "cw.doctor.wedge.session_liveness.get_native_daemon_client",
        "cw.doctor.wedge.orphans.get_native_daemon_client",
        "cw.doctor.wedge.reap.get_native_daemon_client",
        "cw.doctor.loop_health.get_native_daemon_client",
        "cw.doctor.routed_result_wedge.get_native_daemon_client",
    ):
        monkeypatch.setattr(target, lambda: daemon)
    return home, daemon


def _make_pending_fix_dispatch(**overrides: Any) -> PendingFixDispatch:
    """Minimal-but-valid ``PendingFixDispatch`` with keyword overrides.

    Dict-merge + model-construct idiom matching ``_make_ticket_task``
    (``tests/conftest.py``); shared so ``tests/test_reconcile_fix_dispatch.py``,
    ``tests/test_dispatch.py``, and the backstop-exemption tests
    (``tests/test_reconcile_tasks.py``, ``tests/test_reconcile_phantom.py``)
    do not each hand-roll their own ``PendingFixDispatch(...)`` defaults.
    """
    kwargs: dict[str, Any] = {
        "prompt": "fix the MUST_FIX items\n",
        "label": "fix-T-1",
        "cycle": 1,
        "requested_by_session_id": "review-sess",
        "requested_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return PendingFixDispatch(**kwargs)


def _shipped_salvage_payload(ticket_id: str = "salv-1") -> dict[str, Any]:
    """A shipped sentinel payload claiming *ticket_id*.

    Reconcile's transcript scans skip a sentinel that claims a different ticket
    than the session's (#2515), so a test whose session is not ``salv-1`` must
    pass its own ticket id here.
    """
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": 12,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
        "branch": f"auto-dev/{ticket_id}",
        "worktree_path": f"/tmp/wt/{ticket_id}",
        "fork_point_sha": "abc1234",
        "commits": ["sha1"],
        "pr": {
            "number": 99,
            "url": "https://github.com/foo/bar/pull/99",
            "auto_merge": True,
            "base": "main",
        },
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "cost_usd": 1.5,
        "next_actions": ["wait_for_ci"],
    }


def _no_op_salvage_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "salv-noop",
        "status": "no_op",
        "stage_reached": "stage1_pre_flight",
        "scope": {
            "tier": "small",
            "files": 0,
            "lines_estimate": 0,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "none",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["close_issue_as_completed"],
    }


def _stage_complete_payload() -> dict[str, Any]:
    """Minimal valid stage_complete payload (#699): PR-less intermediate success.

    Models an IMPL worker that finished its stage and exited (the staged engine
    spawns a fresh worker per stage). status=stage_complete is in
    STAGE_SUCCESS_STATUSES but NOT in SALVAGE_TERMINAL_STATUSES, so terminal
    salvage skips it — it must advance the stage, not be reverted as a crash
    (#716).
    """
    return {
        "schema_version": 4,
        "ticket_id": "salv-stage",
        "status": "stage_complete",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 60,
            "lines_actual": 55,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
        "branch": "dev/salv-stage",
        "worktree_path": "/tmp/wt/salv-stage",
        "fork_point_sha": "deadbeef",
        "commits": ["sha-a", "sha-b"],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": [],
    }


def _blocked_result_payload(reason: str = "status_unknown") -> dict[str, Any]:
    """Minimal valid parser-synthesized ``BlockedResult`` payload.

    The shape ``_validate_harvest_payload`` discriminates onto ``BlockedResult``
    rather than ``AutoDevResult``: ``status="blocked"`` with **no**
    ``schema_version`` key (GitHub #1457). Companion to
    :func:`_make_terminal_payload` for the AutoDevResult side.
    """
    return {
        "status": "blocked",
        "blocker": {
            "stage": "stage2_impl",
            "reason": reason,
            "details": "synthetic blocked result for tests",
        },
    }


def _write_salvage_transcript(
    home: Path,
    worktree: Path,
    claude_session_id: str,
    payload: dict[str, Any],
    *,
    surface_ref: str = "fake-short-id",
    emit_via: str = "text",
    extra_records: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a transcript jsonl under ``home`` carrying a wrapped sentinel.

    Mirrors Claude's on-disk layout: ``<home>/.claude/projects/<encoded>/
    <surface_ref>-<uuid>.jsonl`` with the encoded path replacing both ``/``
    and ``.`` with ``-`` (matching Claude Code's actual encoding).

    ``surface_ref`` is prepended to the filename so that
    ``_locate_session_transcript``'s surface_ref-prefix glob can find it.
    The full stem (``<surface_ref>-<uuid>``) becomes the stored
    ``claude_session_id``.

    ``emit_via`` controls where the sentinel frame lands:
    - ``"text"`` (default): inside an assistant text block (the common case).
    - ``"tool_result"``: inside a Bash tool_result (stdout) block, as happens
      when a worker emits the sentinel via ``cat <<EOF`` (#731). The assistant
      record carries only narrative + the tool_use command echo, so the frame
      is reachable ONLY by scanning tool_result blocks.

    ``extra_records``: optional JSONL records written before the main sentinel
    record. Use this to produce multi-sentinel transcripts (e.g. an illustrative
    example block followed by the real sentinel) for last-match tests (#591).
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload)
    frame = f"<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
    stem = f"{surface_ref}-{claude_session_id}"
    path = project_dir / f"{stem}.jsonl"
    prefix = ""
    if extra_records:
        prefix = "\n".join(json.dumps(r) for r in extra_records) + "\n"
    if emit_via == "tool_result":
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Now emitting the sentinel."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"cat <<'EOF'\n{frame}EOF"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": frame}],
                },
            },
        ]
        path.write_text(prefix + "\n".join(json.dumps(r) for r in records) + "\n")
        return path
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"narrative\n{frame}"}],
        },
    }
    path.write_text(prefix + json.dumps(record) + "\n")
    return path


def _write_idle_transcript_with_text(
    home: Path,
    worktree: Path,
    assistant_text: str,
    filename: str = "fake-short-id-sess-486.jsonl",
) -> Path:
    """Write a transcript with a single assistant text block under the project dir.

    Default filename starts with ``fake-short-id`` so that
    ``_locate_session_transcript``'s surface_ref-prefix glob finds it when the
    session has ``surface_ref="fake-short-id"`` (the default in
    ``_mk_headless_daemon_session``).

    Round-3 (#1692): thin wrapper over ``tests.conftest._write_stop_hook_
    transcript`` -- both helpers wrote the identical single-assistant-record
    shape independently. That helper keys its file on an exact
    ``claude_session_id`` rather than an arbitrary ``filename``, so this
    strips the ``.jsonl`` suffix back off to recover the same path.
    """
    return _write_stop_hook_transcript(
        home, worktree, filename.removesuffix(".jsonl"), assistant_text
    )


def _write_transcript_records(
    home: Path,
    worktree: Path,
    records: list[dict[str, object]],
    filename: str = "fake-short-id-sess-1076.jsonl",
) -> Path:
    """Write an arbitrary sequence of JSONL records under the project dir for
    *worktree*.

    Each element of *records* is dumped via ``json.dumps`` on its own line, in order.
    Mirrors the project-dir encoding used by ``_write_idle_transcript`` /
    ``_write_idle_transcript_with_text`` (double-replace, #463) and the
    ``fake-short-id`` filename-prefix convention those helpers use so
    ``_locate_session_transcript``'s surface_ref-prefix glob finds the file.
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _ul_record(text: str, timestamp: str | None = None) -> dict[str, object]:
    """One assistant text record (optionally timestamped) for
    ``_write_transcript_records`` (#1345 usage-limit recency-gate tests)."""
    record: dict[str, object] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


# The observed eb87c137 transcript string quoted in #2153's ticket body.
_API_ERROR_TEXT = "API Error: Server error mid-response"


def _api_error_then_cost_state_records(
    error_ts: datetime,
    cost_ts: datetime,
    *,
    error_text: str = _API_ERROR_TEXT,
) -> list[dict[str, object]]:
    """A work record, an API Error assistant record, then a ``cost-state`` (#2153).

    Separate from ``tests/test_reconcile_usage_limit_mid_turn.py``'s
    ``_limit_tail``/``_trailing_metadata``: those pin the usage-limit capture
    (with a ``last-prompt`` tail and its own frozen timestamps), while the
    dead-session page needs caller-chosen timestamps and an API Error text.
    Only the ``cost-state`` record ``type`` was observed (see that module's
    ``_trailing_metadata`` docstring); its other fields are filler.
    """
    return [
        _ul_record("working on it", (error_ts - timedelta(minutes=1)).isoformat()),
        _ul_record(error_text, error_ts.isoformat()),
        {"type": "cost-state", "timestamp": cost_ts.isoformat()},
    ]


# Verbatim capture, dev-1751 impl worker, session
# 286032f7-47ee-4985-a45d-e7a946aa1d9d, 2026-08-18T17:27:09.071Z (#1923).
# Shared by tests/test_reconcile_phantom.py and
# tests/test_reconcile_shared_sentinels.py so a correction to the captured
# text only needs to land in one place.
PROVIDER_OVERLOAD_TEXT = (
    'Agent "Implement plan for ticket #1751" failed: Agent terminated early '
    "due to an API error: API Error: 529 Overloaded. This is a server-side "
    "issue, usually temporary — try again in a moment. If it persists, check "
    "https://status.claude.com."
)


def _notification_record(text: str, kind: str = "user") -> dict[str, object]:
    """One task-notification record for ``_write_transcript_records`` (#1923).

    Both shapes are lifted verbatim from a live capture (dev-1751 impl
    worker, session 286032f7-47ee-4985-a45d-e7a946aa1d9d) -- neither is
    reachable through ``_iter_assistant_records``/``_ul_record``, which
    require ``type == "assistant"`` and a list-shaped ``message.content``.

    ``kind="user"`` builds a ``type: "user"`` record with a bare-string
    ``message.content``. ``kind="queue-operation"`` builds a
    ``type: "queue-operation"`` record with a bare-string top-level
    ``content`` (no ``message`` key at all).
    """
    if kind == "queue-operation":
        return {"type": "queue-operation", "operation": "enqueue", "content": text}
    return {"type": "user", "message": {"role": "user", "content": text}}


def _make_terminal_payload(status: str, ticket_id: str) -> dict[str, Any]:
    """Build a minimal valid AutoDevResult payload for the given terminal status."""
    # Base shape shared by most statuses.
    base: dict[str, Any] = {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": status,
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": [],
    }
    if status == "plan_pending_approval":
        base["next_actions"] = ["user_approve_plan"]
    elif status == "review_pending_approval":
        # review_pending has a branch + impl stage
        base["stage_reached"] = "stage3_review"
        base["scope"]["lines_actual"] = 8
        base["branch"] = f"dev/{ticket_id}"
        base["fork_point_sha"] = "abc123"
        base["commits"] = ["sha1"]
        base["next_actions"] = ["user_approve_review"]
    elif status == "merge_gate_blocked":
        # merge_gate_blocked requires small tier (already set), branch, impl stage
        base["stage_reached"] = "stage4a_merge_gate"
        base["scope"]["lines_actual"] = 8
        base["branch"] = f"dev/{ticket_id}"
        base["fork_point_sha"] = "abc123"
        base["commits"] = ["sha1"]
        base["next_actions"] = ["resolve_merge_gate"]
    elif status == "ambiguities_pending_resolution":
        base["ambiguities"] = [{"question": "Open or closed enum?"}]
        base["next_actions"] = ["user_resolve_ambiguities"]
    elif status == "premises_pending_verification":
        base["premises"] = [{"claim": "PR #42 codified a deliberate decision"}]
        base["next_actions"] = ["user_verify_premises"]
    elif status == "merge_pending":
        # merge_pending requires a non-null pr (#899) -- PR created,
        # CI/merge gate pending.
        base["stage_reached"] = "stage5_post_create"
        base["scope"]["lines_actual"] = 8
        base["branch"] = f"dev/{ticket_id}"
        base["fork_point_sha"] = "abc123"
        base["commits"] = ["sha1"]
        base["pr"] = {
            "number": 101,
            "url": "https://github.com/org/repo/pull/101",
            "auto_merge": True,
            "base": "main",
        }
    return base


def _mk_daemon_session_with_worktree(
    sid: str,
    status: SessionStatus,
    wt_path: Path,
) -> Session:
    """Build a DAEMON session with worktree_path set, branch=None."""
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        status=status,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
        worktree_path=wt_path,
        branch=None,  # Always None on DAEMON sessions
    )


def _mk_timed_out_daemon_session(
    sid: str,
    ticket_id: str,
    completed_at: datetime,
) -> Session:
    """Return a TIMED_OUT DAEMON session mirroring test_doctor.py helper shape.

    branch=None because DAEMON sessions always have branch=None (spawn.py never
    sets it). name follows the auto-dev/<ticket_id> convention.
    """
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{ticket_id}",
        status=SessionStatus.TIMED_OUT,
        worktree_path=None,
        surface_ref=None,
        started_at=datetime.now(UTC),
        branch=None,
        completed_at=completed_at,
    )


def _state_queue_snapshot() -> bytes:
    """Read state + queue + events-inbox bytes for detect/propose-purity assertions."""
    from cw.config import dev_queue_file, events_dir, state_file

    inbox = events_dir() / "inbox.jsonl"
    inbox_bytes = inbox.read_bytes() if inbox.exists() else b""
    return state_file().read_bytes() + dev_queue_file().read_bytes() + inbox_bytes


def _mk_live_idle_daemon_session(
    sid: str,
    surface_ref: str,
    started_at: datetime,
    idle_observation_count: int = 0,
    worktree_path: Path | None = None,
) -> Session:
    """Build a live DAEMON ACTIVE session suitable for idle watchdog tests."""
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        surface_ref=surface_ref,
        started_at=started_at,
        idle_observation_count=idle_observation_count,
        worktree_path=worktree_path,
    )


def _mk_phantom_daemon_session(
    sid: str,
    started_at: datetime,
    surface_ref: str = "dead-ref",
    worktree_path: Path | None = None,
) -> Session:
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        surface_ref=surface_ref,
        started_at=started_at,
        worktree_path=worktree_path,
    )


def _auto_config(**kwargs: object) -> OrchestratorConfig:
    """Return OrchestratorConfig with reap_policy=AUTO for auto-revert tests."""
    return OrchestratorConfig(reap_policy=ReapPolicy.AUTO, **kwargs)  # type: ignore[arg-type]


def _client_with_lane(
    client_name: str,
    lane_name: str,
    *,
    workspace_path: Path | None = None,
    base: ClientConfig | None = None,
    **lane_kwargs: object,
) -> ClientConfig:
    """Build a ClientConfig with one lane.

    Pass any :class:`~cw.models.LaneConfig` field (e.g. ``reap_policy=``,
    ``attempt_ceiling=``) as a keyword — generalized by #1751 so lane-scoped
    resolver tests share one factory instead of accreting a near-duplicate
    helper per field. Built via ``model_validate`` on a merged dict (the same
    idiom as ``conftest._make_ticket_task``) so the open ``**lane_kwargs``
    needs no type suppression.

    Pass an existing *base* :class:`~cw.models.ClientConfig` to copy its other
    fields (e.g. a fixture-provided ``worktree_base``) while replacing its
    lanes with the single one built here, instead of constructing a fresh,
    minimal ``ClientConfig`` from scratch — the other shape a lane-override
    test occasionally needs (#1751 review round 1).
    """
    from cw.models import LaneConfig

    lane = LaneConfig.model_validate({"name": lane_name, **lane_kwargs})
    if base is not None:
        return base.model_copy(update={"name": client_name, "lanes": [lane]})
    return ClientConfig(
        name=client_name,
        workspace_path=workspace_path or Path("/tmp/ws"),
        lanes=[lane],
    )


# ---------------------------------------------------------------------------
# #1487 — stale-merge-base scope fixtures
#
# Shared by every test that needs the #1393 shape: a branch whose base ref
# advanced *after* it forked, so a self-report computed against the stale
# merge-base is grossly inflated relative to the branch's own churn.
# ---------------------------------------------------------------------------

SCOPE_GUARD_BRANCH = "dev/1487-scope"
SCOPE_GUARD_FILES = 3
SCOPE_GUARD_LINES = 15
_SCOPE_GUARD_LINES_PER_FILE = 5
_SCOPE_GUARD_BASE_FILES = 8
_SCOPE_GUARD_BASE_LINES_PER_FILE = 40


def _scope_guard_git(repo: Path, *args: str) -> None:
    """Run git in *repo* with a GIT_*-stripped env."""
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=clean_env,
    )


def _write_lines(repo: Path, prefix: str, count: int, lines_each: int) -> None:
    for i in range(count):
        (repo / f"{prefix}_{i}.txt").write_text("l\n" * lines_each, encoding="utf-8")
    _scope_guard_git(repo, "add", "-A")
    _scope_guard_git(repo, "commit", "-m", f"{prefix} work")


def _make_stale_base_repo(
    make_git_repo: Callable[..., Path],
    name: str,
    *,
    default_branch: str = "main",
) -> Path:
    """Return a repo checked out on a branch whose base advanced after the fork.

    The branch carries exactly ``SCOPE_GUARD_FILES`` files /
    ``SCOPE_GUARD_LINES`` lines of its own; the base branch gains far more
    afterwards, so measuring against a stale merge-base would over-count.
    """
    repo = make_git_repo(name)
    if default_branch != "main":
        _scope_guard_git(repo, "branch", "-m", "main", default_branch)
    _scope_guard_git(repo, "remote", "add", "origin", str(repo))
    _scope_guard_git(repo, "fetch", "origin", default_branch)
    _scope_guard_git(repo, "checkout", "-b", SCOPE_GUARD_BRANCH)
    _write_lines(repo, "branchwork", SCOPE_GUARD_FILES, _SCOPE_GUARD_LINES_PER_FILE)
    _scope_guard_git(repo, "checkout", default_branch)
    _write_lines(
        repo, "basechurn", _SCOPE_GUARD_BASE_FILES, _SCOPE_GUARD_BASE_LINES_PER_FILE
    )
    _scope_guard_git(repo, "fetch", "origin", default_branch)
    _scope_guard_git(repo, "checkout", SCOPE_GUARD_BRANCH)
    return repo


def _inflate_scope(payload: dict[str, Any]) -> dict[str, Any]:
    """Overwrite *payload*'s scope with the #1393 inflated self-report."""
    payload["scope"] = {
        "tier": "small",
        "files": 18,
        "lines_estimate": 60,
        "lines_actual": 1567,
        "forbidden_touched": False,
    }
    return payload


def _attention_events(
    consumer: str, ticket_id: str, *, paused_status: str | None = None
) -> list[dict[str, object]]:
    """``session.needs_attention`` payloads for *ticket_id*, read as *consumer*.

    *paused_status*, when set, keeps only the payloads carrying that value, so
    a test can count one page kind while another (e.g. phantom's
    ``sentinel_mismatch_veto_cap_exhausted``) shares the ticket (#2513).
    """
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        if e.payload.get("ticket_id") == ticket_id
        and (paused_status is None or e.payload.get("paused_status") == paused_status)
    ]


def _failing_record_event(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str,
    event_type: OrchestratorEventType,
    fail_for: Callable[[dict[str, object]], bool],
) -> list[int]:
    """Make the ``record_event`` bound at *target* raise OSError on matching calls.

    *target* is the dotted path of the ``record_event`` name the code under
    test looks up (e.g. ``"cw.reconcile._shared._stage_refusal.record_event"``):
    a patch anywhere else resolves but never intercepts. Returns a
    one-element-per-failure list so a test can count the failures.
    """
    from cw.events import record_event as real_record_event

    failures: list[int] = []

    def flaky(
        etype: OrchestratorEventType,
        payload: dict[str, object] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> object:
        if etype is event_type and fail_for(payload or {}):
            failures.append(1)
            msg = "disk full"
            raise OSError(msg)
        return real_record_event(etype, payload, correlation_id=correlation_id)

    monkeypatch.setattr(target, flaky)
    return failures


def mk_unowned_running_row(
    *,
    client: str,
    ticket_id: str,
    attempts: int,
    claimed_at: datetime | None,
    session_id: str | None = None,
    ever_spawned: bool = False,
    **overrides: object,
) -> TicketTask:
    """Append one RUNNING row to the saved dev queue and return it (#2591).

    The shape a claim leaves when the post-launch stamp never bound a session:
    RUNNING, ``session_id`` unset, and ``ever_spawned=False`` as
    ``dev_queue_add`` seeds it (``cli/dev_queue/crud.py``; the model default
    is True). *overrides* reach the row unchanged (``lane``, ``stage``,
    the per-arrival markers).
    """
    row = TicketTask.model_validate(
        {
            "ticket_id": ticket_id,
            "client": client,
            "status": QueueItemStatus.RUNNING,
            "attempts": attempts,
            "claimed_at": claimed_at,
            "session_id": session_id,
            "ever_spawned": ever_spawned,
            **overrides,
        }
    )
    store = load_dev_queue()
    store.tasks.append(row)
    save_dev_queue(store)
    return row


def write_unreadable_claim_context(worktree: Path, text: str) -> None:
    """Write *text* verbatim as *worktree*'s cw-context.json (#2591).

    Only for the shapes the real writer can never produce: a non-JSON file
    (``"{not json"``) or a JSON value that is not an object (``"[1, 2]"``).
    Every readable claim context goes through ``_write_hook_context_file``.
    """
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(text, encoding="utf-8")
