"""The head-pinned review sign-off attestation on a dev-queue row (RFC 0017 II1).

One ``ReviewAttestation`` records that a review exited clean at exactly one
commit, ``head_sha``. cw writes it (``cw.dev_queue.review_attestation``),
clears it in lockstep with the review approval
(``cw.dev_queue.lifecycle._clear_review_approval``), serializes it into
``queue_metadata.review_attestation`` and verifies it with
``cw review attestation check``. The contract is ``docs/headless-contract.md``
section 13.

Row-state models are lenient (#1200): a stored attestation from a newer or
older cw still loads. That is unlike S1's strict ``ReviewEngineSummary``,
whose fields are copied into the sub-models here rather than embedded.

``counts`` always means unresolved, non-deferred findings outstanding at
``head_sha``. ``found_during_review`` is informational only. ``deferrals`` and
``findings`` are ``None`` ("not reported") when no ledger was read.

Imports only pydantic and the stdlib, so it is a DAG root of ``cw.models``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

REVIEW_ATTESTATION_SCHEMA_VERSION = 1
"""The attestation's own schema version, independent of S1's summary version."""

REVIEW_ATTESTATION_KEY = "review_attestation"
"""The ``queue_metadata`` wire key carrying the row's attestation (cw-context v20)."""

MAX_ATTESTATION_BYTES = 64 * 1024
"""Maximum UTF-8 byte length of one serialized attestation."""

MAX_ATTESTATION_FINDINGS = 200
"""Maximum number of entries in an attestation's ``findings`` digest."""

ATTESTATION_OVERSIZED = "attestation_oversized"
"""The reason token of an attestation refused for exceeding a bound."""

HEAD_SHA_PATTERN = r"^[0-9a-f]{40}$"
"""A full 40-hex lowercase commit id, the only accepted ``head_sha`` shape."""

ReviewerBackend = Literal["claude-native", "codex", "opencode"]
AttestationVerdictValue = Literal["PASS", "FAIL"]
AttestationRisk = Literal["LOW", "MEDIUM", "HIGH"]
FindingDispositionValue = Literal["fixed", "rejected", "deferred", "open"]
OverrideKind = Literal["must_fix_override", "operator_settle", "finding_disposition"]

_NO_LINE = -1


class AttestationCounts(BaseModel):
    """Unresolved, non-deferred findings outstanding at ``head_sha``.

    Ship gates check ``must_fix == 0 and should_fix == 0``. On an engine-less
    attestation ``debt``, ``nit`` and ``principle`` are 0 meaning "not
    measured".
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    must_fix: int = 0
    should_fix: int = 0
    debt: int = 0
    nit: int = 0
    principle: int = 0


class FoundDuringReview(BaseModel):
    """What the review loop found on its way to clean; informational only.

    The sentinel's initial counts, never outstanding findings and never a
    ship-gate input.
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    must_fix: int
    should_fix: int


class AttestationAcceptance(BaseModel):
    """One engine acceptance row: the criterion id and whether it was met."""

    # NOT extra=forbid - persisted/runtime state, see #1200
    id: str
    status: Literal["PASS", "FAIL", "NOT_APPLICABLE"]


class AttestationGateSignal(BaseModel):
    """One engine gate signal: the gate name and its outcome."""

    # NOT extra=forbid - persisted/runtime state, see #1200
    name: str
    status: Literal["PASS", "FAIL", "WARN"]


