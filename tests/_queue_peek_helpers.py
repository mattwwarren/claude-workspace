"""Shared test helpers for the split ``cw.queue_peek`` test suite (#2793).

Transcript writers, sentinel builders, task factories and the path-redirect
helper used by two or more of the ``test_queue_peek_*.py`` files. This module
has no ``test_`` prefix, so pytest does not collect it (same convention as
``tests/_reconcile_helpers.py``); each test module imports what it uses.

``redirect_peek_paths`` patches ``CLAUDE_PROJECTS`` / ``CW_STATE`` on
``cw.queue_peek.locate``, the module whose functions read them. The package
re-exports are snapshots: patching them there no longer reaches any reader.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from cw.models import DEFAULT_STAGE, QueueItemStatus, Stage, TicketTask
from cw.queue_peek import locate
from tests.conftest import _make_ticket_task as _cw_make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


def redirect_peek_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``locate.CLAUDE_PROJECTS`` and ``locate.CW_STATE`` to tmp_path.

    find_transcript_for_ticket's worktree_path branch resolves the project
    dir via cw._util.claude_project_dir(), which calls Path.home() directly
    rather than reading CLAUDE_PROJECTS (GH #1736). That branch needs no
    patch here: the autouse ``_isolate_home`` fixture already points HOME at
    a per-test directory for every test (#1756).
    """
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    state_file = tmp_path / "sessions.json"
    monkeypatch.setattr(locate, "CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr(locate, "CW_STATE", state_file)


def make_jsonl(
    proj_dir: Path, session_uuid: str, first_ts: str, first_text: str
) -> Path:
    """Create a minimal transcript jsonl at proj_dir/{session_uuid}.jsonl."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / f"{session_uuid}.jsonl"
    with jsonl.open("w") as f:
        f.write(json.dumps({"type": "init", "sessionId": session_uuid}) + "\n")
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": session_uuid,
                    "timestamp": first_ts,
                    "message": {"content": first_text},
                }
            )
            + "\n"
        )
    return jsonl


def write_sessions(state_file: Path, sessions: list[dict[str, Any]]) -> None:
    state_file.write_text(json.dumps({"sessions": sessions}))


def make_ticket_task(
    ticket_id: str = "T-1",
    session_id: str | None = "abc12345",
    client: str = "test",
    attempts: int = 1,
    unproductive_attempts: int = 1,
    worktree_path: Path | None = None,
    stage_high_water: Stage | None = None,
    stage: Stage = DEFAULT_STAGE,
) -> TicketTask:
    return _cw_make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        attempts=attempts,
        unproductive_attempts=unproductive_attempts,
        worktree_path=worktree_path,
        stage_high_water=stage_high_water,
        stage=stage,
    )


def make_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def sentinel_payload(
    ticket_id: str = "T-999",
    status: str = "review_pending_approval",
    stage: str = "stage3_review",
    pr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid AutoDevResult payload for sentinel tests."""
    _scope = {
        "tier": "small",
        "files": 2,
        "lines_estimate": 50,
        "lines_actual": 52,
        "forbidden_touched": False,
    }
    _health = {
        "lowest_agent_confidence": "HIGH",
        "any_incomplete_risk": False,
        "shortcuts": [],
        "recommendation": "PROCEED",
        "downgrade_applied": False,
        "fix_loop_escalated": False,
    }
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": status,
        "stage_reached": stage,
        "scope": _scope,
        "plan_source": "github_issue_existing",
        "branch": f"dev/{ticket_id.lower()}",
        "commits": ["abc"] if pr else [],
        "pr": pr,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": _health,
        "friction_highlights": [],
        "blocker": None,
        "next_actions": [],
    }


def sentinel_text(payload: dict[str, Any]) -> str:
    return f"<<<AUTO_DEV_RESULT\n{json.dumps(payload)}\nAUTO_DEV_RESULT>>>"
