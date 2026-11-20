"""The outbox drain's MCP courier path: engine dispatch, events and gate filing (O2b).

``run_drain`` is driven with a :class:`ScriptedCodexRunner`; the gate filer is
replaced by a recorder at ``cw.outbox_drain.file_gate_record`` (the name the
engine imports). No real codex or ``needs_you.py`` is reached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_runner import CodexRunResult
from cw.gh import agent_marked_body
from cw.models import OrchestratorEventType
from cw.outbox import OutboxReason, OutboxRole, OutboxState, list_entries, read_marker
from cw.outbox_drain import run_drain
from tests._backend_mcp_helpers import stage_mcp_config
from tests._shared.bindings_builders import bindings_linear_mcp
from tests._shared.outbox_builders import CLIENT, client_config, enqueue
from tests._tool_fakes import (
    ScriptedCodexRunner,
    stdin_contains,
    tracker_io_turn_result,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures(
    "isolated_backend_layers", "project_walk_stops_at_state_dir"
)

_TICKET = "T-1"
_LINE = "<!-- cw-outbox:m1:1/1 -->"


@pytest.fixture
def recorded_gates(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Every gate payload the engine tried to file; the filer succeeds."""
    calls: list[dict[str, object]] = []

    def _file(payload: dict[str, object]) -> bool:
        calls.append(payload)
        return True

    monkeypatch.setattr("cw.outbox_drain.file_gate_record", _file)
    return calls


@pytest.fixture
def events(
    capture_events: Any,
) -> list[Any]:
    return capture_events("cw.outbox_drain")


def _state() -> OutboxState:
    return list_entries(CLIENT)[0].state


def _drain(tmp_path: Path, runner: ScriptedCodexRunner, **kwargs: Any) -> Any:
    bindings = kwargs.pop("bindings", None) or bindings_linear_mcp()
    cfg = client_config(tmp_path, bindings=bindings)
    return run_drain(CLIENT, cfg, runner=runner, **kwargs)


def _emitted() -> str:
    return agent_marked_body(f"body\n\n{_LINE}")


def _post_ok() -> CodexRunResult:
    return CodexRunResult(
        0,
        "",
        "",
        output_file_content=tracker_io_turn_result(_TICKET, [_emitted()]),
    )


def _status(status: str) -> CodexRunResult:
    return CodexRunResult(
        0,
        "",
        "",
        output_file_content=tracker_io_turn_result(_TICKET, [], status=status),
    )


def _payload_of(events: list[Any], etype: OrchestratorEventType) -> dict[str, Any]:
    matching = [payload for t, payload, _cid in events if t is etype]
    assert len(matching) == 1
    return dict(matching[0])


class TestDeliveryAndTurns:
    def test_pending_post_delivers_and_carries_turn_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Any]
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        runner = ScriptedCodexRunner().add(stdin_contains("post turn"), _post_ok())

        report = _drain(tmp_path, runner)

        assert report.delivered == 1
        assert _state() is OutboxState.DELIVERED
        payload = _payload_of(events, OrchestratorEventType.OUTBOX_ENTRY_DELIVERED)
        assert payload["turns"] == 1

    def test_operator_authored_entry_posts_without_agent_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",), operator_authored=True)
        plain = f"body\n\n{_LINE}"
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"),
            CodexRunResult(
                0,
                "",
                "",
                output_file_content=tracker_io_turn_result(_TICKET, [plain]),
            ),
        )

        _drain(tmp_path, runner)

        stdin = runner.calls[0]["stdin"]
        assert isinstance(stdin, str)
        assert "cw-agent-authored" not in stdin
        assert _state() is OutboxState.DELIVERED


