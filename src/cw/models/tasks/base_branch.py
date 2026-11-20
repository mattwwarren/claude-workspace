"""The one writer of ``TicketTask.base_branch`` (GitHub #3779, RFC 0020 D-2/D-3).

``base_branch`` is the branch a row merges into. It is stamped at enqueue and
then frozen: once a row has ever spawned a session, retargeting it would orphan
the work already done against the old base. :func:`set_base_branch` is the only
assignment to the attribute in ``src/``; ``tests/test_base_branch_seam_guard.py``
keeps it that way.

The immutability predicate is ``ever_spawned``, not ``session_id`` (cleared on
cancel and revert) or ``attempts`` (incremented at claim, before spawn).
``ever_spawned`` survives every requeue, regress, revert and retry. Legacy rows
default ``True`` on load, so they are frozen: fail-closed, intended.

``TicketTask`` has no ``validate_assignment``, so assignment skips validators;
this helper validates the charset itself so an invalid value can never be
written.

A leaf submodule of ``cw.models.tasks``: it imports ``TicketTask`` for type
checking only and ``cw.exceptions`` at runtime (the same edge as
``cw.models.state``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.exceptions import CwError
from cw.models.tasks.validators import _check_branch_charset

if TYPE_CHECKING:
    from cw.models.tasks import TicketTask


class BaseBranchFrozenError(CwError):
    """Refused a ``base_branch`` change on a row that has already spawned.

    Defined beside its sole raiser, the in-tree convention for feature-local
    ``CwError`` subclasses, so ``cw.exceptions`` does not grow.
    """

    __slots__ = ()


def set_base_branch(task: TicketTask, value: str | None) -> None:
    """Set ``task.base_branch`` to *value*, refusing a change once spawned.

    Validates the charset first, so an invalid value is never written. Setting
    the value the row already holds is an idempotent no-op, even on a spawned
    row. Any other change, including null to set and set to null, raises
    :class:`BaseBranchFrozenError` when ``task.ever_spawned`` is true.
    """
    _check_branch_charset("base_branch", value)
    if value == task.base_branch:
        return
    if task.ever_spawned:
        msg = (
            f"Ticket {task.ticket_id!r} already spawned; base_branch is frozen"
            f" at {task.base_branch!r} and cannot change to {value!r}"
        )
        raise BaseBranchFrozenError(msg)
    task.base_branch = value
