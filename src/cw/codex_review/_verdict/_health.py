"""Reviewer-roster health signals derived from per-role document status.

The clean-review ``Health`` derivation and the two failure predicates the
synthesis disposition table reads: whether any run failure is retry-eligible,
and how a reviewer's own ``status`` maps onto reduced coverage — including the
one structurally-forced degradation that must not count as a signal about the
diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import AgentHealthEntry, Health
from cw.codex_review._const import _TRANSIENT_FAILURE_REASONS
from cw.executor_diagnostics import append_diagnostics_pointer

if TYPE_CHECKING:
    from cw.review_findings import (
        ReviewerFindingsDocument,
        ReviewerHealthStatus,
        ReviewerRunFailure,
    )

# #1856, widened by #2174: the codex-review sandbox is unconditionally
# read-only for every reviewer role (`_roles.py::_build_generic_codex_argv`,
# MUST_FIX 4 from #1236) and the prompt tells the model outright that write
# access "is neither offered nor possible"
# (`_context/_prompt_text.py::_CAPABLE_PREAMBLE`). Three roles' rubrics
# structurally cannot be satisfied under that posture: Test Reviewer must
# *run* pytest; Code Quality Reviewer and SysAdmin Reviewer must verify the
# diff against the repo's configured lint/quality gates, which
# `_context/core.py`'s `_load_claude_md_quality_gates` injects into every
# role's prompt as `## Repo Lint Configuration` but which a read-only sandbox
# cannot execute. Each of these roles self-reports ``status="degraded"`` on
# every ticket, forever, for a reason that carries no information about this
# particular diff. `_derive_health` treats this set as a load-bearing
# conditional (it drives control flow), which is why it is a module constant
# rather than inlined literals repeated across `_context/`.
_READ_ONLY_SANDBOX_EXEMPT_ROLES: frozenset[str] = frozenset(
    {"Test Reviewer", "Code Quality Reviewer", "SysAdmin Reviewer"}
)


def _is_environment_muted_degradation(doc: ReviewerFindingsDocument) -> bool:
    """True iff *doc* is one of the read-only-sandbox-exempt roles'
    structurally-forced ``"degraded"``.

    Narrowly scoped to ``(role, status) in (_READ_ONLY_SANDBOX_EXEMPT_ROLES,
    "degraded")`` (#1856, #2174): an exempt-role document that self-reports
    ``"failed"`` instead still downgrades health — only the read-only-sandbox
    "degraded" signal is environment-caused noise, not a stronger failure
    signal.
    """
    return (
        doc.reviewer_role in _READ_ONLY_SANDBOX_EXEMPT_ROLES
        and doc.status == "degraded"
    )


def _has_transient_failure(failures: list[ReviewerRunFailure]) -> bool:
    """True when at least one of *failures* is retry-eligible (#1836).

    Single source of truth for both `synthesize_codex_review_result` blocked
    dispositions (zero-documents and partial-review) that derive
    `Blocker.retry_eligible` from `_TRANSIENT_FAILURE_REASONS` — kept as one
    function so the two branches can't drift on what "transient" means.
    """
    return any(f.reason in _TRANSIENT_FAILURE_REASONS for f in failures)


def _format_failures_detail(
    failures: list[ReviewerRunFailure], *, session_id: str
) -> str:
    """Render *failures* as a short ``role (reason)`` summary for ``details``.

    Appends a pointer to the on-disk diagnostics bundle so an operator reading
    the blocked sentinel knows where the per-role failure artifacts landed.
    """
    summary = "; ".join(f"{f.role} ({f.reason})" for f in failures)
    return append_diagnostics_pointer(summary, session_id=session_id)


# #2094: the only mapping from a reviewer's self-reported status to the
# closed AgentHealthEntry.confidence vocabulary. "ok" -> HIGH mirrors
# _derive_health's own "nothing wrong found" baseline; "degraded" -> MEDIUM
# and "failed" -> LOW mirror the coarser MEDIUM the aggregate gate already
# assigns to any non-"ok" document, refined here per-document instead of
# collapsed to one roster-wide signal.
_STATUS_TO_CONFIDENCE: dict[ReviewerHealthStatus, Literal["HIGH", "MEDIUM", "LOW"]] = {
    "ok": "HIGH",
    "degraded": "MEDIUM",
    "failed": "LOW",
}


def _format_degraded_document_highlights(
    documents: list[ReviewerFindingsDocument], *, session_id: str
) -> list[str]:
    """Render every non-``"ok"`` *documents* entry as a friction highlight.

    One ``f"{role}: {status} — {detail}"`` line per non-``"ok"`` document,
    plus a trailing bare diagnostics-pointer entry (#2094) so an operator's
    next click lands on the per-role documents :func:`_persist_codex_role_document`
    just wrote. Mirrors ``codex_fix_loop._with_snapshot_pointer``'s
    list-append-one-pointer-item shape combined with
    :func:`_format_failures_detail`'s ``append_diagnostics_pointer`` call.

    Empty-case contract: when every document's ``status == "ok"``, this
    returns ``[]`` — no per-document entries and no trailing pointer, so a
    fully clean pass adds zero new ``friction_highlights`` items. A
    read-only-sandbox-exempt role's structurally-forced ``"degraded"``
    document is deliberately NOT excluded here (unlike the aggregate gate in
    :func:`_derive_health`) — it still reports its real status/detail rather
    than being masked, per #2094's non-masking decision.
    """
    highlights = [
        f"{doc.reviewer_role}: {doc.status} — {doc.detail}"
        for doc in documents
        if doc.status != "ok"
    ]
    if not highlights:
        return []
    return [*highlights, append_diagnostics_pointer("", session_id=session_id)]


def _derive_health(documents: list[ReviewerFindingsDocument]) -> Health:
    """Derive the clean-review ``Health`` signal from reviewer document status.

    Reached only after the caller has already established there is no
    MUST_FIX finding and no :class:`ReviewerRunFailure` — i.e. "clean" here
    means "nothing wrong was found," not "full coverage was achieved."
    ``failures`` is deliberately not a parameter: every call site reaches
    this helper only after ``if failures: ...`` has already returned, so
    ``failures == []`` is already an established invariant here.

    Any document whose ``status`` is not ``"ok"`` — a ``degraded`` role that
    could not complete a required check, or a self-reported ``failed``
    document that still parsed — means that role's coverage was reduced even
    though it produced neither a MUST_FIX finding nor a run failure. Reporting
    that as full HIGH-confidence PROCEED would be exactly the "spuriously
    clean sentinel" risk the surrounding disposition logic exists to catch.

    See :func:`~cw.codex_review._verdict._render._render_degraded_roles_note`
    (#1775) for where a degraded role's stated reason
    (``ReviewerRunRecord.detail``) surfaces on the rendered comment — this
    function only derives the health signal, it does not render anything.

    Exception (#1856, widened by #2174): a document from one of
    ``_READ_ONLY_SANDBOX_EXEMPT_ROLES`` (Test Reviewer, Code Quality
    Reviewer, SysAdmin Reviewer) whose ``status`` is specifically
    ``"degraded"`` is excluded from this computation via
    :func:`_is_environment_muted_degradation` — that (role, status) pair is
    the structurally-forced read-only-sandbox tax (none of these roles can
    complete their rubric under codex review's read-only sandbox, on any
    ticket), not a signal about this diff's real coverage. A ``"failed"``
    document from one of these roles is not covered by the exclusion and
    still downgrades health, as does a ``"degraded"`` document from any
    other role.

    ``agent_health_summary`` (#2094) carries one :class:`AgentHealthEntry`
    per *document*, unconditionally — the read-only-sandbox exemption above
    only excludes a document from the aggregate gate computation, it does
    not mask that document's real status/confidence here; masking it would
    silently reintroduce the "which reviewer and why" blindness this ticket
    closes for the one class of degradation that happens on every ticket.
    """
    agent_health_summary = [
        AgentHealthEntry(
            agent_id=doc.reviewer_role,
            confidence=_STATUS_TO_CONFIDENCE[doc.status],
        )
        for doc in documents
    ]
    if any(
        doc.status != "ok" and not _is_environment_muted_degradation(doc)
        for doc in documents
    ):
        return Health(
            lowest_agent_confidence="MEDIUM",
            any_incomplete_risk=True,
            recommendation="EXIT_FOR_HUMAN_REVIEW",
            agent_health_summary=agent_health_summary,
        )
    return Health(
        lowest_agent_confidence="HIGH",
        any_incomplete_risk=False,
        recommendation="PROCEED",
        agent_health_summary=agent_health_summary,
    )
