"""One operator scope-drift grant, kept on the row as a durable ledger entry.

``cw dev-queue approve --scope-drift`` (GitHub #2337) allows paths beyond the
plan's ``## Files Modified`` for one IMPL round. The per-spawn transport fields
on :class:`cw.models.TicketTask` are cleared at spawn success, so a later
REVIEW-to-IMPL repair dispatch would re-present the whole extra list. Each
approval is therefore also appended here as a :class:`ScopeGrant`, and
``cw.scope_grants`` re-derives the transport from the ledger at each IMPL
dispatch, reusing only the grants that still verify.

Written only under ``dev_queue_lock`` by ``cw.dev_queue.scope_drift``. Its own
small module (the ``review_engine_settlement`` precedent) so ``models/tasks``
gains only the field. Not re-exported from ``cw.models``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ScopeGrant(BaseModel):
    """The binding and the exact paths of one scope-drift approval.

    ``head`` is a plain ``str``: the writer validates the full 40-hex shape, and
    the carry re-validates it, so a malformed value never becomes a pydantic
    error on a seam that must not raise. ``repo``, ``pr_number`` and
    ``plan_fingerprint`` are ``None`` when that dimension was unknown at grant
    time; ``None`` on the repository binding fails the carry closed.
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    client: str
    ticket_id: str
    row_created_at: datetime
    repo: str | None = None
    pr_number: int | None = None
    head: str
    policy: str
    plan_fingerprint: str | None = None
    paths: list[str]
    actor: str = ""
    recorded_at: datetime
