"""Transcript-tail detectors: usage limit, provider overload, dangling tool use.

Reads a session transcript's tail for the evidence the liveness, idle and
phantom sweeps act on -- a usage-limit or provider-overload message, an
unresolved non-subagent ``tool_use``, an unconsumed queue-operation
notification -- and the post-review-clean event check. Imports
``_constants`` and ``_transcripts``. Split out of the flat
``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from cw.events import read_events
from cw.exceptions import USAGE_LIMIT_RE
from cw.executor_diagnostics import redact
from cw.models import OrchestratorEventType
from cw.reconcile._shared._constants import (
    _QUEUE_OPERATION_ENQUEUE,
    _QUEUE_OPERATION_RECORD_TYPE,
    _STAGE_REVIEW_COMPLETE,
)
from cw.reconcile._shared._transcripts import (
    _iter_notification_records,
    _iter_transcript_records,
    _locate_session_transcript,
)
from cw.unavailability import FAMILY_PROVIDER_OVERLOAD, classify_provider_unavailability

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session


# Cap (characters) on the command snippet _bash_command_snippet carries into
# a dangling_tool_use distress breadcrumb (#1482). Small and fixed -- the
# breadcrumb is an operator hint, not a full replay of the command.
_TOOL_USE_COMMAND_SNIPPET_MAX_CHARS = 80

# Tool names _detect_dangling_tool_use must never report on (#1969): that
# domain belongs exclusively to _unresolved_subagent_spawn_age_seconds /
# agent_spawn_stamp, because PostToolUse:Agent fires at launch-return, not
# subagent completion -- a transcript-pairing check would false-fire distress
# against a still-outstanding subagent spawn. Mirrors spawn.py's
# _AGENT_TOOL_MATCHER (`^(Agent|Task)$`); duplicated rather than imported
# because cw.spawn already imports from cw.reconcile (circular), and this is
# a 2-name set well under PYTHON-PATTERNS.md's <5-line DRY threshold.
_SUBAGENT_SPAWNING_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "Task"})


class UsageLimitDetection(NamedTuple):
    """Outcome of scanning a session transcript for a usage-limit message (#1345).

    ``detected`` is True iff any post-start assistant record's text matched
    :data:`USAGE_LIMIT_RE`. ``matched_at`` is the LAST matching record's own
    ``timestamp`` (last-match-wins); ``None`` when nothing matched or that
    record has no usable timestamp -- never an earlier match's timestamp, which
    would make a stale anchor look current (#2324). ``transcript_tail_at`` is
    the timestamp of the transcript's last content-bearing record — matched or
    not — from the same forward scan; ``None`` when no record has a parseable
    timestamp. The recency gate (:func:`_usage_limit_is_recent`)
    compares the two so a stale limit message is not mistaken for a live cutoff.
    ``matched_text`` is the LAST matching record's text, timestamped or not, so
    a caller can parse its reset time without a second scan (#2324).
    ``has_unparseable_content_after_match`` is true when a later
    content-bearing record has no usable timestamp. In that case the apparent
    zero-gap tail is unknown rather than empty.
    ``transcript_scan_complete`` is false when the transcript could not be
    read to EOF; callers that act on this evidence must fail closed.
    """

    detected: bool
    matched_at: datetime | None
    transcript_tail_at: datetime | None
    matched_text: str | None = None
    has_unparseable_content_after_match: bool = False
    transcript_scan_complete: bool = True


def _detect_usage_limit(session: Session) -> UsageLimitDetection:
    """Scan the newest post-start transcript for a usage-limit message (#1345).

    Returns a :class:`UsageLimitDetection`: ``detected`` True iff any assistant
    record's text matched :data:`USAGE_LIMIT_RE`, ``matched_at`` the LAST
    matching record's own timestamp (``None`` if it has none),
    ``transcript_tail_at`` the transcript's last content-bearing
    timestamp, ``matched_text`` the LAST matching record's text. Uses
    :func:`_locate_session_transcript` for precise per-session lookup
    (surface_ref-prefix glob, #541). Never raises; returns an all-empty
    detection when the project dir is absent, no matching .jsonl exists, or the
    transcript predates the session start.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return UsageLimitDetection(
            detected=False, matched_at=None, transcript_tail_at=None
        )
    matched_text: str | None = None
    matched_at: datetime | None = None
    has_unparseable_content_after_match = False
    transcript_tail_at: datetime | None = None
    scan = _iter_transcript_records(transcript)
    matched = False
    for record in scan:
        if record.content_bearing:
            if record.timestamp is not None:
                transcript_tail_at = record.timestamp
            elif matched:
                has_unparseable_content_after_match = True
        if (
            record.record_type == "assistant"
            and record.content_bearing
            and record.text is not None
            and USAGE_LIMIT_RE.search(record.text)
        ):
            # last-match-wins for both, from the SAME record: an untimestamped
            # latest match leaves matched_at None (gap unknown) rather than
            # keeping an older match's timestamp, which would fake a zero gap.
            matched = True
            matched_text = record.text
            matched_at = record.timestamp
            has_unparseable_content_after_match = False
    return UsageLimitDetection(
        detected=matched,
        matched_at=matched_at,
        transcript_tail_at=transcript_tail_at,
        matched_text=matched_text,
        has_unparseable_content_after_match=has_unparseable_content_after_match,
        transcript_scan_complete=scan.scan_complete,
    )


class DanglingToolUseEvidence(NamedTuple):
    """An unresolved, non-subagent tool_use found at a transcript's tail (#1482).

    ``tool_name`` is the tool_use block's ``name`` (e.g. ``"Bash"``).
    ``command_snippet`` is a redacted, length-capped rendering of
    ``input.command`` when present, or ``None`` for a tool_use with no such
    field (e.g. ``Write``). Produced only by :func:`_detect_dangling_tool_use`,
    which never reports on :data:`_SUBAGENT_SPAWNING_TOOL_NAMES`.
    """

    tool_name: str
    command_snippet: str | None


def _bash_command_snippet(block: dict[str, object]) -> str | None:
    """Extract, redact, and truncate a tool_use block's ``input.command``.

    Returns ``None`` when the block carries no string ``input.command`` (e.g.
    a non-Bash tool like ``Write``). Redacts secret-shaped substrings via
    :func:`cw.executor_diagnostics.redact` before truncating -- the raw
    command text flows into the operator-local ``session.needs_attention``
    event/SSE surface (GitHub #1482).
    """
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    return _redact_and_truncate(command)


def _redact_and_truncate(text: str) -> str:
    """Redact secret-shaped substrings, then cap at the breadcrumb snippet length.

    Redaction runs first so truncation can never split a secret into an
    unrecognisable (and therefore unredacted) prefix.
    """
    redacted = redact(text)
    if len(redacted) <= _TOOL_USE_COMMAND_SNIPPET_MAX_CHARS:
        return redacted
    return redacted[:_TOOL_USE_COMMAND_SNIPPET_MAX_CHARS] + "…"


def _apply_tool_use_block(
    pending: dict[str, tuple[str, dict[str, object]]], block: dict[str, object]
) -> None:
    """Track or resolve one ``tool_use``/``tool_result`` content block.

    Mutates *pending* in place: a non-subagent ``tool_use`` with a string id
    is added (or overwritten, refreshing its file-order position); a
    ``tool_result`` pops its matching ``tool_use_id``. Any other block shape
    is ignored. Extracted from :func:`_detect_dangling_tool_use` to keep that
    function's branch count under the PLR0912 ceiling.
    """
    block_type = block.get("type")
    if block_type == "tool_use":
        tool_id = block.get("id")
        name = block.get("name")
        if (
            isinstance(tool_id, str)
            and isinstance(name, str)
            and name not in _SUBAGENT_SPAWNING_TOOL_NAMES
        ):
            pending[tool_id] = (name, block)
    elif block_type == "tool_result":
        tool_use_id = block.get("tool_use_id")
        if isinstance(tool_use_id, str):
            pending.pop(tool_use_id, None)


def _detect_dangling_tool_use(session: Session) -> DanglingToolUseEvidence | None:
    """Scan the session's transcript for an unresolved, non-subagent tool_use.

    Uses :func:`_locate_session_transcript` for precise per-session lookup.
    Tracks ``tool_use`` blocks by id in file order, popping an id when a
    matching ``tool_result`` block is later seen; returns evidence for the
    last entry still pending, or ``None`` when everything resolved (or the
    transcript is missing/unreadable).

    ``Agent``/``Task`` tool_use is deliberately excluded (see
    :data:`_SUBAGENT_SPAWNING_TOOL_NAMES`, GitHub #1969) -- that domain
    belongs exclusively to :func:`_unresolved_subagent_spawn_age_seconds` /
    ``agent_spawn_stamp``. Never raises; fails open to ``None`` on any read
    error, mirroring :func:`_detect_usage_limit` / :func:`_detect_provider_overload`.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return None
    pending: dict[str, tuple[str, dict[str, object]]] = {}
    try:
        with transcript.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict):
                        _apply_tool_use_block(pending, block)
    except OSError:
        return None
    if not pending:
        return None
    last_name, last_block = next(reversed(pending.values()))
    return DanglingToolUseEvidence(
        tool_name=last_name,
        command_snippet=_bash_command_snippet(last_block),
    )


def _detect_unconsumed_queue_notification(session: Session) -> str | None:
    """Return the text of an unconsumed queue-operation enqueue at the tail.

    Scans the session's transcript (via :func:`_locate_session_transcript`)
    forward, keeping only the last well-formed dict record. Returns that
    record's redacted, length-capped ``content`` iff it is a ``{"type":
    "queue-operation", "operation": "enqueue"}`` record with string
    ``content`` -- a harness notification (e.g. a backgrounded Bash command's
    completion) that no later turn ever consumed (GitHub #2251). Record shape
    per :func:`_iter_notification_records`. Never raises; fails open to
    ``None`` on a missing or unreadable transcript, mirroring
    :func:`_detect_dangling_tool_use`.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return None
    last_record: dict[str, object] | None = None
    try:
        with transcript.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    last_record = record
    except OSError:
        return None
    if (
        last_record is None
        or last_record.get("type") != _QUEUE_OPERATION_RECORD_TYPE
        or last_record.get("operation") != _QUEUE_OPERATION_ENQUEUE
    ):
        return None
    content = last_record.get("content")
    if not isinstance(content, str):
        return None
    return _redact_and_truncate(content)


def _detect_provider_overload(session: Session) -> bool:
    """Scan the session's transcript for the provider-overload (API 529)

    signature (#1923). Uses :func:`_locate_session_transcript` for precise
    per-session lookup. Returns True iff any yielded notification text
    classifies as :data:`FAMILY_PROVIDER_OVERLOAD`; False when the
    transcript is missing or no text matches. Never raises. Simplified to a
    plain bool, unlike :class:`UsageLimitDetection` -- this field has no
    recency-gate/backoff-window consumer (see A1/R1: payload-only, no
    routing weight), so there is nothing downstream that needs a timestamp.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return False
    for text in _iter_notification_records(transcript):
        if classify_provider_unavailability(text) == FAMILY_PROVIDER_OVERLOAD:
            return True
    return False


def _usage_limit_is_recent(
    detection: UsageLimitDetection,
    *,
    window_seconds: float,
    fail_open: bool = True,
) -> bool:
    """Decide whether a detected usage-limit message is *current* (#1345).

    Contract (operator resolution, issue #1345):
    - not detected → ``False``;
    - incomplete transcript scan → ``False`` (partial evidence is not
      sufficient for a positive usage-limit disposition);
    - detected but either ``matched_at`` or ``transcript_tail_at`` is ``None``
      (no usable anchor) → return ``fail_open`` verbatim;
    - else → recent iff the message landed within ``window_seconds`` of the
      transcript's own tail:
      ``(transcript_tail_at - matched_at).total_seconds() <= window_seconds``.
    """
    if not detection.detected:
        return False
    if not detection.transcript_scan_complete:
        return False
    if detection.matched_at is None or detection.transcript_tail_at is None:
        return fail_open
    gap = (detection.transcript_tail_at - detection.matched_at).total_seconds()
    return gap <= window_seconds


def _detect_post_review_clean(session: Session) -> bool:
    """Return True iff the event bus has a post-review-clean marker for this session.

    Reads STAGE_ENTERED events from the inbox and checks for an event with
    payload["stage"] == _STAGE_REVIEW_COMPLETE correlated to session.id,
    with a time-window guard (event after session.started_at).

    Returns False on any error — conservative default.
    """
    if session.worktree_path is None:
        return False
    try:
        events = read_events(
            event_types=[OrchestratorEventType.STAGE_ENTERED],
            since_ts=session.started_at,
        )
    except Exception:  # noqa: BLE001 — events._parse_lines deliberately re-raises on interior corruption "so callers see real corruption"; this liveness check is the one caller that must not propagate that, so it fails safe to False instead
        return False
    for ev in events:
        session_id = ev.payload.get("session_id")
        stage = ev.payload.get("stage")
        if session_id == session.id and stage == _STAGE_REVIEW_COMPLETE:
            return True
    return False
