"""Stop-hook auto-continuation for a headless turn that ended in text (GitHub #2348).

A headless worker whose turn ends with plain text and no ``AUTO_DEV_RESULT``
sentinel, and that the #2135 park did not claim, is sent a fixed nudge instead
of being left to stall. The Stop hook answers with a documented ``block``
decision on stdout (``{"decision": "block", "reason": ...}``, exit 0), which
makes Claude Code continue the turn with ``reason`` as the next prompt. The
continuation is capped per dev-queue stage row by :data:`MAX_STOP_CONTINUATIONS`;
after the cap the hook falls through to the existing behaviour unchanged.

:func:`maybe_continue_headless_turn` runs only after ``sessions_lock`` is
released, in cost order: payload checks (no I/O), a lock-free precheck of the
row, the transcript's tool-error gate, one short ``dev_queue_lock`` window that
persists the increment, then the audit event and the stdout block. The event
means the continuation was *decided and persisted* before the block was printed;
it does not mean Claude Code delivered or honoured it. A crash between the
persist and the print records one continuation that was never delivered.

Imports nothing from its siblings except ``_constants``. Logs under the pinned
package logger name.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, NamedTuple

import click

from cw._util import claude_project_dir
from cw.cli.stop_hook._constants import (
    _LOGGER_NAME,
    MAX_STOP_CONTINUATIONS,
    STOP_CONTINUATION_NUDGE,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import USAGE_LIMIT_RE
from cw.models import OrchestratorEventType, QueueItemStatus, SessionStatus
from cw.reconcile import find_running_task_for_session
from cw.reconcile._shared import _DEV_QUEUE_LOAD_ERRORS, _lookup_matching_task
from cw.reconcile.liveness_page import _API_ERROR_PREFIX

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session

logger = logging.getLogger(_LOGGER_NAME)

_STOP_CONTINUATION_MODE_ENV = "CW_STOP_CONTINUATION_MODE"
_STOP_CONTINUATION_SCOPES_ENV = "CW_STOP_CONTINUATION_SCOPES"
_STOP_CONTINUATION_MODES = frozenset({"off", "shadow", "enabled"})


class _ContinuationClaim(NamedTuple):
    """What :func:`_claim_stop_continuation` persisted, for the audit event."""

    number: int
    stage: str
    lane: str


def maybe_continue_headless_turn(
    session: Session,
    context: dict[str, object],
    hook_payload: dict[str, object],
    cwd_value: str,
    claude_session_id: object,
) -> None:
    """Send the continuation nudge when this sentinel-less turn qualifies.

    Called only from ``signal_stop`` on the no-sentinel bail with no background
    tasks pending, after ``sessions_lock`` is released. Every exit is silent
    and writes nothing to stdout except the single block decision. Any gate
    that fails, including an unreadable queue or a lost race on the cap, returns
    without a continuation and leaves the counter untouched (fail open).
    """
    ticket_id = context.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id:
        return
    if not _continuation_payload_qualifies(session, hook_payload):
        return
    task = find_running_task_for_session(ticket_id, session.id)
    if task is None or (task.stop_continuations or 0) >= MAX_STOP_CONTINUATIONS:
        return
    transcripts = _find_transcripts(session, cwd_value, claude_session_id)
    if not transcripts or any(_turn_ended_in_tool_error(path) for path in transcripts):
        return
    if not _continuation_rollout_allows(task, ticket_id, session.id):
        return
    claim = _claim_stop_continuation(ticket_id, session)
    if claim is None:
        return
    _record_continuation_event(session, claude_session_id, ticket_id, claim)
    _emit_block_decision()


def _continuation_rollout_mode() -> str:
    """Return the operator-controlled continuation mode, fail-safe."""
    mode = os.environ.get(_STOP_CONTINUATION_MODE_ENV, "shadow").strip().lower()
    return mode if mode in _STOP_CONTINUATION_MODES else "shadow"


def _continuation_scope_enabled(client: str, lane: str) -> bool:
    """Return whether *client* or its exact ``client/lane`` is enrolled."""
    configured = os.environ.get(_STOP_CONTINUATION_SCOPES_ENV, "")
    scopes = {scope.strip() for scope in configured.split(",") if scope.strip()}
    return client in scopes or f"{client}/{lane}" in scopes


def _continuation_rollout_allows(task: Any, ticket_id: str, session_id: str) -> bool:
    """Apply the mode and client/lane enrollment gate before claiming."""
    mode = _continuation_rollout_mode()
    if mode == "off" or not _continuation_scope_enabled(task.client, task.lane):
        return False
    if mode == "shadow":
        logger.info(
            "stop continuation shadowed: client=%s lane=%s ticket=%s session=%s",
            task.client,
            task.lane,
            ticket_id,
            session_id,
        )
        return False
    return True


def _continuation_payload_qualifies(
    session: Session, hook_payload: dict[str, object]
) -> bool:
    """The no-I/O gates on the Stop payload and the session (R5).

    A scheduled wakeup (``session_crons``) means the turn is paused, not stuck;
    blank text means there is nothing to continue from; usage-limit and API
    error text must never be nudged (defence in depth on top of ``StopFailure``
    being unwired); a session with a local liveness handle is not a Claude
    worker that should be continued.
    """
    if hook_payload.get("session_crons"):
        return False
    message = hook_payload.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return False
    if USAGE_LIMIT_RE.search(message) is not None:
        return False
    if message.startswith(_API_ERROR_PREFIX):
        return False
    return session.status is SessionStatus.ACTIVE and session.local_liveness is None


def _transcript_candidates(
    session: Session, cwd_value: str, claude_session_id: object
) -> tuple[Path, ...]:
    """Return distinct transcript candidates from both known directories.

    This is deliberately path-only; callers retain their own scan decisions.
    Both locations are considered because EnterWorktree can leave divergent
    transcripts in the hook cwd and the recorded worktree.
    """
    if not isinstance(claude_session_id, str) or not claude_session_id:
        return ()
    search_dirs: list[str] = [cwd_value]
    if session.worktree_path is not None:
        search_dirs.append(str(session.worktree_path))
    return tuple(
        claude_project_dir(search_dir) / f"{claude_session_id}.jsonl"
        for search_dir in dict.fromkeys(search_dirs)
    )


def _find_transcripts(
    session: Session, cwd_value: str, claude_session_id: object
) -> tuple[Path, ...]:
    """Return every existing transcript candidate, or none when unavailable."""
    try:
        return tuple(
            candidate
            for candidate in _transcript_candidates(
                session, cwd_value, claude_session_id
            )
            if candidate.is_file()
        )
    except OSError:
        return ()


def _turn_ended_in_tool_error(transcript: Path) -> bool:
    """Whether the final tool round trip of the transcript errored (R5).

    Walks the jsonl forward. A ``user`` record carrying ``tool_result`` blocks
    sets the flag from their ``is_error``; a ``user`` record without any
    ``tool_result`` (a plain prompt or a text-only message) clears it. True at
    end of file means the turn ended on a failed tool call, which is excluded.
    Fail closed: an unreadable file or a malformed line (including a truncated
    last line) returns True.
    """
    errored = False
    try:
        with transcript.open(encoding="utf-8") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                record = json.loads(raw_line)
                if not isinstance(record, dict) or record.get("type") != "user":
                    continue
                errored = _user_record_has_tool_error(record)
    except (OSError, ValueError):
        return True
    return errored


def _user_record_has_tool_error(record: dict[str, Any]) -> bool:
    """Whether a ``user`` record's ``tool_result`` blocks report ``is_error``.

    False when the record carries no ``tool_result`` block at all, which is the
    reset that ends a failed round trip.
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    results = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    return any(block.get("is_error") is True for block in results)


