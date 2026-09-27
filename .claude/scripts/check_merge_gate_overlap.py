#!/usr/bin/env python3
# cw-script-version: 1
"""Gate script: narrow Step 4a's merge-gate file overlap before probing (#2431).

Usage (from `/auto-dev-finalize` Step 4a, headless merge gate, once per open
pipeline PR):
    check_merge_gate_overlap.py filter \\
        --branch-files <path> --pr-files <path> \\
        [--ignore-path a] [--ignore-path b] ... --json

Context: Step 4a used to park a candidate branch whenever its changed-file
list intersected any other open pipeline PR's. Two shapes of false positive
dominated those parks: (1) generated files nearly every PR touches — a
checked-in mypy baseline, a lock file — and (2) edits to the same file in
disjoint hunks, which git merges without complaint.

This script is the deterministic half of fixing shape 1 and of deciding when
shape 2 needs checking at all. It intersects the two lists, removes the
client's `merge_gate_ignore_paths` (each passed as its own `--ignore-path`
argument — exact repo-relative string match, never a glob or prefix), and
reports whether any overlap survives. A surviving overlap is not a verdict of
conflict: the caller escalates it to `git merge-tree --write-tree`, which owns
the content-aware decision. This script never shells out to git and never
reads client config — the fence reads `merge_gate_ignore_paths` from
`.claude/cw-context.json` and passes it in.

Stdlib-only by design: nothing in `.claude/scripts/` depends on the `cw`
package. Sibling of `classify_merge_conflict.py` (#1850) and
`check_plan_scope_conformance.py` (#1779) in shape, exit convention, and
JSON-verdict-to-stdout contract.

Exit codes:
    0  — non-blocking: no overlap survives the ignore list. The caller skips
         the merge-tree probe for this PR.
    1  — blocking: some overlap survives. The caller escalates to a
         `git merge-tree` probe.
    2  — usage / IO error (unreadable input list, or a list naming no files —
         an empty list means the caller's collection step failed). Nothing on
         stdout. The caller fails closed and blocks on this PR.

The JSON verdict is written to stdout on exits 0 and 1:
    {"blocking": bool, "overlap": [...], "ignored": [...],
     "overlap_after_ignore": [...]}

Without `--json` the same verdict is summarised as a single human-readable
line, so the script is usable by hand during an incident.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OverlapVerdict:
    """The intersection, the part excused by the ignore list, and the rest."""

    overlap: list[str]
    ignored: list[str]
    overlap_after_ignore: list[str]

    @property
    def blocking(self) -> bool:
        return bool(self.overlap_after_ignore)

    def as_payload(self) -> dict[str, object]:
        return {
            "blocking": self.blocking,
            "overlap": self.overlap,
            "ignored": self.ignored,
            "overlap_after_ignore": self.overlap_after_ignore,
        }


def compute_overlap(
    branch_files: list[str], pr_files: list[str], ignore_paths: list[str]
) -> OverlapVerdict:
    """Intersect the two lists and split the result by the exact-match ignore set."""
    overlap = set(branch_files) & set(pr_files)
    ignored = overlap & set(ignore_paths)
    return OverlapVerdict(
        overlap=sorted(overlap),
        ignored=sorted(ignored),
        overlap_after_ignore=sorted(overlap - ignored),
    )


def _fail(message: str) -> int:
    print(f"check_merge_gate_overlap: {message}", file=sys.stderr)
    return 2


def _read_file_list(flag: str, listing: Path) -> list[str] | None:
    try:
        raw = listing.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"could not read {flag} {listing}: {exc}")
        return None
    paths = [line.strip() for line in raw.splitlines() if line.strip()]
    if not paths:
        _fail(f"{flag} {listing} named no files — the caller's file list is empty")
        return None
    return paths


def _summarize(verdict: OverlapVerdict) -> str:
    if verdict.blocking:
        return (
            "check_merge_gate_overlap: blocking — overlap after ignore list: "
            + ", ".join(verdict.overlap_after_ignore)
        )
    ignored = ", ".join(verdict.ignored) if verdict.ignored else "none"
    return f"check_merge_gate_overlap: non-blocking — ignored overlap: {ignored}"


def cmd_filter(args: argparse.Namespace) -> int:
    """Emit the overlap verdict for one branch/PR file-list pair."""
    branch_files = _read_file_list("--branch-files", Path(args.branch_files))
    if branch_files is None:
        return 2
    pr_files = _read_file_list("--pr-files", Path(args.pr_files))
    if pr_files is None:
        return 2
    verdict = compute_overlap(branch_files, pr_files, args.ignore_path)
    payload = verdict.as_payload()
    print(json.dumps(payload, indent=2) if args.json else _summarize(verdict))
    return 1 if verdict.blocking else 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (non-blocking), 1 (blocking), or 2 (usage/IO)."""
    parser = argparse.ArgumentParser(
        description=(
            "Intersect a branch's and an open PR's changed-file lists, drop"
            " exact-match ignore paths, and report whether overlap survives."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    filter_parser = subparsers.add_parser(
        "filter",
        help="Report the overlap left after the ignore list",
    )
    filter_parser.add_argument(
        "--branch-files",
        required=True,
        help="Path to the candidate branch's newline-delimited changed files",
    )
    filter_parser.add_argument(
        "--pr-files",
        required=True,
        help="Path to the open PR's newline-delimited changed files",
    )
    filter_parser.add_argument(
        "--ignore-path",
        action="append",
        default=[],
        help="Repo-relative path to exclude (exact match); repeat per path",
    )
    filter_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable JSON verdict instead of a summary line",
    )
    args = parser.parse_args(argv)
    return cmd_filter(args)


if __name__ == "__main__":
    sys.exit(main())
