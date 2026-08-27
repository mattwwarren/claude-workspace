"""Diff-integrity guards for the ``cw review`` payload commands (#1924, #1988).

Three checks, run against the raw ``diff`` text a payload carries: it is not an
unresolved placeholder, it does not repeat the same hunk for the same file, and
(when ``--base`` is passed) it is byte-identical to real ``git diff`` output.

Also holds the ``--base``/``--no-base-check`` alternatives pair —
:func:`_require_base_xor_no_base_check` and
:func:`_run_base_check_if_requested` — shared verbatim by
``cw review consolidate`` and ``cw review verify-fixes`` so the two commands'
diff verification cannot silently diverge.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.exceptions import (
    DiffBaseMismatchError,
    DuplicatedHunkError,
    PlaceholderDiffError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# #1924 placeholder-diff detection. Whole-value-only tokens: a payload whose
# `diff` strips down to exactly one of these never carried a diff at all.
# Deliberately narrow, mirroring `_is_placeholder_sentinel_text`'s discipline
# in `cw.auto_dev_result.parse` — do NOT broaden to "looks templated", since
# silently rejecting a genuine diff is a strictly worse bug than the one this
# catches.
_PLACEHOLDER_DIFF_TOKENS = frozenset({"<diff here>", "<insert diff>", "..."})

# Below this many stripped characters, text with no `diff --git` header at all
# is a stub rather than a diff. The floor alone is never sufficient: a real but
# heavily-truncated diff can be shorter than this, which is why the check
# requires the CONJUNCTION of "under the floor" and "no header".
_PLACEHOLDER_LENGTH_FLOOR = 40

# This module's own header matchers, deliberately independent of
# `cw.codex_review._diff`'s: that module owns a full unified-diff parser whose
# per-file buffers are never hunk-separated (it never resets on a bare `@@`
# line), so it cannot answer "did this hunk appear twice" and these guards must
# scan the text themselves.
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)
_DIFF_GIT_PATHS_RE = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")

# Shared verbatim by consolidate's and verify-fixes's --no-base-check options
# (#1988) so the two commands' escape-hatch documentation cannot silently
# diverge -- the same drift-by-duplication shape _require_base_xor_no_base_check
# already closes for the guard logic.
_NO_BASE_CHECK_HELP = (
    "Skip --base verification entirely: the payload's diff will "
    "NOT be checked against real git history, and findings may "
    "be adjudicated against an artifact nobody verified. For "
    "non-git-backed synthetic payloads (tests) and human "
    "post-hoc recovery debugging only — never for pipeline use, "
    "which always passes --base. Mutually exclusive with "
    "--base; exactly one of the two must be given."
)


def _check_not_placeholder_diff(diff_text: str) -> None:
    """Reject a ``diff`` field that never carried a real diff (#1924).

    Two independent triggers: an exact (case-sensitive) match against
    :data:`_PLACEHOLDER_DIFF_TOKENS` at any length, or the conjunction of
    "shorter than :data:`_PLACEHOLDER_LENGTH_FLOOR`" and "carries no
    ``diff --git`` header". A real diff containing ``...`` somewhere in a body
    line is untouched — the token match is whole-value-only.
    """
    stripped = diff_text.strip()
    if stripped in _PLACEHOLDER_DIFF_TOKENS:
        msg = (
            f"The payload's diff is the unresolved placeholder {stripped!r}, not "
            "a real unified diff. Capture the diff with `git diff` and pass its "
            "verbatim output."
        )
        raise PlaceholderDiffError(msg)
    if (
        len(stripped) < _PLACEHOLDER_LENGTH_FLOOR
        and _DIFF_GIT_HEADER_RE.search(diff_text) is None
    ):
        msg = (
            f"The payload's diff is {len(stripped)} characters and carries no "
            "`diff --git` header, so it cannot be a real unified diff. Capture "
            "the diff with `git diff` and pass its verbatim output."
        )
        raise PlaceholderDiffError(msg)


def _diff_git_path(header_line: str) -> str:
    """The b-side path named by a ``diff --git`` header, or the line itself."""
    match = _DIFF_GIT_PATHS_RE.match(header_line)
    return match.group("b") if match else header_line


def _iter_file_sections(diff_text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(path, body_lines)`` per ``diff --git`` section of *diff_text*."""
    current_path: str | None = None
    body: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current_path is not None:
                yield current_path, body
            current_path = _diff_git_path(raw)
            body = []
            continue
        if current_path is not None:
            body.append(raw)
    if current_path is not None:
        yield current_path, body


def _iter_hunks(body: list[str]) -> Iterator[str]:
    """Yield each ``@@``-headed hunk of one file section as a single string."""
    current: list[str] | None = None
    for raw in body:
        if raw.startswith("@@"):
            if current is not None:
                yield "\n".join(current)
            current = [raw]
            continue
        if current is not None:
            current.append(raw)
    if current is not None:
        yield "\n".join(current)


def _check_no_duplicate_hunks(diff_text: str) -> None:
    """Reject a diff repeating the same hunk for the same file (#1924).

    The duplicate key is ``(file path, hunk header + body)``, so the same hunk
    text under two different files — the same one-line change applied to two
    modules — is legitimate and passes. ``seen`` spans the whole document, not
    one section, so a diff concatenated with itself is caught even though each
    copy is internally consistent.
    """
    seen: set[tuple[str, str]] = set()
    for path, body in _iter_file_sections(diff_text):
        for hunk in _iter_hunks(body):
            key = (path, hunk)
            if key in seen:
                header = hunk.splitlines()[0]
                msg = (
                    f"The payload's diff repeats the same hunk for {path}: "
                    f"{header!r} appears more than once with identical content. "
                    "A diff reconstructed by hand is not evidence — re-capture "
                    "it with `git diff`."
                )
                raise DuplicatedHunkError(msg)
            seen.add(key)


def _check_diff_matches_base(
    diff_text: str, base: str, reviewed_sha: str, worktree: Path
) -> None:
    """Reject a payload whose diff is not ``git diff <base>...<sha>`` (#1924).

    Exact string equality after trimming a single trailing newline from each
    side — deliberately not a semantic diff comparison. The point is to prove
    the payload text came out of git verbatim; anything that "means the same
    thing" but does not match byte-for-byte was retyped. Called from both
    ``review_consolidate`` (#1924) and ``review_verify_fixes`` (#1988).
    """
    completed = subprocess.run(
        ["git", "diff", "--no-color", f"{base}...{reviewed_sha}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited {completed.returncode}"
        msg = (
            f"Could not compute `git diff {base}...{reviewed_sha}` in {worktree}: "
            f"{detail}"
        )
        raise DiffBaseMismatchError(msg)
    if diff_text.removesuffix("\n") != completed.stdout.removesuffix("\n"):
        msg = (
            f"The payload's diff does not match `git diff {base}...{reviewed_sha}` "
            f"in {worktree} (payload {len(diff_text)} chars, git "
            f"{len(completed.stdout)} chars). Pass the verbatim git output."
        )
        raise DiffBaseMismatchError(msg)


def _require_base_xor_no_base_check(base: str | None, no_base_check: bool) -> None:
    """Enforce --base/--no-base-check as a required alternatives pair (#1988).

    Why not click's own `required=True` (the dev-queue-prune precedent,
    src/cw/cli/dev_queue/crud.py): --base and --no-base-check are
    alternatives, so requiring either outright would forbid the other.
    This reproduces the same guarantee -- you cannot silently omit
    diff-integrity verification -- as a UsageError raised before any
    payload parsing runs. Shared by ``review_consolidate`` and
    ``review_verify_fixes`` so the two commands cannot silently diverge.
    """
    if base is None and not no_base_check:
        msg = "Must pass either --base <ref> or --no-base-check."
        raise click.UsageError(msg)
    if base is not None and no_base_check:
        msg = "--base and --no-base-check are mutually exclusive."
        raise click.UsageError(msg)


def _run_base_check_if_requested(
    diff: str, base: str | None, reviewed_sha: str, worktree: Path | None
) -> None:
    """Run --base verification when requested; no-op when base is None (#1988).

    Shared by ``review_consolidate`` and ``review_verify_fixes`` so worktree
    resolution and the check invocation cannot silently diverge between the
    two commands -- the same drift-by-duplication shape
    ``_require_base_xor_no_base_check`` already closes for the guard logic.
    """
    if base is None:
        return
    base_check_worktree = worktree if worktree is not None else Path.cwd()
    _check_diff_matches_base(diff, base, reviewed_sha, base_check_worktree)
