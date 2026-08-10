#!/usr/bin/env python3
"""Gate script: is the impl stage's `impl-complete` trailer still valid? (#1794)

Usage (from `/auto-dev-impl`'s Pre-Stage Detector Guard):
    python .claude/scripts/check_impl_guard_staleness.py \\
        --head-commit-at "$(git log -1 --format=%cI HEAD)" \\
        --comments-file /tmp/impl-comments-$CW_SESSION.json \\
        --regressed-into-stage "$REGRESSED_INTO_STAGE"

Context: the Pre-Stage Detector Guard used to treat an `Auto-Dev-Stage:
impl-complete` trailer on branch HEAD as unconditionally authoritative for
"nothing to do here." Two independent facts invalidate that premise without
the guard ever seeing them:

1. An operator comment posted *after* HEAD was written — the guard never read
   ticket comments at all, and `.cw/context.json`'s cached array is a Stage-0
   snapshot that is never refreshed between stages (dispatch spawns
   `/auto-dev-{stage}` directly per stage; Stage 0 does not re-run).
2. A deliberate operator regress (`cw dev-queue requeue <T> --regress --stage
   impl`), which mutates only queue state and never touches git — so HEAD's
   trailer silently overrides the operator's explicit backward move.

This script is the deterministic half of the fix. The comparison it performs
is real epoch arithmetic across two different ISO-8601 flavours (git's `%cI`
carries a local UTC offset; GitHub's `createdAt` is `Z`-suffixed), which is
exactly the kind of computation prose must not be trusted to do by eye.

Stdlib-only by design: nothing in `.claude/scripts/` depends on the `cw`
package, and this gate must keep working as a standalone script. Sibling of
`check_plan_scope_conformance.py` (#1779) in shape, exit convention, and
JSON-verdict-to-stdout contract.

Exit codes:
    0  — verdict computed (whether or not it is stale)
    2  — usage / parse error (unreadable or malformed `--comments-file`,
         unparseable `--head-commit-at`). Does NOT block: the caller's prose
         treats exit 2 as "cannot determine staleness — fail open to the
         existing short-circuit behaviour", the same fail-open convention as
         `check_not_main_checkout.py`'s and `check_plan_scope_conformance.py`'s
         exit 2.

The JSON verdict is written to stdout on exit 0:
    {"stale": bool, "reasons": [...], "head_commit_at": "<iso>",
     "newest_comment_at": "<iso>|null", "regressed_into_stage": "<stage>|\"\""}

`reasons` is the operator-facing audit surface: the guard copies it into
`friction_highlights` so a run that declined to short-circuit says why.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# A comment's timestamp arrives under one of two keys: `createdAt` is gh's
# field name (a live `gh issue view <n> --json comments` result), `created_at`
# is the snake_case shape the guard itself re-materialises into
# `.cw/context.json`. Accept either; an entry carrying neither is skipped, not
# fatal — a stale cached file full of old-shape bare-string entries must
# degrade to "no timestamp evidence", never crash the gate.
_CREATED_AT_KEYS = ("createdAt", "created_at")

_REASON_REGRESSED = "regressed_to_impl"
_REASON_STALE_COMMENT = "stale_comment_after_head"


def _parse_iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, or return None if it is unparseable.

    A naive result is pinned to UTC so it can never raise TypeError when
    compared against an offset-aware one. Python 3.11+ `fromisoformat` already
    accepts a `Z` suffix; the explicit normalisation is defence in depth for
    anyone running this standalone script under an older interpreter, which is
    a real possibility precisely because it takes no dependency on `cw`.
    """
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _load_comments(path: Path) -> list[object] | None:
    """Read the comments JSON array, or return None on any malformed input."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"check_impl_guard_staleness: could not read --comments-file {path}: {exc}",
            file=sys.stderr,
        )
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"check_impl_guard_staleness: --comments-file {path} is not valid"
            f" JSON: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(payload, list):
        print(
            f"check_impl_guard_staleness: --comments-file {path} must be a JSON"
            f" array of comment objects, got {type(payload).__name__}.",
            file=sys.stderr,
        )
        return None
    return payload


def newest_comment_timestamp(comments: list[object]) -> tuple[str, datetime] | None:
    """Return the (raw, parsed) newest comment timestamp, or None if there is none.

    Entries that are not objects, that carry neither timestamp key, or whose
    timestamp does not parse are skipped rather than treated as fatal — the
    absence of usable timestamp evidence is "not stale", not "cannot compute".
    """
    newest: tuple[str, datetime] | None = None
    for entry in comments:
        if not isinstance(entry, dict):
            continue
        for key in _CREATED_AT_KEYS:
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            parsed = _parse_iso8601(value)
            if parsed is None:
                continue
            if newest is None or parsed > newest[1]:
                newest = (value, parsed)
            break
    return newest


def compute_verdict(
    head_commit_at_raw: str,
    head_commit_at: datetime,
    comments: list[object],
    regressed_into_stage: str,
) -> dict[str, object]:
    """Build the staleness/regress verdict.

    ``regressed_into_stage`` is judged by *presence*, not by value: it is
    already a per-arrival marker (dispatch clears it the moment this stage's
    session is spawned), and only ``auto-dev-impl.md`` reads it, so a non-empty
    value can only ever mean "this impl entry was reached via a backward move."
    Thresholding it — the way the cumulative ``regress_attempts`` counter would
    have to be — is exactly the false positive this field exists to avoid.
    """
    newest = newest_comment_timestamp(comments)
    reasons: list[str] = []
    if regressed_into_stage.strip():
        reasons.append(_REASON_REGRESSED)
    if newest is not None and newest[1] > head_commit_at:
        reasons.append(_REASON_STALE_COMMENT)
    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "head_commit_at": head_commit_at_raw,
        "newest_comment_at": newest[0] if newest is not None else None,
        "regressed_into_stage": regressed_into_stage,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (verdict computed) or 2 (usage/parse error)."""
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether an `Auto-Dev-Stage: impl-complete` trailer is still"
            " authoritative, or has been invalidated by a newer ticket comment"
            " or a deliberate operator regress."
        ),
    )
    parser.add_argument(
        "--head-commit-at",
        required=True,
        help="Branch HEAD's committer date, ISO-8601 (git log -1 --format=%%cI)",
    )
    parser.add_argument(
        "--comments-file",
        required=True,
        help="Path to a JSON array of live-fetched ticket comments",
    )
    parser.add_argument(
        "--regressed-into-stage",
        default="",
        help=(
            "Raw queue_metadata.regressed_into_stage from .claude/cw-context.json;"
            " empty (jq's `// empty` on null) means no regress"
        ),
    )
    args = parser.parse_args(argv)

    head_commit_at = _parse_iso8601(args.head_commit_at)
    if head_commit_at is None:
        print(
            "check_impl_guard_staleness: could not parse --head-commit-at"
            f" {args.head_commit_at!r} as an ISO-8601 timestamp — cannot"
            " determine staleness.",
            file=sys.stderr,
        )
        return 2

    comments = _load_comments(Path(args.comments_file))
    if comments is None:
        return 2

    verdict = compute_verdict(
        args.head_commit_at,
        head_commit_at,
        comments,
        args.regressed_into_stage,
    )
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
