"""Verify the ticket IDs a worker filed through MCP (#3041, R10).

cw has no Linear client, so this proves the SHAPE of the reported IDs only:
the prefix is one the client owns in ``linear_prefix_map``, there is exactly
one ID per emitted key, and each URL contains its ID. It cannot prove the
tickets exist. All-or-nothing: the caller records nothing unless the result
carries no failure.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_debt_record import ReviewDebtRecord

_ID_RE = re.compile(r"(?P<prefix>[A-Za-z][A-Za-z0-9]*)-\d+")
_FIELDS = frozenset({"key", "ticket_id", "url"})

UNREADABLE = "unreadable_ids_file"
NOT_JSON = "not_json"
WRONG_SHAPE = "wrong_shape"
MALFORMED_ID = "malformed_id"
PREFIX_NOT_ALLOWED = "prefix_not_allowed"
NO_PREFIX_FOR_CLIENT = "no_prefix_for_client"
MISSING_KEY = "missing_key"
EXTRA_KEY = "extra_key"
DUPLICATE_KEY = "duplicate_key"
URL_MISMATCH = "url_mismatch"


class ReportedId(NamedTuple):
    """One ``{key, ticket_id, url}`` the worker reported."""

    key: str
    ticket_id: str
    url: str


class VerifyFailure(NamedTuple):
    """One verification failure; ``key`` is None when not attributable."""

    key: str | None
    reason: str


class VerifyResult(NamedTuple):
    """The reported IDs and every failure found (all collected first)."""

    reported: list[ReportedId]
    failures: list[VerifyFailure]


def _parse(raw: str | None) -> list[ReportedId] | VerifyFailure:
    if raw is None:
        return VerifyFailure(None, UNREADABLE)
    try:
        payload = json.loads(raw)
    except ValueError:
        return VerifyFailure(None, NOT_JSON)
    if not isinstance(payload, list):
        return VerifyFailure(None, WRONG_SHAPE)
    reported: list[ReportedId] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != _FIELDS:
            return VerifyFailure(None, WRONG_SHAPE)
        values = [item["key"], item["ticket_id"], item["url"]]
        if not all(isinstance(value, str) and value for value in values):
            return VerifyFailure(None, WRONG_SHAPE)
        reported.append(ReportedId(*values))
    return reported


def _check_one(item: ReportedId, prefixes: frozenset[str]) -> str | None:
    match = _ID_RE.fullmatch(item.ticket_id)
    if match is None:
        return MALFORMED_ID
    if match.group("prefix") not in prefixes:
        return PREFIX_NOT_ALLOWED
    if item.ticket_id not in item.url:
        return URL_MISMATCH
    return None


def _check_keys(
    reported: Sequence[ReportedId], expected: frozenset[str]
) -> list[VerifyFailure]:
    failures: list[VerifyFailure] = []
    seen: set[str] = set()
    for item in reported:
        if item.key in seen:
            failures.append(VerifyFailure(item.key, DUPLICATE_KEY))
        elif item.key not in expected:
            failures.append(VerifyFailure(item.key, EXTRA_KEY))
        seen.add(item.key)
    failures += [VerifyFailure(key, MISSING_KEY) for key in sorted(expected - seen)]
    return failures


def verify_reported_ids(
    raw: str | None, expected: frozenset[str], prefixes: frozenset[str]
) -> VerifyResult:
    """Check *raw* (the ``--ids`` file text, None if unreadable) against *expected*.

    *expected* is the row's ``emitted`` keys with no ticket yet; *prefixes* the
    Linear prefixes ``linear_prefix_map`` assigns to the client.
    """
    parsed = _parse(raw)
    if isinstance(parsed, VerifyFailure):
        return VerifyResult([], [parsed])
    if not parsed and not expected:
        return VerifyResult([], [])
    if not prefixes:
        return VerifyResult(parsed, [VerifyFailure(None, NO_PREFIX_FOR_CLIENT)])
    failures = [
        VerifyFailure(item.key, reason)
        for item in parsed
        if (reason := _check_one(item, prefixes)) is not None
    ]
    failures += _check_keys(parsed, expected)
    return VerifyResult(parsed, failures)


def apply_reported(
    records: Sequence[ReviewDebtRecord], reported: Sequence[ReportedId]
) -> list[ReviewDebtRecord]:
    """The records *reported* names, with ``ticket_id``/``ticket_url`` set.

    The mode stays ``emitted``: cw did not file the ticket itself.
    """
    by_key = {item.key: item for item in reported}
    return [
        record.model_copy(
            update={
                "ticket_id": by_key[record.key].ticket_id,
                "ticket_url": by_key[record.key].url,
            }
        )
        for record in records
        if record.key in by_key
    ]
