"""Per-session operator mailbox: append-only JSONL with a consumed cursor.

The transport half of ``cw session send`` (GitHub #2212). Deliberately a
scoped reimplementation of :mod:`cw.events`'s three primitives -- ``flock``
lock file, ``"a"``-mode JSONL append, cursor file -- rather than a
parameterization of that module.

Why not reuse ``cw.events``: that inbox is the *fleet-wide* orchestrator bus,
one shared file and one shared lock for every session, whose producers
correlate through ``ticket_id`` (RFC 0008 / #978). Routing a per-session
mailbox through it would serialize unrelated sessions' appends behind one
lock and force every reader to filter a fleet-wide stream by session id --
the opposite of a mailbox that is greppable per session in a post-mortem.
Per PYTHON-PATTERNS.md's DRY guidance the extraction threshold is three
occurrences; this is the second, and the two differ in partitioning key.

Delivery contract, identical to ``events.read_events``: at-least-once. A
cursor that is not found in the inbox replays from the start, so consumers
must be idempotent.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.config import state_dir
from cw.exceptions import CwError
from cw.models import SessionInboxMessage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# A session id becomes a directory name, so it is validated at this boundary
# rather than trusted: the CLI takes it from an operator-supplied argument,
# and ".." or a separator would resolve the mailbox outside STATE_DIR.
# Matches the character set cw's own ids use (uuid4 hex, or a dashed name).
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")

_MESSAGE_ID_LENGTH = 8  # mirrors Session.id's uuid4().hex[:8] convention


def _validated(session_id: str) -> str:
    """Return *session_id* if it is safe as a path component, else raise."""
    if not _SAFE_SESSION_ID.match(session_id):
        msg = (
            f"Invalid session id for an inbox path: {session_id!r}."
            " Expected letters, digits, underscore or dash."
        )
        raise CwError(msg)
    return session_id


def session_inbox_dir(session_id: str) -> Path:
    """Return the directory holding one session's mailbox."""
    return state_dir() / "inboxes" / _validated(session_id)


def inbox_path(session_id: str) -> Path:
    """Return the path to a session's append-only inbox JSONL file."""
    return session_inbox_dir(session_id) / "inbox.jsonl"


def _cursor_path(session_id: str) -> Path:
    """Return the path to a session's consumed-cursor file."""
    return session_inbox_dir(session_id) / "cursor.json"


def _lock_path(session_id: str) -> Path:
    """Return the path to a session's inbox lock file."""
    return session_inbox_dir(session_id) / ".inbox.lock"


@contextlib.contextmanager
def _inbox_lock(session_id: str) -> Iterator[None]:
    """Acquire an exclusive file lock for one session's inbox."""
    session_inbox_dir(session_id).mkdir(parents=True, exist_ok=True)
    fd = _lock_path(session_id).open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def append_message(
    session_id: str, *, author: str, body: str
) -> SessionInboxMessage:
    """Append one operator message to *session_id*'s inbox and return it."""
    message = SessionInboxMessage(
        id=uuid.uuid4().hex[:_MESSAGE_ID_LENGTH],
        created_at=datetime.now(UTC),
        author=author,
        body=body,
    )
    with _inbox_lock(session_id):
        path = inbox_path(session_id)
        with path.open("a") as f:
            f.write(message.model_dump_json() + "\n")
    return message


def _parse_lines(lines: list[str]) -> list[SessionInboxMessage]:
    """Parse JSONL lines into messages, tolerating a torn trailing line.

    Interior corrupt lines re-raise so real corruption stays loud -- the same
    contract as ``events._parse_lines``. Unlike that function there is no
    unknown-enum tolerance to add: this model carries no enum field, so a
    forward-compatibility skip would have nothing to skip on.
    """
    last_nonempty_idx = max(
        (i for i, line in enumerate(lines) if line.strip()), default=-1
    )
    messages: list[SessionInboxMessage] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if i == last_nonempty_idx:
                logger.warning(
                    "skipping malformed trailing line in session inbox: %s", exc
                )
                continue
            raise
        try:
            messages.append(SessionInboxMessage.model_validate(raw))
        except ValidationError:
            if i == last_nonempty_idx:
                logger.warning("skipping malformed trailing record in session inbox")
                continue
            raise
    return messages


def read_messages(session_id: str) -> list[SessionInboxMessage]:
    """Return every message in *session_id*'s inbox, oldest first."""
    path = inbox_path(session_id)
    with _inbox_lock(session_id):
        raw_text = path.read_text() if path.exists() else ""
    if not raw_text:
        return []
    return _parse_lines(raw_text.splitlines())


def load_cursor(session_id: str) -> str | None:
    """Return the last-consumed message id for *session_id*, or None."""
    path = _cursor_path(session_id)
    if not path.exists():
        return None
    data: dict[str, str] = json.loads(path.read_text())
    return data.get("cursor")


def advance_cursor(session_id: str, message_id: str) -> None:
    """Persist *session_id*'s cursor to *message_id*.

    Writes the cursor file only; the inbox itself is append-only and is never
    rewritten by consumption. That asymmetry is what makes replay idempotent
    -- re-applying a consumed message changes this file, not the record.
    """
    path = _cursor_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {"cursor": message_id, "updated_at": datetime.now(UTC).isoformat()}
        ),
    )


def read_unconsumed(session_id: str) -> list[SessionInboxMessage]:
    """Return *session_id*'s messages after its persisted cursor.

    At-least-once: a cursor that no longer appears in the inbox replays every
    message from the start, mirroring ``events.read_events``. Consumers are
    expected to be idempotent.
    """
    messages = read_messages(session_id)
    if not messages:
        return []
    cursor = load_cursor(session_id)
    if cursor is None:
        return messages
    for i, message in enumerate(messages):
        if message.id == cursor:
            return messages[i + 1 :]
    logger.warning(
        "cursor %s not found in session %s inbox; replaying from start",
        cursor,
        session_id,
    )
    return messages
