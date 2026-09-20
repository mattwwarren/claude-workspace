"""Transcript sentinel helpers shared across CLI commands.

Both ``signal-stop`` (headless DAEMON backstop) and ``dev-queue wait``
(sentinel-aware polling) need to detect and parse the AUTO_DEV_RESULT
sentinel inside a Claude session transcript. The logic lives here so both
command submodules import the same implementation.

GitHub #2135 adds a sibling reader to the same transcripts:
:func:`_park_comment_posted_in_transcript` answers "did this session post its
own park/blocker comment and then stop without emitting a sentinel?". It is a
separate walk rather than an extension of the sentinel readers above because
those answer a different question — ``_iter_sentinel_text_blocks``
deliberately excludes ``tool_use`` blocks, and the reconcile package's
``_detect_dangling_tool_use`` reports only the *unresolved* tail.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, NamedTuple

from cw._util import (
    _iter_sentinel_text_blocks,
    _iter_tool_result_text,
    claude_project_dir,
)
from cw.auto_dev_result import (
    _CLOSE_SENTINEL,
    _OPEN_SENTINEL,
    AutoDevResult,
    BlockedResult,
    _is_placeholder_sentinel_text,
    extract_block,
    is_documented_example,
    parse_stdout,
)
from cw.gh import is_agent_authored

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _parse_sentinel_from_transcript(
    cwd: str,
    claude_session_id: str | None,
    *,
    warned_blocks: set[str] | None = None,
) -> AutoDevResult | BlockedResult | None:
    """Return the parsed sentinel from the transcript, or None if absent.

    Claude stores session transcripts at:
      ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``

    where the encoded path replaces both ``/`` and ``.`` with ``-``. The JSONL
    contains one event per line; ``assistant`` events carry ``message.content``
    blocks whose ``text`` fields hold the model output, JSON-escaped (real
    newlines become the two-character sequence ``\\n``). Running ``extract_block``
    against the raw file therefore misses sentinels that are valid in their
    decoded form, so this scans each candidate block individually after JSON
    decoding — assistant text blocks AND ``tool_result`` blocks, since a worker
    may emit the sentinel via ``cat <<EOF`` (landing it in Bash stdout rather
    than assistant text; GitHub #731). Returns None on any I/O error or when no
    complete sentinel pair is found — distinct from a BlockedResult, which means
    the sentinel framing was present but the inner payload was unusable.

    Used by ``signal_stop`` for headless DAEMON sessions, whose result must
    be captured here because they bypass session lifecycle tracking entirely.
    See GitHub issue #225 (capture gap) and issue #176 Layer 1 (transcript-walk origin).

    ``warned_blocks`` (issue #1247) is an optional caller-owned set forwarded
    unchanged into every ``parse_stdout`` call below, deduping repeated
    ``_log.warning`` calls for the same malformed block both across repeated
    calls to this function (e.g. a poll loop rescanning an unresolved
    transcript) and across multiple candidate blocks within one call. Left
    ``None`` (the default), every warning logs independently as before.
    """
    if not claude_session_id:
        return None
    transcript_path = claude_project_dir(cwd) / f"{claude_session_id}.jsonl"
    last_result: AutoDevResult | BlockedResult | None = None
    for text in _iter_sentinel_text_blocks(transcript_path):
        block = extract_block(text)
        if block is not None:
            if _is_placeholder_sentinel_text(block):
                continue
            result = parse_stdout(text, warned_blocks=warned_blocks)
            if isinstance(result, AutoDevResult) and is_documented_example(result):
                continue
            last_result = result
    return last_result


def _sentinel_present_in_transcript(
    cwd: str,
    claude_session_id: str | None,
) -> bool:
    """Return True if the AUTO_DEV_RESULT sentinel block appears in the transcript.

    Thin wrapper around :func:`_parse_sentinel_from_transcript` preserved for
    callers that only need the boolean (Layer 1 budget gate in signal_stop).
    A non-None return — including a BlockedResult for malformed payloads —
    means the agent emitted *something*; the result-capture path uses the
    full parsed value, but the budget path only cares "did it emit?"
    """
    return _parse_sentinel_from_transcript(cwd, claude_session_id) is not None


# The exit-only members of the pipeline fixed-header set documented in
# ``.claude/commands/auto-dev.md``'s *Comment provenance rule*. A worker that
# posts one of these has finished the stage and is on its way out, so a Stop
# with no sentinel after such a post is an abandoned exit (GitHub #2135).
# ``## Multi-Marker Gate Blocked`` (retired) and the plan-of-record post are
# excluded on purpose: the former is historical, the latter is not an exit --
# a session keeps working after posting it.
_PARK_COMMENT_HEADERS: tuple[str, ...] = (
    "## Pending Verification Scan",
    "## Blocking Review Findings",
    "## Operator-Actionable Review Findings",
)

# ``--body-file <path>`` as workers actually write it, with or without
# surrounding quotes. Deliberately does NOT cover ``-F``/``-b`` short flags:
# see :func:`_park_comment_posted_in_transcript` for the documented
# false-negative post shapes.
_BODY_FILE_RE = re.compile(r"--body-file[=\s]+[\"']?(\S+?)[\"']?(?:\s|$)")

# The inline ``--body "<text>"`` form. Matched only to find where the body
# starts inside the command, so the header test below can still anchor at a
# line start. ``--body-file`` cannot match this pattern (it needs ``=`` or
# whitespace immediately after ``--body``), and it is tried first regardless.
_INLINE_BODY_RE = re.compile(r"--body[=\s]+[\"']?")

# A ``gh issue comment`` match is only evidence when it *starts* a shell
# command. These are the tokens allowed to sit between the segment start and
# the match: environment assignments and the small set of wrapper commands
# workers actually use (``timeout 60 gh …`` is the shape the checked-in real
# capture holds), plus their flags and a bare duration argument. Anything else
# in front -- ``echo``, ``cat``, ``printf`` -- means the match is inert text
# being written or displayed, not a post (GitHub #2135, operator round 4).
_WRAPPER_ATOM = (
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*"
    r"|timeout|env|nohup|command|stdbuf"
    r"|--?[A-Za-z0-9]\S*"
    r"|\d+(?:\.\d+)?[smhd]?)"
)
_WRAPPER_PREFIX_RE = re.compile(rf"^\s*(?:{_WRAPPER_ATOM}\s+)*$")

# ``<<WORD`` / ``<<-'WORD'`` -- the start of a heredoc whose body is data, not
# commands. Its lines are masked out of the command-start scan so a heredoc
# that merely *writes* an example post can never look like one.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Constructs whose expansion this transcript does not hold. A body value
# beginning with one of these is unknowable, and an unresolvable body is not
# evidence (GitHub #2135, operator round 4, finding 1).
_UNRESOLVED_BODY_PREFIXES = ("$(", "`", "<<")


def _skip_quoted(command: str, start: int) -> int:
    """Return the index just past the quoted run opening at *start* (#2135).

    An unterminated quote consumes the rest of the command, which is the
    fail-closed direction: nothing after it can then start a segment.
    """
    quote = command[start]
    index = start + 1
    while index < len(command):
        char = command[index]
        if char == "\\" and quote == '"':
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return len(command)


def _skip_heredoc_body(command: str, start: int, delimiter: str) -> int:
    """Return the index just past the heredoc body terminated by *delimiter*.

    An unterminated heredoc consumes the rest of the command (#2135) -- again
    fail-closed, since the remaining lines are then all treated as data.
    """
    index = start
    while index < len(command):
        end = command.find("\n", index)
        line = command[index:] if end < 0 else command[index:end]
        if line.strip() == delimiter:
            return len(command) if end < 0 else end + 1
        if end < 0:
            break
        index = end + 1
    return len(command)


def _command_start_offsets(command: str) -> tuple[int, ...]:
    """Offsets in *command* at which a new shell command can begin (#2135).

    A quote-aware, heredoc-aware split on ``;``, ``&&``, ``||``, ``|`` and
    newline. Quote-aware so a separator inside a ``--body "…"`` argument does
    not split the invocation's own body; heredoc-aware so the lines of a
    ``cat <<'EOF' > script.sh`` body are never offered as command starts.
    """
    offsets = [0]
    pending: list[str] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\":
            index += 2
        elif char in "'\"":
            index = _skip_quoted(command, index)
        elif char == "<" and (heredoc := _HEREDOC_RE.match(command, index)):
            pending.append(heredoc.group(2))
            index = heredoc.end()
        elif char == "\n":
            index += 1
            while pending:
                index = _skip_heredoc_body(command, index, pending.pop(0))
            offsets.append(index)
        elif char in ";&|":
            while index < len(command) and command[index] in ";&|":
                index += 1
            offsets.append(index)
        else:
            index += 1
    return tuple(offsets)


def _starts_a_command(command: str, offsets: tuple[int, ...], match_start: int) -> bool:
    """True iff the match at *match_start* begins a real invocation (#2135).

    Everything between its segment's start and the match must be wrapper
    tokens (see :data:`_WRAPPER_PREFIX_RE`); leading whitespace is ignored.
    """
    segment_start = max(offset for offset in offsets if offset <= match_start)
    return _WRAPPER_PREFIX_RE.match(command[segment_start:match_start]) is not None


def _body_is_unresolved(command: str, value_start: int) -> bool:
    """True iff the body value at *value_start* opens with a construct whose
    expansion this transcript does not hold (#2135)."""
    return command[value_start:].startswith(_UNRESOLVED_BODY_PREFIXES)


def _resolve_body(
    command: str, search_from: int, written: dict[str, str]
) -> str | None:
    """Resolve the body argument that follows the invocation at *search_from*.

    ``None`` means "not knowable from this transcript", which the caller
    treats as no evidence: a ``--body-file`` naming a path no successful
    ``Write`` in this transcript produced, or either flag whose value opens
    with an unresolved ``$(``/backtick/``<<``.
    """
    body_file = _BODY_FILE_RE.search(command, search_from)
    if body_file is not None:
        if _body_is_unresolved(command, body_file.start(1)):
            return None
        return written.get(body_file.group(1))
    inline = _INLINE_BODY_RE.search(command, search_from)
    if inline is None:
        return command[search_from:]
    if _body_is_unresolved(command, inline.end()):
        return None
    return command[inline.end() :]


class _ToolCall(NamedTuple):
    """A tool_use block that was resolved by a later tool_result (#2135)."""

    name: str
    tool_input: dict[str, object]
    is_error: bool


class _LegBoundary(NamedTuple):
    """A user re-entry record, by 0-based JSONL line index (#2135).

    The field is ``line_index`` rather than ``index`` because a ``NamedTuple``
    may not shadow ``tuple.index``.
    """

    line_index: int


class _SentinelText(NamedTuple):
    """One text block from the same set ``_iter_sentinel_text_blocks`` yields."""

    text: str


class _ParkPostScan(NamedTuple):
    """Outcome of :func:`_park_comment_posted_in_transcript` (#2135).

    ``posted`` -- a completed, non-error park-header comment post to this
    ticket exists in the transcript's *current run leg*.
    ``framing_after`` -- raw AUTO_DEV_RESULT framing text appears in a
    sentinel-bearing block after that post; the caller must defer on it.
    ``leg_start`` -- the JSONL line index of the last user re-entry record, or
    ``None`` when the transcript holds none (the whole file is then the leg).
    """

    posted: bool
    framing_after: bool
    leg_start: int | None


_LegEvent = _LegBoundary | _SentinelText | _ToolCall


def _is_leg_boundary(record: dict[str, object]) -> bool:
    """True iff *record* is a user re-entry record — the run-leg boundary.

    Claude Code writes no dedicated "resume" record: a read-only survey of
    2,456 local cw worker transcripts found every ``system`` subtype in use
    (``stop_hook_summary``, ``turn_duration``, ``away_summary``,
    ``scheduled_task_fire``, ``local_command``, ``bridge_status``,
    ``informational``, ``compact_boundary``) and none of them marks one. A cw
    re-entry (``resume_session`` passes ``--resume <claude_session_id>``)
    lands as an ordinary ``user`` record carrying the ``Continue
    auto-dev-<stage> …`` prompt, exactly like a ``<task-notification>`` or an
    ``Another Claude session sent a message: …`` injection.

    The discriminator is therefore "a ``user`` record that is not a pure
    ``tool_result`` carrier": content a ``str``, or a list holding a ``text``
    block and no ``tool_result`` block. Those 2,456 transcripts' 36,332
    ``user`` records partition cleanly into 3,820 bare ``str``, 1,041
    text-block-only, 31,471 tool_result-only and **0 mixed**.

    This deliberately does not try to tell a resume prompt from other injected
    user text. Over-matching is fail-safe: it only shrinks the evidence
    window, turning a would-be park into today's defer (GitHub #2135).
    """
    if record.get("type") != "user":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return False
        if block.get("type") == "text":
            has_text = True
    return has_text


def _iter_message_records(
    transcript_path: Path,
) -> Iterator[tuple[int, dict[str, object], dict[str, object]]]:
    """Yield ``(line_index, record, message)`` for each message-bearing record.

    Mirrors ``_iter_sentinel_text_blocks``'s tolerance (``cw/_util.py``): a
    missing file, an ``OSError`` mid-read, a malformed line, a non-dict record
    or a bookkeeping record with no ``message`` dict is skipped rather than
    raised — real transcripts interleave ``attachment``,
    ``file-history-delta``, ``last-prompt``, ``ai-title``, ``agent-name``,
    ``mode``, ``permission-mode`` and ``atis-latch`` records freely. The line
    index counts every line, skipped ones included, so it is a stable
    reference into the file (GitHub #2135).
    """
    if not transcript_path.is_file():
        return
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                yield index, record, message
    except OSError:
        return


def _block_events(
    block: dict[str, object],
    *,
    is_assistant: bool,
    pending: dict[str, tuple[str, dict[str, object]]],
) -> Iterator[_SentinelText | _ToolCall]:
    """Yield the leg events a single content block contributes (#2135).

    A ``tool_use`` block is remembered in *pending* and yields nothing — a
    call with no result never completed, so it is never evidence. A
    ``tool_result`` yields its own text **first** and only then the resolved
    :class:`_ToolCall`, so a post's own result text is never counted as
    appearing "after" that post. ``bool(block.get("is_error"))`` treats a
    missing key as non-error, matching the real ``Write`` result shape (real
    captures split three ways: key omitted, ``false``, ``true``).
    """
    block_type = block.get("type")
    if is_assistant and block_type == "text":
        text = block.get("text")
        if isinstance(text, str):
            yield _SentinelText(text)
    elif block_type == "tool_use":
        tool_id = block.get("id")
        name = block.get("name")
        tool_input = block.get("input")
        if (
            isinstance(tool_id, str)
            and isinstance(name, str)
            and isinstance(tool_input, dict)
        ):
            pending[tool_id] = (name, tool_input)
    elif block_type == "tool_result":
        for text in _iter_tool_result_text(block):
            yield _SentinelText(text)
        tool_use_id = block.get("tool_use_id")
        if isinstance(tool_use_id, str):
            call = pending.pop(tool_use_id, None)
            if call is not None:
                yield _ToolCall(call[0], call[1], bool(block.get("is_error")))


def _iter_leg_events(transcript_path: Path) -> Iterator[_LegEvent]:
    """Walk *transcript_path* once, yielding the three leg-event kinds (#2135).

    Run-leg boundaries, sentinel-bearing text (the exact block set
    ``_iter_sentinel_text_blocks`` defines: assistant ``text`` blocks and
    ``tool_result`` content), and completed tool calls, in file order. The
    pending-call map is cleared at every boundary so a body written before a
    re-entry can never supply a post made after it.
    """
    pending: dict[str, tuple[str, dict[str, object]]] = {}
    for index, record, message in _iter_message_records(transcript_path):
        if _is_leg_boundary(record):
            pending.clear()
            yield _LegBoundary(index)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        is_assistant = record.get("type") == "assistant"
        for block in content:
            if isinstance(block, dict):
                yield from _block_events(
                    block, is_assistant=is_assistant, pending=pending
                )


def _post_body(command: str, written: dict[str, str], ticket_id: str) -> str | None:
    """Resolve the comment body a real ``gh issue comment`` invocation posted.

    The match must **start a shell command** inside *command*, not merely
    appear somewhere in it (GitHub #2135, operator round 4): a Bash call that
    writes or echoes an example post exits 0 having posted nothing, and its
    embedded text carries the same header and provenance marker a real post
    does. ``_starts_a_command`` is the only thing that separates the two.

    Returns ``None`` when *command* contains no such invocation for
    *ticket_id*, or when the body it names is not knowable from this
    transcript (see :func:`_resolve_body`). Otherwise returns the ``Write``-ed
    body, or the inline ``--body "<text>"`` argument.
    """
    pattern = re.compile(rf"\bgh\s+issue\s+comment\s+#?{re.escape(ticket_id)}(?!\w)")
    offsets = _command_start_offsets(command)
    for match in pattern.finditer(command):
        if not _starts_a_command(command, offsets, match.start()):
            continue
        body = _resolve_body(command, match.end(), written)
        if body is not None:
            return body
    return None


def _is_park_body(body: str) -> bool:
    """True iff *body* is an agent-authored post under a park/blocker header."""
    if not is_agent_authored(body):
        return False
    return any(line.startswith(_PARK_COMMENT_HEADERS) for line in body.splitlines())


def _park_comment_posted_in_transcript(
    transcript_path: Path, ticket_id: str
) -> _ParkPostScan:
    """Scan a transcript for evidence of an abandoned exit (GitHub #2135).

    The evidence is a completed, non-error ``gh issue comment <ticket_id>``
    whose body carries both the agent provenance marker and one of
    :data:`_PARK_COMMENT_HEADERS`, made in the transcript's **current run
    leg** (after the last user re-entry record; with no such record the whole
    file is the leg). Combined by the caller with "the sentinel parse returned
    ``None``", that is a stage which announced its exit and then stopped
    without emitting a sentinel.

    ``framing_after`` additionally reports any raw ``AUTO_DEV_RESULT`` framing
    text in a sentinel-bearing block after that post, deliberately with **no**
    placeholder or documented-example carve-out: a frame the parser skipped or
    discarded is exactly the case that must defer rather than be stamped with
    a disposition that would hide the worker's real ``blocker.reason``.

    Limits, all of which yield ``posted=False`` (today's defer, never a false
    park):

    * GitHub ``gh issue comment`` posts only. Linear-tracked tickets get no
      behavior change; tracker-agnostic evidence is a follow-up.
    * The evidence is this Claude session's transcript and only its current
      run leg.
    * The ``gh issue comment`` text must **start a shell command** —
      ``timeout 60 gh …`` and the second link of an ``&&`` chain qualify;
      inert text does not. A heredoc that writes an example post, an ``echo``
      of one, or a commented-out line all carry the same header and marker as
      a real post while posting nothing, so counting them would falsely park a
      live session's row (GitHub #2135, operator round 4, finding 1).
    * The join is ``Write`` + a literal ``--body-file <path>``, or the inline
      ``--body`` form, and the body value must be resolvable: one opening with
      ``$(``, a backtick or ``<<`` is unknowable, and an unresolvable body is
      not evidence. **Documented false-negative shapes:** a ``--body-file``
      path holding an unexpanded ``$VAR``/``${VAR}``, a body assembled by a
      shell or Python heredoc rather than a ``Write`` tool_use, the ``-F`` /
      ``-b`` short flags, ``--repo`` placed before the issue number, and
      ``--body-file -``. A producer-side park marker is the follow-up that
      closes them.
    """
    written: dict[str, str] = {}
    posted = False
    framing_after = False
    leg_start: int | None = None
    for event in _iter_leg_events(transcript_path):
        if isinstance(event, _LegBoundary):
            written.clear()
            posted = False
            framing_after = False
            leg_start = event.line_index
        elif isinstance(event, _SentinelText):
            if posted and (
                _OPEN_SENTINEL in event.text or _CLOSE_SENTINEL in event.text
            ):
                framing_after = True
        elif event.is_error:
            continue
        elif event.name == "Write":
            file_path = event.tool_input.get("file_path")
            content = event.tool_input.get("content")
            if isinstance(file_path, str) and isinstance(content, str):
                written[file_path] = content
        elif event.name == "Bash":
            command = event.tool_input.get("command")
            if isinstance(command, str):
                body = _post_body(command, written, ticket_id)
                if body is not None and _is_park_body(body):
                    posted = True
    return _ParkPostScan(
        posted=posted, framing_after=framing_after, leg_start=leg_start
    )
