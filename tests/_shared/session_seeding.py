"""Builders that seed sessions, ticket tasks and tick summaries into test
state, and the audit-append failure doubles. Moved from
``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.config import load_state, save_state
from cw.models import (
    CwState,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.orchestrate import TickSummary

if TYPE_CHECKING:
    from collections.abc import Mapping


def _plan_pending_payload(**overrides: object) -> dict[str, Any]:
    """Minimal valid ``plan_pending_approval`` AutoDevResult payload.

    Shared by ``test_auto_dev_result.py`` (schema/parser cases) and
    ``test_result.py`` (the #2382 emit fingerprint binding), which used to
    carry two drifting private copies. ``overrides`` are merged last, so a
    caller sets a key -- ``plan_draft_fingerprint=None`` included -- and the
    key is present with that value.
    """
    payload: dict[str, Any] = {
        "schema_version": 1,
        "ticket_id": "GEN-2",
        "status": "plan_pending_approval",
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "large",
            "files": 25,
            "lines_estimate": 1200,
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
        "next_actions": ["user_approve_plan"],
    }
    payload.update(overrides)
    return payload


def _fail_audit_append(monkeypatch: pytest.MonkeyPatch) -> list[OrchestratorEventType]:
    """Make every ``session.result_emitted`` audit append raise ``OSError`` (#2465).

    Patches ``cw.result.record_event`` -- the one chokepoint behind
    ``_record_result_emitted_audit``, which every result-write path (direct
    CLI, Stop-hook harvest, executor-direct, reconcile) funnels through.
    Returns the list of attempted event types so a test can assert the append
    was (or, for a refused write, was not) tried.
    """
    attempts: list[OrchestratorEventType] = []

    def _raise_event(
        event_type: OrchestratorEventType, *_a: object, **_k: object
    ) -> None:
        attempts.append(event_type)
        msg = "event inbox unavailable"
        raise OSError(msg)

    monkeypatch.setattr("cw.result.record_event", _raise_event)
    return attempts


def _audit_failure_logged(
    caplog: pytest.LogCaptureFixture,
    *,
    session_id: str,
    source: str | None = None,
    status: str | None = None,
    payload_digest: str | None = None,
) -> logging.LogRecord | None:
    """Return the fail-open audit-append WARNING for SESSION_ID, else ``None``.

    Only a ``cw.result`` WARNING record counts. ``session=<id> `` is matched
    with its trailing space so ``test1234`` never matches ``test12345``. When
    SOURCE, STATUS or PAYLOAD_DIGEST are given the found record must carry
    them, so a log that drops those fields fails the calling test.
    """
    record = next(
        (
            r
            for r in caplog.records
            if r.name == "cw.result"
            and r.levelno == logging.WARNING
            and "audit append failed" in r.getMessage()
            and f"session={session_id} " in r.getMessage()
        ),
        None,
    )
    if record is None:
        return None
    message = record.getMessage()
    expected = {
        "source": source,
        "status": status,
        "payload_digest": payload_digest,
    }
    for field, value in expected.items():
        if value is not None:
            assert f"{field}={value}" in message, (
                f"audit-failure log is missing {field}={value}: {message}"
            )
    return record


def _seed_daemon_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    session_id: str = "test1234",
    client: str = "test-client",
    name: str | None = None,
    surface_ref: str | None = "fake-pane-99",
    status: SessionStatus = SessionStatus.ACTIVE,
    **overrides: object,
) -> Session:
    """Create and save a daemon session in state.

    Extra ``**overrides`` are merged after the named params, so a caller can
    set any additional ``Session`` field (e.g. ``purpose``, ``origin``,
    ``worktree_path``) without a parallel seed helper (#1308).
    """
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "id": session_id,
        "name": name or f"{client}/auto-dev/GEN-42",
        "client": client,
        "purpose": SessionPurpose.IMPL,
        "origin": SessionOrigin.DAEMON,
        "status": status,
        "workspace_path": workspace,
        "surface_ref": surface_ref,
    }
    kwargs.update(overrides)
    sess = Session.model_validate(kwargs)
    state = CwState(sessions=[sess])
    save_state(state)
    return sess


def _make_daemon_session(**overrides: object) -> Session:
    """Canonical non-persisting DAEMON ``Session`` builder (#1308).

    Builds a fixed baseline daemon session; any ``**overrides`` are merged
    after the defaults so every local ``Session(...)`` construction across the
    test suite can delegate here as ``_make_daemon_session(field=value, ...)``.
    """
    kwargs: dict[str, object] = {
        "id": "sess-1",
        "name": "client-a/auto-dev/T-1",
        "client": "client-a",
        "purpose": SessionPurpose.IMPL,
        "origin": SessionOrigin.DAEMON,
        "status": SessionStatus.ACTIVE,
        "workspace_path": Path("/tmp/ws"),
        "worktree_path": Path("/tmp/wt"),
        "surface_ref": "live-ref",
        "claude_session_id": None,
        "started_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return Session.model_validate(kwargs)


def _seed_completed_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    ticket_id: str,
    client: str = "test-client",
    status: SessionStatus = SessionStatus.TIMED_OUT,
    last_result: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> Session:
    """Seed a TIMED_OUT or COMPLETED session for a given ticket in state.

    Hoisted from ``tests/test_spawn.py`` (#2280) so ``tests/test_codex_
    executor.py`` can seed a prior codex-review park for the same
    ``prior_attempts_summary`` machinery without duplicating the builder.
    """
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    sess = Session(
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=status,
        workspace_path=workspace,
        last_result=last_result,
        completed_at=completed_at or datetime.now(UTC),
    )
    state = load_state()
    state.sessions.append(sess)
    save_state(state)
    return sess


def _seed_sessions(*sessions: Session) -> None:
    """Append pre-built *sessions* to sessions.json in a single ``save_state``.

    Appends to (does not replace) whatever is already persisted. Pairs with the
    non-persisting ``_make_daemon_session`` builder. Hoisted from
    ``tests/test_session_retention.py`` and ``tests/test_session_inspect.py``
    (#1999), following the #1308/#2438 hoist precedent.

    Why ``_seed_daemon_session`` does not fit: it REPLACES the whole state with
    a single freshly built session (``CwState(sessions=[sess])``) and builds its
    own ``tmp_path/workspace/<client>`` directory, so it cannot take pre-built
    Sessions or controlled ``started_at``/``completed_at`` for N sessions in one
    save. Changing it to append is rejected: it has ~90 call sites across 5
    files that may rely on the replace semantics.
    """
    state = load_state()
    state.sessions.extend(sessions)
    save_state(state)


def write_legacy_sessions_json(
    state_dir: Path,
    *,
    schema_version: int = 4,
    sessions: list[dict[str, object]] | None = None,
    mtime_ns: int | None = None,
) -> Path:
    """Write a legacy-schema ``sessions.json`` under *state_dir* and return it.

    Default: v4 with one valid session carrying a legacy ``surface_ref`` (the
    payload from ``test_migrate_round_trip_clears_legacy_surface_ref``). When
    *mtime_ns* is given the file's mtime and atime are set to it, so a later
    write is distinguishable from the seed.
    """
    if sessions is None:
        sessions = [
            {
                "id": "legacy01",
                "name": "c/impl",
                "client": "c",
                "purpose": "impl",
                "workspace_path": str(state_dir),
                "surface_ref": "cmux-legacy",
            }
        ]
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "sessions.json"
    path.write_text(
        json.dumps({"schema_version": schema_version, "sessions": sessions})
    )
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def find_completed_session(state: CwState) -> Session:
    """Return the sole session carrying a terminal last_result.

    Shared by the test_executor_*.py and test_codex_executor.py completion-path
    tests so the `next((s for s in state.sessions if s.last_result is not
    None), None)` idiom isn't duplicated at every call site (GitHub #1458).
    Asserts exactly one such session exists.
    """
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    return session


def _make_ticket_task(**overrides: object) -> TicketTask:
    """Minimal-but-valid ``TicketTask`` with keyword overrides (#1308).

    Only ``ticket_id`` and ``client`` are required by the model; both are
    defaulted here so ``_make_ticket_task()`` yields a valid PENDING task.
    Follows the same dict-merge + ``model_validate`` idiom as
    ``_make_escalation`` / ``_make_finding``.
    """
    kwargs: dict[str, object] = {
        "ticket_id": "T-1",
        "client": "test-client",
    }
    kwargs.update(overrides)
    return TicketTask.model_validate(kwargs)


def _make_tick_summary(**overrides: object) -> TickSummary:
    """Canonical ``TickSummary`` builder with keyword overrides (#1875).

    Follows the same dict-merge + ``model_validate`` idiom as
    ``_make_daemon_session`` / ``_make_ticket_task``. Shared so the doctor's
    on-demand loop-liveness check and the dispatch loop's proactive staleness
    watchdog -- which exercise the *same* stale+pending predicate
    (``cw.dispatch._stale_pending_clients``) -- never drift onto two
    independently-maintained builders for it.
    """
    kwargs: dict[str, object] = {
        "claimed": 0,
        "pending": 0,
        "running": 0,
        "cap": 3,
        "skip_reason": "none",
        "tick_at": datetime.now(UTC),
    }
    kwargs.update(overrides)
    return TickSummary.model_validate(kwargs)


def _patch_ticks(
    monkeypatch: pytest.MonkeyPatch,
    per_client: Mapping[str, Mapping[str, object]],
) -> None:
    """Patch ``cw dev-queue status``'s tick read to one tick per client (#2696).

    Each value is a ``_make_tick_summary`` override mapping, so a client gets
    the canonical defaults plus whatever it overrides. Installs ``{client:
    tick}`` through the ``cw.cli.dev_queue.status.client_tick_snapshots`` seam
    the status tests share (#2516: status reads client-wide ticks through it,
    so the stub takes and ignores the one ``events`` argument; per-ticket
    events seeded in the inbox still reach the held-by-dispatch block);
    ``_patch_tick`` (``tests/test_cli.py``) and ``_patch_tick_for``
    (``tests/test_dev_queue.py``) are one-client callers.
    """
    ticks = {
        client: _make_tick_summary(**overrides)
        for client, overrides in per_client.items()
    }
    monkeypatch.setattr(
        "cw.cli.dev_queue.status.client_tick_snapshots", lambda _events: ticks
    )
