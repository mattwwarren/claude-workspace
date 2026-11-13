"""Tests for cw.tracker_io._verify: self-attested read-back verification (#3057)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args

import pytest

from cw.gh import AGENT_COMMENT_MARKER
from cw.tracker_io import (
    READBACK_WINDOW,
    PostedComment,
    PublicState,
    TrackerIoReason,
    parse_turn_result,
    public_state_for,
    verify_turn_result,
)
from cw.tracker_io._prompt import TurnStatus
from cw.tracker_io._verify import _STATUS_REASON
from tests._tool_fakes import marked_parts, tracker_io_turn_result

_TICKET = "GEN-7"
_LONG_A = "A" * 150 + "\n" + "middle " * 80 + "\nend of part one"
_LONG_B = "B" * 300 + "\nclosing line of part two"
_PARTS = marked_parts(_LONG_A, _LONG_B)

Mutate = Callable[[dict[str, Any]], None]


def _verify(
    mutate: Mutate | None = None,
    *,
    parts: tuple[str, ...] = _PARTS,
    status: str = "ok",
    ticket: str = _TICKET,
) -> tuple[TrackerIoReason | None, PublicState, bool | None]:
    text = tracker_io_turn_result(ticket, parts, status=status, mutate=mutate)
    outcome = verify_turn_result(_TICKET, parts, parse_turn_result(text))
    return outcome.reason, outcome.public_state, outcome.marker_survived


def _set(path: tuple[Any, ...], value: object) -> Mutate:
    def mutate(doc: dict[str, Any]) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _drop_part(doc: dict[str, Any]) -> None:
    doc["parts"].pop()


def _swap_parts(doc: dict[str, Any]) -> None:
    doc["parts"].reverse()


def _duplicate_id(doc: dict[str, Any]) -> None:
    doc["parts"][1]["comment_id"] = doc["parts"][0]["comment_id"]
    doc["parts"][1]["url"] = doc["parts"][0]["url"]


def _clear_parts(doc: dict[str, Any]) -> None:
    doc["parts"] = []


@pytest.mark.parametrize(
    ("mutate", "status", "ticket", "reason"),
    [
        (None, "ok", "GEN-8", TrackerIoReason.TICKET_MISMATCH),
        (
            _set(("parts", 0, "url"), "https://linear.app/acme/issue/GEN-9#cmt-1"),
            "ok",
            _TICKET,
            TrackerIoReason.TICKET_MISMATCH,
        ),
        (_drop_part, "ok", _TICKET, TrackerIoReason.IDS_COUNT_MISMATCH),
        (_swap_parts, "ok", _TICKET, TrackerIoReason.IDS_OUT_OF_ORDER),
        (_duplicate_id, "ok", _TICKET, TrackerIoReason.ID_DUPLICATE),
        (_set(("parts", 0, "comment_id"), ""), "ok", _TICKET, "id_malformed"),
        (_set(("parts", 0, "comment_id"), "a b"), "ok", _TICKET, "id_malformed"),
        (
            _set(("parts", 0, "url"), "https://linear.app/acme/issue/GEN-7#other"),
            "ok",
            _TICKET,
            TrackerIoReason.URL_MISMATCH,
        ),
        (
            _set(("parts", 1, "readback_head"), "Bxx"),
            "ok",
            _TICKET,
            TrackerIoReason.READBACK_MISMATCH,
        ),
        (
            _set(("parts", 1, "readback_tail"), "reworded closing line"),
            "ok",
            _TICKET,
            TrackerIoReason.READBACK_MISMATCH,
        ),
        (
            _set(("parts", 0, "readback_head"), "  "),
            "ok",
            _TICKET,
            TrackerIoReason.READBACK_MISSING,
        ),
        (
            _set(("parts", 0, "readback_tail"), AGENT_COMMENT_MARKER),
            "ok",
            _TICKET,
            TrackerIoReason.READBACK_MISSING,
        ),
        (None, "failed", _TICKET, TrackerIoReason.TURN_REPORTED_FAILURE),
        (None, "tool_unavailable", _TICKET, TrackerIoReason.TOOL_UNAVAILABLE),
    ],
)
def test_named_reason_per_failure(
    mutate: Mutate | None, status: str, ticket: str, reason: str
) -> None:
    got, state, _marker = _verify(mutate, status=status, ticket=ticket)
    assert got == reason
    # Every case above returned ids, so something is public.
    assert state is PublicState.CONFIRMED


def test_empty_url_is_allowed() -> None:
    reason, _state, _marker = _verify(_set(("parts", 0, "url"), ""))
    assert reason is None


def test_unparseable_result_is_possible() -> None:
    outcome = verify_turn_result(_TICKET, _PARTS, None)
    assert outcome.reason is TrackerIoReason.RESULT_UNPARSEABLE
    assert outcome.public_state is PublicState.POSSIBLE
    assert outcome.posted == ()
    assert outcome.marker_survived is None


@pytest.mark.parametrize("status", ["failed", "tool_unavailable"])
def test_agent_failure_with_zero_parts_is_none(status: str) -> None:
    reason, state, marker = _verify(_clear_parts, status=status)
    assert reason is not None
    assert state is PublicState.NONE
    assert marker is None


def test_ok_with_zero_parts_is_possible() -> None:
    reason, state, _marker = _verify(_clear_parts)
    assert reason is TrackerIoReason.IDS_COUNT_MISMATCH
    assert state is PublicState.POSSIBLE


def test_success_with_marker_intact() -> None:
    text = tracker_io_turn_result(_TICKET, _PARTS)
    outcome = verify_turn_result(_TICKET, _PARTS, parse_turn_result(text))
    assert outcome.reason is None
    assert outcome.verified
    assert outcome.public_state is PublicState.CONFIRMED
    assert outcome.marker_survived is True
    assert outcome.posted == (
        PostedComment(
            1, "cmt-1", "https://linear.app/acme/issue/GEN-7/x#comment-cmt-1"
        ),
        PostedComment(
            2, "cmt-2", "https://linear.app/acme/issue/GEN-7/x#comment-cmt-2"
        ),
    )


def _strip_markers(doc: dict[str, Any]) -> None:
    for part in doc["parts"]:
        body = part["readback_tail"].removesuffix(AGENT_COMMENT_MARKER).rstrip()
        stored = _LONG_A if part["index"] == 1 else _LONG_B
        part["readback_tail"] = stored[-READBACK_WINDOW:]
        assert stored.endswith(body)


def test_success_with_marker_stripped() -> None:
    """Linear dropped the hidden marker: still verified, survival recorded."""
    reason, state, marker = _verify(_strip_markers)
    assert reason is None
    assert state is PublicState.CONFIRMED
    assert marker is False


def test_tail_length_comes_from_the_stripped_readback() -> None:
    """The stored tail lacks the marker; only the emitted side carries one."""
    tail = _LONG_B[-READBACK_WINDOW:]
    reason, _state, marker = _verify(_set(("parts", 1, "readback_tail"), tail))
    assert reason is None
    assert marker is False


def test_short_part_head_and_tail_are_the_whole_body() -> None:
    parts = marked_parts("short body")
    assert len(parts[0]) < READBACK_WINDOW
    reason, _state, marker = _verify(parts=parts)
    assert reason is None
    assert marker is True


def test_crlf_readback_is_normalised() -> None:
    parts = marked_parts("line one\nline two")
    crlf = parts[0].replace("\n", "\r\n")
    mutate = _set(("parts", 0, "readback_head"), crlf)
    reason, _state, _marker = _verify(mutate, parts=parts)
    assert reason is None
    reason, _state, _marker = _verify(
        _set(("parts", 0, "readback_tail"), crlf), parts=parts
    )
    assert reason is None


def test_middle_of_a_long_part_is_not_verified() -> None:
    """Pins the documented limit: head/tail cannot see the middle."""
    tampered = _LONG_A[:300] + "INSERTED" + _LONG_A[300:]
    assert len(_PARTS[0]) - 300 > READBACK_WINDOW > 0
    stored = marked_parts(tampered, _LONG_B)
    text = tracker_io_turn_result(_TICKET, stored)
    outcome = verify_turn_result(_TICKET, _PARTS, parse_turn_result(text))
    assert outcome.reason is None


def test_ticket_id_in_url_is_case_insensitive() -> None:
    url = "https://linear.app/acme/issue/gen-7/x#comment-cmt-1"
    reason, _state, _marker = _verify(_set(("parts", 0, "url"), url))
    assert reason is None


@pytest.mark.parametrize(
    ("reason", "parts_returned", "state"),
    [
        (None, 2, PublicState.CONFIRMED),
        (TrackerIoReason.TRACKER_UNSUPPORTED, 0, PublicState.NONE),
        (TrackerIoReason.MCP_SERVER_UNNAMED, 0, PublicState.NONE),
        (TrackerIoReason.TRACKER_MCP_NOT_ENABLED, 0, PublicState.NONE),
        (TrackerIoReason.PLAN_REFUSED, 0, PublicState.NONE),
        (TrackerIoReason.SCRATCH_UNWRITABLE, 0, PublicState.NONE),
        (TrackerIoReason.CODEX_UNAVAILABLE, 0, PublicState.NONE),
        (TrackerIoReason.TOOL_UNAVAILABLE, 0, PublicState.NONE),
        (TrackerIoReason.TOOL_UNAVAILABLE, 1, PublicState.CONFIRMED),
        (TrackerIoReason.TURN_REPORTED_FAILURE, 0, PublicState.NONE),
        (TrackerIoReason.TURN_REPORTED_FAILURE, 1, PublicState.CONFIRMED),
        (TrackerIoReason.TURN_TIMEOUT, 0, PublicState.POSSIBLE),
        (TrackerIoReason.TURN_EXIT_NONZERO, 0, PublicState.POSSIBLE),
        (TrackerIoReason.RESULT_UNPARSEABLE, 0, PublicState.POSSIBLE),
        (TrackerIoReason.TICKET_MISMATCH, 1, PublicState.CONFIRMED),
        (TrackerIoReason.TICKET_MISMATCH, 0, PublicState.POSSIBLE),
        (TrackerIoReason.IDS_COUNT_MISMATCH, 1, PublicState.CONFIRMED),
        (TrackerIoReason.IDS_OUT_OF_ORDER, 2, PublicState.CONFIRMED),
        (TrackerIoReason.ID_MALFORMED, 1, PublicState.CONFIRMED),
        (TrackerIoReason.ID_DUPLICATE, 2, PublicState.CONFIRMED),
        (TrackerIoReason.URL_MISMATCH, 1, PublicState.CONFIRMED),
        (TrackerIoReason.READBACK_MISMATCH, 1, PublicState.CONFIRMED),
        (TrackerIoReason.READBACK_MISSING, 1, PublicState.CONFIRMED),
    ],
)
def test_public_state_mapping(
    reason: TrackerIoReason | None, parts_returned: int, state: PublicState
) -> None:
    assert public_state_for(reason, parts_returned=parts_returned) is state


def test_every_reason_has_a_mapping_row() -> None:
    """A new reason must be classified deliberately (the table above)."""
    for reason in TrackerIoReason:
        assert isinstance(public_state_for(reason, parts_returned=0), PublicState)


def test_status_reason_covers_every_non_ok_turn_status() -> None:
    """#3383 drift guard: the reason map and the TurnStatus literal stay in step.

    ``"ok"`` is the success status and maps to no reason. The real enforcement
    is the ``dict[TurnStatus, TrackerIoReason]`` annotation under mypy --strict.
    """
    statuses = set(get_args(TurnStatus))
    assert set(_STATUS_REASON) | {"ok"} == statuses
    assert all(
        isinstance(reason, TrackerIoReason) for reason in _STATUS_REASON.values()
    )
