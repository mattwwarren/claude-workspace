"""The git-free builder of the review sign-off attestation (#3040, RFC 0017 II1).

``record_review_attestation`` is one of the two writers of
``TicketTask.review_attestation`` (the other, the clear, is
``lifecycle._clear_review_approval``). It is called from the REVIEW-stage
clean auto-advance (``dispatch.routing._stage_advance_unchecked``, through
:func:`attest_review_exit`) and from ``lifecycle._record_review_approval``.

ADR-0019: both call sites can run with ``sessions_lock`` held, so this module
runs no git, no ``gh`` and no subprocess, and takes no lock. It reads files
only: the convergence ledger and the engine round's summary, both in
:func:`attest_review_exit` (#3108 wires the summary into the one
``engine_summary`` argument). ``head_sha`` is the sentinel's
``review.reviewed_sha``; comparing it with the live HEAD is the verifier's job
(``cw review attestation check``). Its only side effects beyond the row are
``record_event`` and logging.

The deferrals' ticket links (#3094) are filled from ``TicketTask.review_debt``
at build time (:func:`fill_deferral_tickets`) and refreshed whenever a debt
record lands (:func:`refresh_deferral_tickets`, called from
``cw.dev_queue.review_debt_records``). Both are pure over the row, so the
builder stays git-free.

A skipped write logs one INFO line, ``review attestation skipped:
reason=<token> ticket=<id> client=<name>``, and nothing else. The tokens are
the ``SKIP_*`` constants; ``docs/headless-contract.md`` section 13 lists them.
A downgraded write (an engine-configured claude-native client whose round left
no usable summary at the reviewed head) is stored ledger-only and logs one
``review attestation engine evidence unavailable`` INFO line; that line is not
a skip token.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from cw.auto_dev_result import STAGE_SUCCESS_STATUSES
from cw.events import record_event
from cw.models import OrchestratorEventType, Stage
from cw.models.review_attestation import (
    ATTESTATION_OVERSIZED,
    HEAD_SHA_PATTERN,
    MAX_ATTESTATION_BYTES,
    MAX_ATTESTATION_FINDINGS,
    AttestationAcceptance,
    AttestationCounts,
    AttestationDeferral,
    AttestationFinding,
    AttestationGateSignal,
    AttestationOverride,
    FindingDispositionValue,
    FoundDuringReview,
    ReviewAttestation,
    ReviewerBackend,
    attestation_finding_count,
    attestation_size_bytes,
    sorted_for_wire,
)
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    finalize_ready,
    load_ledger,
)
from cw.review_engine_summary_io import find_round_summary_for_head
from cw.review_finding_dispositions import debt_key
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.models.client import ReviewConfig
    from cw.models.review_debt_record import ReviewDebtRecord
    from cw.models.review_engine_summary import ReviewEngineSummary
    from cw.review_convergence import (
        ConvergenceLedger,
        FindingIdentity,
    )

_log = logging.getLogger(__name__)

SKIP_REFRESH_NO_ATTESTATION: Final = "refresh_no_attestation"
SKIP_REFRESH_UNCHANGED: Final = "refresh_unchanged"
SKIP_NO_CLIENT: Final = "no_client"
SKIP_NOT_SUCCESS: Final = "not_success"
SKIP_HEALTH_DEGRADED: Final = "health_degraded"
SKIP_MISSING_REVIEWED_SHA: Final = "missing_reviewed_sha"
SKIP_BAD_REVIEWED_SHA: Final = "bad_reviewed_sha"
SKIP_UNSUPPORTED_BACKEND: Final = "unsupported_backend"
SKIP_LEDGER_ABSENT: Final = "ledger_absent"
SKIP_LEDGER_DISAGREES: Final = "ledger_disagrees"
SKIP_NO_ENGINE_CONFIG: Final = "no_engine_config"
SKIP_ENGINE_MISMATCH: Final = "engine_mismatch"
SKIP_INVALID_MODEL: Final = "invalid_model"
SKIP_WEAKER_THAN_STORED: Final = "weaker_than_stored"

_REVIEW_PENDING_APPROVAL: Final = "review_pending_approval"
_ENGINE_CLEAN_VERDICT: Final = "PASS"
_ENGINE_COMPLETE: Final = "COMPLETE"
_CLAUDE_NATIVE: Final = "claude-native"
_HEAD_RE: Final = re.compile(HEAD_SHA_PATTERN)
#: The executor backends an attestation may name, keyed by the config value.
_REVIEWER_BACKENDS: Final[dict[str, ReviewerBackend]] = {
    "claude-native": "claude-native",
    "codex": "codex",
    "opencode": "opencode",
}
_REVERSED: Final = "REVERSED"
_SETTLE_DISPOSITION: Final[dict[str, FindingDispositionValue]] = {
    "reject": "rejected",
    "defer": "deferred",
}


def attest_review_exit(
    task: TicketTask,
    *,
    last_result: dict[str, object] | None,
    client: ClientConfig | None,
) -> ReviewAttestation | None:
    """Attest a REVIEW exit of *task*, reading its ledger when clean mode.

    The one place in this module that touches the filesystem. The worktree is
    resolved with ``verify_checkout=False`` (no ``git branch`` probe): the
    ledger authenticates itself by ticket id and clean sha. A blocking-mode
    client reads no ledger. On an engine-configured claude-native client the
    engine round's summary is found by the reviewed head (the call sites have
    no round id) and handed to the builder; see :func:`_engine_summary_for_exit`.
    """
    ledger = None
    if client is not None and client.review.is_clean:
        ledger = _load_task_ledger(task, client)
    return record_review_attestation(
        task,
        last_result=last_result,
        engine_summary=_engine_summary_for_exit(task, client, last_result, ledger),
        ledger=ledger,
        client=client,
    )


def _task_worktree(task: TicketTask, client: ClientConfig) -> Path | None:
    """The task's worktree without a ``git branch`` probe (ADR-0019)."""
    return resolve_task_worktree(task, client, verify_checkout=False)


