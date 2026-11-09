"""Bounded loader for a review engine's JSON summary (#3036, RFC 0017 S1).

The engine is untrusted input. :func:`load_review_engine_summary` reads at most
one byte past ``MAX_ENGINE_SUMMARY_BYTES``, then decodes, parses, checks the
schema version and validates, in that order, and raises one
:class:`EngineSummaryError` naming the first failed stage as an
:class:`EngineSummaryRejection`. That reason set is the contract later stages
consume (``docs/headless-contract.md`` section 12).

Every rejection carries ``issues``: one per pydantic error for a validation
failure, otherwise one. Issue text never includes a field's input value, so an
engine's ``evidence`` or ``summary`` text is never echoed, and it is sanitized
(non-printable characters become ``?``, truncated to ``MAX_ISSUE_TEXT_CHARS``)
because engine-controlled key names reach pydantic error locations.

The loader writes nothing and emits no event. It does not reuse
``cw.result._read_json_payload``, which is uncapped and exits the process.

:func:`find_round_summary_for_head` (#3108) finds the one round's summary for a
reviewed head when the caller has no round id (the dev-queue attestation
builder). It is filesystem-only: no git, no subprocess, no lock (ADR-0019).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from cw.exceptions import CwError
from cw.models.review_engine_summary import (
    INCONSISTENT_ERROR_TYPES,
    MAX_ENGINE_SUMMARY_BYTES,
    REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
    ReviewEngineSummary,
)
from cw.review_engine.constants import (
    ENGINE_STATE_RELATIVE_DIR,
    ENGINE_SUMMARY_FILENAME,
    ROUND_ID_RE,
)

if TYPE_CHECKING:
    from pydantic_core import ErrorDetails

#: Truncation limit for every issue's path and message (not a rejection reason).
MAX_ISSUE_TEXT_CHARS = 200

#: How many of the newest round directories :func:`find_round_summary_for_head`
#: reads. A worktree accrues one directory per engine round; the round that
#: backs a clean exit is among the latest, so the scan stays bounded.
MAX_ROUND_DIRS_SCANNED = 64

_ROUND_ID_PATTERN: Final = re.compile(ROUND_ID_RE)
_COMPLETE: Final = "COMPLETE"
_CLEAN_VERDICT: Final = "PASS"

_SCHEMA_VERSION_KEY: Final = "schema_version"
_JSON_TYPE_NAMES: Final[dict[type, str]] = {
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


class EngineSummaryRejection(StrEnum):
    """Why an engine summary was rejected, one value per loader stage."""

    UNREADABLE = "unreadable"
    OVERSIZED = "oversized"
    NOT_UTF8 = "not_utf8"
    NOT_JSON = "not_json"
    NOT_OBJECT = "not_object"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    SCHEMA_INVALID = "schema_invalid"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class EngineSummaryIssue:
    """One printable rejection line: its reason, dotted field path and message."""

    reason: EngineSummaryRejection
    field_path: str | None
    message: str


class EngineSummaryError(CwError):
    """An engine summary that failed a loader stage.

    ``reason`` and ``field_path`` are the first issue's. ``schema_invalid``
    issues sort ahead of ``inconsistent`` ones, so a field-level malformation
    is reported before a cross-field rule.
    """

    __slots__ = ()

    reason: EngineSummaryRejection
    field_path: str | None
    issues: tuple[EngineSummaryIssue, ...]

    def __init__(
        self,
        message: str,
        *,
        reason: EngineSummaryRejection,
        field_path: str | None = None,
        issues: tuple[EngineSummaryIssue, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.field_path = field_path
        self.issues = issues or (EngineSummaryIssue(reason, field_path, message),)


def _sanitize(text: str) -> str:
    """*text* with non-printable characters as ``?``, cut to the issue cap."""
    cleaned = "".join(char if char.isprintable() else "?" for char in text)
    return cleaned[:MAX_ISSUE_TEXT_CHARS]


def _reject(
    reason: EngineSummaryRejection, message: str, field_path: str | None = None
) -> EngineSummaryError:
    return EngineSummaryError(_sanitize(message), reason=reason, field_path=field_path)


def _read_capped(path: Path) -> bytes:
    """At most cap + 1 bytes of *path*, so a huge file is never fully read."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_ENGINE_SUMMARY_BYTES + 1)
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        msg = f"cannot read {path}: {detail}"
        raise _reject(EngineSummaryRejection.UNREADABLE, msg) from exc
    return raw


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_ENGINE_SUMMARY_BYTES:
        msg = f"summary exceeds the {MAX_ENGINE_SUMMARY_BYTES}-byte cap"
        raise _reject(EngineSummaryRejection.OVERSIZED, msg)


def _json_error_detail(exc: ValueError | RecursionError) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    if isinstance(exc, RecursionError):
        return "nesting is too deep"
    return str(exc)


