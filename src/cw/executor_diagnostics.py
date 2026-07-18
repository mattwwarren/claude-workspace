"""Typed failure diagnostics and artifacts for executors (#1239).

A backend-neutral home for the structured record cw writes when an executor
(codex reviewer role, aider impl run, or any future backend) fails to produce a
usable result. The :class:`ExecutorFailure` model captures the classified
failure category, the sanitized invocation, bounded output excerpts, and timing;
:func:`persist_diagnostics_bundle` writes it — plus copies of the raw scratch
schema/output files — under ``state_dir()/sessions/<id>/diagnostics/`` for local
post-mortem, and :func:`cleanup_expired_diagnostics` reaps bundles past a
retention window.

Two data tiers, deliberately kept separate (see the plan's §2 redaction notes):

- The ``ExecutorFailure`` JSON and the summary that flows into ``Blocker.details``
  are the *sanitized* tier — ``argv_sanitized`` is executor-specifically redacted
  (:func:`redact_argv`), and the output excerpts are secret-scrubbed
  (:func:`redact`) and length-bounded (:func:`_bounded`).
- The ``*-schema.json`` / ``*-output.json`` copies are the *raw* tier — full,
  unredacted, state-dir-only, never echoed to stdout or GitHub by this ticket's
  code paths.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from cw.config import diagnostics_dir, state_dir

_log = logging.getLogger(__name__)

# Closed taxonomy of executor failure categories. semantic_validation_failure
# is reserved: no live producer emits it yet (it is the future home for a
# result that parsed and matched schema but failed a semantic gate), but the
# category exists so downstream consumers can branch on it without a later
# schema bump. See GitHub #1239.
ExecutorFailureCategory = Literal[
    "spawn_error",
    "runtime_error",
    "timeout",
    "nonzero_exit",
    "missing_output",
    "empty_output",
    "invalid_json",
    "schema_mismatch",
    "semantic_validation_failure",
]

# Every bounded excerpt field is capped at this many characters. Matches
# codex_runner.py's stderr[-4000:] and local_runner's _AIDER_LOG_TAIL_CHARS
# conventions so the three never drift onto different caps.
_EXCERPT_LIMIT = 4000

_REDACTION_PLACEHOLDER = "<redacted>"

# Common secret shapes. Conservative and false-positive-tolerant: over-redacting
# a local diagnostics artifact is cheaper than leaking a token. The generic
# high-entropy rule only fires for a 32+ char run immediately preceded by ``=``
# or ``:`` (an assignment/header shape), so ordinary file paths are left alone.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?<=[=:])[A-Za-z0-9_-]{32,}"),
)


def _bounded(text: str) -> str:
    """Return *text* capped at :data:`_EXCERPT_LIMIT`, keeping the tail.

    When *text* exceeds the cap, the newest ``_EXCERPT_LIMIT`` characters are
    kept and a ``...[truncated, N chars omitted]...\\n`` marker is prepended so
    a reader knows the head was dropped (the tail carries the failure's last
    output, which is the diagnostically useful part).
    """
    if len(text) <= _EXCERPT_LIMIT:
        return text
    omitted = len(text) - _EXCERPT_LIMIT
    return f"...[truncated, {omitted} chars omitted]...\n{text[-_EXCERPT_LIMIT:]}"


def redact(text: str) -> str:
    """Replace known secret shapes in *text* with a redaction placeholder."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTION_PLACEHOLDER, text)
    return text


def redact_argv(argv: list[str], *, executor_name: str) -> list[str]:
    """Return an executor-appropriate sanitized copy of *argv*.

    Codex/Claude argvs are content-free (the prompt travels over stdin), so
    they pass through unchanged. Aider embeds the full ticket+plan text in its
    ``--message`` value; that value is replaced wholesale with a
    ``<redacted: N chars>`` placeholder rather than regex-scrubbed — selectively
    redacting free-form ticket text is too risky, so it is dropped entirely.
    """
    if executor_name != "aider" or "--message" not in argv:
        return list(argv)
    out = list(argv)
    idx = out.index("--message")
    if idx + 1 < len(out):
        value = out[idx + 1]
        out[idx + 1] = f"<redacted: {len(value)} chars>"
    return out


class ExecutorFailure(BaseModel):
    """A typed, sanitized record of one executor failure (#1239)."""

    category: ExecutorFailureCategory
    executor_name: str  # "codex" | "aider" | "claude"
    # Best-effort; None means "not cheaply available", NOT "unknown error".
    # Populated only when the executor already exposes it via a cheap cached
    # source (e.g. a capability-probe/doctor CheckResult) — never shelled out
    # for at failure time. No such infrastructure exists on main today, so
    # every current call site leaves this None (cf. the cost_usd convention).
    executor_version: str | None = None
    reviewer_role: str | None  # None for non-review stages (aider/claude impl)
    argv_sanitized: list[str]  # redacted per-executor (see redact_argv)
    duration_seconds: float | None
    exit_code: int | None
    session_id: str  # cw session id
    # Executor-native session/run identifier when available; None for
    # codex/aider today.
    run_id: str | None = None
    stdout_excerpt: str  # secret-scrubbed + bounded to _EXCERPT_LIMIT chars
    stderr_excerpt: str  # secret-scrubbed + bounded to _EXCERPT_LIMIT chars
    structured_output_excerpt: str | None = None  # scrubbed + bounded when set
    occurred_at: datetime

    @field_validator("stdout_excerpt", "stderr_excerpt")
    @classmethod
    def _scrub_and_bound(cls, value: str) -> str:
        return _bounded(redact(value))

    @field_validator("structured_output_excerpt")
    @classmethod
    def _scrub_and_bound_optional(cls, value: str | None) -> str | None:
        return _bounded(redact(value)) if value is not None else None


