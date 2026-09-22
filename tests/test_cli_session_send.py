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
from cw.session_resume_trigger import FakeResumeTriggerAdapter, ResumeTriggerResult
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, Session

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


def _invoke(
    runner: CliRunner,
    args: list[str],
    adapter: FakeResumeTriggerAdapter | None = None,
) -> tuple[int, str, FakeResumeTriggerAdapter]:
    fake = adapter or FakeResumeTriggerAdapter()
    with patch(
        "cw.cli.session_send.get_resume_trigger_adapter", return_value=fake
    ):
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
