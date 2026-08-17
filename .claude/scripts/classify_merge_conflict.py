#!/usr/bin/env python3
"""Gate script: mechanically resolve only provably-safe merge conflicts (#1850).

Usage (from `/auto-dev-finalize` Step 4c.5, semantic auto-resolve attempt):
    classify_merge_conflict.py resolve \\
        --conflicted-files /tmp/conflicted-files-$CW_SESSION --json

Context: Step 4c.5 used to make exactly one mechanical rebase attempt against
a freshly-conflicted PR and, on failure, park unconditionally with
`blocker.reason: "merge_conflict_post_push"`. A large share of those parks are
conflicts no human would think twice about — two branches appending disjoint
CHANGELOG sections, two branches adding different imports to the same block,
one branch inserting where the other changed nothing.

This script is the deterministic half of automating exactly those and nothing
else. It is emphatically NOT a "resolution agent": it classifies each conflict
block by *shape*, resolves only three enumerated categories, and refuses
everything else without writing a byte. `prep-pr.md` Step 1's pre-push refusal
to auto-resolve ("a mis-resolved merge is worse than a surfaced block") is the
reasoning this script respects rather than overrides — a narrow enumerated
safe-set with a fail-closed default is the version of autonomous resolution
that reasoning does not rule out.

Safe categories (evaluated in this order, per block):
    one_sided_insert — one side of the block is entirely blank, i.e. the other
        branch made no textual change in this span. Resolution: keep the
        non-empty side.
    import_union — every non-blank line on BOTH sides is an import statement
        (Python `import`/`from ... import`, JS/TS `import ...`). Resolution:
        the union of unique lines in first-seen order (ours, then theirs).
    doc_append — the file path is on the documentation allowlist (see
        `is_doc_path`: CHANGELOG-named files or anything under the
        repo-root `docs/` directory — root-anchored, never a bare `.md`
        suffix or a `docs`-named directory nested elsewhere in the tree) and
        both sides are non-empty. Resolution: our block followed by their
        block. Gated by PATH, never by content shape: a CHANGELOG-shaped
        disjoint append inside a source file — or inside this repo's own
        orchestration prose, e.g. `.claude/commands/*.md` or
        `.claude/docs/coding/*.md` — is `unsafe`.

Anything else — overlapping edits, mixed content, malformed or diff3-style
markers, a listed file with no markers at all — is `unsafe`.

Atomic and fail-closed: every block in every named file must classify safe or
the command makes NO writes at all. A partially-resolved working tree is worse
than a parked one, because the caller's revert path is the only thing standing
between a bad resolution and a pushed merge commit.

Stdlib-only by design: nothing in `.claude/scripts/` depends on the `cw`
package, and this gate must keep working as a standalone script. Sibling of
`check_plan_scope_conformance.py` (#1779) and `check_impl_guard_staleness.py`
(#1794) in shape, exit convention, and JSON-verdict-to-stdout contract.

Exit codes:
    0  — every block classified safe; all files rewritten in place.
    1  — REFUSED: at least one unsafe block. No writes. The caller aborts the
         merge and parks with the unchanged `merge_conflict_post_push`
         sentinel, quoting the verdict in `blocker.details`.
    2  — usage / IO error (unreadable or empty `--conflicted-files` list, or an
         unreadable file named within it). No writes, nothing on stdout. The
         caller treats it exactly like exit 1 — same fail-closed-to-park
         convention as `check_plan_scope_conformance.py`'s exit 2.

The JSON verdict is written to stdout on exits 0 and 1:
    exit 0: {"safe": true, "resolved_files": [...], "categories": {cat: count}}
    exit 1: {"safe": false, "files": [{"path": ..., "unsafe_blocks": [...]}]}

Without `--json` the same verdict is summarised as a single human-readable
line, so the script is usable by hand during an incident.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

OURS_MARKER = "<<<<<<<"
BASE_MARKER = "|||||||"  # diff3 conflictStyle — deliberately unsupported
SEP_MARKER = "======="
THEIRS_MARKER = ">>>>>>>"

CATEGORY_ONE_SIDED = "one_sided_insert"
CATEGORY_IMPORT_UNION = "import_union"
CATEGORY_DOC_APPEND = "doc_append"
CATEGORY_UNSAFE = "unsafe"

REASON_NO_MARKERS = "no_conflict_markers"
REASON_MALFORMED = "malformed_conflict_markers"
REASON_UNSAFE_SHAPE = "overlapping_change_in_non_doc_path"

# Matches a Python (`import x`, `from x import y`) or JS/TS (`import {x}`,
# `import type {x}`, `import x from 'y'`) import statement. Leading whitespace
# is tolerated so an indented import block still unions cleanly.
_IMPORT_RE = re.compile(r"^\s*(?:import\b|from\s+\S+\s+import\b)")

_DOC_DIR = "docs"
_CHANGELOG_PREFIX = "CHANGELOG"


class ConflictParseError(Exception):
    """The file's markers are not a clean two-way conflict sequence."""


