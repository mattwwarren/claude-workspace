"""Tests for :mod:`cw.dev_queue.review_debt_records` (#3041)."""

from __future__ import annotations

import pytest

from cw import _lock_guard
from cw.dev_queue.review_debt_records import read_review_debt, record_review_debt
from cw.dev_queue.storage import load_dev_queue, save_dev_queue
from cw.exceptions import DEV_QUEUE_LOCK_NAME, CwError
from cw.models import DevQueueStore, OrchestratorEventType
from cw.models.review_attestation import AttestationDeferral
from tests._cli_review_helpers import _seed_review_row
from tests._review_attestation_helpers import attestation_for
from tests._review_debt_helpers import _debt_record
from tests._ticket_view_helpers import events_of

_TICKET = "3041"
_CLIENT = "acme"
_PR = "https://github.com/owner/repo/pull/9"


def _row_debt() -> list[str]:
    store = load_dev_queue()
    rows = [t for t in store.tasks if t.ticket_id == _TICKET]
    return [record.key for record in rows[0].review_debt]


def test_records_land_on_the_row() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    merged = record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    assert [record.key for record in merged] == ["a" * 64]
    assert _row_debt() == ["a" * 64]


def test_upsert_by_key_replaces_and_keeps_order() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(
        _TICKET, _CLIENT, [_debt_record(key="k1"), _debt_record(key="k2")]
    )
    record_review_debt(_TICKET, _CLIENT, [_debt_record(key="k1", ticket_id="202")])
    records = read_review_debt(_TICKET, _CLIENT)
    assert [(r.key, r.ticket_id) for r in records] == [("k1", "202"), ("k2", "101")]


def test_upsert_preserves_an_existing_pr_url_for_the_same_ticket() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(_TICKET, _CLIENT, [_debt_record(pr_url=_PR)])
    record_review_debt(_TICKET, _CLIENT, [_debt_record(ticket_url="#101")])
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert record.pr_url == _PR
    assert (record.ticket_id, record.ticket_url) == ("101", "#101")


def test_upsert_resets_the_pr_url_when_a_new_ticket_replaces_the_old() -> None:
    """#3044: a refiled ticket must not inherit the closed ticket's PR."""
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(_TICKET, _CLIENT, [_debt_record(pr_url=_PR)])
    record_review_debt(_TICKET, _CLIENT, [_debt_record(ticket_id="303")])
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert record.pr_url is None
    assert record.ticket_id == "303"


def test_upsert_takes_an_incoming_pr_url() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    other = "https://github.com/owner/repo/pull/10"
    record_review_debt(_TICKET, _CLIENT, [_debt_record(pr_url=_PR)])
    record_review_debt(_TICKET, _CLIENT, [_debt_record(pr_url=other)])
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert record.pr_url == other


def test_a_missing_row_raises() -> None:
    with pytest.raises(CwError):
        record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    with pytest.raises(CwError):
        read_review_debt(_TICKET, _CLIENT)


def test_the_write_takes_only_the_dev_queue_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row write runs under ``dev_queue_lock`` alone, never ``sessions_lock``."""
    _seed_review_row(_TICKET, _CLIENT)
    seen: list[tuple[str, ...]] = []

    def spy_save(store: DevQueueStore) -> None:
        seen.append(tuple(lock.name for lock in _lock_guard.held_locks()))
        save_dev_queue(store)

    monkeypatch.setattr("cw.dev_queue.review_debt_records.save_dev_queue", spy_save)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    assert seen == [(DEV_QUEUE_LOCK_NAME,)]


def _seed_attested(key: str) -> None:
    """A row whose stored attestation carries one null-ticket deferral keyed *key*."""
    _seed_review_row(_TICKET, _CLIENT)
    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == _TICKET)
    deferral = AttestationDeferral(debt_key=key, severity="SHOULD_FIX")
    task.review_attestation = attestation_for("a" * 40, deferrals=[deferral])
    save_dev_queue(store)


def _stored_deferral() -> AttestationDeferral:
    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == _TICKET)
    assert task.review_attestation is not None
    (deferral,) = task.review_attestation.deferrals or []
    return deferral


def _refresh_events() -> list[object]:
    return [
        event
        for event in events_of(OrchestratorEventType.REVIEW_ATTESTATION_RECORDED)
        if event.payload["refresh"] is True
    ]


def test_a_record_fills_the_stored_attestation_link() -> None:
    _seed_attested("a" * 64)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    deferral = _stored_deferral()
    assert (deferral.debt_key, deferral.ticket_id) == ("a" * 64, "101")
    assert len(_refresh_events()) == 1


def test_an_identical_record_emits_no_new_refresh() -> None:
    _seed_attested("a" * 64)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    assert len(_refresh_events()) == 1


def test_a_row_without_an_attestation_is_unaffected() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == _TICKET)
    assert task.review_attestation is None
    assert _refresh_events() == []


def test_the_refresh_write_takes_only_the_dev_queue_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an attestation present the refresh still saves under the one lock."""
    _seed_attested("a" * 64)
    seen: list[tuple[str, ...]] = []

    def spy_save(store: DevQueueStore) -> None:
        seen.append(tuple(lock.name for lock in _lock_guard.held_locks()))
        save_dev_queue(store)

    monkeypatch.setattr("cw.dev_queue.review_debt_records.save_dev_queue", spy_save)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])
    assert seen == [(DEV_QUEUE_LOCK_NAME,)]
