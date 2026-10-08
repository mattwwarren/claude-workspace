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
(cycle-0) MUST_FIX findings the cycle resolved and how many lines the cycle's
commit churned. The loop is diverging when it has resolved none of the
original findings for ``stall_cycles`` consecutive cycles (default
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
from cw.codex_review import _parse_unified_diff
from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.worktree import _parse_numstat_totals

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.review_findings import AcceptedFinding

_log = logging.getLogger(__name__)

# Default consecutive cycles resolving zero originally-found MUST_FIX findings
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


class _DivergenceCycleRecord(NamedTuple):
    """One fix cycle's progress-vs-growth measurements."""

    cycle: int
    must_fix_before: int
    must_fix_after: int
    originally_resolved: int
    net_lines_added: int
    cumulative_net_lines_added: int


class DivergenceState(NamedTuple):
    """Cross-cycle divergence-tracking state, threaded immutably through the loop.

    ``cumulative_net_lines_added`` never resets: only ``stall_streak`` resets on
    a cycle that resolves an original finding, so one token fix cannot launder
    a large accumulated diff.
    """

    original_keys: frozenset[_OpenFindingKey]
    pre_loop_diff_lines: int
    pre_loop_head_sha: str
    stall_streak: int
    cumulative_net_lines_added: int
    history: tuple[_DivergenceCycleRecord, ...]
    stall_cycles: int = _DIVERGENCE_STALL_CYCLES


def initial_divergence_state(
    *,
    original_keys: frozenset[_OpenFindingKey],
    pre_loop_diff_lines: int,
    pre_loop_head_sha: str,
    stall_cycles: int = _DIVERGENCE_STALL_CYCLES,
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
    )


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


def record_divergence_cycle(
    state: DivergenceState,
    *,
    cycle: int,
    pre_open_keys: frozenset[_OpenFindingKey],
    post_open_keys: frozenset[_OpenFindingKey],
    net_lines_added: int,
) -> DivergenceState:
    """Return *state* advanced by one fix cycle's before/after open-finding sets.

    Only originally-found keys count as progress: resolving a finding the loop
    itself introduced is the loop cleaning up after itself, not convergence.
    """
    originally_resolved = len((pre_open_keys & state.original_keys) - post_open_keys)
    stall_streak = state.stall_streak + 1 if originally_resolved == 0 else 0
    cumulative = state.cumulative_net_lines_added + net_lines_added
    record = _DivergenceCycleRecord(
        cycle=cycle,
        must_fix_before=len(pre_open_keys),
        must_fix_after=len(post_open_keys),
        originally_resolved=originally_resolved,
        net_lines_added=net_lines_added,
        cumulative_net_lines_added=cumulative,
    )
    return state._replace(
        stall_streak=stall_streak,
        cumulative_net_lines_added=cumulative,
        history=(*state.history, record),
    )


def is_diverging(state: DivergenceState) -> bool:
    """Return True iff the loop is stalled on original findings AND growing."""
    threshold = max(
        _DIVERGENCE_MIN_LINES,
        state.pre_loop_diff_lines * _DIVERGENCE_PRE_LOOP_FRACTION,
    )
    return (
        state.stall_streak >= state.stall_cycles
        and state.cumulative_net_lines_added > threshold
    )


def _loop_diff(
    worktree: Path, since: str
) -> tuple[dict[str, dict[int, str]], frozenset[str]]:
    """Added lines per file since *since*, and the files the loop created.

    Created files come from ``--diff-filter=A``: ``_parse_unified_diff`` drops
    the ``new file mode`` header. ``--no-renames`` so a copy reads as added.
    """
    span = f"{since}..HEAD"
    diff = git_output(["diff", "-U0", "--no-renames", span], cwd=worktree)
    created = git_output(
        ["diff", "--diff-filter=A", "--name-only", "--no-renames", span],
        cwd=worktree,
    )
    _diffs, added, _window, _changed = _parse_unified_diff(diff)
    return added, frozenset(line for line in created.splitlines() if line)


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


def loop_generated_findings(
    worktree: Path, state: DivergenceState, open_findings: list[AcceptedFinding]
) -> list[AcceptedFinding]:
    """Return the open findings that point at code the fix loop itself added.

    A finding qualifies iff its identity (:func:`_open_finding_key`, the key
    ``original_keys`` holds) is not an originally-found one, its file is not
    ``"N/A"``, and either its line range overlaps a line added since the
    pre-loop head or it is file-level on a file the loop created. Runs at park
    time, so a git failure logs one WARNING and returns ``[]`` rather than
    turning the park into a crash.
    """
    try:
        added, created = _loop_diff(worktree, state.pre_loop_head_sha)
    except subprocess.CalledProcessError as exc:
        _log.warning(
            "codex fix loop: loop-generated finding detection failed "
            "(pre_loop_head=%s): %s",
            state.pre_loop_head_sha,
            exc,
        )
        return []
    return [
        af
        for af in open_findings
        if af.finding.file != _NO_FILE
        and _open_finding_key(af.finding) not in state.original_keys
        and _on_loop_code(af, added, created)
    ]


def render_divergence_report(
    state: DivergenceState, loop_generated: Sequence[AcceptedFinding] = ()
) -> str:
    """Render the per-cycle breakdown appended to the park's blocker details."""
    lines = [
        "Fix loop diverging: no originally-found MUST_FIX finding resolved for "
        f"{state.stall_streak} consecutive cycle(s) while the diff grew by "
        f"{state.cumulative_net_lines_added} line(s). Pre-loop head: "
        f"{state.pre_loop_head_sha} (pre-loop diff: "
        f"{state.pre_loop_diff_lines} line(s)).",
    ]
    lines.extend(
        f"- cycle {r.cycle}: MUST_FIX {r.must_fix_before}→{r.must_fix_after}, "
        f"originally-resolved {r.originally_resolved}, "
        f"net lines +{r.net_lines_added} (cumulative {r.cumulative_net_lines_added})"
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
        "(ticket=%s, stall_streak=%d, cumulative_net_lines_added=%d, since=%s, "
        "loop_generated=%d)",
        ticket_id,
        state.stall_streak,
        state.cumulative_net_lines_added,
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
