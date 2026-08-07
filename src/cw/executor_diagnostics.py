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
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationInfo, field_validator

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

# Closed taxonomy of executor backends (#1330 item 3) — matches the sibling
# ExecutorFailureCategory's use of Literal (not StrEnum) as this file's local
# convention for closed string taxonomies.
ExecutorName = Literal["codex", "aider", "claude", "opencode"]

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


_REDACTED_MESSAGE_RE = re.compile(r"^<redacted: \d+ chars>$")

# Minimum argv length for opencode prompt redaction: binary + subcommand + prompt.
_OPENCODE_MIN_REDACT_ARGV = 3


def redact_argv(argv: list[str], *, executor_name: ExecutorName) -> list[str]:
    """Return an executor-appropriate sanitized copy of *argv*.

    Codex/Claude argvs are content-free (the prompt travels over stdin), so
    they pass through unchanged. Aider embeds the full ticket+plan text in its
    ``--message`` value; opencode embeds it as the trailing positional argument.
    Both are replaced wholesale with a ``<redacted: N chars>`` placeholder
    rather than regex-scrubbed — selectively redacting free-form ticket text is
    too risky, so it is dropped entirely.

    Idempotent (#1330 item 3/4): after one redaction, the redacted value
    matches ``_REDACTED_MESSAGE_RE``, so a second pass must not re-wrap the
    already-redacted placeholder — that would silently corrupt the recorded
    original length. This guard matters both for a caller that accidentally
    redacts twice, and for ``ExecutorFailure.model_validate_json`` reloading an
    already-persisted bundle through the model's own ``argv_sanitized``
    field_validator.
    """
    if executor_name == "aider" and "--message" in argv:
        out = list(argv)
        idx = out.index("--message")
        if idx + 1 < len(out) and not _REDACTED_MESSAGE_RE.match(out[idx + 1]):
            value = out[idx + 1]
            out[idx + 1] = f"<redacted: {len(value)} chars>"
        return out
    if executor_name == "opencode" and len(argv) >= _OPENCODE_MIN_REDACT_ARGV:
        out = list(argv)
        last_idx = len(out) - 1
        if not out[last_idx].startswith("-") and not _REDACTED_MESSAGE_RE.match(
            out[last_idx]
        ):
            value = out[last_idx]
            out[last_idx] = f"<redacted: {len(value)} chars>"
        return out
    return list(argv)


class ExecutorFailure(BaseModel):
    """A typed, sanitized record of one executor failure (#1239)."""

    category: ExecutorFailureCategory
    executor_name: ExecutorName
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

    @field_validator("argv_sanitized")
    @classmethod
    def _sanitize_argv(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """Redact *value* per-executor at construction time (#1330 item 4).

        Single point of enforcement: callers pass raw argv and this validator
        applies :func:`redact_argv`, so a caller can no longer forget to
        redact. ``executor_name`` is declared before ``argv_sanitized`` in this
        class body, so Pydantic v2's declaration-order validation guarantees
        ``info.data["executor_name"]`` is already populated here (unless that
        field itself failed validation, in which case it is absent and *value*
        passes through unredacted rather than raising a second error).
        """
        executor_name = info.data.get("executor_name")
        if executor_name is None:
            return value
        return redact_argv(value, executor_name=executor_name)


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
    ``cw.codex_review._verdict``'s ``_format_failures_detail``, and the
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
    failure: ExecutorFailure,
    scratch_schema_path: Path | None = None,
    scratch_output_path: Path | None = None,
) -> Path:
    """Write the diagnostics bundle for one failure; return the bundle dir.

    Writes ``<role_slug>-<category>-<timestamp>.json`` (the sanitized
    :class:`ExecutorFailure`) and, when the scratch paths are provided and
    exist, unredacted ``<role_slug>-<category>-<timestamp>-schema.json`` /
    ``-output.json`` copies. ``<category>`` is ``failure.category`` itself —
    there is no separate ``reason`` parameter to drift from it (#1330 item 1).
    ``<timestamp>`` is ``failure.occurred_at`` (microsecond precision),
    disambiguating repeat same-role/same-category failures within one session
    so they no longer silently overwrite each other (#1330 item 7).

    Never raises: on any :class:`OSError` it logs a WARNING and returns the
    (possibly partially-written) bundle dir, so a diagnostics-write failure can
    never block the blocked-result return path that called it.
    """
    bundle = diagnostics_bundle_dir(session_id)
    timestamp = failure.occurred_at.strftime("%Y%m%dT%H%M%S%f")
    stem = f"{role_slug}-{failure.category}-{timestamp}"
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


