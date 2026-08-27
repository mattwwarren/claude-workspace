"""Shared ``review`` click group and cross-submodule payload helpers.

Defines the ``review`` group object once so every command submodule
(``consolidate``, ``commands``) can decorate its commands with
``@review.command(...)``. Also holds the two helpers every payload command
shares: :func:`_build_captured_diff` (raw diff text -> ``CapturedDiff``) and
:func:`_parse_payload_or_exit` (read + validate a JSON payload, or exit 1).
"""

from __future__ import annotations

import click
from pydantic import BaseModel, ValidationError

from cw.cli._base import main
from cw.review_findings import CapturedDiff


@main.group(name="review")
def review() -> None:
    """Operator review-request tracking (RFC 0011 S2)."""


def _build_captured_diff(diff_text: str) -> CapturedDiff:
    """Parse raw unified diff text into a :class:`CapturedDiff`.

    Reuses :func:`cw.codex_review._parse_unified_diff` (function-local import
    — that parser and this command's envelope both live in modules outside
    this ticket's touch-point contract; the codex module owns the parser and
    is not modified here) rather than duplicating the ~60-line unified-diff
    parser. Mirrors ``codex_review._capture_diff``'s post-subprocess body
    exactly: ``files`` is derived from ``file_line_text`` so it can never
    drift from the per-line content.
    """
    from cw.codex_review import _parse_unified_diff

    file_diffs, file_line_text, file_window_text, _changed_files = _parse_unified_diff(
        diff_text
    )
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    return CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=file_window_text,
    )


def _parse_payload_or_exit[InputT: BaseModel](path: str, model: type[InputT]) -> InputT:
    """Read PATH ('-' for stdin) and validate it against *model*, or exit 1.

    The three ``cw review`` payload commands share one failure shape —
    ``field.path: message`` lines on stderr, exit 1 — so they share the
    reading and validating too rather than letting three copies drift.
    """
    from cw.result import _format_errors, _read_json_payload

    payload = _read_json_payload(path)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        raise click.exceptions.Exit(1) from exc
