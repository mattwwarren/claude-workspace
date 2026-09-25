#!/usr/bin/env python3
# cw-script-version: 1
"""Gate script: does an operator override cover the MUST_FIX verdict (#2205)?

Usage (from `/auto-dev-finalize`'s "MUST_FIX Override Verification" step):
    python .claude/scripts/check_must_fix_override.py \\
        --verdict .claude/review-verdict.json \\
        --context .claude/cw-context.json \\
        --head "$(git rev-parse HEAD)"

A codex background review that finds MUST_FIX findings parks the ticket with
``blocked_reason: codex_must_fix_findings``. ``cw dev-queue requeue --stage
finalize`` resets that row to PENDING, which clears ``blocked_reason`` before
the FINALIZE session spawns, so FINALIZE cannot learn from the row that it was
parked. This script re-derives it from the worktree's structured verdict
(``.claude/review-verdict.json``, ownership-stamped by its ``ticket_id``) and
compares it against the operator's ``cw dev-queue approve --override-must-fix``
record, which dispatch threads into ``cw-context.json`` as
``queue_metadata.must_fix_override``.

The override counts only while it is bound to exactly this verdict: its
``reviewed_sha`` equals the verdict's, HEAD has not moved past that SHA, and its
finding identities equal the verdict's MUST_FIX fingerprints. A later review
round or a new commit voids it.

Statuses:
    clean      — no verdict file, a foreign verdict, or a non-blocking verdict
    overridden — a blocking verdict fully covered by a matching override
    blocked    — a blocking verdict with no matching override, or a verdict
                 file that cannot be read (fails closed)

Exit codes:
    0  — clean or overridden
    1  — blocked; FINALIZE exits ``blocked`` with
         ``blocker.reason: "codex_must_fix_findings"``
    2  — usage error (argparse)

The JSON verdict is written to stdout on exits 0 and 1:
    {"status": ..., "reviewed_sha": str | null, "findings": [{"file", "summary",
     "fingerprint": [file, normalized_summary] | null}, ...],
     "override": {...} | null, "detail": str}

Stdlib-only: nothing in ``.claude/scripts/`` imports ``cw``, because these
scripts run inside client worktrees where the package may not be importable.
The fingerprint below is therefore an inline copy of
``cw.review_debt.fingerprint_v1``, the source of truth;
``tests/test_check_must_fix_override.py`` pins the two in sync.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Inline copy of cw.review_debt's fingerprint_v1 constants — keep in sync.
_NO_ANCHOR_FILE = "N/A"
_WHITESPACE_RE = re.compile(r"\s+")
_POSITION_RE = re.compile(r"(?:\s+at)?\s*(?::\d+\b|\blines?\s+\d+(?:\s*-\s*\d+)?)")
_DIGIT_RUN_RE = re.compile(r"\d+")
_DIGIT_PLACEHOLDER = "N"

_OVERRIDE_KEY = "must_fix_override"
_PAIR_LEN = 2  # a finding id is a (file, normalized_summary) pair
_REMEDIATION = (
    "run `cw dev-queue approve <ticket> --override-must-fix --reason"
    ' "..."` then `cw dev-queue requeue <ticket> --stage finalize`'
)

Fingerprint = tuple[str, str]


def _normalize_summary(summary: str) -> str:
    """Inline copy of ``cw.review_debt._normalize_summary``."""
    collapsed = _WHITESPACE_RE.sub(" ", summary.lower()).strip()
    without_positions = _POSITION_RE.sub("", collapsed)
    masked = _DIGIT_RUN_RE.sub(_DIGIT_PLACEHOLDER, without_positions)
    return _WHITESPACE_RE.sub(" ", masked).strip()


def fingerprint_v1(file: str, summary: str) -> Fingerprint | None:
    """Inline copy of ``cw.review_debt.fingerprint_v1``."""
    if file == _NO_ANCHOR_FILE:
        return None
    return (file, _normalize_summary(summary))


class _VerdictError(Exception):
    """The verdict file exists but is not a usable review verdict."""


def _load_verdict(path: Path) -> dict[str, object] | None:
    """Return the verdict envelope, None when absent; raise when unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"could not read {path}: {exc}"
        raise _VerdictError(msg) from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{path} is not valid JSON: {exc}"
        raise _VerdictError(msg) from exc
    if not isinstance(doc, dict):
        msg = f"{path} is not a JSON object"
        raise _VerdictError(msg)
    return doc


