"""Tests for the ``cw background-tool-guard-pre`` PreToolUse hook (#2303).

Three headless impl subagents (#2250, #2280, #2275) wedged the same way: a
pipeline-dependent Bash call was backgrounded (``run_in_background: true``) or
handed to the Monitor tool, and the turn ended waiting on a completion
notification that a headless DAEMON session never receives.
:mod:`cw.cli._background_tool_policy` refuses both shapes in headless workers
and nowhere else; :mod:`cw.cli.background_tool_guard_pre` turns the verdict
into the PreToolUse exit-code contract and a durable
``guard.background_tool_refused`` event.

Bash payloads derive from ``tests/test_cli_guard_busy_wait.py``'s
``_BASH_PRE_PAYLOAD`` envelope. The Monitor payload's ``tool_input`` mirrors
the one field observed in the operator's live #2275 capture
(``"name":"Monitor","input":{"command":"f=/tmp/…/tasks/…"}``); nothing else
about its shape is invented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cli import _background_tool_policy, _hook_io
from cw.cli._background_tool_policy import (
    _RefusalDecision,
    _resolve_background_tool_guard_enabled,
    classify_background_tool,
)
from cw.events import read_events
from cw.models import OrchestratorEventType
from tests.conftest import (
    _headless_worktree,
    _invoke_hook_command,
    _write_clients_yaml,
    _write_hook_context_file,
)
from tests.test_cli_guard_busy_wait import _BASH_PRE_PAYLOAD

if TYPE_CHECKING:
    from pathlib import Path

_BLOCK_EXIT = 2
_FIELD = "background_tool_guard_enabled"
_MONITOR_COMMAND = "f=/tmp/redacted/tasks/b3bgk2td.output; tail -f $f"


def _bash_payload(cwd: Path, *, run_in_background: object) -> dict[str, object]:
    """The Bash envelope pointed at *cwd* with *run_in_background* applied."""
    return {
        **_BASH_PRE_PAYLOAD,
        "cwd": str(cwd),
        "tool_input": {
            "command": "uv run pytest tests/",
            "run_in_background": run_in_background,
        },
    }


def _monitor_payload(cwd: Path) -> dict[str, object]:
    """The Monitor shape, narrowed to the one field the live capture showed."""
    return {
        **_BASH_PRE_PAYLOAD,
        "cwd": str(cwd),
        "tool_name": "Monitor",
        "tool_input": {"command": _MONITOR_COMMAND},
    }


def _interactive_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "interactive"
    worktree.mkdir()
    _write_hook_context_file(worktree, headless=False)
    return worktree


def _bare_dir(tmp_path: Path) -> Path:
    bare = tmp_path / "bare"
    bare.mkdir()
    return bare


def _disable_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.cli._background_tool_policy._resolve_background_tool_guard_enabled",
        lambda _client, _lane: False,
    )


def _refused_events() -> list[object]:
    return list(
        read_events(event_types=[OrchestratorEventType.GUARD_BACKGROUND_TOOL_REFUSED])
    )


def _write_global_disable(tmp_config_dir: Path) -> None:
    orchestrator_path = tmp_config_dir / ".claude-workspace" / "orchestrator.yaml"
    orchestrator_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator_path.write_text(f"{_FIELD}: false\n")


class TestResolveBackgroundToolGuardEnabled:
    """Lane-then-global fallthrough, mirroring the #2211 spawn guard."""

    def test_defaults_on_with_no_client_or_lane(self) -> None:
        assert _resolve_background_tool_guard_enabled(None, None) is True

    def test_global_disable_wins_with_no_lane_override(
        self, tmp_config_dir: Path
    ) -> None:
        _write_global_disable(tmp_config_dir)

        assert _resolve_background_tool_guard_enabled(None, None) is False

    def test_lane_override_disables_against_enabled_global(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=_FIELD)

        assert _resolve_background_tool_guard_enabled("acme", "fast") is False

    def test_lane_override_enables_against_disabled_global(
        self, tmp_config_dir: Path
    ) -> None:
        """The override is bidirectional — a lane can turn the guard back ON."""
        _write_global_disable(tmp_config_dir)
        _write_clients_yaml(tmp_config_dir, lane_value="true", field_name=_FIELD)

        assert _resolve_background_tool_guard_enabled("acme", "fast") is True

    def test_unknown_client_falls_through_to_global(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=_FIELD)

        assert _resolve_background_tool_guard_enabled("not-a-client", "fast") is True

    def test_unknown_lane_falls_through_to_global(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=_FIELD)

        assert _resolve_background_tool_guard_enabled("acme", "not-a-lane") is True


