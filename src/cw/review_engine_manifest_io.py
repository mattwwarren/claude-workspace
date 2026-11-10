"""Bounded loader for a review engine's ``cw-engine.json`` (#3216, RFC 0018 A4).

The manifest, and the files it names, are untrusted repo input.
:func:`load_engine_manifest` reads at most one byte past
``MAX_ENGINE_MANIFEST_BYTES``, then decodes, parses (rejecting a duplicate key
at any depth), checks ``manifest_version``, validates the model, checks role-id
slug uniqueness and finally reads each referenced file under the path rules.
It raises one :class:`EngineManifestError` naming the first failed stage as an
:class:`EngineManifestRejection`.

The issue shape matches ``cw.review_engine_summary_io``: every rejection carries
``issues`` (``reason``, dotted ``field_path``, sanitized capped ``message``),
issue text never echoes a field's input value, and ``schema_invalid`` issues
sort ahead of the others.

The loader writes nothing, emits no event and imports neither ``cw.codex_review``
nor any git helper, so ``cw review engine validate --kind manifest`` stays light.
It reads only inside the manifest's directory: referenced files are resolved
against it and refused when they resolve outside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

from pydantic import ValidationError

from cw.exceptions import CwError
from cw.models.review_engine_manifest import (
    ENGINE_MANIFEST_VERSION,
    MAX_ENGINE_MANIFEST_BYTES,
    MAX_ENGINE_SPEC_BYTES,
    EngineManifest,
)
from cw.review_engine.constants import role_slug

if TYPE_CHECKING:
    from pydantic_core import ErrorDetails

#: Truncation limit for every issue's path and message (not a rejection reason).
MAX_ISSUE_TEXT_CHARS = 200

_VERSION_KEY: Final = "manifest_version"
_BACKENDS_FIELD: Final = "backends"
_AGGREGATION_FIELD: Final = "aggregation_prompt"
_JSON_TYPE_NAMES: Final[dict[type, str]] = {
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}

ManifestCause = Literal["codex_not_declared", "manifest_invalid"]


class EngineManifestRejection(StrEnum):
    """Why an engine manifest was rejected, one value per loader stage."""

    UNREADABLE = "unreadable"
    OVERSIZED = "oversized"
    NOT_UTF8 = "not_utf8"
    NOT_JSON = "not_json"
    DUPLICATE_KEY = "duplicate_key"
    NOT_OBJECT = "not_object"
    UNSUPPORTED_MANIFEST_VERSION = "unsupported_manifest_version"
    SCHEMA_INVALID = "schema_invalid"
    INCONSISTENT = "inconsistent"
    PATH_INVALID = "path_invalid"
    FILE_NOT_UTF8 = "file_not_utf8"
    FILE_BLANK = "file_blank"
    FILE_OVERSIZED = "file_oversized"


@dataclass(frozen=True)
class EngineManifestIssue:
    """One printable rejection line: its reason, dotted field path and message."""

    reason: EngineManifestRejection
    field_path: str | None
    message: str


class EngineManifestError(CwError):
    """An engine manifest that failed a loader stage.

    ``reason``, ``field_path`` and ``message`` are the first issue's.
    ``schema_invalid`` issues sort ahead of the other reasons.
    """

    __slots__ = ()

    reason: EngineManifestRejection
    field_path: str | None
    message: str
    issues: list[EngineManifestIssue]

    def __init__(self, issues: list[EngineManifestIssue], summary: str = "") -> None:
        first = issues[0]
        super().__init__(summary or first.message)
        self.reason = first.reason
        self.field_path = first.field_path
        self.message = first.message
        self.issues = issues


@dataclass(frozen=True)
class LoadedEngineManifest:
    """A validated manifest and the file texts it references."""

    manifest: EngineManifest
    #: Role id to agent-spec text, in manifest role order.
    agent_specs: dict[str, str]
    #: The aggregation prompt file's text.
    aggregation_prompt: str


class _DuplicateKeyError(ValueError):
    """A JSON object repeated a key (raised from the parse hook)."""


def manifest_refusal_cause(error: EngineManifestError) -> ManifestCause:
    """``codex_not_declared`` iff the rejection is about ``backends``.

    Every other rejection is ``manifest_invalid``. A path or file failure never
    carries a ``backends`` field path, so the prefix check is unambiguous.
    """
    path = error.field_path
    if path is not None and (
        path == _BACKENDS_FIELD or path.startswith(f"{_BACKENDS_FIELD}.")
    ):
        return "codex_not_declared"
    return "manifest_invalid"


def _sanitize(text: str) -> str:
    """*text* with non-printable characters as ``?``, cut to the issue cap."""
    cleaned = "".join(char if char.isprintable() else "?" for char in text)
    return cleaned[:MAX_ISSUE_TEXT_CHARS]


def _issue(
    reason: EngineManifestRejection, message: str, field_path: str | None = None
) -> EngineManifestIssue:
    path = None if field_path is None else _sanitize(field_path)
    return EngineManifestIssue(reason, path, _sanitize(message))


def _reject(
    reason: EngineManifestRejection, message: str, field_path: str | None = None
) -> EngineManifestError:
    return EngineManifestError([_issue(reason, message, field_path)])


def _reject_all(issues: list[EngineManifestIssue]) -> EngineManifestError:
    """One error over *issues*, ``schema_invalid`` first, order otherwise kept."""
    ordered = sorted(
        issues,
        key=lambda item: item.reason is not EngineManifestRejection.SCHEMA_INVALID,
    )
    summary = ""
    if len(ordered) > 1:
        summary = f"{len(ordered)} validation issue(s); first: {ordered[0].message}"
    return EngineManifestError(ordered, summary)


def _read_capped(path: Path) -> bytes:
    """At most cap + 1 bytes of *path*, so a huge file is never fully read."""
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_ENGINE_MANIFEST_BYTES + 1)
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        msg = f"cannot read {path}: {detail}"
        raise _reject(EngineManifestRejection.UNREADABLE, msg) from exc


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate key {key!r}"
            raise _DuplicateKeyError(msg)
        result[key] = value
    return result


def _json_error_detail(exc: ValueError | RecursionError) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    if isinstance(exc, RecursionError):
        return "nesting is too deep"
    return str(exc)


def _decode_json_object(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_ENGINE_MANIFEST_BYTES:
        msg = f"manifest exceeds the {MAX_ENGINE_MANIFEST_BYTES}-byte cap"
        raise _reject(EngineManifestRejection.OVERSIZED, msg)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"manifest is not valid UTF-8 (byte {exc.start})"
        raise _reject(EngineManifestRejection.NOT_UTF8, msg) from exc
    try:
        payload: object = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError as exc:
        raise _reject(EngineManifestRejection.DUPLICATE_KEY, str(exc)) from exc
    except (ValueError, RecursionError) as exc:
        msg = f"manifest is not valid JSON: {_json_error_detail(exc)}"
        raise _reject(EngineManifestRejection.NOT_JSON, msg) from exc
    if not isinstance(payload, dict):
        kind = _JSON_TYPE_NAMES.get(type(payload), type(payload).__name__)
        msg = f"manifest must be a JSON object, got {kind}"
        raise _reject(EngineManifestRejection.NOT_OBJECT, msg)
    return payload


def _check_version(payload: dict[str, object]) -> None:
    """Fail closed on any version but the supported one, before field checks."""
    if _VERSION_KEY not in payload:
        detail = "is missing"
    else:
        value = payload[_VERSION_KEY]
        if isinstance(value, bool) or not isinstance(value, int):
            detail = f"has type {type(value).__name__}, not int"
        elif value != ENGINE_MANIFEST_VERSION:
            detail = f"{value} is not supported"
        else:
            return
    msg = f"{_VERSION_KEY} {detail} (expected {ENGINE_MANIFEST_VERSION})"
    raise _reject(
        EngineManifestRejection.UNSUPPORTED_MANIFEST_VERSION, msg, _VERSION_KEY
    )


def _issue_from(error: ErrorDetails) -> EngineManifestIssue:
    """One issue from one pydantic error; its ``input`` is never read."""
    path = ".".join(str(part) for part in error["loc"])
    return _issue(EngineManifestRejection.SCHEMA_INVALID, error["msg"], path or None)


def _validate_model(payload: dict[str, object]) -> EngineManifest:
    try:
        return EngineManifest.model_validate(payload)
    except ValidationError as exc:
        issues = [_issue_from(error) for error in exc.errors(include_input=False)]
        raise _reject_all(issues) from exc


def _check_slug_collisions(manifest: EngineManifest) -> None:
    """Reject roles whose ids normalize to the same slug (``a-b`` and ``a--b``)."""
    first_seen: dict[str, int] = {}
    issues: list[EngineManifestIssue] = []
    for index, role in enumerate(manifest.roles):
        slug = role_slug(role.id)
        if slug not in first_seen:
            first_seen[slug] = index
            continue
        msg = (
            f"role id normalizes to {slug!r}, "
            f"already used by roles.{first_seen[slug]}.id"
        )
        issues.append(
            _issue(EngineManifestRejection.INCONSISTENT, msg, f"roles.{index}.id")
        )
    if issues:
        raise _reject_all(issues)


def _lexical_refusal(ref: str) -> str | None:
    """Why *ref* is refused on its text alone, or ``None``."""
    pure = PurePosixPath(ref)
    if pure.is_absolute():
        return "path must be relative to the manifest directory"
    if ".." in pure.parts:
        return "path must not contain a '..' segment"
    return None


def _filesystem_refusal(base: Path, candidate: Path) -> str | None:
    """Why the resolved *candidate* is refused, or ``None``."""
    if not candidate.is_relative_to(base.resolve()):
        return "path resolves outside the manifest directory"
    if not candidate.exists():
        return "file does not exist"
    if not candidate.is_file():
        return "path is not a regular file"
    return None


def _resolve_ref(base: Path, ref: str) -> Path | str:
    """The real file *ref* names inside *base*, or the reason it is refused."""
    refusal = _lexical_refusal(ref)
    if refusal is not None:
        return refusal
    try:
        resolved = (base / ref).resolve()
        refusal = _filesystem_refusal(base, resolved)
    except (OSError, ValueError, RuntimeError):
        return "path cannot be resolved"
    return resolved if refusal is None else refusal


def _read_ref_text(path: Path) -> str | tuple[EngineManifestRejection, str]:
    """The file's text, or the rejection (reason, message) it earns."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_ENGINE_SPEC_BYTES + 1)
    except OSError:
        return EngineManifestRejection.PATH_INVALID, "file cannot be read"
    if len(raw) > MAX_ENGINE_SPEC_BYTES:
        msg = f"file exceeds the {MAX_ENGINE_SPEC_BYTES}-byte cap"
        return EngineManifestRejection.FILE_OVERSIZED, msg
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"file is not valid UTF-8 (byte {exc.start})"
        return EngineManifestRejection.FILE_NOT_UTF8, msg
    if not text.strip():
        return EngineManifestRejection.FILE_BLANK, "file is blank"
    return text


