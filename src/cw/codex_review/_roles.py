"""Per-role codex execution and failure classification for the codex-review package.

Runs each selected reviewer role as a generic ``codex exec`` call under one
shared wall-clock deadline (Comment 3, #1236), writing an OpenAI strict-mode
schema and reading back the ``-o`` output document. Classifies every failure
into the fine-grained #1239 diagnostics taxonomy, persists a typed diagnostics
bundle, and maps back to the coarse ``ReviewerRunFailure.reason`` vocabulary.

``time`` and ``uuid`` are imported at module scope here (not via ``from``) so
tests can monkeypatch ``cw.codex_review._roles.time.monotonic`` /
``cw.codex_review._roles.uuid.uuid4``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from typing import TYPE_CHECKING

from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
)
from cw.codex_review._context import _parse_reviewer_document
from cw.config import state_dir
from cw.executor_diagnostics import build_executor_failure, persist_diagnostics_bundle
from cw.openai_strict_schema import to_openai_strict_schema
from cw.review_findings import ReviewerFindingsDocument, ReviewerRunFailure

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.executor_diagnostics import ExecutorFailureCategory

_log = logging.getLogger(__name__)

# Exit code Popen/RealCodexRunner reports when the codex binary is not on PATH
# (FileNotFoundError → CodexRunResult(returncode=127, ...)); paired with a
# "command not found" stderr it classifies as a spawn_error (#1239).
_COMMAND_NOT_FOUND_RETURNCODE = 127


def _codex_scratch_dir(session_id: str) -> Path:
    """Return (creating if needed) a per-run scratch dir under ``state_dir()``.

    Snap-confined codex cannot read ``/tmp`` (snap private tmp namespace), so
    schema/output file paths handed to ``codex exec --output-schema ... -o ...``
    MUST live under the user's home tree. This replaces the ``executor.py`` codex
    path's former ``tempfile.TemporaryDirectory()`` (which resolves under
    ``/tmp`` on this host) with a directory under ``state_dir()``.
    """
    scratch = state_dir() / "codex-review" / session_id
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _build_generic_codex_argv(
    *, model: str | None, schema_path: Path, output_path: Path
) -> list[str]:
    """Return the generic ``codex exec`` argv (no ``review``/``--base``).

    ``--sandbox read-only`` (ticket AC, MUST_FIX 4, #1236): every reviewer
    input is inlined into the prompt over stdin — a reviewer role has no
    legitimate reason to write to the worktree, so it never gets write
    access, matching the pre-#1236 ``codex exec review`` path's implicit
    read-only posture.
    """
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model:
        argv += ["-m", model]
    return argv


def _slug(role: str) -> str:
    """Filesystem-safe slug for a reviewer role name."""
    return re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")


def _classify_codex_output_failure(content: str | None) -> ExecutorFailureCategory:
    """Classify an unparseable codex ``-o`` output into a typed category (#1239).

    Split from :func:`_classify_codex_failure` to keep both under the PLR0911
    return cap. Only called once the process exited 0, so the output itself is
    the sole failure source: ``missing_output`` / ``empty_output`` /
    ``invalid_json`` / ``schema_mismatch``. A genuinely valid document never
    reaches here (the caller returned it), so the final return is unreachable.
    """
    if content is None:
        return "missing_output"
    if not content.strip():
        return "empty_output"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return "invalid_json"
    try:
        ReviewerFindingsDocument.model_validate(data)
    except ValueError:
        return "schema_mismatch"
    return "schema_mismatch"  # pragma: no cover — unreachable on the failure path


def _classify_codex_failure(result: CodexRunResult) -> ExecutorFailureCategory:
    """Map a failed :class:`CodexRunResult` to a typed failure category (#1239).

    The single source of truth ``_run_codex_role`` delegates to (#1330 item 5)
    for the timed_out -> returncode -> output-parse ordering, refining the
    coarse ``ReviewerRunFailure.reason`` into the finer diagnostics taxonomy: a
    non-zero exit is split into ``spawn_error`` (codex binary missing) vs
    ``nonzero_exit``, and an unparseable output is delegated to
    :func:`_classify_codex_output_failure`.
    """
    if result.timed_out:
        return "timeout"
    if (
        result.returncode == _COMMAND_NOT_FOUND_RETURNCODE
        and "command not found" in result.stderr
    ):
        return "spawn_error"
    if result.returncode != 0:
        return "nonzero_exit"
    return _classify_codex_output_failure(result.output_file_content)


def _persist_codex_role_diagnostics(
    *,
    session_id: str,
    role: str,
    category: ExecutorFailureCategory,
    result: CodexRunResult,
    argv: list[str],
    duration_seconds: float,
    schema_path: Path,
    output_path: Path,
) -> None:
    """Build the typed :class:`ExecutorFailure` and write its diagnostics bundle.

    *category* is classified once by the caller (``_run_codex_role``) and
    threaded through here rather than re-derived via a second
    ``_classify_codex_failure(result)`` call (#1330 item 5).
    """
    failure = build_executor_failure(
        category=category,
        executor_name="codex",
        session_id=session_id,
        # Codex argv is content-free (prompt travels over stdin); the model's
        # own argv_sanitized field_validator leaves it unchanged, kept as raw
        # here for symmetry with the aider call sites.
        argv=argv,
        stdout_excerpt=result.stdout,
        stderr_excerpt=result.stderr,
        reviewer_role=role,
        duration_seconds=duration_seconds,
        exit_code=result.returncode,
        structured_output_excerpt=result.output_file_content,
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug=_slug(role),
        failure=failure,
        scratch_schema_path=schema_path,
        scratch_output_path=output_path,
    )


def _run_codex_role(
    *,
    runner: CodexRunner,
    worktree: Path,
    role: str,
    prompt: str,
    model: str | None,
    timeout_seconds: int | None,
    scratch_dir: Path,
    session_id: str,
) -> tuple[ReviewerFindingsDocument | None, ReviewerRunFailure | None]:
    """Run one reviewer role; return ``(document, failure)`` (exactly one set).

    Logs each failure mode (timeout, non-zero exit, missing/malformed output)
    via ``_log.warning`` before constructing the ``ReviewerRunFailure``, and
    persists a typed diagnostics bundle (classified into the finer #1239
    taxonomy) under ``session_id``'s diagnostics dir on every failure branch.
    """
    slug = _slug(role)
    schema_path = scratch_dir / f"{slug}-schema.json"
    output_path = scratch_dir / f"{slug}-output.json"
    schema_path.write_text(
        json.dumps(
            to_openai_strict_schema(ReviewerFindingsDocument.model_json_schema())
        ),
        encoding="utf-8",
    )
    argv = _build_generic_codex_argv(
        model=model, schema_path=schema_path, output_path=output_path
    )
    start = time.monotonic()
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    duration = time.monotonic() - start

    if not result.timed_out and result.returncode == 0:
        doc = _parse_reviewer_document(result.output_file_content)
        if doc is not None:
            return doc, None

    category = _classify_codex_failure(result)
    reason = _CATEGORY_TO_REASON[category]
    _log.warning("codex review role %r failed: %s (%s)", role, reason, category)
    _persist_codex_role_diagnostics(
        session_id=session_id,
        role=role,
        category=category,
        result=result,
        argv=argv,
        duration_seconds=duration,
        schema_path=schema_path,
        output_path=output_path,
    )
    return None, ReviewerRunFailure(role=role, reason=reason)


def run_codex_roles(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> tuple[list[ReviewerFindingsDocument], list[ReviewerRunFailure]]:
    """Run every role under one shared wall-clock deadline (Comment 3).

    A ``None`` budget means no deadline (unlimited per-role timeout). Otherwise a
    single deadline is computed once; each role gets the remaining budget (never
    below ``_MIN_ROLE_TIMEOUT_SECONDS``), and a role that cannot get at least the
    floor is skipped as ``budget_exhausted`` — mandatory roles that already ran
    are unaffected.

    The per-run scratch dir (schema/output files, see ``_codex_scratch_dir``)
    is removed before returning, success or failure — it lives under the
    shared, long-running ``state_dir()``, not an auto-cleaning
    ``tempfile.TemporaryDirectory()``, so leaving it behind on every call
    leaks disk on a long-running dispatch host (MUST_FIX 1, #1236).
    """
    scratch_dir = _codex_scratch_dir(uuid.uuid4().hex)
    try:
        documents: list[ReviewerFindingsDocument] = []
        failures: list[ReviewerRunFailure] = []
        deadline: float | None = (
            None
            if wall_clock_budget_seconds is None
            else time.monotonic() + wall_clock_budget_seconds
        )
        for role in roles:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= _MIN_ROLE_TIMEOUT_SECONDS:
                    _log.warning("codex review role %r skipped: budget exhausted", role)
                    failures.append(
                        ReviewerRunFailure(role=role, reason=CODEX_BUDGET_EXHAUSTED)
                    )
                    continue
                timeout: int | None = max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)
            else:
                timeout = None
            doc, failure = _run_codex_role(
                runner=runner,
                worktree=worktree,
                role=role,
                prompt=prompts_by_role[role],
                model=model,
                timeout_seconds=timeout,
                scratch_dir=scratch_dir,
                session_id=session_id,
            )
            if doc is not None:
                documents.append(doc)
            if failure is not None:
                failures.append(failure)
        return documents, failures
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