def _load_task_ledger(
    task: TicketTask, client: ClientConfig
) -> ConvergenceLedger | None:
    worktree = _task_worktree(task, client)
    if worktree is None:
        return None
    try:
        return load_ledger(worktree / LEDGER_RELATIVE_PATH)
    except (OSError, ValueError):
        return None


def _engine_scan_head(
    task: TicketTask,
    client: ClientConfig | None,
    last_result: dict[str, object] | None,
    ledger: ConvergenceLedger | None,
) -> str | None:
    """The clean head to scan engine rounds for, or None when no scan applies.

    Every check that could skip the write for another reason runs first, so
    the scan never changes which skip token a call reports. The engine lane is
    claude-native only: a codex or opencode round never writes the summary
    file this scan reads.
    """
    if client is None or client.review.engine is None or ledger is None:
        return None
    head = _clean_head(last_result)
    if isinstance(head, _Skip):
        return None
    if _backend(task, client) != _CLAUDE_NATIVE:
        return None
    return head if _ledger_agrees(ledger, task, head) else None


def _engine_summary_for_exit(
    task: TicketTask,
    client: ClientConfig | None,
    last_result: dict[str, object] | None,
    ledger: ConvergenceLedger | None,
) -> ReviewEngineSummary | None:
    """The engine round's summary at the reviewed head, or None.

    None with the scan gate passed means no usable evidence (no worktree, no
    engine directory, no summary at this head, only invalid files): the caller
    then writes a ledger-only attestation and this logs one INFO line. That is
    a downgrade, not a skip, so it has no ``SKIP_*`` token.
    """
    head = _engine_scan_head(task, client, last_result, ledger)
    if head is None or client is None:
        return None
    worktree = _task_worktree(task, client)
    summary = (
        find_round_summary_for_head(worktree, head) if worktree is not None else None
    )
    if summary is None:
        _log.info(
            "review attestation engine evidence unavailable: ticket=%s client=%s",
            task.ticket_id,
            task.client,
        )
    return summary


