"""Tests for review-debt emit and MCP ID verification (#3041, R9/R10)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from cw.models import OrchestratorEventType
from cw.review_debt_filing import (
    CAUSE_AUTO_CREATE_OFF,
    CAUSE_MCP,
    apply_reported,
    debt_marker,
    emit_review_debt,
    payload_path,
    verify_reported_ids,
)
from tests._review_debt_helpers import (
    _debt_ctx,
    _debt_entry,
    _debt_record,
    _events_of,
)

if TYPE_CHECKING:
    from pathlib import Path

_A = _debt_entry(summary="first finding")
_B = _debt_entry(summary="second finding at /home/alice/x.py")
_PREFIXES = frozenset({"ENG"})


def test_emit_writes_one_redacted_payload_per_unticketed_entry(
    tmp_path: Path,
) -> None:
    out = tmp_path / "review-debt"
    result = emit_review_debt(
        [_A, _B, _A._replace(ticket="#4")], _debt_ctx(), out, cause=CAUSE_MCP
    )
    assert not result.refused
    assert sorted(f.key for f in result.files) == sorted([_A.key, _B.key])
    payload = json.loads(payload_path(out, _B.key).read_text(encoding="utf-8"))
    assert payload["key"] == _B.key
    assert payload["marker"] == debt_marker(_B.key)
    assert "/home/alice" not in payload["title"] + payload["body"]
    assert [(r.key, r.mode, r.ticket_id) for r in result.records] == sorted(
        [(_A.key, "emitted", None), (_B.key, "emitted", None)]
    )
    events = _events_of(OrchestratorEventType.REVIEW_DEBT_EMITTED)
    assert {e["cause"] for e in events} == {"mcp"}
    assert len(events) == 2


def test_a_re_emit_is_byte_identical(tmp_path: Path) -> None:
    out = tmp_path / "out"
    emit_review_debt([_A], _debt_ctx(), out, cause=CAUSE_MCP)
    path = payload_path(out, _A.key)
    first = path.read_bytes()
    again = emit_review_debt(
        [_A._replace(evidence="different now")], _debt_ctx(), out, cause=CAUSE_MCP
    )
    assert path.read_bytes() == first
    assert [f.written for f in again.files] == [False]
    assert len(_events_of(OrchestratorEventType.REVIEW_DEBT_EMITTED)) == 1
    assert first.endswith(b"}\n")
    assert json.loads(first) == dict(sorted(json.loads(first).items()))


def test_a_corrupt_payload_is_rewritten(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    payload_path(out, _A.key).write_text("{not json", encoding="utf-8")
    result = emit_review_debt([_A], _debt_ctx(), out, cause=CAUSE_MCP)
    assert [f.written for f in result.files] == [True]


def test_emit_refuses_on_unknown_visibility(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = emit_review_debt(
        [_A], _debt_ctx(visibility=None), out, cause=CAUSE_AUTO_CREATE_OFF
    )
    assert result.refused
    assert not out.exists()


def test_emit_skips_ticketed_and_already_recorded_entries(tmp_path: Path) -> None:
    existing = (
        _debt_record(key=_A.key),
        _debt_record(key=_B.key, mode="emitted", ticket_id=None, ticket_url=None),
    )
    result = emit_review_debt(
        [_A, _B], _debt_ctx(existing=existing), tmp_path, cause=CAUSE_MCP
    )
    assert [f.key for f in result.files] == [_B.key]
    assert result.records == []


def test_emit_with_nothing_to_emit(tmp_path: Path) -> None:
    out = tmp_path / "never"
    result = emit_review_debt([], _debt_ctx(), out, cause=CAUSE_MCP)
    assert (result.refused, result.files, result.records) == (False, [], [])
    assert not out.exists()


def test_payload_names_replace_colons(tmp_path: Path) -> None:
    assert payload_path(tmp_path, "engine:a:b").name == "engine_a_b.json"


def test_a_hashed_key_gets_an_injective_payload_name(tmp_path: Path) -> None:
    hashed = _debt_entry(fingerprint="fp_1").key
    name = payload_path(tmp_path, hashed).name
    assert re.fullmatch(r"engine_h_[0-9a-f]{32}\.json", name)
    raw = {_A.key, _B.key, "engine:fp-1", "engine:r1:abc"}
    assert name not in {payload_path(tmp_path, key).name for key in raw}


def _ids(*items: tuple[str, str, str]) -> str:
    return json.dumps([{"key": k, "ticket_id": t, "url": u} for k, t, u in items])


def test_record_success() -> None:
    raw = _ids((_A.key, "ENG-12", "https://linear.app/x/issue/ENG-12/t"))
    result = verify_reported_ids(raw, frozenset({_A.key}), _PREFIXES)
    assert result.failures == []
    records = apply_reported(
        [_debt_record(key=_A.key, mode="emitted", ticket_id=None, ticket_url=None)],
        result.reported,
    )
    assert [(r.ticket_id, r.ticket_url, r.mode) for r in records] == [
        ("ENG-12", "https://linear.app/x/issue/ENG-12/t", "emitted")
    ]


def test_nothing_expected_and_nothing_reported_is_fine() -> None:
    assert verify_reported_ids("[]", frozenset(), frozenset()) == ([], [])


_URL = "https://linear.app/x/issue/"


@pytest.mark.parametrize(
    ("raw", "expected", "prefixes", "failures"),
    [
        (None, {"k"}, _PREFIXES, [(None, "unreadable_ids_file")]),
        ("{not", {"k"}, _PREFIXES, [(None, "not_json")]),
        ('{"key": "k"}', {"k"}, _PREFIXES, [(None, "wrong_shape")]),
        ('[{"key": "k"}]', {"k"}, _PREFIXES, [(None, "wrong_shape")]),
        ("[1]", {"k"}, _PREFIXES, [(None, "wrong_shape")]),
        (
            '[{"key": "k", "ticket_id": "", "url": "u"}]',
            {"k"},
            _PREFIXES,
            [(None, "wrong_shape")],
        ),
        (
            _ids(("k", "ENG12", f"{_URL}ENG12")),
            {"k"},
            _PREFIXES,
            [("k", "malformed_id")],
        ),
        (
            _ids(("k", "OPS-1", f"{_URL}OPS-1")),
            {"k"},
            _PREFIXES,
            [("k", "prefix_not_allowed")],
        ),
        (
            _ids(("k", "ENG-1", f"{_URL}ENG-1")),
            {"k"},
            frozenset(),
            [(None, "no_prefix_for_client")],
        ),
        (
            _ids(("k", "ENG-1", f"{_URL}ENG-2")),
            {"k"},
            _PREFIXES,
            [("k", "url_mismatch")],
        ),
        ("[]", {"k"}, _PREFIXES, [("k", "missing_key")]),
        (
            _ids(("k", "ENG-1", f"{_URL}ENG-1"), ("x", "ENG-2", f"{_URL}ENG-2")),
            {"k"},
            _PREFIXES,
            [("x", "extra_key")],
        ),
        (
            _ids(("k", "ENG-1", f"{_URL}ENG-1"), ("k", "ENG-2", f"{_URL}ENG-2")),
            {"k"},
            _PREFIXES,
            [("k", "duplicate_key")],
        ),
    ],
)
def test_each_verify_failure(
    raw: str | None,
    expected: set[str],
    prefixes: frozenset[str],
    failures: list[tuple[str | None, str]],
) -> None:
    result = verify_reported_ids(raw, frozenset(expected), prefixes)
    assert [(f.key, f.reason) for f in result.failures] == failures
