"""JSONL audit-event parsing for ``codex exec --json`` (#1710).

``codex exec --json`` prints one JSON object per line to stdout for the
lifetime of a run: ``thread.started``, ``turn.started``, ``item.started`` /
``item.completed`` (one per tool call or agent message), and a terminal
``turn.completed`` (carrying a ``usage`` block) or ``turn.failed``. This module
turns that stream into a flat :class:`~cw.review_findings.ReviewerRunMetrics`
bag that ``_roles`` attaches to the per-role
:class:`~cw.review_findings.ReviewerRunRecord`.

Two properties are load-bearing:

- **It never raises and never logs.** Codex is a system we do not own, so a
  malformed, truncated, or entirely non-JSONL stdout degrades to all-``None`` /
  empty defaults rather than taking down a review that otherwise succeeded
  (R2). That same tolerance is what lets ``_run_codex_role`` feed the
  flag-rejection retry's non-JSONL stdout straight in with no special-casing.
  The malformed-stream *warning* lives in ``_roles`` — only that layer has the
  ``role`` / ``session_id`` needed to attribute it.
- **Unexpected item types are allowlisted, not denylisted.** Anything outside
  :data:`_EXPECTED_REVIEWER_ITEM_TYPES` is recorded in
  ``unexpected_tool_attempts`` — purely observational, never read by health,
  blocking, or gate logic (R4) — so a reviewer reaching for an MCP or web tool
  surfaces without this module having to guess codex's exact type strings.

The parsing idiom (per-line ``json.loads`` with ``JSONDecodeError: continue``,
then ``.get()`` field extraction) mirrors
:func:`cw.queue_peek.parse_transcript`, the tree's established defensive
line-oriented parser.
"""

from __future__ import annotations

import json

from cw.review_findings import ReviewerRunMetrics

# Item types a read-only reviewer role legitimately produces. Grounded in live
# captures from codex-cli 0.147.0 (``agent_message``, ``command_execution``);
# ``reasoning`` and ``error`` are included because they are documented sibling
# item types of the same stream. Anything else is recorded as an unexpected
# tool attempt rather than silently accepted.
_EXPECTED_REVIEWER_ITEM_TYPES = frozenset(
    {"agent_message", "command_execution", "reasoning", "error"}
)

# Wire event names.
_THREAD_STARTED = "thread.started"
_TURN_COMPLETED = "turn.completed"
_TURN_FAILED = "turn.failed"
_ITEM_STARTED = "item.started"
_ITEM_COMPLETED = "item.completed"

# The item type whose presence proves the role actually executed a command.
_COMMAND_ITEM_TYPE = "command_execution"


def _as_int(value: object) -> int | None:
    """Return *value* as an int, or ``None`` when it is not a plain int.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, so a
    ``true`` in a usage field would otherwise silently become ``1``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _empty_metrics(duration_seconds: float) -> ReviewerRunMetrics:
    """Return the all-default metrics bag for a run with no parseable events."""
    return ReviewerRunMetrics(
        thread_id=None,
        # Not observably present anywhere in codex-cli 0.147.0's event stream
        # (adopted assumption 1, #1710) — the field exists so a later codex
        # version can populate it without a schema change.
        effective_model=None,
        duration_seconds=duration_seconds,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        terminal_event=None,
        tool_call_counts={},
        had_command_evidence=False,
        unexpected_tool_attempts=[],
    )


def _iter_events(stdout: str) -> list[dict[str, object]]:
    """Return every line of *stdout* that parses into a JSON object."""
    events: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _apply_item_event(
    event: dict[str, object], metrics: ReviewerRunMetrics, *, completed: bool
) -> None:
    """Fold one ``item.started``/``item.completed`` event into *metrics*.

    Counting is **completed-only** — a single tool call surfaces as both an
    ``item.started`` and an ``item.completed`` with the same item id, so
    counting both would double every tool call. The unexpected-type check
    scans both, because a role that *started* a disallowed tool is worth
    recording even if the call never completed.
    """
    item = event.get("item")
    if not isinstance(item, dict):
        return
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return
    if item_type not in _EXPECTED_REVIEWER_ITEM_TYPES:
        attempts = metrics["unexpected_tool_attempts"]
        if item_type not in attempts:
            attempts.append(item_type)
        return
    if not completed:
        return
    counts = metrics["tool_call_counts"]
    counts[item_type] = counts.get(item_type, 0) + 1
    if item_type == _COMMAND_ITEM_TYPE:
        metrics["had_command_evidence"] = True


def _apply_usage(event: dict[str, object], metrics: ReviewerRunMetrics) -> None:
    """Fold a ``turn.completed`` event's ``usage`` block into *metrics*."""
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return
    metrics["input_tokens"] = _as_int(usage.get("input_tokens"))
    metrics["cached_input_tokens"] = _as_int(usage.get("cached_input_tokens"))
    metrics["output_tokens"] = _as_int(usage.get("output_tokens"))
    # Wire key is ``reasoning_output_tokens``; the record field is
    # ``reasoning_tokens`` (the AC's own vocabulary).
    metrics["reasoning_tokens"] = _as_int(usage.get("reasoning_output_tokens"))


def _parse_codex_audit_events(
    stdout: str, *, duration_seconds: float
) -> ReviewerRunMetrics:
    """Parse a ``codex exec --json`` JSONL stream into per-role run metrics.

    *duration_seconds* is passed straight through — the caller has already
    measured wall time from its single ``time.monotonic()`` pair, and this
    function never reads the clock itself.

    ``terminal_event`` records the ``type`` of the **last** recognized event:
    ``"turn.completed"`` / ``"turn.failed"`` on a healthy run, and something
    else (or ``None``) when the stream was cut off mid-run or was never JSONL
    at all. The caller inspects it; nothing here raises or logs on a
    malformed stream.
    """
    metrics = _empty_metrics(duration_seconds)
    for event in _iter_events(stdout):
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        metrics["terminal_event"] = event_type
        if event_type == _THREAD_STARTED:
            thread_id = event.get("thread_id")
            metrics["thread_id"] = thread_id if isinstance(thread_id, str) else None
        elif event_type in (_ITEM_STARTED, _ITEM_COMPLETED):
            _apply_item_event(event, metrics, completed=event_type == _ITEM_COMPLETED)
        elif event_type == _TURN_COMPLETED:
            _apply_usage(event, metrics)
    return metrics