def _engine_label(
    review: ReviewConfig,
    backend: ReviewerBackend,
    summary: ReviewEngineSummary | None,
) -> str | None:
    """The attestation's ``engine`` label: the skill, only with evidence behind it.

    A claude-native attestation names the engine only when a summary backed it.
    A codex or opencode attestation keeps the configured skill as its label.
    """
    if review.engine is None:
        return None
    if summary is None and backend == _CLAUDE_NATIVE:
        return None
    return review.engine.skill


def record_review_attestation(
    task: TicketTask,
    *,
    last_result: dict[str, object] | None,
    engine_summary: ReviewEngineSummary | None,
    ledger: ConvergenceLedger | None,
    client: ClientConfig | None,
    now: datetime | None = None,
    refresh_only: bool = False,
) -> ReviewAttestation | None:
    """Build, check and store *task*'s attestation; return it, or None.

    ``refresh_only`` (the override re-stamp and the review-less release) only
    rebuilds ``overrides`` on an existing attestation at the approval's sha and
    never creates one. Otherwise an attestation is written only on a clean
    exit (Contract Semantics item 1); every other outcome writes nothing and
    logs one skip line, or, on overflow, a warning and a rejected event.
    """
    if refresh_only:
        return _refresh_overrides(task)
    checked = _check_clean_exit(task, last_result, engine_summary, ledger, client)
    if isinstance(checked, _Skip):
        _skip(task, checked.reason)
        return None
    head, backend, clean_client = checked
    review = clean_client.review
    att = _assemble(
        task,
        head=head,
        backend=backend,
        last_result=last_result,
        engine_summary=engine_summary,
        engine=_engine_label(review, backend, engine_summary),
        ledger=ledger if review.is_clean else None,
        now=now or datetime.now(UTC),
    )
    if att is None:
        _skip(task, SKIP_INVALID_MODEL)
        return None
    if not _stronger_or_equal(att, task.review_attestation):
        _skip(task, SKIP_WEAKER_THAN_STORED)
        return None
    return _store(task, att, refresh=False)


def _check_clean_exit(
    task: TicketTask,
    last_result: dict[str, object] | None,
    engine_summary: ReviewEngineSummary | None,
    ledger: ConvergenceLedger | None,
    client: ClientConfig | None,
) -> tuple[str, ReviewerBackend, ClientConfig] | _Skip:
    """``(head, backend, client)`` on a clean exit, else the skip reason."""
    if client is None:
        return _Skip(SKIP_NO_CLIENT)
    head = _clean_head(last_result)
    if isinstance(head, _Skip):
        return head
    backend = _backend(task, client)
    if backend is None:
        return _Skip(SKIP_UNSUPPORTED_BACKEND)
    reason = _evidence_disagreement(task, client, head, ledger, engine_summary)
    if reason is not None:
        return _Skip(reason)
    return head, backend, client


def _evidence_disagreement(
    task: TicketTask,
    client: ClientConfig,
    head: str,
    ledger: ConvergenceLedger | None,
    engine_summary: ReviewEngineSummary | None,
) -> str | None:
    """The skip reason when the ledger or the engine summary disagrees, else None."""
    if client.review.is_clean:
        if ledger is None:
            return SKIP_LEDGER_ABSENT
        if not _ledger_agrees(ledger, task, head):
            return SKIP_LEDGER_DISAGREES
    if engine_summary is None:
        return None
    if client.review.engine is None:
        return SKIP_NO_ENGINE_CONFIG
    if not _engine_agrees(engine_summary, head):
        return SKIP_ENGINE_MISMATCH
    return None


class _Skip:
    """A skip reason returned in place of a head sha."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _clean_head(last_result: dict[str, object] | None) -> str | _Skip:
    """The sentinel's 40-hex ``reviewed_sha`` on a clean exit, else a skip."""
    from cw.dispatch.review_gates import (
        _resolve_review_reviewed_sha,
        _should_gate_for_review_health,
    )

    status = last_result.get("status") if last_result is not None else None
    if status not in STAGE_SUCCESS_STATUSES and status != _REVIEW_PENDING_APPROVAL:
        return _Skip(SKIP_NOT_SUCCESS)
    if _should_gate_for_review_health(last_result):
        return _Skip(SKIP_HEALTH_DEGRADED)
    sha = _resolve_review_reviewed_sha(last_result)
    if sha is None:
        return _Skip(SKIP_MISSING_REVIEWED_SHA)
    if _HEAD_RE.fullmatch(sha) is None:
        return _Skip(SKIP_BAD_REVIEWED_SHA)
    return sha