class AttestationDeferral(BaseModel):
    """One finding deferred out of the review, not counted as outstanding.

    ``fingerprint`` is an engine fingerprint, ``identity`` the ledger's
    ``FindingIdentity.audit_key()``; the builder sets exactly one.
    ``ticket_id`` and ``ticket_url`` stay null until III1 (#3041).
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    fingerprint: str | None = None
    identity: str | None = None
    severity: str
    rule_id: str | None = None
    file: str | None = None
    line: int | None = None
    ticket_id: str | None = None
    ticket_url: str | None = None


class AttestationOverride(BaseModel):
    """One operator override in force when the attestation was written."""

    # NOT extra=forbid - persisted/runtime state, see #1200
    kind: OverrideKind
    actor: str
    reason: str
    recorded_at: str


class AttestationFinding(BaseModel):
    """One entry of the bounded ledger findings digest (no evidence text)."""

    # NOT extra=forbid - persisted/runtime state, see #1200
    fingerprint: str | None = None
    identity: str | None = None
    severity: str
    rule_id: str | None = None
    file: str | None = None
    line: int | None = None
    disposition: FindingDispositionValue


class ReviewAttestation(BaseModel):
    """A clean review exit pinned to one commit (RFC 0017 II1)."""

    # NOT extra=forbid - persisted/runtime state, see #1200
    schema_version: int = REVIEW_ATTESTATION_SCHEMA_VERSION
    ticket_id: str
    client: str
    head_sha: str = Field(pattern=HEAD_SHA_PATTERN)
    written_at: datetime
    engine: str | None = None
    reviewer_backend: ReviewerBackend
    verdict: AttestationVerdictValue
    counts: AttestationCounts
    found_during_review: FoundDuringReview | None = None
    acceptance: list[AttestationAcceptance] | None = None
    risk: AttestationRisk | None = None
    gate_signals: list[AttestationGateSignal] | None = None
    deferrals: list[AttestationDeferral] | None = None
    overrides: list[AttestationOverride] = Field(default_factory=list)
    findings: list[AttestationFinding] | None = None

    @field_validator("written_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        """Reject a naive timestamp and normalize an aware one to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "written_at must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)


def _anchored_key(
    entry: AttestationDeferral | AttestationFinding,
) -> tuple[str, str, int, str, str]:
    """``(fingerprint or identity, file, line, rule_id, severity)``, ``None`` low."""
    return (
        entry.fingerprint or entry.identity or "",
        entry.file or "",
        entry.line if entry.line is not None else _NO_LINE,
        entry.rule_id or "",
        entry.severity,
    )


def _override_key(entry: AttestationOverride) -> tuple[str, str, str, str]:
    return (entry.recorded_at, entry.kind, entry.actor, entry.reason)


def sorted_for_wire(att: ReviewAttestation) -> ReviewAttestation:
    """Return *att* with every list in its deterministic wire order.

    Applied once by the builder before the row is stored, so the row,
    cw-context and the golden fixture are byte-identical for any input order.
    ``None`` lists stay ``None``.
    """
    update: dict[str, object] = {
        "overrides": sorted(att.overrides, key=_override_key),
    }
    if att.deferrals is not None:
        update["deferrals"] = sorted(att.deferrals, key=_anchored_key)
    if att.findings is not None:
        update["findings"] = sorted(att.findings, key=_anchored_key)
    if att.acceptance is not None:
        update["acceptance"] = sorted(att.acceptance, key=lambda a: (a.id, a.status))
    if att.gate_signals is not None:
        update["gate_signals"] = sorted(
            att.gate_signals, key=lambda g: (g.name, g.status)
        )
    return att.model_copy(update=update)


def attestation_size_bytes(att: ReviewAttestation) -> int:
    """The UTF-8 byte length of *att*'s JSON serialization."""
    return len(att.model_dump_json().encode("utf-8"))


def attestation_finding_count(att: ReviewAttestation) -> int:
    """The number of ``findings`` entries, 0 when not reported."""
    return len(att.findings) if att.findings is not None else 0


def attestation_size_ok(att: ReviewAttestation) -> bool:
    """True when *att* is within both ``MAX_ATTESTATION_*`` bounds."""
    return (
        attestation_finding_count(att) <= MAX_ATTESTATION_FINDINGS
        and attestation_size_bytes(att) <= MAX_ATTESTATION_BYTES
    )
