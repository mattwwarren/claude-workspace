"""Tests for ``cw.dispatch_state_ticket_state``, the closed-ticket cache (#1605)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

import cw.dispatch_state
from cw.dispatch_state import save_open_pr_probe_entry
from cw.dispatch_state_ticket_state import (
    TICKET_STATE_PROBE_KEY,
    TicketStateProbe,
    guard_cancelled_row_keys,
    load_ticket_state_probes,
    mark_ticket_closed_cancelled,
    save_ticket_state_probes,
    ticket_state_key,
)

_T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
_A = ticket_state_key("acme", "1", _T0)
_B = ticket_state_key("acme", "2", _T0)
_C = ticket_state_key("acme", "3", _T0)


def _raw() -> dict[str, object]:
    payload = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
    assert isinstance(payload, dict)
    return payload


def test_key_shape() -> None:
    assert _A == "acme/1::2026-11-13T12:00:00+00:00"


def test_round_trip() -> None:
    entries = {
        _A: TicketStateProbe(checked_at=_T0, closed=True),
        _B: TicketStateProbe(checked_at=_T0, closed=False),
        _C: TicketStateProbe(
            checked_at=None, closed=False, attempted_at=_T0 + timedelta(seconds=1)
        ),
    }

    save_ticket_state_probes("acme", entries, prune_to_keys=list(entries))
    mark_ticket_closed_cancelled(_A, at=_T0 + timedelta(minutes=1))

    loaded = load_ticket_state_probes()
    assert loaded[_A] == TicketStateProbe(
        checked_at=_T0, closed=True, cancelled_at=_T0 + timedelta(minutes=1)
    )
    assert loaded[_B] == entries[_B]
    assert loaded[_C] == entries[_C]
    assert guard_cancelled_row_keys() == frozenset({_A})


def test_stamp_without_an_entry_creates_a_closed_one() -> None:
    mark_ticket_closed_cancelled(_A, at=_T0)

    assert load_ticket_state_probes()[_A] == TicketStateProbe(
        checked_at=_T0, closed=True, cancelled_at=_T0
    )


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        {"checked_at": _T0.isoformat()},
        {"closed": "yes"},
        {"closed": True, "checked_at": 3},
        {"closed": True, "cancelled_at": "not-a-date"},
        {"closed": True, "attempted_at": "2026-11-13T12:00:00"},
    ],
    ids=["not-dict", "no-closed", "closed-str", "checked-int", "bad-date", "naive"],
)
def test_a_malformed_entry_is_dropped_alone(bad: object) -> None:
    save_ticket_state_probes(
        "acme", {_A: TicketStateProbe(_T0, closed=True)}, prune_to_keys=[_A, _B]
    )
    payload = _raw()
    entries = payload[TICKET_STATE_PROBE_KEY]
    assert isinstance(entries, dict)
    entries[_B] = bad
    cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(payload))

    assert set(load_ticket_state_probes()) == {_A}


@pytest.mark.parametrize("content", [None, "{corrupt", "[1, 2]", '{"x": 1}'])
def test_absent_or_corrupt_sidecar_is_empty(content: str | None) -> None:
    path = cw.dispatch_state.DISPATCH_STATE_FILE
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    assert load_ticket_state_probes() == {}
    assert guard_cancelled_row_keys() == frozenset()


def test_a_write_keeps_other_sidecar_keys() -> None:
    save_open_pr_probe_entry(
        "acme", "1", cw.dispatch_state.OpenPrProbeCache(_T0, has_open_pr=False)
    )
    payload = _raw()
    payload["usage_limited_until"] = {"acme": (_T0 + timedelta(days=1)).isoformat()}
    cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(payload))

    save_ticket_state_probes(
        "acme", {_A: TicketStateProbe(_T0, closed=True)}, prune_to_keys=[_A]
    )

    after = _raw()
    assert "open_pr_probe" in after
    assert "usage_limited_until" in after
    entries = after[TICKET_STATE_PROBE_KEY]
    assert isinstance(entries, dict)
    assert _A in entries


def test_prune_drops_only_this_clients_absent_rows() -> None:
    other = ticket_state_key("zeta", "1", _T0)
    save_ticket_state_probes(
        "acme",
        {_A: TicketStateProbe(_T0, closed=True), _B: TicketStateProbe(_T0, False)},
        prune_to_keys=[_A, _B],
    )
    save_ticket_state_probes(
        "zeta", {other: TicketStateProbe(_T0, closed=False)}, prune_to_keys=[other]
    )

    save_ticket_state_probes("acme", {}, prune_to_keys=[_A])

    assert set(load_ticket_state_probes()) == {_A, other}


def test_an_unchanged_save_does_not_write() -> None:
    save_ticket_state_probes("acme", {}, prune_to_keys=[])

    assert not cw.dispatch_state.DISPATCH_STATE_FILE.exists()


def test_a_write_oserror_is_swallowed_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "read-only"
        raise OSError(msg)

    monkeypatch.setattr("cw.dispatch_state_ticket_state.atomic_write_text", _boom)
    with caplog.at_level(logging.WARNING):
        save_ticket_state_probes(
            "acme", {_A: TicketStateProbe(_T0, closed=True)}, prune_to_keys=[_A]
        )
        mark_ticket_closed_cancelled(_A, at=_T0)

    assert "failed to persist ticket_state_probe" in caplog.text
    assert "failed to stamp ticket_state_probe" in caplog.text
    assert load_ticket_state_probes() == {}
