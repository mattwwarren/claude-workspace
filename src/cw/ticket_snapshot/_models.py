"""Models, caps, digest and failure vocabulary for the ticket snapshot store.

Threat model (RFC 0019 T1, #3711). The store protects against edits to a
worktree's ``.cw/context.json``: a snapshot file is content-addressed, so its
bytes must hash to the digest the queue row carries, or the read refuses it.
An edit to the store file itself is detected for the same reason. A process
with write access to the whole state directory could rewrite the file and the
row together, exactly as with every other state file cw keeps; the store does
not defend against that and does not claim to.

No trust is evaluated here. Each comment is recorded with the raw values the
tracker returned plus two computed booleans (``agent_authored`` from
``cw.gh.is_agent_authored``, ``minimized`` from the tracker). Whether a comment
may direct anything is a later decision (RFC 0019 T2).
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from cw.exceptions import CwError
from cw.models import _SAFE_TICKET_ID

SNAPSHOT_SCHEMA_VERSION = 1
MAX_COMMENT_BODY_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_COMMENTS = 1000

_CLIENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TICKET_HASH_PREFIX = "#"
_FETCHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SnapshotFailureCause = Literal["missing", "mismatch", "invalid", "too_new"]
RenderFailureCause = Literal[
    "missing", "mismatch", "invalid", "too_new", "render_error"
]


class SnapshotError(CwError):
    """Base for every snapshot-store failure."""


class SnapshotRefusedError(SnapshotError):
    """Input refused before anything is written. Messages never carry input values."""


class SnapshotUntrustedError(SnapshotError):
    """A stored snapshot failed a read check; ``reason`` names which one."""

    def __init__(self, message: str, *, reason: SnapshotFailureCause) -> None:
        super().__init__(message)
        self.reason = reason


class SnapshotComment(BaseModel):
    """One tracker comment, recorded verbatim plus the two computed booleans."""

    model_config = ConfigDict(extra="forbid")

    id: str
    author_id: str | None
    author: str | None
    created_at: str
    body: str
    agent_authored: bool
    minimized: bool


class TicketSnapshot(BaseModel):
    """The stored ticket snapshot. Its own schema_version, refused when newer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    ticket_id: str
    client: str
    ticket_url: str | None
    fetched_at: datetime
    title: str
    body: str
    comments: list[SnapshotComment]

    @field_validator("ticket_id")
    @classmethod
    def _ticket_id_safe(cls, value: str) -> str:
        if not _SAFE_TICKET_ID.match(value):
            msg = "ticket_id is not a safe identifier"
            raise ValueError(msg)
        return value

    @field_validator("client")
    @classmethod
    def _client_safe(cls, value: str) -> str:
        if not _CLIENT_RE.match(value):
            msg = "client is not a safe name"
            raise ValueError(msg)
        return value

    @field_validator("fetched_at")
    @classmethod
    def _fetched_at_utc_seconds(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "fetched_at must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(microsecond=0)

    @field_serializer("fetched_at")
    def _serialize_fetched_at(self, value: datetime) -> str:
        return value.strftime(_FETCHED_AT_FORMAT)


def check_client(client: str) -> str:
    """Return *client* if it is a safe directory name, else refuse."""
    if not _CLIENT_RE.match(client):
        msg = "client name is not a safe directory name"
        raise SnapshotRefusedError(msg)
    return client


def check_digest(digest: str) -> str:
    """Return *digest* if it is a lowercase sha256 hex digest, else refuse."""
    if not _DIGEST_RE.match(digest):
        msg = "snapshot digest is not a sha256 hex digest"
        raise SnapshotRefusedError(msg)
    return digest


def normalize_ticket_id(raw: str) -> str:
    """Strip one leading ``#`` and check the id; the one place this happens.

    Called by both snapshot constructors before the store write and the event,
    so ``#12`` and ``12`` can never be stored under two names.
    """
    stripped = raw.removeprefix(_TICKET_HASH_PREFIX)
    if not _SAFE_TICKET_ID.match(stripped):
        msg = "ticket id is not a safe identifier"
        raise SnapshotRefusedError(msg)
    return stripped


def canonical_json(snapshot: TicketSnapshot) -> str:
    """The exact bytes the store writes and the digest covers."""
    payload: dict[str, Any] = snapshot.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_of(text: str) -> str:
    """sha256 hex digest of the canonical text (ASCII by construction)."""
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def enforce_caps(snapshot: TicketSnapshot) -> None:
    """Refuse, never truncate, a snapshot over a size or count cap."""
    if len(snapshot.comments) > MAX_COMMENTS:
        msg = f"snapshot has more than {MAX_COMMENTS} comments; refused, not truncated"
        raise SnapshotRefusedError(msg)
    for index, comment in enumerate(snapshot.comments, start=1):
        if len(comment.body.encode("utf-8")) > MAX_COMMENT_BODY_BYTES:
            msg = (
                f"comment {index} body exceeds {MAX_COMMENT_BODY_BYTES} bytes;"
                " refused, not truncated"
            )
            raise SnapshotRefusedError(msg)
    if len(canonical_json(snapshot).encode("ascii")) > MAX_SNAPSHOT_BYTES:
        msg = f"snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes; refused, not truncated"
        raise SnapshotRefusedError(msg)
