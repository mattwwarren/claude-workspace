"""Render a :class:`~cw.models.review_history.ReviewHistory` as text (#2790).

The ``cw review history`` text form, in the ``cw dev-queue show`` convention
(``cw.ticket_view.render_ticket_view``): one ``key: value`` per line in a fixed
key order, every key always printed (``-`` when absent), nothing truncated, and
no tables. Finding text is folded onto one line. Then one indented block per
round, then the ``settled_other``, ``dispositions``, ``voided``, ``releases``
and ``notes`` blocks; ``notes`` is always printed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_history import (
        DispositionView,
        ReleaseView,
        ReviewHistory,
        RoundView,
        SettledView,
        VoidedView,
    )

_ABSENT = "-"
_NOT_RECORDED = "not recorded"
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _kv(key: str, value: str, indent: str = "") -> str:
    return f"{indent}{key}: {value}"


def _or_absent(value: str | None) -> str:
    return _one_line(value) if value else _ABSENT


def _count(value: int | None) -> str:
    return _NOT_RECORDED if value is None else str(value)


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return _NOT_RECORDED
    total = round(seconds)
    sign = "-" if total < 0 else ""
    hours, rest = divmod(abs(total), _SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, _SECONDS_PER_MINUTE)
    if hours:
        return f"{sign}{hours}h{minutes:02d}m{secs:02d}s"
    return f"{sign}{minutes}m{secs:02d}s"


def _block(name: str, lines: Sequence[str]) -> list[str]:
    """``name:`` then its ``  - `` lines, or ``name: -`` when there are none."""
    if not lines:
        return [_kv(name, _ABSENT)]
    return [f"{name}:", *(f"  - {line}" for line in lines)]


def _finding(severity: str, file: str, summary: str) -> str:
    return f"{severity} {_one_line(file)}: {_one_line(summary)}"


def _state(history: ReviewHistory) -> str:
    ledger = history.summary.ledger_status or _ABSENT
    tail = "in flight, showing the rounds so far" if history.in_flight else "finished"
    return f"row {history.row_status} at {history.row_stage}; ledger {ledger}; {tail}"


def _stop(history: ReviewHistory) -> str:
    reason = history.summary.stop_reason
    if not reason:
        return _ABSENT
    identities = history.summary.stop_identities
    if not identities:
        return reason
    return f"{reason} ({'; '.join(_one_line(i) for i in identities)})"


def _fix_cycles(history: ReviewHistory) -> str:
    used, cap = history.summary.fix_cycles_used, history.summary.max_cycles
    if used is None:
        return _ABSENT
    return str(used) if cap is None else f"{used} of {cap}"


def _deferred(history: ReviewHistory) -> str:
    """The deferred count; a sentinel-only count has no finding text behind it."""
    summary = history.summary
    count = summary.deferred_count
    if count is None or count == len(summary.deferred):
        return _count(count)
    return f"{count} (text not recorded)"


def _header(history: ReviewHistory) -> list[str]:
    summary = history.summary
    series = " ".join(str(n) for n in summary.new_accepted_series)
    return [
        f"review_history: {history.ticket_id} (client {history.client})",
        _kv("state", _state(history)),
        _kv("mode", summary.convergence_mode),
        _kv("rounds", _count(summary.rounds_run)),
        _kv("converged", _yes_no(summary.converged)),
        _kv("final_clean_sha", _or_absent(summary.final_clean_sha)),
        _kv("stop", _stop(history)),
        _kv("new_accepted_by_round", series or _ABSENT),
        _kv("fix_cycles", _fix_cycles(history)),
        _kv("confirm_backend", _or_absent(summary.confirm_backend)),
        _kv("deferred", _deferred(history)),
        _kv("confirm_failures", _count(summary.confirm_failures)),
        _kv("sources", ", ".join(history.sources) or _ABSENT),
    ]


def _new_findings(view: RoundView) -> str:
    if view.new_accepted is None:
        return _NOT_RECORDED
    split = view.new_by_severity
    if split is None:
        return f"{view.new_accepted} (severity split not recorded)"
    return (
        f"{view.new_accepted} (MUST_FIX {split.must_fix}, "
        f"SHOULD_FIX {split.should_fix})"
    )


def _settled_counts(view: RoundView) -> str:
    if view.new_settled is not None and view.resurfaced_settled is not None:
        return f"{view.new_settled} new, {view.resurfaced_settled} resurfaced"
    if view.settled_total is not None:
        return f"{view.settled_total} (new/resurfaced split not recorded)"
    return _NOT_RECORDED


def _settled_line(entry: SettledView) -> str:
    return (
        f"{entry.outcome} {_finding(entry.severity, entry.file, entry.summary)}"
        f" -- {_one_line(entry.rationale)}"
    )


def _round_block(view: RoundView) -> list[str]:
    pad = "  "
    lines = [
        f"round {view.round} ({view.kind}):",
        _kv("source", view.source, pad),
        _kv("backend", _or_absent(view.backend), pad),
        _kv("sha", _or_absent(view.reviewed_sha), pad),
        _kv("reviewers", ", ".join(view.roster) or _ABSENT, pad),
        _kv("new_findings", _new_findings(view), pad),
        _kv("fix_dispatched", _count(view.fix_dispatched), pad),
        _kv("settled", _settled_counts(view), pad),
    ]
    lines.extend(f"    - {_settled_line(entry)}" for entry in view.settled)
    lines.append(_kv("wall_time", _duration(view.wall_seconds), pad))
    lines.append(_kv("recorded_at", _or_absent(view.recorded_at), pad))
    return lines


def _settled_other(history: ReviewHistory) -> list[str]:
    """Every top-level settled entry no round block printed, with its provenance."""
    shown = {
        (e.severity, e.file, e.summary, e.round)
        for view in history.rounds
        for e in view.settled
    }
    lines: list[str] = []
    for entry in history.settled:
        key = (entry.severity, entry.file, entry.summary, entry.round)
        if entry.origin == "adjudication" and key in shown:
            continue
        provenance = (
            f"{entry.origin}, actor {_or_absent(entry.actor)}, settled_at "
            f"{_or_absent(entry.settled_at)}, round {entry.round}"
        )
        lines.append(f"{_settled_line(entry)} ({provenance})")
    return lines


def _disposition_line(entry: DispositionView) -> str:
    return (
        f"{entry.outcome} {_one_line(entry.file)}: {_or_absent(entry.summary)}"
        f" -- {_or_absent(entry.rationale)} (actor {_or_absent(entry.actor)}, "
        f"recorded_at {_or_absent(entry.recorded_at)}, sha "
        f"{_or_absent(entry.reviewed_sha)})"
    )


def _voided_line(entry: VoidedView) -> str:
    return (
        f"{_finding(entry.severity, entry.file, entry.summary)} -- original "
        f"rejection: {_or_absent(entry.original_rationale)} (comment "
        f"{_one_line(entry.operator_comment_id)}, voided_at "
        f"{_or_absent(entry.voided_at)}, event_at {entry.event_at})"
    )


def _release_line(entry: ReleaseView) -> str:
    return (
        f"{_one_line(entry.reason)} released by {entry.cause} at round "
        f"{entry.round}, sha {_or_absent(entry.sha)} (at {_or_absent(entry.at)})"
    )


def _notes(history: ReviewHistory) -> list[str]:
    return _block("notes", [_one_line(note) for note in history.notes])


def render_review_history(history: ReviewHistory) -> str:
    """The ``cw review history`` text for *history*."""
    if not history.sources:
        first = (
            f"No review data recorded for ticket '{history.ticket_id}' (client "
            f"{history.client}): no converge ledger, no session review result "
            "and no row dispositions were found."
        )
        return "\n".join([first, *_notes(history)])
    lines = _header(history)
    for view in history.rounds:
        lines.extend(_round_block(view))
    lines.extend(_block("settled_other", _settled_other(history)))
    lines.extend(
        _block("dispositions", [_disposition_line(d) for d in history.dispositions])
    )
    lines.extend(_block("voided", [_voided_line(v) for v in history.voided]))
    lines.extend(
        _block("releases", [_release_line(r) for r in history.summary.releases])
    )
    lines.extend(_notes(history))
    return "\n".join(lines)