def diagnostics_bundle_dir(session_id: str) -> Path:
    """Return the per-session diagnostics bundle dir (wraps config accessor)."""
    return diagnostics_dir(session_id)


def render_bundle_path(session_id: str) -> str:
    """Render *session_id*'s diagnostics bundle dir as a stable, short path.

    Home-relative when the bundle sits under the user's home dir, absolute
    otherwise (e.g. an XDG-relocated or tmp state dir under test) — the
    rendering never raises. Local-only pointer, no secrets — safe for
    ``Blocker.details``. Shared by every executor backend (codex/aider) so
    they all use one rendering rule (#1239).
    """
    bundle = diagnostics_bundle_dir(session_id)
    try:
        return str(bundle.relative_to(Path.home()))
    except ValueError:
        return str(bundle)


def append_diagnostics_pointer(detail: str, *, session_id: str) -> str:
    """Append a ``[diagnostics: <bundle path>]`` pointer to *detail*.

    Shared by every executor path (codex per-role failures via
    ``codex_review.py``'s ``_format_failures_detail``, and the
    LocalExecutor/aider paths) so a blocked sentinel's ``Blocker.details``
    always points an operator at the on-disk diagnostics artifacts (#1239).
    When *detail* is empty (e.g. an unreadable aider.log), the pointer is
    returned bare rather than with a leading space.
    """
    pointer = f"[diagnostics: {render_bundle_path(session_id)}]"
    return f"{detail} {pointer}" if detail else pointer


def persist_diagnostics_bundle(
    *,
    session_id: str,
    role_slug: str,
    reason: str,
    failure: ExecutorFailure,
    scratch_schema_path: Path | None = None,
    scratch_output_path: Path | None = None,
) -> Path:
    """Write the diagnostics bundle for one failure; return the bundle dir.

    Writes ``<role_slug>-<reason>.json`` (the sanitized :class:`ExecutorFailure`)
    and, when the scratch paths are provided and exist, unredacted
    ``<role_slug>-<reason>-schema.json`` / ``-output.json`` copies.

    Never raises: on any :class:`OSError` it logs a WARNING and returns the
    (possibly partially-written) bundle dir, so a diagnostics-write failure can
    never block the blocked-result return path that called it.
    """
    bundle = diagnostics_bundle_dir(session_id)
    stem = f"{role_slug}-{reason}"
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / f"{stem}.json").write_text(
            failure.model_dump_json(indent=2), encoding="utf-8"
        )
        if scratch_schema_path is not None and scratch_schema_path.exists():
            shutil.copy2(scratch_schema_path, bundle / f"{stem}-schema.json")
        if scratch_output_path is not None and scratch_output_path.exists():
            shutil.copy2(scratch_output_path, bundle / f"{stem}-output.json")
    except OSError as exc:
        _log.warning(
            "diagnostics bundle write failed for session %s: %s", session_id, exc
        )
    return bundle


def cleanup_expired_diagnostics(*, retention_hours: int) -> int:
    """Remove diagnostics bundles whose newest file is older than the window.

    Scans ``state_dir()/sessions/*/diagnostics/`` and ``rmtree``s any bundle
    dir whose newest-file mtime predates ``retention_hours`` ago. Returns the
    count removed. Never raises (every OSError is swallowed); logs one summary
    line only when the count is non-zero.
    """
    sessions_root = state_dir() / "sessions"
    if not sessions_root.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - retention_hours * 3600
    removed = 0
    removed_session_ids: list[str] = []
    try:
        session_dirs = list(sessions_root.iterdir())
    except OSError:
        return 0
    for session_dir in session_dirs:
        bundle = session_dir / "diagnostics"
        if not bundle.is_dir():
            continue
        try:
            files = list(bundle.iterdir())
            newest = max((f.stat().st_mtime for f in files), default=0.0)
        except OSError:
            continue
        if newest >= cutoff:
            continue
        try:
            shutil.rmtree(bundle)
        except OSError:
            continue
        removed += 1
        removed_session_ids.append(session_dir.name)
    if removed:
        _log.warning(
            "diagnostics cleanup removed %d bundle(s) older than %dh: %s",
            removed,
            retention_hours,
            ", ".join(removed_session_ids),
        )
    return removed
