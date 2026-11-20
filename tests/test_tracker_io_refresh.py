"""The MCP courier refresh turn: one read-only codex turn for a snapshot (#3760).

RFC 0019 T2b. Every turn goes through :class:`ScriptedCodexRunner`; no real
codex launches. The refresh result is a cw-owned schema document (the bare T1
stdin document), so :func:`tracker_io_refresh_result` builds it by hand.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_runner import CodexRunResult
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.models.bindings import Bindings
from cw.ticket_snapshot import (
    REFRESH_REASONS,
    RefreshError,
    SnapshotRefusedError,
    TicketSnapshot,
)
from cw.ticket_snapshot._fetch import StdinComment, StdinDocument
from cw.tracker_io import TURN_TIMEOUT_SECONDS, TrackerIoReason
from cw.tracker_io._prompt import (
    REFRESH_SCHEMA,
    REFRESH_TURN_MARKER,
    RefreshComment,
    RefreshDocument,
)
from cw.tracker_io._refresh import (
    REFRESH_REASON_MAP,
    courier_refresher,
    courier_snapshot,
)
from tests._backend_mcp_helpers import stage_mcp_config
from tests._shared.bindings_builders import LINEAR_RENAMED_TOOLS, bindings_linear_mcp
from tests._shared.outbox_builders import client_config, entries_for
from tests._shared.ticket_context import seeded_row
from tests._ticket_view_helpers import events_of
from tests._tool_fakes import (
    ScriptedCodexRunner,
    any_call,
    stdin_contains,
    tracker_io_refresh_result,
    tracker_io_turn_result,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

pytestmark = pytest.mark.usefixtures(
    "isolated_backend_layers", "project_walk_stops_at_state_dir"
)

_RECORDED = OrchestratorEventType.TICKET_SNAPSHOT_RECORDED
_COMMENT_TEXT = "a distinctive operator remark"


def _client(
    tmp_path: Path, *, bindings: Bindings | None = None, operator_ids: bool = True
) -> ClientConfig:
    resolved = (
        bindings
        if bindings is not None
        else bindings_linear_mcp(operator_ids=["op-1"] if operator_ids else [])
    )
    return client_config(tmp_path, bindings=resolved, tracker="linear")


def _run(output: str | None = None, **kwargs: Any) -> CodexRunResult:
    return CodexRunResult(
        kwargs.pop("returncode", 0), "", "", output_file_content=output, **kwargs
    )


def _runner(reply: CodexRunResult) -> ScriptedCodexRunner:
    return ScriptedCodexRunner().add(stdin_contains(REFRESH_TURN_MARKER), reply)


def _argv(runner: ScriptedCodexRunner, index: int = 0) -> list[str]:
    argv = runner.calls[index]["argv"]
    assert isinstance(argv, list)
    return [str(a) for a in argv]


def _failure(
    runner: ScriptedCodexRunner, task: TicketTask, client: ClientConfig
) -> RefreshError:
    with pytest.raises(RefreshError) as info:
        courier_snapshot(runner, task, client)
    return info.value


def _assert_nothing_recorded() -> None:
    assert entries_for() == []
    assert events_of(_RECORDED) == []


class TestSuccess:
    def test_declared_server_returns_the_snapshot_in_one_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        task = seeded_row()
        runner = _runner(
            _run(
                tracker_io_refresh_result(
                    ticket_url="https://linear.app/x/issue/ACM-1",
                    comments=[
                        {"id": "c1", "body": "first"},
                        {"id": "c2", "body": "second"},
                    ],
                )
            )
        )

        snapshot = courier_snapshot(runner, task, _client(tmp_path))

        assert isinstance(snapshot, TicketSnapshot)
        assert snapshot.title == "Refreshed title"
        assert snapshot.body == "Refreshed body"
        assert snapshot.ticket_url == "https://linear.app/x/issue/ACM-1"
        assert [c.id for c in snapshot.comments] == ["c1", "c2"]
        assert snapshot.ticket_id == task.ticket_id
        assert snapshot.client == task.client
        assert len(runner.calls) == 1
        assert runner.calls[0]["timeout"] == TURN_TIMEOUT_SECONDS

    def test_agent_authored_is_computed_by_cw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.gh import AGENT_COMMENT_MARKER

        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(
            _run(
                tracker_io_refresh_result(
                    comments=[
                        {"id": "c1", "body": f"bot\n{AGENT_COMMENT_MARKER}"},
                        {"id": "c2", "body": "plain"},
                    ]
                )
            )
        )

        snapshot = courier_snapshot(runner, seeded_row(), _client(tmp_path))

        assert [c.agent_authored for c in snapshot.comments] == [True, False]
        assert "agent_authored" not in json.dumps(REFRESH_SCHEMA)

    def test_courier_refresher_closes_over_the_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(_run(tracker_io_refresh_result()))

        snapshot = courier_refresher(runner)(seeded_row(), _client(tmp_path))

        assert snapshot.title == "Refreshed title"
        assert len(runner.calls) == 1


class TestSchema:
    def test_top_level_keys_are_exactly_the_bare_document(self) -> None:
        assert set(REFRESH_SCHEMA["properties"]) == {
            "ticket_url",
            "title",
            "body",
            "comments",
        }
        text = json.dumps(REFRESH_SCHEMA)
        for banned in ("status", "detail", "ticket_id"):
            assert f'"{banned}"' not in text

    def test_comment_properties_are_the_six_keys(self) -> None:
        comment = REFRESH_SCHEMA["$defs"]["RefreshComment"]
        assert set(comment["properties"]) == {
            "id",
            "author_id",
            "author",
            "created_at",
            "body",
            "minimized",
        }
        assert "agent_authored" not in comment["properties"]
        assert "author_conflict" not in comment["properties"]

    def test_schema_is_openai_strict_at_every_object(self) -> None:
        def _objects(node: Any) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            if isinstance(node, dict):
                if "properties" in node:
                    found.append(node)
                for value in node.values():
                    found.extend(_objects(value))
            elif isinstance(node, list):
                for value in node:
                    found.extend(_objects(value))
            return found

        objects = _objects(REFRESH_SCHEMA)
        assert len(objects) == 2
        for node in objects:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])

    def test_required_and_nullable_fields(self) -> None:
        top = REFRESH_SCHEMA["properties"]
        comment = REFRESH_SCHEMA["$defs"]["RefreshComment"]["properties"]
        assert "anyOf" not in top["comments"]
        assert "anyOf" not in comment["minimized"]
        for nullable in (top["ticket_url"], comment["author_id"], comment["author"]):
            assert {"type": "null"} in nullable["anyOf"]

    def test_field_names_match_the_stdin_document(self) -> None:
        """Drift guard: the strict models duplicate the parser's field list."""
        assert set(RefreshDocument.model_fields) == set(StdinDocument.model_fields)
        assert set(RefreshComment.model_fields) == set(StdinComment.model_fields)


