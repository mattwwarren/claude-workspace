"""Release a regressed task's unticketed review defers (#3137).

A codex or opencode SHOULD_FIX defer settles on the convergence ledger as
``outcome="defer"``. When the finding was never filed (auto-create off, a
tracker still pending, a filing failure), that settled defer and its artifact
entry outlive the fix that made the finding go away. Its finding is then
re-recorded as a defer, not re-opened, so the attestation's ``deferrals`` keeps
listing a defer that no longer applies.

A regress into PLAN or IMPL means the work is being redone. This module drops
the *unticketed* adjudicated defers from the ledger and the artifact, so a
defer that still applies is re-raised and re-settled, and one that no longer
applies never comes back. A ticketed defer keeps its ticket: the release never
touches it, and closing its issue stays the sweep's job.

Git-free and lock-free: the file work reads and writes the ledger and the
artifact only, so the caller may run it under ``sessions_lock`` (ADR-0019). It
never raises; a corrupt ledger or artifact is logged and left as it was.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    finding_identity,
    load_ledger,
    save_ledger,
)
from cw.review_finding_dispositions.model import debt_key
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_adjudication import Adjudication
    from cw.review_convergence import ConvergenceLedger, FindingIdentity, SettledEntry

_log = logging.getLogger(__name__)

_DEFER = "defer"
_ADJUDICATION = "adjudication"
#: The summary length a release log line keeps.
_LOG_SUMMARY_CHARS = 200


def _ticketed_identities(
    task: TicketTask, artifact: list[Adjudication]
) -> set[FindingIdentity]:
    """Identities of the artifact defers that already carry a ticket."""
    ticketed_row_keys = {r.key for r in task.review_debt if r.ticket_id is not None}
    return {
        finding_identity(a.severity, a.file, a.summary, fingerprint=a.fingerprint)
        for a in artifact
        if a.ticket or _artifact_key(a) in ticketed_row_keys
    }


def _artifact_key(entry: Adjudication) -> str:
    """The debt key an artifact defer files under (the filer's own function)."""
    return debt_key(entry.file, entry.summary, fingerprint=entry.fingerprint)


def _settled_is_ticketed(
    entry: SettledEntry,
    ticketed: set[FindingIdentity],
    ticketed_row_keys: set[str],
) -> bool:
    """Whether a settled defer keeps its ticket (artifact or engine row record)."""
    if entry.identity in ticketed:
        return True
    fingerprint = entry.identity.fingerprint
    if fingerprint is None:
        return False
    key = debt_key(
        entry.identity.file,
        entry.identity.summary_normalized,
        fingerprint=fingerprint,
    )
    return key in ticketed_row_keys


def _drop_settled(
    ledger: ConvergenceLedger,
    task: TicketTask,
    ticketed: set[FindingIdentity],
) -> list[SettledEntry]:
    """The settled defers to drop: adjudicated, unticketed, not operator-set."""
    ticketed_row_keys = {r.key for r in task.review_debt if r.ticket_id is not None}
    return [
        entry
        for entry in ledger.settled
        if entry.outcome == _DEFER
        and entry.origin == _ADJUDICATION
        and not _settled_is_ticketed(entry, ticketed, ticketed_row_keys)
    ]


def _release_ledger(
    worktree: Path,
    task: TicketTask,
    ticketed: set[FindingIdentity],
) -> list[SettledEntry]:
    """Drop the unticketed settled defers from the ledger; return what it dropped."""
    ledger_path = worktree / LEDGER_RELATIVE_PATH
    ledger = load_ledger(ledger_path)
    if ledger is None:
        return []
    dropped = _drop_settled(ledger, task, ticketed)
    if not dropped:
        return []
    dropped_ids = {entry.identity for entry in dropped}
    kept = [e for e in ledger.settled if e.identity not in dropped_ids]
    save_ledger(ledger_path, ledger.model_copy(update={"settled": kept}))
    return dropped


def _release_artifact(
    worktree: Path,
    ticketed: set[FindingIdentity],
    artifact: list[Adjudication],
) -> list[Adjudication]:
    """Rewrite the artifact without its unticketed defers; return what it dropped."""
    path = worktree / DEFERRED_FINDINGS_RELATIVE_PATH
    released = [
        a
        for a in artifact
        if a.outcome == _DEFER
        and finding_identity(a.severity, a.file, a.summary, fingerprint=a.fingerprint)
        not in ticketed
    ]
    if not released:
        return []
    kept = [a for a in artifact if a not in released]
    if kept:
        atomic_write_text(path, render_deferred_findings_md(kept))
    else:
        path.unlink(missing_ok=True)
    return released


def release_unticketed_defers(task: TicketTask, client_cfg: ClientConfig | None) -> int:
    """Drop the regressed task's unticketed adjudicated defers; return the count.

    Called from ``_stage_regress`` on a regress into PLAN or IMPL. Returns 0,
    and logs nothing, when there is no worktree, ledger or artifact to release.
    """
    worktree = resolve_task_worktree(task, client_cfg, verify_checkout=False)
    if worktree is None:
        return 0
    try:
        artifact = read_deferred_findings_md(worktree / DEFERRED_FINDINGS_RELATIVE_PATH)
        ticketed = _ticketed_identities(task, artifact)
        dropped = _release_ledger(worktree, task, ticketed)
        released = _release_artifact(worktree, ticketed, artifact)
    except (OSError, ValueError):
        _log.warning(
            "review defer release skipped ticket=%s client=%s",
            task.ticket_id,
            task.client,
            exc_info=True,
        )
        return 0
    if not dropped and not released:
        return 0
    _log.info(
        "released unticketed review defers on regress ticket=%s client=%s released=%d",
        task.ticket_id,
        task.client,
        len(dropped) + len(released),
    )
    for entry in dropped:
        _log.info(
            "released review defer ticket=%s client=%s key=%s summary=%r",
            task.ticket_id,
            task.client,
            _settled_key(entry),
            entry.identity.summary_normalized[:_LOG_SUMMARY_CHARS],
        )
    for item in released:
        _log.info(
            "released review defer ticket=%s client=%s key=%s summary=%r",
            task.ticket_id,
            task.client,
            _artifact_key(item),
            item.summary[:_LOG_SUMMARY_CHARS],
        )
    return len(dropped) + len(released)


def _settled_key(entry: SettledEntry) -> str:
    """The debt key a settled defer files under (engine fingerprint or summary hash)."""
    identity = entry.identity
    return debt_key(
        identity.file, identity.summary_normalized, fingerprint=identity.fingerprint
    )
