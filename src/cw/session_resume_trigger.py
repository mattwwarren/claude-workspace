"""Wake a paused session so it reads its inbox (GitHub #2212).

**This is a resume-with-continuity respawn, not a live-turn injection.**
``docs/spikes/claude-bg-wakeup-drop-findings.md`` (#1889) already investigated
and ruled out a mid-turn delivery primitive for ``claude --bg``: "There is no
code path anywhere in ``src/cw/`` that injects a message, re-resumes a
session, or delivers any notification *into* a running ``claude --bg``
process." :class:`~cw.native_daemon.NativeDaemonClient` has no inject/queue
method, and none is invented here. What this module does is compose the
mechanism ``resume_session``'s dead-surface branch already uses for exactly
this kind of recovery: ``_resolve_resume_cwd`` -> ``_resume_spawn_args`` ->
``spawn_bg(extra_args=["--resume", ...])`` -> ``_verify_roster_registration``
-> ``mutate_state`` -> ``record_event``.

**Scope is deliberately restricted to sessions that are already paused** --
the owning ``TicketTask`` is ``BLOCKED_ON_USER``, or the ``Session`` is
``SessionStatus.IDLE``. Delivering to a genuinely live session is deferred to
**#2255**, and :func:`ResumeTriggerAdapter.trigger` returns before touching
the daemon at all in that case. The restriction is not polish deferred for
later: ``SessionStatus`` carries no signal finer than ACTIVE/IDLE, so nothing
today distinguishes "mid-tool-call" from "paused, safe to interrupt," and
this module does not invent one. See ``docs/adr/0017-session-inbox-and-
resume-trigger.md`` for the decision record.

``daemon.stop()`` is never called. ``grep -n "\\.stop(" src/cw/session.py``
returns nothing -- the branch this module copies has never needed it either,
for any session it has ever respawned -- and the gate is what makes that
safe: by definition the session is not concurrently mid-task.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

from cw.config import get_client, mutate_state
from cw.dev_queue import list_tickets
from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent, record_event
from cw.models import CwState, QueueItemStatus, SessionStatus
from cw.native_daemon import get_native_daemon_client
from cw.session import _resolve_resume_cwd, _resume_spawn_args
from cw.spawn import (
    _ROSTER_POLL_INTERVAL_SECS,
    _ROSTER_POLL_TIMEOUT_SECS,
    _verify_roster_registration,
)

if TYPE_CHECKING:
    from cw.models import Session, SessionInboxMessage
    from cw.native_daemon import NativeDaemonClient

logger = logging.getLogger(__name__)

DEFERRED_LIVE_DELIVERY_REASON = (
    "session is live and mid-task; live-session delivery is deferred to #2255"
)
_NO_TRANSCRIPT_REASON = (
    "session has no claude_session_id, so there is no transcript to resume"
    " into; the message stays queued for a future resume"
)
_DELIVERED_REASON = "session respawned with --resume; it will read its inbox"


class ResumeTriggerResult(NamedTuple):
    """Outcome of one resume attempt.

    ``delivered=False`` is an ordinary, expected outcome -- a gate refusal or
    a respawn failure -- and never implies the message failed to queue. The
    two are independent: ``cw session send`` appends before it triggers.
    """

    delivered: bool
    reason: str
    surface_ref: str | None = None


@runtime_checkable
class ResumeTriggerAdapter(Protocol):
    """Protocol for waking a paused session so it reads a queued message."""

    def trigger(
        self, session: Session, message: SessionInboxMessage
    ) -> ResumeTriggerResult:
        """Attempt to wake *session* to read *message*.

        Never raises for an ineligible or unreachable session: the message is
        already durably queued by the time this is called, so a failure here
        is reported, not propagated.
        """
        ...


def _is_eligible(session: Session) -> bool:
    """Whether *session* is paused enough to be safely respawned.

    Two independent OR'd signals, not one derived from the other:
    ``_route_stopped_without_sentinel``'s docstring states that
    ``session.status`` is never touched on a BLOCKED_ON_USER transition, so a
    parked row's owning Session can still read ACTIVE. A session with no
    dev-queue row at all (an ad hoc USER-origin session) gates purely on
    ``SessionStatus.IDLE``.
    """
    if session.status is SessionStatus.IDLE:
        return True
    task = next(
        (t for t in list_tickets(session.client) if t.session_id == session.id), None
    )
    return task is not None and task.status is QueueItemStatus.BLOCKED_ON_USER


def _build_resume_prompt(message: SessionInboxMessage) -> str:
    """Compose the prompt the respawned session wakes up on.

    The message body is inlined rather than merely referenced so the session
    can act on it even if it never calls back into the inbox -- the inbox
    remains the durable record and the cursor the replay guard, but a woken
    session should not need a second round trip to learn what it was asked.
    """
    return (
        f"The operator ({message.author}) has answered the question you paused"
        f" on:\n\n{message.body}\n\nContinue from where you left off."
    )


class NativeDaemonResumeTriggerAdapter:
    """Resume trigger backed by ``claude --bg`` (the one production harness).

    A second real harness is deliberately not built here: codex is wired in
    this repo only as a one-shot review subprocess, not a resumable worker
    session, so a second adapter would mean inventing worker-spawn machinery
    that does not exist. The Protocol boundary is what makes that a follow-up
    rather than a rewrite.
    """

    def __init__(
        self,
        *,
        native_daemon: NativeDaemonClient | None = None,
        roster_poll_timeout: float = _ROSTER_POLL_TIMEOUT_SECS,
        roster_poll_interval: float = _ROSTER_POLL_INTERVAL_SECS,
    ) -> None:
        self._daemon = native_daemon or get_native_daemon_client()
        self._roster_poll_timeout = roster_poll_timeout
        self._roster_poll_interval = roster_poll_interval

    def trigger(
        self, session: Session, message: SessionInboxMessage
    ) -> ResumeTriggerResult:
        """Respawn *session* with ``--resume`` so it wakes on *message*."""
        if not _is_eligible(session):
            return ResumeTriggerResult(
                delivered=False, reason=DEFERRED_LIVE_DELIVERY_REASON
            )
        if not session.claude_session_id:
            return ResumeTriggerResult(delivered=False, reason=_NO_TRANSCRIPT_REASON)
        try:
            return self._respawn(session, message)
        except CwError as exc:
            logger.warning("resume trigger for session %s failed: %s", session.id, exc)
            return ResumeTriggerResult(
                delivered=False, reason=f"daemon respawn failed: {exc}"
            )

    def _respawn(
        self, session: Session, message: SessionInboxMessage
    ) -> ResumeTriggerResult:
        """Run the dead-surface respawn composition. Raises CwError on failure."""
        client = get_client(session.client)
        session_cwd = _resolve_resume_cwd(session, client)
        extra_args, permission_mode = _resume_spawn_args(session, client)

        new_short_id = self._daemon.spawn_bg(
            cwd=session_cwd,
            prompt=_build_resume_prompt(message),
            extra_args=extra_args or None,
            permission_mode=permission_mode,
        )
        _verify_roster_registration(
            self._daemon,
            new_short_id,
            timeout=self._roster_poll_timeout,
            interval=self._roster_poll_interval,
        )

        def _update(state: CwState) -> None:
            live = state.find_by_name_or_id(session.id)
            if live is not None:
                live.surface_ref = new_short_id
                live.status = SessionStatus.ACTIVE
                live.resumed_at = datetime.now(UTC)

        mutate_state(_update)
        # The history-bus SESSION_RESUMED record every daemon respawn already
        # emits (session.py:530-539). Distinct from cw.events.record_event,
        # which the CLI layer emits for the inbox append -- same name,
        # different module, different signature, neither substituting for the
        # other.
        record_event(
            session.client,
            HistoryEvent(
                event_type=EventType.SESSION_RESUMED,
                client=session.client,
                session_id=session.id,
                session_name=session.name,
                purpose=session.purpose,
            ),
        )
        return ResumeTriggerResult(
            delivered=True, reason=_DELIVERED_REASON, surface_ref=new_short_id
        )


class FakeResumeTriggerAdapter:
    """In-memory adapter for tests. Records all calls; no real I/O.

    Mirrors ``FakeNativeDaemonClient``'s list-recording shape.
    """

    def __init__(self, *, result: ResumeTriggerResult | None = None) -> None:
        self.trigger_calls: list[tuple[str, str]] = []
        self.result = result or ResumeTriggerResult(
            delivered=True, reason=_DELIVERED_REASON, surface_ref="fake0001"
        )

    def trigger(
        self, session: Session, message: SessionInboxMessage
    ) -> ResumeTriggerResult:
        """Record ``(session id, message id)`` and return the canned result."""
        self.trigger_calls.append((session.id, message.id))
        return self.result


def get_resume_trigger_adapter() -> ResumeTriggerAdapter:
    """Return the active resume-trigger adapter.

    Always the native-daemon one in production; tests inject
    :class:`FakeResumeTriggerAdapter` at the call site, mirroring
    ``get_native_daemon_client``.
    """
    return NativeDaemonResumeTriggerAdapter()
