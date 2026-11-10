"""The verdict-comment entry point: headline selection and section order.

:func:`render_verdict_comment` picks one of four headlines and then appends
every per-concern section unconditionally, in a fixed order. It owns none of
the sections; each lives in the sibling submodule for its concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._const import CODEX_REVIEWER_LABEL
from cw.codex_review._verdict._render._dispositions import (
    _render_refused_dispositions,
    _render_stale_dispositions,
)
from cw.codex_review._verdict._render._findings import _render_findings
from cw.codex_review._verdict._render._notes import (
    _render_agent_spec_note,
    _render_capability_note,
    _render_clean_headline,
    _render_debt_note,
    _render_degraded_roles_note,
    _render_delta_note,
    _render_failed_roles_note,
    _render_history_note,
    _render_operator_constraints_note,
    _render_review_config_note,
)
from cw.codex_review._verdict._render._rejected import (
    _render_rejected_below_must_fix,
    _render_rejected_must_fix,
    _render_run_failure_discarded_note,
)
from cw.codex_review._verdict._render._settle import _render_settle_payloads
from cw.review_findings import contested_findings

if TYPE_CHECKING:
    from cw.review_findings import ReviewVerdict


def _contest_headline(count: int, verdict: ReviewVerdict) -> list[str]:
    """The decided-constraint contest headline (#2984), first when present."""
    lines = [
        f"**OPERATOR DECISION REQUIRED — {count} finding(s) contest a recorded"
        " decision.** The fix loop did not act on them and nothing was dropped:"
        " each is listed below with the decision it quotes. Settle a finding to"
        " dismiss it, or change course yourself, then requeue."
    ]
    if verdict.blocking:
        lines.append(
            f"{len(verdict.must_fix)} other MUST_FIX finding(s) are also open;"
            " they are held until you have decided."
        )
    return lines


def render_verdict_comment(
    verdict: ReviewVerdict,
    *,
    fix_loop_enabled: bool,
    reviewer_label: str = CODEX_REVIEWER_LABEL,
) -> str:
    """Render a consolidated verdict into a GitHub-issue-comment markdown body.

    ``reviewer_label`` (#2791) names the backend in the title only
    (``## <label> Review Verdict``): the Claude confirm round renders through
    this same function, and must never be posted under a Codex header. The
    default keeps every existing caller byte-identical.

    ``fix_loop_enabled`` is the caller's own already-known fix-loop state for
    this run — required (not optional) so no call site can silently fall back
    to a wrong default (#1705). It discriminates fix-loop-disabled from
    fix-loop-enabled-but-unneeded histories that would otherwise render
    identically from ``verdict.review`` alone.

    The headline is four-way as of #2000: blocking, mechanically-rejected-
    MUST_FIX, proceeding-but-something-below-MUST_FIX-was-deleted, or clean.
    A decided-constraint contest (#2984) is a fifth, checked first.
    Both rejected-findings *sections* render unconditionally regardless of
    which headline won, so the mixed case (something blocking AND something
    dropped) reports both.
    """
    lines = [f"## {reviewer_label} Review Verdict", ""]
    if contested := contested_findings(verdict):
        # #2984: checked before `blocking`. A pure contest is not blocking, so
        # without this branch it would open with the clean headline.
        lines.extend(_contest_headline(len(contested), verdict))
    elif verdict.blocking:
        lines.append(
            f"**BLOCKING** — {len(verdict.must_fix)} MUST_FIX finding(s) must be "
            "addressed before this branch can proceed."
        )
        lines.extend(
            _render_history_note(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    elif verdict.rejected_must_fix:
        # #1714: never render the clean headline here. Nothing survived to
        # block on, but a MUST_FIX was dropped unread -- "Non-blocking, no
        # MUST_FIX findings" would be the exact false all-clear this branch
        # exists to prevent.
        lines.append(
            f"**MUST_FIX REJECTED — OPERATOR REVIEW REQUIRED** — "
            f"{len(verdict.rejected_must_fix)} MUST_FIX finding(s) were "
            "mechanically rejected before adjudication (dropped, not evaluated "
            "on their merits) and require operator review before this branch "
            "can proceed."
        )
    elif verdict.rejected_count:
        # #2000: nothing MUST_FIX-shaped was dropped (the branch above already
        # returned if so), so this pass does proceed -- but it proceeds having
        # deleted findings nobody read, and the clean headline would say the
        # opposite. Qualified, not blocking: the round-1 operator resolution
        # keeps this informational rather than folding a matcher miss into
        # Health.recommendation's "coverage degraded" gate.
        lines.append(
            f"**PROCEED ({verdict.rejected_count} finding(s) mechanically "
            "rejected)** — no MUST_FIX findings survived validation, but "
            f"{verdict.rejected_count} finding(s) below MUST_FIX were "
            "mechanically rejected before adjudication and never evaluated "
            "on their merits — see the rejected-findings section below "
            "before treating this pass as clean."
        )
    else:
        lines.append(
            _render_clean_headline(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    lines.append("")
    lines.extend(_render_failed_roles_note(verdict))
    lines.extend(_render_degraded_roles_note(verdict))
    lines.extend(_render_capability_note(verdict))
    lines.extend(_render_agent_spec_note(verdict))
    lines.extend(_render_delta_note(verdict))
    lines.extend(_render_review_config_note(verdict))
    lines.extend(_render_operator_constraints_note(verdict.review))
    lines.extend(_render_rejected_must_fix(verdict))
    lines.extend(_render_rejected_below_must_fix(verdict))
    lines.extend(_render_run_failure_discarded_note(verdict))
    lines.extend(_render_debt_note(verdict))
    lines.extend(_render_findings(verdict, "MUST_FIX", "MUST_FIX"))
    lines.extend(_render_findings(verdict, "SHOULD_FIX", "SHOULD_FIX"))
    # #2210 round 2: immediately before the settle machinery, because the
    # action it asks for IS that machinery — an operator reading "this record
    # was refused" needs "here is how to record it properly" next, not five
    # sections away.
    lines.extend(_render_refused_dispositions(verdict))
    # #2232: beside the refusals, and for the same reason — both are "a record
    # you made did not fire, here is why", and both want the settle machinery
    # they point at to be the next thing read.
    lines.extend(_render_stale_dispositions(verdict))
    # #2210: last, so the operator reads the findings before the machinery for
    # settling them. Every producer of this text (Blocker.details, the fix
    # loop's park, and the posted comment) goes through this one function, so
    # one insertion covers every lane.
    lines.extend(_render_settle_payloads(verdict))
    return "\n".join(lines).rstrip() + "\n"
