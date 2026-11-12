"""Loop-wide divergence guard for the codex fix loop (#2394).

The #1837 admission gate (:mod:`cw.codex_fix_loop.convergence`) decides which
newly-appearing MUST_FIX findings a fix cycle caused, and a finding anchored in
the latest delta is always admitted — correctly, since the cycle really did
write that code. The failure mode it cannot see is a loop that answers each
MUST_FIX by writing *more* code, which the next re-review flags, which the next
cycle answers with still more code. Every individual admission is legitimate;
the loop as a whole diverges, burning every cycle while the diff balloons.

This module is a second-order guard on loop *progress*, not finding
*identity*. After each cycle it records how many of the originally-found
(cycle-0) MUST_FIX findings the cycle resolved, how many new findings the
cycle drew that point at the loop's own code (#2665), and how many lines the
cycle's commit churned. Net progress is resolved originals minus those
loop-attributed new findings, so a reworded original that cites a helper the
loop added is not progress. The loop is diverging when it has made no net
progress for ``stall_cycles`` consecutive cycles (default
:data:`_DIVERGENCE_STALL_CYCLES`, 1 since #2633; ``codex_fix_loop_stall_cycles``)
AND its cumulative churn has passed a size threshold — both conditions, so
neither a growing-but-converging loop nor a stalled-but-small one trips it.
The park reports which open findings the loop's own code generated (#2633).

State is an immutable :class:`DivergenceState` threaded through pure "take
state, return new state" functions, matching ``_track_open_findings``'s shape.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_output
from cw.codex_fix_loop.convergence import _open_finding_key
from cw.codex_fix_loop.loop_citation import cites_symbol, loop_added_symbols
from cw.codex_review import _parse_unified_diff
from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.review_convergence import resolve_loop_diff_base
from cw.worktree import _parse_numstat_totals

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.review_findings import AcceptedFinding

_log = logging.getLogger(__name__)

# Default consecutive cycles making no net progress on the originally-found
# MUST_FIX findings
# before the loop may be declared diverging (#2633: one cycle; configurable via
# codex_fix_loop_stall_cycles, globally or per lane).
_DIVERGENCE_STALL_CYCLES = 1
# A finding with no diff anchor; never attributable to the loop's own lines.
_NO_FILE = "N/A"
# Absolute floor on cumulative fix-cycle churn (added + removed lines) before
# the loop may be declared diverging — keeps a small stalled loop running to
# the ordinary cycle cap.
_DIVERGENCE_MIN_LINES = 150
# Fraction of the pre-loop branch diff the cumulative churn must also exceed,
# so a large PR is not tripped by fix churn proportionate to its own size.
_DIVERGENCE_PRE_LOOP_FRACTION = 0.5
# #2666: the threshold may also scale with the original findings' anchor size,
# so a six-line finding resolved by 59 churned lines (#2590) parks at 48 rather
# than needing 150. Relative minimum matches DEFAULT_CUMULATIVE_GROWTH_BUDGET_LINES
# by convention only (separate constant, no import).
_DIVERGENCE_ANCHOR_RATIO = 8
_DIVERGENCE_RELATIVE_MIN_LINES = 40


class _DivergenceCycleRecord(NamedTuple):
    """One fix cycle's progress-vs-growth measurements."""

    cycle: int
    must_fix_before: int
    must_fix_after: int
    originally_resolved: int
    net_lines_added: int
    cumulative_net_lines_added: int
    loop_attributed: int


class DivergenceState(NamedTuple):
    """Cross-cycle divergence-tracking state, threaded immutably through the loop.

    ``cumulative_net_lines_added`` never resets: only ``stall_streak`` resets on
    a cycle with net progress on the original findings, so one token fix cannot
    launder a large accumulated diff.

    ``original_anchor_lines`` (#2666) is the total line count of the original
    findings' anchors, or ``None`` when the relative threshold does not apply
    (switch off, or any original finding is file-level / unanchored).
    """

    original_keys: frozenset[_OpenFindingKey]
    pre_loop_diff_lines: int
    pre_loop_head_sha: str
    stall_streak: int
    cumulative_net_lines_added: int
    history: tuple[_DivergenceCycleRecord, ...]
    stall_cycles: int = _DIVERGENCE_STALL_CYCLES
    original_anchor_lines: int | None = None


