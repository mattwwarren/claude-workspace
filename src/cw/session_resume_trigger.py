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

import contextlib
import fcntl
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

from cw.config import get_client, load_state, mutate_state
from cw.dev_queue import list_tickets
from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent, record_event
from cw.models import CwState, QueueItemStatus, SessionStatus
from cw.native_daemon import get_native_daemon_client
from cw.session import _resolve_resume_cwd, _resume_spawn_args
from cw.session_inbox import advance_cursor, read_unconsumed, session_inbox_dir
from cw.spawn import (
    _ROSTER_POLL_INTERVAL_SECS,
    _ROSTER_POLL_TIMEOUT_SECS,
    _verify_roster_registration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
_ALREADY_RESUMED_REASON = (
    "session was already resumed by a concurrent trigger; message stays"
    " queued for its next pause"
)
_VANISHED_REASON = "session no longer exists; message stays queued"


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


def _build_resume_prompt(messages: list[SessionInboxMessage]) -> str:
    """Compose the prompt the respawned session wakes up on.

    Message bodies are inlined rather than merely referenced so the session
    can act on them even if it never calls back into the inbox -- the inbox
    remains the durable record and the cursor the replay guard, but a woken
    session should not need a second round trip to learn what it was asked.

    Takes every unconsumed message, not just the one that triggered this
    respawn: a session can accumulate more than one queued message while
    paused (a gate refusal, then a later successful trigger), and the reader
    this function feeds is the *only* consumer of the mailbox in production
    (#2212 review finding 1) -- skipping straight to the newest message
    would silently drop any earlier ones still sitting unconsumed.
    """
    if len(messages) == 1:
        message = messages[0]
        return (
            f"The operator ({message.author}) has answered the question you"
            f" paused on:\n\n{message.body}\n\nContinue from where you left off."
        )
    body = "\n\n".join(
        f"[{m.created_at.isoformat()}] {m.author}:\n{m.body}" for m in messages
    )
    return (
        "The operator has sent the following queued messages while you were"
        f" paused, oldest first:\n\n{body}\n\nContinue from where you left off."
    )


def _resume_trigger_lock_path(session_id: str) -> Path:
    """Return the path to a session's resume-trigger claim lock."""
    return session_inbox_dir(session_id) / ".resume-trigger.lock"


@contextlib.contextmanager
def _resume_trigger_lock(session_id: str) -> Iterator[None]:
    """Serialize resume-trigger attempts for one session.

    A per-session file lock, not the global ``sessions_lock()``: held across
    the whole eligibility-recheck -> spawn -> state-commit sequence so two
    concurrent ``cw session send`` calls on the same session cannot both
    pass eligibility and double-spawn (#2212 review finding 3), while
    leaving every *other* session's ``cw`` operations unblocked during the
    (potentially multi-second) daemon spawn and roster-registration poll --
    unlike ``sessions_lock()``, which is a single global file and would
    stall the whole fleet for that window.
    """
    session_inbox_dir(session_id).mkdir(parents=True, exist_ok=True)
    fd = _resume_trigger_lock_path(session_id).open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


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
        # Resolved lazily in _respawn, not here: constructing
        # RealNativeDaemonClient does no I/O and cannot fail (it only stores
        # a path), so there was never a gate-bypass risk either way -- this
        # is cleanliness only, reading better next to the rest of the lazy
        # per-attempt state in _respawn (#2212 review finding 6).
        self._native_daemon_override = native_daemon
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
        """Run the dead-surface respawn composition. Raises CwError on failure.

        The whole body runs under a per-session lock (#2212 review finding
        3): re-reads fresh state and re-checks daemon liveness before
        spawning anything, so a second concurrent call for the same session
        -- whether racing this one or arriving after it already committed --
        finds the session already live and declines rather than
        double-spawning. The task-status half of eligibility
        (``BLOCKED_ON_USER``) is not itself refreshed here on the theory
        that a daemon-liveness check is the decisive, race-proof signal: the
        very first thing a successful respawn does is put the *new* surface
        into the live set, so a second racing call always sees it there
        regardless of which OR-branch made the first call eligible.
        """
        daemon = self._native_daemon_override or get_native_daemon_client()
        client = get_client(session.client)

        with _resume_trigger_lock(session.id):
            fresh = load_state().find_by_name_or_id(session.id)
            if fresh is None:
                return ResumeTriggerResult(delivered=False, reason=_VANISHED_REASON)
            if not fresh.claude_session_id:
                return ResumeTriggerResult(
                    delivered=False, reason=_NO_TRANSCRIPT_REASON
                )
            if (
                fresh.surface_ref
                and fresh.surface_ref in daemon.list_live_session_short_ids()
            ):
                return ResumeTriggerResult(
                    delivered=False,
                    reason=_ALREADY_RESUMED_REASON,
                    surface_ref=fresh.surface_ref,
                )

            session_cwd = _resolve_resume_cwd(fresh, client)
            extra_args, permission_mode = _resume_spawn_args(fresh, client)
            # Every message still unconsumed, not just the one that
            # triggered this call -- at-least-once mailbox semantics
            # (#2212 review finding 1). Falls back to the triggering
            # message itself only if the inbox somehow shows nothing
            # unconsumed (e.g. a caller that never durably queued it).
            unconsumed = read_unconsumed(fresh.id) or [message]

            new_short_id = daemon.spawn_bg(
                cwd=session_cwd,
                prompt=_build_resume_prompt(unconsumed),
                extra_args=extra_args or None,
                permission_mode=permission_mode,
            )
            try:
                _verify_roster_registration(
                    daemon,
                    new_short_id,
                    timeout=self._roster_poll_timeout,
                    interval=self._roster_poll_interval,
                )
            except CwError:
                # Registration failed after the process was already spawned
                # -- stop it rather than leaving an orphan the operator's
                # `delivered=False` result gives no hint even exists (#2212
                # review finding 3). Best-effort: a failed cleanup must not
                # mask the original registration error.
                with contextlib.suppress(Exception):
                    daemon.stop(new_short_id)
                raise

            def _update(state: CwState) -> None:
                live = state.find_by_name_or_id(session.id)
                if live is not None:
                    live.surface_ref = new_short_id
                    live.status = SessionStatus.ACTIVE
                    live.resumed_at = datetime.now(UTC)

            mutate_state(_update)
            # The history-bus SESSION_RESUMED record every daemon respawn
            # already emits (session.py:530-539). Distinct from
            # cw.events.record_event, which the CLI layer emits for the
            # inbox append -- same name, different module, different
            # signature, neither substituting for the other.
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
            advance_cursor(fresh.id, unconsumed[-1].id)

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