class TestUnreachableRelease:
    def test_tool_unavailable_releases_files_one_gate_and_counts_the_turn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        events: list[Any],
        recorded_gates: list[dict[str, object]],
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"), _status("tool_unavailable")
        )

        _drain(tmp_path, runner)
        _drain(tmp_path, runner)

        assert _state() is OutboxState.PENDING
        marker = read_marker(CLIENT, OutboxRole.TICKETS)
        assert marker is not None
        assert marker.gate_filed is True
        assert len(recorded_gates) == 1
        assert "linear_mcp_unauthenticated" in str(recorded_gates[0]["title"])
        pending = [
            payload
            for etype, payload, _cid in events
            if etype is OrchestratorEventType.OUTBOX_ENTRY_PENDING
        ]
        assert pending
        assert pending[0]["turns"] == 1

    def test_unreadable_config_refuses_pre_launch_without_turn_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        events: list[Any],
        recorded_gates: list[dict[str, object]],
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "malformed", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        runner = ScriptedCodexRunner()

        _drain(tmp_path, runner)

        assert runner.calls == []
        assert _state() is OutboxState.PENDING
        pending = _payload_of(events, OrchestratorEventType.OUTBOX_ENTRY_PENDING)
        assert "turns" not in pending
        assert recorded_gates
        cause = str(recorded_gates[0]["question"])
        assert "config" in cause

    def test_no_codex_realization_is_g1_and_files_no_turn(
        self,
        tmp_path: Path,
        recorded_gates: list[dict[str, object]],
    ) -> None:
        enqueue(ticket=_TICKET, parts=("body",))
        runner = ScriptedCodexRunner()

        _drain(
            tmp_path,
            runner,
            bindings=bindings_linear_mcp(harnesses=("opencode",)),
        )

        assert runner.calls == []
        assert _state() is OutboxState.PENDING
        assert len(recorded_gates) == 1
        assert "no codex realization" in str(recorded_gates[0]["question"])

    def test_filer_returning_false_keeps_the_bit_clear_for_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        attempts: list[object] = []

        def _refuse(payload: dict[str, object]) -> bool:
            attempts.append(payload)
            return False

        monkeypatch.setattr("cw.outbox_drain.file_gate_record", _refuse)
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"), _status("tool_unavailable")
        )

        _drain(tmp_path, runner)
        _drain(tmp_path, runner)

        marker = read_marker(CLIENT, OutboxRole.TICKETS)
        assert marker is not None
        assert marker.gate_filed is False
        assert len(attempts) == 2

    def test_failed_filing_is_retried_by_the_next_tick_and_then_sticks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gate that fails to file on one tick files on the next (#3740 review).

        The next tick re-probes the marked role (``run.broken`` starts empty each
        tick), so the cause reaches ``_file_gate_once`` again. Once it files, the
        bit sticks and a third tick files nothing more.
        """
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        outcomes = iter([False, True])
        attempts: list[object] = []

        def _flaky(payload: dict[str, object]) -> bool:
            attempts.append(payload)
            return next(outcomes, True)

        monkeypatch.setattr("cw.outbox_drain.file_gate_record", _flaky)
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"), _status("tool_unavailable")
        )

        _drain(tmp_path, runner)
        _drain(tmp_path, runner)
        _drain(tmp_path, runner)

        marker = read_marker(CLIENT, OutboxRole.TICKETS)
        assert marker is not None
        assert marker.gate_filed is True
        assert len(attempts) == 2


class TestUnverifiedPaths:
    def test_timeout_is_unverified_with_one_call_and_no_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Any]
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"),
            CodexRunResult(0, "", "", timed_out=True),
        )

        _drain(tmp_path, runner)

        assert len(runner.calls) == 1
        assert _state() is OutboxState.UNVERIFIED
        payload = _payload_of(events, OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED)
        assert payload["turns"] == 1

    def test_app_delta_mismatch_is_unverified_with_three_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Any]
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server="linear")
        enqueue(ticket=_TICKET, parts=("body",))
        before = _snapshot(state="Todo")
        after = _snapshot(state="Done", comments=[("c1", [_LINE])])
        runner = (
            ScriptedCodexRunner()
            .add(
                stdin_contains("snapshot turn"),
                CodexRunResult(0, "", "", output_file_content=before),
                times=1,
            )
            .add(stdin_contains("post turn"), _post_ok())
            .add(
                stdin_contains("snapshot turn"),
                CodexRunResult(0, "", "", output_file_content=after),
            )
        )

        _drain(tmp_path, runner)

        assert _state() is OutboxState.UNVERIFIED
        payload = _payload_of(events, OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED)
        assert payload["turns"] == 3
        assert payload["reason"] == OutboxReason.READBACK_MISMATCH.value


def _snapshot(
    *,
    state: str,
    comments: list[tuple[str, list[str]]] | None = None,
) -> str:
    import json

    return json.dumps(
        {
            "status": "ok",
            "detail": "",
            "ticket_id": _TICKET,
            "comments": [
                {"id": cid, "marker_lines": lines} for cid, lines in comments or []
            ],
            "state": state,
            "assignee": "",
            "labels": [],
        }
    )


def test_gate_is_not_filed_for_a_per_item_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_gates: list[dict[str, object]],
) -> None:
    """A non-binding failure never reaches the gate filer."""
    stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server="linear")
    enqueue(ticket=_TICKET, parts=("body",))
    runner = ScriptedCodexRunner().add(
        stdin_contains("post turn"), CodexRunResult(1, "", "boom")
    )

    _drain(tmp_path, runner)

    assert recorded_gates == []
