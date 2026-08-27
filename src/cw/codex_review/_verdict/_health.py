"""Reviewer-roster health signals derived from per-role document status.

The clean-review ``Health`` derivation and the two failure predicates the
synthesis disposition table reads: whether any run failure is retry-eligible,
and how a reviewer's own ``status`` maps onto reduced coverage — including the
one structurally-forced degradation that must not count as a signal about the
diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import Health
from cw.codex_review._const import _TRANSIENT_FAILURE_REASONS
from cw.executor_diagnostics import append_diagnostics_pointer

if TYPE_CHECKING:
    from cw.review_findings import ReviewerFindingsDocument, ReviewerRunFailure

# #1856: the codex-review sandbox is unconditionally read-only for every
# reviewer role (`_roles.py::_build_generic_codex_argv`, MUST_FIX 4 from
# #1236) and the prompt tells the model outright that write access "is
# neither offered nor possible" (`_context/_prompt_text.py::_CAPABLE_PREAMBLE`).
# Test Reviewer is the one role whose job needs to *run* pytest, which a
# read-only sandbox structurally cannot do — so it self-reports
# ``status="degraded"`` on every ticket, forever, for a reason that carries
# no information about this particular diff. `_derive_health` treats that
# name as a load-bearing conditional (it drives control flow), which is why
# it is a module constant rather than a fifth inlined copy of the literal
# already repeated across `_context/`.
_TEST_REVIEWER_ROLE = "Test Reviewer"


def _is_environment_muted_degradation(doc: ReviewerFindingsDocument) -> bool:
    """True iff *doc* is Test Reviewer's structurally-forced ``"degraded"``.

    Narrowly scoped to ``(role, status) == (_TEST_REVIEWER_ROLE, "degraded")``
    (#1856): a Test Reviewer document that self-reports ``"failed"`` instead
    still downgrades health — only the read-only-sandbox "degraded" signal is
    environment-caused noise, not a stronger failure signal.
    """
    return doc.reviewer_role == _TEST_REVIEWER_ROLE and doc.status == "degraded"


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

    Exception (#1856): a Test Reviewer document whose ``status`` is
    specifically ``"degraded"`` is excluded from this computation via
    :func:`_is_environment_muted_degradation` — that (role, status) pair is
    the structurally-forced read-only-sandbox tax (Test Reviewer can never
    start pytest under codex review's read-only sandbox, on any ticket), not
    a signal about this diff's real coverage. A Test Reviewer ``"failed"``
    document is not covered by the exclusion and still downgrades health, as
    does a ``"degraded"`` document from any other role.
    """
    if any(
        doc.status != "ok" and not _is_environment_muted_degradation(doc)
        for doc in documents
    ):
        return Health(
            lowest_agent_confidence="MEDIUM",
            any_incomplete_risk=True,
            recommendation="EXIT_FOR_HUMAN_REVIEW",
        )
    return Health(
        lowest_agent_confidence="HIGH",
        any_incomplete_risk=False,
        recommendation="PROCEED",
    )
