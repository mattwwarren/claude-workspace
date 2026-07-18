"""Tests for cw.plan and `cw dev-queue plan` CLI command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import add_ticket, load_dev_queue, load_plan
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    DispatchPlan,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.plan import run_planner

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def planner_client(make_git_repo: Callable[[str], Path]) -> ClientConfig:
    """A ClientConfig usable as the planner host.

    Uses a real git repo so spawn_create_impl's _validate_worktree check
    passes (plan.py passes workspace_path as the worktree, which in
    production is always a real git checkout).
    """
    workspace = make_git_repo("workspace/planner-client")
    return ClientConfig(
        name="planner-client",
        workspace_path=workspace,
        default_branch="main",
    )


class _ScriptedDaemon(FakeNativeDaemonClient):
    """FakeNativeDaemonClient that, on spawn, writes a planned JSON file.

    Re-reads the most recent prompt file from ``DEV_PLAN_OUTPUT_DIR`` to
    discover the expected output_path declared by run_planner, then writes
    *output_payload* (string) to that path.  Mimics the production
    /orchestrate-plan skill side-effect without invoking Claude.
    """

    def __init__(self, output_payload: str) -> None:
        super().__init__()
        self._payload = output_payload

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        short_id = super().spawn_bg(
            cwd=cwd,
            prompt=prompt,
            extra_args=extra_args,
            permission_mode=permission_mode,
        )
        from cw.config import DEV_PLAN_OUTPUT_DIR

        prompts = sorted(DEV_PLAN_OUTPUT_DIR.glob("prompt-*.txt"))
        if not prompts:
            return short_id
        prompt_text = prompts[-1].read_text()
        first_line = prompt_text.splitlines()[0]
        # Format: "/orchestrate-plan <output_path>"
        parts = first_line.split(" ", maxsplit=1)
        if len(parts) == 2:
            output_path = parts[1].strip()
            Path(output_path).write_text(self._payload)
        return short_id


# ---------------------------------------------------------------------------
# TestRunPlanner
# ---------------------------------------------------------------------------


class TestRunPlanner:
    def test_no_pending_tickets_raises(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        with pytest.raises(CwError, match="No pending tickets"):
            run_planner(
                client=planner_client,
                native_daemon=FakeNativeDaemonClient(),
                timeout_seconds=1,
            )

    def test_happy_path_persists_plan(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))
        add_ticket(TicketTask(ticket_id="GEN-2", client="planner-client"))

        plan_payload = DispatchPlan(
            tasks=[
                TicketTask(ticket_id="GEN-2", client="planner-client"),
                TicketTask(ticket_id="GEN-1", client="planner-client"),
            ],
            grouping_hints={"GEN-2": "should run first per planner heuristic"},
        ).model_dump_json()
        daemon = _ScriptedDaemon(plan_payload)

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=10,
            poll_interval=0.05,
        )

        assert result.error is None
        assert result.plan is not None
        assert [t.ticket_id for t in result.plan.tasks] == ["GEN-2", "GEN-1"]

        loaded = load_plan()
        assert loaded is not None
        assert [t.ticket_id for t in loaded.tasks] == ["GEN-2", "GEN-1"]

    def test_malformed_json_returns_error_no_persist(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))
        daemon = _ScriptedDaemon("this is { not valid JSON")

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=5,
            poll_interval=0.05,
        )

        assert result.plan is None
        assert result.error is not None
        assert "validation" in result.error.lower()

        # Plan must NOT be persisted on failure
        assert load_plan() is None

        # Queue must remain unchanged (still PENDING)
        store = load_dev_queue()
        assert all(t.status == QueueItemStatus.PENDING for t in store.tasks)

    def test_invalid_schema_returns_error(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))
        # Valid JSON but missing required ticket_task fields
        bad_payload = json.dumps({"tasks": [{"foo": "bar"}]})
        daemon = _ScriptedDaemon(bad_payload)

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=5,
            poll_interval=0.05,
        )

        assert result.plan is None
        assert result.error is not None
        assert load_plan() is None

    def test_timeout_returns_error(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))
        # Plain FakeNativeDaemonClient never writes the output file
        daemon = FakeNativeDaemonClient()

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=1,
            poll_interval=0.05,
        )

        assert result.plan is None
        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert load_plan() is None

    def test_prompt_uses_compact_repr(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(
            TicketTask(
                ticket_id="GEN-1",
                client="planner-client",
                scope_hint="small",
                session_id="abc-session-123",
                total_cost_usd=9.99,
                headless_timeout_override=600,
                regress_attempts=3,
            )
        )

        plan_payload = DispatchPlan(
            tasks=[TicketTask(ticket_id="GEN-1", client="planner-client")]
        ).model_dump_json()
        daemon = _ScriptedDaemon(plan_payload)

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=5,
            poll_interval=0.05,
        )

        assert result.plan is not None
        prompt_text = result.prompt_path.read_text()

        # Planning fields must appear in the prompt (keys + values where non-default)
        assert "GEN-1" in prompt_text
        assert "planner-client" in prompt_text
        assert "small" in prompt_text
        assert '"priority"' in prompt_text
        assert '"lane"' in prompt_text
        assert '"stage"' in prompt_text

        # Runtime-state fields must NOT appear in the prompt
        assert "abc-session-123" not in prompt_text  # session_id value
        assert "session_id" not in prompt_text
        assert "worktree_path" not in prompt_text
        assert "total_cost_usd" not in prompt_text
        assert "headless_timeout_override" not in prompt_text
        assert "regress_attempts" not in prompt_text
        assert "completed_at" not in prompt_text

    def test_client_filter_limits_tickets_in_prompt(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))
        add_ticket(TicketTask(ticket_id="OTH-1", client="other-client"))

        plan_payload = DispatchPlan(
            tasks=[TicketTask(ticket_id="GEN-1", client="planner-client")]
        ).model_dump_json()
        daemon = _ScriptedDaemon(plan_payload)

        result = run_planner(
            client=planner_client,
            native_daemon=daemon,
            timeout_seconds=5,
            poll_interval=0.05,
            client_filter="planner-client",
        )

        assert result.plan is not None
        # Verify the prompt file only contained the filtered ticket
        prompt_text = result.prompt_path.read_text()
        assert "GEN-1" in prompt_text
        assert "OTH-1" not in prompt_text


# ---------------------------------------------------------------------------
# TestCLIDevQueuePlan
# ---------------------------------------------------------------------------


class TestCLIDevQueuePlan:
    def test_cli_plan_happy_path(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        # Register the planner client in the in-memory client config
        clients = {planner_client.name: planner_client}
        monkeypatch.setattr("cw.cli.dev_queue.tasks.load_clients", lambda: clients)
        monkeypatch.setattr("cw.config.load_clients", lambda: clients)

        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))

        plan_payload = DispatchPlan(
            tasks=[TicketTask(ticket_id="GEN-1", client="planner-client")]
        ).model_dump_json()
        daemon = _ScriptedDaemon(plan_payload)
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "plan", "--client", "planner-client", "--timeout", "5"],
        )

        assert result.exit_code == 0, result.output
        assert "Plan persisted" in result.output

        loaded = load_plan()
        assert loaded is not None
        assert [t.ticket_id for t in loaded.tasks] == ["GEN-1"]

    def test_cli_plan_validation_failure_exits_nonzero(
        self,
        tmp_config_dir: Path,
        planner_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        clients = {planner_client.name: planner_client}
        monkeypatch.setattr("cw.cli.dev_queue.tasks.load_clients", lambda: clients)
        monkeypatch.setattr("cw.config.load_clients", lambda: clients)

        add_ticket(TicketTask(ticket_id="GEN-1", client="planner-client"))

        daemon = _ScriptedDaemon("not valid json at all")
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "plan", "--client", "planner-client", "--timeout", "5"],
        )

        assert result.exit_code != 0
        assert "Planner failed" in result.output
        # Queue unchanged
        store = load_dev_queue()
        assert all(t.status == QueueItemStatus.PENDING for t in store.tasks)
        # Plan not persisted
        assert load_plan() is None
