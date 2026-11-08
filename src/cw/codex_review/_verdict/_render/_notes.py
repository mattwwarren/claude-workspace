"""The headline and the per-concern note sections of a verdict comment.

Each note helper is written to the same empty-list-returns-``[]`` shape, so a
pass with nothing to say about a concern produces no bytes for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._context._default_branch_config import (
    REVIEW_EXTRAS_PATH,
    REVIEW_POLICY_PATH,
)
from cw.codex_review._verdict._render._common import _safe

if TYPE_CHECKING:
    from cw.auto_dev_result import Review
    from cw.review_findings import (
        AgentSpecStatus,
        ReviewerRunRecord,
        ReviewVerdict,
    )


def _render_clean_headline(review: Review, *, fix_loop_enabled: bool) -> str:
    """Render the non-blocking headline, distinguishing three histories (#1705).

    ``Review.fix_cycles_used``/``must_fix_initial``/``deferred`` alone cannot
    tell a fix-loop-disabled single pass apart from a fix-loop-enabled pass
    whose cycle-0 review was already clean — both produce
    ``fix_cycles_used == 0``. ``fix_loop_enabled`` (caller-known, threaded in
    via ``synthesize_codex_review_result``) is the discriminator (R1).

    Within the ``fix_cycles_used > 0`` (genuine fix-loop engagement) branch,
    ``Review.had_real_commit`` (#1723) further discriminates a converged loop
    that actually committed a change from one that converged purely because
    every fix cycle was a tolerated no-op — only ``"no_op"`` renders an
    UNVERIFIED headline rather than claiming findings were resolved;
    ``"committed"`` and ``"unknown"`` keep the resolved wording (#1745).
    """
    if review.fix_cycles_used > 0:
        # `_clean_exit` (the sole caller reaching this branch) is only invoked
        # when `open_findings` is empty, so `review.deferred` is always `0`
        # here — this formula's overflow guard (#2352) lives in
        # `_render_history_note`, not here.
        resolved = review.must_fix_initial - review.deferred
        if review.had_real_commit == "no_op":
            return (
                f"**UNVERIFIED** — the fix loop converged without changing "
                f"any file: {resolved} of {review.must_fix_initial} "
                f"originally-found MUST_FIX finding(s) show as resolved "
                f"across {review.fix_cycles_used} fix cycle(s), but no fix "
                "cycle actually committed a change. Treat this as unverified "
                "rather than genuinely fixed."
            )
        return (
            f"**Non-blocking** — {resolved} of {review.must_fix_initial} "
            f"originally-found MUST_FIX finding(s) resolved across "
            f"{review.fix_cycles_used} fix cycle(s); none remain open."
        )
    if fix_loop_enabled:
        return (
            "**Non-blocking** — no MUST_FIX findings. The fix loop was "
            "available for this run; none were needed."
        )
    return (
        "**Non-blocking** — no MUST_FIX findings. Single-pass review "
        "(fix loop disabled for this lane)."
    )


def _render_history_note(review: Review, *, fix_loop_enabled: bool) -> list[str]:
    """Render the blocking-branch history note (R1's blocking-branch half).

    Mirrors ``_render_clean_headline``'s discrimination for the still-blocking
    case: a fix-loop-disabled block must state its own single-pass state
    rather than silently looking like a fix loop that made no progress.
    """
    if not fix_loop_enabled:
        return ["_Single-pass review — fix loop disabled for this lane._", ""]
    if review.fix_cycles_used > 0:
        # #2352: `deferred` can exceed `must_fix_initial` once
        # `_admit_new_must_fix` admits a genuinely-new MUST_FIX finding
        # mid-loop — the two counts describe different populations, so
        # subtracting them can go negative. Render both counts independently
        # instead of deriving a "resolved" count in that case.
        if review.deferred > review.must_fix_initial:
            return [
                f"_{review.must_fix_initial} originally-found MUST_FIX "
                f"finding(s); {review.deferred} still open after "
                f"{review.fix_cycles_used} fix cycle(s) — more MUST_FIX "
                "finding(s) were admitted into the fix loop mid-run than "
                "were originally found, so a resolved count cannot be "
                "derived by subtracting the two._",
                "",
            ]
        resolved = review.must_fix_initial - review.deferred
        return [
            f"_{resolved} of {review.must_fix_initial} originally-found "
            f"MUST_FIX finding(s) resolved across {review.fix_cycles_used} "
            f"fix cycle(s); {review.deferred} still open._",
            "",
        ]
    return []


def _render_failed_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "PARTIAL COVERAGE" note naming any role that failed to run.

    Reads ``verdict.agents_run`` (#1710's ``ReviewerRunRecord`` list) directly
    — no new plumbing needed. Surfaces reviewer-run failure onto the posted
    GitHub comment; previously only reached ``Blocker.details`` internally via
    ``_format_failures_detail`` on the zero-documents path.
    """
    failed_roles = [r.reviewer_role for r in verdict.agents_run if r.status == "failed"]
    if not failed_roles:
        return []
    roles = ", ".join(_safe(role) for role in failed_roles)
    plural = "" if len(failed_roles) == 1 else "s"
    return [
        f"**PARTIAL COVERAGE** — {len(failed_roles)} role{plural} failed to run: "
        f"{roles}.",
        "",
    ]


def _degraded_role_label(record: ReviewerRunRecord) -> str:
    """Name one degraded role, with its stated reason if it gave one (#1775).

    ``record.detail`` is copied verbatim from the source
    ``ReviewerFindingsDocument`` by :func:`consolidate_verdict`, so a blank
    value here means the reviewer genuinely gave no reason -- not that the
    plumbing dropped it.
    """
    if record.detail:
        return f"{_safe(record.reviewer_role)}: degraded — {_safe(record.detail)}"
    return f"{_safe(record.reviewer_role)}: degraded (no reason given)"


def _render_degraded_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "DEGRADED COVERAGE" note naming any role that ran degraded.

    Sibling of :func:`_render_failed_roles_note`: reads ``verdict.agents_run``
    directly, same empty-list-returns-``[]`` shape. A "failed" role (never
    produced a document) and a "degraded" role (produced a document but
    could not complete a required check) are distinct facts, so this note is
    additive to -- not a replacement for -- the partial-coverage note (#1775).
    """
    degraded = [r for r in verdict.agents_run if r.status == "degraded"]
    if not degraded:
        return []
    labels = ", ".join(_degraded_role_label(r) for r in degraded)
    plural = "" if len(degraded) == 1 else "s"
    return [
        f"**DEGRADED COVERAGE** — {len(degraded)} role{plural} ran degraded: {labels}.",
        "",
    ]


def _render_capability_note(verdict: ReviewVerdict) -> list[str]:
    """Render the probed filesystem-capability mode the review ran under.

    Deferred from #1709 pending #1705's rewrite of this function (#1725).
    ``capability_mode`` is ``None`` for any run that never probed (e.g. the
    LocalExecutor path, or a test verdict built without capability wiring) --
    that must render nothing, not "unknown", per #1709/#1725: an unprobed run
    and a probed-but-unclassifiable run are different facts, and only the
    probe (``_classify_capability_failure``) is allowed to say "unknown".
    """
    if verdict.capability_mode is None:
        return []
    if verdict.capability_mode == "capable":
        return ["_Reviewed with repo filesystem access (capable)._", ""]
    reason_suffix = (
        f" (reason: {_safe(verdict.capability_reason)})"
        if verdict.capability_reason
        else ""
    )
    return [
        "_Reviewed in degraded mode — inlined-diff-only, no repo filesystem "
        f"access{reason_suffix}._",
        "",
    ]


def _agent_spec_label(status: AgentSpecStatus) -> str:
    """Name why *status*'s role ran without a loaded specification (#1773).

    ``empty_repo_file`` is checked first and independently of ``source``: once
    the repo-tracked file was found blank AND nothing usable replaced it, that
    is the actionable fact for whoever reads the comment, whichever source was
    consulted last.
    """
    if status.empty_repo_file:
        return "present but empty, no usable fallback"
    if status.source == "global":
        return "global spec found but empty"
    return "absent"


def _render_agent_spec_note(verdict: ReviewVerdict) -> list[str]:
    """Render the per-role agent-spec resolution summary (#1773).

    An empty ``agent_spec_status`` renders nothing: a verdict from a path that
    never resolved specs (the LocalExecutor path, a directly-synthesized test
    verdict) has no claim to make either way — same convention as
    ``_render_capability_note``'s unprobed case.

    A role counts as unspecified iff its final ``empty`` is True, whatever
    ``source`` says, which yields exactly one of three headlines. The
    recovered-empty-repo-file addendum is then appended independently of which
    headline won, so a truncated repo-tracked file still gets reported in a
    pass where some *other* role was also unspecified.
    """
    statuses = verdict.agent_spec_status
    if not statuses:
        return []
    unspecified = [s for s in statuses if s.empty]
    total = len(statuses)
    if not unspecified:
        line = f"_Agent specs loaded for all {total} reviewer role(s)._"
    elif len(unspecified) == total:
        line = (
            "**ALL AGENT SPECS UNSPECIFIED** — no reviewer role in this pass "
            "had a loaded agent specification (repo or global); every "
            "prompt's `## Agent Specification` section was empty."
        )
    else:
        named = ", ".join(
            f"{_safe(s.role)} ({_agent_spec_label(s)})" for s in unspecified
        )
        line = (
            f"**AGENT SPEC(S) UNSPECIFIED** — {len(unspecified)} of {total} "
            f"role(s) ran without a loaded specification: {named}."
        )
    # A still-unspecified role already carries "(present but empty, no usable
    # fallback)" above, so only genuinely recovered ones get the addendum.
    for s in statuses:
        if s.empty_repo_file and not s.empty:
            line += (
                f" **NOTE:** {_safe(s.role)}'s repo-tracked spec was present but "
                "empty — recovered via the global fallback; the repo-tracked "
                "file may be truncated or need attention."
            )
    return [line, ""]


def _render_delta_note(verdict: ReviewVerdict) -> list[str]:
    """Say which head this pass's diff was taken from, when it was a delta.

    ``None`` means say nothing (``_render_capability_note``'s convention): the
    pass reviewed the whole branch, which is the unremarkable case.
    """
    if verdict.previous_reviewed_sha is None:
        return []
    return [
        "This pass reviewed only what changed since "
        f"`{_safe(verdict.previous_reviewed_sha)}` (fix-loop delta review).",
        "",
    ]


def _render_review_config_note(verdict: ReviewVerdict) -> list[str]:
    """Say where the repository's review configuration was read from (#3038).

    Renders only when the configuration matters, so a repo with none of the
    four files on its default branch, and none edited, gets a comment
    byte-identical to one from before the default-branch read. In order:
    unresolved (the files were treated as absent), modified by the branch (the
    default-branch copy was applied regardless), or the plain provenance line
    naming the ref and its short commit so a stale tracking ref is auditable.
    ``None`` (a path that never resolved configuration) renders nothing.
    """
    config = verdict.review_config
    if config is None:
        return []
    branch = _safe(config.default_branch)
    if not config.resolved:
        return [
            f"**REVIEW CONFIGURATION UNAVAILABLE** — neither `origin/{branch}` nor "
            f"`{branch}` resolves in this worktree, so `{REVIEW_EXTRAS_PATH}`, "
            f"`{REVIEW_POLICY_PATH}` and the sensitive-files manifests were "
            "treated as absent: no repository review configuration was applied to "
            "this review.",
            "",
        ]
    ref = _safe(config.ref or config.default_branch)
    sha = _safe(config.short_commit)
    if config.modified:
        paths = ", ".join(f"`{_safe(p)}`" for p in config.modified)
        return [
            "**REVIEW CONFIGURATION MODIFIED BY BRANCH** — this branch changed "
            f"{paths}; this review applied the configuration at `{ref}` "
            f"(`{sha}`), not the branch's edits.",
            "",
        ]
    if config.present:
        paths = ", ".join(f"`{_safe(p)}`" for p in config.present)
        return [f"_Review configuration read from `{ref}` (`{sha}`): {paths}._", ""]
    return []


def _render_debt_note(verdict: ReviewVerdict) -> list[str]:
    """Render the debt the fix loop recorded instead of acting on (#1837).

    Two kinds land here: accepted DEBT-severity findings, and MUST_FIX
    findings the loop's admission gate refused because the latest fix cycle
    did not cause them. Neither blocks, and neither should vanish — this
    section is where an operator finds out what was set aside.

    Empty-returns-``[]``, mirroring ``_render_failed_roles_note``. The list is
    already deduplicated by fingerprint before it reaches the verdict, so
    there is no "already rendered" bookkeeping to do here.

    This prints ``record.fingerprint[1]`` — the SUMMARY half of the ledger
    identity, not the ``file::`` prefix — and only for DEBT records. That gap
    is what #2210's :func:`_render_settle_payloads` closes for the blocking
    findings an operator actually has to settle.
    """
    if not verdict.debt:
        return []
    lines = ["### Debt — recorded, not blocking", ""]
    for record in verdict.debt:
        lines.append(
            f"- **{_safe(record.file)}** — {_safe(record.summary)} "
            f"({record.tracking_disposition}, fingerprint "
            f"`{_safe(record.fingerprint[1])}`)"
        )
        if record.suggested_follow_up:
            lines.append(f"  - {_safe(record.suggested_follow_up)}")
    lines.append("")
    return lines