@dataclass(frozen=True)
class ConflictBlock:
    """One `<<<<<<< / ======= / >>>>>>>` span, split into its two sides."""

    ours: list[str]
    theirs: list[str]


@dataclass
class FileVerdict:
    """Per-file classification result. ``resolved_text`` is None when unsafe."""

    path: str
    resolved_text: str | None = None
    categories: dict[str, int] = field(default_factory=dict)
    unsafe_blocks: list[dict[str, object]] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.unsafe_blocks


def is_doc_path(path: str) -> bool:
    """Return True if *path* is on the documentation allowlist.

    The allowlist is deliberately narrow and purely path-based: a CHANGELOG by
    any extension, or anything under the **repo-root** ``docs/`` directory —
    matching the binding operator directive verbatim ("`doc_append` stays
    path-gated to the docs/CHANGELOG allowlist"). The invariant is root
    anchoring, not "a `docs` path segment anywhere": conflicted-file paths
    arrive repo-relative (from `git diff --name-only`), so `docs` must be the
    *first* path component, never merely present at any depth. A directory
    literally named `docs` nested elsewhere (e.g. this repo's own
    `.claude/docs/coding/`) is NOT the project's documentation tree and is NOT
    doc-safe — nor is a bare ``.md`` suffix anywhere in the tree. This repo's
    own orchestration prose that autonomous agents execute
    (``.claude/commands/*.md``, ``.claude/skills/**/*.md``,
    ``.claude/docs/coding/*.md``, ``CLAUDE.md``) must never qualify: a
    conflicting edit there is a semantic collision, not a disjoint-append
    shape, even though it can look path-eligible by suffix or by an
    unanchored substring/segment match. Content shape never promotes a source
    file into this category — that is the whole point of `doc_append` being
    path-gated.
    """
    parsed = Path(path)
    if parsed.name.upper().startswith(_CHANGELOG_PREFIX):
        return True
    return bool(parsed.parts) and parsed.parts[0] == _DOC_DIR


def _scan_side(
    lines: list[str],
    start: int,
    stop_marker: str,
    forbidden: tuple[str, ...],
) -> tuple[list[str], int]:
    """Collect lines from *start* until *stop_marker*, rejecting *forbidden*."""
    body: list[str] = []
    index = start
    while index < len(lines) and not lines[index].startswith(stop_marker):
        current = lines[index]
        if current.startswith(forbidden):
            message = f"unexpected marker at line {index + 1}: {current!r}"
            raise ConflictParseError(message)
        body.append(current)
        index += 1
    if index >= len(lines):
        message = f"unterminated conflict block: missing {stop_marker!r}"
        raise ConflictParseError(message)
    return body, index


def parse_segments(text: str) -> list[str | ConflictBlock]:
    """Split *text* into literal lines and `ConflictBlock`s, in file order.

    Raises `ConflictParseError` on anything that is not a clean two-way
    sequence: a stray separator/terminator, a nested opener, an unterminated
    block, or a diff3 base section. Every one of those means the caller's
    model of the file is wrong, and a wrong model must never produce a write.
    """
    segments: list[str | ConflictBlock] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith((SEP_MARKER, THEIRS_MARKER, BASE_MARKER)):
            message = f"stray marker at line {index + 1}: {line!r}"
            raise ConflictParseError(message)
        if not line.startswith(OURS_MARKER):
            segments.append(line)
            index += 1
            continue
        ours, index = _scan_side(
            lines, index + 1, SEP_MARKER, (OURS_MARKER, THEIRS_MARKER, BASE_MARKER)
        )
        theirs, index = _scan_side(
            lines, index + 1, THEIRS_MARKER, (OURS_MARKER, SEP_MARKER, BASE_MARKER)
        )
        segments.append(ConflictBlock(ours=ours, theirs=theirs))
        index += 1
    return segments


def classify_block(path: str, ours: list[str], theirs: list[str]) -> str:
    """Return the safe-resolution category for one block, or `unsafe`."""
    ours_body = [line for line in ours if line.strip()]
    theirs_body = [line for line in theirs if line.strip()]
    if not ours_body or not theirs_body:
        return CATEGORY_ONE_SIDED
    if all(_IMPORT_RE.match(line) for line in (*ours_body, *theirs_body)):
        return CATEGORY_IMPORT_UNION
    if is_doc_path(path):
        return CATEGORY_DOC_APPEND
    return CATEGORY_UNSAFE


def resolve_block(category: str, ours: list[str], theirs: list[str]) -> list[str]:
    """Render the resolved replacement lines for an already-classified block."""
    if category == CATEGORY_ONE_SIDED:
        return ours if any(line.strip() for line in ours) else theirs
    if category == CATEGORY_IMPORT_UNION:
        union: list[str] = []
        for line in (*ours, *theirs):
            if line.strip() and line not in union:
                union.append(line)
        return union
    return [*ours, *theirs]