def _claim_stop_continuation(
    ticket_id: str, session: Session
) -> _ContinuationClaim | None:
    """Increment the row's counter under one ``dev_queue_lock`` window.

    Re-matches and re-validates the row under the lock: it must still be
    RUNNING for this session and still below the cap, since another Stop or a
    requeue may have moved it since the lock-free precheck. Taken unbounded,
    alone, with nothing else held; the save is the only write. A queue load
    error fails open with one WARNING and no save.
    """
    with dev_queue_lock():
        try:
            store = load_dev_queue()
        except _DEV_QUEUE_LOAD_ERRORS as exc:
            logger.warning(
                "stop continuation skipped: dev queue unreadable for ticket=%s (%s)",
                ticket_id,
                exc,
            )
            return None
        lookup = _lookup_matching_task(store, ticket_id, session.id)
        task = lookup.target
        if task is None or lookup.target_status is not QueueItemStatus.RUNNING:
            return None
        previous = task.stop_continuations or 0
        if previous >= MAX_STOP_CONTINUATIONS:
            return None
        task.stop_continuations = previous + 1
        save_dev_queue(store)
        return _ContinuationClaim(
            number=previous + 1, stage=task.stage.value, lane=task.lane
        )


def _record_continuation_event(
    session: Session,
    claude_session_id: object,
    ticket_id: str,
    claim: _ContinuationClaim,
) -> None:
    """Emit ``session.stop_continued`` after the queue lock is released.

    Identity comes from the hook payload's ``session_id`` (the caller's
    ``claude_session_id``), not ``session.claude_session_id``, which is only
    conditionally populated for an ACTIVE session.
    """
    record_event(
        OrchestratorEventType.SESSION_STOP_CONTINUED,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": claude_session_id,
            "stage": claim.stage,
            "lane": claim.lane,
            "continuation": claim.number,
            "cap": MAX_STOP_CONTINUATIONS,
        },
        correlation_id=ticket_id,
    )


def _emit_block_decision() -> None:
    """Print the documented Stop-hook ``block`` decision as the only stdout.

    The contract is Claude Code's hooks reference, Stop section: a JSON object
    with ``decision: "block"`` and a required ``reason`` continues the turn.
    Exit status stays 0.
    """
    click.echo(json.dumps({"decision": "block", "reason": STOP_CONTINUATION_NUDGE}))
