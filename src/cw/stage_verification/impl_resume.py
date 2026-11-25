"""The codex IMPL re-entry contract, run by cw (#3529, RFC 0018 C2).

An IMPL row whose branch already carries commits is not always a failure. The
Claude-native pre-stage detector guard (``auto-dev-impl.md`` and its appendix)
classifies the entry and picks one of four dispositions; this module is the
cw-run mirror, replacing the old ``existing_branch_work`` refusal.

Entry classification (:func:`classify_entry`):

- ``fresh``: no commit ahead of the fork point;
- ``s2_implementing``: commits ahead, and HEAD does not carry an
  ``Auto-Dev-Stage: impl-complete`` trailer;
- ``past_s2``: HEAD itself carries that trailer. This narrows the Claude
  detector, which accepts the trailer on any commit in ``fork..HEAD``: the
  ``advance`` disposition pushes and stamps ``stage_complete`` with no turn and
  no cw gates, so it may only publish a tip a gated run declared complete.

Dispositions (:func:`resume_disposition`): proceed (a fresh run), resume (run
the turn on top of the existing commits, delivering operator-authority
comments when stale), or advance (past_s2, not stale: no turn). An unconcluded
merge that arrives with a regress signal is adopted
(:mod:`cw.stage_verification.impl_merge_adopt`) and always resumes, whatever
the entry classification was. :func:`plan_reentry` is the one entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from cw._git import git_output
from cw.branch_ahead import merge_in_progress
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_merge_adopt import (
    MERGE_IN_PROGRESS_PREFIX,
    AdoptedMerge,
    adopt_unconcluded_merge,
)
from cw.stage_verification.impl_regress_findings import regress_review_findings
from cw.stage_verification.impl_staleness import (
    REGRESS_IMPL,
    Staleness,
    check_staleness,
    regress_signal,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.local_runner import GitFacts
    from cw.models import TicketTask
    from cw.ticket_snapshot import SnapshotComment

IMPL_COMPLETE_STAGE: Final = "impl-complete"
_TRAILER_FORMAT: Final = "--format=%(trailers:key=Auto-Dev-Stage,valueonly)"
_SHA_PREFIX_CHARS: Final = 12


class EntryKind(StrEnum):
    """What the branch at IMPL entry already holds."""

    FRESH = "fresh"
    S2_IMPLEMENTING = "s2_implementing"
    PAST_S2 = "past_s2"


class Disposition(StrEnum):
    """What the IMPL run does with the entry."""

    PROCEED = "proceed"
    RESUME = "resume"
    ADVANCE = "advance"


@dataclass(frozen=True)
class ImplEntry:
    """A classified entry: its kind, HEAD, the commits ahead (newest first)."""

    kind: EntryKind
    head_sha: str
    commits: tuple[str, ...]
    friction: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReentryPlan:
    """How a re-entry proceeds.

    ``entry`` is ``None`` for a row classify_entry exempts (a repair row, or one
    delivering review findings) whose unconcluded merge was adopted.
    """

    disposition: Disposition
    entry: ImplEntry | None
    staleness: Staleness | None
    merge: AdoptedMerge | None
    friction: tuple[str, ...]


def classify_entry(
    facts: GitFacts, task: TicketTask, worktree: Path
) -> ImplEntry | ImplBlock | None:
    """Classify the branch at IMPL entry.

    ``None`` for a row that carries its own work order: a repair row
    (``review_feedback`` set) or a REVIEW/FINALIZE-regressed row whose findings
    are delivered (#3694, #3862). An unresolvable fork point fails closed (cw
    cannot tell whether the branch carries prior work). A withheld-findings
    outcome adds its friction line to the entry.
    """
    if task.review_feedback is not None:
        return None
    outcome = regress_review_findings(task, worktree)
    if outcome is not None and outcome.findings is not None:
        return None
    if not facts["fork_point"]:
        return ImplBlock(
            IMPL_FAILED,
            "the fork point against the default branch did not resolve, so cw "
            "cannot tell whether this branch already carries work",
        )
    friction = (outcome.friction,) if outcome is not None else ()
    commits = tuple(facts["commits"])
    if not commits:
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        return ImplEntry(EntryKind.FRESH, head, (), friction)
    trailers = git_output(["log", "-1", _TRAILER_FORMAT, "HEAD"], cwd=worktree)
    complete = IMPL_COMPLETE_STAGE in trailers.split()
    kind = EntryKind.PAST_S2 if complete else EntryKind.S2_IMPLEMENTING
    return ImplEntry(kind, commits[0], commits, friction)


def resume_disposition(
    kind: EntryKind | None, *, stale: bool, merge_adopted: bool
) -> Disposition:
    """The disposition table, checked in order.

    An adopted merge always resumes. No entry (an exempt row) or a fresh one
    proceeds. s2 resumes. past_s2 advances only when not stale.
    """
    if merge_adopted:
        return Disposition.RESUME
    if kind is None or kind is EntryKind.FRESH:
        return Disposition.PROCEED
    if kind is EntryKind.S2_IMPLEMENTING or stale:
        return Disposition.RESUME
    return Disposition.ADVANCE


def _merge_step(
    worktree: Path, base_branch: str, task: TicketTask
) -> tuple[AdoptedMerge | None, str] | ImplBlock:
    """Adopt an arriving merge, or refuse one; ``(adopted | None, regress signal)``."""
    present = merge_in_progress(worktree)
    if present is None:
        return ImplBlock(
            IMPL_FAILED,
            f"{MERGE_IN_PROGRESS_PREFIX}cw could not tell whether a merge is in "
            "progress in the worktree",
        )
    signal = regress_signal(task, worktree, merge_present=present)
    if not present:
        return None, signal
    if signal != REGRESS_IMPL:
        return ImplBlock(
            IMPL_FAILED,
            f"{MERGE_IN_PROGRESS_PREFIX}the worktree arrived mid-merge (MERGE_HEAD "
            "is set) with no regress into IMPL, so cw does not adopt it; conclude "
            "or abort the merge, then requeue",
        )
    adopted = adopt_unconcluded_merge(worktree, base_branch=base_branch)
    if isinstance(adopted, ImplBlock):
        return adopted
    return adopted, signal


def _friction(
    entry: ImplEntry | None,
    staleness: Staleness | None,
    merge: AdoptedMerge | None,
    disposition: Disposition,
) -> tuple[str, ...]:
    """The plan's friction lines, in the order the run reports them."""
    lines: list[str] = list(entry.friction) if entry is not None else []
    if staleness is not None:
        lines.extend(staleness.friction)
        if staleness.stale:
            lines.append(f"impl_guard_stale: {', '.join(staleness.reasons)}")
    if merge is not None:
        lines.append(
            f"impl_unconcluded_merge_adopted: {merge.merge_head[:_SHA_PREFIX_CHARS]}"
        )
    if entry is not None and entry.kind is not EntryKind.FRESH:
        head = entry.head_sha
        if disposition is Disposition.ADVANCE:
            lines.append(
                f"impl_advanced_past_s2: HEAD {head[:_SHA_PREFIX_CHARS]} carries "
                "the impl-complete trailer; no turn ran"
            )
        else:
            lines.append(f"impl_resumed_from: {head}")
    return tuple(lines)