def initial_divergence_state(
    *,
    original_keys: frozenset[_OpenFindingKey],
    pre_loop_diff_lines: int,
    pre_loop_head_sha: str,
    stall_cycles: int = _DIVERGENCE_STALL_CYCLES,
    original_anchor_lines: int | None = None,
) -> DivergenceState:
    """Return the pre-loop state seeded from cycle 0's open MUST_FIX keys."""
    return DivergenceState(
        original_keys=original_keys,
        pre_loop_diff_lines=pre_loop_diff_lines,
        pre_loop_head_sha=pre_loop_head_sha,
        stall_streak=0,
        cumulative_net_lines_added=0,
        history=(),
        stall_cycles=stall_cycles,
        original_anchor_lines=original_anchor_lines,
    )


def total_anchor_lines(findings: Iterable[AcceptedFinding]) -> int | None:
    """Return the summed anchor size of *findings*, or ``None`` if unusable.

    Each finding's anchor is ``line_end - line_start + 1`` (``line_end`` falls
    back to ``line_start``, floor 1). ``None`` when there are no findings, or
    when any finding is file-level or ``"N/A"``: the relative threshold then
    does not apply and the absolute one stays in force.
    """
    total = 0
    seen = False
    for af in findings:
        finding = af.finding
        if finding.file == _NO_FILE or finding.line_start is None:
            return None
        end = finding.line_end if finding.line_end is not None else finding.line_start
        total += max(1, end - finding.line_start + 1)
        seen = True
    return total if seen else None


def _absolute_threshold(state: DivergenceState) -> int:
    return max(
        _DIVERGENCE_MIN_LINES,
        int(state.pre_loop_diff_lines * _DIVERGENCE_PRE_LOOP_FRACTION),
    )


def divergence_threshold(state: DivergenceState) -> int:
    """Return the cumulative-churn bar the loop must exceed to be diverging.

    Absolute ``max(150, 0.5 x pre-loop diff)`` when no anchor is known. With
    anchors, ``min`` of that and ``max(40, 8 x anchor lines)``: the relative
    term can only lower the bar, and the absolute value stays the upper bound.
    """
    absolute = _absolute_threshold(state)
    if state.original_anchor_lines is None:
        return absolute
    relative = max(
        _DIVERGENCE_RELATIVE_MIN_LINES,
        _DIVERGENCE_ANCHOR_RATIO * state.original_anchor_lines,
    )
    return min(absolute, relative)


def net_lines_for_commit(worktree: Path, commit_sha: str | None) -> int:
    """Return *commit_sha*'s churn (added + removed lines), or 0 for no commit.

    ``None`` is a tolerated no-op fix cycle — nothing was committed, so there
    is nothing to measure and no git call is made.
    """
    if commit_sha is None:
        return 0
    numstat = git_output(
        ["diff", "--numstat", f"{commit_sha}~1..{commit_sha}"], cwd=worktree
    )
    _files, lines = _parse_numstat_totals(numstat)
    return lines


def pre_loop_diff_lines(worktree: Path, default_branch: str) -> int:
    """Return the branch's churn (added + removed lines) before the fix loop.

    Measured from :func:`~cw.review_convergence.resolve_loop_diff_base`, the
    same base the loop's review diff uses (#2823), so a stale local default
    branch cannot inflate the baseline the divergence threshold scales with.
    """
    base = resolve_loop_diff_base(worktree, default_branch)
    numstat = git_output(["diff", "--numstat", f"{base}...HEAD"], cwd=worktree)
    _files, lines = _parse_numstat_totals(numstat)
    return lines


def record_divergence_cycle(
    state: DivergenceState,
    *,
    cycle: int,
    pre_open_keys: frozenset[_OpenFindingKey],
    post_open_keys: frozenset[_OpenFindingKey],
    net_lines_added: int,
    loop_attributed: int,
) -> DivergenceState:
    """Return *state* advanced by one fix cycle's before/after open-finding sets.

    Only originally-found keys count as progress: resolving a finding the loop
    itself introduced is the loop cleaning up after itself, not convergence.
    *loop_attributed* (#2665) is how many of the cycle's new findings point at
    code the loop added; each offsets one resolved original, because a
    reworded original that cites the loop's own helper replaced a complaint
    rather than resolving it. The streak advances unless net progress
    (``originally_resolved - loop_attributed``) is positive.
    """
    originally_resolved = len((pre_open_keys & state.original_keys) - post_open_keys)
    net_progress = originally_resolved - loop_attributed
    stall_streak = state.stall_streak + 1 if net_progress <= 0 else 0
    cumulative = state.cumulative_net_lines_added + net_lines_added
    record = _DivergenceCycleRecord(
        cycle=cycle,
        must_fix_before=len(pre_open_keys),
        must_fix_after=len(post_open_keys),
        originally_resolved=originally_resolved,
        net_lines_added=net_lines_added,
        cumulative_net_lines_added=cumulative,
        loop_attributed=loop_attributed,
    )
    return state._replace(
        stall_streak=stall_streak,
        cumulative_net_lines_added=cumulative,
        history=(*state.history, record),
    )


