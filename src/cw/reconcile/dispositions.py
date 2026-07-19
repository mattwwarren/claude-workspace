"""Idle/stalled routed-sentinel and salvage completion payload helpers.

Extracted from the near-identical SESSION_COMPLETED tails duplicated across
``idle.py`` and ``stalled.py`` (GitHub #1306). The R2 split deliberately keeps
two different shapes rather than a single helper:

- :func:`build_salvage_completion_payload` is a PURE function — it builds and
  returns the 8-field ``SESSION_COMPLETED`` payload dict. Zero I/O: it never
  calls ``record_event`` or touches the daemon client. Callers that need to
  interleave the emission with their own additional mutations (e.g. idle.py's
  and stalled.py's salvage loops, which call ``_apply_salvaged_completion``
  before this payload is built) keep ``record_event``/``stop()`` inline at
  the call site and use this helper only for the payload construction.
- :func:`emit_routed_sentinel_completion` is SIDE-EFFECTING — it builds the
  same payload (via the pure helper above), calls ``record_event``, and then
  — only when the session still has a ``surface_ref`` — stops the daemon
  surface. The emit-before-stop order is binding: it matches every existing
  routed-sentinel call site in idle.py/stalled.py/phantom.py.

Both helpers pass ``ticket_id`` through unguarded (no truthiness filter) and
neither passes a ``correlation_id`` to ``record_event``, matching the
pre-existing idle.py/stalled.py call sites this module replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.reconcile import _deps

if TYPE_CHECKING:
    from cw.auto_dev_result import Status
    from cw.models import Session


def build_salvage_completion_payload(
    session: Session,
    *,
    ticket_id: str | None,
    status: Status,
) -> dict[str, object]:
    """Build the 8-field salvaged SESSION_COMPLETED payload. Pure — zero I/O.

    ``ticket_id`` is passed through unguarded, matching the pre-existing
    idle.py/stalled.py inline payload constructions this replaces.
    """
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": ticket_id,
        "claude_session_id": session.claude_session_id,
        "crashed": False,
        "salvaged": True,
        "status": status,
    }


def emit_routed_sentinel_completion(
    session: Session,
    *,
    ticket_id: str | None,
    status: Status,
) -> None:
    """Emit a salvaged SESSION_COMPLETED, then stop the surface if still live.

    Emit-before-stop order is binding — matches every existing routed-sentinel
    call site in idle.py/stalled.py. No ``correlation_id`` is passed, matching
    those same call sites (phantom.py's analogous call does pass one; that
    divergence is deliberate and out of scope here — see GitHub #1306).
    """
    payload = build_salvage_completion_payload(
        session, ticket_id=ticket_id, status=status
    )
    record_event(OrchestratorEventType.SESSION_COMPLETED, payload)
    if session.surface_ref is not None:
        _deps.get_native_daemon_client().stop(session.surface_ref)
