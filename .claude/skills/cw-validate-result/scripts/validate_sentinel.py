#!/usr/bin/env python3
"""Forensic validator for a cw-dispatched /auto-dev session's AUTO_DEV_RESULT.

Wraps the cw-followup skill's ``parse_sentinel.py`` to resolve and parse the
sentinel, then walks the headless-contract checklist and emits a PASS/FAIL
summary per row. Distinguishes the four important outcomes:

- ``no_sentinel`` — the run exited without emitting a sentinel at all
- ``invalid_sentinel`` — the sentinel was present but failed schema validation
- ``producer_status_unknown`` — the parser couldn't type the producer's status
- ``valid`` — the sentinel parsed cleanly

Output is one JSON object on stdout with ``outcome``, ``checks`` (list of rows
with ``name``, ``passed``, ``detail``), and the embedded parser output. Exit
code reflects the outcome: 0 on ``valid`` or ``producer_status_unknown`` (the
run finished and emitted something usable), 1 on ``no_sentinel`` or
``invalid_sentinel`` (the run did not produce a usable result).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args


def _bootstrap_sys_path() -> None:
    """Add repo src/ to sys.path so cw imports work under bare python3."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            src = str(parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            return
    msg = (
        f"Could not locate pyproject.toml walking up from {__file__} — bootstrap failed"
    )
    raise RuntimeError(msg)


_bootstrap_sys_path()

# Single source of truth for the canonical status set — never hardcode it.
# `Status` grew two v4 members in issue #191 (ambiguities_pending_resolution,
# premises_pending_verification); deriving the set keeps this validator in
# lockstep with the parser instead of drifting behind it.
from cw.auto_dev_result import Status

_SKILL_DIR = Path(__file__).resolve().parents[1]
# cw-followup's parse_sentinel lives in a sibling skill — it owns transcript
# resolution + parsing so we don't reimplement either.
_PARSE_SCRIPT = _SKILL_DIR.parent / "cw-followup" / "scripts" / "parse_sentinel.py"
_REPO_ROOT = _SKILL_DIR.parents[2]


def _run_parser(args: list[str]) -> dict[str, Any]:
    """Shell out to cw-followup's parse_sentinel.py with the given args.

    Uses ``uv run`` against the cw repo so the parser's import of
    ``cw.auto_dev_result`` resolves the same way the production code does.
    """
    cmd = [
        "uv",
        "run",
        "--project",
        str(_REPO_ROOT),
        "python",
        str(_PARSE_SCRIPT),
        *args,
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    try:
        parsed: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"could not parse parse_sentinel.py stdout: {exc}\n")
        raise SystemExit(2) from exc
    if not isinstance(parsed, dict):
        sys.stderr.write("parse_sentinel.py emitted a non-object\n")
        raise SystemExit(2)
    return parsed


def _outcome(parser_output: dict[str, Any]) -> str:
    if not parser_output.get("sentinel_found"):
        return "no_sentinel"
    kind = parser_output.get("result_kind")
    if kind == "AutoDevResult":
        return "valid"
    result = parser_output.get("result") or {}
    blocker = result.get("blocker") or {}
    reason = blocker.get("reason")
    if reason == "status_unknown":
        return "producer_status_unknown"
    return "invalid_sentinel"


def _check(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _build_checks(parser_output: dict[str, Any], outcome: str) -> list[dict[str, Any]]:
    raw = parser_output.get("raw_payload") or {}
    result = parser_output.get("result") or {}
    checks: list[dict[str, Any]] = []

    checks.append(
        _check(
            "sentinel_emitted",
            outcome != "no_sentinel",
            f"assistant_blocks_scanned={parser_output.get('assistant_blocks_scanned')}",
        ),
    )

    canonical_statuses = set(get_args(Status))
    effective_status = raw.get("status") or result.get("status")
    checks.append(
        _check(
            "status_is_canonical",
            effective_status in canonical_statuses,
            f"effective_status={effective_status!r}",
        ),
    )

    result_kind = parser_output.get("result_kind")
    blocker_reason = ((result.get("blocker") or {}) or {}).get("reason")
    checks.append(
        _check(
            "schema_validates",
            result_kind == "AutoDevResult",
            f"result_kind={result_kind!r}, blocker_reason={blocker_reason!r}",
        ),
    )

    health = raw.get("health") or {}
    expected_health = {
        "lowest_agent_confidence",
        "any_incomplete_risk",
        "recommendation",
    }
    health_keys = set(health) if isinstance(health, dict) else set()
    checks.append(
        _check(
            "health_present",
            expected_health.issubset(health_keys),
            f"missing={sorted(expected_health - health_keys)}",
        ),
    )

    blocker = raw.get("blocker")
    is_blocked = effective_status == "blocked"
    blocker_present = blocker is not None
    blocker_invariant = (is_blocked and blocker_present) or (
        not is_blocked and not blocker_present
    )
    checks.append(
        _check(
            "blocker_iff_blocked",
            blocker_invariant,
            f"is_blocked={is_blocked}, blocker_present={blocker_present}",
        ),
    )

    pr = raw.get("pr")
    is_shipped = effective_status == "shipped"
    checks.append(
        _check(
            "pr_iff_shipped",
            (is_shipped and pr is not None) or (not is_shipped and pr is None),
            f"is_shipped={is_shipped}, pr_present={pr is not None}",
        ),
    )

    next_actions = raw.get("next_actions") or []
    wait_present = "wait_for_ci" in next_actions
    checks.append(
        _check(
            "wait_for_ci_iff_shipped",
            (is_shipped and wait_present) or (not is_shipped and not wait_present),
            f"is_shipped={is_shipped}, wait_for_ci_present={wait_present}",
        ),
    )

    # Phase C (issue #174) — agent_health_summary advisory check.
    # Optional field; absent on payloads from older producers. Always passes;
    # surfaces entry count for human inspection.
    agent_health_summary = health.get("agent_health_summary")
    summary_detail = (
        f"entries={len(agent_health_summary)}"
        if isinstance(agent_health_summary, list)
        else "absent"
    )
    checks.append(_check("agent_health_summary_present", True, summary_detail))

    # Phase D (issue #174) — pr_created advisory check.
    # Optional field; absent on payloads from older producers. Always passes;
    # surfaces ci_status_at_creation for human inspection when present.
    pr_created = raw.get("pr_created")
    if isinstance(pr_created, dict):
        created_detail = (
            f"present, ci_status_at_creation="
            f"{pr_created.get('ci_status_at_creation')!r}"
        )
    else:
        created_detail = "absent"
    checks.append(_check("pr_created_present", True, created_detail))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session-id")
    source.add_argument("--ticket-id")
    source.add_argument("--transcript-path")
    args = parser.parse_args()

    parser_args: list[str] = []
    if args.session_id:
        parser_args = ["--session-id", args.session_id]
    elif args.ticket_id:
        parser_args = ["--ticket-id", args.ticket_id]
    elif args.transcript_path:
        parser_args = ["--transcript-path", args.transcript_path]

    parser_output = _run_parser(parser_args)
    outcome = _outcome(parser_output)
    checks = _build_checks(parser_output, outcome)

    summary = {
        "outcome": outcome,
        "checks": checks,
        "session": parser_output.get("session"),
        "transcript_path": parser_output.get("transcript_path"),
        "effective_status": (parser_output.get("raw_payload") or {}).get("status")
        or (parser_output.get("result") or {}).get("status"),
        "next_actions": (parser_output.get("raw_payload") or {}).get("next_actions")
        or [],
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if outcome in {"valid", "producer_status_unknown"} else 1


if __name__ == "__main__":
    sys.exit(main())