def is_diverging(state: DivergenceState) -> bool:
    """Return True iff the loop makes no net progress on originals AND is growing."""
    return (
        state.stall_streak >= state.stall_cycles
        and state.cumulative_net_lines_added > divergence_threshold(state)
    )


class _LoopDiff(NamedTuple):
    """What the loop has written since the pre-loop head."""

    file_diffs: dict[str, str]
    added: dict[str, dict[int, str]]
    created: frozenset[str]


def _loop_diff(worktree: Path, since: str) -> _LoopDiff:
    """Per-file diffs, added lines and created files since *since*.

    Created files come from ``--diff-filter=A``: ``_parse_unified_diff`` drops
    the ``new file mode`` header. ``--no-renames`` so a copy reads as added.
    """
    span = f"{since}..HEAD"
    diff = git_output(["diff", "-U0", "--no-renames", span], cwd=worktree)
    created = git_output(
        ["diff", "--diff-filter=A", "--name-only", "--no-renames", span],
        cwd=worktree,
    )
    file_diffs, added, _window, _changed = _parse_unified_diff(diff)
    return _LoopDiff(
        file_diffs, added, frozenset(line for line in created.splitlines() if line)
    )


def _safe_loop_diff(worktree: Path, state: DivergenceState) -> _LoopDiff | None:
    """:func:`_loop_diff` since the pre-loop head, or ``None`` after one WARNING.

    Both callers are advisory, so a git failure must not crash the loop.
    """
    try:
        return _loop_diff(worktree, state.pre_loop_head_sha)
    except subprocess.CalledProcessError as exc:
        _log.warning(
            "codex fix loop: loop-generated finding detection failed "
            "(pre_loop_head=%s): %s",
            state.pre_loop_head_sha,
            exc,
        )
        return None


def _on_loop_code(
    af: AcceptedFinding, added: dict[str, dict[int, str]], created: frozenset[str]
) -> bool:
    """Whether *af* points at lines (or a whole file) the loop itself wrote."""
    finding = af.finding
    if finding.line_start is None:
        return finding.file in created
    end = finding.line_end if finding.line_end is not None else finding.line_start
    lines = added.get(finding.file, {})
    return any(line in lines for line in range(finding.line_start, end + 1))


def _attributed(af: AcceptedFinding, loop: _LoopDiff, symbols: frozenset[str]) -> bool:
    """Whether *af* cites a loop-added symbol, or points at loop-written lines.

    The line half never applies to ``"N/A"`` (no diff anchor); the name half
    does, since a finding with no anchor can still name the loop's helper.
    """
    if cites_symbol(af.finding, symbols):
        return True
    return af.finding.file != _NO_FILE and _on_loop_code(af, loop.added, loop.created)


def loop_generated_findings(
    worktree: Path, state: DivergenceState, open_findings: list[AcceptedFinding]
) -> list[AcceptedFinding]:
    """Return the open findings that point at code the fix loop itself added.

    A finding qualifies iff its identity (:func:`_open_finding_key`, the key
    ``original_keys`` holds) is not an originally-found one and it is
    attributed to the loop (:func:`_attributed`): it cites a symbol the loop
    added (#2665), or its line range overlaps a line added since the pre-loop
    head, or it is file-level on a file the loop created. Runs at park time, so
    a git failure logs one WARNING and returns ``[]`` rather than turning the
    park into a crash.
    """
    loop = _safe_loop_diff(worktree, state)
    if loop is None:
        return []
    symbols = loop_added_symbols(loop.file_diffs, loop.added)
    return [
        af
        for af in open_findings
        if _open_finding_key(af.finding) not in state.original_keys
        and _attributed(af, loop, symbols)
    ]