def plan_reentry(
    *,
    task: TicketTask,
    worktree: Path,
    base_branch: str,
    facts: GitFacts,
    comments: Sequence[SnapshotComment],
    scratch: Path,
) -> ReentryPlan | ImplBlock | None:
    """Classify the entry, check staleness and pick the disposition.

    ``None`` means proceed exactly as a fresh run does. An adopted merge always
    yields a plan with disposition RESUME, whatever :func:`classify_entry`
    returned (a ``classify_entry`` block still wins, fail-closed). An arriving
    merge with no regress signal is a ``merge_in_progress:`` block.
    """
    merged = _merge_step(worktree, base_branch, task)
    if isinstance(merged, ImplBlock):
        return merged
    merge, signal = merged
    entry = classify_entry(facts, task, worktree)
    if isinstance(entry, ImplBlock):
        return entry
    staleness: Staleness | None = None
    if entry is not None and entry.kind is not EntryKind.FRESH:
        staleness = check_staleness(
            worktree, comments=comments, signal=signal, scratch=scratch
        )
        if staleness.block is not None:
            return staleness.block
    disposition = resume_disposition(
        entry.kind if entry is not None else None,
        stale=staleness.stale if staleness is not None else False,
        merge_adopted=merge is not None,
    )
    if disposition is Disposition.PROCEED:
        return None
    return ReentryPlan(
        disposition,
        entry,
        staleness,
        merge,
        _friction(entry, staleness, merge, disposition),
    )