# Throttle for cleanup_expired_diagnostics (#1330 item 8): a persisted
# sentinel *file* under state_dir() (not a module-level Python global, which
# would leak across tests in the same pytest process and need a manual reset
# hook). Its mtime plays the same "persisted data" role pr_hydrate.py's
# _throttled() gets from PrState.hydrated_at — no equivalent model field
# exists here, so a dedicated file stands in for one.
_CLEANUP_SENTINEL_NAME = ".diagnostics_cleanup_last_run"
_CLEANUP_MIN_INTERVAL_SECONDS = 3600  # 1 hour


def _cleanup_recently_run(sentinel: Path, *, now: float) -> bool:
    """Return True when *sentinel*'s mtime is within the throttle window of *now*."""
    try:
        last_run = sentinel.stat().st_mtime
    except OSError:
        return False
    return now - last_run < _CLEANUP_MIN_INTERVAL_SECONDS


def _touch_cleanup_sentinel(sentinel: Path, *, now: float) -> None:
    """Best-effort: stamp *sentinel* with *now*, creating it if absent.

    Uses ``os.utime`` after ``touch()`` so the recorded mtime is derived from
    the same ``datetime.now(UTC)`` value the throttle check compares against,
    rather than whatever real wall-clock time the OS would otherwise stamp —
    this keeps the pair exercisable under ``freeze_time`` in tests. Never
    raises: a failure here only means the next call isn't throttled, which is
    the safe direction to fail in.
    """
    try:
        sentinel.touch()
        os.utime(sentinel, (now, now))
    except OSError:
        pass


def cleanup_expired_diagnostics(*, retention_hours: int) -> int:
    """Remove diagnostics bundles whose newest file is older than the window.

    Scans ``state_dir()/sessions/*/diagnostics/`` and ``rmtree``s any bundle
    dir whose newest-file mtime predates ``retention_hours`` ago. Returns the
    count removed. Never raises (every OSError is swallowed); logs one summary
    line only when the count is non-zero.

    Internally throttled (#1330 item 8): a full filesystem walk only runs once
    per ``_CLEANUP_MIN_INTERVAL_SECONDS`` (1 hour), tracked via a sentinel
    file's mtime under ``state_dir()``. Callers (``_sweep_expired_diagnostics``
    in ``dispatch/tick.py``) call this unconditionally every tick; the throttle
    is invisible to them — a throttled call simply returns ``0``.
    """
    sessions_root = state_dir() / "sessions"
    if not sessions_root.exists():
        return 0
    now = datetime.now(UTC).timestamp()
    sentinel = state_dir() / _CLEANUP_SENTINEL_NAME
    if _cleanup_recently_run(sentinel, now=now):
        return 0
    cutoff = now - retention_hours * 3600
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
    _touch_cleanup_sentinel(sentinel, now=now)
    return removed


def build_executor_failure(
    *,
    category: ExecutorFailureCategory,
    executor_name: ExecutorName,
    session_id: str,
    argv: list[str],
    stdout_excerpt: str,
    stderr_excerpt: str,
    reviewer_role: str | None = None,
    duration_seconds: float | None = None,
    exit_code: int | None = None,
    structured_output_excerpt: str | None = None,
) -> ExecutorFailure:
    """Construct an :class:`ExecutorFailure`, applying the shared defaults.

    Replaces 3 near-identical ``ExecutorFailure(...)`` construction blocks that
    each independently hardcoded ``executor_version=None``, ``run_id=None``,
    and ``occurred_at=datetime.now(UTC)`` (#1330 item 2). *argv* is passed
    through raw — the model's own ``argv_sanitized`` field_validator applies
    :func:`redact_argv`, so callers no longer pre-redact.
    """
    return ExecutorFailure(
        category=category,
        executor_name=executor_name,
        executor_version=None,
        reviewer_role=reviewer_role,
        argv_sanitized=argv,
        duration_seconds=duration_seconds,
        exit_code=exit_code,
        session_id=session_id,
        run_id=None,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        structured_output_excerpt=structured_output_excerpt,
        occurred_at=datetime.now(UTC),
    )
