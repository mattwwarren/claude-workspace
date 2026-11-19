"""Tests for cw.outbox_producer: the shared best-effort prose enqueue (#3707)."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import pytest

from cw import outbox
from cw._lock_guard import HeldLock, held_locks
from cw.models.client import OutboxConfig
from cw.outbox import (
    MAX_MARKER_CHARS,
    MAX_PART_CHARS,
    MAX_PARTS,
    MAX_TICKET_CHARS,
    EnqueueResult,
    OutboxRecordError,
)
from cw.outbox_producer import (
    SKIP_ALREADY_POSTED,
    SKIP_BODY_TOO_LONG,
    SKIP_ENQUEUE_FAILED,
    EnqueueOutcome,
    content_marker,
    enqueue_prose,
)
from tests._doctor_helpers import make_client
from tests._shared.outbox_builders import CLIENT, entries_for

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_MARKER_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


def _enqueue(
    ticket: str = "T-1",
    marker: str = "m1",
    parts: Sequence[str] = ("body",),
    client: str = CLIENT,
) -> EnqueueOutcome:
    return enqueue_prose(client, ticket, "codex-plan", marker, parts)


def test_created_entry() -> None:
    outcome = _enqueue(parts=("one", "two"))

    ((entry, parts),) = entries_for()
    assert outcome == EnqueueOutcome(entry.id, True, None)
    assert entry.ticket == "T-1"
    assert entry.marker == "m1"
    assert entry.producer == "codex-plan"
    assert entry.redacted is False
    assert entry.operator_authored is False
    assert parts == ["one", "two"]


def test_flags_are_forwarded() -> None:
    enqueue_prose(
        CLIENT, "T-1", "codex-plan", "m1", ("b",), operator_authored=True, redacted=True
    )

    ((entry, _),) = entries_for()
    assert entry.operator_authored is True
    assert entry.redacted is True


def test_strips_leading_hash() -> None:
    _enqueue(ticket="#42")

    ((entry, _),) = entries_for()
    assert entry.ticket == "42"


def test_ticket_over_64_chars_is_enqueue_failed() -> None:
    outcome = _enqueue(ticket="T" * (MAX_TICKET_CHARS + 1))

    assert outcome == EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)
    assert entries_for() == []


def test_invalid_ticket_is_enqueue_failed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="cw.outbox_producer"):
        outcome = _enqueue(ticket="not a ticket!")

    assert outcome == EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)
    assert any(
        r.getMessage().startswith("outbox_enqueue_failed ticket=not a ticket!")
        for r in caplog.records
    )


def test_oserror_is_enqueue_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> EnqueueResult:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.outbox_producer.enqueue", boom)

    assert _enqueue() == EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)


def test_outbox_error_is_enqueue_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> EnqueueResult:
        msg = "corrupt record"
        raise OutboxRecordError(msg)

    monkeypatch.setattr("cw.outbox_producer.enqueue", boom)

    assert _enqueue() == EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)


def test_unicode_encode_failure_is_enqueue_failed() -> None:
    assert _enqueue(parts=("\ud800",)).skipped_reason == SKIP_ENQUEUE_FAILED
    assert _enqueue(marker="\ud800").skipped_reason == SKIP_ENQUEUE_FAILED


def test_overlong_marker_is_enqueue_failed() -> None:
    assert _enqueue(marker="m" * (MAX_MARKER_CHARS + 1)).skipped_reason == (
        SKIP_ENQUEUE_FAILED
    )


def test_body_over_part_cap_is_body_too_long() -> None:
    outcome = _enqueue(parts=("x" * (MAX_PART_CHARS + 1),))

    assert outcome == EnqueueOutcome(None, False, SKIP_BODY_TOO_LONG)
    assert entries_for() == []


def test_too_many_parts_is_body_too_long() -> None:
    outcome = _enqueue(parts=["p"] * (MAX_PARTS + 1))

    assert outcome == EnqueueOutcome(None, False, SKIP_BODY_TOO_LONG)
    assert entries_for() == []


def test_empty_part_is_body_too_long() -> None:
    assert _enqueue(parts=("ok", "")).skipped_reason == SKIP_BODY_TOO_LONG


def test_total_bytes_over_cap_is_body_too_long() -> None:
    # Within every per-part cap, over MAX_BODY_BYTES in sum (multi-byte chars).
    parts = ["é" * MAX_PART_CHARS] * MAX_PARTS

    assert _enqueue(parts=parts).skipped_reason == SKIP_BODY_TOO_LONG


def test_duplicate_is_already_posted() -> None:
    first = _enqueue()
    second = _enqueue()

    assert first.created is True
    assert second == EnqueueOutcome(first.entry_id, False, SKIP_ALREADY_POSTED)
    assert len(entries_for()) == 1


def test_enabled_false_client_still_enqueues(tmp_path: Path) -> None:
    client = make_client(name=CLIENT, workspace_path=tmp_path).model_copy(
        update={"outbox": OutboxConfig(enabled=False)}
    )

    outcome = _enqueue(client=client.name)

    assert outcome.created is True
    assert len(entries_for(client.name)) == 1


def test_content_marker_is_stable_and_patterned() -> None:
    text = "the plan"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    marker = content_marker("plan", text)

    assert marker == f"plan-{digest}"
    assert content_marker("plan", text) == marker
    assert content_marker("plan", "another plan") != marker
    assert content_marker("park", text) != marker
    assert _MARKER_PATTERN.match(marker)
    # The store's own marker check accepts it.
    assert _enqueue(marker=marker).created is True


def test_no_lock_held_across_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[HeldLock, ...]] = []
    real = outbox.enqueue

    def spy(client: str, **kwargs: Any) -> EnqueueResult:
        seen.append(held_locks())
        return real(client, **kwargs)

    monkeypatch.setattr("cw.outbox_producer.enqueue", spy)

    assert _enqueue().created is True
    assert seen == [()]