def _load_ref(base: Path, ref: str, field_path: str) -> str | EngineManifestIssue:
    """The text of the file *ref* names, or the issue explaining why not."""
    located = _resolve_ref(base, ref)
    if isinstance(located, str):
        return _issue(EngineManifestRejection.PATH_INVALID, located, field_path)
    read = _read_ref_text(located)
    if isinstance(read, str):
        return read
    reason, message = read
    return _issue(reason, message, field_path)


def _load_files(manifest: EngineManifest, base: Path) -> LoadedEngineManifest:
    """Read every referenced file; report every failure at once."""
    refs = [
        (role.id, role.agent_spec, f"roles.{index}.agent_spec")
        for index, role in enumerate(manifest.roles)
    ]
    refs.append(("", manifest.aggregation_prompt, _AGGREGATION_FIELD))
    texts: dict[str, str] = {}
    issues: list[EngineManifestIssue] = []
    for key, ref, field_path in refs:
        loaded = _load_ref(base, ref, field_path)
        if isinstance(loaded, EngineManifestIssue):
            issues.append(loaded)
        else:
            texts[key] = loaded
    if issues:
        raise _reject_all(issues)
    prompt = texts.pop("")
    return LoadedEngineManifest(
        manifest=manifest, agent_specs=texts, aggregation_prompt=prompt
    )


def load_engine_manifest(manifest_path: Path) -> LoadedEngineManifest:
    """Read and validate the manifest at *manifest_path*, or raise.

    Files the manifest names are read relative to ``manifest_path.parent``.
    Raises :class:`EngineManifestError`.
    """
    payload = _decode_json_object(_read_capped(manifest_path))
    _check_version(payload)
    manifest = _validate_model(payload)
    _check_slug_collisions(manifest)
    return _load_files(manifest, manifest_path.parent)
