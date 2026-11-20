"""The MCP courier's turns, dispositions and provenance (O2b, RFC 0019, P1.7).

Every turn goes through :class:`ScriptedCodexRunner`; no real codex launches.
The user-layer config is staged per test (``declared`` = the server is a
``mcp_servers`` entry, ``missing`` = no layer, so the server is a plugin app).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.codex_runner import CodexRunResult
from cw.tracker_io import (
    CourierContext,
    CourierDisposition,
    CourierPart,
    CourierResult,
    TrackerIoReason,
    courier_drain,
)
from cw.tracker_io._dedup import bodies
from tests._backend_mcp_helpers import stage_mcp_config
from tests._shared.bindings_builders import bindings_linear_mcp
from tests._shared.outbox_builders import client_config
from tests._tool_fakes import (
    ScriptedCodexRunner,
    stdin_contains,
    tracker_io_turn_result,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures(
    "isolated_backend_layers", "project_walk_stops_at_state_dir"
)

_TICKET = "ENG-1"
_SERVER = "linear"
_LINE_1 = "<!-- cw-outbox:m1:1/1 -->"


def _parts(*texts: str) -> list[CourierPart]:
    total = len(texts)
    out: list[CourierPart] = []
    for index, text in enumerate(texts, start=1):
        line = f"<!-- cw-outbox:m1:{index}/{total} -->"
        out.append(CourierPart(body=f"{text}\n\n{line}", marker_line=line))
    return out


def _snapshot(
    *,
    status: str = "ok",
    comments: list[tuple[str, list[str]]] | None = None,
    state: str = "Todo",
    assignee: str = "",
    labels: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "status": status,
            "detail": "",
            "ticket_id": _TICKET,
            "comments": [
                {"id": cid, "marker_lines": lines} for cid, lines in comments or []
            ],
            "state": state,
            "assignee": assignee,
            "labels": labels or [],
        }
    )


def _run(output: str | None = None, *, timed_out: bool = False) -> CodexRunResult:
    return CodexRunResult(0, "", "", output_file_content=output, timed_out=timed_out)


def _client(tmp_path: Path) -> ClientConfig:
    return client_config(tmp_path, bindings=bindings_linear_mcp())


def _courier(
    tmp_path: Path,
    runner: ScriptedCodexRunner,
    parts: list[CourierPart],
    *,
    recheck: bool = False,
    operator_authored: bool = False,
    ticket: str = _TICKET,
) -> CourierResult:
    context = CourierContext(runner=runner, client=_client(tmp_path))
    return courier_drain(
        ticket,
        parts,
        operator_authored=operator_authored,
        recheck=recheck,
        context=context,
    )


def _argv(runner: ScriptedCodexRunner, index: int) -> list[str]:
    argv = runner.calls[index]["argv"]
    assert isinstance(argv, list)
    return [str(a) for a in argv]


class TestDeclaredServerPerTool:
    def test_post_turn_is_one_turn_and_delivers_verified_parts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha", "beta")
        emitted = bodies(parts, operator_authored=False)
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"),
            _run(tracker_io_turn_result(_TICKET, emitted)),
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.DELIVERED
        assert result.turns == 1
        assert [p.index for p in result.posted] == [1, 2]
        argv = _argv(runner, 0)
        assert f"mcp_servers.{_SERVER}.enabled_tools=" in " ".join(argv)
        assert "apps.linear.default_tools_enabled=true" not in " ".join(argv)

    def test_already_present_delivers_without_new_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"),
            _run(tracker_io_turn_result(_TICKET, emitted, status="already_present")),
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.DELIVERED
        assert result.already_present is True
        assert result.turns == 1

    def test_timeout_is_unverified_with_no_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha")
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"), _run(timed_out=True)
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.UNVERIFIED
        assert result.reason is TrackerIoReason.TURN_TIMEOUT
        assert result.turns == 1
        assert len(runner.calls) == 1

    def test_recheck_already_present_is_one_verify_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = ScriptedCodexRunner().add(
            stdin_contains("verify turn"),
            _run(tracker_io_turn_result(_TICKET, emitted, status="already_present")),
        )

        result = _courier(tmp_path, runner, parts, recheck=True)

        assert result.disposition is CourierDisposition.DELIVERED
        assert result.already_present is True
        assert result.turns == 1
        assert len(runner.calls) == 1

    def test_recheck_not_found_then_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = (
            ScriptedCodexRunner()
            .add(
                stdin_contains("verify turn"),
                _run(tracker_io_turn_result(_TICKET, [], status="ok")),
            )
            .add(
                stdin_contains("post turn"),
                _run(tracker_io_turn_result(_TICKET, emitted)),
            )
        )

        result = _courier(tmp_path, runner, parts, recheck=True)

        assert result.disposition is CourierDisposition.DELIVERED
        assert result.turns == 2

    def test_operator_authored_entry_carries_no_agent_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=True)
        runner = ScriptedCodexRunner().add(
            stdin_contains("post turn"), _run(tracker_io_turn_result(_TICKET, emitted))
        )

        result = _courier(tmp_path, runner, parts, operator_authored=True)

        assert result.disposition is CourierDisposition.DELIVERED
        stdin = runner.calls[0]["stdin"]
        assert isinstance(stdin, str)
        assert f"alpha\n\n{_LINE_1}" in stdin
        assert "cw-agent-authored" not in emitted[0]


class TestPluginAppPerServer:
    def test_clean_app_host_runs_three_turns_and_delivers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = (
            ScriptedCodexRunner()
            .add(stdin_contains("snapshot turn"), _run(_snapshot()), times=1)
            .add(
                stdin_contains("post turn"),
                _run(tracker_io_turn_result(_TICKET, emitted)),
            )
            .add(
                stdin_contains("snapshot turn"),
                _run(_snapshot(comments=[("c1", [_LINE_1])])),
            )
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.DELIVERED
        assert result.turns == 3
        for index in range(3):
            joined = " ".join(_argv(runner, index))
            assert "apps.linear.default_tools_enabled=true" in joined
            assert "mcp_servers.linear" not in joined

    def test_delta_mismatch_is_unverified_readback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = (
            ScriptedCodexRunner()
            .add(
                stdin_contains("snapshot turn"),
                _run(_snapshot(state="Todo")),
                times=1,
            )
            .add(
                stdin_contains("post turn"),
                _run(tracker_io_turn_result(_TICKET, emitted)),
            )
            .add(
                stdin_contains("snapshot turn"),
                _run(_snapshot(state="Done", comments=[("c1", [_LINE_1])])),
            )
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.UNVERIFIED
        assert result.reason is TrackerIoReason.READBACK_MISMATCH
        assert result.turns == 3

    def test_marker_stripped_after_post_is_never_delivered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        parts = _parts("alpha")
        emitted = bodies(parts, operator_authored=False)
        runner = (
            ScriptedCodexRunner()
            .add(stdin_contains("snapshot turn"), _run(_snapshot()), times=1)
            .add(
                stdin_contains("post turn"),
                _run(tracker_io_turn_result(_TICKET, emitted)),
            )
            .add(
                stdin_contains("snapshot turn"),
                _run(_snapshot(comments=[("c1", [])])),
            )
        )

        result = _courier(tmp_path, runner, parts)

        assert result.disposition is CourierDisposition.UNVERIFIED
        assert result.reason is TrackerIoReason.READBACK_MISMATCH

    def test_before_snapshot_tool_unavailable_is_unreachable_no_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        runner = ScriptedCodexRunner().add(
            stdin_contains("snapshot turn"),
            _run(_snapshot(status="tool_unavailable")),
        )

        result = _courier(tmp_path, runner, _parts("alpha"))

        assert result.disposition is CourierDisposition.UNREACHABLE
        assert result.reason is TrackerIoReason.TOOL_UNAVAILABLE
        assert result.turns == 1
        assert len(runner.calls) == 1

    def test_before_snapshot_failed_is_failed_no_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        runner = ScriptedCodexRunner().add(
            stdin_contains("snapshot turn"), _run(_snapshot(status="failed"))
        )

        result = _courier(tmp_path, runner, _parts("alpha"))

        assert result.disposition is CourierDisposition.FAILED
        assert result.turns == 1
        assert len(runner.calls) == 1


class TestRefusals:
    def test_unreadable_user_layer_refuses_before_any_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "malformed", server=_SERVER)
        runner = ScriptedCodexRunner()

        result = _courier(tmp_path, runner, _parts("alpha"))

        assert result.disposition is CourierDisposition.UNREACHABLE
        assert result.reason is TrackerIoReason.MCP_SERVER_UNNAMED
        assert result.turns == 0
        assert result.layers_unreadable
        assert runner.calls == []

    def test_no_tickets_binding_is_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.models.bindings import Bindings

        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing", server=_SERVER)
        context = CourierContext(
            runner=ScriptedCodexRunner(),
            client=client_config(tmp_path, bindings=Bindings()),
        )

        result = courier_drain(
            _TICKET,
            _parts("alpha"),
            operator_authored=False,
            recheck=False,
            context=context,
        )

        assert result.disposition is CourierDisposition.UNREACHABLE
        assert result.reason is TrackerIoReason.TRACKER_UNSUPPORTED
        assert result.turns == 0

    def test_invalid_ticket_id_is_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled", server=_SERVER)

        result = _courier(
            tmp_path, ScriptedCodexRunner(), _parts("alpha"), ticket="bad id!"
        )

        assert result.disposition is CourierDisposition.FAILED
        assert result.reason is TrackerIoReason.PLAN_REFUSED
        assert result.turns == 0
