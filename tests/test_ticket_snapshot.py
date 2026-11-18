"""Ticket snapshot store, read checks, caps, mappers and record path (RFC 0019 T1)."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.ticket_snapshot import (
    MAX_COMMENT_BODY_BYTES,
    MAX_COMMENTS,
    SnapshotComment,
    SnapshotRefusedError,
    SnapshotUntrustedError,
    TicketSnapshot,
    _fetch,
    normalize_ticket_id,
    read,
    record_snapshot,
    snapshot_from_github,
    snapshot_from_stdin,
    snapshot_path,
    write_snapshot,
)
from cw.ticket_snapshot._models import canonical_json, digest_of
from tests._shared.ticket_context import make_snapshot, observed_issue_envelope
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    events_of,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed(snapshot: TicketSnapshot) -> Any:
    digest = write_snapshot(snapshot)
    task = make_task(snapshot_digest=digest, snapshot_fetched_at=snapshot.fetched_at)
    seed_queue([task])
    return task


def test_digest_is_sha256_of_exact_canonical_bytes() -> None:
    snapshot = make_snapshot()
    text = canonical_json(snapshot)
    assert digest_of(text) == hashlib.sha256(text.encode("ascii")).hexdigest()
    digest = write_snapshot(snapshot)
    assert snapshot_path(ACME, digest).name == f"{digest}.json"
    assert snapshot_path(ACME, digest).read_text(encoding="ascii") == text


def test_non_ascii_is_escaped_so_digest_is_ascii_stable() -> None:
    snapshot = make_snapshot(title="café")
    text = canonical_json(snapshot)
    assert text.isascii()
    assert "caf\\u00e9" in text


def test_fetched_at_is_in_hashed_content_so_refresh_changes_digest() -> None:
    first = write_snapshot(make_snapshot())
    later = make_snapshot(fetched_at=make_snapshot().fetched_at.replace(second=5))
    assert write_snapshot(later) != first


def test_same_snapshot_write_is_idempotent() -> None:
    snapshot = make_snapshot()
    assert write_snapshot(snapshot) == write_snapshot(snapshot)


def test_layout_is_client_dir_and_modes() -> None:
    digest = write_snapshot(make_snapshot())
    path = snapshot_path(ACME, digest)
    assert path.parent.name == ACME
    assert path.parent.parent.name == "ticket_snapshots"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_ticket_id_never_in_path() -> None:
    digest = write_snapshot(make_snapshot())
    assert ACME_TICKET not in str(snapshot_path(ACME, digest))


@pytest.mark.parametrize("bad", ["..", "a/b", "", "-x"])
def test_bad_client_name_refused(bad: str) -> None:
    with pytest.raises(SnapshotRefusedError):
        snapshot_path(bad, "a" * 64)


def test_bad_digest_refused() -> None:
    with pytest.raises(SnapshotRefusedError):
        snapshot_path(ACME, "not-a-digest")


def test_ticket_id_normalized_once() -> None:
    assert normalize_ticket_id("#12") == "12"
    assert normalize_ticket_id("12") == "12"
    with pytest.raises(SnapshotRefusedError):
        normalize_ticket_id("#../x")


def test_extra_fields_forbidden_on_model() -> None:
    with pytest.raises(ValidationError):
        TicketSnapshot.model_validate(
            {**make_snapshot().model_dump(mode="json"), "extra": 1}
        )


# --- read(task): every R14 branch ---


def test_read_returns_none_when_row_digest_none() -> None:
    assert read(make_task()) is None


def test_read_returns_stored_snapshot() -> None:
    task = _seed(make_snapshot())
    assert read(task) == make_snapshot()


def test_read_store_tamper_raises_mismatch(tmp_path: Path) -> None:
    task = _seed(make_snapshot())
    path = snapshot_path(ACME, task.snapshot_digest or "")
    path.write_text(
        path.read_text(encoding="ascii").replace("Authored", "Edited"), encoding="ascii"
    )
    with pytest.raises(SnapshotUntrustedError) as info:
        read(task)
    assert info.value.reason == "mismatch"


def test_read_missing_store_file_raises_missing() -> None:
    task = make_task(snapshot_digest="a" * 64)
    with pytest.raises(SnapshotUntrustedError) as info:
        read(task)
    assert info.value.reason == "missing"


def test_read_invalid_json_with_matching_hash_raises_invalid() -> None:
    raw = b"{not json"
    digest = hashlib.sha256(raw).hexdigest()
    path = snapshot_path(ACME, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(snapshot_digest=digest))
    assert info.value.reason == "invalid"


def test_read_newer_schema_version_raises_too_new_and_file_untouched() -> None:
    raw = json.dumps({"schema_version": 99}).encode("ascii")
    digest = hashlib.sha256(raw).hexdigest()
    path = snapshot_path(ACME, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    mtime = path.stat().st_mtime_ns
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(snapshot_digest=digest))
    assert info.value.reason == "too_new"
    assert path.read_bytes() == raw
    assert path.stat().st_mtime_ns == mtime


def test_read_ticket_id_mismatch_raises_mismatch() -> None:
    digest = write_snapshot(make_snapshot())
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(ticket_id="OTHER-9", snapshot_digest=digest))
    assert info.value.reason == "mismatch"


def test_read_row_with_non_hex_digest_raises_invalid() -> None:
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(snapshot_digest="XYZ"))
    assert info.value.reason == "invalid"


def test_untrusted_error_chains_cause() -> None:
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(snapshot_digest="b" * 64))
    assert info.value.__cause__ is not None


# --- caps ---


def test_comment_body_over_cap_refused() -> None:
    big = "x" * (MAX_COMMENT_BODY_BYTES + 1)
    comment = make_snapshot().comments[0].model_copy(update={"body": big})
    with pytest.raises(SnapshotRefusedError):
        write_snapshot(make_snapshot(comments=[comment]))


def test_comment_body_at_cap_accepted() -> None:
    body = "x" * MAX_COMMENT_BODY_BYTES
    comment = make_snapshot().comments[0].model_copy(update={"body": body})
    write_snapshot(make_snapshot(comments=[comment]))


def test_comment_count_over_cap_refused() -> None:
    template = make_snapshot().comments[0]
    many = [template.model_copy(update={"id": str(i)}) for i in range(MAX_COMMENTS + 1)]
    with pytest.raises(SnapshotRefusedError):
        write_snapshot(make_snapshot(comments=many))


def test_total_over_cap_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.ticket_snapshot._models.MAX_SNAPSHOT_BYTES", 10)
    with pytest.raises(SnapshotRefusedError):
        write_snapshot(make_snapshot())


# --- mappers ---


def test_github_mapping_keeps_raw_values_and_computes_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    envelope = observed_issue_envelope()
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: envelope)
    snapshot = snapshot_from_github("#2633", ACME, tmp_path)
    assert snapshot.ticket_id == "2633"
    first = snapshot.comments[0]
    raw = envelope["comments"][0]
    assert first.id == raw["id"]
    assert first.author == "operator-login"
    assert first.created_at == raw["createdAt"]
    assert first.body == raw["body"]
    assert first.minimized is raw["isMinimized"]


def test_github_fetch_failure_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: None)
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_github("2633", ACME, tmp_path)


def test_github_comment_missing_id_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    envelope = observed_issue_envelope()
    del envelope["comments"][0]["id"]
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: envelope)
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_github("2633", ACME, tmp_path)


def _doc(**overrides: Any) -> str:
    doc: dict[str, Any] = {
        "ticket_url": None,
        "title": "T",
        "body": "B",
        "comments": [
            {
                "id": "c1",
                "author_id": None,
                "author": "someone",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "hello",
            }
        ],
    }
    doc.update(overrides)
    return json.dumps(doc)


def test_stdin_document_valid_and_computes_agent_authored() -> None:
    snapshot = snapshot_from_stdin(_doc(), "#7", ACME)
    assert snapshot.ticket_id == "7"
    assert snapshot.comments[0].agent_authored is False
    assert snapshot.comments[0].author_id is None


def test_stdin_caller_supplied_agent_authored_refused() -> None:
    doc = json.loads(_doc())
    doc["comments"][0]["agent_authored"] = True
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_stdin(json.dumps(doc), "7", ACME)


def test_stdin_missing_created_at_refused() -> None:
    doc = json.loads(_doc())
    del doc["comments"][0]["created_at"]
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_stdin(json.dumps(doc), "7", ACME)


def test_stdin_not_json_refused() -> None:
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_stdin("{nope", "7", ACME)


def test_stdin_refusal_names_fields_never_values() -> None:
    doc = json.loads(_doc(title=12345))
    with pytest.raises(SnapshotRefusedError) as info:
        snapshot_from_stdin(json.dumps(doc), "7", ACME)
    assert "12345" not in str(info.value)
    assert "title" in str(info.value)


# --- record path ---


def test_record_stamps_row_and_records_event() -> None:
    seed_queue([make_task()])
    record = record_snapshot(make_snapshot(ticket_id=ACME_TICKET))
    row = queue_row()
    assert row.snapshot_digest == record.snapshot_digest
    assert row.snapshot_fetched_at is not None
    assert record.row_updated is True
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RECORDED)
    assert set(event.payload) == {
        "ticket_id",
        "client",
        "snapshot_digest",
        "previous_digest",
        "refresh",
        "comments",
        "row_updated",
    }
    assert event.correlation_id == ACME_TICKET
    assert "Authored test ticket body" not in repr(event.payload)


def test_record_no_row_writes_store_and_event_row_updated_false() -> None:
    record = record_snapshot(make_snapshot(ticket_id=ACME_TICKET))
    assert record.row_updated is False
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RECORDED)
    assert event.payload["row_updated"] is False


def test_refresh_sets_previous_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_snapshot(ticket_id=ACME_TICKET)
    seed_queue([make_task()])
    record_snapshot(first)
    later = make_snapshot(ticket_id=ACME_TICKET, title="changed")
    record = record_snapshot(later, refresh=True)
    assert record.previous_digest == write_snapshot(first)
    assert record.refresh is True


def test_comments_count_in_event_is_snapshot_length() -> None:
    seed_queue([make_task()])
    record_snapshot(make_snapshot(ticket_id=ACME_TICKET))
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RECORDED)
    assert event.payload["comments"] == 4


def test_snapshot_error_is_cw_error() -> None:
    assert issubclass(SnapshotRefusedError, CwError)
    assert issubclass(SnapshotUntrustedError, CwError)


def test_comment_model_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        SnapshotComment.model_validate(
            {
                "id": "1",
                "author_id": None,
                "author": None,
                "created_at": "x",
                "body": "b",
                "agent_authored": False,
                "minimized": False,
                "extra": 1,
            }
        )
