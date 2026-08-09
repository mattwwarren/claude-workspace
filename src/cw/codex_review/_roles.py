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

from cw.codex_review._audit_events import (
    _TURN_COMPLETED,
    _TURN_FAILED,
    _parse_codex_audit_events,
)
from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    _is_spawn_error,
)
from cw.codex_review._context import _parse_reviewer_document
from cw.codex_review._profile import (
    _lean_profile_argv,
    _persist_profile_diagnostics,
    _probe_runtime_cli_version,
)
from cw.config import state_dir
from cw.executor_diagnostics import build_executor_failure, persist_diagnostics_bundle
from cw.openai_strict_schema import to_openai_strict_schema
from cw.review_findings import (
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewerRunMetrics,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review._context import _InstructionSource
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.executor_diagnostics import ExecutorFailureCategory

_log = logging.getLogger(__name__)

# The two audit flags #1710 adds to every generic reviewer invocation. Kept as
# one tuple so the argv builder and the degrade-and-retry strip cannot drift.
_AUDIT_ARGV_FLAGS = ("--json", "--ephemeral")

# clap-style "you passed a flag I don't know" phrasings. An older codex-cli
# that predates --json/--ephemeral rejects the whole invocation with one of
# these plus the offending flag name; both must be present before
# _run_codex_role degrades and retries (see _is_audit_flag_rejection).
_FLAG_REJECTION_MARKERS = (
    "unexpected argument",
    "unrecognized argument",
    "unrecognized option",
    "unknown argument",
    "unknown option",
)

# A healthy ``codex exec --json`` stream ends on one of these. Anything else
# (or nothing at all) means the stream was truncated or never produced. Built
# from _audit_events's own wire-name constants so the two modules cannot drift
# on codex's terminal-event vocabulary (#1710 review finding).
_TERMINAL_EVENTS = frozenset({_TURN_COMPLETED, _TURN_FAILED})


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
    *,
    model: str | None,
    schema_path: Path,
    output_path: Path,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Return the generic ``codex exec`` argv (no ``review``/``--base``).

    ``--sandbox read-only`` (ticket AC, MUST_FIX 4, #1236): every reviewer
    input is inlined into the prompt over stdin — a reviewer role has no
    legitimate reason to write to the worktree, so it never gets write
    access, matching the pre-#1236 ``codex exec review`` path's implicit
    read-only posture.

    ``--json`` + ``--ephemeral`` (#1710): the former makes codex print its
    run as a JSONL event stream on stdout (parsed by ``_audit_events`` into
    per-role telemetry), the latter stops a one-shot reviewer invocation from
    persisting a resumable session file under ``~/.codex/sessions``. Both are
    unconditional and independent of the ``-o`` document, which is still
    written normally. They are inserted before ``--output-schema`` so the
    trailing ``-m <model>`` append contract stays intact.

    The lean-profile block (#1711, :func:`_lean_profile_argv`) sits between the
    sandbox pair and the audit flags, so it is equally outside
    ``_AUDIT_ARGV_FLAGS`` — the degrade-and-retry strip in
    :func:`_run_codex_role` removes only the audit flags and therefore never
    weakens the profile. ``reasoning_effort=None`` (the builder default) means
    "do not pin it"; the ``"high"`` default lives on ``StageExecutorConfig``,
    where lane/client overrides can reach it.
    """
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        *_lean_profile_argv(reasoning_effort=reasoning_effort),
        *_AUDIT_ARGV_FLAGS,
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
    if _is_spawn_error(result):
        return "spawn_error"
    if result.returncode != 0:
        return "nonzero_exit"
    return _classify_codex_output_failure(result.output_file_content)


def _is_audit_flag_rejection(result: CodexRunResult) -> bool:
    """True when *result* looks like codex rejecting ``--json``/``--ephemeral``.

    A codex-cli older than the one this feature was built against does not know
    these two flags and refuses the whole invocation, which would otherwise turn
    an observability upgrade into a total loss of review coverage. This narrow
    predicate gates a single degrade-and-retry in :func:`_run_codex_role`.

    Both conditions are required: stderr must carry a clap-style rejection
    marker **and** name one of the two flags we actually added. An unrelated
    non-zero exit whose stderr merely says "unrecognized" for some other reason
    must not be retried, and neither must one that only echoes our argv back.

    This is a heuristic — no capture of a real older codex-cli's exact wording
    was available (Deferred Premise 2, #1710). A false negative is safe: the
    role fails exactly as it did before this ticket, which is today's behavior
    for any ``nonzero_exit``.
    """
    if result.timed_out or result.returncode == 0:
        return False
    lowered = result.stderr.lower()
    return any(marker in lowered for marker in _FLAG_REJECTION_MARKERS) and any(
        flag in result.stderr for flag in _AUDIT_ARGV_FLAGS
    )


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
    reasoning_effort: str | None = None,
) -> tuple[
    ReviewerFindingsDocument | None, ReviewerRunFailure | None, ReviewerRunMetrics
]:
    """Run one reviewer role; return ``(document, failure, metrics)``.

    Exactly one of ``document``/``failure`` is set. ``metrics`` is always set
    (#1710): the audit telemetry parsed from the ``codex exec --json`` stream,
    degrading to all-defaults when no parseable stream was produced. It is
    returned on both branches — a role that failed mid-run still has telemetry
    worth recording.

    Logs each failure mode (timeout, non-zero exit, missing/malformed output)
    via ``_log.warning`` before constructing the ``ReviewerRunFailure``, and
    persists a typed diagnostics bundle (classified into the finer #1239
    taxonomy) under ``session_id``'s diagnostics dir on every failure branch.

    Degrade-and-retry (#1710): when the first invocation fails specifically
    because codex did not recognize ``--json``/``--ephemeral``
    (:func:`_is_audit_flag_rejection`), the two flags are stripped from
    ``argv`` and the role is run **once** more, so an older codex-cli loses the
    audit stream rather than the review itself. The retry's result replaces the
    first for every downstream step, and ``argv`` is reassigned in place so a
    subsequent failure's diagnostics name what actually ran. Every other
    failure — a timeout, a spawn error, an ordinary non-zero exit — falls
    through to the pre-existing path untouched.
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
        model=model,
        schema_path=schema_path,
        output_path=output_path,
        reasoning_effort=reasoning_effort,
    )
    start = time.monotonic()
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    if (
        not result.timed_out
        and result.returncode != 0
        and _classify_codex_failure(result) == "nonzero_exit"
        and _is_audit_flag_rejection(result)
    ):
        argv = [flag for flag in argv if flag not in _AUDIT_ARGV_FLAGS]
        result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    # Exactly two time.monotonic() reads per call, retry or not (a retry is a
    # second subprocess, not a second clock read) — so *duration* is total wall
    # time across both invocations when one happened. The invariant is asserted
    # by the _Clock-driven tests in tests/test_codex_review_roles.py.
    duration = time.monotonic() - start
    metrics = _parse_codex_audit_events(result.stdout, duration_seconds=duration)
    # No codex-cli event carries a model field, so the audit parse always
    # leaves this None (#1710). The model cw resolved and passed on the argv is
    # the authoritative answer to "which model reviewed this" (#1711), so it is
    # stamped here rather than left unanswerable. Set on both the success and
    # failure branches below — a role that died mid-run still ran under a model.
    metrics["effective_model"] = model

    if not result.timed_out and result.returncode == 0:
        doc = _parse_reviewer_document(result.output_file_content)
        if doc is not None:
            if (
                _AUDIT_ARGV_FLAGS[0] in argv
                and metrics["terminal_event"] not in _TERMINAL_EVENTS
            ):
                # A retry (no --json) legitimately has no terminal event, so the
                # argv guard is what keeps this warning specific to a genuinely
                # malformed stream.
                _log.warning(
                    "codex review role %r (session %r) produced a malformed/"
                    "incomplete JSONL audit stream (terminal_event=%r)",
                    role,
                    session_id,
                    metrics["terminal_event"],
                )
            return doc, None, metrics

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
    return None, ReviewerRunFailure(role=role, reason=reason), metrics


def run_codex_roles(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    reasoning_effort: str | None = None,
    instruction_sources: list[_InstructionSource] | None = None,
    profile_diagnostics_discriminator: str | None = None,
) -> tuple[
    list[ReviewerFindingsDocument],
    list[ReviewerRunFailure],
    dict[str, ReviewerRunMetrics],
]:
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

    The third return element maps each role that actually invoked codex to its
    audit telemetry (#1710). A ``budget_exhausted`` skip never calls
    :func:`_run_codex_role`, so it correctly contributes no entry — "no
    telemetry" and "telemetry showing nothing happened" are different facts.

    ``reasoning_effort``/``instruction_sources`` (#1711) describe the profile
    this whole pass ran under, so the diagnostics artifact is written exactly
    once per invocation — before the role loop, so a pass that dies partway
    through still records what it was configured to be. ``reasoning_effort`` is
    additionally threaded into every role's argv.
    """
    cli_version = _probe_runtime_cli_version()
    _persist_profile_diagnostics(
        session_id=session_id,
        model=model,
        reasoning_effort=reasoning_effort,
        cli_version=cli_version,
        instruction_sources=instruction_sources,
        pass_discriminator=profile_diagnostics_discriminator,
    )
    scratch_dir = _codex_scratch_dir(uuid.uuid4().hex)
    try:
        documents: list[ReviewerFindingsDocument] = []
        failures: list[ReviewerRunFailure] = []
        metrics_by_role: dict[str, ReviewerRunMetrics] = {}
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
            doc, failure, metrics = _run_codex_role(
                runner=runner,
                worktree=worktree,
                role=role,
                prompt=prompts_by_role[role],
                model=model,
                timeout_seconds=timeout,
                scratch_dir=scratch_dir,
                session_id=session_id,
                reasoning_effort=reasoning_effort,
            )
            metrics_by_role[role] = metrics
            if doc is not None:
                documents.append(doc)
            if failure is not None:
                failures.append(failure)
        return documents, failures, metrics_by_role
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