def _verdict_fields(doc: dict[str, object]) -> tuple[bool, str, list[dict[str, str]]]:
    """Extract ``(blocking, reviewed_sha, must_fix)``; raise on a bad shape."""
    verdict = doc.get("verdict")
    if not isinstance(verdict, dict):
        msg = "review verdict envelope has no 'verdict' object"
        raise _VerdictError(msg)
    blocking = verdict.get("blocking")
    reviewed_sha = verdict.get("reviewed_sha")
    raw_must_fix = verdict.get("must_fix")
    if not isinstance(blocking, bool) or not isinstance(reviewed_sha, str):
        msg = "review verdict lacks a boolean 'blocking' or a string 'reviewed_sha'"
        raise _VerdictError(msg)
    if not isinstance(raw_must_fix, list):
        msg = "review verdict lacks a 'must_fix' list"
        raise _VerdictError(msg)
    must_fix: list[dict[str, str]] = []
    for item in raw_must_fix:
        file = item.get("file") if isinstance(item, dict) else None
        summary = item.get("summary") if isinstance(item, dict) else None
        if not isinstance(file, str) or not isinstance(summary, str):
            msg = "review verdict has a must_fix finding without a file/summary"
            raise _VerdictError(msg)
        must_fix.append({"file": file, "summary": summary})
    return blocking, reviewed_sha, must_fix