class TestArgv:
    def test_per_tool_declared_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(_run(tracker_io_refresh_result()))

        courier_snapshot(runner, seeded_row(), _client(tmp_path))

        argv = _argv(runner)
        assert 'mcp_servers.linear.enabled_tools=["get_issue","list_comments"]' in argv
        assert "--output-schema" in argv
        assert "-o" in argv
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert not any(a.startswith("apps.") for a in argv)
        stdin = runner.calls[0]["stdin"]
        assert isinstance(stdin, str)
        assert stdin.startswith(f"You are {REFRESH_TURN_MARKER}")

    def test_per_server_plugin_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "missing")
        runner = _runner(_run(tracker_io_refresh_result()))

        courier_snapshot(runner, seeded_row(), _client(tmp_path))

        argv = _argv(runner)
        assert "apps.linear.default_tools_enabled=true" in argv
        assert "enabled_tools" not in " ".join(argv)
        assert "through the `linear` app" in str(runner.calls[0]["stdin"])

    def test_renamed_tools_come_through_the_binding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        bindings = bindings_linear_mcp(
            tools=LINEAR_RENAMED_TOOLS, operator_ids=["op-1"]
        )
        runner = _runner(_run(tracker_io_refresh_result()))

        courier_snapshot(runner, seeded_row(), _client(tmp_path, bindings=bindings))

        assert (
            'mcp_servers.linear.enabled_tools=["linear_get_issue",'
            '"linear_list_comments"]'
        ) in _argv(runner)

    def test_binding_naming_nothing_falls_back_to_the_tables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        bindings = bindings_linear_mcp(tools={}, operator_ids=["op-1"])
        runner = _runner(_run(tracker_io_refresh_result()))

        courier_snapshot(runner, seeded_row(), _client(tmp_path, bindings=bindings))

        assert (
            'mcp_servers.linear.enabled_tools=["get_issue","list_comments"]'
        ) in _argv(runner)

    def test_other_declared_servers_are_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        sibling = '\n[mcp_servers.other]\ncommand = "x"\n'
        config.write_text(config.read_text(encoding="utf-8") + sibling, "utf-8")
        runner = _runner(_run(tracker_io_refresh_result()))

        courier_snapshot(runner, seeded_row(), _client(tmp_path))

        assert "mcp_servers.other.enabled=false" in _argv(runner)

    def test_no_retry_on_a_failed_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(_run(timed_out=True))

        _failure(runner, seeded_row(), _client(tmp_path))

        assert len(runner.calls) == 1


