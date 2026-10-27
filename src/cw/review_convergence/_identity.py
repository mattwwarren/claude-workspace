"""Finding identity for the converge-to-clean ledger (#2787).

A finding's identity is ``(severity, file, whitespace-normalized summary)``.
It is the voided matcher's 4-tuple (``cw.review_adjudication._voided
._voided_fingerprint``) minus ``evidence``, on purpose: a fix rewrites the
code, so the evidence quote a reviewer re-derives after a fix differs from the
one it derived before. Oscillation ("fixed, then back") would be undetectable
if evidence were part of the key, and so would an operator settle for a
recurring false positive: the voided matcher's own docstring says its void
"lapses" once the code is rewritten, which is exactly the case a recurrence is
in.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from cw.review_adjudication._voided import _normalize_finding_text
from cw.review_findings import Severity


class FindingIdentity(BaseModel):
    """The content anchor of one finding: severity, file, normalized summary.

    Frozen (and therefore hashable) so it keys dicts and sets directly.
    ``file`` is compared verbatim, including the ``"N/A"`` literal of a finding
    with no diff anchor: identity is plain text and never goes through
    ``_disposition_key``, which returns ``None`` for ``N/A``.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    file: str
    summary_normalized: str

    def audit_key(self) -> str:
        """The stable ``<severity>::<file>::<summary>`` key of an audit event.

        Severity-aware (``cw review settle``'s key has none), so two identities
        that differ only by severity never collapse (R21).
        """
        return f"{self.severity}::{self.file}::{self.summary_normalized}"


def finding_identity(severity: Severity, file: str, summary: str) -> FindingIdentity:
    """Build the identity of a finding, normalizing *summary*'s whitespace."""
    return FindingIdentity(
        severity=severity,
        file=file,
        summary_normalized=_normalize_finding_text(summary),
    )
