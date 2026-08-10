"""Unified-diff capture and parsing for the codex-review package.

Captures ``git diff <default_branch>...HEAD`` and splits it into per-file hunk
text, a per-file added-line map, and the full changed-path list (including pure
deletions) from a single parse — avoiding a second ``git diff --name-only``
subprocess (SHOULD_FIX 11, #1236). Consumed by ``core``'s review-pass assembly.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from cw.review_findings import CapturedDiff

if TYPE_CHECKING:
    from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# Matches every ``diff --git a/<path> b/<path>`` header, including files with
# no added lines at all (pure deletions) — the header line is present
# regardless of what follows, unlike ``+++ b/<path>`` (absent for deletions,
# replaced with ``+++ /dev/null``). Used to derive the changed-file list from
# a single already-parsed diff instead of a second ``git diff --name-only``
# subprocess call (Performance, SHOULD_FIX 11).
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/.+ b/(.+)$")


def _parse_hunk_new_start(header: str) -> int:
    """Return the new-file starting line number from a ``@@`` hunk header."""
    match = _HUNK_RE.match(header)
    return int(match.group(1)) if match else 0


def _apply_hunk_line(
    raw: str,
    current_file: str,
    new_line_no: int,
    file_line_text: dict[str, dict[int, str]],
    file_window_text: dict[str, dict[int, str]],
) -> int:
    """Classify one hunk-body line and return the next new-file line number.

    Extracted from :func:`_parse_unified_diff` to keep that function's branch
    count under the repo's ``PLR0912`` ceiling (#1738).
    """
    if raw.startswith("@@"):
        return _parse_hunk_new_start(raw)
    if raw.startswith("+"):
        file_line_text[current_file][new_line_no] = raw[1:]
        file_window_text[current_file][new_line_no] = raw[1:]
        return new_line_no + 1
    if raw.startswith("-"):
        return new_line_no
    if raw.startswith("\\"):
        # `\ No newline at end of file` -- diff metadata, not file content.
        return new_line_no
    file_window_text[current_file][new_line_no] = raw[1:] if raw[:1] == " " else raw
    return new_line_no + 1


def _parse_unified_diff(
    diff_text: str,
) -> tuple[
    dict[str, str], dict[str, dict[int, str]], dict[str, dict[int, str]], list[str]
]:
    """Split a unified diff into per-file hunk text and per-file line-content maps.

    Tracks the new-file line number through each ``@@ -a,b +c,d @@`` header:
    ``+`` and context lines advance the counter, ``-`` lines do not. Deleted
    files (``+++ /dev/null``) contribute no new-file lines. Returns
    ``(file_diffs, file_line_text, file_window_text, changed_files)``; the
    caller derives ``files`` from ``file_line_text`` so the two can never
    drift. ``file_window_text`` (#1738) is the superset of ``file_line_text``
    that also captures unchanged CONTEXT-line content at its real new-file
    line number — ``file_line_text`` stays added-only, byte-identical to
    before, since it feeds ``_line_reference_valid``'s anchor-validity gate
    (``cw.review_findings``), which must not be loosened by this change.
    ``changed_files`` is every path named by a ``diff --git`` header, in diff
    order — including pure deletions, which contribute nothing to
    ``file_diffs``/``file_line_text``/``file_window_text`` but must still
    appear in the changed-file list (SHOULD_FIX 11, #1236: this replaces a
    second, redundant ``git diff --name-only`` subprocess call).
    """
    file_diffs: dict[str, str] = {}
    file_line_text: dict[str, dict[int, str]] = {}
    file_window_text: dict[str, dict[int, str]] = {}
    changed_files: list[str] = []
    current_file: str | None = None
    current_lines: list[str] = []
    new_line_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current_file is not None:
                file_diffs[current_file] = "\n".join(current_lines)
            current_file = None
            current_lines = []
            header_match = _DIFF_GIT_HEADER_RE.match(raw)
            if header_match:
                changed_files.append(header_match.group(1))
            continue
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/")
            current_file = None if path == "/dev/null" else path
            if current_file is not None:
                file_line_text.setdefault(current_file, {})
                file_window_text.setdefault(current_file, {})
            current_lines.append(raw)
            continue
        if current_file is None:
            continue
        current_lines.append(raw)
        new_line_no = _apply_hunk_line(
            raw, current_file, new_line_no, file_line_text, file_window_text
        )

    if current_file is not None:
        file_diffs[current_file] = "\n".join(current_lines)
    return file_diffs, file_line_text, file_window_text, changed_files


def _capture_diff(
    worktree: Path, default_branch: str
) -> tuple[CapturedDiff, str, list[str]]:
    """Capture ``git diff <default_branch>...HEAD`` as a :class:`CapturedDiff`.

    ``files`` (on the returned ``CapturedDiff``) is derived from
    ``file_line_text`` (the added-line map) so it can never drift from the
    per-line content. Returns ``(diff, reviewed_sha, changed_files)`` —
    ``changed_files`` is the full changed-path list (including pure
    deletions), parsed from this same diff text rather than a second
    subprocess call (SHOULD_FIX 11, #1236).
    """
    reviewed_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()
    diff_text = subprocess.check_output(
        ["git", "diff", "--no-color", f"{default_branch}...HEAD"],
        cwd=worktree,
        text=True,
    )
    file_diffs, file_line_text, file_window_text, changed_files = _parse_unified_diff(
        diff_text
    )
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    diff = CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=file_window_text,
    )
    return diff, reviewed_sha, changed_files