def classify_file(path: str, text: str) -> FileVerdict:
    """Classify (and, if wholly safe, resolve) one conflicted file's content."""
    verdict = FileVerdict(path=path)
    try:
        segments = parse_segments(text)
    except ConflictParseError as exc:
        verdict.unsafe_blocks.append(
            {"block": None, "reason": REASON_MALFORMED, "detail": str(exc)}
        )
        return verdict

    resolved: list[str] = []
    ordinal = 0
    for segment in segments:
        if isinstance(segment, str):
            resolved.append(segment)
            continue
        ordinal += 1
        category = classify_block(path, segment.ours, segment.theirs)
        if category == CATEGORY_UNSAFE:
            verdict.unsafe_blocks.append(
                {
                    "block": ordinal,
                    "reason": REASON_UNSAFE_SHAPE,
                    "ours_head": segment.ours[0] if segment.ours else "",
                    "theirs_head": segment.theirs[0] if segment.theirs else "",
                }
            )
            continue
        verdict.categories[category] = verdict.categories.get(category, 0) + 1
        resolved.extend(resolve_block(category, segment.ours, segment.theirs))

    if ordinal == 0:
        verdict.unsafe_blocks.append({"block": None, "reason": REASON_NO_MARKERS})
    if verdict.unsafe_blocks:
        verdict.categories.clear()
        return verdict

    trailer = "\n" if text.endswith("\n") else ""
    verdict.resolved_text = "\n".join(resolved) + trailer
    return verdict


def _fail(message: str) -> int:
    print(f"classify_merge_conflict: {message}", file=sys.stderr)
    return 2


def _read_conflicted_paths(listing: Path) -> list[str] | None:
    try:
        raw = listing.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"could not read --conflicted-files {listing}: {exc}")
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _summarize(verdicts: list[FileVerdict], *, safe: bool) -> str:
    if safe:
        categories = _aggregate_categories(verdicts)
        detail = ", ".join(
            f"{name}={count}" for name, count in sorted(categories.items())
        )
        return f"classify_merge_conflict: resolved {len(verdicts)} file(s) — {detail}"
    unsafe_files = [v for v in verdicts if not v.safe]
    blocks = sum(len(v.unsafe_blocks) for v in unsafe_files)
    return (
        f"classify_merge_conflict: refused — {blocks} unsafe block(s) across"
        f" {len(unsafe_files)} file(s): {', '.join(v.path for v in unsafe_files)}"
    )


def _aggregate_categories(verdicts: list[FileVerdict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for verdict in verdicts:
        for name, count in verdict.categories.items():
            totals[name] = totals.get(name, 0) + count
    return totals


def _emit(payload: dict[str, object], summary: str, *, as_json: bool) -> None:
    print(json.dumps(payload, indent=2) if as_json else summary)


def cmd_resolve(args: argparse.Namespace) -> int:
    """Classify every named file and, only if all are safe, rewrite them."""
    paths = _read_conflicted_paths(Path(args.conflicted_files))
    if paths is None:
        return 2
    if not paths:
        return _fail(
            f"--conflicted-files {args.conflicted_files} named no files —"
            " nothing to resolve"
        )

    verdicts: list[FileVerdict] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _fail(f"could not read conflicted file {path}: {exc}")
        verdicts.append(classify_file(path, text))

    if any(not verdict.safe for verdict in verdicts):
        payload: dict[str, object] = {
            "safe": False,
            "files": [
                {"path": verdict.path, "unsafe_blocks": verdict.unsafe_blocks}
                for verdict in verdicts
                if not verdict.safe
            ],
        }
        _emit(payload, _summarize(verdicts, safe=False), as_json=args.json)
        return 1

    # Stage every write before performing any of them: "safe" is an
    # all-or-nothing verdict, so a half-rewritten working tree must be
    # impossible even if a defect made one resolution come back empty.
    pending: list[tuple[Path, str]] = []
    for verdict in verdicts:
        if verdict.resolved_text is None:
            return _fail(
                f"internal error: {verdict.path} classified safe but produced"
                " no resolution"
            )
        pending.append((Path(verdict.path), verdict.resolved_text))
    for target, content in pending:
        target.write_text(content, encoding="utf-8")

    _emit(
        {
            "safe": True,
            "resolved_files": [verdict.path for verdict in verdicts],
            "categories": _aggregate_categories(verdicts),
        },
        _summarize(verdicts, safe=True),
        as_json=args.json,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (resolved), 1 (refused), or 2 (usage/IO error)."""
    parser = argparse.ArgumentParser(
        description=(
            "Mechanically resolve merge conflicts whose shape is provably"
            " safe, and refuse — without writing — every other shape."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Classify and, if wholly safe, resolve the named conflicted files",
    )
    resolve_parser.add_argument(
        "--conflicted-files",
        required=True,
        help=(
            "Path to a newline-delimited list of conflicted files"
            " (git diff --name-only --diff-filter=U)"
        ),
    )
    resolve_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable JSON verdict instead of a summary line",
    )
    args = parser.parse_args(argv)
    return cmd_resolve(args)


if __name__ == "__main__":
    sys.exit(main())
