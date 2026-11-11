"""Sample client, session and state fixtures, the native-daemon fake, the
routed-orphan and post-launch-attention doubles, and event capture. A
registered plugin. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.spawn import SPAWN_POST_LAUNCH_FAILED_REASON

if TYPE_CHECKING:
    from collections.abc import Callable


# A captured record_event invocation: (event_type, payload, correlation_id).
CapturedEvent = tuple[OrchestratorEventType, dict[str, Any], str | None]


@pytest.fixture
def sample_client(tmp_path: Path) -> ClientConfig:
    """A ClientConfig pointing at tmp_path."""
    workspace = tmp_path / "workspace" / "test-project"
    workspace.mkdir(parents=True)
    return ClientConfig(
        name="test-client",
        workspace_path=workspace,
        default_branch="main",
    )


@pytest.fixture
def sample_session(sample_client: ClientConfig) -> Session:
    """A Session with known values."""
    return Session(
        id="abcd1234",
        name="test-client/impl",
        client="test-client",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=sample_client.workspace_path,
        surface_ref="impl",
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_state(sample_client: ClientConfig) -> CwState:
    """A CwState with a mix of active/backgrounded/completed sessions."""
    return CwState(
        sessions=[
            Session(
                id="sess0001",
                name="test-client/impl",
                client="test-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="sess0002",
                name="test-client/idea",
                client="test-client",
                purpose=SessionPurpose.IDEA,
                status=SessionStatus.BACKGROUNDED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 9, 0, 0, tzinfo=UTC),
                backgrounded_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="sess0003",
                name="other-client/impl",
                client="other-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.COMPLETED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 14, 8, 0, 0, tzinfo=UTC),
            ),
        ]
    )


@pytest.fixture
def mock_native_daemon() -> FakeNativeDaemonClient:
    """A FakeNativeDaemonClient for testing daemon-origin spawn and reconcile."""
    return FakeNativeDaemonClient()


def _stop_leaves_worker_listed(
    monkeypatch: pytest.MonkeyPatch, fake: FakeNativeDaemonClient
) -> None:
    """Make *fake*'s ``stop`` record the call but never drop the worker.

    The "``claude stop`` returned yet the roster still lists the worker"
    shape: :meth:`NativeDaemonClient.stop` swallows every failure, so a
    caller that must know the worker is gone has to confirm it in the roster
    (#2324, #2517). One shared fake for every such test (N6).
    """
    monkeypatch.setattr(fake, "stop", fake.stop_calls.append)


def _stop_makes_roster_unreadable(
    monkeypatch: pytest.MonkeyPatch, fake: FakeNativeDaemonClient
) -> None:
    """Make *fake*'s ``stop`` record the call and leave the roster unreadable.

    The worker stays listed and every fail-closed roster read afterwards
    returns ``None``, so the stop can never be confirmed (#2517).
    """

    def _stop(short_id: str) -> None:
        fake.stop_calls.append(short_id)
        fake.roster_unreadable = True

    monkeypatch.setattr(fake, "stop", _stop)


_ROUTED_ORPHAN_CLOSE_MODULE = "cw.cli.routed_orphan_close"


@pytest.fixture
def instant_routed_roster_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the routed-orphan close's stop-confirm wait and corroboration gap.

    Shared by the approve/requeue close tests and the named
    ``cw doctor --reap --routed-session-id`` tests (#2517, #2708), so a roster
    poll never sleeps and a frozen clock cannot hang it.
    """
    monkeypatch.setattr(
        f"{_ROUTED_ORPHAN_CLOSE_MODULE}._ROUTED_STOP_CONFIRM_TIMEOUT_SECS", 0.0
    )
    monkeypatch.setattr(
        f"{_ROUTED_ORPHAN_CLOSE_MODULE}._ROUTED_STOP_CONFIRM_INTERVAL_SECS", 0.0
    )


@pytest.fixture
def orphan_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """``caplog`` capturing the routed-orphan close WARNING lines (never INFO).

    Covers both the shared close (``cw.cli.routed_orphan_close``) and the named
    entry point (``cw.cli.routed_named_close``, #2708).
    """
    caplog.set_level(logging.WARNING, logger=_ROUTED_ORPHAN_CLOSE_MODULE)
    caplog.set_level(logging.WARNING, logger="cw.cli.routed_named_close")
    return caplog


# The canonical SESSION_NEEDS_ATTENTION payload keys (docs/events.md), with
# ``lane`` as the ninth.
_CANONICAL_ATTENTION_KEYS: frozenset[str] = frozenset(
    {
        "session_id",
        "session_name",
        "client",
        "ticket_id",
        "claude_session_id",
        "paused_status",
        "breadcrumbs",
        "crashed",
        "lane",
    }
)


@pytest.fixture
def fail_state_write_after_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``cw.spawn``'s post-launch ``save_state`` raise ``OSError`` (#2502).

    Opt-in. ``spawn_create_impl`` calls ``save_state`` only inside its
    post-launch ``sessions_lock`` block, after ``daemon.spawn_bg`` returned, so
    the raise lands with a live worker and no ``Session`` row.
    """

    def _raise(_state: object) -> None:
        msg = "simulated sessions.json write failure"
        raise OSError(msg)

    monkeypatch.setattr("cw.spawn.save_state", _raise)


def post_launch_attention_payload(events: list[CapturedEvent]) -> dict[str, Any]:
    """Return the one ``spawn_post_launch_failed`` page in *events* (#2502).

    Asserts there is exactly one ``SESSION_NEEDS_ATTENTION``, that it carries
    exactly the canonical nine keys, and that it is the post-launch page.
    """
    pages = [
        payload
        for etype, payload, _ in events
        if etype == OrchestratorEventType.SESSION_NEEDS_ATTENTION
    ]
    assert len(pages) == 1, pages
    payload = pages[0]
    assert set(payload) == _CANONICAL_ATTENTION_KEYS
    assert payload["paused_status"] == SPAWN_POST_LAUNCH_FAILED_REASON
    assert payload["crashed"] is False
    return payload


def fire_and_forget_breadcrumb(sid: str, backend: str, pid: int, cause: str) -> str:
    """Return the exact breadcrumb of a fire-and-forget post-launch page (#2592).

    An independent literal of the operator-facing contract: it imports nothing
    from ``src``, so a regression there cannot move the expected text with it.
    *cause* is the full cause text (``"OSError: disk full"``); nothing is
    truncated, so callers pass short causes.
    """
    if backend in ("aider", "opencode"):
        return (
            f"worker live (session {sid}): {backend} pid {pid} is still running"
            " and its liveness handle was not recorded. Recovery: stop that"
            f" process, then run `cw spawn close {sid}`. `cw spawn close` alone"
            " does not stop the process, and requeueing before it is stopped"
            f" starts a second worker on the same worktree. Cause: {cause}"
        )
    if backend == "codex":
        return (
            f"worker live (session {sid}): codex pid {pid} is still running and"
            " its liveness handle was not recorded. Recovery: a codex job"
            " completes its own session when it finishes, so do nothing while"
            " that pid is alive and making progress. If the pid is gone or"
            f" hung, stop it, then run `cw spawn close {sid}`. Stopping a"
            " healthy job kills an in-flight review, and `cw spawn close`"
            f" cancels the row. Cause: {cause}"
        )
    msg = f"no fire-and-forget breadcrumb for backend {backend!r}"
    raise ValueError(msg)


@pytest.fixture
def capture_events(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., list[CapturedEvent]]:
    """Patch ``record_event`` on an arbitrary module and capture its calls.

    ``monkeypatch.setattr`` patches a name by the *calling* module's binding,
    so a test that needs to observe events emitted from ``cw.dev_queue`` must
    patch ``cw.dev_queue.record_event`` — the ``capture_event`` closures in
    ``test_dispatch.py`` that patch ``cw.dispatch.routing.record_event`` will
    NOT see events emitted from ``cw.dev_queue``. This factory patches
    ``<module_path>.record_event`` and returns a list that accumulates
    ``(event_type, payload, correlation_id)`` tuples for each emit, optionally
    filtered to a single ``event_type``.

    Call it once per module you want to observe; a test that spans two producer
    modules (e.g. the dispatch finalize-regress path, which emits from both
    ``cw.dispatch.routing.stage_failure`` and ``cw.dev_queue``) calls it twice
    with distinct lists.
    """

    def _factory(
        module_path: str,
        event_type: OrchestratorEventType | None = None,
    ) -> list[CapturedEvent]:
        captured: list[CapturedEvent] = []

        def _capture(
            etype: OrchestratorEventType,
            payload: dict[str, Any] | None = None,
            *,
            correlation_id: str | None = None,
        ) -> None:
            if event_type is None or etype == event_type:
                captured.append((etype, payload or {}, correlation_id))

        monkeypatch.setattr(f"{module_path}.record_event", _capture)
        return captured

    return _factory