def _load_context(path: Path) -> dict[str, object]:
    """Return cw-context.json, or {} when absent/unreadable (no override then)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _override_of(context: dict[str, object]) -> object:
    metadata = context.get("queue_metadata")
    return metadata.get(_OVERRIDE_KEY) if isinstance(metadata, dict) else None


def _parse_override(raw: object) -> tuple[str, set[Fingerprint]] | None:
    """Return ``(reviewed_sha, finding_ids)``, or None for a malformed record."""
    if not isinstance(raw, dict):
        return None
    actor = raw.get("actor")
    reason = raw.get("reason")
    reviewed_sha = raw.get("reviewed_sha")
    finding_ids = raw.get("finding_ids")
    recorded_at = raw.get("recorded_at")
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(reviewed_sha, str)
        or not reviewed_sha.strip()
        or not isinstance(recorded_at, str)
        or not recorded_at.strip()
        or not isinstance(finding_ids, list)
    ):
        return None
    try:
        datetime.fromisoformat(recorded_at.strip())
    except ValueError:
        return None
    ids: set[Fingerprint] = set()
    for pair in finding_ids:
        if (
            not isinstance(pair, list)
            or len(pair) != _PAIR_LEN
            or not all(isinstance(part, str) for part in pair)
        ):
            return None
        ids.add((pair[0], pair[1]))
    return reviewed_sha, ids


def _result(
    status: str,
    detail: str,
    *,
    reviewed_sha: str | None = None,
    findings: list[dict[str, object]] | None = None,
    override: object = None,
) -> dict[str, object]:
    return {
        "status": status,
        "reviewed_sha": reviewed_sha,
        "findings": findings or [],
        "override": override if isinstance(override, dict) else None,
        "detail": detail,
    }


def _judge_override(
    raw_override: object,
    reviewed_sha: str,
    live_ids: set[Fingerprint],
    head: str,
) -> tuple[str, str]:
    """``(status, detail)`` for a blocking verdict given the override record."""
    if raw_override is None:
        return (
            "blocked",
            f"blocking MUST_FIX verdict with no operator override; {_REMEDIATION}",
        )
    parsed = _parse_override(raw_override)
    if parsed is None:
        return (
            "blocked",
            f"malformed must_fix_override in cw-context.json; {_REMEDIATION}",
        )
    override_sha, override_ids = parsed
    if override_sha != reviewed_sha:
        return "blocked", (
            f"stale override: bound to reviewed_sha {override_sha}, but the live"
            f" verdict reviewed {reviewed_sha}; {_REMEDIATION}"
        )
    if head != reviewed_sha:
        return "blocked", (
            f"HEAD {head} has moved past the reviewed_sha {reviewed_sha} the"
            f" override is bound to; {_REMEDIATION}"
        )
    if override_ids != live_ids:
        uncovered = sorted(live_ids - override_ids)
        extra = sorted(override_ids - live_ids)
        return "blocked", (
            f"override finding set does not match the verdict: uncovered"
            f" {uncovered}, not on verdict {extra}; {_REMEDIATION}"
        )
    return "overridden", "operator override matches the reviewed commit and findings"


def check_must_fix_override(
    verdict_path: Path, context_path: Path, head: str
) -> dict[str, object]:
    """Compare the worktree's live verdict against the operator override."""
    try:
        doc = _load_verdict(verdict_path)
        if doc is None:
            return _result("clean", f"no review verdict at {verdict_path}")
        blocking, reviewed_sha, must_fix = _verdict_fields(doc)
    except _VerdictError as exc:
        return _result(
            "blocked",
            f"unreadable review verdict, failing closed: {exc}; fix or re-run"
            " the review",
        )

    context = _load_context(context_path)
    owner = doc.get("ticket_id")
    ticket = context.get("ticket_id")
    owner_valid = isinstance(owner, str) and bool(owner.strip())
    ticket_valid = isinstance(ticket, str) and bool(ticket.strip())
    if not owner_valid or not ticket_valid:
        status = "blocked" if blocking else "clean"
        return _result(
            status,
            (
                "review verdict and cw-context must both contain non-empty string"
                " ticket_id values before an override can be evaluated"
            ),
            reviewed_sha=reviewed_sha,
        )
    if owner != ticket:
        return _result(
            "clean",
            f"review verdict belongs to {owner!r}, not {ticket!r}; stale or"
            " foreign, not authoritative",
        )

    findings: list[dict[str, object]] = []
    live_ids: set[Fingerprint] = set()
    unanchored: list[str] = []
    for finding in must_fix:
        fingerprint = fingerprint_v1(finding["file"], finding["summary"])
        findings.append(
            {**finding, "fingerprint": list(fingerprint) if fingerprint else None}
        )
        if fingerprint is None:
            unanchored.append(finding["summary"])
        else:
            live_ids.add(fingerprint)

    raw_override = _override_of(context)
    if not blocking:
        return _result(
            "clean",
            "review verdict is not blocking",
            reviewed_sha=reviewed_sha,
            findings=findings,
            override=raw_override,
        )
    if unanchored:
        status, detail = (
            "blocked",
            (
                f"MUST_FIX finding(s) with no diff anchor cannot be overridden:"
                f" {unanchored}"
            ),
        )
    else:
        status, detail = _judge_override(raw_override, reviewed_sha, live_ids, head)
    return _result(
        status,
        detail,
        reviewed_sha=reviewed_sha,
        findings=findings,
        override=raw_override,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (clean/overridden), 1 (blocked), or 2 (usage)."""
    parser = argparse.ArgumentParser(
        description="Check a codex MUST_FIX verdict against the operator override.",
    )
    parser.add_argument(
        "--verdict", required=True, help="Path to .claude/review-verdict.json"
    )
    parser.add_argument(
        "--context", required=True, help="Path to .claude/cw-context.json"
    )
    parser.add_argument("--head", required=True, help="Current HEAD commit SHA")
    args = parser.parse_args(argv)

    verdict = check_must_fix_override(
        Path(args.verdict), Path(args.context), args.head.strip()
    )
    print(json.dumps(verdict, indent=2))
    return 1 if verdict["status"] == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
