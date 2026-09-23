"""Tests for ``cw session send`` — the operator's inbound channel (#2212)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cw import session_inbox
from cw.cli import main
from cw.models import (
    CwState,
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
)
from cw.session_resume_trigger import (
    FakeResumeTriggerAdapter,
    NativeDaemonResumeTriggerAdapter,
    ResumeTriggerResult,
)
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, Session
    from cw.native_daemon import FakeNativeDaemonClient

_SESSION_ID = "sess2212"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _session(sample_client: ClientConfig, **overrides: object) -> Session:
    kwargs: dict[str, object] = {
        "id": _SESSION_ID,
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


def _persist(session: Session) -> None:
    from cw.config import save_state

    save_state(CwState(sessions=[session]))


def _persist_multiple(sessions: list[Session]) -> None:
    from cw.config import save_state

    save_state(CwState(sessions=sessions))


def _write_clients_file(tmp_config_dir: Path, sample_client: ClientConfig) -> None:
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        "clients:\n"
        "  test-client:\n"
        f"    workspace_path: {sample_client.workspace_path}\n"
    )


def _invoke(
    runner: CliRunner,
    args: list[str],
    adapter: FakeResumeTriggerAdapter | None = None,
) -> tuple[int, str, FakeResumeTriggerAdapter]:
    fake = adapter or FakeResumeTriggerAdapter()
    with patch("cw.cli.session_send.get_resume_trigger_adapter", return_value=fake):
        result = runner.invoke(main, ["session", "send", *args])
    return result.exit_code, result.output, fake


class TestHappyPath:
    def test_queues_the_message_and_triggers_the_adapter(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        session = _session(sample_client)
        _persist(session)

        code, output, fake = _invoke(runner, [_SESSION_ID, "--message", "go ahead"])

        assert code == 0
        messages = session_inbox.read_messages(_SESSION_ID)
        assert [m.body for m in messages] == ["go ahead"]
        assert fake.trigger_calls == [(_SESSION_ID, messages[0].id)]
        assert "queued" in output.lower()

    def test_records_the_session_message_sent_event(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        session = _session(sample_client)
        _persist(session)

        with patch("cw.cli.session_send.record_event") as mock_record:
            _invoke(runner, [_SESSION_ID, "--message", "go ahead"])

        assert mock_record.call_count == 1
        event_type, payload = mock_record.call_args.args
        assert event_type is OrchestratorEventType.SESSION_MESSAGE_SENT
        message = session_inbox.read_messages(_SESSION_ID)[0]
        assert payload == {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "message_id": message.id,
            "author": message.author,
        }

    def test_message_file_is_read_from_disk(
        self, sample_client: ClientConfig, runner: CliRunner, tmp_path: Path
    ) -> None:
        _persist(_session(sample_client))
        body_file = tmp_path / "answer.md"
        body_file.write_text("use the second approach\n")

        code, _output, _fake = _invoke(
            runner, [_SESSION_ID, "--message-file", str(body_file)]
        )

        assert code == 0
        assert [m.body for m in session_inbox.read_messages(_SESSION_ID)] == [
            "use the second approach"
        ]

    def test_author_defaults_to_the_local_os_user(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        with patch("cw.cli.session_send.getuser", return_value="localuser"):
            _invoke(runner, [_SESSION_ID, "--message", "hi"])
        assert session_inbox.read_messages(_SESSION_ID)[0].author == "localuser"

    def test_author_option_is_recorded_verbatim(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        _invoke(runner, [_SESSION_ID, "--message", "hi", "--author", "Matt W"])
        assert session_inbox.read_messages(_SESSION_ID)[0].author == "Matt W"

    def test_resolves_a_session_id_prefix(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        code, _output, fake = _invoke(runner, ["sess22", "--message", "hi"])
        assert code == 0
        assert len(fake.trigger_calls) == 1


class TestNonDeliveryIsStillExitZero:
    def test_gate_refusal_warns_but_exits_zero(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """Durable queuing, not delivery, is this command's reliability bar."""
        _persist(_session(sample_client, status=SessionStatus.ACTIVE))
        refusal = ResumeTriggerResult(
            delivered=False,
            reason="session is live and mid-task; deferred to #2255",
        )

        code, output, _fake = _invoke(
            runner,
            [_SESSION_ID, "--message", "hi"],
            adapter=FakeResumeTriggerAdapter(result=refusal),
        )

        assert code == 0
        assert [m.body for m in session_inbox.read_messages(_SESSION_ID)] == ["hi"]
        assert "#2255" in output
        assert "Warning" in output

    def test_adapter_failure_warns_but_exits_zero(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        failure = ResumeTriggerResult(
            delivered=False, reason="daemon respawn failed: usage limit"
        )

        code, output, _fake = _invoke(
            runner,
            [_SESSION_ID, "--message", "hi"],
            adapter=FakeResumeTriggerAdapter(result=failure),
        )

        assert code == 0
        assert session_inbox.read_messages(_SESSION_ID)
        assert "usage limit" in output

    def test_event_is_recorded_even_when_not_delivered(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client, status=SessionStatus.ACTIVE))
        refusal = ResumeTriggerResult(delivered=False, reason="deferred to #2255")
        with patch("cw.cli.session_send.record_event") as mock_record:
            _invoke(
                runner,
                [_SESSION_ID, "--message", "hi"],
                adapter=FakeResumeTriggerAdapter(result=refusal),
            )
        assert mock_record.call_count == 1