def _decode_json_object(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"summary is not valid UTF-8 (byte {exc.start})"
        raise _reject(EngineSummaryRejection.NOT_UTF8, msg) from exc
    try:
        payload: object = json.loads(text)
    except (ValueError, RecursionError) as exc:
        msg = f"summary is not valid JSON: {_json_error_detail(exc)}"
        raise _reject(EngineSummaryRejection.NOT_JSON, msg) from exc
    if not isinstance(payload, dict):
        kind = _JSON_TYPE_NAMES.get(type(payload), type(payload).__name__)
        msg = f"summary must be a JSON object, got {kind}"
        raise _reject(EngineSummaryRejection.NOT_OBJECT, msg)
    return payload


def _check_version(payload: dict[str, object]) -> None:
    """Fail closed on any version but the supported one, before field checks."""
    if _SCHEMA_VERSION_KEY not in payload:
        detail = "is missing"
    else:
        value = payload[_SCHEMA_VERSION_KEY]
        if isinstance(value, bool) or not isinstance(value, int):
            detail = f"has type {type(value).__name__}, not int"
        elif value != REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION:
            detail = f"{value} is not supported"
        else:
            return
    msg = f"schema_version {detail} (expected {REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION})"
    raise _reject(
        EngineSummaryRejection.UNSUPPORTED_SCHEMA_VERSION, msg, _SCHEMA_VERSION_KEY
    )


def _issue_from(error: ErrorDetails) -> EngineSummaryIssue:
    """One issue from one pydantic error; its ``input`` is never read."""
    parts = [str(part) for part in error["loc"]]
    extra = error.get("ctx", {}).get("field_path")
    if isinstance(extra, str):
        parts.append(extra)
    path = ".".join(parts)
    reason = (
        EngineSummaryRejection.INCONSISTENT
        if error["type"] in INCONSISTENT_ERROR_TYPES
        else EngineSummaryRejection.SCHEMA_INVALID
    )
    return EngineSummaryIssue(
        reason, _sanitize(path) if path else None, _sanitize(error["msg"])
    )


def _issues_from(exc: ValidationError) -> tuple[EngineSummaryIssue, ...]:
    issues = [_issue_from(error) for error in exc.errors(include_input=False)]
    issues.sort(
        key=lambda issue: issue.reason is not EngineSummaryRejection.SCHEMA_INVALID
    )
    return tuple(issues)


def parse_review_engine_summary(raw: bytes) -> ReviewEngineSummary:
    """Validate *raw* summary bytes, or raise :class:`EngineSummaryError`."""
    _check_size(raw)
    payload = _decode_json_object(raw)
    _check_version(payload)
    try:
        summary = ReviewEngineSummary.model_validate(payload)
    except ValidationError as exc:
        issues = _issues_from(exc)
        first = issues[0]
        msg = f"{len(issues)} validation issue(s); first: {first.message}"
        raise EngineSummaryError(
            msg, reason=first.reason, field_path=first.field_path, issues=issues
        ) from exc
    return summary


def load_review_engine_summary(path: Path) -> ReviewEngineSummary:
    """Read and validate the summary at *path*, or raise :class:`EngineSummaryError`."""
    return parse_review_engine_summary(_read_capped(path))


def _round_dirs(worktree: Path) -> list[Path]:
    """The newest ``MAX_ROUND_DIRS_SCANNED`` real round directories, newest first.

    A symlinked entry, a file and a name failing ``ROUND_ID_RE`` are skipped. A
    missing or unreadable engine directory yields none.
    """
    engine_dir = worktree / ENGINE_STATE_RELATIVE_DIR
    stamped: list[tuple[float, str, Path]] = []
    try:
        with os.scandir(engine_dir) as entries:
            for entry in entries:
                if _ROUND_ID_PATTERN.fullmatch(entry.name) is None:
                    continue
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                mtime = entry.stat(follow_symlinks=False).st_mtime
                stamped.append((mtime, entry.name, Path(entry.path)))
    except OSError:
        return []
    stamped.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in stamped[:MAX_ROUND_DIRS_SCANNED]]


def _load_round_summary(directory: Path) -> ReviewEngineSummary | None:
    """The valid summary in round *directory*, or None for any unusable one."""
    path = directory / ENGINE_SUMMARY_FILENAME
    if path.is_symlink():
        return None
    try:
        return load_review_engine_summary(path)
    except EngineSummaryError:
        return None


def find_round_summary_for_head(
    worktree: Path, head_sha: str
) -> ReviewEngineSummary | None:
    """The engine summary whose ``head_sha`` is *head_sha*, or None (#3108).

    Scans ``<worktree>/.cw/review-engine/<round id>/summary.json`` newest
    round first, skipping anything unusable. Among summaries at *head_sha* it
    prefers the newest ``COMPLETE``/``PASS`` one, then the newest match of any
    verdict, so the caller can tell a contradicting summary from a missing one.
    """
    fallback: ReviewEngineSummary | None = None
    for directory in _round_dirs(worktree):
        summary = _load_round_summary(directory)
        if summary is None or summary.head_sha != head_sha:
            continue
        if summary.coverage == _COMPLETE and summary.verdict == _CLEAN_VERDICT:
            return summary
        if fallback is None:
            fallback = summary
    return fallback
