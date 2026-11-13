"""Tests for the Stop-hook auto-continuation (GitHub #2348).

End-to-end through ``cw signal-stop`` on a headless DAEMON session with a
RUNNING dev-queue row and a sentinel-less transcript. The event is captured by
patching ``cw.cli.stop_hook.continuation.record_event`` (the name that module
binds), so no inbox is read.

Transcript fixtures are written by the helpers below, not copied from a real
capture: the ``tool_result`` / ``is_error`` record shape is the one already
confirmed from a real transcript (see the #2348 plan), but the surrounding
records are sparse hand-written stand-ins. The Stop payload follows the
documented Claude Code hooks reference (Stop section) and is not yet checked
against a captured payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cw.cli.stop_hook._constants import (
    MAX_STOP_CONTINUATIONS,
    STOP_CONTINUATION_NUDGE,
)
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.stage_entry import enter_stage
from cw.models import OrchestratorEventType, QueueItemStatus, SessionStatus, Stage
from cw.models.orchestrator_config.operator_forward import _DEFAULT_OPERATOR_EVENT_TYPES
from cw.models.tasks.store import DevQueueStore
from tests._shared.session_seeding import _make_ticket_task
from tests._shared.stop_hook_context import (
    _invoke_hook_command,
    _seed_session,
    _stop_payload,
    _write_hook_context_file,
    _write_stop_hook_transcript,
)

if TYPE_CHECKING:
    from click.testing import Result

TICKET_ID = "940"
SESSION_ID = "sess940g"
CLAUDE_SESSION_ID = "claude-uuid-2229"
NUDGE_JSON = json.dumps({"decision": "block", "reason": STOP_CONTINUATION_NUDGE})
PLAIN_TEXT = "I have written the tests and will keep going on the fix."


def _assistant(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool_result(*, is_error: bool) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "is_error": is_error,
                    "content": "command failed" if is_error else "ok",
                }
            ],
        },
    }


def _write_transcript(worktree: Path, records: list[dict[str, object]]) -> None:
    _write_stop_hook_transcript(
        Path.home(), worktree, CLAUDE_SESSION_ID, records=records
    )


def _seed_row(
    *,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
    stop_continuations: int | None = None,
) -> None:
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=TICKET_ID,
                    client="client-a",
                    session_id=SESSION_ID,
                    status=status,
                    stage=Stage.IMPL,
                    stop_continuations=stop_continuations,
                )
            ]
        )
    )


def _row_counter() -> int | None:
    return load_dev_queue().tasks[0].stop_continuations


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    session = _seed_session(tmp_path)
    assert session.worktree_path is not None
    _write_hook_context_file(session.worktree_path, headless=True)
    monkeypatch.chdir(session.worktree_path)
    return session.worktree_path


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, dict[str, object]]]:
    events: list[tuple[object, dict[str, object]]] = []

    def _record(
        event_type: object, payload: dict[str, object] | None = None, **_: object
    ) -> None:
        events.append((event_type, payload or {}))

    monkeypatch.setattr("cw.cli.stop_hook.continuation.record_event", _record)
    return events


def _stop(worktree: Path, **extra: object) -> Result:
    payload = _stop_payload(worktree, **extra)
    payload["session_id"] = CLAUDE_SESSION_ID
    return _invoke_hook_command("signal-stop", payload)


def _continued_events(
    recorded: list[tuple[object, dict[str, object]]],
) -> list[dict[str, object]]:
    return [p for t, p in recorded if t is OrchestratorEventType.SESSION_STOP_CONTINUED]


class TestContinuationHappyPath:
    def test_first_continuation_blocks_and_persists(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.exit_code == 0
        assert result.stdout.strip() == NUDGE_JSON
        assert _row_counter() == 1

    def test_continuation_event_payload(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        _stop(worktree, last_assistant_message=PLAIN_TEXT)

        [payload] = _continued_events(recorded)
        assert payload["claude_session_id"] == CLAUDE_SESSION_ID
        assert payload["client"] == "client-a"
        assert payload["session_name"] == "client-a/impl"
        assert payload["session_id"] == SESSION_ID
        assert payload["ticket_id"] == TICKET_ID
        assert payload["stage"] == Stage.IMPL.value
        assert payload["continuation"] == 1
        assert payload["cap"] == MAX_STOP_CONTINUATIONS
        assert "lane" in payload


class TestContinuationCap:
    def test_ordinary_client_continues_without_configuration(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        """No allowlist or rollout config exists: an ordinary client continues."""
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == NUDGE_JSON
        assert _row_counter() == 1

    def test_cap_two_then_falls_through(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        outputs = [
            _stop(worktree, last_assistant_message=PLAIN_TEXT).stdout.strip()
            for _ in range(3)
        ]

        assert outputs == [NUDGE_JSON, NUDGE_JSON, ""]
        assert _row_counter() == MAX_STOP_CONTINUATIONS
        assert len(_continued_events(recorded)) == MAX_STOP_CONTINUATIONS
        assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


class TestContinuationExclusions:
    def test_backgrounded_daemon_session_never_continues(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])
        state = load_state()
        session = next(s for s in state.sessions if s.id == SESSION_ID)
        session.status = SessionStatus.BACKGROUNDED
        save_state(state)

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == ""
        assert _row_counter() is None
        assert _continued_events(recorded) == []

    def test_divergent_transcripts_fail_closed(
        self,
        worktree: Path,
        recorded: list[tuple[object, dict[str, object]]],
        tmp_path: Path,
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])
        alternate = tmp_path / "alternate-worktree"
        alternate.mkdir()
        _write_stop_hook_transcript(
            Path.home(),
            alternate,
            CLAUDE_SESSION_ID,
            records=[_tool_result(is_error=True)],
        )
        state = load_state()
        session = next(s for s in state.sessions if s.id == SESSION_ID)
        session.worktree_path = alternate
        save_state(state)

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == ""
        assert _row_counter() is None
        assert _continued_events(recorded) == []

    def test_blocked_sentinel_never_continues(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        sentinel = (
            '<<<AUTO_DEV_RESULT\n{"status": "blocked", "ticket_id": "940"}\n'
            "AUTO_DEV_RESULT>>>"
        )
        _write_transcript(worktree, [_assistant(sentinel)])

        result = _stop(worktree, last_assistant_message=sentinel)

        assert result.stdout.strip() == ""
        assert _row_counter() is None
        assert _continued_events(recorded) == []

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.CANCELLED,
            QueueItemStatus.COMPLETED,
        ],
    )
    def test_non_running_row_never_continues(
        self,
        worktree: Path,
        recorded: list[tuple[object, dict[str, object]]],
        status: QueueItemStatus,
    ) -> None:
        _seed_row(status=status)
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == ""
        assert _row_counter() is None
        assert _continued_events(recorded) == []

    @pytest.mark.parametrize(
        "message",
        [
            "Usage limit reached. Your limit resets at 5pm.",
            "API Error: 429 rate limited",
        ],
    )
    def test_usage_limit_and_api_error_text_never_continues(
        self,
        worktree: Path,
        recorded: list[tuple[object, dict[str, object]]],
        message: str,
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(message)])

        result = _stop(worktree, last_assistant_message=message)

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_blank_message_never_continues(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant("   ")])

        result = _stop(worktree, last_assistant_message="   ")

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_session_crons_never_continue(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        result = _stop(
            worktree,
            last_assistant_message=PLAIN_TEXT,
            session_crons=[{"id": "wakeup-1"}],
        )

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_background_tasks_never_continue(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        result = _stop(
            worktree,
            last_assistant_message=PLAIN_TEXT,
            background_tasks=[{"id": "bg-1"}],
        )

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_missing_transcript_fails_closed(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_tool_error_final_round_trip_never_continues(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(
            worktree,
            [
                _assistant("running it"),
                _tool_result(is_error=True),
                _assistant(PLAIN_TEXT),
            ],
        )

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == ""
        assert _row_counter() is None

    def test_tool_error_then_recovery_continues(
        self, worktree: Path, recorded: list[tuple[object, dict[str, object]]]
    ) -> None:
        _seed_row()
        _write_transcript(
            worktree,
            [
                _tool_result(is_error=True),
                _assistant("retrying"),
                _tool_result(is_error=False),
                _assistant(PLAIN_TEXT),
            ],
        )

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.stdout.strip() == NUDGE_JSON
        assert _row_counter() == 1


class TestContinuationFailOpen:
    def test_claim_load_error_fails_open(
        self,
        worktree: Path,
        recorded: list[tuple[object, dict[str, object]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_row()
        _write_transcript(worktree, [_assistant(PLAIN_TEXT)])

        def _boom() -> object:
            message = "dev_queue.json unreadable"
            raise OSError(message)

        monkeypatch.setattr("cw.cli.stop_hook.continuation.load_dev_queue", _boom)

        result = _stop(worktree, last_assistant_message=PLAIN_TEXT)

        assert result.exit_code == 0
        assert result.stdout.strip() == ""
        assert _continued_events(recorded) == []
        assert _row_counter() is None


class TestStageReset:
    def test_enter_stage_clears_counter(self) -> None:
        task = _make_ticket_task(stage=Stage.IMPL, stop_continuations=2)

        enter_stage(task, Stage.REVIEW)

        assert task.stop_continuations is None

    def test_fresh_task_starts_unset(self) -> None:
        assert _make_ticket_task().stop_continuations is None


class TestContinuationEventRegistration:
    def test_event_registered_and_not_operator_forwarded(self) -> None:
        assert OrchestratorEventType("session.stop_continued") is (
            OrchestratorEventType.SESSION_STOP_CONTINUED
        )
        assert (
            OrchestratorEventType.SESSION_STOP_CONTINUED
            not in _DEFAULT_OPERATOR_EVENT_TYPES
        )


class TestApiErrorPrefixSingleSource:
    def test_prefix_is_not_redefined_in_stop_hook_constants(self) -> None:
        from cw.cli.stop_hook import _constants, continuation
        from cw.reconcile import liveness_page

        assert not hasattr(_constants, "_API_ERROR_PREFIX")
        assert continuation._API_ERROR_PREFIX is liveness_page._API_ERROR_PREFIX


class TestContinuationHelpersDirect:
    """Fail-closed and race branches reached without a full Stop-hook run."""

    def test_find_transcripts_rejects_unusable_session_id(self, worktree: Path) -> None:
        from cw.cli.stop_hook.continuation import _find_transcripts

        session = _seed_session(worktree.parent, sess_id=SESSION_ID)
        assert _find_transcripts(session, str(worktree), None) == ()
        assert _find_transcripts(session, str(worktree), "") == ()

    def test_malformed_transcript_line_fails_closed(self, tmp_path: Path) -> None:
        from cw.cli.stop_hook.continuation import _turn_ended_in_tool_error

        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "assistant"}\n{"type": "us', encoding="utf-8")
        assert _turn_ended_in_tool_error(transcript) is True

    def test_user_record_without_list_content_resets_flag(self, tmp_path: Path) -> None:
        from cw.cli.stop_hook.continuation import _turn_ended_in_tool_error

        transcript = tmp_path / "t.jsonl"
        records = [
            _tool_result(is_error=True),
            {"type": "user", "message": {"role": "user", "content": "plain prompt"}},
        ]
        transcript.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )
        assert _turn_ended_in_tool_error(transcript) is False

    def test_claim_returns_none_when_row_absent(self, worktree: Path) -> None:
        from typing import cast

        from cw.cli.stop_hook.continuation import _claim_stop_continuation
        from cw.models import Session

        save_dev_queue(DevQueueStore(tasks=[]))
        session = cast("Session", SimpleNamespace(id=SESSION_ID))
        assert _claim_stop_continuation(TICKET_ID, session) is None

    def test_claim_returns_none_when_capped_at_claim_time(self, worktree: Path) -> None:
        from typing import cast

        from cw.cli.stop_hook.continuation import _claim_stop_continuation
        from cw.models import Session

        _seed_row(stop_continuations=MAX_STOP_CONTINUATIONS)
        session = cast("Session", SimpleNamespace(id=SESSION_ID))
        assert _claim_stop_continuation(TICKET_ID, session) is None
        assert _row_counter() == MAX_STOP_CONTINUATIONS
