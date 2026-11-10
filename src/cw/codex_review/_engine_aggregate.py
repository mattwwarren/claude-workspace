"""The read-only aggregation turn of a codex engine round (RFC 0018 A4, #3216).

After every engine role ran, one ``codex exec`` turn aggregates the role
documents into the summary's judgement fields
(:class:`~cw.review_engine.aggregate.EngineAggregationOutput`). cw fills the
other three fields, writes the summary file itself, and leaves validation to the
shared core (:mod:`cw.review_engine.validate`).

A dedicated small runner, because ``_run_codex_role`` is hard-wired to
``ReviewerFindingsDocument`` (R13). It reuses the shared argv builder with the
sandbox pinned ``read-only`` (never the lane's widened reviewer mode), the
audit flags and their degrade-and-retry, the snap-safe scratch dir and the
process-failure mapping.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.codex_review._const import _CATEGORY_TO_REASON
from cw.codex_review._roles import (
    _AUDIT_ARGV_FLAGS,
    _build_generic_codex_argv,
    _classify_codex_failure,
    _codex_scratch_dir,
    _is_audit_flag_rejection,
)
from cw.codex_review._shard import ReviewInputTooLargeError
from cw.models.enums import SandboxMode
from cw.review_engine.aggregate import (
    EngineAggregationOutput,
    assemble_summary,
    engine_aggregation_schema,
    render_aggregation_envelope,
)
from cw.review_engine.constants import (
    AGGREGATION_REASON_EMPTY_OUTPUT,
    AGGREGATION_REASON_INVALID_JSON,
    AGGREGATION_REASON_MISSING_OUTPUT,
    AGGREGATION_REASON_SCHEMA_MISMATCH,
    FRICTION,
    EngineRoundFailure,
)
from cw.review_engine.validate import SummaryFailure

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult

_log = logging.getLogger(__name__)

_SCHEMA_FILENAME = "aggregation-schema.json"
_OUTPUT_FILENAME = "aggregation-output.json"


def _aggregation_failure(reason: str) -> SummaryFailure:
    failure = EngineRoundFailure.ENGINE_AGGREGATION_FAILED
    return SummaryFailure(failure, FRICTION[failure].format(reason=reason))


def _output_failure_reason(content: str | None) -> str | None:
    """The exit-0 output failure kind, or ``None`` when *content* is valid."""
    if content is None:
        return AGGREGATION_REASON_MISSING_OUTPUT
    if not content.strip():
        return AGGREGATION_REASON_EMPTY_OUTPUT
    try:
        json.loads(content)
    except json.JSONDecodeError:
        return AGGREGATION_REASON_INVALID_JSON
    try:
        EngineAggregationOutput.model_validate_json(content)
    except ValidationError:
        return AGGREGATION_REASON_SCHEMA_MISMATCH
    return None


def _run_once(
    runner: CodexRunner,
    worktree: Path,
    argv: list[str],
    timeout_seconds: int | None,
    stdin: str,
) -> CodexRunResult:
    """One run, retried once without the audit flags on an older codex-cli."""
    result = runner.run(worktree, argv, timeout_seconds, stdin=stdin)
    if _is_audit_flag_rejection(result):
        stripped = [flag for flag in argv if flag not in _AUDIT_ARGV_FLAGS]
        result = runner.run(worktree, stripped, timeout_seconds, stdin=stdin)
    return result


def _write_summary(
    content: str, head_sha: str, summary_path: Path
) -> SummaryFailure | None:
    output = EngineAggregationOutput.model_validate_json(content)
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(summary_path, json.dumps(assemble_summary(output, head_sha)))
    except OSError:
        reason = EngineRoundFailure.ENGINE_NO_SUMMARY
        return SummaryFailure(reason, FRICTION[reason].format(path=str(summary_path)))
    return None


def run_codex_aggregator(
    *,
    runner: CodexRunner,
    worktree: Path,
    head_sha: str,
    aggregation_prompt: str,
    role_documents: Sequence[tuple[str, str]],
    summary_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    max_prompt_chars: int | None,
) -> SummaryFailure | None:
    """Run the aggregation turn and write the summary to *summary_path*.

    *role_documents* pairs each role id with its document JSON, in manifest
    order. Returns ``None`` once the summary file is written; otherwise the
    ``engine_aggregation_failed`` failure (``engine_no_summary`` when the write
    itself fails). An envelope over *max_prompt_chars* raises
    :class:`~cw.codex_review.ReviewInputTooLargeError` before any codex call.
    """
    envelope = render_aggregation_envelope(
        head_sha=head_sha,
        aggregation_prompt=aggregation_prompt,
        role_documents=role_documents,
    )
    if max_prompt_chars is not None and len(envelope) > max_prompt_chars:
        raise ReviewInputTooLargeError(
            actual_chars=len(envelope), max_chars=max_prompt_chars
        )
    scratch = _codex_scratch_dir(uuid.uuid4().hex)
    try:
        schema_path = scratch / _SCHEMA_FILENAME
        schema_path.write_text(
            json.dumps(engine_aggregation_schema()), encoding="utf-8"
        )
        argv = _build_generic_codex_argv(
            model=model,
            reasoning_effort=reasoning_effort,
            schema_path=schema_path,
            output_path=scratch / _OUTPUT_FILENAME,
            sandbox_mode=SandboxMode.READ_ONLY,
        )
        result = _run_once(runner, worktree, argv, timeout_seconds, envelope)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.timed_out or result.returncode != 0:
        reason = _CATEGORY_TO_REASON[_classify_codex_failure(result)]
        return _aggregation_failure(reason)
    output_reason = _output_failure_reason(result.output_file_content)
    if output_reason is not None or result.output_file_content is None:
        return _aggregation_failure(output_reason or AGGREGATION_REASON_MISSING_OUTPUT)
    return _write_summary(result.output_file_content, head_sha, summary_path)