def _backend(task: TicketTask, client: ClientConfig) -> ReviewerBackend | None:
    """The REVIEW executor backend, when it is one an attestation may name."""
    from cw.executor import resolve_executor_config

    backend = resolve_executor_config(Stage.REVIEW, task, client).backend
    return _REVIEWER_BACKENDS.get(backend)


def _ledger_agrees(ledger: ConvergenceLedger, task: TicketTask, head: str) -> bool:
    return (
        ledger.ticket_id == task.ticket_id
        and finalize_ready(ledger)
        and ledger.last_clean_sha == head
    )


def _engine_agrees(summary: ReviewEngineSummary, head: str) -> bool:
    return (
        summary.head_sha == head
        and summary.coverage == _ENGINE_COMPLETE
        and summary.verdict == _ENGINE_CLEAN_VERDICT
    )


def _counts(summary: ReviewEngineSummary | None) -> AttestationCounts:
    """Outstanding at ``head_sha``: 0/0 engine-less, ``summary.counts`` engine."""
    if summary is None:
        return AttestationCounts()
    return AttestationCounts.model_validate(summary.counts.model_dump())


def _count_value(review: dict[object, object], key: str) -> int | None:
    value = review.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _found_during_review(
    last_result: dict[str, object] | None, summary: ReviewEngineSummary | None
) -> FoundDuringReview | None:
    """The sentinel's initial find counts, or None when unknown or engine-backed."""
    if summary is not None or last_result is None:
        return None
    review = last_result.get("review")
    if not isinstance(review, dict):
        return None
    must_fix = _count_value(review, "must_fix_initial")
    should_fix = _count_value(review, "should_fix")
    if must_fix is None or should_fix is None:
        return None
    return FoundDuringReview(must_fix=must_fix, should_fix=should_fix)


def _anchor(identity: FindingIdentity) -> dict[str, object]:
    """The digest anchor: the engine fingerprint, else the ledger audit key."""
    fingerprint = identity.fingerprint
    return {
        "fingerprint": fingerprint,
        "identity": None if fingerprint is not None else identity.audit_key(),
        "severity": identity.severity,
        "file": identity.file,
    }


def _digest_from_ledger(
    ledger: ConvergenceLedger,
) -> tuple[list[AttestationFinding], list[AttestationDeferral]]:
    """The bounded findings digest and the ``defer`` entries; no evidence text."""
    findings = [
        AttestationFinding.model_validate(
            {**_anchor(entry.identity), "disposition": "fixed"}
        )
        for entry in ledger.fixed
    ]
    deferrals: list[AttestationDeferral] = []
    for entry in ledger.settled:
        disposition = _SETTLE_DISPOSITION[entry.outcome]
        findings.append(
            AttestationFinding.model_validate(
                {**_anchor(entry.identity), "disposition": disposition}
            )
        )
        if entry.outcome == "defer":
            deferrals.append(
                AttestationDeferral.model_validate(
                    {**_anchor(entry.identity), "debt_key": entry.debt_key}
                )
            )
    return findings, deferrals


def _candidate_keys(deferral: AttestationDeferral) -> list[str]:
    """The debt record keys *deferral* may match, most specific first (#3094).

    The ledger-stored built-in key is primary. ``engine:<fingerprint>`` is a
    secondary candidate, so the join survives if filing later threads
    fingerprints through. An unsafe fingerprint yields no engine candidate.
    """
    keys: list[str] = []
    if deferral.debt_key is not None:
        keys.append(deferral.debt_key)
    if deferral.fingerprint is not None:
        with contextlib.suppress(ValueError):
            keys.append(debt_key("", "", fingerprint=deferral.fingerprint))
    return keys


