"""PR event responder: decision table that reacts to PR events by spawning sessions."""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cw.atomic import atomic_write_text
from cw.cmux import get_cmux_adapter
from cw.config import load_clients, load_state, save_state, state_dir
from cw.events import advance_cursor, read_events
from cw.models import (
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.cmux import CmuxAdapter
    from cw.models import ClientConfig, CwState

logger = logging.getLogger(__name__)

_CONSUMER = "pr_responder"

_PR_EVENT_TYPES = [
    OrchestratorEventType.PR_CI_FAILED,
    OrchestratorEventType.PR_REVIEW_RECEIVED,
    OrchestratorEventType.PR_MERGEABLE,
    OrchestratorEventType.PR_MERGED,
]

_DISPATCH_FILE_NAME = "pr_dispatch.json"
_LOG_ONLY_TYPES = frozenset(
    {OrchestratorEventType.PR_MERGEABLE, OrchestratorEventType.PR_MERGED}
)


class PRDispatchRecord(BaseModel):
    """Tracks in-flight PR response sessions."""

    # key: "repo#pr_number|role"  (role = "fix-ci" or "address-review")
    active: dict[str, str] = Field(default_factory=dict)


def load_dispatch_record() -> PRDispatchRecord:
    """Load PRDispatchRecord from the state directory, or return an empty one."""
    path = state_dir() / _DISPATCH_FILE_NAME
    if not path.exists():
        return PRDispatchRecord()
    return PRDispatchRecord.model_validate_json(path.read_text())


def save_dispatch_record(record: PRDispatchRecord) -> None:
    """Persist PRDispatchRecord to the state directory atomically."""
    state_dir().mkdir(parents=True, exist_ok=True)
    path = state_dir() / _DISPATCH_FILE_NAME
    atomic_write_text(path, record.model_dump_json(indent=2))


def _is_session_active(session_id: str, state: CwState) -> bool:
    """Return True if session_id exists and is not COMPLETED."""
    for session in state.sessions:
        if session.id == session_id and session.status != SessionStatus.COMPLETED:
            return True
    return False


def _spawn_session(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt_file: Path,
    surface: str,
    label: str,
    adapter: CmuxAdapter,
) -> str:
    """Spawn a daemon-managed Claude session and persist it to state.

    Mirrors the logic in cli._spawn_create_impl to avoid a circular import
    (daemon -> pr_responder -> cli -> daemon).

    Returns:
        The new session's ID.
    """
    prompt_content = prompt_file.read_text()
    workspace = client.cmux_workspace or client.name
    # ``claude -w`` takes a worktree name, not a path — cd into the worktree
    # instead. See cw.spawn.spawn_create_impl for the canonical pattern.
    cwd = shlex.quote(str(worktree))
    command = f"cd {cwd} && claude --print {prompt_content!r}"
    surface_ref = adapter.spawn(workspace, command, surface)

    sess = Session(
        name=f"{client.name}/{label}",
        client=client.name,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        workspace_path=client.workspace_path,
        worktree_path=worktree,
        surface_ref=surface_ref,
    )

    state = load_state()
    state.sessions.append(sess)
    save_state(state)
    return sess.id


def respond_to_pr_events(adapter: CmuxAdapter | None = None) -> int:
    """Read unprocessed PR events and spawn sessions per the decision table.

    Decision table:
        pr.ci_failed        → spawn /fix-ci <pr_number>          (1 concurrent per PR)
        pr.review_received  → spawn /address-review <pr_number>  (1 concurrent per PR)
        pr.mergeable        → log only, no spawn
        pr.merged           → log only, no spawn

    Args:
        adapter: CmuxAdapter to use. Defaults to get_cmux_adapter().

    Returns:
        Count of sessions spawned.
    """
    events = read_events(consumer=_CONSUMER, event_types=_PR_EVENT_TYPES)
    if not events:
        return 0

    resolved_adapter = adapter or get_cmux_adapter()
    clients = load_clients()
    state = load_state()
    dispatch_record = load_dispatch_record()
    spawned_count = 0

    for event in events:
        payload = event.payload
        event_type = event.type

        # Log-only events — advance cursor and continue
        if event_type in _LOG_ONLY_TYPES:
            logger.info("PR event %s (log-only): %s", event_type, payload)
            advance_cursor(_CONSUMER, event.id)
            continue

        client_name: str = str(payload.get("client", ""))
        repo: str = str(payload.get("repo", ""))
        pr_number: int = int(payload.get("pr_number", 0))
        branch: str = str(payload.get("branch", str(pr_number)))

        if event_type == OrchestratorEventType.PR_CI_FAILED:
            role = "fix-ci"
            skill_cmd = f"/fix-ci {pr_number}"
        else:
            # PR_REVIEW_RECEIVED
            role = "address-review"
            skill_cmd = f"/address-review {pr_number}"

        dispatch_key = f"{repo}#{pr_number}|{role}"

        # Throttle: skip if an active session already handles this dispatch key
        existing_session_id = dispatch_record.active.get(dispatch_key)
        if existing_session_id is not None and _is_session_active(
            existing_session_id, state
        ):
            throttle_msg = (
                f"Throttled {role} for {dispatch_key}"
                f" (session {existing_session_id} still active)"
            )
            logger.info(throttle_msg)
            advance_cursor(_CONSUMER, event.id)
            continue

        # Resolve client config — skip gracefully if unknown
        if client_name not in clients:
            unknown_client_msg = f"Unknown client {client_name!r} in PR event, skipping"
            logger.warning(unknown_client_msg)
            advance_cursor(_CONSUMER, event.id)
            continue

        client_cfg = clients[client_name]

        # Resolve worktree path; ensure directory exists so prompt file can be written
        worktree_path = worktree_path_for(client_cfg, branch)
        worktree_path.mkdir(parents=True, exist_ok=True)

        # Write prompt file
        prompt_file = worktree_path / ".cw-pr-prompt.txt"
        prompt_file.write_text(skill_cmd)

        # Spawn session
        session_label = f"{role}/{pr_number}"
        session_id = _spawn_session(
            client=client_cfg,
            worktree=worktree_path,
            prompt_file=prompt_file,
            surface="split",
            label=session_label,
            adapter=resolved_adapter,
        )

        # Record dispatch and persist
        dispatch_record.active[dispatch_key] = session_id
        save_dispatch_record(dispatch_record)

        # Reload state so subsequent throttle checks see the new session
        state = load_state()

        advance_cursor(_CONSUMER, event.id)
        spawned_count += 1

        spawned_msg = f"Spawned session {session_id} for {dispatch_key} ({role})"
        logger.info(spawned_msg)

    return spawned_count


def clear_completed_pr_sessions(state: CwState) -> None:
    """Remove PRDispatchRecord entries whose sessions are completed.

    Args:
        state: Current CwState used to determine completed session IDs.
    """
    dispatch_record = load_dispatch_record()
    completed_ids = {
        s.id for s in state.sessions if s.status == SessionStatus.COMPLETED
    }
    dispatch_record.active = {
        key: sid
        for key, sid in dispatch_record.active.items()
        if sid not in completed_ids
    }
    save_dispatch_record(dispatch_record)
