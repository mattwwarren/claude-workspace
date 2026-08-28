"""The two adjudication data models and the outcome vocabulary (#1805/#1814).

:class:`Adjudication` is one coordinating-session decision;
:class:`VoidedFinding` is one operator-settled rejection recorded durably. They
share nothing but the ``Severity``/``Disposition`` aliases they both borrow
from :mod:`cw.review_findings` — the seams that consume them
(:mod:`~cw.review_adjudication._match`,
:mod:`~cw.review_adjudication._voided`,
:mod:`~cw.review_adjudication._deferred_md`) stay deliberately apart, for the
reason the package docstring records.

Split out of the single ``review_adjudication.py`` module (#2011); import these
names from :mod:`cw.review_adjudication`, not from this private submodule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from cw.review_findings import Disposition, Severity

AdjudicationOutcome = Literal["fix", "reject", "defer", "operator_action"]

# The four genuine, recorded post-adjudication decisions. Every other
# disposition an accepted finding can end at is an absence of decision.
_OUTCOME_DISPOSITIONS: dict[AdjudicationOutcome, Disposition] = {
    "fix": "fixed",
    "reject": "rejected",
    "defer": "deferred",
    "operator_action": "operator_actionable",
}

_MUST_FIX = "MUST_FIX"

#: The one outcome scoped to a single severity (#1817, Decision C2).
_OPERATOR_ACTION: AdjudicationOutcome = "operator_action"


class Adjudication(BaseModel):
    """One coordinating-session decision about one accepted finding.

    ``severity``/``file``/``line_start``/``line_end`` are the identity fields
    matched against :attr:`AcceptedFinding.finding` (see
    :func:`_location_key`). ``reviewers`` is deliberately NOT among them: it is
    the post-dedup merged role list, so it carries no disambiguating power
    beyond what the location key already has.

    ``evidence`` is a same-location tiebreak, not part of the identity key —
    the fragility this ticket exists to fix is precisely verbatim
    evidence-string and off-by-one line mismatches (#1792, #1738, #1715,
    #1743), so requiring an exact evidence match to identify a finding
    reproduces the bug. It still disambiguates the rare case of two distinct
    findings at one location.

    ``summary`` is render-only: :func:`render_deferred_findings_md` fills the
    documented ``"<summary>"`` slot from it, so rendering needs no second
    lookup against the verdict.

    ``outcome="operator_action"`` (#1817) is the OPERATOR ACTIONABLE bucket:
    the session accepts the finding but its remedy is outside this diff, so it
    is posted to the tracker as an operator checklist item rather than fixed,
    rejected, or deferred to PR-body time. It is scoped to ``MUST_FIX``
    severity at the model (Decision C2) — a SHOULD_FIX finding with no diff
    anchor routes through ordinary DEFER — so the enforcement survives a
    prose-only edit of ``auto-dev-review.md``'s bucket rules. Its ``rationale``
    is REQUIRED by the same ``outcome != "fix"`` gate every other non-fix
    outcome goes through, and must name the concrete action the operator needs
    to take rather than restate the finding.

    ``round``/``recorded_at`` (#1840) are the audit context the CLI stamps onto
    the entries one ``cw review adjudicate --deferred-findings-out`` call newly
    applies, so an outcome flip across rounds reads as history rather than as a
    contradiction. Their defaults (``None``/``""``) both mean "not yet stamped
    by the CLI", which covers two cases identically: a brand-new entry that has
    not been through the write path yet, and any entry parsed back out of a
    pre-#1840-shaped artifact. Nothing distinguishes the two, because there is
    nothing to distinguish — a legacy entry is never subsequently stamped.
    Neither field is part of any identity, matching, or dedup key.
    """

    severity: Severity
    file: str
    line_start: int | None = None
    line_end: int | None = None
    evidence: str = ""
    summary: str = ""
    outcome: AdjudicationOutcome
    rationale: str = ""
    round: int | None = None
    recorded_at: str = ""

    @model_validator(mode="after")
    def _check_rationale_recorded(self) -> Adjudication:
        # "Record the rationale" is the adjudication instruction's own rule for
        # REJECT/DEFER (auto-dev-review.md Checkpoint 3a) — a blank one is a
        # contract violation, not a silently-accepted value. A FIX needs none:
        # the fix itself is the record.
        if self.outcome != "fix" and not self.rationale.strip():
            msg = (
                f"an adjudication with outcome={self.outcome!r} must record a "
                "non-empty rationale"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_operator_action_is_must_fix(self) -> Adjudication:
        # #1817 Decision C2: OPERATOR ACTIONABLE is a MUST_FIX-only route.
        # Enforced here rather than only in auto-dev-review.md's bucket prose
        # so a SHOULD_FIX can never silently regain the route through an
        # instruction-file edit — the same reason the rationale gate above is
        # a model validator rather than a documented convention.
        if self.outcome == _OPERATOR_ACTION and self.severity != _MUST_FIX:
            msg = (
                f"an adjudication with outcome={_OPERATOR_ACTION!r} must have "
                f"severity={_MUST_FIX!r} (got {self.severity!r}) — a non-"
                "MUST_FIX finding whose remedy is outside the diff routes "
                "through ordinary DEFER"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_operator_action_has_no_diff_anchor(self) -> Adjudication:
        # Mirrors Finding._check_no_diff_anchor_file_is_na: an operator_action
        # entry is copied straight off a no_diff_anchor Finding, which is
        # itself model-pinned to file="N/A" with no line anchor. Enforcing the
        # same shape here means a coordinating-session typo on this entry's
        # file/line fields fails fast instead of silently missing the
        # location-key match against its Finding (#1817 review, 2026-08-11).
        no_line_anchor = self.line_start is None and self.line_end is None
        if self.outcome == _OPERATOR_ACTION and (
            self.file != "N/A" or not no_line_anchor
        ):
            msg = (
                f"an adjudication with outcome={_OPERATOR_ACTION!r} must have "
                f'file="N/A" and no line anchor (got file={self.file!r}, '
                f"line_start={self.line_start}, line_end={self.line_end})"
            )
            raise ValueError(msg)
        return self


class VoidedFinding(BaseModel):
    """One finding an operator settled as REJECTed, recorded durably (#1814).

    Minted exactly once, by the Claude coordinating session, at the moment
    Checkpoint 3a step 4c correlates a specific operator comment to a specific
    finding — that correlation needs LLM judgment over free-text prose, so a
    codex-only lane can consult existing voids but never create new ones.
    Persisted as a JSON sentinel inside a ticket comment (see
    :func:`render_voided_findings_block`) rather than a ``.cw/`` file, because
    ``.cw/`` state does not survive the worktree teardown of the very
    regress/redispatch cycle this record exists to survive.

    ``severity``/``file``/``summary``/``evidence`` are the identity fields. The
    absence of ``line_start``/``line_end`` is the design, not an omission:
    matching on position is what a re-derived finding's drifted line anchor
    (#1715) would break, and what an unrelated new finding at the same line
    would falsely satisfy. See :func:`_voided_fingerprint` and ADR-0015.

    The remaining fields are provenance, carried so an operator reading a
    suppression can find the settling comment without re-fetching the thread.
    ``voided_at`` may arrive blank from a producer that has no clock handy
    (``cw review check-voided`` stamps it); it is never part of identity.
    """

    severity: Severity
    file: str
    summary: str
    evidence: str
    operator_comment_id: str
    operator_comment_excerpt: str = ""
    voided_at: str = ""
    original_rationale: str = ""

    @field_validator("file", "summary", "evidence", "operator_comment_id")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        # Mirrors Finding._reject_blank: a blank identity field would make the
        # fingerprint match findings it has no business matching, which is the
        # one failure mode this ticket's zero-false-positive stance forbids.
        if not v.strip():
            msg = f"voided-finding field must be non-empty, non-whitespace (got {v!r})"
            raise ValueError(msg)
        return v
