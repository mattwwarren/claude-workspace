"""Load the facts a refusal's next-command hint needs (#2692, #2760, #2765).

``next_commands.refusal_suffix`` is pure (``TestPurity`` pins that it reads no
state), so the state reads a refusal site needs live here: the ``Session`` its
row is bound to and, fail-soft, the client config that resolves the row's
pipeline position. ``bound_session`` is a bare, lock-free ``load_state()``
read, which ADR-0005 permits for read-only callers, including inside the
dev-queue lock (``move_ticket``). It is never called under ``sessions_lock``:
``unblock_ticket`` already holds the session and passes it straight to
``refusal_suffix``, with the position and finalize fact it resolved before
taking the lock.

The lookup is id-only (``Session.id == task.session_id``), the same policy
``show`` uses (``resolve_session_for_task``), and deliberately not
``find_by_name_or_id``: a legacy row whose ``session_id`` holds a session name
resolves to no session here (Adopted Assumption 19).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import yaml

from cw.config import get_client, load_state
from cw.dev_queue.next_commands import refusal_suffix
from cw.exceptions import CwError
from cw.models import Stage
from cw.models.enums import PipelinePosition, stage_position

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


def bound_session(task: TicketTask) -> Session | None:
    """The ``Session`` *task* is bound to, or None (unbound, gone, unreadable)."""
    if task.session_id is None:
        return None
    try:
        state = load_state()
    except (OSError, ValueError, CwError):
        return None
    return next((s for s in state.sessions if s.id == task.session_id), None)


def row_pipeline_position(task: TicketTask) -> PipelinePosition:
    """*task*'s stage position in its lane pipeline; INTERIOR if unresolvable.

    A plain ``get_client`` read (no lock, no subprocess). An unknown client or
    an unreadable ``clients.yaml`` falls back to INTERIOR, the pre-#2760 hint:
    ``approve`` itself fails on such a config.
    """
    # Keep this cycle-breaking import deferred (as ``operator_resolutions``
    # does); ``cw.executor`` reaches back through the ``cw.dev_queue`` package
    # initializer.
    executor = importlib.import_module("cw.executor")
    try:
        position: PipelinePosition = executor.pipeline_position(
            task, get_client(task.client)
        )
    except (CwError, OSError, ValueError, yaml.YAMLError):
        return PipelinePosition.INTERIOR
    return position


def row_finalize_in_pipeline(task: TicketTask) -> bool:
    """Whether FINALIZE is in *task*'s lane pipeline; True if unresolvable (#2765).

    The fail-soft twin of :func:`row_pipeline_position`: an unknown client or
    an unreadable ``clients.yaml`` gives True, the pre-#2765 hint.
    """
    executor = importlib.import_module("cw.executor")
    try:
        finalize: bool = executor.finalize_in_pipeline(task, get_client(task.client))
    except (CwError, OSError, ValueError, yaml.YAMLError):
        return True
    return finalize


def refusal_suffix_for_stages(
    task: TicketTask,
    session: Session | None,
    stages: list[Stage],
    *,
    skip: str | None = None,
) -> str:
    """``refusal_suffix`` with position and finalize fact read from *stages*.

    For a refusal site that already holds the row's resolved pipeline *stages*:
    both caller-resolved facts come from that one list, so they cannot disagree
    and no site can forget one (#2765).
    """
    return refusal_suffix(
        task,
        session=session,
        skip=skip,
        pipeline_position=stage_position(task.stage, stages),
        finalize_in_pipeline=Stage.FINALIZE in stages,
    )


def refusal_hint(task: TicketTask, *, skip: str | None = None) -> str:
    """``refusal_suffix`` for *task*, with its session, position and finalize fact."""
    return refusal_suffix(
        task,
        session=bound_session(task),
        skip=skip,
        pipeline_position=row_pipeline_position(task),
        finalize_in_pipeline=row_finalize_in_pipeline(task),
    )