def _filled_deferral(
    deferral: AttestationDeferral, by_key: dict[str, ReviewDebtRecord]
) -> AttestationDeferral:
    """*deferral* with its ticket link copied from a matching record, else as is."""
    for key in _candidate_keys(deferral):
        record = by_key.get(key)
        if record is not None:
            return deferral.model_copy(
                update={"ticket_id": record.ticket_id, "ticket_url": record.ticket_url}
            )
    return deferral


def fill_deferral_tickets(
    deferrals: Sequence[AttestationDeferral], records: Sequence[ReviewDebtRecord]
) -> list[AttestationDeferral]:
    """*deferrals* with ticket links copied from the debt *records* (#3094).

    A record with no ticket id yet (``emitted``) never fills, so a deferral
    stays null until its ticket exists. A miss returns the deferral untouched,
    so a refresh never blanks an existing link. Pure and idempotent: the same
    inputs always give equal output.
    """
    by_key = {record.key: record for record in records if record.ticket_id is not None}
    return [_filled_deferral(deferral, by_key) for deferral in deferrals]


def _utc_text(value: datetime | str | None) -> str:
    """The UTC ``isoformat()`` of a timestamp, ``""`` when it does not parse."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _overrides_from_row(task: TicketTask) -> list[AttestationOverride]:
    """``must_fix_override`` and non-``REVERSED`` finding dispositions."""
    overrides: list[AttestationOverride] = []
    override = task.must_fix_override
    if override is not None:
        overrides.append(
            AttestationOverride(
                kind="must_fix_override",
                actor=override.actor,
                reason=override.reason,
                recorded_at=_utc_text(override.recorded_at),
            )
        )
    overrides.extend(
        AttestationOverride(
            kind="finding_disposition",
            actor=disposition.actor,
            reason=disposition.rationale,
            recorded_at=_utc_text(disposition.recorded_at),
        )
        for disposition in task.finding_dispositions.values()
        if disposition.outcome != _REVERSED
    )
    return overrides


def _overrides_from_ledger(ledger: ConvergenceLedger) -> list[AttestationOverride]:
    """Operator-origin ledger settles (clean mode only)."""
    return [
        AttestationOverride(
            kind="operator_settle",
            actor=entry.actor or "",
            reason=entry.rationale,
            recorded_at=_utc_text(entry.settled_at),
        )
        for entry in ledger.settled
        if entry.origin == "operator"
    ]


def _engine_fields(summary: ReviewEngineSummary | None) -> dict[str, object]:
    if summary is None:
        return {"acceptance": None, "risk": None, "gate_signals": None}
    return {
        "acceptance": [
            AttestationAcceptance(id=row.id, status=row.status)
            for row in summary.acceptance
        ],
        "risk": summary.risk,
        "gate_signals": [
            AttestationGateSignal(name=signal.name, status=signal.status)
            for signal in summary.gate_signals
        ],
    }


def _assemble(
    task: TicketTask,
    *,
    head: str,
    backend: ReviewerBackend,
    last_result: dict[str, object] | None,
    engine_summary: ReviewEngineSummary | None,
    engine: str | None,
    ledger: ConvergenceLedger | None,
    now: datetime,
) -> ReviewAttestation | None:
    """The validated, wire-ordered attestation, or None on a validation failure."""
    overrides = _overrides_from_row(task)
    findings: list[AttestationFinding] | None = None
    deferrals: list[AttestationDeferral] | None = None
    if ledger is not None:
        findings, deferrals = _digest_from_ledger(ledger)
        deferrals = fill_deferral_tickets(deferrals, task.review_debt)
        overrides.extend(_overrides_from_ledger(ledger))
    fields: dict[str, object] = {
        "ticket_id": task.ticket_id,
        "client": task.client,
        "head_sha": head,
        "written_at": now,
        "engine": engine,
        "reviewer_backend": backend,
        "verdict": "PASS",
        "counts": _counts(engine_summary),
        "found_during_review": _found_during_review(last_result, engine_summary),
        "deferrals": deferrals,
        "overrides": overrides,
        "findings": findings,
        **_engine_fields(engine_summary),
    }
    try:
        return sorted_for_wire(ReviewAttestation.model_validate(fields))
    except ValidationError:
        return None


def _stronger_or_equal(
    new: ReviewAttestation, stored: ReviewAttestation | None
) -> bool:
    """The strength rule: engine-backed is never replaced by engine-less, same head."""
    if stored is None or stored.head_sha != new.head_sha:
        return True
    return not (stored.engine is not None and new.engine is None)


def _refresh_overrides(task: TicketTask) -> ReviewAttestation | None:
    """Rebuild ``overrides`` on an attestation at the approval's sha."""
    stored = task.review_attestation
    if stored is None or stored.head_sha != task.review_approved_sha:
        _skip(task, SKIP_REFRESH_NO_ATTESTATION)
        return None
    # The ledger is not read here: its operator settles are kept as stored.
    kept = [entry for entry in stored.overrides if entry.kind == "operator_settle"]
    refreshed = sorted_for_wire(
        stored.model_copy(update={"overrides": [*_overrides_from_row(task), *kept]})
    )
    if refreshed.overrides == stored.overrides:
        _skip(task, SKIP_REFRESH_UNCHANGED)
        return None
    return _store(task, refreshed, refresh=True)


