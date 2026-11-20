"""Ticket snapshot store, read checks, caps, mappers and record path (RFC 0019 T1)."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
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
    _record,
    flag_author_conflicts,
    normalize_ticket_id,
    read,
    record_snapshot,
    snapshot_from_github,
    snapshot_from_stdin,
    snapshot_path,
    write_snapshot,
)
from cw.ticket_snapshot._models import (
    SNAPSHOT_SCHEMA_VERSION,
    canonical_json,
    digest_of,
    digest_or_none,
)
from cw.ticket_snapshot._render import render_context
from tests._shared.ticket_context import (
    make_comment,
    make_snapshot,
    make_snapshot_from,
    observed_issue_envelope,
    seeded_row,
)
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
    task = seeded_row(make_snapshot())
    assert read(task) == make_snapshot()


def test_read_store_tamper_raises_mismatch(tmp_path: Path) -> None:
    task = seeded_row(make_snapshot())
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


# --- RFC 0019 T1 review round 1 (items 27-31) ---


def test_fetched_at_keeps_microseconds_so_same_second_refresh_differs() -> None:
    base = make_snapshot()
    first = base.model_copy(
        update={"fetched_at": base.fetched_at.replace(microsecond=1)}
    )
    second = base.model_copy(
        update={"fetched_at": base.fetched_at.replace(microsecond=2)}
    )
    assert ".000001Z" in canonical_json(first)
    assert write_snapshot(first) != write_snapshot(second)


def test_rendered_fetched_at_keeps_microseconds() -> None:
    snapshot = make_snapshot(
        fetched_at=make_snapshot().fetched_at.replace(microsecond=123456)
    )
    rendered = render_context(snapshot, "a" * 64, materialized_by_session=None)
    assert rendered["snapshot_fetched_at"] == "2026-11-18T12:00:00.123456Z"


def test_read_non_utf8_bytes_with_matching_hash_raises_invalid() -> None:
    raw = b'{"x": "\xff"}'
    digest = hashlib.sha256(raw).hexdigest()
    path = snapshot_path(ACME, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(SnapshotUntrustedError) as info:
        read(make_task(snapshot_digest=digest))
    assert info.value.reason == "invalid"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("a" * 64, "a" * 64),
        ("A" * 64, None),
        ("a" * 63, None),
        ("x" * 64, None),
    ],
)
def test_digest_or_none_is_the_one_shape_check(
    value: str | None, expected: str | None
) -> None:
    assert digest_or_none(value) == expected


def _gh_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "title": "T",
        "body": "B",
        "url": "https://example.test/3711",
        "comments": [],
    }
    doc.update(overrides)
    return doc


def _gh_raw_comment(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "IC_1",
        "body": "hello",
        "createdAt": "2026-11-18T20:00:00Z",
        "author": {"login": "octo"},
        "isMinimized": False,
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    "doc",
    [
        _gh_doc(title=None),
        _gh_doc(body=3),
        _gh_doc(url=["x"]),
        _gh_doc(comments="nope"),
    ],
)
def test_github_malformed_top_level_refused(
    doc: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: doc)
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_github("3711", ACME, tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-dict",
        _gh_raw_comment(author="octo"),
        _gh_raw_comment(author={"login": 7}),
        _gh_raw_comment(isMinimized="yes"),
    ],
)
def test_github_malformed_comment_refused_never_coerced(
    raw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _fetch, "fetch_issue_snapshot", lambda *_a, **_k: _gh_doc(comments=[raw])
    )
    with pytest.raises(SnapshotRefusedError):
        snapshot_from_github("3711", ACME, tmp_path)


def test_github_null_author_and_null_url_recorded_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _gh_doc(url=None, comments=[_gh_raw_comment(author=None)])
    monkeypatch.setattr(_fetch, "fetch_issue_snapshot", lambda *_a, **_k: doc)
    snapshot = snapshot_from_github("#3711", ACME, tmp_path)
    assert snapshot.ticket_url is None
    assert snapshot.ticket_id == "3711"
    (comment,) = snapshot.comments
    assert comment.author_id is None
    assert comment.author is None
    assert comment.minimized is False


# --- RFC 0019 T2 (#3712): author-conflict flagging ---


def _snap(*comments: SnapshotComment) -> TicketSnapshot:
    return make_snapshot_from(comments, ticket_id=ACME_TICKET)


def _flags(snapshot: TicketSnapshot) -> dict[str, bool]:
    return {c.id: c.author_conflict for c in snapshot.comments}


def test_schema_version_is_still_one() -> None:
    assert SNAPSHOT_SCHEMA_VERSION == 1


def test_old_schema_json_without_author_conflict_validates_with_digest_intact() -> None:
    payload = make_snapshot().model_dump(mode="json")
    for comment in payload["comments"]:
        del comment["author_conflict"]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    digest = hashlib.sha256(raw).hexdigest()
    path = snapshot_path(ACME, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    snapshot = read(make_task(snapshot_digest=digest))
    assert snapshot is not None
    assert [c.author_conflict for c in snapshot.comments] == [False] * 4
    assert path.read_bytes() == raw


def test_flag_previous_different_author_id_sets_conflict() -> None:
    previous = _snap(make_comment("c1", author_id="alice"))
    current = _snap(make_comment("c1", author_id="mallory"))
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": True}
    assert newly == 1
    assert _flags(current) == {"c1": False}


def test_flag_casefold_equal_author_id_is_not_a_conflict() -> None:
    previous = _snap(make_comment("c1", author_id="Alice"))
    current = _snap(make_comment("c1", author_id="aLICE"))
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": False}
    assert newly == 0


def test_flag_twice_in_one_document_flags_both_comments() -> None:
    current = _snap(
        make_comment("c1", author_id="alice"),
        make_comment("c1", author_id="mallory"),
        make_comment("c2", author_id="alice"),
    )
    flagged, newly = flag_author_conflicts(current, None)
    assert [c.author_conflict for c in flagged.comments] == [True, True, False]
    assert newly == 2


def test_flag_previous_true_is_carried_forward_without_a_new_count() -> None:
    previous = _snap(make_comment("c1", author_id="alice", author_conflict=True))
    current = _snap(make_comment("c1", author_id="alice"))
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": True}
    assert newly == 0


def test_flag_previous_true_with_a_fresh_contradiction_is_not_counted_again() -> None:
    previous = _snap(make_comment("c1", author_id="alice", author_conflict=True))
    current = _snap(make_comment("c1", author_id="mallory"))
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": True}
    assert newly == 0


def test_flag_already_flagged_input_is_not_counted_again() -> None:
    current = _snap(
        make_comment("c1", author_id="alice", author_conflict=True),
    )
    flagged, newly = flag_author_conflicts(current, None)
    assert _flags(flagged) == {"c1": True}
    assert newly == 0


@pytest.mark.parametrize(
    ("before", "after"),
    [(None, "alice"), ("alice", None), (None, None)],
)
def test_flag_null_to_value_is_not_a_conflict(
    before: str | None, after: str | None
) -> None:
    previous = _snap(make_comment("c1", author_id=before))
    current = _snap(make_comment("c1", author_id=after))
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": False}
    assert newly == 0


def test_flag_only_counts_comments_that_were_not_already_flagged() -> None:
    previous = _snap(
        make_comment("c1", author_id="alice", author_conflict=True),
        make_comment("c2", author_id="bob"),
    )
    current = _snap(
        make_comment("c1", author_id="alice"),
        make_comment("c2", author_id="eve"),
    )
    flagged, newly = flag_author_conflicts(current, previous)
    assert _flags(flagged) == {"c1": True, "c2": True}
    assert newly == 1


def test_record_without_detect_never_flags_even_on_a_contradiction() -> None:
    seeded_row(_snap(make_comment("c1", author_id="alice")))
    record_snapshot(_snap(make_comment("c1", author_id="mallory")))
    row = queue_row()
    stored = read(row)
    assert stored is not None
    assert _flags(stored) == {"c1": False}
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []


def test_record_with_detect_flags_stores_and_emits_one_event() -> None:
    seeded_row(_snap(make_comment("c1", author_id="alice")))
    record = record_snapshot(
        _snap(make_comment("c1", author_id="mallory")), detect_author_conflicts=True
    )
    stored = read(queue_row())
    assert stored is not None
    assert _flags(stored) == {"c1": True}
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT)
    assert event.payload == {
        "ticket_id": ACME_TICKET,
        "client": ACME,
        "snapshot_digest": record.snapshot_digest,
        "comments": 1,
    }
    assert event.correlation_id == ACME_TICKET
    assert "operator comment" not in repr(event.payload)


def test_record_with_detect_and_no_conflict_emits_no_event() -> None:
    seeded_row(_snap(make_comment("c1", author_id="alice")))
    record_snapshot(
        _snap(make_comment("c1", author_id="alice"), make_comment("c2")),
        detect_author_conflicts=True,
    )
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []


def test_record_carried_flag_stays_set_and_emits_no_second_event() -> None:
    seeded_row(_snap(make_comment("c1", author_id="alice")))
    record_snapshot(
        _snap(make_comment("c1", author_id="mallory")), detect_author_conflicts=True
    )
    record_snapshot(
        _snap(make_comment("c1", author_id="mallory")), detect_author_conflicts=True
    )
    stored = read(queue_row())
    assert stored is not None
    assert _flags(stored) == {"c1": True}
    assert len(events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT)) == 1


def test_record_with_no_row_compares_nothing() -> None:
    record_snapshot(
        _snap(make_comment("c1", author_id="alice")), detect_author_conflicts=True
    )
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []


def test_record_with_untrusted_previous_compares_nothing_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue([make_task(snapshot_digest="c" * 64)])
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        record_snapshot(
            _snap(make_comment("c1", author_id="alice")), detect_author_conflicts=True
        )
    assert "author conflicts not compared" in caplog.text
    assert "reason=missing" in caplog.text
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT) == []
    stored = read(queue_row())
    assert stored is not None
    assert _flags(stored) == {"c1": False}


def test_record_with_unreadable_previous_still_records_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seeded_row(_snap(make_comment("c1", author_id="alice")))

    def _unreadable(_task: object) -> TicketSnapshot:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(_record, "read", _unreadable)
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        record = record_snapshot(
            _snap(make_comment("c1", author_id="mallory")),
            detect_author_conflicts=True,
        )

    assert record.row_updated is True
    assert "PermissionError" in caplog.text
    assert "permission denied" not in caplog.text


def test_conflict_event_failure_does_not_break_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = _record.record_event

    def _flaky(event_type: OrchestratorEventType, *args: Any, **kwargs: Any) -> Any:
        if event_type is OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT:
            msg = "disk gone"
            raise OSError(msg)
        return real(event_type, *args, **kwargs)

    monkeypatch.setattr(_record, "record_event", _flaky)
    seeded_row(_snap(make_comment("c1", author_id="alice")))
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        record = record_snapshot(
            _snap(make_comment("c1", author_id="mallory")),
            detect_author_conflicts=True,
        )
    assert record.row_updated is True
    assert "author_conflict event not recorded" in caplog.text


_COLD_IMPORT_ORDERS = (
    ("cw.ticket_snapshot", "cw.dev_queue"),
    ("cw.dev_queue", "cw.ticket_snapshot"),
    ("cw.codex_review", "cw.ticket_snapshot"),
)


@pytest.mark.parametrize("order", _COLD_IMPORT_ORDERS)
def test_package_imports_cold_in_either_order(order: tuple[str, str]) -> None:
    """RFC 0019 T2: the review context imports this package, which imports
    ``cw.dev_queue``; ``cw.dev_queue`` must therefore not import
    ``cw.codex_review``, or one of these orders is a circular import."""
    code = "; ".join(f"import {name}" for name in order)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_dev_queue_does_not_load_codex_review() -> None:
    code = (
        "import sys, cw.dev_queue; "
        "print(any(m.startswith('cw.codex_review') for m in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert proc.stdout.strip() == "False"


def test_codex_must_fix_reason_has_one_definition() -> None:
    """The MUST_FIX blocker reason literal lives in one ``src/cw`` module (#3712)."""
    import ast
    from pathlib import Path

    import cw
    from cw.auto_dev_result.schema import _vocab
    from cw.codex_review import CODEX_MUST_FIX_FINDINGS
    from cw.stage_verification import finalize_result

    literal = "codex_must_fix_findings"
    assert CODEX_MUST_FIX_FINDINGS is _vocab.CODEX_MUST_FIX_FINDINGS
    assert finalize_result.CODEX_MUST_FIX_FINDINGS is _vocab.CODEX_MUST_FIX_FINDINGS
    assert literal == _vocab.CODEX_MUST_FIX_FINDINGS

    src_root = Path(cw.__file__).parent
    defining = {
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if any(
            isinstance(node, ast.Constant) and node.value == literal
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert "auto_dev_result/schema/_vocab.py" in defining  # scan is not vacuous
    assert defining == {"auto_dev_result/schema/_vocab.py"}
