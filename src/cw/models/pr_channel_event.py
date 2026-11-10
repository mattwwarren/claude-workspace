"""PR channel message contract, schema v1 (#3173).

Pure pydantic plus stdlib, with no ``cw`` imports. Imported by submodule path
(``cw.models.pr_channel_event``) and not re-exported from ``cw.models``; the
same convention as ``review_engine_summary.py``.

The v1 contract lives inside the existing ``notification.message`` JSON string.
The durable envelope stays ``{notification_type, message, title, offset}``.
Consumers parse ``message``, ignore unknown keys, and treat a missing
``schema_version`` as legacy v0 (new fields null).

``kind`` describes what the trigger payload reports:

- ``transition``: the trigger payload itself reports the change, and the server
  applies it through the existing overlay (or watched-PR registration).
  ``transition`` is NOT proof of the PR's present aggregate state.
  ``review_received.review_decision`` is the single submitted review's state,
  not GitHub's aggregate ``reviewDecision``. ``ci_failed`` reflects one workflow
  run, which may be stale or superseded.
- ``hint``: wake-up only. Reread before acting. Never mutates cw state. Reserved:
  zero v1 emitters.

``event_id`` is a pure function of ``(schema_version, repo.casefold(),
pr_number, event_type, source_id)``. It uses no offset, clock or body hash.
It is null whenever ``source_id`` is null.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PR_CHANNEL_SCHEMA_VERSION: Final = 1

_HEAD_SHA_RE: Final = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")


class PREventType(StrEnum):
    """Wire ``event_type`` values accepted by ``POST /pr-event``."""

    CI_FAILED = "ci_failed"
    REVIEW_RECEIVED = "review_received"
    MERGEABLE = "mergeable"
    MERGED = "merged"
    REVIEW_REQUESTED = "review_requested"


class PREventKind(StrEnum):
    """Server-derived meaning of an event. A sender cannot claim a kind."""

    TRANSITION = "transition"
    HINT = "hint"


# Plain dict so tests can ``monkeypatch.setitem`` it. Every existing type keeps
# its state mutation, so every one is ``transition``. ``hint`` has zero emitters.
EVENT_KIND_BY_TYPE: dict[PREventType, PREventKind] = {
    PREventType.CI_FAILED: PREventKind.TRANSITION,
    PREventType.REVIEW_RECEIVED: PREventKind.TRANSITION,
    PREventType.MERGEABLE: PREventKind.TRANSITION,
    PREventType.MERGED: PREventKind.TRANSITION,
    PREventType.REVIEW_REQUESTED: PREventKind.TRANSITION,
}


def kind_for(event_type: PREventType) -> PREventKind:
    """Return the server-derived kind for ``event_type``."""
    return EVENT_KIND_BY_TYPE[event_type]


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def derive_event_id(
    *,
    schema_version: int,
    repo: str,
    pr_number: int,
    event_type: str,
    source_id: str | None,
) -> str | None:
    """Return ``sha256(canonical_json(identity))`` or None when ``source_id`` is None.

    Never falls back to a body hash. The repo slug is case-folded so a case
    change in the sender does not split identity.
    """
    if source_id is None:
        return None
    canonical = _canonical_json(
        {
            "schema_version": schema_version,
            "repo": repo.casefold(),
            "pr_number": pr_number,
            "event_type": str(event_type),
            "source_id": source_id,
        }
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def normalize_source_id(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(value, drop_reason)``. Blank, whitespace or non-str becomes null.

    A valid value is kept verbatim and case-sensitive, never stripped.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "not a string"
    if raw.strip() == "":
        return None, "blank"
    return raw, None


def normalize_head_sha(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(value, drop_reason)``. Full 40- or 64-hex SHA, lowercased."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "not a string"
    lowered = raw.lower()
    if not _HEAD_SHA_RE.match(lowered):
        return None, "not a full hex SHA"
    return lowered, None


def normalize_event_time(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(value, drop_reason)``. Tz-aware ISO-8601 normalized to UTC.

    A naive timestamp, garbage or a non-str is dropped to null, never guessed.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "not a string"
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, "not ISO-8601"
    if parsed.tzinfo is None:
        return None, "naive timestamp"
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds"), None


def build_pr_event_message(
    *,
    repo: str,
    pr_number: int,
    event_type: PREventType,
    payload: dict[str, Any],
    source_id: str | None,
    head_sha: str | None,
    event_time: str | None,
    received_at: str,
) -> PREventMessage:
    """Build a fully-populated v1 message from already-normalized values."""
    kind = kind_for(event_type)
    return PREventMessage(
        schema_version=PR_CHANNEL_SCHEMA_VERSION,
        repo=repo,
        pr_number=pr_number,
        event_type=event_type,
        kind=kind,
        payload=payload,
        source_id=source_id,
        event_id=derive_event_id(
            schema_version=PR_CHANNEL_SCHEMA_VERSION,
            repo=repo,
            pr_number=pr_number,
            event_type=event_type.value,
            source_id=source_id,
        ),
        head_sha=head_sha,
        event_time=event_time,
        received_at=received_at,
    )


_V1_OPTIONAL_KEYS: Final = frozenset(
    {"kind", "source_id", "event_id", "head_sha", "event_time", "received_at"}
)
_MESSAGE_KEYS: Final = (
    frozenset(
        {
            "schema_version",
            "repo",
            "pr_number",
            "event_type",
            "payload",
        }
    )
    | _V1_OPTIONAL_KEYS
)


class PREventMessage(BaseModel):
    """The JSON object carried in ``notification.message`` (schema v1).

    Strict and ``extra="forbid"``: this is the producer's shape. Consumers
    ignore unknown keys (see ``from_wire``); they must not enforce a closed
    ``event_type`` enum or ``additionalProperties: false`` themselves.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(ge=0)
    repo: str
    pr_number: int
    event_type: PREventType
    kind: PREventKind | None = Field(
        default=None,
        description=(
            "transition: the trigger payload reports the change; NOT proof of the "
            "PR's present aggregate state. hint: wake-up only, reread before acting."
        ),
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    source_id: str | None = None
    event_id: str | None = None
    head_sha: str | None = None
    event_time: str | None = None
    received_at: str | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> PREventMessage:
        if self.schema_version >= 1:
            if self.kind is None:
                msg = "schema_version >= 1 requires kind"
                raise ValueError(msg)
            if self.received_at is None:
                msg = "schema_version >= 1 requires received_at"
                raise ValueError(msg)
        expected = derive_event_id(
            schema_version=self.schema_version,
            repo=self.repo,
            pr_number=self.pr_number,
            event_type=self.event_type.value,
            source_id=self.source_id,
        )
        if self.event_id != expected:
            msg = "event_id must equal derive_event_id(...), null iff source_id is null"
            raise ValueError(msg)
        if self.head_sha is not None and not _HEAD_SHA_RE.match(self.head_sha):
            msg = "head_sha must be a full lowercase hex SHA"
            raise ValueError(msg)
        return self

    @classmethod
    def from_wire(cls, raw: str) -> PREventMessage | None:
        """Read a ``message`` string. Returns None when it is not a v1/v0 message.

        A missing ``schema_version`` is legacy v0: the new fields read as null.
        Unknown top-level keys are dropped (consumers ignore unknown keys).
        """
        try:
            data: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if "schema_version" not in data:
            data = {
                "schema_version": 0,
                "repo": data.get("repo"),
                "pr_number": data.get("pr_number"),
                "event_type": data.get("event_type"),
                "payload": data.get("payload", {}),
            }
        else:
            data = {k: v for k, v in data.items() if k in _MESSAGE_KEYS}
        try:
            return cls.model_validate_json(json.dumps(data))
        except ValidationError:
            return None


__all__ = [
    "EVENT_KIND_BY_TYPE",
    "PR_CHANNEL_SCHEMA_VERSION",
    "PREventKind",
    "PREventMessage",
    "PREventType",
    "build_pr_event_message",
    "derive_event_id",
    "kind_for",
    "normalize_event_time",
    "normalize_head_sha",
    "normalize_source_id",
]
