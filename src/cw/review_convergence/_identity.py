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

from pydantic import BaseModel, ConfigDict, Field

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
    # #3039 (RFC 0017 I1): an engine finding's identity is its engine
    # fingerprint. ``None`` (the default, dropped from dumps) is the builtin
    # three-field identity, so a builtin ledger serializes unchanged.
    fingerprint: str | None = Field(default=None, exclude_if=lambda v: v is None)

    def __eq__(self, other: object) -> bool:
        """Fingerprint identity when either side carries one, else three fields.

        An engine identity is severity-independent: a deferred fingerprint that
        re-raises at a different severity is the same finding, and the stored
        severity is what escalation detection compares. A fingerprint identity
        never equals a fingerprint-less one, so a missed threading site fails
        closed rather than silently matching a builtin entry.
        """
        if not isinstance(other, FindingIdentity):
            return NotImplemented
        if self.fingerprint is not None or other.fingerprint is not None:
            return self.fingerprint == other.fingerprint
        return (self.severity, self.file, self.summary_normalized) == (
            other.severity,
            other.file,
            other.summary_normalized,
        )

    def __hash__(self) -> int:
        if self.fingerprint is not None:
            return hash(("engine", self.fingerprint))
        return hash((self.severity, self.file, self.summary_normalized))

    def audit_key(self) -> str:
        """The stable audit key, ``engine::<fingerprint>`` or ``<sev>::<file>::<text>``.

        Severity-aware (``cw review settle``'s key has none), so two identities
        that differ only by severity never collapse (R21). An engine identity
        is keyed by its fingerprint alone.
        """
        if self.fingerprint is not None:
            return f"engine::{self.fingerprint}"
        return f"{self.severity}::{self.file}::{self.summary_normalized}"


def finding_identity(
    severity: Severity, file: str, summary: str, fingerprint: str | None = None
) -> FindingIdentity:
    """Build the identity of a finding, normalizing *summary*'s whitespace."""
    return FindingIdentity(
        severity=severity,
        file=file,
        summary_normalized=_normalize_finding_text(summary),
        fingerprint=fingerprint,
    )
