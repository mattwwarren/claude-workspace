"""One operator-declared merge prerequisite on a dev-queue row (GitHub #3791).

``cw dev-queue prerequisite TICKET -c CLIENT --pr N`` records that the row must
not be dispatched to any stage until same-repo PR ``N`` merges. The entry is
verified with one gh call at declaration (the PR is OPEN and in the client's
repo) and is never derived from review findings, ticket comments or the ticket
body.

The claim gate (``cw.dispatch.claim.merge_prerequisite``) parks a row with an
unsatisfied entry as a ``dependency_hold`` on that PR. The serve tick's
stale-gate release (``cw.reconcile.tasks.stale_gate``) stamps ``satisfied_at``
on every entry whose PR has merged just before it releases the hold. The stamp
is the durable record: the merged watch is retired in the same save, so the
evidence would otherwise be gone by the next claim.

Same-repo only by writer rule: the verb and the locked core write
``pr.repo=None``. The readers below skip any entry with ``pr.repo`` set, so a
hand-edited cross-repo entry is inert. Cross-repo waits belong to
``cw dev-queue hold --until-pr-merged owner/repo#N`` (#3634).

Its own small module (the ``scope_grant`` precedent) so ``next_commands``, the
claim gate and ``stale_gate`` can import the pure helpers without a cycle
through ``cw.dev_queue``. Not re-exported from ``cw.models``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from cw.models.pr_ref import PrRef

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.models.pr_ref import PrRefKey


class MergePrerequisite(BaseModel):
    """A same-repo PR that must merge before the row is dispatched again.

    ``repo`` is the slug gh verified at declaration (the audit ``repo`` of the
    ``ticket.held`` event the claim gate records). ``satisfied_at`` is ``None``
    until a merge is observed.
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    pr: PrRef
    repo: str
    actor: str = ""
    declared_at: datetime
    satisfied_at: datetime | None = None


def _is_open_same_repo(entry: MergePrerequisite) -> bool:
    return entry.satisfied_at is None and entry.pr.repo is None


def unsatisfied(entries: Iterable[MergePrerequisite]) -> list[MergePrerequisite]:
    """Same-repo entries with no ``satisfied_at``, in declared order."""
    return [entry for entry in entries if _is_open_same_repo(entry)]


def mark_merged(
    entries: Iterable[MergePrerequisite], merged_keys: set[PrRefKey], now: datetime
) -> int:
    """Stamp ``satisfied_at`` on unsatisfied same-repo entries in *merged_keys*.

    Returns the number stamped. A satisfied entry is never restamped and a
    cross-repo entry never matches.
    """
    stamped = 0
    for entry in entries:
        if _is_open_same_repo(entry) and entry.pr.key() in merged_keys:
            entry.satisfied_at = now
            stamped += 1
    return stamped
