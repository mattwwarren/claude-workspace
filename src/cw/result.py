"""Sentinel validation utilities for cw result subcommands."""

from __future__ import annotations

import json
import sys
from typing import Any

import click
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """Validate a raw AutoDevResult payload dict.

    Returns a list of field-error lines ("field.path: message").
    Empty list means valid.
    """
    try:
        AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            msg = err["msg"]
            lines.append(f"{loc}: {msg}")
        return lines
    else:
        return []


@click.group()
def result() -> None:
    """Validate AutoDevResult sentinels before emission.

    Pre-emit gate: validates the inner JSON object (NOT the <<<AUTO_DEV_RESULT>>>
    framed block) against the authoritative AutoDevResult schema.

    Field rules:
    - schema_version: int -- must be 1-4
    - ticket_id: str
    - status: "shipped" | "no_op" | "plan_pending_approval" |
      "ambiguities_pending_resolution" | "premises_pending_verification" |
      "review_pending_approval" | "merge_gate_blocked" | "scope_exceeded" |
      "forbidden_area" | "blocked"
    - stage_reached: str literal (see cw schema show auto-dev-result for full list)
    - scope: {tier, files, lines_estimate, lines_actual, forbidden_touched}
    - plan_source: "linear_existing" | "github_issue_existing" | "generated" |
      "free_text" | "none"
    - branch: str | null

    Constraints (cross-field invariants):
    - pr: non-null iff status == "shipped"
    - blocker: non-null iff status == "blocked"
    - next_actions contains "wait_for_ci" iff status == "shipped"
    - scope.tier: required when stage_reached not in {stage1_plan, stage1_pre_flight}
    - scope.lines_actual: required when stage_reached not in
      {stage1_plan, stage1_pre_flight}
    - health.lowest_agent_confidence: required when stage_reached not in
      {stage1_plan, stage1_pre_flight}
    - branch: null when status in pre-branch statuses (plan_pending_approval, etc.)
    - health.downgrade_applied: True requires status == "review_pending_approval"
    - schema_version: v2-introduced statuses require schema_version >= 2

    Use 'cw schema show auto-dev-result' for the full schema reference.
    """


@result.command(name="validate")
@click.argument("path")
def result_validate(path: str) -> None:
    """Validate a candidate sentinel JSON against AutoDevResult schema.

    PATH is a file path or '-' for stdin. The payload must be the inner
    JSON object only -- do NOT include the <<<AUTO_DEV_RESULT>>> delimiters.

    On success: exits 0, prints normalized JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    if path == "-":
        raw = sys.stdin.read()
    else:
        with click.open_file(path, "r") as f:
            raw = f.read()

    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"json: {exc}", err=True)
        raise SystemExit(1) from exc

    errors = validate_payload(payload)
    if errors:
        for line in errors:
            click.echo(line, err=True)
        raise SystemExit(1)

    result_obj = AutoDevResult.model_validate(payload)
    click.echo(result_obj.model_dump_json(indent=2))