def attributed_new_finding_count(
    worktree: Path,
    state: DivergenceState,
    *,
    pre_open_keys: frozenset[_OpenFindingKey],
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
) -> int:
    """Count this cycle's new findings that :func:`_attributed` to the loop (#2665).

    Only findings that are neither original nor already open before the cycle
    count, so a persistent loop-generated finding is not charged twice. With
    none, no git call is made. A git failure logs one WARNING and counts 0.
    """
    fresh = [
        af
        for key, af in open_findings.items()
        if key not in pre_open_keys and key not in state.original_keys
    ]
    if not fresh:
        return 0
    loop = _safe_loop_diff(worktree, state)
    if loop is None:
        return 0
    symbols = loop_added_symbols(loop.file_diffs, loop.added)
    return sum(_attributed(af, loop, symbols) for af in fresh)


def _threshold_line(state: DivergenceState) -> str:
    """Name the threshold and how it was derived (#2666)."""
    threshold = divergence_threshold(state)
    if state.original_anchor_lines is None:
        return (
            f"Divergence threshold: {threshold} line(s) of cumulative churn "
            f"(absolute: max({_DIVERGENCE_MIN_LINES}, "
            f"{_DIVERGENCE_PRE_LOOP_FRACTION} x pre-loop diff))."
        )
    anchor = state.original_anchor_lines
    return (
        f"Divergence threshold: {threshold} line(s) of cumulative churn "
        f"(relative to {anchor} original anchor line(s): "
        f"max({_DIVERGENCE_RELATIVE_MIN_LINES}, "
        f"{_DIVERGENCE_ANCHOR_RATIO} x {anchor}); "
        f"the absolute threshold of {_absolute_threshold(state)} still caps it)."
    )


def render_divergence_report(
    state: DivergenceState, loop_generated: Sequence[AcceptedFinding] = ()
) -> str:
    """Render the per-cycle breakdown appended to the park's blocker details."""
    lines = [
        "Fix loop diverging: no net progress on originally-found MUST_FIX "
        f"findings for {state.stall_streak} consecutive cycle(s) (resolved "
        "originals minus new findings attributed to the loop's own code) while "
        f"the diff grew by {state.cumulative_net_lines_added} line(s). "
        f"Pre-loop head: {state.pre_loop_head_sha} (pre-loop diff: "
        f"{state.pre_loop_diff_lines} line(s)).",
        _threshold_line(state),
    ]
    lines.extend(
        f"- cycle {r.cycle}: MUST_FIX {r.must_fix_before}→{r.must_fix_after}, "
        f"originally-resolved {r.originally_resolved}, "
        f"net lines +{r.net_lines_added} (cumulative {r.cumulative_net_lines_added})"
        + (f", loop-attributed {r.loop_attributed}" if r.loop_attributed else "")
        for r in state.history
    )
    if loop_generated:
        open_count = state.history[-1].must_fix_after if state.history else 0
        lines.append(
            "Findings generated by the loop's own code "
            f"({len(loop_generated)} of {open_count} open):"
        )
        lines.extend(
            f"- {af.finding.file}:{af.finding.line_start or ''} {af.finding.summary}"
            for af in loop_generated
        )
    return "\n".join(lines)


def emit_divergence_event(
    *,
    state: DivergenceState,
    ticket_id: str,
    loop_generated: Sequence[AcceptedFinding] = (),
) -> None:
    """Log and record the divergence trip that is about to park the loop."""
    _log.info(
        "auto-dev: codex fix loop diverging; parking early "
        "(ticket=%s, stall_streak=%d, cumulative_net_lines_added=%d, threshold=%d, "
        "since=%s, loop_generated=%d)",
        ticket_id,
        state.stall_streak,
        state.cumulative_net_lines_added,
        divergence_threshold(state),
        state.pre_loop_head_sha,
        len(loop_generated),
    )
    record_event(
        OrchestratorEventType.FIX_LOOP_DIVERGENCE_DETECTED,
        payload={
            "pre_loop_head_sha": state.pre_loop_head_sha,
            "cycles": [record._asdict() for record in state.history],
            "cumulative_net_lines_added": state.cumulative_net_lines_added,
            "stall_streak": state.stall_streak,
            "threshold": divergence_threshold(state),
            "anchor_lines": state.original_anchor_lines,
            "loop_generated_findings": [
                {
                    "file": af.finding.file,
                    "line_start": af.finding.line_start,
                    "summary": af.finding.summary,
                }
                for af in loop_generated
            ],
        },
        correlation_id=ticket_id,
    )
