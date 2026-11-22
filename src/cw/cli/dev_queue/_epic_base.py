"""CLI-side ``--base`` gate for ``cw dev-queue add`` (#3783, RFC 0020 B1).

Split out of ``crud.py`` to keep it under the ~1000-line ceiling, as ``_adopt``
was. ``crud`` keeps only the option and branchless delegations: ``EnqueueBase``
is a null object, so with no ``--base`` every method is a no-op and the add loop
gains no branches.

The library checks live in ``cw.dev_queue.epic_base``. This module adds the
parse-time charset callback, the per-invocation memo (one origin check per
client), the sibling note and the ``--base not applied`` suffix.
"""

from __future__ import annotations

import logging

import click

from cw.config import get_client
from cw.dev_queue import load_dev_queue
from cw.dev_queue.epic_base import (
    check_epic_size_ceiling,
    refuse_redundant_base,
    verify_base_on_origin,
)
from cw.models.tasks.validators import _check_branch_charset

logger = logging.getLogger(__name__)

_NOT_APPLIED_SUFFIX = (
    " --base not applied: the existing row keeps its own base"
    " (there is no retarget verb)."
)


def validate_base_option(
    _ctx: click.Context, _param: click.Parameter, value: str | None
) -> str | None:
    """Click callback: check ``--base``'s charset at parse time, before config loads."""
    if value is None:
        return None
    try:
        _check_branch_charset("base_branch", value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--base") from exc
    return value


def _label(branch: str | None) -> str:
    return f"base '{branch}'" if branch else "the client default"


class EnqueueBase:
    """The ``--base`` gate for one ``add`` invocation.

    ``verify`` runs once per resolved client, before ``build_queued_task`` and
    ``add_ticket``, so it never runs under the dev-queue lock.
    """

    def __init__(self, base: str | None) -> None:
        self.base = base
        self._verified: set[str] = set()

    def verify(self, client_name: str) -> None:
        """Redundancy, origin and ceiling checks for ``client_name``, memoized."""
        if self.base is None or client_name in self._verified:
            return
        client = get_client(client_name)
        refuse_redundant_base(client, self.base)
        verify_base_on_origin(client, self.base)
        check_epic_size_ceiling(client, self.base)
        self._verified.add(client_name)

    def note_siblings(self, ticket_id: str, client_name: str) -> None:
        """Print one stderr note per same-ticket row on a different base.

        Runs after a successful insert, outside any lock. Rows never inherit a
        base, so this is advisory only. Fail-soft: the row is already persisted,
        so an unreadable queue file becomes a DEBUG line, never a failed enqueue.
        """
        try:
            queue = load_dev_queue()
        except (OSError, ValueError):
            logger.debug("sibling note skipped: dev queue unreadable", exc_info=True)
            return
        for row in queue.tasks:
            if (
                row.ticket_id == ticket_id
                and row.client == client_name
                and row.base_branch != self.base
            ):
                click.echo(
                    f"Note: {ticket_id} already has a {row.stage.value} row"
                    f" ({row.status.value}) on {_label(row.base_branch)}; this new"
                    f" row is on {_label(self.base)}. Rows do not inherit a base"
                    " (RFC 0020, ADR-0024).",
                    err=True,
                )

    def skipped_suffix(self) -> str:
        """Appended to the Skipped echo when ``--base`` was given but not applied."""
        return _NOT_APPLIED_SUFFIX if self.base is not None else ""