def refresh_deferral_tickets(task: TicketTask) -> ReviewAttestation | None:
    """Re-fill the stored deferrals' ticket links from ``task.review_debt`` (#3094).

    Called under the dev-queue lock right after a debt record lands, so the
    link reaches the row (and the next spawn's cw-context) without a re-review.
    Silent when there is nothing to do: no attestation, no deferrals, or no
    change. ``written_at``, ``head_sha`` and the strength rule are untouched, and
    the head is not gated on ``review_approved_sha``: links are keyed by finding,
    not by commit.
    """
    stored = task.review_attestation
    if stored is None or not stored.deferrals:
        return None
    filled = fill_deferral_tickets(stored.deferrals, task.review_debt)
    if filled == stored.deferrals:
        return None
    refreshed = sorted_for_wire(stored.model_copy(update={"deferrals": filled}))
    return _store(task, refreshed, refresh=True)


def _within_bounds(task: TicketTask, att: ReviewAttestation) -> bool:
    """True within the bounds; else warn and emit the rejected event."""
    finding_count = attestation_finding_count(att)
    size_bytes = attestation_size_bytes(att)
    if (
        finding_count <= MAX_ATTESTATION_FINDINGS
        and size_bytes <= MAX_ATTESTATION_BYTES
    ):
        return True
    _log.warning(
        "review attestation rejected: reason=%s ticket=%s client=%s head=%s "
        "findings=%d bytes=%d",
        ATTESTATION_OVERSIZED,
        task.ticket_id,
        task.client,
        att.head_sha,
        finding_count,
        size_bytes,
    )
    record_event(
        OrchestratorEventType.REVIEW_ATTESTATION_REJECTED,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "head_sha": att.head_sha,
            "reason": ATTESTATION_OVERSIZED,
            "finding_count": finding_count,
            "size_bytes": size_bytes,
            "max_findings": MAX_ATTESTATION_FINDINGS,
            "max_bytes": MAX_ATTESTATION_BYTES,
        },
        correlation_id=task.ticket_id,
    )
    return False


def _store(
    task: TicketTask, att: ReviewAttestation, *, refresh: bool
) -> ReviewAttestation | None:
    """Store *att* on the row and emit ``review.attestation_recorded``."""
    if not _within_bounds(task, att):
        return None
    task.review_attestation = att
    record_event(
        OrchestratorEventType.REVIEW_ATTESTATION_RECORDED,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "head_sha": att.head_sha,
            "engine": att.engine,
            "reviewer_backend": att.reviewer_backend,
            "counts": att.counts.model_dump(),
            "verdict": att.verdict,
            "refresh": refresh,
        },
        correlation_id=task.ticket_id,
    )
    return att


def _skip(task: TicketTask, reason: str) -> None:
    """Log the one skip line for *reason* and write nothing."""
    _log.info(
        "review attestation skipped: reason=%s ticket=%s client=%s",
        reason,
        task.ticket_id,
        task.client,
    )
