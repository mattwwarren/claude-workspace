"""Redact, wrap, split, post and read back an agent-authored tracker comment (#2810).

The library half of ``cw post-comment``, the one deterministic seam every
auto-dev stage routes agent or tool findings text through before it reaches a
public GitHub issue. The session hands over raw text; this module redacts it
(:mod:`cw.redact_post_text`), measures the redacted text, wraps it in the
caller's header, sub-heading, part label and fence, splits it into comments
that fit, posts them oldest first through :func:`cw.gh.post_issue_comment`
(which appends the provenance marker) and reads every part back by the
comment URL ``gh`` prints.

Pure helpers: :func:`strip_trailing_marker`, :func:`fence_for`,
:func:`wrap_part`, :func:`split_text`, :func:`plan_parts`,
:func:`normalize_for_compare`, :func:`readback_matches`,
:func:`parse_comment_url` and :func:`find_comment_body`. Orchestration:
:func:`post_parts`. The visibility check, the body file and the result JSON
live in :mod:`cw.cli.post_comment`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.gh import (
    AGENT_COMMENT_MARKER,
    FETCH_COMMENTS_TIMEOUT,
    agent_marked_body,
    fetch_issue_comments,
    post_issue_comment,
)
from cw.redact_post_text import redact_post_text

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "MAX_COMMENT_CHARS",
    "PART_TEXT_CHARS",
    "PartPlan",
    "PostReason",
    "PostResult",
    "PostStatus",
    "PostedPart",
    "fence_for",
    "find_comment_body",
    "normalize_for_compare",
    "parse_comment_url",
    "plan_parts",
    "post_parts",
    "readback_matches",
    "split_text",
    "strip_trailing_marker",
    "wrap_part",
]

# Every posted comment, wrapper and marker included, stays at or under this
# many characters: under GitHub's 65,536-character limit, and the threshold the
# Linear --emit-only parts share.
MAX_COMMENT_CHARS = 60_000
# The redacted text per part, leaving 1,000 characters for the wrapper. Text at
# or under this size posts as one unlabelled comment.
PART_TEXT_CHARS = 59_000
# A comment not yet visible right after `gh issue comment` returns is fetched
# again; a mismatch is never retried.
_READBACK_ATTEMPTS = 3
_READBACK_DELAY_SECONDS = 2.0
_MIN_FENCE_LENGTH = 3
_BACKTICK_RUN = re.compile(r"`+")
_COMMENT_URL = re.compile(r"https://\S+?#issuecomment-\d+")
_CRLF = "\r\n"


class PostStatus(StrEnum):
    """The outcome of :func:`post_parts`; values are the result-JSON strings."""

    POSTED = "posted"
    REFUSED = "refused"
    PARTIAL = "partial"


class PostReason(StrEnum):
    """Why a post was refused (nothing public) or partial (maybe public).

    Closed set; the stage docs list every value with its exit code.
    """

    BODY_UNREADABLE = "body_unreadable"
    EMPTY_BODY = "empty_body"
    TRACKER_NOT_GITHUB = "tracker_not_github"
    VISIBILITY_UNDETERMINED = "visibility_undetermined"
    SINGLE_PART_TOO_LONG = "single_part_too_long"
    POST_FAILED = "post_failed"
    POST_OUTCOME_UNKNOWN = "post_outcome_unknown"
    URL_MISSING = "url_missing"
    READBACK_MISMATCH = "readback_mismatch"
    READBACK_MISSING = "readback_missing"


@dataclass(frozen=True)
class PostedPart:
    """One part whose ``gh issue comment`` returned a comment URL (1-based)."""

    index: int
    url: str


@dataclass(frozen=True)
class PostResult:
    """What :func:`post_parts` did: *posted* lists every part that is public."""

    status: PostStatus
    reason: PostReason | None
    posted: tuple[PostedPart, ...]
    parts_total: int


@dataclass(frozen=True)
class PartPlan:
    """The wrapped bodies to post (marker not yet appended).

    *redacted* is True when redaction changed the text. *fits* is False only
    in single mode when the one body is too long; *bodies* then holds the split
    the text would need, so its length is the part count to report.
    """

    bodies: tuple[str, ...]
    redacted: bool
    fits: bool = True


def strip_trailing_marker(text: str) -> str:
    """Drop any trailing provenance markers, so posting never doubles one.

    Text that does not end with the marker is returned unchanged; otherwise the
    marker and the whitespace around it are removed, repeatedly. Idempotent.
    """
    stripped = text.rstrip()
    if not stripped.endswith(AGENT_COMMENT_MARKER):
        return text
    while stripped.endswith(AGENT_COMMENT_MARKER):
        stripped = stripped.removesuffix(AGENT_COMMENT_MARKER).rstrip()
    return stripped


def fence_for(text: str) -> str:
    """A backtick fence one longer than the longest run in *text*, at least 3."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    return "`" * max(_MIN_FENCE_LENGTH, longest + 1)


def _fenced(text: str) -> str:
    fence = fence_for(text)
    inner = text if text.endswith("\n") else f"{text}\n"
    return f"{fence}\n{inner}{fence}"


def wrap_part(
    text: str,
    *,
    index: int,
    total: int,
    header: str | None = None,
    subheading: str | None = None,
    fence: bool = False,
) -> str:
    """Wrap one part: header, sub-heading, label, then the (fenced) text.

    Each present line is separated from the next by a blank line. The label
    ``(part i of n)`` appears only when *total* > 1. With no header,
    sub-heading or label the result is the bare (fenced) text.
    """
    label = f"(part {index} of {total})" if total > 1 else None
    lines = [line for line in (header, subheading, label) if line]
    content = _fenced(text) if fence else text
    return "\n\n".join([*lines, content])


def _chunk_line(line: str, limit: int) -> list[str]:
    """Split one over-long *line* into pieces of at most *limit* characters.

    A cut never falls between the ``\\r`` and ``\\n`` of a CRLF pair.
    """
    pieces: list[str] = []
    while len(line) > limit:
        cut = limit
        if cut > 1 and line[cut - 1 : cut + 1] == _CRLF:
            cut -= 1
        pieces.append(line[:cut])
        line = line[cut:]
    if line:
        pieces.append(line)
    return pieces


def split_text(text: str, limit: int = PART_TEXT_CHARS) -> list[str]:
    """Split *text* on line boundaries into parts of at most *limit* characters.

    A single line longer than *limit* is split inside the line. Nothing is
    dropped or rewritten: ``"".join(split_text(t)) == t``, line endings
    included.
    """
    if limit < 1:
        msg = f"split limit must be positive, got {limit}"
        raise ValueError(msg)
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        for piece in _chunk_line(line, limit):
            if current and len(current) + len(piece) > limit:
                parts.append(current)
                current = ""
            current += piece
    if current or not parts:
        parts.append(current)
    return parts


def _wrap_all(
    chunks: Sequence[str], *, header: str | None, subheading: str | None, fence: bool
) -> tuple[str, ...]:
    total = len(chunks)
    return tuple(
        wrap_part(
            chunk,
            index=index,
            total=total,
            header=header,
            subheading=subheading,
            fence=fence,
        )
        for index, chunk in enumerate(chunks, start=1)
    )


def _excess(bodies: Sequence[str]) -> int:
    """How far the longest marked body runs over :data:`MAX_COMMENT_CHARS`."""
    return max(len(agent_marked_body(body)) for body in bodies) - MAX_COMMENT_CHARS


def _split_to_fit(
    text: str, *, header: str | None, subheading: str | None, fence: bool
) -> tuple[str, ...]:
    """Split and wrap *text*, shrinking the part size until every part fits.

    The wrapper's size depends on the part (its fence) and the part count (its
    label), so the measure is of the wrapped, marked body. Each round lowers
    the limit by the overshoot, or halves it when the overshoot is larger than
    half the limit, so the limit falls strictly every round and the loop ends.
    """
    limit = PART_TEXT_CHARS
    while True:
        bodies = _wrap_all(
            split_text(text, limit), header=header, subheading=subheading, fence=fence
        )
        excess = _excess(bodies)
        if excess <= 0:
            return bodies
        limit = max(limit - excess, limit // 2)
        if limit < 1:
            msg = (
                "the comment wrapper alone (header, sub-heading, label, fence and"
                f" marker) exceeds {MAX_COMMENT_CHARS} characters"
            )
            raise ValueError(msg)


def plan_parts(
    text: str,
    *,
    home: str,
    worktrees: Sequence[str],
    header: str | None = None,
    subheading: str | None = None,
    fence: bool = False,
    single: bool = False,
) -> PartPlan:
    """Redact *text*, then measure and wrap it into the bodies to post.

    Redaction runs first and is never skipped: what is measured, split and
    posted is the redacted text. A trailing provenance marker is stripped
    before redaction (the post appends it). *single* asks for exactly one
    comment; a body too long for that comes back with ``fits=False``.
    """
    unmarked = strip_trailing_marker(text)
    redacted_text = redact_post_text(unmarked, home=home, worktrees=worktrees)
    redacted = redacted_text != unmarked
    if single:
        body = wrap_part(
            redacted_text,
            index=1,
            total=1,
            header=header,
            subheading=subheading,
            fence=fence,
        )
        if _excess([body]) <= 0:
            return PartPlan(bodies=(body,), redacted=redacted)
    bodies = _split_to_fit(
        redacted_text, header=header, subheading=subheading, fence=fence
    )
    return PartPlan(bodies=bodies, redacted=redacted, fits=not single)


def normalize_for_compare(text: str) -> str:
    """CRLF to LF, then strip trailing whitespace; nothing else changes."""
    return text.replace(_CRLF, "\n").rstrip()


def readback_matches(sent: str, fetched: str) -> bool:
    """True when the normalised sides are equal and neither is empty."""
    want = normalize_for_compare(sent)
    got = normalize_for_compare(fetched)
    return bool(want) and bool(got) and want == got


def parse_comment_url(stdout: str | bytes) -> str | None:
    """The ``#issuecomment-<id>`` URL ``gh issue comment`` printed, or None."""
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    match = _COMMENT_URL.search(text)
    return match.group(0) if match else None


def find_comment_body(comments: Sequence[dict[str, object]], url: str) -> str | None:
    """The body of the comment whose ``url`` is *url*, or None.

    Matched by URL, never by header, so an older comment with the same header
    cannot stand in for the one just posted.
    """
    for comment in comments:
        body = comment.get("body")
        if comment.get("url") == url and isinstance(body, str):
            return body
    return None


def _read_back(
    ticket_id: str, url: str, sent: str, *, cwd: Path | None
) -> PostReason | None:
    """Fetch the comment at *url* and compare it with *sent*.

    None when it matches; ``READBACK_MISMATCH`` on a difference (not retried);
    ``READBACK_MISSING`` when it is still absent after every attempt.
    """
    for attempt in range(_READBACK_ATTEMPTS):
        if attempt:
            time.sleep(_READBACK_DELAY_SECONDS)
        comments = fetch_issue_comments(ticket_id, FETCH_COMMENTS_TIMEOUT, cwd=cwd)
        fetched = find_comment_body(comments or [], url)
        if fetched is not None:
            if readback_matches(sent, fetched):
                return None
            return PostReason.READBACK_MISMATCH
    return PostReason.READBACK_MISSING


def post_parts(ticket: str, bodies: Sequence[str], *, cwd: Path | None) -> PostResult:
    """Post *bodies* oldest first and read each one back; stop at the first fault.

    A first part whose ``gh`` exits non-zero is ``refused`` (nothing public).
    Every other fault is ``partial``: a later non-zero exit, an unknown outcome
    (``post_issue_comment`` returned None, so the comment may have landed, even
    for part 1), a zero exit with no URL, or a read-back that mismatched or
    never found the comment. A part whose URL was returned is listed in
    ``posted`` even when its read-back then failed: it is public.
    """
    ticket_id = ticket.lstrip("#")
    total = len(bodies)
    posted: list[PostedPart] = []

    def _stop(status: PostStatus, reason: PostReason) -> PostResult:
        return PostResult(status, reason, tuple(posted), total)

    for index, body in enumerate(bodies, start=1):
        proc = post_issue_comment(ticket_id, body, cwd=cwd)
        if proc is None:
            return _stop(PostStatus.PARTIAL, PostReason.POST_OUTCOME_UNKNOWN)
        if proc.returncode != 0:
            status = PostStatus.REFUSED if index == 1 else PostStatus.PARTIAL
            return _stop(status, PostReason.POST_FAILED)
        url = parse_comment_url(proc.stdout)
        if url is None:
            return _stop(PostStatus.PARTIAL, PostReason.URL_MISSING)
        posted.append(PostedPart(index=index, url=url))
        fault = _read_back(ticket_id, url, agent_marked_body(body), cwd=cwd)
        if fault is not None:
            return _stop(PostStatus.PARTIAL, fault)
    return PostResult(PostStatus.POSTED, None, tuple(posted), total)
