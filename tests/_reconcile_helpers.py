"""Shared test helpers for the ``cw.reconcile`` per-submodule test suite.

Cross-category factories, payload builders, and transcript writers used by two
or more of the split ``test_reconcile_*.py`` files. This module has no
``test_`` prefix, so pytest does not collect it (same convention as
``tests/conftest.py``); it is imported explicitly by the test modules that use
each helper.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    ReapPolicy,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from tests.conftest import _make_daemon_session


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


def _shipped_salvage_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "salv-1",
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
        "branch": "auto-dev/salv-1",
        "worktree_path": "/tmp/wt/salv-1",
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
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    record = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
    )
    path.write_text(record + "\n")
    return path


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
    lane_policy: ReapPolicy,
    *,
    workspace_path: Path | None = None,
) -> ClientConfig:
    """Build a ClientConfig with one lane carrying a specific reap_policy."""
    from cw.models import LaneConfig

    return ClientConfig(
        name=client_name,
        workspace_path=workspace_path or Path("/tmp/ws"),
        lanes=[LaneConfig(name=lane_name, reap_policy=lane_policy)],
    )


def _write_staged_clients_yaml(tmp_config_dir: Path, client_name: str) -> None:
    """Write a minimal staged clients.yaml for _apply_sentinel_to_task tests.

    Uses the same tmp_config_dir that tmp_config_dir fixture redirected
    cw.config.CLIENTS_FILE into, so load_effective_clients() resolves it.
    """
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    clients_file = config_dir / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  {client_name}:\n"
        f"    workspace_path: /tmp/ws-staged\n"
        f"    default_branch: main\n"
        f"    pipeline:\n"
        f"      stages: [plan, impl, review, finalize]\n"
    )
