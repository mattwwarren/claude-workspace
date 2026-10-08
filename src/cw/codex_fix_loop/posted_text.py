"""The one redact-and-cap helper for text a fix-loop guard posts (#2633).

Every string a guard derives from added source lines or subprocess output and
posts to the tracker (hook output, growth and constraint ``details`` lines)
goes through here, so the guards cannot drift apart. :func:`redact_and_cap`
runs :func:`cw._text.redact` over the whole text first, so no cap can split a
secret and leave a prefix behind, then applies a per-line, a line-count and a
total cap. ``cw._text.redact`` misses short ``password=...`` values and AWS
key ids, so :func:`describe_added_line` and :func:`withhold_secret_lines`
withhold a line outright when it looks like an assignment to a secret-named
identifier or carries an AWS access key id.
"""

from __future__ import annotations

import re

from cw._text import redact

ADDED_LINE_MAX_CHARS = 120
POSTED_TEXT_MAX_CHARS = 2000
TRUNCATION_MARKER = "...[truncated]"
WITHHELD = "<source text withheld: looks like a secret assignment>"

# An assignment (`=` or `:`) to an identifier, dotted name or quoted key whose
# token contains a secret-ish word. Anchored at a token start so `monkey_patch`
# never matches.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w.])[\"']?[\w.]*"
    r"(?:secret|passw(?:or)?d|token|api[_-]?key|private[_-]?key|credential)"
    r"[\w.]*[\"']?\s*(?::\s*\w+\s*)?[:=]"
)
_AWS_KEY_ID = re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b")


def looks_like_secret(line: str) -> bool:
    """Whether *line* assigns a secret-named identifier or holds an AWS key id."""
    return bool(_SECRET_ASSIGNMENT.search(line) or _AWS_KEY_ID.search(line))


def redact_and_cap(
    text: str,
    *,
    max_line_chars: int = ADDED_LINE_MAX_CHARS,
    max_lines: int | None = None,
    max_total_chars: int = POSTED_TEXT_MAX_CHARS,
) -> str:
    """Redact *text*, then cap each line, the line count and the total length.

    Appends :data:`TRUNCATION_MARKER` exactly once when anything was cut.
    """
    lines = redact(text).splitlines()
    cut = any(len(line) > max_line_chars for line in lines)
    lines = [line[:max_line_chars] for line in lines]
    if max_lines is not None and len(lines) > max_lines:
        lines, cut = lines[:max_lines], True
    out = "\n".join(lines)
    if len(out) > max_total_chars:
        out, cut = out[:max_total_chars], True
    return f"{out}{TRUNCATION_MARKER}" if cut else out


def withhold_secret_lines(text: str) -> str:
    """Replace each line of *text* that :func:`looks_like_secret` with WITHHELD."""
    return "\n".join(
        WITHHELD if looks_like_secret(line) else line for line in text.splitlines()
    )


def describe_added_line(text: str) -> str:
    """The postable form of one added source line: withheld, or redacted and capped."""
    return WITHHELD if looks_like_secret(text) else redact_and_cap(text)