class TestRefusals:
    def test_unknown_session_exits_one_and_writes_nothing(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))

        code, output, fake = _invoke(runner, ["nosuchid", "--message", "hi"])

        assert code == 1
        assert "not found" in output.lower()
        assert fake.trigger_calls == []
        assert session_inbox.read_messages("nosuchid") == []
        assert session_inbox.read_messages(_SESSION_ID) == []

    @pytest.mark.parametrize(
        "status", [SessionStatus.COMPLETED, SessionStatus.TIMED_OUT]
    )
    def test_terminal_session_is_refused(
        self,
        sample_client: ClientConfig,
        runner: CliRunner,
        status: SessionStatus,
    ) -> None:
        _persist(_session(sample_client, status=status))

        code, output, fake = _invoke(runner, [_SESSION_ID, "--message", "hi"])

        assert code == 1
        assert status.value in output
        assert fake.trigger_calls == []
        assert session_inbox.read_messages(_SESSION_ID) == []

    def test_neither_message_nor_message_file_is_refused(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        code, output, _fake = _invoke(runner, [_SESSION_ID])
        assert code != 0
        assert "--message" in output
        assert session_inbox.read_messages(_SESSION_ID) == []

    def test_both_message_and_message_file_is_refused(
        self, sample_client: ClientConfig, runner: CliRunner, tmp_path: Path
    ) -> None:
        _persist(_session(sample_client))
        body_file = tmp_path / "answer.md"
        body_file.write_text("from file")

        code, output, _fake = _invoke(
            runner,
            [_SESSION_ID, "--message", "inline", "--message-file", str(body_file)],
        )

        assert code != 0
        assert "--message" in output
        assert session_inbox.read_messages(_SESSION_ID) == []

    def test_empty_message_is_refused(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist(_session(sample_client))
        code, _output, _fake = _invoke(runner, [_SESSION_ID, "--message", "   "])
        assert code != 0
        assert session_inbox.read_messages(_SESSION_ID) == []

    def test_missing_message_file_is_refused(
        self, sample_client: ClientConfig, runner: CliRunner, tmp_path: Path
    ) -> None:
        _persist(_session(sample_client))
        code, _output, _fake = _invoke(
            runner, [_SESSION_ID, "--message-file", str(tmp_path / "absent.md")]
        )
        assert code != 0
        assert session_inbox.read_messages(_SESSION_ID) == []

    def test_ambiguous_session_prefix_is_refused_before_mutating_anything(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """A mutating command must not silently pick whichever session
        sorts first on a colliding prefix (#2212 review finding 2)."""
        _persist_multiple(
            [
                _session(sample_client, id="sess2212a", name="test-client/a/2212"),
                _session(sample_client, id="sess2212b", name="test-client/b/2212"),
            ]
        )

        code, output, fake = _invoke(runner, ["sess2212", "--message", "hi"])

        assert code == 1
        assert "ambiguous" in output.lower()
        assert "sess2212a" in output
        assert "sess2212b" in output
        assert fake.trigger_calls == []
        assert session_inbox.read_messages("sess2212a") == []
        assert session_inbox.read_messages("sess2212b") == []

    def test_unambiguous_longer_prefix_still_resolves(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        _persist_multiple(
            [
                _session(sample_client, id="sess2212a", name="test-client/a/2212"),
                _session(sample_client, id="sess2212b", name="test-client/b/2212"),
            ]
        )

        code, _output, fake = _invoke(runner, ["sess2212a", "--message", "hi"])

        assert code == 0
        assert len(fake.trigger_calls) == 1


class TestEventBusResilience:
    def test_event_write_failure_does_not_fail_an_already_queued_message(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """OSError from record_event must not turn a durably-queued message
        into a command failure (#2212 review finding 4)."""
        _persist(_session(sample_client))

        with patch(
            "cw.cli.session_send.record_event", side_effect=OSError("disk full")
        ):
            code, output, fake = _invoke(runner, [_SESSION_ID, "--message", "hi"])

        assert code == 0
        assert [m.body for m in session_inbox.read_messages(_SESSION_ID)] == ["hi"]
        assert len(fake.trigger_calls) == 1
        assert "queued" in output.lower()


class TestSingleStateResolution:
    def test_ambiguity_check_and_resolution_share_one_state_load(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """Two separate load_state() reads previously let the ambiguity
        check and the actual resolution judge two different snapshots of
        the world (#2212 review round 4, finding 3) -- assert there is only
        one read now."""
        from cw.config import load_state as real_load_state

        _persist(_session(sample_client))

        with patch(
            "cw.cli.session_send.load_state", side_effect=real_load_state
        ) as mock_load_state:
            code, _output, _fake = _invoke(runner, [_SESSION_ID, "--message", "hi"])

        assert code == 0
        assert mock_load_state.call_count == 1

    def test_archived_terminal_session_still_gets_the_friendly_refusal_message(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """A prefix matching only an archived terminal session must still
        get the friendlier "it will never read an inbox message" refusal,
        not a generic "not found" -- the caveat the operator asked to
        preserve when resolution was collapsed to a single read (#2212
        review round 4, finding 3 caveat)."""
        from freezegun import freeze_time

        from cw.config import load_state as real_load_state
        from cw.session_retention import prune_sessions

        session = _session(
            sample_client,
            status=SessionStatus.COMPLETED,
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            completed_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        _persist(session)
        with freeze_time(datetime(2026, 9, 22, tzinfo=UTC)):
            prune_sessions()
        assert real_load_state().sessions == []

        code, output, fake = _invoke(runner, [_SESSION_ID, "--message", "hi"])

        assert code == 1
        assert "will never read an inbox message" in output
        assert "completed" in output.lower()
        assert fake.trigger_calls == []
        assert session_inbox.read_messages(_SESSION_ID) == []


class TestMailboxDeliveryEndToEnd:
    def test_send_to_a_parked_session_is_visible_after_respawn(
        self,
        sample_client: ClientConfig,
        runner: CliRunner,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        """The real adapter, not the Fake -- proves the send -> queue ->
        respawn -> consume path end-to-end with no hand-called
        advance_cursor anywhere in this test (#2212 review finding 1)."""
        _write_clients_file(tmp_config_dir, sample_client)
        session = _session(sample_client)
        _persist(session)
        real_adapter = NativeDaemonResumeTriggerAdapter(
            native_daemon=mock_native_daemon
        )

        with patch(
            "cw.cli.session_send.get_resume_trigger_adapter",
            return_value=real_adapter,
        ):
            result = runner.invoke(
                main,
                [
                    "session",
                    "send",
                    _SESSION_ID,
                    "--message",
                    "use the second approach",
                ],
            )

        assert result.exit_code == 0
        assert session_inbox.read_unconsumed(_SESSION_ID) == []
        _cwd, prompt = mock_native_daemon.spawn_calls[0]
        assert "use the second approach" in prompt
