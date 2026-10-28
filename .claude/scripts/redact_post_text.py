#!/usr/bin/env python3
# cw-script-version: 1
"""Redact paths and secrets from text bound for a public tracker post (#2794).

Usage (from `/auto-dev-finalize`'s "Finalize review-findings comment" step):
    python3 .claude/scripts/redact_post_text.py \\
        --worktree "$GUARD_ROOT" --worktree "$CTX_WORKTREE" \\
        < raw.txt > redacted.txt

A FINALIZE run whose `/prep-pr` ship-it gate blocked on review findings posts
those findings to the ticket so the regressed IMPL run can read them. The
repository is public, and finding text routinely carries this machine's
worktree paths, home directory and `~/.claude/projects/<encoded>` transcript
directory names (the encoded name embeds the username), so the text is run
through this script before it is split into parts and posted.

Shapes replaced, paths first, then secrets:
    1. each `--worktree <path>` value, longest first   -> `<worktree>`
    2. `(/home/<u>|/Users/<u>|$HOME|~)/.cw/wt/<hash>[/<name>]` -> `<worktree>`
    3. `(/home/<u>|/Users/<u>|$HOME|~)/.claude/projects/<...>`
                                     -> `<home>/.claude/projects/<redacted>`
    4. the `$HOME` value, `/home/<u>` and `/Users/<u>`  -> `<home>`
    5. secret shapes (a mirror of `cw._text._SECRET_PATTERNS`) -> `<redacted>`

Redaction replaces spans and never truncates: the output differs from the
input only inside replaced spans, a missing trailing newline stays missing,
and the CLI round-trips CRLF and undecodable bytes unchanged. Over-redaction is
tolerated, as in `cw._text`.

Stdlib-only by design: nothing in `.claude/scripts/` depends on the `cw`
package, so the secret patterns are a mirrored copy, pinned equal to
`cw._text._SECRET_PATTERNS` by `tests/test_redact_post_text.py`.

Exit codes:
    0  -- redacted text written to stdout
    2  -- usage error (argparse)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import TextIO

WORKTREE_PLACEHOLDER = "<worktree>"
HOME_PLACEHOLDER = "<home>"
REDACTION_PLACEHOLDER = "<redacted>"
TRANSCRIPT_PLACEHOLDER = f"{HOME_PLACEHOLDER}/.claude/projects/{REDACTION_PLACEHOLDER}"

# Undecodable input bytes survive the round trip instead of failing the post.
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"

# One path segment. A trailing "." is never part of it, so a path that ends a
# sentence keeps its full stop.
_SEGMENT = r"[\w-]+(?:\.[\w-]+)*"
_USER_HOMES = rf"/home/{_SEGMENT}|/Users/{_SEGMENT}"
# A replaced value must not be the prefix of a longer name ("wt-7" in "wt-70").
_NOT_MID_NAME = r"(?![\w-])"

# Mirror of cw._text._SECRET_PATTERNS; see that module for the rationale of
# each shape. Kept byte-identical by a parity test.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?<=[=:])[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?i)\b(?:Authorization|token)\s*[:=]\s*\S+"),
)


def _usable(value: str) -> str:
    """Return *value* without trailing slashes, or "" when nothing is left.

    An empty value must never reach a replacement: replacing "" inserts the
    placeholder between every character of the text.
    """
    return value.strip().rstrip("/")


def _home_prefixes(home: str) -> str:
    """The alternation of every home-directory spelling, *home* included."""
    alternatives = [_USER_HOMES, "~"]
    if home:
        alternatives.append(re.escape(home))
    return "|".join(alternatives)


def redact_paths(text: str, *, home: str, worktrees: Sequence[str]) -> str:
    """Replace worktree, transcript-dir and home paths in *text* with placeholders."""
    usable_home = _usable(home)
    usable_worktrees = {_usable(w) for w in worktrees} - {""}
    for worktree in sorted(usable_worktrees, key=len, reverse=True):
        text = re.sub(re.escape(worktree) + _NOT_MID_NAME, WORKTREE_PLACEHOLDER, text)
    prefixes = _home_prefixes(usable_home)
    text = re.sub(
        rf"(?:{prefixes})/\.cw/wt/{_SEGMENT}(?:/{_SEGMENT})?",
        WORKTREE_PLACEHOLDER,
        text,
    )
    text = re.sub(
        rf"(?:{prefixes})/\.claude/projects/{_SEGMENT}(?:/{_SEGMENT})*",
        TRANSCRIPT_PLACEHOLDER,
        text,
    )
    if usable_home:
        text = re.sub(re.escape(usable_home) + _NOT_MID_NAME, HOME_PLACEHOLDER, text)
    return re.sub(rf"(?:{_USER_HOMES}){_NOT_MID_NAME}", HOME_PLACEHOLDER, text)


def redact_secrets(text: str) -> str:
    """Replace known secret shapes in *text*, exactly as `cw._text.redact` does."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTION_PLACEHOLDER, text)
    return text


def redact_post_text(text: str, *, home: str, worktrees: Sequence[str]) -> str:
    """Redact paths, then secrets. Never truncates."""
    return redact_secrets(redact_paths(text, home=home, worktrees=worktrees))


def _resolve_home(environ: Mapping[str, str]) -> str:
    """`$HOME` when set (an empty value skips the home shapes), else `~`."""
    home = environ.get("HOME")
    if home is not None:
        return home
    try:
        return str(Path("~").expanduser())
    except RuntimeError:
        return ""


def _read_stdin(stdin: TextIO | None) -> str:
    if stdin is not None:
        return stdin.read()
    return sys.stdin.buffer.read().decode(_ENCODING, _ERRORS)


def _write_stdout(stdout: TextIO | None, text: str) -> None:
    if stdout is not None:
        stdout.write(text)
        return
    sys.stdout.buffer.write(text.encode(_ENCODING, _ERRORS))
    sys.stdout.buffer.flush()


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Redact stdin to stdout. Return 0; argparse exits 2 on a usage error."""
    parser = argparse.ArgumentParser(
        description="Redact worktree/home paths and secrets from a tracker post.",
    )
    parser.add_argument(
        "--worktree",
        action="append",
        default=[],
        help="a worktree path to replace with <worktree> (repeatable)",
    )
    args = parser.parse_args(argv)
    worktrees: list[str] = args.worktree
    env: Mapping[str, str] = environ if environ is not None else os.environ
    text = _read_stdin(stdin)
    _write_stdout(
        stdout, redact_post_text(text, home=_resolve_home(env), worktrees=worktrees)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