class TestNoBinding:
    def test_no_tickets_binding(self, tmp_path: Path) -> None:
        runner = _runner(_run(tracker_io_refresh_result()))
        client = _client(tmp_path, bindings=Bindings.model_validate({}))

        error = _failure(runner, seeded_row(), client)

        assert error.reason == "no_binding"
        assert runner.calls == []
        _assert_nothing_recorded()

    def test_unsupported_tracker(self, tmp_path: Path) -> None:
        runner = _runner(_run(tracker_io_refresh_result()))
        bindings = Bindings.model_validate(
            {"tickets": {"system": "github-issues", "cli": "gh"}}
        )

        error = _failure(runner, seeded_row(), _client(tmp_path, bindings=bindings))

        assert error.reason == "no_binding"
        assert runner.calls == []


class TestCourierUnreachable:
    def test_no_codex_realization(self, tmp_path: Path) -> None:
        runner = _runner(_run(tracker_io_refresh_result()))
        bindings = bindings_linear_mcp(harnesses=("opencode",), operator_ids=["op-1"])

        error = _failure(runner, seeded_row(), _client(tmp_path, bindings=bindings))

        assert error.reason == "courier_unreachable"
        assert runner.calls == []
        _assert_nothing_recorded()

    def test_declared_but_disabled_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "disabled")
        runner = _runner(_run(tracker_io_refresh_result()))

        error = _failure(runner, seeded_row(), _client(tmp_path))

        assert error.reason == "courier_unreachable"
        assert runner.calls == []
        _assert_nothing_recorded()

    def test_unreadable_config_layer_refuses_before_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "malformed")
        runner = _runner(_run(tracker_io_refresh_result()))

        error = _failure(runner, seeded_row(), _client(tmp_path))

        assert error.reason == "courier_unreachable"
        assert runner.calls == []
        _assert_nothing_recorded()

    def test_spawn_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(CodexRunResult(127, "", "codex: command not found"))

        assert _failure(runner, seeded_row(), _client(tmp_path)).reason == (
            "courier_unreachable"
        )
        _assert_nothing_recorded()

    def test_runner_raising_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _boom(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            msg = "no exec"
            raise OSError(msg)

        runner = ScriptedCodexRunner().add(any_call, _boom)

        assert _failure(runner, seeded_row(), _client(tmp_path)).reason == (
            "courier_unreachable"
        )


def _drop_minimized(doc: dict[str, Any]) -> None:
    del doc["comments"][0]["minimized"]


def _null_minimized(doc: dict[str, Any]) -> None:
    doc["comments"][0]["minimized"] = None


def _extra_agent_authored(doc: dict[str, Any]) -> None:
    doc["agent_authored"] = True


def _drop_comments(doc: dict[str, Any]) -> None:
    del doc["comments"]


class TestTurnFailed:
    @pytest.mark.parametrize(
        "result",
        [
            _run(timed_out=True),
            _run(returncode=2),
            _run(None),
            _run("not json at all"),
            _run(tracker_io_turn_result("ACM-1", ["x"])),
            _run(
                tracker_io_refresh_result(
                    comments=[{"id": "c1"}], mutate=_drop_minimized
                )
            ),
            _run(
                tracker_io_refresh_result(
                    comments=[{"id": "c1"}], mutate=_null_minimized
                )
            ),
            _run(tracker_io_refresh_result(mutate=_extra_agent_authored)),
            _run(tracker_io_refresh_result(mutate=_drop_comments)),
        ],
        ids=[
            "timeout",
            "nonzero",
            "no_output_file",
            "non_json",
            "post_shaped",
            "missing_minimized",
            "null_minimized",
            "extra_agent_authored",
            "missing_comments",
        ],
    )
    def test_each_case_is_turn_failed(
        self, result: CodexRunResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(result)

        error = _failure(runner, seeded_row(), _client(tmp_path))

        assert error.reason == "turn_failed"
        assert len(runner.calls) == 1
        _assert_nothing_recorded()


class TestRefused:
    def test_invalid_ticket_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        runner = _runner(_run(tracker_io_refresh_result()))
        task = seeded_row().model_copy(update={"ticket_id": "TICKET 1"})

        error = _failure(runner, task, _client(tmp_path))

        assert error.reason == "refused"
        assert runner.calls == []

    def test_snapshot_from_stdin_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _refuse(*_args: object, **_kwargs: object) -> TicketSnapshot:
            msg = "refused"
            raise SnapshotRefusedError(msg)

        monkeypatch.setattr("cw.tracker_io._refresh.snapshot_from_stdin", _refuse)
        runner = _runner(_run(tracker_io_refresh_result()))

        assert _failure(runner, seeded_row(), _client(tmp_path)).reason == "refused"
        _assert_nothing_recorded()


class TestExceptionContract:
    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.SubprocessError("x"),
            subprocess.TimeoutExpired("codex", 1),
            ValueError("x"),
            CwError("x"),
        ],
        ids=["subprocess_error", "timeout_expired", "value_error", "cw_error"],
    )
    def test_narrow_set_maps_to_turn_failed(
        self, exc: Exception, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _raise(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            raise exc

        runner = ScriptedCodexRunner().add(any_call, _raise)

        assert _failure(runner, seeded_row(), _client(tmp_path)).reason == (
            "turn_failed"
        )

    def test_an_inner_refresh_error_keeps_its_own_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _inner(*_args: object, **_kwargs: object) -> TicketSnapshot:
            msg = "inner"
            raise RefreshError(msg, reason="cap")

        monkeypatch.setattr("cw.tracker_io._refresh._fetch", _inner)

        error = _failure(ScriptedCodexRunner(), seeded_row(), _client(tmp_path))

        assert error.reason == "cap"

    def test_an_unlisted_exception_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _raise(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            msg = "programming error"
            raise RuntimeError(msg)

        runner = ScriptedCodexRunner().add(any_call, _raise)

        with pytest.raises(RuntimeError, match="programming error"):
            courier_snapshot(runner, seeded_row(), _client(tmp_path))

    def test_a_stage_never_raises_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.ticket_snapshot import snapshot_for_stage

        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _raise(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            msg = "x"
            raise subprocess.SubprocessError(msg)

        runner = ScriptedCodexRunner().add(any_call, _raise)
        task = seeded_row()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        snapshot = snapshot_for_stage(
            task,
            _client(tmp_path),
            worktree,
            stage="review",
            courier=courier_refresher(runner),
        )

        assert snapshot is not None
        assert snapshot.title != "Refreshed title"
        (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_DEGRADED)
        assert event.payload["kind"] == "refresh_failed"
        assert event.payload["reason"] == "turn_failed"


class TestReasonMap:
    def test_keys_are_the_eight_reasons_the_path_can_see(self) -> None:
        assert set(REFRESH_REASON_MAP) == {
            TrackerIoReason.TRACKER_UNSUPPORTED,
            TrackerIoReason.MCP_SERVER_UNNAMED,
            TrackerIoReason.TRACKER_MCP_NOT_ENABLED,
            TrackerIoReason.SCRATCH_UNWRITABLE,
            TrackerIoReason.CODEX_UNAVAILABLE,
            TrackerIoReason.PLAN_REFUSED,
            TrackerIoReason.TURN_TIMEOUT,
            TrackerIoReason.TURN_EXIT_NONZERO,
        }

    def test_every_value_is_in_the_closed_vocabulary(self) -> None:
        assert set(REFRESH_REASON_MAP.values()) <= set(REFRESH_REASONS)

    def test_an_unlisted_reason_falls_back_to_turn_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.tracker_io._refresh.REFRESH_REASON_MAP", dict[Any, Any]()
        )
        runner = _runner(_run(tracker_io_refresh_result()))
        bindings = Bindings.model_validate({})

        error = _failure(runner, seeded_row(), _client(tmp_path, bindings=bindings))

        assert error.reason == "turn_failed"


class TestNoTicketText:
    def test_messages_and_logs_carry_no_ticket_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        # A pydantic-invalid document: a raw ValidationError would echo the body.
        content = tracker_io_refresh_result(
            comments=[{"id": "c1", "body": _COMMENT_TEXT}], mutate=_null_minimized
        )
        runner = _runner(_run(content))

        with caplog.at_level(logging.DEBUG):
            error = _failure(runner, seeded_row(), _client(tmp_path))

        assert _COMMENT_TEXT not in str(error)
        assert _COMMENT_TEXT not in caplog.text

    def test_a_mapped_exception_logs_only_its_type(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")

        def _raise(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            raise ValueError(_COMMENT_TEXT)

        runner = ScriptedCodexRunner().add(any_call, _raise)

        with caplog.at_level(logging.DEBUG):
            error = _failure(runner, seeded_row(), _client(tmp_path))

        assert _COMMENT_TEXT not in str(error)
        assert _COMMENT_TEXT not in caplog.text
        assert "ValueError" in caplog.text


class TestImportOrder:
    """The injected courier keeps ``ticket_snapshot`` free of ``tracker_io``."""

    @pytest.mark.parametrize(
        "code",
        [
            "import cw.ticket_snapshot, cw.tracker_io",
            "import cw.tracker_io, cw.ticket_snapshot",
            "import cw.tracker_io",
            "import cw.ticket_snapshot",
        ],
    )
    def test_either_import_order_loads(self, code: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
