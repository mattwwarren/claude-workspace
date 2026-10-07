"""Code-change detection between SHAs and touched-thread tracking."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from review_monitor_lib.shell import _run_git

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR


# How many lines around a changed line to consider "code changed" for a thread
CODE_CHANGE_WINDOW = 5


def check_code_changed(
    file_path: str,
    line: int | None,
    changed_lines: dict[str, set[int]],
) -> bool:
    """Return True if any changed line falls within a window around *line*.

    The window is ``[line - CODE_CHANGE_WINDOW, line + CODE_CHANGE_WINDOW]``
    (inclusive on both ends).

    A *line* of None (an outdated / moved review thread, which GitHub anchors
    to no current line) has no window to test, so it never counts as touched.
    """
    lines = changed_lines.get(file_path)
    if lines is None or line is None:
        return False
    low = line - CODE_CHANGE_WINDOW
    high = line + CODE_CHANGE_WINDOW
    return any(low <= changed <= high for changed in lines)


def parse_diff_changed_lines(diff_output: str) -> dict[str, set[int]]:
    """Parse a unified diff and return added line numbers keyed by file path.

    Only lines that are *added* (``+`` prefix, not ``+++`` header) in the new
    version are included.  The file paths are taken from ``+++ b/<path>``
    headers.

    Returns
    -------
        Mapping of file path to set of new-file line numbers that were added.
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line: int = 0  # tracks position in the new file

    # Matches: @@ -old_start[,old_count] +new_start[,new_count] @@
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for raw_line in diff_output.splitlines():
        # New-file header — e.g. "+++ b/src/foo.py"
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]  # strip "+++ b/"
            result.setdefault(current_file, set())
            current_line = 0
            continue

        # Hunk header — reset the new-file line counter
        m = hunk_re.match(raw_line)
        if m:
            current_line = int(m.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+++"):
            # Skip the +++ header line itself (already handled above)
            continue

        if raw_line.startswith("+"):
            # Added line — record and advance
            result[current_file].add(current_line)
            current_line += 1
        elif raw_line.startswith("-"):
            # Removed line — does not advance new-file counter
            pass
        else:
            # Context line — advance new-file counter
            current_line += 1

    return result


def _apply_code_changes(
    pr: MonitoredPR,
    base_sha: str,
    new_sha: str,
) -> tuple[bool, str | None, list[str]]:
    """Run git diff and identify threads whose lines the new commits touched.

    Does NOT mark threads addressed. A line being touched is a *candidate*
    signal — the commit may have changed that line for an unrelated reason.
    The delta-review confirmation pass (skill side) decides whether the change
    actually addresses the thread's comment and, if so, calls the
    ``confirm-thread`` subcommand to set ``code_changed``.

    Returns ``(has_delta_diff, delta_diff_text, touched_thread_ids)``.
    """
    diff_output = _run_git(["diff", f"{base_sha}..{new_sha}"], cwd=pr.repo_path)
    changed_lines = parse_diff_changed_lines(diff_output)
    touched = [
        tid
        for tid, ts in pr.thread_status.items()
        if check_code_changed(ts.file, ts.line, changed_lines)
    ]
    if pr.role == "reviewer" and diff_output:
        return True, diff_output, touched
    return False, None, touched


def _detect_touched_threads(
    pr: MonitoredPR, *, delta_base_sha: str, new_sha: str
) -> tuple[bool, str | None, list[str]]:
    """Detect threads touched by commits since the delta baseline.

    The diff is computed from ``delta_base_sha`` (not ``last_seen_sha``): an
    unconsumed reviewer delta keeps surfacing every cycle until the skill acks
    it, so the delta is never lost if Step 3 misses a cycle.

    A touched thread is a candidate for resolution — the skill's delta
    confirmation pass verifies and calls ``confirm-thread`` to mark it. A stale
    repo_path (e.g. a cleaned-up worktree) is skipped, not fatal — the cycle
    still completes; delta detection resumes once the path is valid again.
    """
    if delta_base_sha != new_sha and pr.repo_path and Path(pr.repo_path).is_dir():
        return _apply_code_changes(pr, delta_base_sha, new_sha)
    return False, None, []