class TestClassifyBackgroundTool:
    """The decision table."""

    def test_headless_backgrounded_bash_is_refused(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        decision = classify_background_tool(
            _bash_payload(worktree, run_in_background=True)
        )

        assert isinstance(decision, _RefusalDecision)
        assert "#2303" in decision.reason
        assert "run_in_background" in decision.reason
        assert decision.tool_name == "Bash"

    def test_headless_foreground_bash_is_allowed(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        assert (
            classify_background_tool(_bash_payload(worktree, run_in_background=False))
            is None
        )

    def test_headless_monitor_is_refused(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        decision = classify_background_tool(_monitor_payload(worktree))

        assert decision is not None
        assert "#2303" in decision.reason
        assert "Monitor" in decision.reason
        assert decision.tool_name == "Monitor"

    def test_refusal_names_the_sanctioned_retry(self, tmp_path: Path) -> None:
        """The reason is the only channel the refused worker has."""
        worktree = _headless_worktree(tmp_path)

        decision = classify_background_tool(_monitor_payload(worktree))

        assert decision is not None
        assert "timeout" in decision.reason
        assert "#2291" in decision.reason
        assert _FIELD in decision.reason

    def test_interactive_backgrounded_bash_is_allowed(self, tmp_path: Path) -> None:
        worktree = _interactive_worktree(tmp_path)

        assert (
            classify_background_tool(_bash_payload(worktree, run_in_background=True))
            is None
        )

    def test_interactive_monitor_is_allowed(self, tmp_path: Path) -> None:
        worktree = _interactive_worktree(tmp_path)

        assert classify_background_tool(_monitor_payload(worktree)) is None

    def test_no_context_allows_backgrounded_bash(self, tmp_path: Path) -> None:
        bare = _bare_dir(tmp_path)

        assert (
            classify_background_tool(_bash_payload(bare, run_in_background=True))
            is None
        )

    def test_no_context_allows_monitor(self, tmp_path: Path) -> None:
        assert classify_background_tool(_monitor_payload(_bare_dir(tmp_path))) is None

    def test_disabled_guard_allows_backgrounded_bash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_guard(monkeypatch)
        worktree = _headless_worktree(tmp_path)

        assert (
            classify_background_tool(_bash_payload(worktree, run_in_background=True))
            is None
        )

    def test_disabled_guard_allows_monitor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_guard(monkeypatch)
        worktree = _headless_worktree(tmp_path)

        assert classify_background_tool(_monitor_payload(worktree)) is None

    def test_malformed_tool_input_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {
            **_bash_payload(worktree, run_in_background=True),
            "tool_input": "not-a-dict",
        }

        assert classify_background_tool(payload) is None
        assert "#2303" in capsys.readouterr().err

    def test_non_bool_run_in_background_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        assert (
            classify_background_tool(_bash_payload(worktree, run_in_background="yes"))
            is None
        )
        assert "#2303" in capsys.readouterr().err

    def test_unrecognized_tool_name_yields_no_verdict(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {
            **_bash_payload(worktree, run_in_background=True),
            "tool_name": "Read",
        }

        assert classify_background_tool(payload) is None

    def test_none_payload_yields_no_verdict(self) -> None:
        """Unreadable stdin upstream must not become a refusal."""
        assert classify_background_tool(None) is None

    def test_decision_carries_client_lane_session_ticket_from_context(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_hook_context_file(worktree, lane="fast", headless=True)

        decision = classify_background_tool(
            _bash_payload(worktree, run_in_background=True)
        )

        assert decision is not None
        assert decision.client == "client-a"
        assert decision.lane == "fast"
        assert decision.session_id == "sess940g"
        assert decision.ticket_id == "940"

    def test_decision_carries_agent_id_when_present(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {**_monitor_payload(worktree), "agent_id": "abc123"}

        decision = classify_background_tool(payload)

        assert decision is not None
        assert decision.agent_id == "abc123"

    def test_decision_omits_agent_id_when_absent(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        decision = classify_background_tool(_monitor_payload(worktree))

        assert decision is not None
        assert decision.agent_id is None


class TestEnforceReuse:
    def test_enforce_is_the_shared_hook_io_primitive(self) -> None:
        """Guards against this module growing its own copy of ``enforce``."""
        assert _background_tool_policy.enforce is _hook_io.enforce


class TestBackgroundToolGuardPreCLI:
    """The click registration and its PreToolUse exit-code contract."""

    def test_refusal_exits_2_with_reason_on_stderr(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        result = _invoke_hook_command(
            "background-tool-guard-pre",
            _bash_payload(worktree, run_in_background=True),
        )

        assert result.exit_code == _BLOCK_EXIT
        assert "#2303" in result.output

    def test_allowed_call_exits_0_silently(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        result = _invoke_hook_command(
            "background-tool-guard-pre",
            _bash_payload(worktree, run_in_background=False),
        )

        assert result.exit_code == 0
        assert result.output == ""

    def test_fail_open_on_unexpected_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_payload: object) -> None:
            raise RuntimeError

        monkeypatch.setattr(
            "cw.cli.background_tool_guard_pre.classify_background_tool", _boom
        )
        worktree = _headless_worktree(tmp_path)

        result = _invoke_hook_command(
            "background-tool-guard-pre",
            _bash_payload(worktree, run_in_background=True),
        )

        assert result.exit_code == 0


class TestRefusalEventRecorded:
    """Production observability: every refusal leaves a durable record."""

    def test_refusal_emits_guard_background_tool_refused_event(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_hook_context_file(worktree, lane="fast", headless=True)

        result = _invoke_hook_command(
            "background-tool-guard-pre",
            _bash_payload(worktree, run_in_background=True),
        )

        assert result.exit_code == _BLOCK_EXIT
        events = read_events(
            event_types=[OrchestratorEventType.GUARD_BACKGROUND_TOOL_REFUSED]
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["tool_name"] == "Bash"
        assert payload["client"] == "client-a"
        assert payload["lane"] == "fast"
        assert payload["ticket_id"] == "940"
        assert events[0].correlation_id == "sess940g"

    def test_event_includes_agent_id_when_present(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        _invoke_hook_command(
            "background-tool-guard-pre",
            {**_monitor_payload(worktree), "agent_id": "abc123"},
        )

        events = read_events(
            event_types=[OrchestratorEventType.GUARD_BACKGROUND_TOOL_REFUSED]
        )
        assert len(events) == 1
        assert events[0].payload["tool_name"] == "Monitor"
        assert events[0].payload["agent_id"] == "abc123"

    def test_event_omits_agent_id_when_absent(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        _invoke_hook_command("background-tool-guard-pre", _monitor_payload(worktree))

        events = read_events(
            event_types=[OrchestratorEventType.GUARD_BACKGROUND_TOOL_REFUSED]
        )
        assert len(events) == 1
        assert "agent_id" not in events[0].payload

    def test_allowed_call_emits_no_event(self, tmp_path: Path) -> None:
        worktree = _interactive_worktree(tmp_path)

        result = _invoke_hook_command(
            "background-tool-guard-pre",
            _bash_payload(worktree, run_in_background=True),
        )

        assert result.exit_code == 0
        assert _refused_events() == []

    def test_record_failure_does_not_suppress_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken event bus must never turn a refusal into an allow."""

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr("cw.cli.background_tool_guard_pre.record_event", _boom)
        worktree = _headless_worktree(tmp_path)

        result = _invoke_hook_command(
            "background-tool-guard-pre", _monitor_payload(worktree)
        )

        assert result.exit_code == _BLOCK_EXIT
