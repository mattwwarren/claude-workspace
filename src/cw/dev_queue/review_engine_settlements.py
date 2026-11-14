"""The single writer of ``TicketTask.review_engine_settlements`` (GitHub #3625).

``record_engine_settlement`` is the only code that writes the settlement list.
It runs under ``dev_queue_lock`` only, never takes ``sessions_lock``, and spawns
no process (ADR-0019). Its guarantee is exact: **the reviewed branch cannot
edit the file this lives in.** It is cw state outside the worktree. It is not
that the branch cannot cause a write: a worker running ``cw review converge
record-round`` in its own session reaches this writer by design. The gate below
keeps that write narrow. It refuses unless the resolved row is REVIEW-stage and
RUNNING, and it writes nothing otherwise, so a refusal leaves no attestation and
never a false PASS.

The pure reader and the reconciler live in
:mod:`cw.dev_queue.review_engine_reconcile`, which imports no ``crud`` and so
cannot close an import cycle through ``lifecycle`` and ``review_attestation``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Final

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.models import QueueItemStatus, Stage
from cw.models.review_attestation import HEAD_SHA_PATTERN
from cw.models.review_engine_settlement import ReviewEngineSettlement

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from cw.models import TicketTask

_log = logging.getLogger(__name__)

#: Heads kept per row. Newest wins; the oldest head is dropped first.
MAX_SETTLEMENT_HEADS: Final = 8

_HEAD_RE: Final = re.compile(HEAD_SHA_PATTERN)


def _require_review_running(task: TicketTask) -> None:
    """Refuse unless the row is REVIEW-stage and RUNNING. Writes nothing otherwise."""
    if task.stage != Stage.REVIEW or task.status != QueueItemStatus.RUNNING:
        msg = (
            "review engine settlement needs a RUNNING REVIEW-stage row, got "
            f"stage={task.stage} status={task.status}"
        )
        raise CwError(msg)


def _upsert_bounded(
    existing: Sequence[ReviewEngineSettlement], incoming: ReviewEngineSettlement
) -> list[ReviewEngineSettlement]:
    """Replace the record for *incoming*'s head, append it, keep the newest N."""
    kept = [record for record in existing if record.head_sha != incoming.head_sha]
    kept.append(incoming)
    return kept[-MAX_SETTLEMENT_HEADS:]


def record_engine_settlement(
    ticket_id: str,
    client_name: str,
    head_sha: str,
    fingerprints: Sequence[str],
    *,
    recorded_at: datetime,
) -> None:
    """Record the deferred *fingerprints* of an engine round at *head_sha*.

    Every refusal is raised before the first write, so a refused call leaves the
    saved store byte-unchanged.

    Raises:
        CwError: if *head_sha* is not 40-hex, no row exists for
            ``(ticket_id, client_name)``, or the row is not a RUNNING REVIEW row.
        OSError: if the store could not be written.
    """
    if _HEAD_RE.fullmatch(head_sha) is None:
        msg = f"review engine settlement needs a 40-hex head_sha, got {head_sha!r}"
        raise CwError(msg)
    record = ReviewEngineSettlement(
        head_sha=head_sha,
        fingerprints=sorted(set(fingerprints)),
        recorded_at=recorded_at,
    )
    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        _require_review_running(task)
        task.review_engine_settlements = _upsert_bounded(
            task.review_engine_settlements, record
        )
        save_dev_queue(store)
    _log.info(
        "Recorded %d engine settlement fingerprint(s) at %s on %s/%s",
        len(record.fingerprints),
        head_sha,
        client_name,
        ticket_id,
    )
