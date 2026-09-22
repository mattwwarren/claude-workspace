"""Tests for cw.session_resume_trigger — the idle/paused resume trigger (#2212)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.config import load_state, save_state
from cw.models import (
    CwState,
    QueueItemStatus,
    SessionInboxMessage,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.session_resume_trigger import (
    DEFERRED_LIVE_DELIVERY_REASON,
    FakeResumeTriggerAdapter,
    NativeDaemonResumeTriggerAdapter,
    ResumeTriggerAdapter,
    ResumeTriggerResult,
    get_resume_trigger_adapter,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, Session

_MESSAGE = SessionInboxMessage(
    id="msg00001",
    created_at=datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC),
    author="matt",
    body="Yes, go ahead and use the second approach.",
)


def _write_clients_file(tmp_config_dir: Path, sample_client: ClientConfig) -> None:
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        "clients:\n"
        "  test-client:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )


def _persist(session: Session) -> None:
    save_state(CwState(sessions=[session]))


def _eligible_session(sample_client: ClientConfig, **overrides: object) -> Session:
    kwargs: dict[str, object] = {
        "id": "sess2212",
        "name": "test-client/auto-dev/2212",
        "client": "test-client",
        "origin": SessionOrigin.DAEMON,
        "status": SessionStatus.IDLE,
        "workspace_path": sample_client.workspace_path,
        "worktree_path": sample_client.workspace_path,
        "surface_ref": "deadbeef",
        "claude_session_id": "aaaa1111-0000-0000-0000-000000000000",
        "started_at": datetime(2026, 9, 22, 11, 0, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return _make_daemon_session(**kwargs)


# ---------------------------------------------------------------------------
# FakeResumeTriggerAdapter
# ---------------------------------------------------------------------------


class TestFakeResumeTriggerAdapter:
    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(FakeResumeTriggerAdapter(), ResumeTriggerAdapter)

    def test_records_calls(self, sample_client: ClientConfig) -> None:
        fake = FakeResumeTriggerAdapter()
        session = _eligible_session(sample_client)
        fake.trigger(session, _MESSAGE)
        assert fake.trigger_calls == [(session.id, _MESSAGE.id)]

    def test_default_result_is_delivered(self, sample_client: ClientConfig) -> None:
        result = FakeResumeTriggerAdapter().trigger(
            _eligible_session(sample_client), _MESSAGE
        )
        assert result.delivered is True

    def test_injected_result_is_returned(self, sample_client: ClientConfig) -> None:
        canned = ResumeTriggerResult(delivered=False, reason="nope")
        fake = FakeResumeTriggerAdapter(result=canned)
        assert fake.trigger(_eligible_session(sample_client), _MESSAGE) == canned


class TestFactory:
    def test_returns_the_native_daemon_adapter(self) -> None:
        assert isinstance(
            get_resume_trigger_adapter(), NativeDaemonResumeTriggerAdapter
        )

    def test_factory_result_satisfies_the_protocol(self) -> None:
        assert isinstance(get_resume_trigger_adapter(), ResumeTriggerAdapter)


# ---------------------------------------------------------------------------
# Gate: eligibility is checked before the daemon is touched at all
# ---------------------------------------------------------------------------


class TestGate:
    def test_running_task_and_active_session_is_refused(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, status=SessionStatus.ACTIVE)
        _persist(session)
        task = _make_ticket_task(
            ticket_id="2212",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[task]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert result.reason == DEFERRED_LIVE_DELIVERY_REASON
        assert "#2255" in result.reason
        # Regression guard: the deferred case must never reach the daemon.
        assert mock_native_daemon.spawn_calls == []
        assert mock_native_daemon.stop_calls == []

    def test_blocked_on_user_task_is_eligible_even_when_session_is_active(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """_route_stopped_without_sentinel never touches Session.status, so a
        parked row's session can still read ACTIVE — the two signals are OR'd.
        """
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, status=SessionStatus.ACTIVE)
        _persist(session)
        task = _make_ticket_task(
            ticket_id="2212",
            client="test-client",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=session.id,
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[task]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is True
        assert len(mock_native_daemon.spawn_calls) == 1

    def test_idle_session_with_no_owning_task_is_eligible(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, status=SessionStatus.IDLE)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is True
        assert len(mock_native_daemon.spawn_calls) == 1

    def test_another_sessions_blocked_task_does_not_grant_eligibility(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, status=SessionStatus.ACTIVE)
        _persist(session)
        other = _make_ticket_task(
            ticket_id="9999",
            client="test-client",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id="othersid",
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[other]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert mock_native_daemon.spawn_calls == []


# ---------------------------------------------------------------------------
# NativeDaemonResumeTriggerAdapter: the respawn composition
# ---------------------------------------------------------------------------


class TestNativeDaemonResumeTriggerAdapter:
    def _trigger(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        daemon: FakeNativeDaemonClient,
        session: Session,
    ) -> ResumeTriggerResult:
        _write_clients_file(tmp_config_dir, sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=daemon)
        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            return adapter.trigger(session, _MESSAGE)

    def test_spawns_with_resume_extra_args_and_never_stops(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        result = self._trigger(
            tmp_config_dir, sample_client, mock_native_daemon, session
        )

        assert result.delivered is True
        extra_args = mock_native_daemon.spawn_extra_args[0]
        assert extra_args is not None
        assert extra_args[:2] == ["--resume", session.claude_session_id]
        # The gate makes a stop() unnecessary; resume_session's dead-surface
        # branch does not call it either.
        assert mock_native_daemon.stop_calls == []

    def test_spawn_cwd_is_the_resolved_resume_cwd(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        self._trigger(tmp_config_dir, sample_client, mock_native_daemon, session)
        cwd, _prompt = mock_native_daemon.spawn_calls[0]
        assert cwd == session.worktree_path

    def test_prompt_carries_the_queued_message_body(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        self._trigger(tmp_config_dir, sample_client, mock_native_daemon, session)
        _cwd, prompt = mock_native_daemon.spawn_calls[0]
        assert _MESSAGE.body in prompt
        assert _MESSAGE.author in prompt

    def test_verifies_roster_registration_with_the_new_short_id(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        _write_clients_file(tmp_config_dir, sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)
        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch(
                "cw.session_resume_trigger._verify_roster_registration"
            ) as mock_verify,
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is True
        assert mock_verify.call_args.args[1] == result.surface_ref

    def test_updates_session_surface_ref_status_and_resumed_at(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        result = self._trigger(
            tmp_config_dir, sample_client, mock_native_daemon, session
        )

        updated = load_state().sessions[0]
        assert updated.surface_ref == result.surface_ref
        assert updated.status == SessionStatus.ACTIVE
        assert updated.resumed_at is not None

    def test_emits_the_session_resumed_history_event(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client)
        _write_clients_file(tmp_config_dir, sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)
        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch("cw.session_resume_trigger.record_event") as mock_record,
        ):
            adapter.trigger(session, _MESSAGE)

        assert mock_record.call_count == 1
        client_arg, event = mock_record.call_args.args
        assert client_arg == session.client
        assert event.event_type.value == "session_resumed"
        assert event.session_id == session.id

    def test_missing_claude_session_id_is_refused_without_spawning(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        session = _eligible_session(sample_client, claude_session_id=None)
        result = self._trigger(
            tmp_config_dir, sample_client, mock_native_daemon, session
        )

        assert result.delivered is False
        assert "transcript" in result.reason
        assert mock_native_daemon.spawn_calls == []

    def test_spawn_failure_reports_not_delivered(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        mock_native_daemon.raise_usage_limit = True
        session = _eligible_session(sample_client)
        result = self._trigger(
            tmp_config_dir, sample_client, mock_native_daemon, session
        )

        assert result.delivered is False
        assert result.reason
        assert load_state().sessions[0].surface_ref == "deadbeef"

    def test_does_not_resolve_the_daemon_client_at_construction(self) -> None:
        """Lazy: resolved inside _respawn, not __init__ (#2212 review finding 6)."""
        with patch(
            "cw.session_resume_trigger.get_native_daemon_client"
        ) as mock_factory:
            NativeDaemonResumeTriggerAdapter()
        assert mock_factory.call_count == 0

    def test_defaults_to_the_real_daemon_client_on_trigger(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter()
        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch(
                "cw.session_resume_trigger.get_native_daemon_client",
                return_value=mock_native_daemon,
            ) as mock_factory,
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert mock_factory.call_count == 1
        assert result.delivered is True

    def test_corrupted_daemon_session_state_is_refused_not_raised(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """_resolve_resume_cwd raises CwError for a DAEMON session with no
        worktree_path (#940); the trigger reports it rather than exploding the
        operator's ``cw session send``.
        """
        session = _eligible_session(sample_client, worktree_path=None)
        result = self._trigger(
            tmp_config_dir, sample_client, mock_native_daemon, session
        )

        assert result.delivered is False
        assert mock_native_daemon.spawn_calls == []


# ---------------------------------------------------------------------------
# The mailbox reader wiring (#2212 review finding 1): _respawn is the
# production consumer of read_unconsumed/advance_cursor, and delivery is
# proven without ever hand-calling advance_cursor -- that would just be
# re-simulating the consumer this section exists to exercise for real.
# ---------------------------------------------------------------------------


class TestMailboxReaderWiring:
    def test_a_queued_message_is_visible_to_the_session_after_respawn(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """End-to-end: append -> trigger -> the message is consumed.

        No hand-advance of the cursor anywhere in this test -- delivery is
        proven by ``_respawn`` alone.
        """
        from cw import session_inbox

        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        queued = session_inbox.append_message(
            session.id, author="matt", body="use the second approach"
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, queued)

        assert result.delivered is True
        assert session_inbox.read_unconsumed(session.id) == []
        _cwd, prompt = mock_native_daemon.spawn_calls[0]
        assert "use the second approach" in prompt

    def test_multiple_unconsumed_messages_are_all_delivered_in_one_respawn(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """A message queued before the session became eligible is not lost."""
        from cw import session_inbox

        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        session_inbox.append_message(session.id, author="matt", body="first answer")
        second = session_inbox.append_message(
            session.id, author="matt", body="second answer"
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, second)

        assert result.delivered is True
        _cwd, prompt = mock_native_daemon.spawn_calls[0]
        assert "first answer" in prompt
        assert "second answer" in prompt
        assert session_inbox.read_unconsumed(session.id) == []

    def test_falls_back_to_the_triggering_message_when_inbox_is_empty(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """A caller that never durably queued the message still gets a prompt."""
        session = _eligible_session(sample_client)
        self._trigger(tmp_config_dir, sample_client, mock_native_daemon, session)
        _cwd, prompt = mock_native_daemon.spawn_calls[0]
        assert _MESSAGE.body in prompt

    def _trigger(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        daemon: FakeNativeDaemonClient,
        session: Session,
    ) -> ResumeTriggerResult:
        _write_clients_file(tmp_config_dir, sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=daemon)
        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            return adapter.trigger(session, _MESSAGE)


# ---------------------------------------------------------------------------
# Concurrent-send guard (#2212 review finding 3): a second trigger for a
# session already live in the daemon must decline, not double-spawn; a
# roster-registration failure after spawn_bg must stop the orphan.
# ---------------------------------------------------------------------------


class TestConcurrentSendGuard:
    def test_session_vanished_between_outer_check_and_the_lock_is_refused(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """The outer session object was resolved before the lock; if it is
        gone from state by the time _respawn reloads it fresh, decline
        rather than spawning against stale data."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        # Deliberately not persisted: `load_state()` inside `_respawn`
        # therefore finds no matching session.
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert mock_native_daemon.spawn_calls == []

    def test_transcript_lost_between_outer_check_and_the_lock_is_refused(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(_eligible_session(sample_client, claude_session_id=None))
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert mock_native_daemon.spawn_calls == []

    def test_already_live_surface_declines_without_double_spawning(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """Simulates the race: a concurrent trigger already resumed this
        session (its new surface is live in the daemon) by the time this
        call reaches the per-session lock."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, surface_ref="alreadylive")
        _persist(session)
        mock_native_daemon._live.add("alreadylive")
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert result.surface_ref == "alreadylive"
        assert mock_native_daemon.spawn_calls == []

    def test_task_no_longer_blocked_between_outer_check_and_lock_is_refused(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """The outer gate in trigger() and the recheck inside the lock must
        both see BLOCKED_ON_USER -- a task that moved on between them (e.g.
        an operator requeue racing a send) must not let a stale outer pass
        smuggle a respawn through (#2212 review round 2, finding 1/2)."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client, status=SessionStatus.ACTIVE)
        _persist(session)
        blocked_task = _make_ticket_task(
            ticket_id="2212",
            client="test-client",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=session.id,
        )
        running_task = _make_ticket_task(
            ticket_id="2212",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
        )
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with patch(
            "cw.session_resume_trigger.list_tickets",
            side_effect=[[blocked_task], [running_task]],
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert result.reason == DEFERRED_LIVE_DELIVERY_REASON
        assert mock_native_daemon.spawn_calls == []

    def test_roster_registration_failure_stops_the_orphaned_process(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """spawn_bg succeeds but the worker never registers -- the orphan
        must be stopped, not left running with delivered=False and no
        indication anything was ever spawned (#2212 review finding 3)."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        mock_native_daemon.raise_unregistered = True
        adapter = NativeDaemonResumeTriggerAdapter(
            native_daemon=mock_native_daemon, roster_poll_timeout=0.0
        )

        with patch("cw.session_resume_trigger.list_tickets", return_value=[]):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert len(mock_native_daemon.spawn_calls) == 1
        spawned_short_id = f"{mock_native_daemon._counter:08x}"
        assert mock_native_daemon.stop_calls == [spawned_short_id]
        # State must not have been committed for a respawn that never
        # actually registered.
        assert load_state().sessions[0].surface_ref == "deadbeef"


# ---------------------------------------------------------------------------
# Scoped post-spawn compensation (#2212 review round 2, finding 3): a
# pre-commit failure (roster registration above, or mutate_state itself)
# stops the orphaned process; a post-commit failure (the history event or
# the mailbox cursor) must NOT stop a surface that is now the authoritative,
# genuinely-live state -- see docs/adr/0017-session-inbox-and-resume-trigger.md.
# ---------------------------------------------------------------------------


class TestPostSpawnCompensation:
    def test_mutate_state_failure_before_commit_stops_the_orphaned_process(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """mutate_state failing means nothing committed -- the same
        orphan-stop compensation as a roster-verify failure applies."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch(
                "cw.session_resume_trigger.mutate_state",
                side_effect=OSError("disk full"),
            ),
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is False
        assert "disk full" in result.reason
        spawned_short_id = f"{mock_native_daemon._counter:08x}"
        assert mock_native_daemon.stop_calls == [spawned_short_id]
        # mutate_state was replaced entirely, so the real save never ran.
        assert load_state().sessions[0].surface_ref == "deadbeef"

    def test_post_commit_event_write_failure_does_not_stop_the_surface(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """Once mutate_state commits, the surface is genuinely live -- a
        failure recording SESSION_RESUMED or advancing the cursor must not
        stop it (that would manufacture a phantom): accept it, log it, and
        surface it in the reason, but still report delivered=True."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch(
                "cw.session_resume_trigger.record_event",
                side_effect=OSError("history disk full"),
            ),
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is True
        assert "post-commit" in result.reason
        assert mock_native_daemon.stop_calls == []
        updated = load_state().sessions[0]
        assert updated.status == SessionStatus.ACTIVE
        assert updated.surface_ref == result.surface_ref

    def test_post_commit_cursor_write_failure_does_not_stop_the_surface(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """Same as the history-event case, but the failure is in the
        mailbox cursor write instead -- both live in the same try block and
        must both be tolerated post-commit."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _eligible_session(sample_client)
        _persist(session)
        adapter = NativeDaemonResumeTriggerAdapter(native_daemon=mock_native_daemon)

        with (
            patch("cw.session_resume_trigger.list_tickets", return_value=[]),
            patch(
                "cw.session_resume_trigger.advance_cursor",
                side_effect=OSError("cursor disk full"),
            ),
        ):
            result = adapter.trigger(session, _MESSAGE)

        assert result.delivered is True
        assert "post-commit" in result.reason
        assert mock_native_daemon.stop_calls == []
        assert load_state().sessions[0].status == SessionStatus.ACTIVE


class TestResumeTriggerResult:
    def test_is_frozen(self) -> None:
        result = ResumeTriggerResult(delivered=True, reason="ok")
        field = "delivered"  # indirection keeps mypy off a read-only assignment
        with pytest.raises(AttributeError):
            setattr(result, field, False)

    def test_surface_ref_defaults_to_none(self) -> None:
        assert ResumeTriggerResult(delivered=False, reason="r").surface_ref is None
