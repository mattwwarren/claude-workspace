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

from cw.atomic import atomic_write_text
from cw.codex_review._const import _CATEGORY_TO_REASON
from cw.codex_review._roles import (
    _build_generic_codex_argv,
    _classify_codex_failure,
    _codex_scratch_dir,
    _parse_codex_output,
    _refined_nonzero_reason,
    _run_codex_with_audit_degrade,
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
    AGGREGATION_FAILED_REASONS,
    AGGREGATION_REASON_MISSING_OUTPUT,
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
    """The ``engine_aggregation_failed`` failure for a vocabulary *reason*.

    An unknown *reason* is an internal-contract violation (#3441): every
    producer in this module maps onto ``AGGREGATION_FAILED_REASONS``.
    """
    if reason not in AGGREGATION_FAILED_REASONS:
        msg = f"unknown aggregation failure reason: {reason!r}"
        raise ValueError(msg)
    failure = EngineRoundFailure.ENGINE_AGGREGATION_FAILED
    return SummaryFailure(failure, FRICTION[failure].format(reason=reason))


def _no_summary(summary_path: Path) -> SummaryFailure:
    """The ``engine_no_summary`` failure naming *summary_path*."""
    reason = EngineRoundFailure.ENGINE_NO_SUMMARY
    return SummaryFailure(reason, FRICTION[reason].format(path=str(summary_path)))


def _write_summary(
    output: EngineAggregationOutput, head_sha: str, summary_path: Path
) -> SummaryFailure | None:
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(summary_path, json.dumps(assemble_summary(output, head_sha)))
    except OSError:
        return _no_summary(summary_path)
    return None


def _run_turn(
    runner: CodexRunner,
    worktree: Path,
    envelope: str,
    *,
    summary_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
) -> CodexRunResult | SummaryFailure:
    """Write the schema to a fresh scratch dir and run the aggregation turn.

    A scratch-dir or schema-write ``OSError`` is ``engine_no_summary`` before
    any codex call (#3436): cw cannot produce the summary, and a plain requeue
    would hit the same unwritable state dir.
    """
    try:
        scratch = _codex_scratch_dir(uuid.uuid4().hex)
    except OSError:
        _log.warning("codex aggregation scratch dir is unwritable", exc_info=True)
        return _no_summary(summary_path)
    try:
        schema_path = scratch / _SCHEMA_FILENAME
        try:
            schema_path.write_text(
                json.dumps(engine_aggregation_schema()), encoding="utf-8"
            )
        except OSError:
            _log.warning(
                "codex aggregation schema write failed: %s", schema_path, exc_info=True
            )
            return _no_summary(summary_path)
        argv = _build_generic_codex_argv(
            model=model,
            reasoning_effort=reasoning_effort,
            schema_path=schema_path,
            output_path=scratch / _OUTPUT_FILENAME,
            sandbox_mode=SandboxMode.READ_ONLY,
        )
        result, _argv = _run_codex_with_audit_degrade(
            runner, worktree, argv, timeout_seconds, envelope
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result


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
    result = _run_turn(
        runner,
        worktree,
        envelope,
        summary_path=summary_path,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(result, SummaryFailure):
        return result
    if result.timed_out or result.returncode != 0:
        category = _classify_codex_failure(result)
        reason = _CATEGORY_TO_REASON[category]
        if category == "nonzero_exit":
            refined, _ = _refined_nonzero_reason(result)
            reason = refined or reason
        return _aggregation_failure(reason)
    output, output_reason = _parse_codex_output(
        result.output_file_content, validator=EngineAggregationOutput.model_validate
    )
    if output is None:
        return _aggregation_failure(output_reason or AGGREGATION_REASON_MISSING_OUTPUT)
    return _write_summary(output, head_sha, summary_path)
