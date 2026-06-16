"""Transcript sentinel helpers shared across CLI commands.

Both ``signal-stop`` (headless DAEMON backstop) and ``dev-queue wait``
(sentinel-aware polling) need to detect and parse the AUTO_DEV_RESULT
sentinel inside a Claude session transcript. The logic lives here so both
command submodules import the same implementation.
"""

from __future__ import annotations

from cw._util import _iter_assistant_text_blocks, claude_project_dir
from cw.auto_dev_result import (
    AutoDevResult,
    BlockedResult,
    extract_block,
    parse_stdout,
)


def _parse_sentinel_from_transcript(
    cwd: str,
    claude_session_id: str | None,
) -> AutoDevResult | BlockedResult | None:
    """Return the parsed sentinel from the transcript, or None if absent.

    Claude stores session transcripts at:
      ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``

    where the encoded path replaces both ``/`` and ``.`` with ``-``. The JSONL
    contains one event per line; ``assistant`` events carry ``message.content``
    blocks whose ``text`` fields hold the model output, JSON-escaped (real
    newlines become the two-character sequence ``\\n``). Running ``extract_block``
    against the raw file therefore misses sentinels that are valid in their
    decoded form, so this scans each assistant text block individually after
    JSON decoding. Returns None on any I/O error or when no complete sentinel
    pair is found — distinct from a BlockedResult, which means the sentinel
    framing was present but the inner payload was unusable (§6 failure modes).

    Used by ``signal_stop`` for headless DAEMON sessions, whose result must
    be captured here because they bypass session lifecycle tracking entirely.
    See GitHub issue #225 (capture gap) and issue #176 Layer 1 (transcript-walk origin).
    """
    if not claude_session_id:
        return None
    transcript_path = claude_project_dir(cwd) / f"{claude_session_id}.jsonl"
    for text in _iter_assistant_text_blocks(transcript_path):
        if extract_block(text) is not None:
            return parse_stdout(text)
    return None


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
