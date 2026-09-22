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

    def test_defaults_to_the_real_daemon_client(self) -> None:
        with patch(
            "cw.session_resume_trigger.get_native_daemon_client"
        ) as mock_factory:
            NativeDaemonResumeTriggerAdapter()
        assert mock_factory.call_count == 1

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


class TestResumeTriggerResult:
    def test_is_frozen(self) -> None:
        result = ResumeTriggerResult(delivered=True, reason="ok")
        field = "delivered"  # indirection keeps mypy off a read-only assignment
        with pytest.raises(AttributeError):
            setattr(result, field, False)

    def test_surface_ref_defaults_to_none(self) -> None:
        assert ResumeTriggerResult(delivered=False, reason="r").surface_ref is None
