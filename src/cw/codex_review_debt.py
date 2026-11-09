"""File the codex lane's SHOULD_FIX deferrals as review debt at record time (#3137).

The codex loop round and the Claude confirm round stamp SHOULD_FIX findings as
``defer`` on the convergence ledger, where they are non-gating. Nothing filed
them: only ``cw review adjudicate`` and the engine-confirm path reach the
review-debt filer, so a codex defer stayed unticketed until a finalize retry,
if one ever ran.

:func:`file_codex_deferrals` writes the round's new defers to the session
artifact and hands them to the filer. It runs in the detached ``cw codex run``
process, whose cwd is not guaranteed to be the session worktree, so every path
it touches is anchored on *worktree*.

**Structural call (named, not hidden):** the filer lives in
``cw.cli.review.debt``, and that package reaches back into the codex lane
through ``cw.dev_queue`` and ``cw.dispatch``. A module-level import from here
closes a cycle from every entry point, and a function-level import is barred by
PLC0415. So the filer is *registered*: ``cw.cli.review.debt`` calls
:func:`register_debt_filer` when it loads, and ``cw.cli.codex`` (the detached
``cw codex run`` process, which never loads ``cw.cli.review.debt``) registers it
explicitly before the driver starts. Hoisting ``run_debt_filing`` into a core
package removes the registry; that is the follow-up.

Contract: *ledger* is the PRE-record ledger, the one ``record_round`` received.
``record_round`` settles every new defer into its returned ledger, so a
post-record ledger would make the "not already settled" filter drop them all.

Filing never raises: a failure is logged and the loop carries on, because the
defer is already recorded and a finalize retry can still file it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    Adjudication,
    merge_deferred_adjudications,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_convergence import finding_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundFinding

_log = logging.getLogger(__name__)

_DEFER: Final = "defer"
#: The filer's registry key; ``cw.cli.review.debt`` and ``cw.cli.codex`` fill it.
_FILER_KEY = "run_debt_filing"
_FILERS: dict[str, Callable[..., None]] = {}


def register_debt_filer(run_filing: Callable[..., None]) -> None:
    """Install the review-debt filer this lane hands its defers to.

    Called by ``cw.cli.review.debt`` at import and by ``cw codex run`` before the
    detached driver starts. Re-registering replaces the previous filer, which is
    what a test that swaps the filer relies on.
    """
    _FILERS[_FILER_KEY] = run_filing


def round_deferral(finding: RoundFinding, round_number: int, now: str) -> Adjudication:
    """The debt ``Adjudication`` of one round-recorded SHOULD_FIX defer.

    Shared by the codex lane and ``cw review converge record-round``. A round
    records no line or evidence, so the identity is the file and the summary
    alone. The rationale is the finding's own (the lane's text, not a fixed one).
    """
    return Adjudication(
        severity=finding.severity,
        file=finding.file,
        line_start=None,
        line_end=None,
        evidence="",
        summary=finding.summary,
        outcome=_DEFER,
        rationale=finding.rationale,
        round=round_number,
        recorded_at=now,
    )


def _new_defers(
    findings: Sequence[RoundFinding], ledger: ConvergenceLedger
) -> list[RoundFinding]:
    """The round's defers that the pre-record ledger had not already settled."""
    settled = {entry.identity for entry in ledger.settled}
    return [
        finding
        for finding in findings
        if finding.outcome == _DEFER
        and finding_identity(
            finding.severity,
            finding.file,
            finding.summary,
            fingerprint=finding.fingerprint,
        )
        not in settled
    ]


def _write_artifact(path: Path, applied: Sequence[Adjudication]) -> None:
    """Merge *applied* into the session artifact, then write it atomically.

    The artifact is the rediscovery point a finalize Step 4d retry reads, so it
    is written before the filer runs.
    """
    prior = read_deferred_findings_md(path)
    merged = merge_deferred_adjudications(prior, list(applied))
    rendered = render_deferred_findings_md(merged)
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)


def file_codex_deferrals(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    findings: Sequence[RoundFinding],
    ledger: ConvergenceLedger,
    round_number: int,
    now: str,
) -> None:
    """Write the round's new defers to the session artifact, then file them.

    Returns before any IO when nothing is new. A missing filer (the registry
    was never filled) is logged, not silently skipped: the artifact still holds
    the defers for a later ``cw review debt file``.
    """
    new_defers = _new_defers(findings, ledger)
    if not new_defers:
        return
    applied = [round_deferral(f, round_number, now) for f in new_defers]
    artifact = worktree / DEFERRED_FINDINGS_RELATIVE_PATH
    run_filing = _FILERS.get(_FILER_KEY)
    try:
        _write_artifact(artifact, applied)
        if run_filing is None:
            # The artifact above still holds the defers for `cw review debt file`.
            _log.warning(
                "review-debt filer not registered, deferrals left in artifact "
                "ticket=%s client=%s session=%s round=%s",
                task.ticket_id,
                client.name,
                sid,
                round_number,
            )
            return
        run_filing(client.name, task.ticket_id, artifact, applied, cwd=worktree)
    except (OSError, ValueError, CwError):
        _log.warning(
            "codex review-debt filing failed ticket=%s client=%s session=%s round=%s",
            task.ticket_id,
            client.name,
            sid,
            round_number,
            exc_info=True,
        )
        return
    _log.info(
        "codex review-debt deferrals handed to filing ticket=%s client=%s "
        "session=%s round=%s count=%d",
        task.ticket_id,
        client.name,
        sid,
        round_number,
        len(applied),
    )
