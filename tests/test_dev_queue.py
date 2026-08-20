"""Tests for cw.dev_queue and related CLI commands."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.atomic import _BACKUP_SUFFIX
from cw.cli import main
from cw.config import clients_file, load_orchestrator_config
from cw.dev_queue import (
    _find_ticket,
    _lock,
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    load_dev_queue,
    load_plan,
    migrate_dev_queue,
    plan_path,
    register_watched_pr,
    remove_ticket,
    resolve_client,
    save_dev_queue,
    save_plan,
    transition_task_status,
    wait_for_terminal,
)
from cw.dev_queue.crud import register_or_adopt_watched_pr
from cw.dev_queue.lifecycle import _advance_task_pointer, _stage_regress
from cw.dispatch import (
    FRESHNESS_MAIN_BEHIND,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
    _lane_stats_for_client,
)
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    ClientConfig,
    DevQueueStore,
    DispatchPlan,
    DispatchSkipReason,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
    WatchedPr,
)
from tests.conftest import (
    _make_daemon_session,
    _make_ticket_task,
    _write_project_config_yaml,
    plan_body,
    stub_fetch_plan,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ReapReason, Session
    from tests.conftest import CapturedEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dev_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect dev queue file and lock to tmp_path.

    Also writes a minimal clients.yaml so add_ticket's lane validation can
    resolve 'genhealth' and 'other' (the client names used across these tests).
    Both clients have no explicit lanes, so effective_lanes synthesises the
    default 'default' lane, which is what every TicketTask defaults to.
    """
    dev_queue_file = tmp_path / "dev_queue.json"
    dev_queue_lock = tmp_path / ".dev_queue.lock"
    dev_plan_file = tmp_path / "dev_plan.json"
    dev_plan_lock = tmp_path / ".dev_plan.lock"

    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", dev_queue_lock)
    monkeypatch.setattr("cw.config.DEV_PLAN_FILE", dev_plan_file)
    monkeypatch.setattr("cw.config.DEV_PLAN_LOCK", dev_plan_lock)

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    clients_file().write_text(
        f"clients:\n"
        f"  genhealth:\n    workspace_path: {ws}\n"
        f"  other:\n    workspace_path: {ws}\n"
    )

    return tmp_path


@pytest.fixture
def tmp_orchestrator_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect orchestrator config file to tmp_path."""
    config_dir = tmp_path / ".claude-workspace"
    config_file = config_dir / "orchestrator.yaml"

    monkeypatch.setattr("cw.config.ORCHESTRATOR_CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.ORCHESTRATOR_CONFIG_FILE", config_file)

    return tmp_path


@pytest.fixture
def sample_config() -> OrchestratorConfig:
    """OrchestratorConfig with a GEN -> genhealth prefix mapping."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"default": 2},
        linear_prefix_map={"GEN": "genhealth"},
    )


# ---------------------------------------------------------------------------
# TestResolveClient
# ---------------------------------------------------------------------------


class TestResolveClient:
    def test_client_override_wins(self, sample_config: OrchestratorConfig) -> None:
        result = resolve_client("GEN-100", sample_config, client_override="myteam")
        assert result == "myteam"

    def test_prefix_map_resolves_client(
        self, sample_config: OrchestratorConfig
    ) -> None:
        result = resolve_client("GEN-100", sample_config, client_override=None)
        assert result == "genhealth"

    def test_prefix_map_resolves_any_suffix(
        self, sample_config: OrchestratorConfig
    ) -> None:
        result = resolve_client("GEN-999", sample_config, client_override=None)
        assert result == "genhealth"

    def test_unknown_prefix_raises_cw_error(
        self, sample_config: OrchestratorConfig
    ) -> None:
        with pytest.raises(CwError, match="Cannot resolve client"):
            resolve_client("ABC-5", sample_config, client_override=None)

    def test_no_dash_in_ticket_raises_cw_error(
        self, sample_config: OrchestratorConfig
    ) -> None:
        with pytest.raises(CwError, match="Cannot resolve client"):
            resolve_client("NODASH", sample_config, client_override=None)

    def test_client_override_takes_precedence_over_missing_prefix(
        self, sample_config: OrchestratorConfig
    ) -> None:
        result = resolve_client("UNKNOWN-1", sample_config, client_override="override")
        assert result == "override"


# ---------------------------------------------------------------------------
# TestLoadSaveDevQueue
# ---------------------------------------------------------------------------


class TestLoadSaveDevQueue:
    def test_load_missing_file_returns_empty_store(self, tmp_dev_queue: Path) -> None:
        store = load_dev_queue()
        assert store.tasks == []

    def test_add_ticket_persists_to_store(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="GEN-100", client="genhealth")
        add_ticket(task)
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].ticket_id == "GEN-100"
        assert store.tasks[0].client == "genhealth"

    def test_add_multiple_tickets(self, tmp_dev_queue: Path) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="GEN-101", client="genhealth"))
        store = load_dev_queue()
        assert len(store.tasks) == 2
        ids = [t.ticket_id for t in store.tasks]
        assert "GEN-100" in ids
        assert "GEN-101" in ids

    def test_ticket_default_status_is_pending(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="GEN-200", client="genhealth")
        add_ticket(task)
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.PENDING

    def test_ticket_priority_stored(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="GEN-300", client="genhealth", priority=5)
        add_ticket(task)
        store = load_dev_queue()
        assert store.tasks[0].priority == 5

    # -- #1653: a parked row owns the ticket; re-adding is refused ---------

    def test_add_refused_when_row_parked_blocked_on_user(
        self, tmp_dev_queue: Path
    ) -> None:
        """A BLOCKED_ON_USER row blocks a re-add — no sibling row is minted."""
        parked = TicketTask(
            ticket_id="GEN-400",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="ambiguities_pending_resolution",
        )
        save_dev_queue(DevQueueStore(tasks=[parked]))

        inserted = add_ticket(TicketTask(ticket_id="GEN-400", client="genhealth"))

        assert inserted is False
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_add_refused_when_row_awaiting_operator_signoff(
        self, tmp_dev_queue: Path
    ) -> None:
        """An AWAITING_OPERATOR_SIGNOFF row blocks a re-add too."""
        parked = TicketTask(
            ticket_id="GEN-401",
            client="genhealth",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[parked]))

        inserted = add_ticket(TicketTask(ticket_id="GEN-401", client="genhealth"))

        assert inserted is False
        assert len(load_dev_queue().tasks) == 1

    def test_add_parked_refusal_is_not_stage_scoped(self, tmp_dev_queue: Path) -> None:
        """Unlike the terminal dedup, a park blocks adds at ANY stage."""
        parked = TicketTask(
            ticket_id="GEN-402",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="plan_pending_approval",
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[parked]))

        inserted = add_ticket(
            TicketTask(ticket_id="GEN-402", client="genhealth", stage=Stage.IMPL)
        )

        assert inserted is False
        assert len(load_dev_queue().tasks) == 1

    def test_add_parked_other_ticket_still_inserts(self, tmp_dev_queue: Path) -> None:
        """A park on one ticket never blocks enqueueing a different ticket."""
        parked = TicketTask(
            ticket_id="GEN-403",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="review_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[parked]))

        inserted = add_ticket(TicketTask(ticket_id="GEN-404", client="genhealth"))

        assert inserted is True
        assert len(load_dev_queue().tasks) == 2

    def test_save_dev_queue_first_write_creates_no_backup(
        self, tmp_dev_queue: Path
    ) -> None:
        save_dev_queue(DevQueueStore())
        assert list(tmp_dev_queue.glob(f"dev_queue.json{_BACKUP_SUFFIX}*")) == []

    def test_save_dev_queue_rotates_backup_on_second_write(
        self, tmp_dev_queue: Path
    ) -> None:
        first = DevQueueStore(
            tasks=[TicketTask(ticket_id="GEN-100", client="genhealth")]
        )
        save_dev_queue(first)
        first_payload = (tmp_dev_queue / "dev_queue.json").read_text(encoding="utf-8")

        second = DevQueueStore(
            tasks=[TicketTask(ticket_id="GEN-200", client="genhealth")]
        )
        save_dev_queue(second)

        backups = list(tmp_dev_queue.glob(f"dev_queue.json{_BACKUP_SUFFIX}*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == first_payload

    def test_save_dev_queue_backup_rotation_keeps_last_five(
        self, tmp_dev_queue: Path
    ) -> None:
        for i in range(7):
            store = DevQueueStore(
                tasks=[TicketTask(ticket_id=f"GEN-{i}", client="genhealth")]
            )
            save_dev_queue(store)

        backups = list(tmp_dev_queue.glob(f"dev_queue.json{_BACKUP_SUFFIX}*"))
        assert len(backups) == 5

    def test_save_dev_queue_refuses_real_path(
        self,
        tmp_dev_queue: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """save_dev_queue must refuse to write under the real state dir (#1017)."""
        from unittest.mock import MagicMock

        import cw.config

        real_dev_queue_file = cw.config._REAL_STATE_DIR / "dev_queue.json"
        monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", real_dev_queue_file)
        mock_write = MagicMock()
        mock_rotate = MagicMock()
        monkeypatch.setattr("cw.dev_queue.storage.atomic_write_text", mock_write)
        monkeypatch.setattr("cw.dev_queue.storage.rotate_backup", mock_rotate)

        with pytest.raises(CwError, match="refusing real-state write"):
            save_dev_queue(DevQueueStore())

        mock_write.assert_not_called()
        mock_rotate.assert_not_called()


# ---------------------------------------------------------------------------
# TestListTickets
# ---------------------------------------------------------------------------


class TestListTickets:
    def test_list_all_tickets(self, tmp_dev_queue: Path) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="ABC-1", client="other"))
        tickets = list_tickets()
        assert len(tickets) == 2

    def test_list_filtered_by_client(self, tmp_dev_queue: Path) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="ABC-1", client="other"))
        tickets = list_tickets(client="genhealth")
        assert len(tickets) == 1
        assert tickets[0].ticket_id == "GEN-100"

    def test_list_empty_returns_empty(self, tmp_dev_queue: Path) -> None:
        tickets = list_tickets()
        assert tickets == []


# ---------------------------------------------------------------------------
# TestConcurrentAdd
# ---------------------------------------------------------------------------


class TestConcurrentAdd:
    def test_concurrent_adds_do_not_lose_data(self, tmp_dev_queue: Path) -> None:
        """File locking prevents concurrent adds from losing data."""
        n = 20
        errors: list[Exception] = []

        def _add(i: int) -> None:
            try:
                add_ticket(TicketTask(ticket_id=f"GEN-{i}", client="genhealth"))
            except Exception as e:  # noqa: BLE001
                # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331
                # (3-part justification — paired-test part is N/A because
                # the catch IS the test scaffold collecting per-thread
                # failures for the assertion on line 210):
                # 1. Test-scaffold surface: the assertion under test
                #    (errors == []) needs to surface ANY thread-local
                #    exception, including unexpected ones — narrowing
                #    here would mask real bugs.
                # 2. Logging: caught exception is appended to the
                #    'errors' list and surfaced in the assertion message
                #    on failure.
                # 3. Non-critical: this is a test thread; the main
                #    test orchestrates failure surfacing via the errors
                #    list and asserts on it after join().
                errors.append(e)

        threads = [threading.Thread(target=_add, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent adds: {errors}"
        store = load_dev_queue()
        assert len(store.tasks) == n


# ---------------------------------------------------------------------------
# TestDevQueueStoreModel
# ---------------------------------------------------------------------------


class TestDevQueueStoreModel:
    def test_pending_filters_correctly(self) -> None:
        store = DevQueueStore(
            tasks=[
                TicketTask(ticket_id="A", client="c", status=QueueItemStatus.PENDING),
                TicketTask(ticket_id="B", client="c", status=QueueItemStatus.RUNNING),
                TicketTask(ticket_id="C", client="c", status=QueueItemStatus.COMPLETED),
            ]
        )
        assert len(store.pending()) == 1
        assert store.pending()[0].ticket_id == "A"

    def test_running_filters_correctly(self) -> None:
        store = DevQueueStore(
            tasks=[
                TicketTask(ticket_id="A", client="c", status=QueueItemStatus.PENDING),
                TicketTask(ticket_id="B", client="c", status=QueueItemStatus.RUNNING),
            ]
        )
        assert len(store.running()) == 1
        assert store.running()[0].ticket_id == "B"

    def test_by_client_filters_correctly(self) -> None:
        store = DevQueueStore(
            tasks=[
                TicketTask(ticket_id="A", client="alpha"),
                TicketTask(ticket_id="B", client="beta"),
                TicketTask(ticket_id="C", client="alpha"),
            ]
        )
        assert len(store.by_client("alpha")) == 2
        assert len(store.by_client("beta")) == 1
        assert store.by_client("gamma") == []


# ---------------------------------------------------------------------------
# TestOrchestratorConfig
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    def test_creates_default_file_when_missing(
        self, tmp_orchestrator_config: Path
    ) -> None:
        config = load_orchestrator_config()
        config_file = (
            tmp_orchestrator_config / ".claude-workspace" / "orchestrator.yaml"
        )
        assert config_file.exists()
        assert config.tick_interval_seconds == 30

    def test_loads_existing_file(self, tmp_orchestrator_config: Path) -> None:
        config_dir = tmp_orchestrator_config / ".claude-workspace"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "orchestrator.yaml"
        config_file.write_text(
            "tick_interval_seconds: 60\n"
            "per_client_max_parallel:\n"
            "  genhealth: 3\n"
            "linear_prefix_map:\n"
            "  GEN: genhealth\n"
        )
        config = load_orchestrator_config()
        assert config.tick_interval_seconds == 60
        assert config.per_client_max_parallel == {"genhealth": 3}
        assert config.linear_prefix_map == {"GEN": "genhealth"}

    def test_default_values(self) -> None:
        config = OrchestratorConfig()
        assert config.tick_interval_seconds == 30
        assert config.per_client_max_parallel == {}
        assert config.linear_prefix_map == {}


# ---------------------------------------------------------------------------
# TestCLIDevQueueAdd
# ---------------------------------------------------------------------------


class TestCLIDevQueueAdd:
    def test_add_with_client_flag(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch events.record_event to no-op
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "add", "ABC-5", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        assert "ABC-5" in result.output
        assert "genhealth" in result.output
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].ticket_id == "ABC-5"
        assert store.tasks[0].client == "genhealth"

    def test_add_resolves_via_prefix_map(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_dir = tmp_orchestrator_config / ".claude-workspace"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "orchestrator.yaml"
        config_file.write_text(
            "tick_interval_seconds: 30\n"
            "per_client_max_parallel: {}\n"
            "linear_prefix_map:\n"
            "  GEN: genhealth\n"
        )
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "add", "GEN-100"])
        assert result.exit_code == 0, result.output
        assert "genhealth" in result.output
        store = load_dev_queue()
        assert store.tasks[0].client == "genhealth"

    def test_add_multiple_tickets(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_dir = tmp_orchestrator_config / ".claude-workspace"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "orchestrator.yaml"
        config_file.write_text(
            "tick_interval_seconds: 30\n"
            "per_client_max_parallel: {}\n"
            "linear_prefix_map:\n"
            "  GEN: genhealth\n"
        )
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "add", "GEN-100", "GEN-101"])
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert len(store.tasks) == 2

    def test_add_unknown_prefix_fails(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "add", "UNKNOWN-1"])
        assert result.exit_code != 0
        assert "Cannot resolve client" in result.output

    def test_add_with_priority(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--priority", "3"],
        )
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert store.tasks[0].priority == 3

    def test_add_undeclared_lane_exits_nonzero(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cw dev-queue add with undeclared --lane exits non-zero."""
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--lane", "fast"],
        )
        assert result.exit_code != 0
        assert "fast" in result.output

    def test_add_with_stage_impl_lands_task_at_impl(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cw dev-queue add --stage impl lands the task at IMPL (GitHub #1682)."""
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--stage", "impl"],
        )
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        assert store.tasks[0].stage_high_water == Stage.IMPL

    def test_add_without_stage_defaults_to_plan(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: omitting --stage keeps today's default behavior."""
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "add", "ABC-5", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.PLAN
        assert store.tasks[0].stage_high_water is None

    def test_add_with_invalid_stage_choice_exits_nonzero_and_does_not_insert(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unrecognized --stage value fails loudly at parse time (no fallback)."""
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--stage", "bogus"],
        )
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "invalid choice" in output_lower or "error" in output_lower
        store = load_dev_queue()
        assert store.tasks == []

    def test_add_with_stage_not_in_client_pipeline_exits_nonzero(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--stage review against a pipeline that excludes review exits non-zero."""
        ws = tmp_dev_queue / "ws"
        clients_file().write_text(
            f"clients:\n  genhealth:\n    workspace_path: {ws}\n"
            "    pipeline:\n      stages: [plan, impl]\n"
        )
        monkeypatch.setattr("cw.cli.dev_queue.crud.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "add",
                "ABC-5",
                "--client",
                "genhealth",
                "--stage",
                "review",
            ],
        )
        assert result.exit_code != 0
        assert "not in the pipeline" in result.output

    def test_add_with_stage_emits_ticket_enqueued_event_with_stage_field(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The TICKET_ENQUEUED payload carries the resolved stage (GitHub #1682)."""
        events: list[tuple[OrchestratorEventType, dict[str, object], str | None]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda etype, payload=None, **kw: events.append(
                (etype, payload or {}, kw.get("correlation_id"))
            ),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--stage", "impl"],
        )
        assert result.exit_code == 0, result.output
        enqueued = [e for e in events if e[0] == OrchestratorEventType.TICKET_ENQUEUED]
        assert len(enqueued) == 1
        _, payload, _ = enqueued[0]
        assert payload["stage"] == "impl"

    def test_add_without_stage_emits_ticket_enqueued_event_with_plan_stage(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: the payload's existing keys plus a default 'plan'."""
        events: list[tuple[OrchestratorEventType, dict[str, object], str | None]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda etype, payload=None, **kw: events.append(
                (etype, payload or {}, kw.get("correlation_id"))
            ),
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "add", "ABC-5", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        enqueued = [e for e in events if e[0] == OrchestratorEventType.TICKET_ENQUEUED]
        assert len(enqueued) == 1
        _, payload, _ = enqueued[0]
        assert payload["stage"] == "plan"
        assert payload["ticket_id"] == "ABC-5"
        assert payload["client"] == "genhealth"


# ---------------------------------------------------------------------------
# TestCLIDevQueueStatus
# ---------------------------------------------------------------------------


class TestCLIDevQueueStatus:
    def test_status_empty_queue(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()

    def test_status_renders_table(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="GEN-101", client="genhealth"))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "genhealth" in result.output
        assert "GEN-100" in result.output
        assert "GEN-101" in result.output

    def test_status_shows_counts(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="GEN-101", client="genhealth"))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        # 2 pending tickets
        assert "2" in result.output

    def test_status_filtered_by_client(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
    ) -> None:
        add_ticket(TicketTask(ticket_id="GEN-100", client="genhealth"))
        add_ticket(TicketTask(ticket_id="ABC-1", client="other"))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status", "--client", "genhealth"])
        assert result.exit_code == 0, result.output
        assert "genhealth" in result.output
        assert "other" not in result.output

    def test_status_stale_tick_shows_marker(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale tick_at → [STALE — no tick in Ns] appended to the tick line."""
        from datetime import timedelta

        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-200", client="genhealth"))
        stale_at = datetime.now(UTC) - timedelta(seconds=150)
        tick = TickSummary(
            claimed=0, pending=1, running=0, cap=3, skip_reason="none", tick_at=stale_at
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"genhealth": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "[STALE — no tick in " in result.output

    def test_status_fresh_tick_no_marker(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh tick_at → no [STALE] marker."""
        from datetime import timedelta

        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-201", client="genhealth"))
        fresh_at = datetime.now(UTC) - timedelta(seconds=10)
        tick = TickSummary(
            claimed=0, pending=1, running=0, cap=3, skip_reason="none", tick_at=fresh_at
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"genhealth": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "[STALE" not in result.output


# ---------------------------------------------------------------------------
# TestDevQueueLaneBreakdownOccupants — occupant line in dev-queue status (#1243)
# ---------------------------------------------------------------------------


def _patch_tick_for(monkeypatch: pytest.MonkeyPatch, client_name: str) -> None:
    """Force a fresh, non-skip tick for *client_name* so lane breakdown renders."""
    from cw.orchestrate import TickSummary

    tick = TickSummary(
        claimed=0,
        pending=0,
        running=0,
        cap=2,
        skip_reason="none",
        tick_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "cw.cli.dev_queue.status.latest_tick_summary_by_client",
        lambda: {client_name: tick},
    )


def _write_orchestrator_yaml(tmp_orchestrator_config: Path, body: str) -> None:
    """Write an orchestrator.yaml under the redirected config dir."""
    config_dir = tmp_orchestrator_config / ".claude-workspace"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "orchestrator.yaml").write_text(body)


class TestDevQueueLaneBreakdownOccupants:
    """dev-queue status names lane occupants when a lane is noteworthy (#1243)."""

    def test_default_lane_signoff_at_cap_renders_occupant_line(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A signoff-parked default-lane ticket surfaces a 'lane full' occupant line."""
        # load_orchestrator_config() auto-creates orchestrator.yaml with
        # default_ceiling=2 when absent -- pin it to 1 so the single
        # signoff-parked occupant is genuinely at cap (matches the ticket's
        # own worked example), not merely noteworthy-with-headroom.
        _write_orchestrator_yaml(
            tmp_orchestrator_config,
            "default_ceiling: 1\nper_client_ceiling: {}\n",
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-5175",
                client="genhealth",
                status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            )
        )
        _patch_tick_for(monkeypatch, "genhealth")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "lane full: GEN-5175 (awaiting_operator_signoff)" in result.output

    def test_default_lane_running_under_cap_output_unchanged(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single RUNNING task under cap prints no lane line at all (quiet case)."""
        _write_orchestrator_yaml(
            tmp_orchestrator_config,
            "default_ceiling: 2\nper_client_ceiling: {}\n",
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-1",
                client="genhealth",
                status=QueueItemStatus.RUNNING,
            )
        )
        _patch_tick_for(monkeypatch, "genhealth")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "lane full" not in result.output
        assert "    lane " not in result.output

    def test_default_lane_two_running_at_cap_renders_all_occupants(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two RUNNING tasks at cap render both occupants in the lane-full line."""
        _write_orchestrator_yaml(
            tmp_orchestrator_config,
            "default_ceiling: 2\nper_client_ceiling: {}\n",
        )
        add_ticket(
            TicketTask(
                ticket_id="1195",
                client="genhealth",
                status=QueueItemStatus.RUNNING,
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="1198",
                client="genhealth",
                status=QueueItemStatus.RUNNING,
            )
        )
        _patch_tick_for(monkeypatch, "genhealth")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert (
            "lane full: 1195 (running), 1198 (running)" in result.output
            or "lane full: 1198 (running), 1195 (running)" in result.output
        )

    def test_named_lane_quiet_lane_still_prints_base_line_no_occupant_line(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Named-lane client: healthy lane prints base line, noteworthy gets full."""
        ws = tmp_dev_queue / "ws"
        clients_file().write_text(
            "clients:\n"
            "  genhealth:\n"
            f"    workspace_path: {ws}\n"
            "    lanes:\n"
            "      - name: fast\n"
            "        max_parallel: 2\n"
            "      - name: impl\n"
            "        max_parallel: 1\n"
        )
        add_ticket(
            TicketTask(
                ticket_id="FAST-1",
                client="genhealth",
                lane="fast",
                status=QueueItemStatus.RUNNING,
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="IMPL-1",
                client="genhealth",
                lane="impl",
                status=QueueItemStatus.BLOCKED_ON_USER,
            )
        )
        _patch_tick_for(monkeypatch, "genhealth")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "    lane fast:" in result.output
        assert "    lane impl:" in result.output
        # Only the impl lane is noteworthy → exactly one occupant line.
        assert result.output.count("lane full") == 1
        assert "lane full: IMPL-1 (blocked_on_user)" in result.output

    def test_blocked_occupant_under_cap_renders_occupants_not_full(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blocked occupant with lane headroom is noteworthy but not "full"."""
        ws = tmp_dev_queue / "ws"
        clients_file().write_text(
            "clients:\n"
            "  genhealth:\n"
            f"    workspace_path: {ws}\n"
            "    lanes:\n"
            "      - name: impl\n"
            "        max_parallel: 3\n"
        )
        add_ticket(
            TicketTask(
                ticket_id="IMPL-1",
                client="genhealth",
                lane="impl",
                status=QueueItemStatus.BLOCKED_ON_USER,
            )
        )
        _patch_tick_for(monkeypatch, "genhealth")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        # 1 occupant against cap=3 — noteworthy (blocked>0) but not at cap, so
        # the line must not claim the lane is "full".
        assert "lane full" not in result.output
        assert "lane occupants: IMPL-1 (blocked_on_user)" in result.output

    def test_lane_breakdown_client_missing_from_config_falls_back_gracefully(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A queued task for a client absent from clients.yaml falls back to the
        default_ceiling cap and still renders a sensible occupant line."""
        # Pin default_ceiling=1 (load_orchestrator_config() otherwise
        # auto-creates orchestrator.yaml with default_ceiling=2) so the
        # fallback path is deterministically at cap.
        _write_orchestrator_yaml(
            tmp_orchestrator_config,
            "default_ceiling: 1\nper_client_ceiling: {}\n",
        )
        # Inject directly — add_ticket would defer lane validation, but be explicit
        # that this client is not in clients.yaml.
        with _lock():
            store = load_dev_queue()
            store.tasks.append(
                TicketTask(
                    ticket_id="GHOST-1",
                    client="ghost-client",
                    status=QueueItemStatus.RUNNING,
                )
            )
            save_dev_queue(store)
        _patch_tick_for(monkeypatch, "ghost-client")
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        # Fallback cap is the config default_ceiling (1); one RUNNING task
        # fills it, so the default lane is noteworthy despite the missing
        # client config.
        assert "lane full: GHOST-1 (running)" in result.output


class TestLaneCapsForClient:
    """Direct unit tests for _lane_caps_for_client (#1243)."""

    def test_lane_caps_for_client_no_lanes_uses_ceiling_not_max_parallel_default(
        self,
        tmp_dev_queue: Path,
    ) -> None:
        """No declared lanes → single default lane capped at the client ceiling."""
        from cw.cli.dev_queue.status import _lane_caps_for_client

        config = OrchestratorConfig(default_ceiling=3)
        assert _lane_caps_for_client("genhealth", config) == {DEFAULT_LANE: 3}

    def test_lane_caps_for_client_named_lanes_uses_max_parallel(
        self,
        tmp_dev_queue: Path,
    ) -> None:
        """Declared lanes → each lane's max_parallel is its cap."""
        from cw.cli.dev_queue.status import _lane_caps_for_client

        ws = tmp_dev_queue / "ws"
        clients_file().write_text(
            "clients:\n"
            "  genhealth:\n"
            f"    workspace_path: {ws}\n"
            "    lanes:\n"
            "      - name: impl\n"
            "        max_parallel: 2\n"
        )
        config = OrchestratorConfig(default_ceiling=1)
        assert _lane_caps_for_client("genhealth", config) == {"impl": 2}

    def test_lane_caps_for_client_unknown_client_falls_back_to_default_ceiling(
        self,
        tmp_dev_queue: Path,
    ) -> None:
        """Unknown client → single default lane at the config default ceiling."""
        from cw.cli.dev_queue.status import _lane_caps_for_client

        config = OrchestratorConfig(default_ceiling=5)
        assert _lane_caps_for_client("ghost-client", config) == {DEFAULT_LANE: 5}


# ---------------------------------------------------------------------------
# TestStatusFreshnessSubline — freshness block surfaced in dev-queue status (#820)
# ---------------------------------------------------------------------------


class TestStatusFreshnessSubline:
    def test_non_main_head_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """non_main_head tick shows branch name and fix command."""
        from pathlib import Path

        from cw.models import ClientConfig
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-100", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_NON_MAIN_HEAD,
            blocked_branch="docs/foo",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        ws = Path("/repo/my-client")
        monkeypatch.setattr(
            "cw.cli._base.get_client",
            lambda name: ClientConfig(
                name=name, workspace_path=ws, default_branch="main"
            ),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "docs/foo" in result.output
        assert "(not main)" in result.output
        assert "git -C /repo/my-client checkout main" in result.output

    def test_non_main_head_detached(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """non_main_head with blocked_branch=None shows '(detached)'."""
        from pathlib import Path

        from cw.models import ClientConfig
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-101", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_NON_MAIN_HEAD,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        monkeypatch.setattr(
            "cw.cli._base.get_client",
            lambda name: ClientConfig(
                name=name,
                workspace_path=Path("/repo/my-client"),
                default_branch="main",
            ),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "(detached)" in result.output

    def test_non_main_head_get_client_fallback(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to 'main'/client_name when get_client raises CwError."""
        from cw.exceptions import CwError
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-200", client="unknown-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_NON_MAIN_HEAD,
            blocked_branch="feat/x",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"unknown-client": tick},
        )
        msg = "not found"

        def _raise(_name: str) -> None:
            raise CwError(msg)

        monkeypatch.setattr("cw.cli._base.get_client", _raise)
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "feat/x" in result.output
        # fallback: client_name is used as ws_path, 'main' as default_branch
        assert "git -C unknown-client checkout main" in result.output

    def test_main_behind_origin_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """main_behind_origin tick shows 'main behind origin' subline."""
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-102", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_MAIN_BEHIND,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "main behind origin" in result.output
        assert "auto-ff pending/failed" in result.output

    def test_non_freshness_skip_no_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-FRESHNESS_GATE skip reasons produce no freshness subline."""
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-103", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason="no_pending",
            tick_at=now,
            freshness_detail=None,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "⚠" not in result.output

    def test_json_non_main_head(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json emits dict with skip_reason, freshness_detail, blocked_branch."""
        from cw.orchestrate import TickSummary

        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_NON_MAIN_HEAD,
            blocked_branch="docs/foo",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "my-client" in data
        assert data["my-client"]["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        assert data["my-client"]["freshness_detail"] == FRESHNESS_NON_MAIN_HEAD
        assert data["my-client"]["blocked_branch"] == "docs/foo"

    def test_json_empty_when_no_ticks(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json emits {} when there are no tick events."""
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            dict,
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {}

    def test_json_client_filter_applied(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json respects --client filter, returning only matching client."""
        from cw.orchestrate import TickSummary

        now = datetime.now(UTC)
        tick_a = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_NON_MAIN_HEAD,
            blocked_branch=None,
        )
        tick_b = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_MAIN_BEHIND,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"client-a": tick_a, "client-b": tick_b},
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "status", "--json", "--client", "client-a"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert list(data.keys()) == ["client-a"]
        assert "client-b" not in data

    def test_dirty_checkout_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """freshness_detail='main_dirty_checkout' → subline mentions dirty (#766)."""
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-110", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_MAIN_DIRTY_CHECKOUT,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "dirty" in result.output

    def test_diverged_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """freshness_detail='main_diverged_from_origin' → subline mentions diverged."""
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-111", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_MAIN_DIVERGED,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "diverged" in result.output

    def test_detached_subline(
        self,
        tmp_dev_queue: Path,
        tmp_orchestrator_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """freshness_detail='main_detached_head' → subline mentions detached (#964)."""
        from cw.orchestrate import TickSummary

        add_ticket(TicketTask(ticket_id="GEN-112", client="my-client"))
        now = datetime.now(UTC)
        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=3,
            skip_reason=DispatchSkipReason.FRESHNESS_GATE,
            tick_at=now,
            freshness_detail=FRESHNESS_MAIN_DETACHED,
            blocked_branch=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.status.latest_tick_summary_by_client",
            lambda: {"my-client": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "detached" in result.output


# ---------------------------------------------------------------------------
# TestDispatchPlanPersistence
# ---------------------------------------------------------------------------


class TestDispatchPlanPersistence:
    """Save/load round-trip and missing-file behaviour for the dispatch plan."""

    def test_load_when_missing_returns_none(self, tmp_dev_queue: Path) -> None:
        assert load_plan() is None

    def test_save_roundtrip(self, tmp_dev_queue: Path) -> None:
        plan = DispatchPlan(
            tasks=[
                TicketTask(ticket_id="GEN-100", client="genhealth"),
                TicketTask(ticket_id="GEN-101", client="genhealth"),
            ],
            grouping_hints={"GEN-100": "shared dependency with GEN-101"},
        )
        save_plan(plan)
        loaded = load_plan()
        assert loaded is not None
        assert [t.ticket_id for t in loaded.tasks] == ["GEN-100", "GEN-101"]
        assert loaded.grouping_hints == {"GEN-100": "shared dependency with GEN-101"}

    def test_save_overwrites_previous_plan(self, tmp_dev_queue: Path) -> None:
        save_plan(DispatchPlan(tasks=[TicketTask(ticket_id="X-1", client="c")]))
        save_plan(
            DispatchPlan(
                tasks=[
                    TicketTask(ticket_id="Y-1", client="c"),
                    TicketTask(ticket_id="Y-2", client="c"),
                ]
            )
        )
        loaded = load_plan()
        assert loaded is not None
        assert [t.ticket_id for t in loaded.tasks] == ["Y-1", "Y-2"]

    def test_load_malformed_json_returns_none(self, tmp_dev_queue: Path) -> None:
        plan_path().parent.mkdir(parents=True, exist_ok=True)
        plan_path().write_text("not valid json")
        assert load_plan() is None

    def test_load_invalid_schema_returns_none(self, tmp_dev_queue: Path) -> None:
        plan_path().parent.mkdir(parents=True, exist_ok=True)
        plan_path().write_text('{"tasks": [{"missing": "fields"}]}')
        assert load_plan() is None

    def test_plan_path_returns_configured_path(self, tmp_dev_queue: Path) -> None:
        path = plan_path()
        assert path == tmp_dev_queue / "dev_plan.json"


# ---------------------------------------------------------------------------
# TestConcurrentAccess
# ---------------------------------------------------------------------------


_RACE_WRITER_COUNT = 4
_RACE_READER_COUNT = 4
_RACE_DURATION_SECONDS = 0.4
_RACE_SEED_TICKETS = 40


class TestConcurrentAccess:
    """Concurrent writers + readers must never observe a partial JSON file."""

    def test_reader_never_sees_partial_write(self, tmp_dev_queue: Path) -> None:
        # Seed enough tasks that the serialized file is large enough for a
        # truncating writer to race a reader mid-flight.
        for i in range(_RACE_SEED_TICKETS):
            add_ticket(TicketTask(ticket_id=f"GEN-{i}", client="genhealth"))

        stop = threading.Event()
        reader_errors: list[BaseException] = []

        def writer() -> None:
            while not stop.is_set():
                store = load_dev_queue()
                save_dev_queue(store)

        def reader() -> None:
            while not stop.is_set():
                try:
                    load_dev_queue()
                except (
                    ValueError,
                    OSError,
                ) as exc:  # ValueError covers json.JSONDecodeError
                    reader_errors.append(exc)

        threads = [
            threading.Thread(target=writer, daemon=True)
            for _ in range(_RACE_WRITER_COUNT)
        ] + [
            threading.Thread(target=reader, daemon=True)
            for _ in range(_RACE_READER_COUNT)
        ]
        for t in threads:
            t.start()
        time.sleep(_RACE_DURATION_SECONDS)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert not reader_errors, (
            f"Reader observed {len(reader_errors)} partial writes: {reader_errors[:3]}"
        )

    def test_save_plan_reader_never_sees_partial_write(
        self, tmp_dev_queue: Path
    ) -> None:
        seed = DispatchPlan(
            tasks=[
                TicketTask(ticket_id=f"GEN-{i}", client="genhealth")
                for i in range(_RACE_SEED_TICKETS)
            ]
        )
        save_plan(seed)

        stop = threading.Event()
        reader_errors: list[BaseException] = []

        def writer() -> None:
            while not stop.is_set():
                save_plan(seed)

        # load_plan() intentionally swallows parse errors and returns
        # None, so the reader thread must call the validator directly
        # to expose partial-write observations.
        def reader() -> None:
            path = plan_path()
            while not stop.is_set():
                try:
                    DispatchPlan.model_validate_json(path.read_text())
                except (ValueError, OSError) as exc:
                    reader_errors.append(exc)

        threads = [
            threading.Thread(target=writer, daemon=True)
            for _ in range(_RACE_WRITER_COUNT)
        ] + [
            threading.Thread(target=reader, daemon=True)
            for _ in range(_RACE_READER_COUNT)
        ]
        for t in threads:
            t.start()
        time.sleep(_RACE_DURATION_SECONDS)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert not reader_errors, (
            f"Reader observed {len(reader_errors)} partial writes: {reader_errors[:3]}"
        )


# ---------------------------------------------------------------------------
# TestAddTicketDedupe
# ---------------------------------------------------------------------------


class TestAddTicketDedupe:
    def test_returns_true_on_insert(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="GEN-1", client="genhealth")
        result = add_ticket(task)
        assert result is True

    def test_returns_false_on_pending_duplicate(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="GEN-1", client="genhealth")
        add_ticket(task)
        duplicate = TicketTask(ticket_id="GEN-1", client="genhealth")
        result = add_ticket(duplicate)
        assert result is False
        store = load_dev_queue()
        assert len(store.tasks) == 1

    def test_returns_false_on_running_duplicate(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="GEN-2", client="genhealth", status=QueueItemStatus.RUNNING
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        duplicate = TicketTask(ticket_id="GEN-2", client="genhealth")
        result = add_ticket(duplicate)
        assert result is False
        store2 = load_dev_queue()
        assert len(store2.tasks) == 1

    def test_blocks_completed_duplicate(self, tmp_dev_queue: Path) -> None:
        """Existing COMPLETED entry blocks re-adding (terminal-sibling dedup, #876)."""
        completed = TicketTask(
            ticket_id="GEN-3", client="genhealth", status=QueueItemStatus.COMPLETED
        )
        save_dev_queue(DevQueueStore(tasks=[completed]))
        new_task = TicketTask(ticket_id="GEN-3", client="genhealth")
        result = add_ticket(new_task)
        assert result is False
        store2 = load_dev_queue()
        assert len(store2.tasks) == 1

    def test_blocks_cancelled_duplicate(self, tmp_dev_queue: Path) -> None:
        """Existing CANCELLED entry blocks re-adding (terminal-sibling dedup, #876)."""
        cancelled = TicketTask(
            ticket_id="GEN-4", client="genhealth", status=QueueItemStatus.CANCELLED
        )
        save_dev_queue(DevQueueStore(tasks=[cancelled]))
        new_task = TicketTask(ticket_id="GEN-4", client="genhealth")
        result = add_ticket(new_task)
        assert result is False
        store2 = load_dev_queue()
        assert len(store2.tasks) == 1

    def test_allows_different_stage_after_completed(self, tmp_dev_queue: Path) -> None:
        """COMPLETED PLAN-stage row does NOT block adding an IMPL-stage row (#876)."""
        completed_plan = TicketTask(
            ticket_id="GEN-5",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[completed_plan]))
        impl_task = TicketTask(ticket_id="GEN-5", client="genhealth", stage=Stage.IMPL)
        result = add_ticket(impl_task)
        assert result is True
        store2 = load_dev_queue()
        assert len(store2.tasks) == 2


# ---------------------------------------------------------------------------
# Shared helper: lane-aware client setup
# ---------------------------------------------------------------------------


def _setup_client_with_lanes(
    tmp_config_dir: Path, tmp_path: Path, lanes: list[str]
) -> None:
    """Write clients.yaml with named lanes for 'genhealth'."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    lane_yaml = "".join(
        f"      - name: {ln}\n        max_parallel: 1\n" for ln in lanes
    )
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  genhealth:\n    workspace_path: {ws}\n    lanes:\n{lane_yaml}"
    )


# ---------------------------------------------------------------------------
# TestAddTicketLaneValidation
# ---------------------------------------------------------------------------


class TestAddTicketLaneValidation:
    """add_ticket rejects tasks whose lane is not declared for the client."""

    @pytest.fixture
    def patched_queue(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Patch queue file paths to tmp_path."""
        monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", tmp_path / "dev_queue.json")
        monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", tmp_path / ".dev_queue.lock")
        return tmp_path

    def test_undeclared_lane_raises_lane_not_found_error(
        self, patched_queue: Path, tmp_config_dir: Path
    ) -> None:
        """add_ticket raises LaneNotFoundError for an undeclared lane."""
        from cw.exceptions import LaneNotFoundError

        _setup_client_with_lanes(tmp_config_dir, patched_queue, ["default"])
        task = TicketTask(ticket_id="GEN-10", client="genhealth", lane="fast")
        with pytest.raises(LaneNotFoundError, match="Lane 'fast' is not declared"):
            add_ticket(task)

    def test_declared_lane_is_accepted(
        self, patched_queue: Path, tmp_config_dir: Path
    ) -> None:
        """add_ticket accepts a task whose lane is declared for the client."""
        _setup_client_with_lanes(tmp_config_dir, patched_queue, ["default", "fast"])
        task = TicketTask(ticket_id="GEN-11", client="genhealth", lane="fast")
        result = add_ticket(task)
        assert result is True
        store = load_dev_queue()
        assert store.tasks[0].lane == "fast"

    def test_unknown_client_skips_lane_validation(self, patched_queue: Path) -> None:
        """add_ticket skips lane validation when the client is not in clients.yaml."""
        task = TicketTask(ticket_id="GEN-12", client="unknown-client", lane="fast")
        result = add_ticket(task)
        assert result is True
        store = load_dev_queue()
        assert store.tasks[0].lane == "fast"


# ---------------------------------------------------------------------------
# Shared helper: pipeline-aware client setup (GitHub #1682)
# ---------------------------------------------------------------------------


def _setup_client_with_pipeline_stages(
    tmp_config_dir: Path, tmp_path: Path, stages: list[str]
) -> None:
    """Write clients.yaml with a restricted pipeline for 'genhealth'."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    stages_yaml = ", ".join(stages)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  genhealth:\n    workspace_path: {ws}\n"
        f"    pipeline:\n      stages: [{stages_yaml}]\n"
    )


# ---------------------------------------------------------------------------
# TestValidateStageInPipeline
# ---------------------------------------------------------------------------


class TestValidateStageInPipeline:
    """Unit tests for the shared ``_validate_stage_in_pipeline`` helper."""

    def test_stage_in_pipeline_does_not_raise(self) -> None:
        from cw.dev_queue.crud import _validate_stage_in_pipeline

        _validate_stage_in_pipeline(
            Stage.IMPL, [Stage.PLAN, Stage.IMPL, Stage.REVIEW], client="genhealth"
        )

    def test_stage_not_in_pipeline_raises(self) -> None:
        from cw.dev_queue.crud import _validate_stage_in_pipeline
        from cw.exceptions import RequeueStageError

        with pytest.raises(
            RequeueStageError,
            match=r"Stage 'review' is not in the pipeline for client 'genhealth'\.",
        ):
            _validate_stage_in_pipeline(
                Stage.REVIEW, [Stage.PLAN, Stage.IMPL], client="genhealth"
            )


# ---------------------------------------------------------------------------
# TestAddTicketStagePlacement (GitHub #1682)
# ---------------------------------------------------------------------------


class TestAddTicketStagePlacement:
    """add_ticket places a task at an explicit stage, mirroring requeue --stage."""

    @pytest.fixture
    def patched_queue(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Patch queue file paths to tmp_path."""
        monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", tmp_path / "dev_queue.json")
        monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", tmp_path / ".dev_queue.lock")
        return tmp_path

    def test_add_ticket_default_stage_is_plan_and_high_water_unset(
        self, patched_queue: Path, tmp_config_dir: Path
    ) -> None:
        """Omitting stage preserves today's default behavior exactly."""
        _setup_client_with_pipeline_stages(
            tmp_config_dir, patched_queue, ["plan", "impl", "review", "finalize"]
        )
        task = TicketTask(ticket_id="GEN-20", client="genhealth")
        result = add_ticket(task)
        assert result is True
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.PLAN
        assert store.tasks[0].stage_high_water is None

    def test_add_ticket_with_stage_override_lands_at_stage_and_raises_high_water(
        self, patched_queue: Path, tmp_config_dir: Path
    ) -> None:
        _setup_client_with_pipeline_stages(
            tmp_config_dir, patched_queue, ["plan", "impl", "review", "finalize"]
        )
        task = TicketTask(ticket_id="GEN-21", client="genhealth", stage=Stage.IMPL)
        result = add_ticket(task)
        assert result is True
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        assert store.tasks[0].stage_high_water == Stage.IMPL
        assert store.tasks[0].status == QueueItemStatus.PENDING

    def test_add_ticket_stage_not_in_client_pipeline_raises(
        self, patched_queue: Path, tmp_config_dir: Path
    ) -> None:
        from cw.exceptions import RequeueStageError

        _setup_client_with_pipeline_stages(
            tmp_config_dir, patched_queue, ["plan", "impl"]
        )
        task = TicketTask(ticket_id="GEN-22", client="genhealth", stage=Stage.REVIEW)
        with pytest.raises(RequeueStageError, match="not in the pipeline"):
            add_ticket(task)

    def test_add_ticket_unknown_client_skips_stage_validation(
        self, patched_queue: Path
    ) -> None:
        """add_ticket skips stage validation when the client is not in clients.yaml."""
        task = TicketTask(ticket_id="GEN-23", client="unknown-client", stage=Stage.IMPL)
        result = add_ticket(task)
        assert result is True
        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL


# ---------------------------------------------------------------------------
# TestRemoveTicket
# ---------------------------------------------------------------------------


class TestRemoveTicket:
    def test_removes_single_match(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="TKT-10", client="genhealth")
        save_dev_queue(DevQueueStore(tasks=[task]))
        remove_ticket("TKT-10", "genhealth")
        store = load_dev_queue()
        assert len(store.tasks) == 0

    def test_raises_on_zero_match(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        pattern = "No dev-queue task found for ticket 'TKT-99' in client 'genhealth'"
        with pytest.raises(CwError, match=pattern):
            remove_ticket("TKT-99", "genhealth")

    def test_raises_on_multi_match_without_remove_all(
        self, tmp_dev_queue: Path
    ) -> None:
        tasks = [
            TicketTask(ticket_id="TKT-5", client="genhealth"),
            TicketTask(ticket_id="TKT-5", client="genhealth"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        pattern = r"Multiple dev-queue tasks \(2\) match ticket 'TKT-5'"
        with pytest.raises(CwError, match=pattern):
            remove_ticket("TKT-5", "genhealth")

    def test_removes_all_with_remove_all_flag(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="TKT-5", client="genhealth"),
            TicketTask(ticket_id="TKT-5", client="genhealth"),
            TicketTask(ticket_id="TKT-6", client="genhealth"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        remove_ticket("TKT-5", "genhealth", remove_all=True)
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].ticket_id == "TKT-6"

    def test_emits_task_deleted_operator_remove(
        self,
        tmp_dev_queue: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """remove_ticket emits task.deleted reason=operator_remove with payload."""
        events = capture_events("cw.dev_queue.crud", OrchestratorEventType.TASK_DELETED)
        task = TicketTask(
            ticket_id="TKT-DEL1",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=Stage.REVIEW,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        remove_ticket("TKT-DEL1", "genhealth")
        assert len(events) == 1
        etype, payload, corr = events[0]
        assert etype == OrchestratorEventType.TASK_DELETED
        assert corr == "TKT-DEL1"
        assert payload["ticket_id"] == "TKT-DEL1"
        assert payload["client"] == "genhealth"
        assert payload["stage"] == Stage.REVIEW
        assert payload["status_at_deletion"] == QueueItemStatus.BLOCKED_ON_USER
        assert payload["reason"] == "operator_remove"

    def test_remove_all_emits_one_event_per_task(
        self,
        tmp_dev_queue: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """remove_all removing N tasks emits N task.deleted events (Decision 2)."""
        events = capture_events("cw.dev_queue.crud", OrchestratorEventType.TASK_DELETED)
        tasks = [
            TicketTask(ticket_id="TKT-DUP", client="genhealth"),
            TicketTask(ticket_id="TKT-DUP", client="genhealth"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        remove_ticket("TKT-DUP", "genhealth", remove_all=True)
        assert len(events) == 2
        assert all(
            p["reason"] == "operator_remove" and p["ticket_id"] == "TKT-DUP"
            for _, p, _ in events
        )


# ---------------------------------------------------------------------------
# TestClearTickets
# ---------------------------------------------------------------------------


class TestClearTickets:
    def test_clears_all_for_client_without_status(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="TKT-A", client="genhealth"),
            TicketTask(ticket_id="TKT-B", client="genhealth"),
            TicketTask(ticket_id="TKT-C", client="other"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        clear_tickets("genhealth")
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].client == "other"

    def test_clears_by_status_filter(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(
                ticket_id="TKT-P", client="genhealth", status=QueueItemStatus.PENDING
            ),
            TicketTask(
                ticket_id="TKT-R",
                client="genhealth",
                status=QueueItemStatus.RUNNING,
            ),
            TicketTask(
                ticket_id="TKT-C",
                client="genhealth",
                status=QueueItemStatus.COMPLETED,
            ),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        clear_tickets("genhealth", status=QueueItemStatus.PENDING)
        store = load_dev_queue()
        ticket_ids = [t.ticket_id for t in store.tasks]
        assert "TKT-P" not in ticket_ids
        assert "TKT-R" in ticket_ids
        assert "TKT-C" in ticket_ids

    def test_returns_count_removed(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="TKT-1", client="genhealth"),
            TicketTask(ticket_id="TKT-2", client="genhealth"),
            TicketTask(ticket_id="TKT-3", client="other"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        count = clear_tickets("genhealth")
        assert count == 2

    def test_emits_task_deleted_operator_clear_per_task(
        self,
        tmp_dev_queue: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """clear_tickets emits one task.deleted (reason=operator_clear) per removed."""
        events = capture_events("cw.dev_queue.crud", OrchestratorEventType.TASK_DELETED)
        tasks = [
            TicketTask(ticket_id="TKT-CL1", client="genhealth"),
            TicketTask(ticket_id="TKT-CL2", client="genhealth"),
            TicketTask(ticket_id="TKT-CL3", client="other"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        clear_tickets("genhealth")
        assert len(events) == 2
        removed_ids = {p["ticket_id"] for _, p, _ in events}
        assert removed_ids == {"TKT-CL1", "TKT-CL2"}
        assert all(p["reason"] == "operator_clear" for _, p, _ in events)
        corrs = {corr for _, _, corr in events}
        assert corrs == {"TKT-CL1", "TKT-CL2"}

    def test_clear_by_status_emits_per_removed(
        self,
        tmp_dev_queue: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Status-filtered clear emits task.deleted only for removed tasks."""
        events = capture_events("cw.dev_queue.crud", OrchestratorEventType.TASK_DELETED)
        tasks = [
            TicketTask(
                ticket_id="TKT-SP",
                client="genhealth",
                status=QueueItemStatus.PENDING,
            ),
            TicketTask(
                ticket_id="TKT-SR",
                client="genhealth",
                status=QueueItemStatus.RUNNING,
            ),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        clear_tickets("genhealth", status=QueueItemStatus.PENDING)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["ticket_id"] == "TKT-SP"
        assert payload["status_at_deletion"] == QueueItemStatus.PENDING
        assert payload["reason"] == "operator_clear"


# ---------------------------------------------------------------------------
# TestCLIDevQueueRemove
# ---------------------------------------------------------------------------


class TestCLIDevQueueRemove:
    def test_remove_happy_path(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(ticket_id="CLI-R1", client="genhealth")
        save_dev_queue(DevQueueStore(tasks=[task]))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "remove", "CLI-R1", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        assert "Removed CLI-R1" in result.output
        store = load_dev_queue()
        assert len(store.tasks) == 0

    def test_remove_zero_match_errors(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "remove", "CLI-MISS", "--client", "genhealth"]
        )
        assert result.exit_code != 0
        assert "No dev-queue task found" in result.output

    def test_remove_multi_match_without_all_errors(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="CLI-DUP", client="genhealth"),
            TicketTask(ticket_id="CLI-DUP", client="genhealth"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "remove", "CLI-DUP", "--client", "genhealth"]
        )
        assert result.exit_code != 0
        assert "Multiple dev-queue tasks" in result.output

    def test_remove_with_all_flag(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="CLI-DUP", client="genhealth"),
            TicketTask(ticket_id="CLI-DUP", client="genhealth"),
            TicketTask(ticket_id="CLI-KEEP", client="genhealth"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "remove", "CLI-DUP", "--client", "genhealth", "--all"]
        )
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].ticket_id == "CLI-KEEP"


# ---------------------------------------------------------------------------
# TestCLIDevQueueClear
# ---------------------------------------------------------------------------


class TestCLIDevQueueClear:
    def test_clear_all_for_client(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(ticket_id="CLI-A", client="genhealth"),
            TicketTask(ticket_id="CLI-B", client="genhealth"),
            TicketTask(ticket_id="CLI-C", client="other"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "clear", "--client", "genhealth"])
        assert result.exit_code == 0, result.output
        assert "Cleared 2" in result.output
        store = load_dev_queue()
        assert len(store.tasks) == 1
        assert store.tasks[0].client == "other"

    def test_clear_with_status_filter(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(
                ticket_id="CLI-P", client="genhealth", status=QueueItemStatus.PENDING
            ),
            TicketTask(
                ticket_id="CLI-R", client="genhealth", status=QueueItemStatus.RUNNING
            ),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "clear",
                "--client",
                "genhealth",
                "--status",
                "pending",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Cleared 1" in result.output
        store = load_dev_queue()
        ticket_ids = [t.ticket_id for t in store.tasks]
        assert "CLI-P" not in ticket_ids
        assert "CLI-R" in ticket_ids

    def test_clear_invalid_status_choice_errors(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "clear",
                "--client",
                "genhealth",
                "--status",
                "bogus",
            ],
        )
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "invalid choice" in result.output


# ---------------------------------------------------------------------------
# TestCancelTicket
# ---------------------------------------------------------------------------


class TestCancelTicket:
    def test_cancel_pending_task_marks_cancelled(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="TKT-C1", client="genhealth", status=QueueItemStatus.PENDING
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cancel_ticket("TKT-C1", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-C1")
        assert t.status == QueueItemStatus.CANCELLED
        assert t.session_id is None

    def test_cancel_running_task_marks_cancelled(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="TKT-C2",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-abc",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cleared = cancel_ticket("TKT-C2", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-C2")
        assert t.status == QueueItemStatus.CANCELLED
        assert t.session_id is None
        # cancel_ticket returns the cleared session_ids atomically
        assert cleared == ["sess-abc"]

    def test_cancel_returns_cleared_session_id(self, tmp_dev_queue: Path) -> None:
        """cancel_ticket returns the cleared session_id list atomically."""
        task = TicketTask(
            ticket_id="TKT-RET",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-xyz",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cleared = cancel_ticket("TKT-RET", "genhealth")
        assert cleared == ["sess-xyz"]

    def test_cancel_pending_task_returns_none_session_id(
        self, tmp_dev_queue: Path
    ) -> None:
        """PENDING tasks have session_id=None; cancel_ticket returns [None]."""
        task = TicketTask(
            ticket_id="TKT-PRET",
            client="genhealth",
            status=QueueItemStatus.PENDING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cleared = cancel_ticket("TKT-PRET", "genhealth")
        assert cleared == [None]

    def test_cancel_clears_escalation_fields(self, tmp_dev_queue: Path) -> None:
        """cancel_ticket clears escalation_parked_at/fired_at (#1015, Q5)."""
        task = TicketTask(
            ticket_id="TKT-ESC",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            escalation_parked_at=datetime.now(UTC),
            escalation_fired_at=datetime.now(UTC),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cancel_ticket("TKT-ESC", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-ESC")
        assert t.escalation_parked_at is None
        assert t.escalation_fired_at is None

    def test_cancel_clears_gate_recipe_failed_latch(self, tmp_dev_queue: Path) -> None:
        """cancel_ticket clears gate_recipe_failed_at (#1065, RFC 0009) — the
        same unconditional-clear treatment as the escalation latch above."""
        task = TicketTask(
            ticket_id="TKT-GRF",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            gate_recipe_failed_at=datetime.now(UTC),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cancel_ticket("TKT-GRF", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-GRF")
        assert t.gate_recipe_failed_at is None

    def test_cancel_clears_attention_digest_buffer_marker(
        self, tmp_dev_queue: Path
    ) -> None:
        """cancel_ticket clears attention_digest_buffered_at (#1162, RFC 0011
        A6) -- same unconditional-clear treatment as the escalation and
        gate-recipe-failure latches above; this is what lets
        cw.cw_operator_events._peek_flushable_digest re-derive live held
        state (R9) for free instead of replaying stored events."""
        task = TicketTask(
            ticket_id="TKT-DIGEST",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            attention_digest_buffered_at=datetime.now(UTC),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cancel_ticket("TKT-DIGEST", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-DIGEST")
        assert t.attention_digest_buffered_at is None

    def test_cancel_already_cancelled_returns_empty_list(
        self, tmp_dev_queue: Path
    ) -> None:
        """Already-CANCELLED tasks are skipped; cancel_ticket returns []."""
        task = TicketTask(
            ticket_id="TKT-ARET",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cleared = cancel_ticket("TKT-ARET", "genhealth")
        assert cleared == []

    def test_cancel_nonexistent_raises_cwerror(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        pattern = "No dev-queue task found for ticket 'TKT-MISS' in client 'genhealth'"
        with pytest.raises(CwError, match=pattern):
            cancel_ticket("TKT-MISS", "genhealth")

    def test_cancel_already_cancelled_is_idempotent(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="TKT-C3",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        # Should not raise
        cancel_ticket("TKT-C3", "genhealth")
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TKT-C3")
        assert t.status == QueueItemStatus.CANCELLED

    def test_cancel_does_not_affect_other_client(self, tmp_dev_queue: Path) -> None:
        tasks = [
            TicketTask(
                ticket_id="TKT-X", client="genhealth", status=QueueItemStatus.PENDING
            ),
            TicketTask(
                ticket_id="TKT-X", client="other-client", status=QueueItemStatus.PENDING
            ),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        cancel_ticket("TKT-X", "genhealth")
        store = load_dev_queue()
        gh_task = next(t for t in store.tasks if t.client == "genhealth")
        other_task = next(t for t in store.tasks if t.client == "other-client")
        assert gh_task.status == QueueItemStatus.CANCELLED
        assert other_task.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# TestCancelTaskForSession
# ---------------------------------------------------------------------------


class TestCancelTaskForSession:
    def test_cancels_running_task_by_session_id(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="SID-1",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-001",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        result = cancel_task_for_session("sess-001")
        assert result is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "SID-1")
        assert t.status == QueueItemStatus.CANCELLED
        assert t.session_id is None

    def test_returns_false_when_no_match(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        result = cancel_task_for_session("nonexistent-sess")
        assert result is False

    def test_ignores_non_running_task(self, tmp_dev_queue: Path) -> None:
        """Only RUNNING tasks are cancelled; PENDING/COMPLETED are ignored."""
        task = TicketTask(
            ticket_id="SID-2",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            session_id="sess-002",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        result = cancel_task_for_session("sess-002")
        assert result is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "SID-2")
        assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# TestCLIDevQueueCancel
# ---------------------------------------------------------------------------


class TestCLIDevQueueCancel:
    def test_cancel_pending_task_via_cli(self, tmp_dev_queue: Path) -> None:
        task = TicketTask(
            ticket_id="CLI-C1", client="genhealth", status=QueueItemStatus.PENDING
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "cancel", "CLI-C1", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        assert "Cancelled CLI-C1" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "CLI-C1")
        assert t.status == QueueItemStatus.CANCELLED

    def test_cancel_nonexistent_errors(self, tmp_dev_queue: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "cancel", "CLI-MISS", "--client", "genhealth"]
        )
        assert result.exit_code != 0
        assert "No dev-queue task found" in result.output


class TestMigrateDevQueue:
    def test_v1_to_v2_fills_total_cost_usd(self) -> None:
        """migrate_dev_queue fills total_cost_usd on tasks missing it."""
        raw = {
            "schema_version": 1,
            "tasks": [
                {
                    "ticket_id": "GEN-1",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["total_cost_usd"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v2_total_cost_preserved_idempotently(self) -> None:
        """Existing total_cost_usd values survive a second migration pass."""
        raw = {
            "schema_version": 2,
            "tasks": [
                {
                    "ticket_id": "GEN-2",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": 2.5,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["total_cost_usd"] == 2.5

    def test_load_dev_queue_migrates_v1_file(self, tmp_config_dir: Path) -> None:
        """load_dev_queue applies migration when loading a v1 file from disk."""
        import json

        from cw.config import dev_queue_file

        v1_data = {
            "schema_version": 1,
            "tasks": [
                {
                    "ticket_id": "GEN-3",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
        dev_queue_file().write_text(json.dumps(v1_data))
        store = load_dev_queue()
        assert store.tasks[0].total_cost_usd is None
        assert store.schema_version == DEV_QUEUE_SCHEMA_VERSION

    def test_migrate_dev_queue_no_tasks(self) -> None:
        """migrate_dev_queue handles missing tasks key without crashing."""
        raw: dict[str, object] = {"schema_version": 1}
        migrated = migrate_dev_queue(raw)
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v2_to_v3_fills_lane_default(self) -> None:
        """migrate_dev_queue fills lane on tasks missing it."""
        raw: dict[str, object] = {
            "schema_version": 2,
            "tasks": [
                {
                    "ticket_id": "GEN-10",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["lane"] == DEFAULT_LANE
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v3_lane_preserved_idempotently(self) -> None:
        """Existing lane values survive a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 3,
            "tasks": [
                {
                    "ticket_id": "GEN-11",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": "custom-lane",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["lane"] == "custom-lane"

    def test_v3_to_v4_fills_task_stage_default(self) -> None:
        """migrate_dev_queue fills stage=DEFAULT_STAGE on tasks missing the key."""
        raw: dict[str, object] = {
            "schema_version": 3,
            "tasks": [
                {
                    "ticket_id": "GEN-20",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stage"] == DEFAULT_STAGE.value
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v3_to_v4_fills_task_stage_base_ref_default(self) -> None:
        """migrate_dev_queue fills stage_base_ref=None on tasks missing the key."""
        raw: dict[str, object] = {
            "schema_version": 3,
            "tasks": [
                {
                    "ticket_id": "GEN-21",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stage_base_ref"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v4_task_stage_preserved_idempotently(self) -> None:
        """Existing stage values survive a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 4,
            "tasks": [
                {
                    "ticket_id": "GEN-22",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                    "stage": Stage.IMPL.value,
                    "stage_base_ref": "abc1234",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stage"] == Stage.IMPL.value
        assert migrated["tasks"][0]["stage_base_ref"] == "abc1234"

    def test_v4_to_v5_fills_disposition_default(self) -> None:
        """migrate_dev_queue fills disposition=None on tasks missing the key."""
        raw: dict[str, object] = {
            "schema_version": 4,
            "tasks": [
                {
                    "ticket_id": "GEN-30",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                    "stage": DEFAULT_STAGE.value,
                    "stage_base_ref": None,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["disposition"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_v4_to_v5_fills_pr_url_default(self) -> None:
        """migrate_dev_queue fills pr_url=None on tasks missing the key."""
        raw: dict[str, object] = {
            "schema_version": 4,
            "tasks": [
                {
                    "ticket_id": "GEN-31",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                    "stage": DEFAULT_STAGE.value,
                    "stage_base_ref": None,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["pr_url"] is None

    def test_v4_to_v5_fills_completed_at_default(self) -> None:
        """migrate_dev_queue fills completed_at=None on tasks missing the key."""
        raw: dict[str, object] = {
            "schema_version": 4,
            "tasks": [
                {
                    "ticket_id": "GEN-32",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                    "stage": DEFAULT_STAGE.value,
                    "stage_base_ref": None,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["completed_at"] is None

    def test_v5_disposition_preserved_idempotently(self) -> None:
        """Existing disposition values survive a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 5,
            "tasks": [
                {
                    "ticket_id": "GEN-33",
                    "client": "test-client",
                    "priority": 0,
                    "status": "completed",
                    "total_cost_usd": None,
                    "lane": DEFAULT_LANE,
                    "stage": DEFAULT_STAGE.value,
                    "stage_base_ref": None,
                    "disposition": "shipped",
                    "pr_url": "https://github.com/foo/bar/pull/1",
                    "completed_at": "2026-06-23T10:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["disposition"] == "shipped"
        assert migrated["tasks"][0]["pr_url"] == "https://github.com/foo/bar/pull/1"

    def test_v7_to_v8_fills_pr_state_default(self) -> None:
        """migrate_dev_queue fills pr_state=None on tasks missing the key (v8)."""
        raw: dict[str, object] = {
            "schema_version": 7,
            "tasks": [
                {
                    "ticket_id": "GEN-40",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["pr_state"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v8_pr_state_preserved_idempotently(self) -> None:
        """Existing pr_state survives a second migration pass (idempotent)."""
        raw: dict[str, object] = {
            "schema_version": 8,
            "tasks": [
                {
                    "ticket_id": "GEN-41",
                    "client": "test-client",
                    "priority": 0,
                    "status": "running",
                    "pr_state": {
                        "state": "OPEN",
                        "ci_ok": True,
                        "attention_state": "ready_to_approve",
                        "hydrated_at": "2026-07-04T10:00:00+00:00",
                    },
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["pr_state"]["state"] == "OPEN"

    def test_migrate_dev_queue_fills_signoff_default(self) -> None:
        """migrate_dev_queue fills signoff=None on tasks missing the key (v9)."""
        raw: dict[str, object] = {
            "schema_version": 8,
            "tasks": [
                {
                    "ticket_id": "GEN-50",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["signoff"] is None

    def test_migrate_dev_queue_bumps_to_v9(self) -> None:
        """migrate_dev_queue bumps schema_version to current regardless of input."""
        raw: dict[str, object] = {"schema_version": 1, "tasks": []}
        migrated = migrate_dev_queue(raw)
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v9_signoff_preserved_idempotently(self) -> None:
        """Existing signoff value survives a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 9,
            "tasks": [
                {
                    "ticket_id": "GEN-51",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "signoff": "operator",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["signoff"] == "operator"

    def test_migrate_dev_queue_fills_escalation_defaults(self) -> None:
        """migrate_dev_queue fills escalation_parked_at/fired_at=None (v10)."""
        raw: dict[str, object] = {
            "schema_version": 9,
            "tasks": [
                {
                    "ticket_id": "GEN-60",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["escalation_parked_at"] is None
        assert migrated["tasks"][0]["escalation_fired_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v10_escalation_fields_preserved_idempotently(self) -> None:
        """Existing escalation timestamps survive a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 10,
            "tasks": [
                {
                    "ticket_id": "GEN-61",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "escalation_parked_at": "2026-07-01T00:00:00+00:00",
                    "escalation_fired_at": None,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["escalation_parked_at"] == (
            "2026-07-01T00:00:00+00:00"
        )
        assert migrated["tasks"][0]["escalation_fired_at"] is None

    def test_migrate_dev_queue_fills_false_park_recovery_backoff_default(
        self,
    ) -> None:
        """migrate_dev_queue fills false_park_recovery_count=0 /
        false_park_recovery_next_eligible_at=None on tasks missing the keys
        (v11, GitHub #1030)."""
        raw: dict[str, object] = {
            "schema_version": 10,
            "tasks": [
                {
                    "ticket_id": "GEN-70",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["false_park_recovery_count"] == 0
        assert migrated["tasks"][0]["false_park_recovery_next_eligible_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v11_false_park_recovery_backoff_preserved_idempotently(self) -> None:
        """Existing false-park-recovery backoff state survives a second
        migration pass."""
        raw: dict[str, object] = {
            "schema_version": 11,
            "tasks": [
                {
                    "ticket_id": "GEN-71",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "false_park_recovery_count": 2,
                    "false_park_recovery_next_eligible_at": (
                        "2026-07-08T00:00:00+00:00"
                    ),
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["false_park_recovery_count"] == 2
        assert migrated["tasks"][0]["false_park_recovery_next_eligible_at"] == (
            "2026-07-08T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_gate_recipe_failed_default(self) -> None:
        """migrate_dev_queue fills gate_recipe_failed_at=None on tasks missing
        the key (v12, GitHub #1065, RFC 0009)."""
        raw: dict[str, object] = {
            "schema_version": 11,
            "tasks": [
                {
                    "ticket_id": "GEN-80",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["gate_recipe_failed_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v12_gate_recipe_failed_at_preserved_idempotently(self) -> None:
        """Existing gate_recipe_failed_at timestamp survives a second
        migration pass."""
        raw: dict[str, object] = {
            "schema_version": 12,
            "tasks": [
                {
                    "ticket_id": "GEN-81",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "gate_recipe_failed_at": "2026-07-08T00:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["gate_recipe_failed_at"] == (
            "2026-07-08T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_escalate_merge_block_default(self) -> None:
        """migrate_dev_queue fills escalate_merge_block_fired_at=None on tasks
        missing the key (v14, GitHub #1099, RFC 0010 P4)."""
        raw: dict[str, object] = {
            "schema_version": 13,
            "tasks": [
                {
                    "ticket_id": "GEN-99",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["escalate_merge_block_fired_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v14_escalate_merge_block_fired_at_preserved_idempotently(self) -> None:
        """Existing escalate_merge_block_fired_at survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 15,
            "tasks": [
                {
                    "ticket_id": "GEN-100",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "escalate_merge_block_fired_at": "2026-07-11T00:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["escalate_merge_block_fired_at"] == (
            "2026-07-11T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_request_reviewer_fired_default(self) -> None:
        """migrate_dev_queue fills request_reviewer_fired_at=None on tasks
        missing the key (v16, GitHub #1197)."""
        raw: dict[str, object] = {
            "schema_version": 15,
            "tasks": [
                {
                    "ticket_id": "GEN-101",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["request_reviewer_fired_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v16_request_reviewer_fired_at_preserved_idempotently(self) -> None:
        """Existing request_reviewer_fired_at survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 16,
            "tasks": [
                {
                    "ticket_id": "GEN-102",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "request_reviewer_fired_at": "2026-07-14T00:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["request_reviewer_fired_at"] == (
            "2026-07-14T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_auto_fix_ci_fired_default(self) -> None:
        """migrate_dev_queue fills auto_fix_ci_fired_at=None on tasks missing
        the key (v17, GitHub #1205)."""
        raw: dict[str, object] = {
            "schema_version": 16,
            "tasks": [
                {
                    "ticket_id": "GEN-103",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["auto_fix_ci_fired_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v17_auto_fix_ci_fired_at_preserved_idempotently(self) -> None:
        """Existing auto_fix_ci_fired_at survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 17,
            "tasks": [
                {
                    "ticket_id": "GEN-104",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "auto_fix_ci_fired_at": "2026-07-16T00:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["auto_fix_ci_fired_at"] == (
            "2026-07-16T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_address_review_fired_default(self) -> None:
        """migrate_dev_queue fills address_review_fired_at=None on tasks missing
        the key (v18, GitHub #1206)."""
        raw: dict[str, object] = {
            "schema_version": 17,
            "tasks": [
                {
                    "ticket_id": "GEN-105",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["address_review_fired_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v18_address_review_fired_at_preserved_idempotently(self) -> None:
        """Existing address_review_fired_at survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 18,
            "tasks": [
                {
                    "ticket_id": "GEN-106",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "address_review_fired_at": "2026-07-16T00:00:00+00:00",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["address_review_fired_at"] == (
            "2026-07-16T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_last_blocked_result_default(self) -> None:
        """migrate_dev_queue fills last_blocked_result=None on tasks missing
        the key (v19, GitHub #1266)."""
        raw: dict[str, object] = {
            "schema_version": 18,
            "tasks": [
                {
                    "ticket_id": "GEN-107",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["last_blocked_result"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v19_last_blocked_result_preserved_idempotently(self) -> None:
        """Existing last_blocked_result survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 19,
            "tasks": [
                {
                    "ticket_id": "GEN-108",
                    "client": "test-client",
                    "priority": 0,
                    "status": "failed",
                    "last_blocked_result": {
                        "status": "blocked",
                        "blocker": {"reason": "status_unknown"},
                    },
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["last_blocked_result"] == {
            "status": "blocked",
            "blocker": {"reason": "status_unknown"},
        }

    def test_migrate_dev_queue_fills_cross_repo_override_default(self) -> None:
        """migrate_dev_queue fills cross_repo_override=False on tasks missing the
        key (v20, GitHub #1198)."""
        raw: dict[str, object] = {
            "schema_version": 19,
            "tasks": [
                {
                    "ticket_id": "GEN-201",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["cross_repo_override"] is False
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v20_cross_repo_override_preserved_idempotently(self) -> None:
        """Existing cross_repo_override survives a second migration."""
        raw: dict[str, object] = {
            "schema_version": 20,
            "tasks": [
                {
                    "ticket_id": "GEN-202",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "cross_repo_override": True,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["cross_repo_override"] is True

    def test_migrate_dev_queue_fills_stage_high_water_default_seeded_from_stage(
        self,
    ) -> None:
        """migrate_dev_queue seeds stage_high_water from the task's current
        stage when the key is missing (v21, GitHub #1361)."""
        raw: dict[str, object] = {
            "schema_version": 20,
            "tasks": [
                {
                    "ticket_id": "GEN-203",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "stage": "impl",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stage_high_water"] == "impl"
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_migrate_dev_queue_fills_stage_high_water_default_when_stage_also_missing(
        self,
    ) -> None:
        """migrate_dev_queue seeds both stage and stage_high_water to
        DEFAULT_STAGE when a legacy task is missing both keys (v21,
        GitHub #1361)."""
        raw: dict[str, object] = {
            "schema_version": 3,
            "tasks": [
                {
                    "ticket_id": "GEN-204",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stage"] == DEFAULT_STAGE.value == "plan"
        assert migrated["tasks"][0]["stage_high_water"] == DEFAULT_STAGE.value == "plan"
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v21_stage_high_water_preserved_idempotently(self) -> None:
        """Existing stage_high_water survives a second migration pass unchanged,
        even when it differs from the task's current stage (a legitimate
        state: the task regressed backward after reaching REVIEW)."""
        raw: dict[str, object] = {
            "schema_version": 21,
            "tasks": [
                {
                    "ticket_id": "GEN-205",
                    "client": "test-client",
                    "priority": 0,
                    "status": "blocked_on_user",
                    "stage": "harden",
                    "stage_high_water": "review",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert twice["tasks"][0]["stage_high_water"] == "review"
        assert twice["tasks"][0]["stage"] == "harden"

    def test_migrate_dev_queue_fills_blocked_reason_default(self) -> None:
        """migrate_dev_queue fills blocked_reason=None on tasks missing
        the key (v22, GitHub #1511)."""
        raw: dict[str, object] = {
            "schema_version": 21,
            "tasks": [
                {
                    "ticket_id": "GEN-206",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["blocked_reason"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_migrate_dev_queue_fills_hold_finalize_default(self) -> None:
        """migrate_dev_queue fills hold_finalize=None on tasks missing the key
        (v23, GitHub #1160, RFC 0011 A3)."""
        raw: dict[str, object] = {
            "schema_version": 22,
            "tasks": [
                {
                    "ticket_id": "GEN-1160",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["hold_finalize"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v23_hold_finalize_preserved_idempotently(self) -> None:
        """An existing hold_finalize value survives a second migration pass."""
        raw: dict[str, object] = {
            "schema_version": 22,
            "tasks": [
                {
                    "ticket_id": "GEN-1160",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "hold_finalize": "manual",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert twice["tasks"][0]["hold_finalize"] == "manual"

    def test_migrate_dev_queue_fills_attention_digest_buffered_default(
        self,
    ) -> None:
        """migrate_dev_queue fills attention_digest_buffered_at=None on tasks
        missing the key (v24, GitHub #1162, RFC 0011 A6)."""
        raw: dict[str, object] = {
            "schema_version": 23,
            "tasks": [
                {
                    "ticket_id": "GEN-1162",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["attention_digest_buffered_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v24_attention_digest_buffered_at_preserved_idempotently(
        self,
    ) -> None:
        """An existing attention_digest_buffered_at value survives a second
        migration pass."""
        raw: dict[str, object] = {
            "schema_version": 23,
            "tasks": [
                {
                    "ticket_id": "GEN-1162",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "attention_digest_buffered_at": "2026-07-01T00:00:00+00:00",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert (
            twice["tasks"][0]["attention_digest_buffered_at"]
            == "2026-07-01T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_salvage_no_sentinel_at_default(self) -> None:
        """migrate_dev_queue fills salvage_no_sentinel_at=None on tasks missing
        the key (v25, GitHub #1638)."""
        raw: dict[str, object] = {
            "schema_version": 24,
            "tasks": [
                {
                    "ticket_id": "GEN-1638",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["salvage_no_sentinel_at"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v25_salvage_no_sentinel_at_preserved_idempotently(self) -> None:
        """An existing salvage_no_sentinel_at value survives a second
        migration pass."""
        raw: dict[str, object] = {
            "schema_version": 24,
            "tasks": [
                {
                    "ticket_id": "GEN-1638",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "salvage_no_sentinel_at": "2026-07-01T00:00:00+00:00",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert (
            twice["tasks"][0]["salvage_no_sentinel_at"] == "2026-07-01T00:00:00+00:00"
        )

    def test_migrate_dev_queue_fills_regressed_into_stage_default(self) -> None:
        """migrate_dev_queue fills regressed_into_stage=None on tasks missing
        the key (v27, GitHub #1794)."""
        raw: dict[str, object] = {
            "schema_version": 26,
            "tasks": [
                {
                    "ticket_id": "GEN-60",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["regressed_into_stage"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v27_regressed_into_stage_preserved_idempotently(self) -> None:
        """An already-stamped regressed_into_stage survives a second migration
        pass -- the filler must be additive, never a reset (GitHub #1794)."""
        raw: dict[str, object] = {
            "schema_version": 27,
            "tasks": [
                {
                    "ticket_id": "GEN-1794",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "regressed_into_stage": "impl",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert twice["tasks"][0]["regressed_into_stage"] == "impl"

    def test_migrate_dev_queue_fills_finalize_regress_branch_head_default(
        self,
    ) -> None:
        """migrate_dev_queue fills finalize_regress_branch_head=None on tasks
        missing the key (v28, GitHub #1717)."""
        raw: dict[str, object] = {
            "schema_version": 27,
            "tasks": [
                {
                    "ticket_id": "GEN-1717",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["finalize_regress_branch_head"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v28_finalize_regress_branch_head_preserved_idempotently(self) -> None:
        """An already-stamped finalize_regress_branch_head survives a second
        migration pass -- the filler must be additive, never a reset (#1717)."""
        raw: dict[str, object] = {
            "schema_version": 28,
            "tasks": [
                {
                    "ticket_id": "GEN-1717B",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "finalize_regress_branch_head": "deadbeef",
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert twice["tasks"][0]["finalize_regress_branch_head"] == "deadbeef"

    def test_migrate_dev_queue_fills_pending_operator_comment_default(self) -> None:
        """migrate_dev_queue fills pending_operator_comment=False on tasks
        missing the key (v29, GitHub #1730)."""
        raw: dict[str, object] = {
            "schema_version": 27,
            "tasks": [
                {
                    "ticket_id": "GEN-61",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["pending_operator_comment"] is False
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v29_pending_operator_comment_preserved_idempotently(self) -> None:
        """An already-raised pending_operator_comment survives a second
        migration pass -- the filler is additive, never a reset (#1730)."""
        raw: dict[str, object] = {
            "schema_version": 29,
            "tasks": [
                {
                    "ticket_id": "GEN-1730",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "pending_operator_comment": True,
                }
            ],
        }
        once = migrate_dev_queue(raw)
        twice = migrate_dev_queue(once)
        assert twice["tasks"][0]["pending_operator_comment"] is True

    def test_v29_migration_fills_both_shared_seam_markers_in_one_pass(self) -> None:
        """A single pre-v28 row gains BOTH shared-seam markers (#1717 + #1730).

        The two fillers were authored on branches that never saw each other, so
        this asserts the composed migrate_dev_queue runs both — a filler dropped
        during a merge/rebase would leave one key absent and fail here.
        """
        raw: dict[str, object] = {
            "schema_version": 27,
            "tasks": [
                {
                    "ticket_id": "GEN-1717-1730",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["finalize_regress_branch_head"] is None
        assert migrated["tasks"][0]["pending_operator_comment"] is False

    def test_migrate_dev_queue_fills_finding_dispositions_default(self) -> None:
        """migrate_dev_queue fills finding_dispositions={} on tasks missing the
        key (v31, GitHub #1838)."""
        raw: dict[str, object] = {
            "schema_version": 29,
            "tasks": [
                {
                    "ticket_id": "GEN-1838",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["finding_dispositions"] == {}
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v31_finding_dispositions_preserved_idempotently(self) -> None:
        """An already-populated ledger survives a second migration pass — the
        filler is additive, never a reset (#1838 R3, forward-only)."""
        ledger = {
            "src/cw/foo.py::bug here": {
                "outcome": "REJECTED",
                "rationale": "settled",
                "recorded_at": "2026-08-16T00:00:00Z",
            }
        }
        raw: dict[str, object] = {
            "schema_version": 31,
            "tasks": [
                {
                    "ticket_id": "GEN-1838",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "finding_dispositions": ledger,
                }
            ],
        }
        twice = migrate_dev_queue(migrate_dev_queue(raw))
        assert twice["tasks"][0]["finding_dispositions"] == ledger

    def test_migrate_dev_queue_fills_ever_spawned_default(self) -> None:
        """migrate_dev_queue fills ever_spawned=True on tasks missing the key
        (v33, GitHub #1631) -- fail-open, since a legacy row carries no record
        of whether it ever spawned and refusing its completion retroactively
        would be worse than the bug."""
        raw: dict[str, object] = {
            "schema_version": 32,
            "tasks": [
                {
                    "ticket_id": "GEN-1631",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["ever_spawned"] is True
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v33_ever_spawned_preserved_idempotently(self) -> None:
        """An explicit ever_spawned=False survives a second migration pass --
        the filler is additive, never an overwrite back to the fail-open
        default (#1838's v31 precedent)."""
        raw: dict[str, object] = {
            "schema_version": 33,
            "tasks": [
                {
                    "ticket_id": "GEN-1631",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "ever_spawned": False,
                }
            ],
        }
        twice = migrate_dev_queue(migrate_dev_queue(raw))
        assert twice["tasks"][0]["ever_spawned"] is False

    def test_ticket_task_hold_finalize_rejects_invalid_literal(self) -> None:
        """hold_finalize is a closed Literal: an unrecognised value fails loud."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TicketTask.model_validate(
                {
                    "ticket_id": "GEN-1160",
                    "client": "test-client",
                    "hold_finalize": "yes",
                }
            )

    def test_migrate_dev_queue_fills_stale_gate_defaults(self) -> None:
        """migrate_dev_queue fills stale_gate_detected_at/blocked_on_pr=None
        on tasks missing the keys (v30, GitHub #1713)."""
        raw: dict[str, object] = {
            "schema_version": 29,
            "tasks": [
                {
                    "ticket_id": "GEN-1713",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stale_gate_detected_at"] is None
        assert migrated["tasks"][0]["blocked_on_pr"] is None
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v31_migration_fills_both_v30_and_v31_fields_in_one_pass(self) -> None:
        """A single pre-v30 row gains BOTH #1713's and #1838's fields.

        The two fillers were authored on branches that never saw each other
        (both originally claimed v30), so this asserts the merged
        migrate_dev_queue runs both — a filler dropped while resolving that
        conflict would leave one key absent and fail here.
        """
        raw: dict[str, object] = {
            "schema_version": 29,
            "tasks": [
                {
                    "ticket_id": "GEN-1713-1838",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["stale_gate_detected_at"] is None
        assert migrated["tasks"][0]["blocked_on_pr"] is None
        assert migrated["tasks"][0]["finding_dispositions"] == {}
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_v32_migration_fills_both_v31_and_v32_fields_in_one_pass(self) -> None:
        """A single pre-v31 row gains BOTH #1838's and #1750's fields.

        Same shape as the v30/v31 collision above, one version later: #1838's
        ``finding_dispositions`` (v31) and #1750's ``unproductive_attempts``
        (v32) were authored on branches that never saw each other and both
        originally claimed v31. Resolving that collision meant merging two
        independently-authored fillers into one dispatch loop; a filler dropped
        in the process would leave one key absent and fail here.
        """
        raw: dict[str, object] = {
            "schema_version": 30,
            "tasks": [
                {
                    "ticket_id": "GEN-1838-1750",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["finding_dispositions"] == {}
        assert migrated["tasks"][0]["unproductive_attempts"] == 0
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_migrate_dev_queue_fills_watched_prs_default(self) -> None:
        """migrate_dev_queue fills watched_prs=[] on a store missing the key (v15)."""
        raw: dict[str, object] = {"schema_version": 14, "tasks": []}
        migrated = migrate_dev_queue(raw)
        assert migrated["watched_prs"] == []
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION == 33

    def test_migrate_dev_queue_preserves_existing_watched_prs(self) -> None:
        """An existing watched_prs list survives migration untouched (idempotent)."""
        raw: dict[str, object] = {
            "schema_version": 15,
            "tasks": [],
            "watched_prs": [
                {
                    "pr_url": "https://github.com/foo/bar/pull/3",
                    "repo": "foo/bar",
                    "pr_number": 3,
                    "source": "cli",
                    "status": "active",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert isinstance(migrated["watched_prs"], list)
        assert len(migrated["watched_prs"]) == 1
        assert migrated["watched_prs"][0]["pr_number"] == 3

    def test_load_dev_queue_migrates_old_file_watched_prs(
        self, tmp_config_dir: Path
    ) -> None:
        """load_dev_queue fills watched_prs=[] when loading a pre-v15 file."""
        import json

        from cw.config import dev_queue_file

        pre_v15 = {
            "schema_version": 14,
            "tasks": [
                {
                    "ticket_id": "GEN-200",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
        dev_queue_file().write_text(json.dumps(pre_v15))
        store = load_dev_queue()
        assert store.watched_prs == []
        assert store.schema_version == DEV_QUEUE_SCHEMA_VERSION

    def test_load_dev_queue_migrates_v2_file_lane(self, tmp_config_dir: Path) -> None:
        """load_dev_queue applies lane migration when loading a v2 file from disk."""
        import json

        from cw.config import dev_queue_file

        v2_data = {
            "schema_version": 2,
            "tasks": [
                {
                    "ticket_id": "GEN-12",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
        dev_queue_file().write_text(json.dumps(v2_data))
        store = load_dev_queue()
        assert store.tasks[0].lane == DEFAULT_LANE
        assert store.schema_version == DEV_QUEUE_SCHEMA_VERSION

    def test_revert_to_pending_preserves_lane(self, tmp_config_dir: Path) -> None:
        """Lane value is preserved when a RUNNING task is reverted to PENDING."""
        task = TicketTask(
            ticket_id="LANE-RT-1",
            client="test-client",
            lane="batch",
            status=QueueItemStatus.RUNNING,
        )
        with _lock():
            store = load_dev_queue()
            store.tasks.append(task)
            save_dev_queue(store)

        # Simulate revert: mutate status in place (as _apply_queue_mutations does)
        with _lock():
            store = load_dev_queue()
            for t in store.tasks:
                if t.ticket_id == "LANE-RT-1":
                    t.status = QueueItemStatus.PENDING
            save_dev_queue(store)

        # Reload and verify lane preserved
        store = load_dev_queue()
        reverted = next(t for t in store.tasks if t.ticket_id == "LANE-RT-1")
        assert reverted.lane == "batch"
        assert reverted.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# TestConsumeCompletedSessionsWrapper
# ---------------------------------------------------------------------------


class TestConsumeCompletedSessionsWrapper:
    """Tests for the consume_completed_sessions wrapper in dev_queue."""

    def test_wrapper_delegates_to_dispatch(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """consume_completed_sessions() delegates to dispatch implementation."""
        from cw.dev_queue import consume_completed_sessions

        called: list[int] = []

        def _consume_side_effect() -> int:
            called.append(1)
            return 3

        monkeypatch.setattr(
            "cw.dispatch.consume_completed_sessions",
            _consume_side_effect,
        )
        result = consume_completed_sessions()
        assert result == 3
        assert called == [1]


# ---------------------------------------------------------------------------
# TestWaitForTerminal
# ---------------------------------------------------------------------------


class TestWaitForTerminal:
    """Tests for wait_for_terminal()."""

    @pytest.fixture(autouse=True)
    def _patch_consume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub consume_completed_sessions so tests don't touch event cursor."""
        monkeypatch.setattr(
            "cw.dev_queue.lifecycle.consume_completed_sessions", lambda: 0
        )

    def test_wait_completed_returns_immediately(self, tmp_config_dir: Path) -> None:
        """COMPLETED ticket returns on the first load, no polling."""
        task = TicketTask(
            ticket_id="GEN-1",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            session_id="sess-1",
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        result = wait_for_terminal("GEN-1", "genhealth", timeout=5, poll_interval=0)
        assert result.status == QueueItemStatus.COMPLETED
        assert result.session_id == "sess-1"

    def test_wait_failed_returns_immediately(self, tmp_config_dir: Path) -> None:
        """FAILED ticket returns immediately."""
        task = TicketTask(
            ticket_id="GEN-2",
            client="genhealth",
            status=QueueItemStatus.FAILED,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        result = wait_for_terminal("GEN-2", "genhealth", timeout=5, poll_interval=0)
        assert result.status == QueueItemStatus.FAILED

    def test_wait_cancelled_returns_immediately(self, tmp_config_dir: Path) -> None:
        """CANCELLED ticket returns immediately."""
        task = TicketTask(
            ticket_id="GEN-3",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        result = wait_for_terminal("GEN-3", "genhealth", timeout=5, poll_interval=0)
        assert result.status == QueueItemStatus.CANCELLED

    def test_wait_blocked_on_user_returns_immediately(
        self, tmp_config_dir: Path
    ) -> None:
        """BLOCKED_ON_USER ticket returns immediately."""
        task = TicketTask(
            ticket_id="GEN-4",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        result = wait_for_terminal("GEN-4", "genhealth", timeout=5, poll_interval=0)
        assert result.status == QueueItemStatus.BLOCKED_ON_USER

    def test_wait_already_terminal_at_start(self, tmp_config_dir: Path) -> None:
        """Already-COMPLETED ticket never enters the polling loop."""
        task = TicketTask(
            ticket_id="GEN-5",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        # timeout=0 — would immediately time out if polling were entered
        result = wait_for_terminal("GEN-5", "genhealth", timeout=0, poll_interval=0)
        assert result.status == QueueItemStatus.COMPLETED

    def test_wait_running_then_completed(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RUNNING on first tick, COMPLETED on second tick."""
        task = TicketTask(
            ticket_id="GEN-6",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-6",
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        call_count = 0

        def _side_effect() -> int:
            nonlocal call_count
            call_count += 1
            # On the second consume call, transition the task to COMPLETED
            if call_count >= 2:
                updated = load_dev_queue()
                for t in updated.tasks:
                    if t.ticket_id == "GEN-6":
                        t.status = QueueItemStatus.COMPLETED
                save_dev_queue(updated)
            return 0

        monkeypatch.setattr(
            "cw.dev_queue.lifecycle.consume_completed_sessions", _side_effect
        )

        result = wait_for_terminal("GEN-6", "genhealth", timeout=60, poll_interval=0)
        assert result.status == QueueItemStatus.COMPLETED
        assert call_count >= 2

    def test_wait_timeout_raises(self, tmp_config_dir: Path) -> None:
        """Stays RUNNING past timeout → raises TimeoutError."""
        task = TicketTask(
            ticket_id="GEN-7",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        with pytest.raises(TimeoutError):
            wait_for_terminal("GEN-7", "genhealth", timeout=0, poll_interval=0)

    def test_wait_not_found_raises_cw_error(self, tmp_config_dir: Path) -> None:
        """Ticket not in queue raises CwError."""
        store = DevQueueStore(tasks=[])
        save_dev_queue(store)

        with pytest.raises(CwError, match="No dev-queue task found"):
            wait_for_terminal("GEN-999", "genhealth", timeout=5, poll_interval=0)

    def test_wait_session_id_none(self, tmp_config_dir: Path) -> None:
        """session_id=None on a terminal task does not cause errors."""
        task = TicketTask(
            ticket_id="GEN-8",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            session_id=None,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        result = wait_for_terminal("GEN-8", "genhealth", timeout=5, poll_interval=0)
        assert result.status == QueueItemStatus.COMPLETED
        assert result.session_id is None


# ---------------------------------------------------------------------------
# TestFindTicket
# ---------------------------------------------------------------------------


class TestFindTicket:
    """Tests for _find_ticket() multi-record disambiguation.

    Regression for GitHub issue #506: when a ticket is re-enqueued after
    a terminal run, _find_ticket must prefer the active (non-terminal)
    record over the stale terminal one.
    """

    def test_find_prefers_active_over_terminal_completed(
        self, tmp_config_dir: Path
    ) -> None:
        """PENDING record returned when COMPLETED + PENDING exist for same ticket."""
        terminal = TicketTask(
            ticket_id="GEN-9",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        active = TicketTask(
            ticket_id="GEN-9",
            client="genhealth",
            status=QueueItemStatus.PENDING,
        )
        store = DevQueueStore(tasks=[terminal, active])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-9", "genhealth")
        assert result.status == QueueItemStatus.PENDING

    def test_find_prefers_running_over_terminal_completed(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING record returned when COMPLETED + RUNNING exist for same ticket."""
        terminal = TicketTask(
            ticket_id="GEN-10",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        active = TicketTask(
            ticket_id="GEN-10",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-10",
        )
        store = DevQueueStore(tasks=[terminal, active])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-10", "genhealth")
        assert result.status == QueueItemStatus.RUNNING
        assert result.session_id == "sess-10"

    def test_find_returns_terminal_when_no_active(self, tmp_config_dir: Path) -> None:
        """Terminal record returned when no active record exists (backward compat)."""
        terminal = TicketTask(
            ticket_id="GEN-11",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        store = DevQueueStore(tasks=[terminal])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-11", "genhealth")
        assert result.status == QueueItemStatus.COMPLETED

    def test_wait_for_terminal_skips_stale_completed_record(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wait_for_terminal does not resolve immediately on stale COMPLETED record.

        When a ticket was completed then re-enqueued (PENDING), the stale
        COMPLETED record must not cause wait_for_terminal to return early.
        The active PENDING record should be returned and polling should proceed.
        """
        monkeypatch.setattr(
            "cw.dev_queue.lifecycle.consume_completed_sessions", lambda: 0
        )

        stale_terminal = TicketTask(
            ticket_id="GEN-12",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        active_pending = TicketTask(
            ticket_id="GEN-12",
            client="genhealth",
            status=QueueItemStatus.PENDING,
        )
        store = DevQueueStore(tasks=[stale_terminal, active_pending])
        save_dev_queue(store)

        # timeout=0 means it should time out (not resolve early on stale record)
        with pytest.raises(TimeoutError):
            wait_for_terminal("GEN-12", "genhealth", timeout=0, poll_interval=0)

    def test_find_cancelled_plus_running_returns_running(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING task wins over old CANCELLED task — regression for #579."""
        from datetime import UTC, datetime, timedelta

        old_ts = datetime(2025, 1, 1, tzinfo=UTC)
        new_ts = old_ts + timedelta(hours=1)
        cancelled = TicketTask(
            ticket_id="GEN-579",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
            created_at=old_ts,
        )
        running = TicketTask(
            ticket_id="GEN-579",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-579",
            created_at=new_ts,
        )
        store = DevQueueStore(tasks=[cancelled, running])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-579", "genhealth")
        assert result.status == QueueItemStatus.RUNNING
        assert result.session_id == "sess-579"

    def test_find_completed_only_returns_completed(self, tmp_config_dir: Path) -> None:
        """Only a COMPLETED task in queue → returns it (no live tasks)."""
        task = TicketTask(
            ticket_id="GEN-580",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-580", "genhealth")
        assert result.status == QueueItemStatus.COMPLETED

    def test_find_multi_live_returns_newest_and_warns(
        self, tmp_config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two live PENDING tasks → newest created_at wins; warning on stderr."""
        from datetime import UTC, datetime, timedelta

        old_ts = datetime(2025, 2, 1, tzinfo=UTC)
        new_ts = old_ts + timedelta(hours=2)
        older = TicketTask(
            ticket_id="GEN-581",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            created_at=old_ts,
        )
        newer = TicketTask(
            ticket_id="GEN-581",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            created_at=new_ts,
        )
        store = DevQueueStore(tasks=[older, newer])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-581", "genhealth")
        assert result.created_at == new_ts
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "GEN-581" in captured.err

    def test_find_blocked_on_user_beats_cancelled(self, tmp_config_dir: Path) -> None:
        """BLOCKED_ON_USER wins over CANCELLED when no live (PENDING/RUNNING) task."""
        from datetime import UTC, datetime, timedelta

        old_ts = datetime(2025, 3, 1, tzinfo=UTC)
        new_ts = old_ts + timedelta(minutes=30)
        cancelled = TicketTask(
            ticket_id="GEN-582",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
            created_at=old_ts,
        )
        blocked = TicketTask(
            ticket_id="GEN-582",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            created_at=new_ts,
        )
        store = DevQueueStore(tasks=[cancelled, blocked])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-582", "genhealth")
        assert result.status == QueueItemStatus.BLOCKED_ON_USER

    def test_find_explicit_created_at_tiebreak(self, tmp_config_dir: Path) -> None:
        """Two RUNNING tasks → max created_at wins regardless of list position."""
        from datetime import UTC, datetime, timedelta

        base = datetime(2025, 4, 1, tzinfo=UTC)
        earlier = TicketTask(
            ticket_id="GEN-583",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-old",
            created_at=base,
        )
        later = TicketTask(
            ticket_id="GEN-583",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-new",
            created_at=base + timedelta(hours=3),
        )
        # Put the later one FIRST in the list to confirm max() not index
        store = DevQueueStore(tasks=[later, earlier])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-583", "genhealth")
        assert result.session_id == "sess-new"

    def test_find_ticket_prefers_awaiting_signoff_over_terminal_duplicate(
        self, tmp_config_dir: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF wins over CANCELLED (mirrors BLOCKED_ON_USER).

        See GitHub #990.
        """
        from datetime import UTC, datetime, timedelta

        old_ts = datetime(2025, 5, 1, tzinfo=UTC)
        new_ts = old_ts + timedelta(minutes=30)
        cancelled = TicketTask(
            ticket_id="GEN-990",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
            created_at=old_ts,
        )
        signoff_parked = TicketTask(
            ticket_id="GEN-990",
            client="genhealth",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            created_at=new_ts,
        )
        store = DevQueueStore(tasks=[cancelled, signoff_parked])
        save_dev_queue(store)

        loaded = load_dev_queue()
        result = _find_ticket(loaded, "GEN-990", "genhealth")
        assert result.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF


# ---------------------------------------------------------------------------
# TestMoveTicket
# ---------------------------------------------------------------------------


class TestMoveTicket:
    """Tests for move_ticket()."""

    def test_move_ticket_pending_success(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """PENDING ticket moves to target lane; returns old from_lane."""
        from cw.dev_queue import move_ticket

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default", "fast"])
        task = TicketTask(
            ticket_id="GEN-200",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from_lane = move_ticket("GEN-200", "genhealth", "fast")

        assert from_lane == "default"
        store = load_dev_queue()
        moved = next(t for t in store.tasks if t.ticket_id == "GEN-200")
        assert moved.lane == "fast"

    def test_move_ticket_running_raises_lane_move_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """RUNNING ticket raises LaneMoveError."""
        from cw.dev_queue import move_ticket
        from cw.exceptions import LaneMoveError

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default", "fast"])
        task = TicketTask(
            ticket_id="GEN-201",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(LaneMoveError):
            move_ticket("GEN-201", "genhealth", "fast")

    def test_move_ticket_blocked_on_user_raises_lane_move_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """BLOCKED_ON_USER ticket raises LaneMoveError."""
        from cw.dev_queue import move_ticket
        from cw.exceptions import LaneMoveError

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default", "fast"])
        task = TicketTask(
            ticket_id="GEN-202",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(LaneMoveError):
            move_ticket("GEN-202", "genhealth", "fast")

    def test_move_ticket_rejects_awaiting_signoff(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF ticket raises LaneMoveError (#990)."""
        from cw.dev_queue import move_ticket
        from cw.exceptions import LaneMoveError

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default", "fast"])
        task = TicketTask(
            ticket_id="GEN-204",
            client="genhealth",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(LaneMoveError):
            move_ticket("GEN-204", "genhealth", "fast")

    def test_move_ticket_undeclared_lane_raises_lane_not_found_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Undeclared target lane raises LaneNotFoundError."""
        from cw.dev_queue import move_ticket
        from cw.exceptions import LaneNotFoundError

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default"])
        task = TicketTask(
            ticket_id="GEN-203",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(LaneNotFoundError):
            move_ticket("GEN-203", "genhealth", "undeclared-lane")

    def test_move_ticket_not_found_raises_cw_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Non-existent ticket raises CwError."""
        from cw.dev_queue import move_ticket

        _setup_client_with_lanes(tmp_config_dir, tmp_path, ["default", "fast"])
        save_dev_queue(DevQueueStore(tasks=[]))

        with pytest.raises(CwError, match="No dev-queue task found"):
            move_ticket("GEN-MISSING", "genhealth", "fast")


# ---------------------------------------------------------------------------
# TestDevQueueTasks — cw dev-queue tasks
# ---------------------------------------------------------------------------


class TestDevQueueTasks:
    def _three_tasks(self) -> list[TicketTask]:
        return [
            TicketTask(
                ticket_id="238",
                client="claude-workspace",
                status=QueueItemStatus.RUNNING,
                session_id="sess0001",
                attempts=2,
                lane="default",
            ),
            TicketTask(
                ticket_id="239",
                client="claude-workspace",
                status=QueueItemStatus.PENDING,
                session_id=None,
                attempts=0,
                lane="default",
            ),
            TicketTask(
                ticket_id="240",
                client="other-client",
                status=QueueItemStatus.COMPLETED,
                session_id="sess0003",
                attempts=1,
                lane="fast",
            ),
        ]

    def test_tasks_json_all(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=self._three_tasks()))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "tasks", "--json"])
        assert result.exit_code == 0
        tasks = json.loads(result.output)
        assert isinstance(tasks, list)
        assert len(tasks) == 3
        assert set(tasks[0].keys()) == set(TicketTask.model_fields.keys())

    def test_tasks_filter_by_client(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=self._three_tasks()))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "tasks", "--client", "claude-workspace", "--json"]
        )
        assert result.exit_code == 0
        tasks = json.loads(result.output)
        assert len(tasks) == 2
        assert all(t["client"] == "claude-workspace" for t in tasks)

    def test_tasks_filter_by_status(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=self._three_tasks()))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "tasks", "--status", "running", "--json"]
        )
        assert result.exit_code == 0
        tasks = json.loads(result.output)
        assert len(tasks) == 1
        assert tasks[0]["ticket_id"] == "238"
        assert tasks[0]["status"] == "running"

    def test_tasks_filter_by_ticket(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=self._three_tasks()))
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "tasks", "--ticket", "238", "--json"]
        )
        assert result.exit_code == 0
        tasks = json.loads(result.output)
        assert len(tasks) == 1
        assert tasks[0]["ticket_id"] == "238"

    def test_tasks_human_output_columns(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=self._three_tasks()))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "tasks"])
        assert result.exit_code == 0
        assert "TICKET_ID" in result.output
        assert "CLIENT" in result.output
        assert "STATUS" in result.output
        assert "SESSION_ID" in result.output
        assert "ATTEMPTS" in result.output
        assert "LANE" in result.output
        assert "DISPOSITION" in result.output
        assert "PR" in result.output

    def test_tasks_empty_output(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "tasks"])
        assert result.exit_code == 0
        assert "No tasks found" in result.output


# ---------------------------------------------------------------------------
# Helpers for approve/requeue/unblock tests
# ---------------------------------------------------------------------------


def _write_client_yaml(tmp_config_dir: Path, tmp_path: Path) -> None:
    """Write a minimal clients.yaml for 'genhealth' with default workspace."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  genhealth:\n    workspace_path: {ws}\n"
    )


def _make_blocked_task(
    ticket_id: str = "GEN-500",
    client: str = "genhealth",
    stage: Stage = Stage.PLAN,
    session_id: str | None = "sess1234",
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    disposition: str | None = None,
    scope_hint: str | None = None,
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=status,
        stage=stage,
        session_id=session_id,
        disposition=disposition,
        scope_hint=scope_hint,
    )


def _make_session(
    session_id: str = "sess1234",
    last_result: dict[str, object] | None = None,
    reap_reason: ReapReason | None = None,
    workspace_path: Path | None = None,
) -> Session:
    """Build a Session with minimal required fields."""
    from pathlib import Path

    from cw.models import SessionOrigin

    return _make_daemon_session(
        id=session_id,
        name=f"genhealth/impl-{session_id}",
        client="genhealth",
        origin=SessionOrigin.USER,
        workspace_path=workspace_path or Path("/tmp/ws"),
        surface_ref=None,
        worktree_path=None,
        started_at=datetime.now(UTC),
        last_result=last_result,
        reap_reason=reap_reason,
    )


# ---------------------------------------------------------------------------
# TestApproveTicket — approve_ticket() mutation function
# ---------------------------------------------------------------------------


class TestApproveTicket:
    """Tests for approve_ticket()."""

    def test_approve_plan_pending_advances_to_impl(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """plan_pending_approval BLOCKED task advances to impl PENDING when the
        plan-of-record is already quality-reviewed (#968)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess0001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        assert result["plan_requeued"] is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None

    def test_approve_then_re_park_starts_escalation_latch_fresh(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q5 regression: approve clears a stale latch; a subsequent re-park
        starts the escalation window fresh rather than inheriting the old
        (stale) parked_at/fired_at timestamps (#1015)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        stale_parked_at = datetime(2020, 1, 1, tzinfo=UTC)
        stale_fired_at = datetime(2020, 1, 1, tzinfo=UTC)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess0001")
        task.escalation_parked_at = stale_parked_at
        task.escalation_fired_at = stale_fired_at
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        approve_ticket("GEN-500", "genhealth")

        store = load_dev_queue()
        approved = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert approved.escalation_parked_at is None
        assert approved.escalation_fired_at is None

        # Re-park the same row (simulating a fresh BLOCKED_ON_USER episode) —
        # the latch must start clean, not resurrect the stale 2020 timestamps.
        transition_task_status(approved, QueueItemStatus.BLOCKED_ON_USER)
        save_dev_queue(store)
        store = load_dev_queue()
        reparked = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert reparked.escalation_parked_at is None
        assert reparked.escalation_fired_at is None

    def test_approve_review_pending_advances_to_finalize(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """review_pending_approval BLOCKED task advances to finalize PENDING."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess0002")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0002",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.FINALIZE

    def test_approve_wrong_last_result_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Non-approval last_result status raises ApproveGateError."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess0003")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0003",
            last_result={"status": "ambiguities_pending_resolution"},
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError, match="not at an approval gate"):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_fails_closed_on_branch_staleness_park(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#1823: a branch-staleness park cannot be released by ``approve``.

        The row still carries ``review_pending_approval`` on its *sentinel* --
        the staleness gate diverges only ``task.disposition`` -- so without the
        explicit disposition check in ``_not_at_approval_gate`` the row would
        read as "at the approval gate" and silently ship a stale tree.
        """
        from cw.config import save_state
        from cw.dev_queue import BRANCH_STALENESS_GATE_DISPOSITION, approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess0009",
            disposition=BRANCH_STALENESS_GATE_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0009",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError, match="not at an approval gate"):
            approve_ticket("GEN-500", "genhealth")

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.REVIEW
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_approve_missing_session_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Task with unknown session_id raises ApproveGateError."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="no-such-session")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        with pytest.raises(ApproveGateError, match="session not found"):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_non_blocked_task_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """PENDING task raises ApproveGateError (wrong status)."""
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = TicketTask(
            ticket_id="GEN-500",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(ApproveGateError, match="expected BLOCKED_ON_USER"):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_null_last_result_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Session exists but last_result=None raises ApproveGateError."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess0004")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(session_id="sess0004", last_result=None)
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError, match="not at an approval gate"):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_terminal_stage_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Task at terminal pipeline stage raises ApproveGateError."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.FINALIZE, session_id="sess0005")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0005",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError, match="terminal stage"):
            approve_ticket("GEN-500", "genhealth")

    # -- Operator-signoff gates (RFC 0007 Phase 3, #990) ---------------------

    def test_approve_ticket_returns_awaiting_signoff_false_on_plain_advance(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordinary (no-signoff) approval returns awaiting_signoff=False."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-plain-1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-plain-1",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is False
        assert result["to_stage"] == "impl"

    def test_approve_small_signoff_parked_ticket_clears_to_finalize_pending(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF at REVIEW -> approve clears to FINALIZE PENDING.

        No session/last_result validation on this arm -- the signoff gate is
        purely an operator-authorization state, not an AutoDevResult approval
        gate (#990).
        """
        from cw.dev_queue import approve_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is False
        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.FINALIZE

    def test_approve_signoff_parked_at_terminal_stage_completes(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF already at the terminal stage -> COMPLETED.

        Exercises _clear_signoff_gate's terminal-stage branch (mirrors
        _stage_advance_unchecked's COMPLETED arm) -- this is the second
        approve in the large-ticket two-approval flow once the stage pointer
        has already been advanced to the pipeline's last stage.
        """
        from cw.dev_queue import approve_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.FINALIZE,
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is False
        assert result["from_stage"] == "finalize"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "signoff_gate"

    def test_approve_large_review_pending_with_signoff_parks(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """review_pending_approval at REVIEW + ticket signoff -> re-routes to
        AWAITING_OPERATOR_SIGNOFF instead of advancing straight to FINALIZE."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-signoff-1")
        task.signoff = "operator"
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-signoff-1",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is True
        assert result["from_stage"] == "review"
        assert result["to_stage"] == "review"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert t.disposition == "signoff_gate"
        assert t.stage == Stage.REVIEW

    def test_approve_signoff_park_emits_session_needs_attention(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """The `approve` CLI path's own signoff-park site (approval.py) emits
        SESSION_NEEDS_ATTENTION too, via the shared _park_signoff_gate helper
        (#1552). The emit executes inside cw.dispatch.review_gates (where
        _park_signoff_gate is defined since the #1823 extraction) regardless of
        the calling module."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.dispatch import _SIGNOFF_GATE_REASON
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        attention = capture_events(
            "cw.dispatch.review_gates", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-signoff-attn")
        task.signoff = "operator"
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-signoff-attn",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is True
        assert len(attention) == 1
        _, payload, correlation_id = attention[0]
        assert payload["paused_status"] == _SIGNOFF_GATE_REASON
        assert payload["breadcrumbs"] == ""
        assert payload["ticket_id"] == "GEN-500"
        assert payload["client"] == "genhealth"
        assert correlation_id == "GEN-500"

    def test_approve_large_signoff_second_approve_releases_to_pending(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A second `approve` clears the signoff-parked gate to FINALIZE PENDING."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-signoff-2")
        task.signoff = "operator"
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-signoff-2",
            last_result={"status": "review_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        first = approve_ticket("GEN-500", "genhealth")
        assert first["awaiting_signoff"] is True

        second = approve_ticket("GEN-500", "genhealth")

        assert second["awaiting_signoff"] is False
        assert second["from_stage"] == "review"
        assert second["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.FINALIZE

    def test_approve_awaiting_signoff_twice_errors_cleanly(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Approving an already-cleared (now PENDING) ticket raises cleanly."""
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        approve_ticket("GEN-500", "genhealth")  # clears -> PENDING at FINALIZE

        with pytest.raises(
            ApproveGateError, match="BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF"
        ):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_signoff_parked_stage_not_in_pipeline_errors_cleanly(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF ticket whose stage isn't in the client's
        pipeline raises ApproveGateError, not an unhandled ValueError.

        Mirrors the existing BLOCKED_ON_USER arm's `task.stage not in stages`
        guard (dev_queue.py ~:795), which _clear_signoff_gate's caller must
        also apply before calling `_advance_task_pointer` (#990).
        """
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.HARDEN,  # not in the default pipeline stages
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(ApproveGateError, match="not in pipeline"):
            approve_ticket("GEN-500", "genhealth")

    def test_approve_advance_emits_single_stage_changed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Approve→advance emits exactly one task.stage_changed (no double-emit).

        One real stage move (plan→impl via _advance_task_pointer) must produce
        exactly one task.stage_changed with direction=advance (RFC 0008 W1).
        """
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-adv1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-adv1",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is False
        assert len(events) == 1
        _, payload, corr = events[0]
        assert corr == "GEN-500"
        assert payload["old_stage"] == Stage.PLAN
        assert payload["new_stage"] == Stage.IMPL
        assert payload["direction"] == "advance"

    def test_approve_unreviewed_plan_requeue_emits_no_stage_changed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sibling to test_approve_advance_emits_single_stage_changed: a
        same-stage requeue (unreviewed plan) is not a real stage move, so it
        must emit no task.stage_changed at all (#968)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-adv2")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-adv2",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is True
        assert events == []

    def test_approve_plan_pending_without_markers_requeues_plan_stage(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unreviewed plan (no tracker comment, no worktree fallback) re-parks
        the row at Stage.PLAN/PENDING instead of advancing to impl -- the
        core #968 fix (ticket option (a))."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-unrev1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-unrev1",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "plan"
        assert result["plan_requeued"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.PLAN
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None

    def test_approve_plan_pending_cw_plan_md_fallback_requeues_or_advances(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tracker fetch returns None; `.cw/plan.md` fallback carries both
        signoff markers -> approve advances to impl (plan_requeued=False)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-fallback1")
        task.worktree_path = tmp_path / "wt"
        (task.worktree_path / ".cw").mkdir(parents=True)
        (task.worktree_path / ".cw" / "plan.md").write_text(
            plan_body(), encoding="utf-8"
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-fallback1",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is False
        assert result["to_stage"] == "impl"

    def test_approve_plan_pending_worktree_without_plan_md_requeues(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tracker fetch returns None; worktree_path is set but `.cw/plan.md`
        does not exist -> the `.cw/plan.md`-fallback's `.exists()` guard fails
        closed and approve re-queues at plan stage (#968)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-noplanmd")
        task.worktree_path = tmp_path / "wt-noplanmd"
        task.worktree_path.mkdir(parents=True)
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-noplanmd",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is True
        assert result["to_stage"] == "plan"

    def test_approve_plan_pending_unclosed_marker_requeues(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plan body whose soundness marker is present but never closed with
        ``-->`` must NOT be treated as reviewed -- regression test for the
        unified predicate (#1567). Before the fix, ``_plan_is_reviewed`` did a
        bare ``marker in body`` check, so an unclosed marker satisfied it and
        approve incorrectly advanced the ticket to impl. Mirrors the unclosed-
        marker fixture in test_reconcile_gate_recipes.py's
        test_unclosed_marker_yields_none."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        unclosed_body = (
            "# Plan\n\n"
            "<!-- plan-spec-reviewed: 2026-07-08 v2 -->\n"
            "<!-- plan-soundness-reviewed: 2026-07-08 v1 unterminated, no close"
        )
        stub_fetch_plan(
            monkeypatch,
            unclosed_body,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-unclosed1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-unclosed1",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is True
        assert result["to_stage"] == "plan"

    def test_approve_plan_pending_plan_md_read_error_requeues(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.cw/plan.md` exists but is unreadable as text (here: a directory,
        triggering IsADirectoryError, an OSError subclass) -> the read failure
        degrades to "not reviewed" rather than propagating, and approve
        re-queues at plan stage (#968)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-planmderr")
        task.worktree_path = tmp_path / "wt-planmderr"
        (task.worktree_path / ".cw" / "plan.md").mkdir(parents=True)
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-planmderr",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["plan_requeued"] is True
        assert result["to_stage"] == "plan"


# ---------------------------------------------------------------------------
# TestApproveTicketLockedResolved — _approve_ticket_locked(resolved_task=...)
# ---------------------------------------------------------------------------


class TestApproveTicketLockedResolved:
    """RFC 0009 / #1083: the gate-recipe path threads the validated row's
    identity through _approve_ticket_locked so the mutation acts on THAT row,
    never a re-resolved duplicate."""

    def test_resolved_task_acts_on_that_row_despite_newer_awaiting_duplicate(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolved_task pins the mutation to the caller-validated
        BLOCKED_ON_USER row A even when a strictly-newer
        AWAITING_OPERATOR_SIGNOFF duplicate B exists (which _find_ticket would
        otherwise select via _APPROVABLE_STATUSES newest-wins)."""
        from cw.config import save_state
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        row_a = _make_blocked_task(stage=Stage.PLAN, session_id="sess0001")
        row_a.created_at = datetime(2026, 7, 1, tzinfo=UTC)
        row_b = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess0002",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        row_b.created_at = datetime(2026, 7, 8, tzinfo=UTC)
        save_dev_queue(DevQueueStore(tasks=[row_a, row_b]))
        session = _make_session(
            session_id="sess0001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        with dev_queue_lock():
            result = _approve_ticket_locked("GEN-500", "genhealth", resolved_task=row_a)

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        store = load_dev_queue()
        # Key on created_at (stable identity): row A's session_id is cleared to
        # None on advance, so it can't identify both rows post-approve.
        by_created = {t.created_at: t for t in store.tasks}
        row_a = by_created[datetime(2026, 7, 1, tzinfo=UTC)]
        row_b = by_created[datetime(2026, 7, 8, tzinfo=UTC)]
        assert row_a.stage == Stage.IMPL
        assert row_a.status == QueueItemStatus.PENDING
        # Row B's signoff gate untouched.
        assert row_b.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert row_b.stage == Stage.REVIEW

    def test_resolved_task_status_mismatch_raises_approve_gate_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Belt-and-suspenders: if the row matched by stable identity no longer
        holds the status the caller validated (e.g. same (ticket_id, client,
        created_at) but the live row is AWAITING while the caller validated
        BLOCKED_ON_USER), fail closed rather than clear a gate never checked."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock
        from cw.exceptions import ApproveGateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        ts = datetime(2026, 7, 3, tzinfo=UTC)
        live = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        live.created_at = ts
        save_dev_queue(DevQueueStore(tasks=[live]))
        # Detached spec with matching identity but the OTHER (validated) status.
        detached = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-x")
        detached.created_at = ts

        with dev_queue_lock(), pytest.raises(ApproveGateError):
            _approve_ticket_locked("GEN-500", "genhealth", resolved_task=detached)

        # The signoff row was NOT cleared.
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert store.tasks[0].stage == Stage.REVIEW

    def test_resolved_task_row_vanished_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """If the resolved row's stable identity is absent from the freshly
        loaded store (e.g. a concurrent delete), fail closed."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock
        from cw.exceptions import ApproveGateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        save_dev_queue(DevQueueStore(tasks=[]))
        gone = _make_blocked_task(stage=Stage.PLAN, session_id="sess-gone")
        gone.created_at = datetime(2026, 7, 4, tzinfo=UTC)

        with dev_queue_lock(), pytest.raises(ApproveGateError):
            _approve_ticket_locked("GEN-500", "genhealth", resolved_task=gone)


# ---------------------------------------------------------------------------
# TestApproveScopeHintGateRelease — #1640: approve must release a
# scope_hint-gated park (disposition="approval_gate"), not just the
# plan_pending_approval/review_pending_approval last_result statuses.
# ---------------------------------------------------------------------------


class TestApproveScopeHintGateRelease:
    """Adjacent to TestApproveTicket (#1640 regression coverage)."""

    def test_approve_scope_hint_gated_park_releases_via_disposition(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A task parked BLOCKED_ON_USER by _park_scope_hint_gate (disposition
        'approval_gate', last_result.status 'stage_complete') must release on
        approve, not raise (#1630 regression)."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-1630",
            disposition="approval_gate",
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-1630",
            last_result={"status": "stage_complete"},
        )
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.FINALIZE
        assert t.status == QueueItemStatus.PENDING

    def test_approve_scope_hint_gated_park_matches_review_pending_approval(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Parity: the disposition='approval_gate' release path produces the
        same result shape and (stage, status) as the existing
        review_pending_approval release path."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)

        task_a = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-parity-a")
        save_dev_queue(DevQueueStore(tasks=[task_a]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-parity-a",
                        last_result={"status": "review_pending_approval"},
                    )
                ]
            )
        )
        result_a = approve_ticket("GEN-500", "genhealth")
        store_a = load_dev_queue()
        t_a = next(t for t in store_a.tasks if t.ticket_id == "GEN-500")

        task_b = _make_blocked_task(
            ticket_id="GEN-501",
            stage=Stage.REVIEW,
            session_id="sess-parity-b",
            disposition="approval_gate",
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task_b]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-parity-b",
                        last_result={"status": "stage_complete"},
                    )
                ]
            )
        )
        result_b = approve_ticket("GEN-501", "genhealth")
        store_b = load_dev_queue()
        t_b = next(t for t in store_b.tasks if t.ticket_id == "GEN-501")

        assert result_a.keys() == result_b.keys()
        assert result_a["from_stage"] == result_b["from_stage"]
        assert result_a["to_stage"] == result_b["to_stage"]
        assert result_a["awaiting_signoff"] == result_b["awaiting_signoff"]
        assert result_a["plan_requeued"] == result_b["plan_requeued"]
        assert result_a["finalize_held"] == result_b["finalize_held"]
        assert (t_a.stage, t_a.status) == (t_b.stage, t_b.status)

    def test_approve_error_message_no_longer_suggests_requeue(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A genuinely-not-at-gate task raises without the misleading
        'requeue' suggestion, which belongs to the missing-session message
        (approval.py:286-292) but not to this disposition/last_result gate."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-no-gate")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-no-gate",
            last_result={"status": "ambiguities_pending_resolution"},
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError) as excinfo:
            approve_ticket("GEN-500", "genhealth")

        msg = str(excinfo.value)
        assert "requeue" not in msg.lower()
        assert "not at an approval gate" in msg
        assert "disposition=None" in msg

    def test_approve_genuinely_not_at_gate_still_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Confirms the fix is scoped: a task with no gating disposition, and
        a task with disposition unset at the wrong stage, both still raise
        and leave the dev-queue row unchanged."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.exceptions import ApproveGateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW, session_id="sess-not-gated", disposition=None
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-not-gated",
            last_result={"status": "ambiguities_pending_resolution"},
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(ApproveGateError):
            approve_ticket("GEN-500", "genhealth")

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.stage == Stage.REVIEW

        # Second sub-case: disposition not 'approval_gate', wrong stage (PLAN).
        task2 = _make_blocked_task(
            ticket_id="GEN-502",
            stage=Stage.PLAN,
            session_id="sess-not-gated-2",
            disposition=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task2]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-not-gated-2",
                        last_result={"status": "blocked"},
                    )
                ]
            )
        )

        with pytest.raises(ApproveGateError):
            approve_ticket("GEN-502", "genhealth")

    def test_approve_scope_hint_gated_park_releases_with_null_last_result(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """disposition='approval_gate' releases the task even when
        session.last_result is None -- the disposition check is independent
        of (and does not require) a populated last_result. Confirms
        `_not_at_approval_gate` short-circuits on the disposition clause
        alone."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-1630-null-result",
            disposition="approval_gate",
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(session_id="sess-1630-null-result", last_result=None)
        save_state(CwState(sessions=[session]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.FINALIZE
        assert t.status == QueueItemStatus.PENDING

    def test_approve_signoff_two_step_unaffected_by_scope_hint_disposition(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The #1630 scope_hint-gated shape (disposition='approval_gate',
        scope_hint='large', last_result.status='stage_complete') combined
        with a ticket-level signoff requirement still needs two approves:
        the first parks for signoff (clearing the 'approval_gate'
        disposition to 'signoff_gate'), the second releases to FINALIZE."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-1630-signoff",
            disposition="approval_gate",
            scope_hint="large",
        )
        task.signoff = "operator"
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-1630-signoff",
            last_result={"status": "stage_complete"},
        )
        save_state(CwState(sessions=[session]))

        first = approve_ticket("GEN-500", "genhealth")

        assert first["awaiting_signoff"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert t.disposition == "signoff_gate"
        assert t.stage == Stage.REVIEW

        second = approve_ticket("GEN-500", "genhealth")

        assert second["awaiting_signoff"] is False
        assert second["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.FINALIZE

    def test_cli_approve_releases_scope_hint_gated_park(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """CLI-level: `cw dev-queue approve` releases a scope_hint-gated park
        (#1630 shape) with exit_code 0."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-1630-cli",
            disposition="approval_gate",
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess-1630-cli",
            last_result={"status": "stage_complete"},
        )
        save_state(CwState(sessions=[session]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "approve", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code == 0, result.output
        assert "review -> finalize" in result.output


# ---------------------------------------------------------------------------
# TestApproveTicketLockedForceHold — operator_initiated caller-provenance gate
# ---------------------------------------------------------------------------


class TestApproveTicketLockedForceHold:
    """RFC 0011 A3 (#1160): the A3 force hold is skipped for a human
    ``cw dev-queue approve`` (``operator_initiated=True``) and fires for every
    automatic caller, which omits the kwarg and gets the fail-safe default."""

    def _arm_force_held_review_row(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        session_id: str,
        scope_hint: str | None = None,
    ) -> TicketTask:
        """Save a BLOCKED_ON_USER REVIEW row with the force hold armed, plus its
        owning session carrying a review_pending_approval last_result."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW, session_id=session_id, scope_hint=scope_hint
        )
        task.hold_finalize = "manual"
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id=session_id,
                        last_result={"status": "review_pending_approval"},
                    )
                ]
            )
        )
        return task

    def test_approve_locked_force_held_review_ticket_default_kwarg_holds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Omitting operator_initiated treats the call as automatic: the row
        stays exactly as parked and finalize_held comes back True."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock

        self._arm_force_held_review_row(tmp_config_dir, tmp_path, "sess-fh-1")

        with dev_queue_lock():
            result = _approve_ticket_locked("GEN-500", "genhealth")

        assert result["finalize_held"] is True
        assert result["awaiting_signoff"] is False
        assert result["from_stage"] == "review"
        assert result["to_stage"] == "review"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.REVIEW
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_approve_locked_operator_initiated_true_bypasses_force_hold(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """operator_initiated=True is the human release: the force-hold check is
        skipped entirely and the ticket advances."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock

        self._arm_force_held_review_row(tmp_config_dir, tmp_path, "sess-fh-2")

        with dev_queue_lock():
            result = _approve_ticket_locked(
                "GEN-500", "genhealth", operator_initiated=True
            )

        assert result["finalize_held"] is False
        assert result["awaiting_signoff"] is False
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.FINALIZE
        assert t.status == QueueItemStatus.PENDING

    def test_approve_locked_auto_reactor_style_call_stays_held(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The gate-recipe reactor's exact call shape (resolved_task pinned, no
        operator_initiated) holds rather than approving."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock

        task = self._arm_force_held_review_row(tmp_config_dir, tmp_path, "sess-fh-3")

        with dev_queue_lock():
            result = _approve_ticket_locked("GEN-500", "genhealth", resolved_task=task)

        assert result["finalize_held"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.REVIEW
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_approve_locked_finalize_held_key_always_present_on_other_paths(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """finalize_held is part of the return contract on every path, not only
        the force-hold one: an unreviewed-plan requeue and an ordinary advance
        both report False."""
        from cw.config import save_state
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(spec=False, soundness=False),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-fh-4")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-fh-4",
                        last_result={"status": "plan_pending_approval"},
                    )
                ]
            )
        )

        with dev_queue_lock():
            requeued = _approve_ticket_locked("GEN-500", "genhealth")
        assert requeued["plan_requeued"] is True
        assert requeued["finalize_held"] is False

        # Now the reviewed-plan advance path.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        advanced_task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-fh-5")
        save_dev_queue(DevQueueStore(tasks=[advanced_task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-fh-5",
                        last_result={"status": "plan_pending_approval"},
                    )
                ]
            )
        )
        with dev_queue_lock():
            advanced = _approve_ticket_locked("GEN-500", "genhealth")
        assert advanced["to_stage"] == "impl"
        assert advanced["finalize_held"] is False

    def test_clear_signoff_gate_path_unaffected_by_force_hold(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """An already-signoff-parked row with the force hold armed still clears
        via _clear_signoff_gate on the human approve path, exactly as today."""
        from cw.dev_queue import approve_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id=None,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        task.hold_finalize = "manual"
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["awaiting_signoff"] is False
        assert result["finalize_held"] is False
        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.FINALIZE

    def test_both_gates_armed_manual_approve_hits_signoff_not_force_hold(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Human approve with both gates armed: the force-hold branch is skipped
        (operator_initiated=True) and the unchanged signoff elif parks the row."""
        from cw.dev_queue import approve_ticket

        task = self._arm_force_held_review_row(tmp_config_dir, tmp_path, "sess-fh-6")
        task.signoff = "operator"
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = approve_ticket("GEN-500", "genhealth")

        assert result["finalize_held"] is False
        assert result["awaiting_signoff"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert t.disposition == "signoff_gate"
        assert t.stage == Stage.REVIEW

    def test_approve_locked_operator_initiated_bypasses_hold_with_scope_hint(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """D4 (#1617): a scope_hint == 'large' task still advances via the human
        release path -- _approve_ticket_locked is a gate-release site, excluded
        from the new scope_hint gate (item 1), so it must not regress the
        force-hold bypass this class otherwise verifies."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock

        self._arm_force_held_review_row(
            tmp_config_dir, tmp_path, "sess-fh-scope-1", scope_hint="large"
        )

        with dev_queue_lock():
            result = _approve_ticket_locked(
                "GEN-500", "genhealth", operator_initiated=True
            )

        assert result["finalize_held"] is False
        assert result["awaiting_signoff"] is False
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.FINALIZE
        assert t.status == QueueItemStatus.PENDING

    def test_approve_locked_emits_scope_routing_decision_event(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """_approve_ticket_locked logs the #1617 scope-routing audit event on
        the gate-release path too (D4) -- rule='gate_release', and the
        disposition field reflects which branch actually fired, distinct from
        task.disposition (this branch performs no mutation of its own when the
        force hold is bypassed by operator_initiated=True)."""
        from cw.dev_queue import _approve_ticket_locked, dev_queue_lock
        from cw.models import OrchestratorEventType

        events = capture_events(
            "cw.dev_queue.approval", OrchestratorEventType.SCOPE_ROUTING_DECISION
        )
        self._arm_force_held_review_row(
            tmp_config_dir, tmp_path, "sess-fh-scope-2", scope_hint="large"
        )

        with dev_queue_lock():
            _approve_ticket_locked("GEN-500", "genhealth", operator_initiated=True)

        assert len(events) == 1
        _etype, payload, correlation_id = events[0]
        assert payload["ticket_id"] == "GEN-500"
        assert payload["client"] == "genhealth"
        assert payload["scope_hint"] == "large"
        assert payload["sentinel_tier"] is None
        assert payload["resolved_tier"] == "large"
        assert payload["rule"] == "gate_release"
        assert payload["disposition"] == "advanced"
        assert correlation_id == "GEN-500"


# ---------------------------------------------------------------------------
# TestRequeueTicket — requeue_ticket() mutation function
# ---------------------------------------------------------------------------


class TestRequeueTicket:
    """Tests for requeue_ticket()."""

    def test_requeue_at_current_stage(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """BLOCKED_ON_USER → PENDING at same stage (no --stage)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess9001")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "plan"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.PLAN
        assert t.session_id is None

    def test_requeue_with_forward_stage_override(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--stage that is forward in pipeline moves task forward."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test, so the #1681 impl-bypass guard
        # falls through to the tracker check -- stub it deterministically
        # rather than let it hit a real (unstubbed) `gh` call.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess9002")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_forward_to_review_sets_high_water_to_review(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A forward requeue (plan -> review) raises stage_high_water to the
        target stage (GitHub #1361)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-hw1")
        save_dev_queue(DevQueueStore(tasks=[task]))

        requeue_ticket("GEN-500", "genhealth", stage_override="review")

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage_high_water == Stage.REVIEW

    def test_requeue_forward_override_emits_stage_changed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forward stage_override (plan→impl) emits task.stage_changed advance."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test; stub the tracker fallback the
        # #1681 impl-bypass guard falls through to (see test above).
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-fwd1")
        save_dev_queue(DevQueueStore(tasks=[task]))

        requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert len(events) == 1
        _, payload, corr = events[0]
        assert corr == "GEN-500"
        assert payload["old_stage"] == Stage.PLAN
        assert payload["new_stage"] == Stage.IMPL
        assert payload["direction"] == "advance"

    def test_requeue_same_stage_override_emits_no_stage_changed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A same-stage requeue stays silent on task.stage_changed (Decision 1).

        stage_override equal to the current stage exercises _apply_requeue_stage's
        forward/same-stage tail; the shared helper's old==new guard must suppress
        the emit. The status move (BLOCKED_ON_USER→PENDING) still emits
        task.transition, so we assert that fired to prove the guard is
        stage-scoped, not a blanket no-op.
        """
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        # One capture for all event types — a second capture_events on the same
        # module would clobber the first's monkeypatch of record_event.
        events = capture_events("cw.dev_queue.lifecycle")
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-same1")
        save_dev_queue(DevQueueStore(tasks=[task]))

        requeue_ticket("GEN-500", "genhealth", stage_override="plan")

        types = [etype for etype, _, _ in events]
        assert OrchestratorEventType.TASK_STAGE_CHANGED not in types
        assert types.count(OrchestratorEventType.TASK_TRANSITION) == 1

    def test_requeue_backward_stage_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Backward --stage raises RequeueStageError."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess9003")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError, match="regress"):
            requeue_ticket("GEN-500", "genhealth", stage_override="plan")

    def test_requeue_stage_not_in_client_pipeline_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--stage review against a pipeline that excludes review raises.

        This branch (_validate_stage_in_pipeline, extracted from the former
        inline membership check) was previously untested — regression guard
        added alongside the #1682 extraction to lock in the refactor.
        """
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  genhealth:\n    workspace_path: {ws}\n"
            "    pipeline:\n      stages: [plan, impl]\n"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess9004")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError, match="not in the pipeline"):
            requeue_ticket("GEN-500", "genhealth", stage_override="review")

    def test_requeue_non_blocked_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """PENDING task raises RequeueStateError."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = TicketTask(
            ticket_id="GEN-500",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="expected BLOCKED_ON_USER"):
            requeue_ticket("GEN-500", "genhealth")

    def test_requeue_running_task_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """RUNNING task raises RequeueStateError."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = TicketTask(
            ticket_id="GEN-500",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="expected BLOCKED_ON_USER"):
            requeue_ticket("GEN-500", "genhealth")

    # -- Issue #917: allow_regress backward-regress behavior ----------------

    def test_regress_backward_allowed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """allow_regress=True + backward stage on BLOCKED task succeeds."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess9101")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket(
            "GEN-500", "genhealth", stage_override="impl", allow_regress=True
        )

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "impl"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING

    def test_regress_without_stage_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """allow_regress=True with no stage_override raises RequeueStageError."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess9102")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(
            RequeueStageError,
            match="--regress requires a backward --stage target on a blocked task",
        ):
            requeue_ticket("GEN-500", "genhealth", allow_regress=True)

    def test_regress_refused_on_running(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """allow_regress + backward stage on a RUNNING task raises."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess9103",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket(
                "GEN-500", "genhealth", stage_override="impl", allow_regress=True
            )

    def test_regress_forward_target_is_inert(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """allow_regress + forward/same stage is not an error (inert flag)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test; stub the tracker fallback the
        # #1681 impl-bypass guard falls through to.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess9104")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket(
            "GEN-500", "genhealth", stage_override="impl", allow_regress=True
        )

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        assert result["regressed"] is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL

    def test_regress_return_dict_reports_regressed_and_attempts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backward regress reports regressed + attempts; forward reports neither."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test; stub the tracker fallback the
        # #1681 impl-bypass guard falls through to for the forward GEN-501
        # call below (backward regress calls are unaffected by the guard).
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess9105")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket(
            "GEN-500", "genhealth", stage_override="impl", allow_regress=True
        )
        assert result["regressed"] is True
        assert result["regress_attempts"] == 1

        # Forward requeue (no allow_regress): regressed False, attempts 0.
        task2 = _make_blocked_task(
            ticket_id="GEN-501", stage=Stage.PLAN, session_id="sess9106"
        )
        save_dev_queue(DevQueueStore(tasks=[task2]))
        result2 = requeue_ticket("GEN-501", "genhealth", stage_override="impl")
        assert result2["regressed"] is False
        assert result2["regress_attempts"] == 0

        # Same-stage with allow_regress=True (inert): regressed False, attempts 0.
        task3 = _make_blocked_task(
            ticket_id="GEN-502", stage=Stage.IMPL, session_id="sess9107"
        )
        save_dev_queue(DevQueueStore(tasks=[task3]))
        result3 = requeue_ticket(
            "GEN-502", "genhealth", stage_override="impl", allow_regress=True
        )
        assert result3["regressed"] is False
        assert result3["regress_attempts"] == 0

    # -- AWAITING_OPERATOR_SIGNOFF requeue lever (RFC 0007 Phase 3, #990) ----

    def test_requeue_forward_from_awaiting_signoff_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF ticket can requeue forward without --regress."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-req-1",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="finalize")

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "finalize"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.FINALIZE
        assert t.session_id is None

    def test_requeue_regress_from_awaiting_signoff_to_impl_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--regress moves an AWAITING_OPERATOR_SIGNOFF ticket backward.

        The reject-a-ship lever: an operator can send a signoff-parked ticket
        back to an earlier stage instead of clearing the gate forward.
        """
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-req-2",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket(
            "GEN-500", "genhealth", stage_override="impl", allow_regress=True
        )

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "impl"
        assert result["regressed"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.IMPL
        assert t.regress_attempts == 1

    def test_requeue_from_awaiting_signoff_without_regress_flag_stage_target_forward_ok(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No --stage: re-runs the current stage from AWAITING_OPERATOR_SIGNOFF."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-req-3",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "review"
        assert result["to_stage"] == "review"
        assert result["regressed"] is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    # -- Issue #1018: --from-cancelled requeue escape hatch -----------------

    def test_requeue_from_cancelled_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """CANCELLED row + from_cancelled=True -> PENDING at current stage."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-cancel-1",
            status=QueueItemStatus.CANCELLED,
        )
        task.stage_base_ref = "deadbeef"
        task.regress_attempts = 2
        # #1794: a latent per-arrival regress marker must not survive a
        # forward/same-stage requeue that resolves in the same lock.
        task.regressed_into_stage = Stage.IMPL
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", from_cancelled=True)

        assert result["from_stage"] == "impl"
        assert result["to_stage"] == "impl"
        assert result["from_cancelled_applied"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None
        assert t.regress_attempts == 0
        assert t.regressed_into_stage is None

    def test_requeue_from_cancelled_flag_on_approvable_row_not_applied(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_cancelled=True passed defensively on an already-approvable
        (BLOCKED_ON_USER) row is a harmless no-op for the state gate, but
        from_cancelled_applied must be False — the CANCELLED branch never
        fired, so callers must not attribute the requeue to it."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-cancel-5",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", from_cancelled=True)

        assert result["from_cancelled_applied"] is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_from_cancelled_without_flag_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """CANCELLED row without from_cancelled=True raises RequeueStateError
        naming the --from-cancelled escape hatch."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-cancel-2",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="--from-cancelled"):
            requeue_ticket("GEN-500", "genhealth")

    def test_requeue_from_cancelled_flag_does_not_broaden_running(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_cancelled=True does not admit a RUNNING row (flag is narrow)."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-cancel-3",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="expected BLOCKED_ON_USER"):
            requeue_ticket("GEN-500", "genhealth", from_cancelled=True)

    def test_requeue_from_cancelled_regress_backward_rejected(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_cancelled=True + backward --stage on a CANCELLED row still
        fails: the regress gate is untouched and does not accept CANCELLED."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-cancel-4",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket(
                "GEN-500",
                "genhealth",
                stage_override="plan",
                allow_regress=True,
                from_cancelled=True,
            )

    def test_requeue_from_failed_without_flag_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """FAILED row without from_failed=True raises RequeueStateError
        naming the --from-failed escape hatch."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-failed-2",
            status=QueueItemStatus.FAILED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="--from-failed"):
            requeue_ticket("GEN-500", "genhealth")

    def test_requeue_from_failed_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """FAILED row + from_failed=True -> PENDING at current stage."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-failed-1",
            status=QueueItemStatus.FAILED,
        )
        task.regress_attempts = 2
        task.disposition = "abandoned"
        task.pr_url = "https://github.com/example/repo/pull/1"
        task.completed_at = datetime.now(UTC)
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", from_failed=True)

        assert result["from_stage"] == "impl"
        assert result["to_stage"] == "impl"
        assert result["from_failed_applied"] is True
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None
        assert t.regress_attempts == 0
        assert t.disposition is None
        assert t.pr_url is None
        assert t.completed_at is None

    def test_requeue_from_failed_flag_does_not_broaden_running(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_failed=True does not admit a RUNNING row (flag is narrow)."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-failed-3",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStateError, match="expected BLOCKED_ON_USER"):
            requeue_ticket("GEN-500", "genhealth", from_failed=True)

    def test_requeue_from_failed_flag_on_approvable_row_not_applied(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_failed=True passed defensively on an already-approvable
        (BLOCKED_ON_USER) row is a harmless no-op for the state gate, but
        from_failed_applied must be False — the FAILED branch never fired,
        so callers must not attribute the requeue to it."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess-failed-5",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", from_failed=True)

        assert result["from_failed_applied"] is False
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_from_failed_regress_backward_rejected(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """from_failed=True + backward --stage on a FAILED row still fails:
        the regress gate is untouched and does not accept FAILED."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess-failed-4",
            status=QueueItemStatus.FAILED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket(
                "GEN-500",
                "genhealth",
                stage_override="plan",
                allow_regress=True,
                from_failed=True,
            )

    # -- #1681: impl-bypass plan-availability guard -------------------------

    def test_requeue_stage_impl_bypass_worktree_missing_and_no_tracker_plan_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No worktree on disk, no reviewed tracker comment -> refuse."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        stub_fetch_plan(
            monkeypatch, None, target="cw.dev_queue.requeue.fetch_approved_plan_comment"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-1")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError) as exc_info:
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        msg = str(exc_info.value)
        assert "GEN-500" in msg
        assert ".cw/plan.md" in msg

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.PLAN
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_requeue_stage_impl_bypass_worktree_wrong_branch_and_no_tracker_plan_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worktree dir exists but is checked out on a foreign branch -> refuse.

        Mirrors create_worktree's own stale-worktree refusal (#402/#404):
        a directory that merely exists must not be trusted as "the plan is
        there" without also proving it's the expected branch.
        """
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        wt_path = tmp_path / "stale-worktree"
        wt_path.mkdir()
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: wt_path,
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue._checked_out_branch",
            lambda _wt_path: "some-other-branch",
        )
        stub_fetch_plan(
            monkeypatch, None, target="cw.dev_queue.requeue.fetch_approved_plan_comment"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-2")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

    def test_requeue_stage_impl_bypass_worktree_missing_plan_md_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worktree exists on the right branch but has no .cw/plan.md -> refuse."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        wt_path = tmp_path / "reused-worktree"
        wt_path.mkdir()
        branch = "dev/GEN-500"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: wt_path,
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue._checked_out_branch",
            lambda _wt_path: branch,
        )
        stub_fetch_plan(
            monkeypatch, None, target="cw.dev_queue.requeue.fetch_approved_plan_comment"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-3")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

    def test_requeue_stage_impl_bypass_worktree_has_valid_plan_md_succeeds(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reused worktree with a validly-marked .cw/plan.md -> succeeds, and
        the tracker network call is never made (common reused-worktree path
        must stay zero-network-cost)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        wt_path = tmp_path / "reused-worktree"
        cw_dir = wt_path / ".cw"
        cw_dir.mkdir(parents=True)
        (cw_dir / "plan.md").write_text(plan_body(), encoding="utf-8")
        branch = "dev/GEN-500"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: wt_path,
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue._checked_out_branch",
            lambda _wt_path: branch,
        )

        def _fail_if_called(_ticket_id: str, **_kwargs: object) -> str | None:
            msg = "fetch_approved_plan_comment must not be called on the local-hit path"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.dev_queue.requeue.fetch_approved_plan_comment", _fail_if_called
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-4")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert result["to_stage"] == "impl"

    def test_requeue_stage_impl_bypass_tracker_fallback_recovers_succeeds(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No local worktree, but the tracker carries a reviewed plan comment
        -> the tracker-fallback recovers it and the requeue succeeds."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-5")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert result["to_stage"] == "impl"

    def test_requeue_stage_impl_bypass_tracker_unmarked_comment_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tracker comment with no signoff markers doesn't count as reviewed
        -- proves the guard uses _plan_body_signoff_ok, not a bare "comment
        exists" check."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        stub_fetch_plan(
            monkeypatch,
            plan_body(spec=False),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-6")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError):
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

    # -- #1906: honest tracker gate for the impl-bypass plan-availability
    # guard -- fetch_approved_plan_comment is GitHub-only; skip the call
    # (and the misleading "was found on the tracker" claim) when the
    # resolved tracker is positively known to be non-GitHub. -------------

    def _assert_fetch_approved_plan_comment_not_called(
        self, _ticket_id: str, **_kwargs: object
    ) -> str | None:
        msg = (
            "fetch_approved_plan_comment must not be called for a"
            " known non-GitHub tracker"
        )
        raise AssertionError(msg)

    def test_requeue_stage_impl_bypass_honest_for_non_github_tracker(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Linear-tracked client's impl-bypass guard never calls the
        GitHub-only fetch_approved_plan_comment -- it isn't the right
        tracker to ask -- and the resulting refusal message must not claim
        the tracker was checked for a reviewed plan comment, since it
        wasn't."""
        from cw.dev_queue import requeue_ticket
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        _write_project_config_yaml(
            tmp_path / "ws", "tracking:\n  primary:\n    system: linear\n"
        )
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue.fetch_approved_plan_comment",
            self._assert_fetch_approved_plan_comment_not_called,
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-7a")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError) as exc_info:
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        msg = str(exc_info.value)
        assert "was found on the tracker" not in msg
        assert "linear" in msg
        assert "GEN-500" in msg

    def test_requeue_stage_impl_bypass_github_tracker_message_unchanged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: a github-issues (or unconfigured) tracker's refusal
        message is byte-identical to the pre-#1906 text -- the GitHub/
        default path is untouched."""
        from cw.dev_queue import requeue_ticket
        from cw.dev_queue.lifecycle import _PLAN_SOUNDNESS_MARKER, _PLAN_SPEC_MARKER
        from cw.exceptions import RequeueStageError

        _write_client_yaml(tmp_config_dir, tmp_path)
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        stub_fetch_plan(
            monkeypatch, None, target="cw.dev_queue.requeue.fetch_approved_plan_comment"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-bypass-7c")
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(RequeueStageError) as exc_info:
            requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        wt_path = missing_wt
        expected = (
            f"Cannot requeue ticket 'GEN-500' to stage 'impl':"
            f" no approved plan is available. '{wt_path / '.cw' / 'plan.md'}'"
            " is missing or stale, and no reviewed plan comment"
            f" ('{_PLAN_SPEC_MARKER}' + '{_PLAN_SOUNDNESS_MARKER}')"
            " was found on the tracker. Let Stage 1 (plan) run and post its"
            " approved plan first, or requeue at --stage plan instead."
        )
        assert str(exc_info.value) == expected

    def test_requeue_same_stage_at_impl_unaffected(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-stage requeue at impl performs no worktree/tracker check at all
        -- the guard is scoped to genuine forward bypass of plan, not every
        touch of the impl stage."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)

        def _fail_if_called(*_args: object, **_kwargs: object) -> Path:
            msg = "worktree_path_for must not be called for a same-stage requeue"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.dev_queue.requeue.worktree_path_for", _fail_if_called)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess-bypass-7")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert result["to_stage"] == "impl"

    def test_requeue_forward_to_review_or_finalize_unaffected(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forward requeue targeting review or finalize (not impl) performs no
        worktree/tracker check -- the guard is scoped to target_stage ==
        Stage.IMPL only (review/finalize degrade gracefully on a missing
        plan; see #1681 Decisions)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)

        def _fail_if_called(*_args: object, **_kwargs: object) -> Path:
            msg = "worktree_path_for must not be called for a review/finalize target"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.dev_queue.requeue.worktree_path_for", _fail_if_called)

        task_review = _make_blocked_task(
            ticket_id="GEN-503", stage=Stage.PLAN, session_id="sess-bypass-8a"
        )
        save_dev_queue(DevQueueStore(tasks=[task_review]))
        result_review = requeue_ticket("GEN-503", "genhealth", stage_override="review")
        assert result_review["to_stage"] == "review"

        task_finalize = _make_blocked_task(
            ticket_id="GEN-504", stage=Stage.PLAN, session_id="sess-bypass-8b"
        )
        save_dev_queue(DevQueueStore(tasks=[task_finalize]))
        result_finalize = requeue_ticket(
            "GEN-504", "genhealth", stage_override="finalize"
        )
        assert result_finalize["to_stage"] == "finalize"


# ---------------------------------------------------------------------------
# TestRequeueReviewDeliveryDegrade — #1730 degrade-loudly, never raise
# ---------------------------------------------------------------------------

_SYNTHETIC_BACKEND = "opencode"


def _stub_review_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, tracker: str | None
) -> None:
    """Pin the REVIEW-stage backend/tracker _review_reentry_deliverable resolves.

    Patched at the ORIGIN modules (``cw.executor`` / ``cw.tracker``), not at
    ``cw.dev_queue.requeue``: the helper imports both function-locally on every
    call, so ``cw.dev_queue.requeue`` has no module attribute of either name to
    patch and a patch there would silently no-op (#1730 plan, Phase 1 item 2).
    """
    from cw.models import StageExecutorConfig

    monkeypatch.setattr(
        "cw.executor.resolve_executor_config",
        lambda *_a, **_kw: StageExecutorConfig(backend=backend),
    )
    monkeypatch.setattr("cw.tracker.resolve_tracker", lambda *_a, **_kw: tracker)


class TestRequeueReviewDeliveryDegrade:
    """A REVIEW-stage requeue that cannot deliver operator comments degrades
    loudly (event) and proceeds — it never raises (#1730, comment 6 A2)."""

    def test_requeue_into_review_with_undeliverable_backend_degrades_not_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, _SYNTHETIC_BACKEND, "github-issues")
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-degrade-1")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["to_stage"] == "review"
        assert len(events) == 1
        _etype, payload, corr = events[0]
        assert corr == "GEN-500"
        assert _SYNTHETIC_BACKEND in str(payload["reason"])
        assert payload["backend"] == _SYNTHETIC_BACKEND
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_into_review_with_codex_backend_and_non_github_tracker_degrades(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """codex + a non-github tracker cannot deliver comments — degrade, and
        thread the resolved backend/tracker verbatim into the payload."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, "codex", "linear")
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-degrade-2")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["to_stage"] == "review"
        assert len(events) == 1
        _etype, payload, _corr = events[0]
        assert payload["backend"] == "codex"
        assert payload["tracker"] == "linear"
        assert "github-issues" in str(payload["reason"])

    def test_requeue_into_review_with_codex_backend_and_github_tracker_no_degrade(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Regression guard: codex + github-issues CAN deliver — no event."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, "codex", "github-issues")
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-degrade-3")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["to_stage"] == "review"
        assert events == []

    def test_requeue_into_review_with_claude_native_backend_never_degrades(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """claude-native inlines comments into every reviewer prompt regardless
        of tracker — it never consults the tracker to decide deliverability."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, "claude-native", None)
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess-degrade-4")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth")

        assert result["to_stage"] == "review"
        assert events == []

    def test_requeue_forward_bypass_into_review_also_checked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """The check keys off the RESOLVED to_stage, so a forward bypass
        plan -> review is covered too, not only a same-stage requeue."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, "codex", "linear")
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-degrade-5")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="review")

        assert result["to_stage"] == "review"
        assert len(events) == 1

    def test_requeue_regress_into_review_from_finalize_also_checked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A backward --regress finalize -> review is covered too."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, "codex", "linear")
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task = _make_blocked_task(stage=Stage.FINALIZE, session_id="sess-degrade-6")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket(
            "GEN-500", "genhealth", stage_override="review", allow_regress=True
        )

        assert result["to_stage"] == "review"
        assert result["regressed"] is True
        assert len(events) == 1

    def test_requeue_to_impl_or_finalize_unaffected(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Non-REVIEW targets never run the delivery check, whatever the
        backend resolves to — no event, no exception."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        _stub_review_backend(monkeypatch, _SYNTHETIC_BACKEND, "linear")
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        events = capture_events(
            "cw.dev_queue.requeue",
            OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
        )
        task_impl = _make_blocked_task(
            ticket_id="GEN-510", stage=Stage.PLAN, session_id="sess-degrade-7a"
        )
        save_dev_queue(DevQueueStore(tasks=[task_impl]))
        assert (
            requeue_ticket("GEN-510", "genhealth", stage_override="impl")["to_stage"]
            == "impl"
        )

        task_fin = _make_blocked_task(
            ticket_id="GEN-511", stage=Stage.PLAN, session_id="sess-degrade-7b"
        )
        save_dev_queue(DevQueueStore(tasks=[task_fin]))
        assert (
            requeue_ticket("GEN-511", "genhealth", stage_override="finalize")[
                "to_stage"
            ]
            == "finalize"
        )

        assert events == []


# ---------------------------------------------------------------------------
# TestSelectHeldTickets / TestDrainHeldTickets — RFC 0011 A4 (#1161)
# ---------------------------------------------------------------------------


def _requeue_that_fails_for(
    failing_ticket_id: str,
) -> Callable[..., dict[str, str | bool | int]]:
    """Build a requeue_ticket fake that raises RequeueStateError for one ticket.

    Delegates to the real requeue_ticket for every other ticket id. Shared by
    the drain partial-failure tests (TestDrainHeldTickets, TestCLIDevQueueDrain)
    so the three call sites can't drift out of sync (#1161 review).
    """
    from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket
    from cw.exceptions import RequeueStateError

    def _fake_requeue_ticket(
        ticket_id: str,
        client_name: str,
        stage_override: str | None = None,
        *,
        allow_regress: bool = False,
        from_cancelled: bool = False,
        from_failed: bool = False,
    ) -> dict[str, str | bool | int]:
        if ticket_id == failing_ticket_id:
            msg = "status raced away from BLOCKED_ON_USER"
            raise RequeueStateError(msg)
        return real_requeue_ticket(
            ticket_id,
            client_name,
            stage_override,
            allow_regress=allow_regress,
            from_cancelled=from_cancelled,
            from_failed=from_failed,
        )

    return _fake_requeue_ticket


class TestSelectHeldTickets:
    """Tests for select_held_tickets()."""

    def test_selects_only_held_disposition_rows(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Selection matches disposition=awaiting_operator alone -- a genuine
        Rule-5 park (disposition=blocked) is excluded, but a stale terminal
        row (COMPLETED with a leftover awaiting_operator disposition) is
        still selected (Adopted Assumptions: disposition-only filter; the
        status gate is enforced downstream by requeue_ticket, not here)."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, select_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        held = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-held-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        genuine_blocked = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-held-2",
            disposition="blocked",
        )
        stale_terminal = _make_ticket_task(
            ticket_id="GEN-502",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            stage=Stage.PLAN,
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[held, genuine_blocked, stale_terminal]))

        selected = select_held_tickets("genhealth")

        assert {t.ticket_id for t in selected} == {"GEN-500", "GEN-502"}

    def test_lane_filter_restricts_selection(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """lane= restricts selection to the matching lane's held row only."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, select_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        lane_a = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-lane-a",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        lane_a.lane = "a"
        lane_b = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-lane-b",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        lane_b.lane = "b"
        save_dev_queue(DevQueueStore(tasks=[lane_a, lane_b]))

        selected = select_held_tickets("genhealth", lane="a")

        assert [t.ticket_id for t in selected] == ["GEN-500"]

    def test_empty_queue_returns_empty_list(self, tmp_config_dir: Path) -> None:
        from cw.dev_queue import select_held_tickets

        save_dev_queue(DevQueueStore(tasks=[]))

        assert select_held_tickets("genhealth") == []


class TestDrainHeldTickets:
    """Tests for drain_held_tickets()."""

    def test_drains_all_held_rows_for_client(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Two held rows both requeue to PENDING at their own current stage."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        first = _make_blocked_task(
            ticket_id="GEN-500",
            stage=Stage.PLAN,
            session_id="sess-drain-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        second = _make_blocked_task(
            ticket_id="GEN-501",
            stage=Stage.REVIEW,
            session_id="sess-drain-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[first, second]))

        outcomes = drain_held_tickets("genhealth")

        assert {o["ticket_id"] for o in outcomes} == {"GEN-500", "GEN-501"}
        assert all(o["status"] == "requeued" for o in outcomes)
        store = load_dev_queue()
        by_id = {t.ticket_id: t for t in store.tasks}
        assert by_id["GEN-500"].status == QueueItemStatus.PENDING
        assert by_id["GEN-500"].stage == Stage.PLAN
        assert by_id["GEN-500"].session_id is None
        assert by_id["GEN-501"].status == QueueItemStatus.PENDING
        assert by_id["GEN-501"].stage == Stage.REVIEW
        assert by_id["GEN-501"].session_id is None

    def test_mixed_held_and_blocked_queue_leaves_blocked_untouched(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A genuine disposition=blocked park is left BLOCKED_ON_USER
        untouched while the held row drains."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        held = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-mix-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        genuine_blocked = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-mix-2",
            disposition="blocked",
        )
        save_dev_queue(DevQueueStore(tasks=[held, genuine_blocked]))

        outcomes = drain_held_tickets("genhealth")

        assert [o["ticket_id"] for o in outcomes] == ["GEN-500"]
        store = load_dev_queue()
        by_id = {t.ticket_id: t for t in store.tasks}
        assert by_id["GEN-500"].status == QueueItemStatus.PENDING
        assert by_id["GEN-501"].status == QueueItemStatus.BLOCKED_ON_USER
        assert by_id["GEN-501"].disposition == "blocked"

    def test_empty_held_set_is_a_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No held rows -> empty outcome list, queue snapshot unchanged."""
        from cw.dev_queue import drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        genuine_blocked = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-noop-1",
            disposition="blocked",
        )
        save_dev_queue(DevQueueStore(tasks=[genuine_blocked]))
        before = load_dev_queue()

        outcomes = drain_held_tickets("genhealth")

        assert outcomes == []
        after = load_dev_queue()
        assert after == before

    def test_partial_failure_continues_and_reports_both(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A RequeueStateError on one selected ticket does not abort the
        batch; the first ticket's mutation still persists and both outcomes
        are reported."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        first = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-fail-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        second = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-fail-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[first, second]))

        monkeypatch.setattr(
            "cw.dev_queue.drain.requeue_ticket", _requeue_that_fails_for("GEN-501")
        )

        outcomes = drain_held_tickets("genhealth")

        by_id = {o["ticket_id"]: o for o in outcomes}
        assert by_id["GEN-500"]["status"] == "requeued"
        assert by_id["GEN-501"]["status"] == "failed"
        assert "status raced away" in by_id["GEN-501"]["detail"]
        store = load_dev_queue()
        by_ticket = {t.ticket_id: t for t in store.tasks}
        assert by_ticket["GEN-500"].status == QueueItemStatus.PENDING
        assert by_ticket["GEN-501"].status == QueueItemStatus.BLOCKED_ON_USER

    def test_dry_run_reports_without_mutating(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """dry_run=True reports would_requeue and performs no mutation."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        held = _make_blocked_task(
            ticket_id="GEN-500",
            stage=Stage.IMPL,
            session_id="sess-dry-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[held]))

        outcomes = drain_held_tickets("genhealth", dry_run=True)

        assert len(outcomes) == 1
        assert outcomes[0]["status"] == "would_requeue"
        assert outcomes[0]["detail"] == "impl"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_drain_excludes_a3_force_hold(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A row carrying #1160's A3 force-hold disposition is left untouched by
        both selection and drain (RFC 0011 A4 R11)."""
        from cw.dev_queue import (
            FINALIZE_GATE_HELD_DISPOSITION,
            drain_held_tickets,
            select_held_tickets,
        )

        assert FINALIZE_GATE_HELD_DISPOSITION == "finalize_gate_held"
        _write_client_yaml(tmp_config_dir, tmp_path)
        force_held = _make_blocked_task(
            ticket_id="GEN-500",
            stage=Stage.REVIEW,
            session_id="sess-force-1",
            disposition=FINALIZE_GATE_HELD_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[force_held]))

        assert select_held_tickets("genhealth") == []
        assert drain_held_tickets("genhealth") == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == "finalize_gate_held"

    def test_drain_includes_review_health_gate(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#1702: a review-health-gate park IS batch-releasable.

        Positive mirror of ``test_drain_excludes_a3_force_hold``: unlike a
        force hold (a deliberate operator stop), a degraded-review-health park
        clears by re-running review — exactly what drain does.
        """
        from cw.dev_queue import (
            REVIEW_HEALTH_GATE_DISPOSITION,
            drain_held_tickets,
            select_held_tickets,
        )

        assert REVIEW_HEALTH_GATE_DISPOSITION == "review_health_gate"
        _write_client_yaml(tmp_config_dir, tmp_path)
        health_gated = _make_blocked_task(
            ticket_id="GEN-502",
            stage=Stage.REVIEW,
            session_id="sess-health-1",
            disposition=REVIEW_HEALTH_GATE_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[health_gated]))

        assert [t.ticket_id for t in select_held_tickets("genhealth")] == ["GEN-502"]
        outcomes = drain_held_tickets("genhealth")

        assert [o["status"] for o in outcomes] == ["requeued"]
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-502")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_drain_includes_must_fix_mechanically_rejected(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#1714: a mechanically-rejected-MUST_FIX park IS batch-releasable.

        Same reasoning as ``test_drain_includes_review_health_gate`` above: the
        park says "review dropped a MUST_FIX before adjudicating it", which
        clears by re-running review — exactly what drain does. It is not a
        deliberate operator stop.
        """
        from cw.dev_queue import (
            REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
            drain_held_tickets,
            select_held_tickets,
        )

        assert (
            REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION
            == "codex_must_fix_mechanically_rejected"
        )
        _write_client_yaml(tmp_config_dir, tmp_path)
        parked = _make_blocked_task(
            ticket_id="GEN-503",
            stage=Stage.REVIEW,
            session_id="sess-mech-1",
            disposition=REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[parked]))

        assert [t.ticket_id for t in select_held_tickets("genhealth")] == ["GEN-503"]
        outcomes = drain_held_tickets("genhealth")

        assert [o["status"] for o in outcomes] == ["requeued"]
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-503")
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_no_outer_lock_held_during_batch(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Regression guard for R4: two held rows, real (unmocked)
        requeue_ticket, must both complete without hanging -- proves
        drain_held_tickets holds no outer lock across the per-ticket calls
        (each of which takes dev_queue_lock() internally)."""
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, drain_held_tickets

        _write_client_yaml(tmp_config_dir, tmp_path)
        first = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-lock-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        second = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-lock-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[first, second]))

        outcomes = drain_held_tickets("genhealth")

        assert {o["ticket_id"] for o in outcomes} == {"GEN-500", "GEN-501"}
        assert all(o["status"] == "requeued" for o in outcomes)


# ---------------------------------------------------------------------------
# TestUnblockTicket — unblock_ticket() mutation function
# ---------------------------------------------------------------------------


class TestUnblockTicket:
    """Tests for unblock_ticket()."""

    def test_unblock_park_marked_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """SALVAGE_PARKED session → last_result/reap_reason cleared, task PENDING."""
        from cw.config import load_state, save_state
        from cw.dev_queue import unblock_ticket
        from cw.models import CwState, ReapReason

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess8001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess8001",
            last_result={"status": "salvage_parked"},
            reap_reason=ReapReason.SALVAGE_PARKED,
        )
        save_state(CwState(sessions=[session]))

        unblock_ticket("GEN-500", "genhealth")

        # Session mutations
        state = load_state()
        s = state.find_by_name_or_id("sess8001")
        assert s is not None
        assert s.last_result is None
        assert s.reap_reason is None

        # Queue mutations
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None

    def test_unblock_clears_escalation_fields(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """unblock_ticket clears escalation_parked_at/fired_at (#1015, Q5)."""
        from cw.config import save_state
        from cw.dev_queue import unblock_ticket
        from cw.models import CwState, ReapReason

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess8003")
        task.escalation_parked_at = datetime.now(UTC)
        task.escalation_fired_at = datetime.now(UTC)
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess8003",
            last_result={"status": "salvage_parked"},
            reap_reason=ReapReason.SALVAGE_PARKED,
        )
        save_state(CwState(sessions=[session]))

        unblock_ticket("GEN-500", "genhealth")

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.escalation_parked_at is None
        assert t.escalation_fired_at is None

    def test_unblock_not_park_marked_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Session without SALVAGE_PARKED reap_reason raises UnblockStateError."""
        from cw.config import save_state
        from cw.dev_queue import unblock_ticket
        from cw.exceptions import UnblockStateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess8002")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess8002",
            last_result=None,
            reap_reason=None,
        )
        save_state(CwState(sessions=[session]))

        with pytest.raises(UnblockStateError, match="not park-marked"):
            unblock_ticket("GEN-500", "genhealth")

    def test_unblock_missing_session_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """task.session_id not in sessions raises UnblockStateError."""
        from cw.config import save_state
        from cw.dev_queue import unblock_ticket
        from cw.exceptions import UnblockStateError
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="no-such-session")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        with pytest.raises(UnblockStateError, match="session not found"):
            unblock_ticket("GEN-500", "genhealth")

    def test_unblock_non_blocked_task_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """PENDING task raises UnblockStateError before checking sessions."""
        from cw.dev_queue import unblock_ticket
        from cw.exceptions import UnblockStateError

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = TicketTask(
            ticket_id="GEN-500",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with pytest.raises(UnblockStateError, match="expected BLOCKED_ON_USER"):
            unblock_ticket("GEN-500", "genhealth")


# ---------------------------------------------------------------------------
# TestCLIApprove — cw dev-queue approve
# ---------------------------------------------------------------------------


class TestCLIApprove:
    """CLI tests for `cw dev-queue approve`."""

    def test_approve_happy_path(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI approve advances stage and prints confirmation."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess7001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess7001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "approve", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code == 0, result.output
        assert "plan -> impl" in result.output

    def test_approve_not_at_gate_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Ticket not at approval gate exits 1."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess7002")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess7002",
            last_result={"status": "ambiguities_pending_resolution"},
        )
        save_state(CwState(sessions=[session]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "approve", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0

    def test_approve_cli_event_includes_plan_requeued_key(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TICKET_APPROVED's event payload carries plan_requeued for both the
        unreviewed-requeue and reviewed-advance cases (#968)."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        events = capture_events(
            "cw.cli.dev_queue.crud", OrchestratorEventType.TICKET_APPROVED
        )
        runner = CliRunner()

        # Case 1: unreviewed plan -> plan_requeued=True, echoed re-queue message.
        stub_fetch_plan(
            monkeypatch,
            None,
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-evt1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-evt1",
                        last_result={"status": "plan_pending_approval"},
                    )
                ]
            )
        )

        result = runner.invoke(
            main, ["dev-queue", "approve", "GEN-500", "--client", "genhealth"]
        )

        assert result.exit_code == 0, result.output
        assert len(events) == 1
        assert events[0][1]["plan_requeued"] is True
        assert (
            "Approved GEN-500 (genhealth): plan not yet quality-reviewed"
            " — re-queued at plan stage to run Plan Quality Review."
            " Re-run auto-dev-plan (or dispatch) to proceed."
        ) in result.output

        # Case 2: reviewed plan -> plan_requeued=False, ordinary advance message.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.lifecycle.fetch_approved_plan_comment",
        )
        task2 = _make_blocked_task(
            ticket_id="GEN-501", stage=Stage.PLAN, session_id="sess-evt2"
        )
        save_dev_queue(DevQueueStore(tasks=[task2]))
        save_state(
            CwState(
                sessions=[
                    _make_session(
                        session_id="sess-evt2",
                        last_result={"status": "plan_pending_approval"},
                    )
                ]
            )
        )

        result2 = runner.invoke(
            main, ["dev-queue", "approve", "GEN-501", "--client", "genhealth"]
        )

        assert result2.exit_code == 0, result2.output
        assert len(events) == 2
        assert events[1][1]["plan_requeued"] is False
        assert "plan -> impl" in result2.output


# ---------------------------------------------------------------------------
# TestCLIRequeue — cw dev-queue requeue
# ---------------------------------------------------------------------------


class TestCLIRequeue:
    """CLI tests for `cw dev-queue requeue`."""

    def test_requeue_same_stage(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Requeue without --stage re-runs current stage."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess6001")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "requeue", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code == 0, result.output
        assert "impl -> impl" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_with_forward_stage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--stage impl from plan advances forward."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test; stub the tracker fallback the
        # #1681 impl-bypass guard falls through to.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess6002")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "plan -> impl" in result.output

    def test_requeue_backward_stage_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Backward --stage exits 1."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess6003")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--stage",
                "plan",
            ],
        )
        assert result.exit_code != 0

    def test_requeue_impl_bypass_without_plan_exits_nonzero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--stage impl with neither a local nor tracker plan exits nonzero
        and surfaces the refusal message via @handle_errors (#1681)."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        missing_wt = tmp_path / "no-such-worktree"
        monkeypatch.setattr(
            "cw.dev_queue.requeue.worktree_path_for",
            lambda _client, _branch: missing_wt,
        )
        stub_fetch_plan(
            monkeypatch, None, target="cw.dev_queue.requeue.fetch_approved_plan_comment"
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess-cli-bypass")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code != 0
        assert ".cw/plan.md" in result.output

    def test_requeue_non_blocked_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """PENDING ticket exits 1."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = TicketTask(
            ticket_id="GEN-500",
            client="genhealth",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "requeue", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0

    # -- Issue #917: --regress flag ----------------------------------------

    def test_requeue_regress_backward_allowed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`--regress --stage impl` from review succeeds and moves backward."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess6101")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--regress",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "review -> impl" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_regress_refused_on_running(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`--regress` on a RUNNING task exits nonzero."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.REVIEW,
            session_id="sess6102",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--regress",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code != 0

    def test_requeue_regress_without_stage_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`--regress` without `--stage` exits nonzero."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess6103")
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--regress",
            ],
        )
        assert result.exit_code != 0

    def test_requeue_regress_emits_regress_provenance_event(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--regress` emits TICKET_REQUEUED with cli_regress provenance."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.REVIEW, session_id="sess6104")
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--regress",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_regress"
        assert payload["regressed"] is True
        assert payload["regress_attempts"] == 1

    def test_requeue_ordinary_event_has_no_regress_fields(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain forward requeue omits the regress_attempts key entirely."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        # No worktree exists in this test; stub the tracker fallback the
        # #1681 impl-bypass guard falls through to.
        stub_fetch_plan(
            monkeypatch,
            plan_body(),
            target="cw.dev_queue.requeue.fetch_approved_plan_comment",
        )
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess6105")
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--stage",
                "impl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_requeue"
        assert payload["regressed"] is False
        assert "regress_attempts" not in payload

    # -- Issue #1018: --from-cancelled requeue escape hatch -----------------

    def test_requeue_from_cancelled_cli_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`--from-cancelled` on a CANCELLED ticket exits 0 and moves PENDING."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6201",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-cancelled",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "impl -> impl" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_from_cancelled_cli_without_flag_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """CANCELLED ticket without --from-cancelled exits 1 and names the
        escape hatch in the printed error."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6202",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "requeue", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0
        assert "--from-cancelled" in result.output

    def test_requeue_from_cancelled_emits_reason_event(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--from-cancelled` emits TICKET_REQUEUED with the
        cli_requeue_from_cancelled provenance reason."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6203",
            status=QueueItemStatus.CANCELLED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-cancelled",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_requeue_from_cancelled"
        assert payload["regressed"] is False

    def test_requeue_from_cancelled_flag_on_approvable_row_emits_plain_reason(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--from-cancelled` passed defensively on an already-approvable
        (BLOCKED_ON_USER) row must NOT emit the cli_requeue_from_cancelled
        reason — that would falsely claim the row was recovered from
        CANCELLED when the CANCELLED branch never fired."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6204",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-cancelled",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_requeue"

    def test_requeue_from_failed_cli_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`--from-failed` on a FAILED ticket exits 0 and moves PENDING."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6301",
            status=QueueItemStatus.FAILED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-failed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "impl -> impl" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_requeue_from_failed_cli_without_flag_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """FAILED ticket without --from-failed exits nonzero and names the
        escape hatch in the printed error."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6302",
            status=QueueItemStatus.FAILED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "requeue", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0
        assert "--from-failed" in result.output

    def test_requeue_from_failed_emits_reason_event(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--from-failed` emits TICKET_REQUEUED with the
        cli_requeue_from_failed provenance reason."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6303",
            status=QueueItemStatus.FAILED,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-failed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_requeue_from_failed"
        assert payload["regressed"] is False

    def test_requeue_from_failed_flag_on_approvable_row_emits_plain_reason(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--from-failed` passed defensively on an already-approvable
        (BLOCKED_ON_USER) row must NOT emit the cli_requeue_from_failed
        reason — that would falsely claim the row was recovered from FAILED
        when the FAILED branch never fired."""
        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            stage=Stage.IMPL,
            session_id="sess6304",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda _type, payload=None, **__: captured.append(payload or {}),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "requeue",
                "GEN-500",
                "--client",
                "genhealth",
                "--from-failed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        payload = captured[0]
        assert payload["reason"] == "cli_requeue"


# ---------------------------------------------------------------------------
# TestCLIDevQueueDrain — cw dev-queue drain --held (RFC 0011 A4, #1161)
# ---------------------------------------------------------------------------


class TestCLIDevQueueDrain:
    """CLI tests for `cw dev-queue drain --held`."""

    def test_drain_held_requeues_matching_rows(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-cli-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "drain", "--held", "--client", "genhealth"],
        )

        assert result.exit_code == 0, result.output
        assert "GEN-500" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.PENDING

    def test_drain_missing_client_is_usage_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-cli-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "drain", "--held"])

        assert result.exit_code != 0
        assert "Missing option" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_drain_missing_held_flag_is_usage_error(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "drain", "--client", "genhealth"])

        assert result.exit_code != 0
        assert "Missing option" in result.output

    def test_drain_empty_selection_exits_zero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_client_yaml(tmp_config_dir, tmp_path)
        save_dev_queue(DevQueueStore(tasks=[]))

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "drain", "--held", "--client", "genhealth"]
        )

        assert result.exit_code == 0, result.output
        assert "No held tickets to drain" in result.output

    def test_drain_lane_filter(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        lane_a = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-cli-lane-a",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        lane_a.lane = "a"
        lane_b = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-cli-lane-b",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        lane_b.lane = "b"
        save_dev_queue(DevQueueStore(tasks=[lane_a, lane_b]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "drain",
                "--held",
                "--client",
                "genhealth",
                "--lane",
                "a",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "GEN-500" in result.output
        assert "GEN-501" not in result.output
        store = load_dev_queue()
        by_id = {t.ticket_id: t for t in store.tasks}
        assert by_id["GEN-500"].status == QueueItemStatus.PENDING
        assert by_id["GEN-501"].status == QueueItemStatus.BLOCKED_ON_USER

    def test_drain_dry_run_no_mutation(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-cli-dry-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "drain",
                "--held",
                "--client",
                "genhealth",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Would drain" in result.output
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_drain_partial_failure_nonzero_exit(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        first = _make_blocked_task(
            ticket_id="GEN-500",
            session_id="sess-cli-fail-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        second = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-cli-fail-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[first, second]))

        monkeypatch.setattr(
            "cw.dev_queue.drain.requeue_ticket", _requeue_that_fails_for("GEN-501")
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "drain", "--held", "--client", "genhealth"]
        )

        assert result.exit_code != 0
        assert "Drained GEN-500" in result.output
        assert "Failed to drain GEN-501" in result.output

    def test_drain_events_emitted_per_success(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        _write_client_yaml(tmp_config_dir, tmp_path)
        first = _make_blocked_task(
            ticket_id="GEN-500",
            stage=Stage.PLAN,
            session_id="sess-cli-evt-1",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        second = _make_blocked_task(
            ticket_id="GEN-501",
            session_id="sess-cli-evt-2",
            disposition=AWAITING_OPERATOR_DISPOSITION,
        )
        save_dev_queue(DevQueueStore(tasks=[first, second]))

        monkeypatch.setattr(
            "cw.dev_queue.drain.requeue_ticket", _requeue_that_fails_for("GEN-501")
        )
        events: list[tuple[OrchestratorEventType, dict[str, object], str | None]] = []
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.record_event",
            lambda etype, payload=None, **kw: events.append(
                (etype, payload or {}, kw.get("correlation_id"))
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "drain", "--held", "--client", "genhealth"]
        )

        assert result.exit_code != 0
        requeued_events = [
            e for e in events if e[0] == OrchestratorEventType.TICKET_REQUEUED
        ]
        assert len(requeued_events) == 1
        _, payload, _ = requeued_events[0]
        assert payload["ticket_id"] == "GEN-500"
        assert payload["reason"] == "cli_drain_held"
        assert payload["from_stage"] == "plan"
        assert payload["to_stage"] == "plan"


# ---------------------------------------------------------------------------
# TestCLIUnblock — cw dev-queue unblock
# ---------------------------------------------------------------------------


class TestCLIUnblock:
    """CLI tests for `cw dev-queue unblock`."""

    def test_unblock_happy_path(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """CLI unblock clears park markers and prints confirmation."""
        from cw.config import save_state
        from cw.models import CwState, ReapReason

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess5001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess5001",
            last_result={"status": "salvage_parked"},
            reap_reason=ReapReason.SALVAGE_PARKED,
        )
        save_state(CwState(sessions=[session]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "unblock", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code == 0, result.output
        assert "Unblocked GEN-500" in result.output

    def test_unblock_not_park_marked_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Non-park-marked session exits 1."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.IMPL, session_id="sess5002")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(session_id="sess5002", reap_reason=None)
        save_state(CwState(sessions=[session]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "unblock", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestTransitionTaskStatus
# ---------------------------------------------------------------------------


class TestTransitionTaskStatus:
    def test_sets_status(self) -> None:
        """transition_task_status mutates the task's status field in place."""
        task = TicketTask(
            ticket_id="T-1", client="genhealth", status=QueueItemStatus.PENDING
        )
        transition_task_status(task, QueueItemStatus.COMPLETED)
        assert task.status == QueueItemStatus.COMPLETED

    def test_all_valid_statuses(self) -> None:
        """transition_task_status accepts every QueueItemStatus value."""
        for status in QueueItemStatus:
            task = TicketTask(
                ticket_id="T-2", client="genhealth", status=QueueItemStatus.PENDING
            )
            transition_task_status(task, status)
            assert task.status == status

    def test_cancel_task_for_session_routes_through_seam(
        self, tmp_dev_queue: Path
    ) -> None:
        """cancel_task_for_session calls transition_task_status for the status write."""
        from unittest.mock import patch

        task = TicketTask(
            ticket_id="GEN-1",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            session_id="sess-seam-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with patch(
            "cw.dev_queue.crud.transition_task_status", wraps=transition_task_status
        ) as spy:
            result = cancel_task_for_session("sess-seam-1")

        assert result is True
        assert spy.called
        new_status = spy.call_args.args[1]
        assert new_status == QueueItemStatus.CANCELLED
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.CANCELLED

    def test_terminal_stamps_disposition_and_completed_at(self) -> None:
        """COMPLETED/BLOCKED_ON_USER/FAILED stamp disposition + completed_at."""
        for status in (
            QueueItemStatus.COMPLETED,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.FAILED,
        ):
            task = TicketTask(
                ticket_id="T-dsp", client="genhealth", status=QueueItemStatus.PENDING
            )
            before = datetime.now(UTC)
            transition_task_status(
                task, status, disposition="shipped", pr_url="http://x"
            )
            after = datetime.now(UTC)
            assert task.disposition == "shipped"
            assert task.pr_url == "http://x"
            assert task.completed_at is not None
            assert before <= task.completed_at <= after

    def test_terminal_stamps_none_disposition_by_default(self) -> None:
        """Terminal transition with no disposition kwarg stamps disposition=None."""
        task = TicketTask(
            ticket_id="T-dsp2", client="genhealth", status=QueueItemStatus.PENDING
        )
        transition_task_status(task, QueueItemStatus.COMPLETED)
        assert task.disposition is None
        assert task.pr_url is None
        assert task.completed_at is not None

    def test_reset_clears_disposition(self) -> None:
        """PENDING and CANCELLED clear disposition/pr_url/completed_at."""
        for reset_status in (QueueItemStatus.PENDING, QueueItemStatus.CANCELLED):
            task = TicketTask(
                ticket_id="T-reset",
                client="genhealth",
                status=QueueItemStatus.COMPLETED,
                disposition="shipped",
                pr_url="http://x",
                completed_at=datetime.now(UTC),
            )
            transition_task_status(task, reset_status)
            assert task.disposition is None, f"expected None after {reset_status}"
            assert task.pr_url is None
            assert task.completed_at is None

    def test_running_leaves_disposition_untouched(self) -> None:
        """RUNNING transition does not modify disposition fields."""
        ts = datetime.now(UTC)
        task = TicketTask(
            ticket_id="T-run",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            disposition="shipped",
            pr_url="http://x",
            completed_at=ts,
        )
        transition_task_status(task, QueueItemStatus.RUNNING)
        assert task.disposition == "shipped"
        assert task.pr_url == "http://x"
        assert task.completed_at == ts

    def test_clears_escalation_fields_unconditionally(self) -> None:
        """Any transition clears escalation_parked_at/fired_at (#1015, Q5)."""
        for status in QueueItemStatus:
            task = TicketTask(
                ticket_id="T-esc",
                client="genhealth",
                status=QueueItemStatus.PENDING,
                escalation_parked_at=datetime.now(UTC),
                escalation_fired_at=datetime.now(UTC),
            )
            transition_task_status(task, status)
            assert task.escalation_parked_at is None, (
                f"expected None after transition to {status}"
            )
            assert task.escalation_fired_at is None

    def test_transition_task_status_clears_stale_gate_latch_on_real_transition(
        self,
    ) -> None:
        """Any transition clears stale_gate_detected_at/blocked_on_pr (#1713),
        mirroring the escalation latch's unconditional-clear contract."""
        for status in QueueItemStatus:
            task = TicketTask(
                ticket_id="T-sg",
                client="genhealth",
                status=QueueItemStatus.PENDING,
                stale_gate_detected_at=datetime.now(UTC),
                blocked_on_pr=42,
            )
            transition_task_status(task, status)
            assert task.stale_gate_detected_at is None, (
                f"expected None after transition to {status}"
            )
            assert task.blocked_on_pr is None, (
                f"expected None after transition to {status}"
            )

    def test_requeue_clears_disposition(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Requeued task clears disposition, pr_url, and completed_at."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(ticket_id="GEN-RQ")
        task.disposition = "dirty_worktree"
        task.completed_at = datetime.now(UTC)
        save_dev_queue(DevQueueStore(tasks=[task]))
        requeue_ticket("GEN-RQ", "genhealth")
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.disposition is None
        assert requeued.completed_at is None

    def test_requeue_clears_escalation_fields(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Requeued task clears escalation_parked_at/fired_at (#1015, Q5)."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(ticket_id="GEN-RQ-ESC")
        task.escalation_parked_at = datetime.now(UTC)
        task.escalation_fired_at = datetime.now(UTC)
        save_dev_queue(DevQueueStore(tasks=[task]))
        requeue_ticket("GEN-RQ-ESC", "genhealth")
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.escalation_parked_at is None
        assert requeued.escalation_fired_at is None

    def test_cancel_clears_disposition(self, tmp_config_dir: Path) -> None:
        """Stamped task cancelled → disposition/pr_url/completed_at cleared."""
        task = TicketTask(
            ticket_id="GEN-CXL",
            client="claude-workspace",
            status=QueueItemStatus.RUNNING,
            session_id="sess-cxl",
            disposition="abandoned",
            completed_at=datetime.now(UTC),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        cancel_task_for_session("sess-cxl")
        store = load_dev_queue()
        cancelled = store.tasks[0]
        assert cancelled.status == QueueItemStatus.CANCELLED
        assert cancelled.disposition is None
        assert cancelled.completed_at is None

    # -- task.transition producer (RFC 0008 W1, #978) -----------------------

    def test_emits_task_transition_on_terminal(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """A RUNNING→COMPLETED move emits one task.transition with full payload."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="T-TR1",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
            lane="default",
            stage=Stage.FINALIZE,
            session_id="sess-tr1",
        )
        transition_task_status(
            task,
            QueueItemStatus.COMPLETED,
            disposition="shipped",
            pr_url="http://pr/1",
        )
        assert len(events) == 1
        etype, payload, corr = events[0]
        assert etype == OrchestratorEventType.TASK_TRANSITION
        assert corr == "T-TR1"
        assert payload["ticket_id"] == "T-TR1"
        assert payload["client"] == "genhealth"
        assert payload["lane"] == "default"
        assert payload["stage"] == Stage.FINALIZE
        assert payload["old_status"] == QueueItemStatus.RUNNING
        assert payload["new_status"] == QueueItemStatus.COMPLETED
        assert payload["disposition"] == "shipped"
        assert payload["session_id"] == "sess-tr1"
        assert payload["pr_url"] == "http://pr/1"

    def test_no_transition_emit_on_same_status(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """new_status == old_status emits nothing (Decision 6, no-op guard)."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="T-TR2",
            client="genhealth",
            status=QueueItemStatus.PENDING,
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert events == []

    def test_emits_task_transition_reset_class(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """A terminal→PENDING (reset) move emits task.transition."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="T-TR3",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["old_status"] == QueueItemStatus.BLOCKED_ON_USER
        assert payload["new_status"] == QueueItemStatus.PENDING

    def test_emits_task_transition_park_class(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """A RUNNING→AWAITING_OPERATOR_SIGNOFF (park) move emits task.transition."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="T-TR4",
            client="genhealth",
            status=QueueItemStatus.RUNNING,
        )
        transition_task_status(task, QueueItemStatus.AWAITING_OPERATOR_SIGNOFF)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["new_status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF


# ---------------------------------------------------------------------------
# TestDeriveDisposition
# ---------------------------------------------------------------------------


class TestDeriveDisposition:
    """Unit tests for _derive_disposition."""

    def setup_method(self) -> None:
        from cw.dev_queue import _derive_disposition

        self._derive = _derive_disposition

    def test_shipped(self) -> None:
        assert self._derive("shipped") == "shipped"

    def test_stage_complete(self) -> None:
        assert self._derive("stage_complete") == "stage_complete"

    def test_no_op(self) -> None:
        assert self._derive("no_op") == "no_op"

    def test_stage_failure_statuses(self) -> None:
        for s in ("blocked", "merge_gate_blocked", "scope_exceeded", "forbidden_area"):
            assert self._derive(s) == s

    def test_paused_for_user_input_statuses(self) -> None:
        for s in (
            "ambiguities_pending_resolution",
            "premises_pending_verification",
            "plan_pending_approval",
            "review_pending_approval",
        ):
            assert self._derive(s) == s

    def test_none_returns_abandoned(self) -> None:
        assert self._derive(None) == "abandoned"

    def test_unknown_returns_abandoned(self) -> None:
        assert self._derive("some_unknown_status") == "abandoned"

    def test_stale_dispatch_derives_verbatim(self) -> None:
        """#1862: STAGE_FAILURE_STATUSES membership composes in by reference."""
        assert self._derive("stale_dispatch") == "stale_dispatch"


# ---------------------------------------------------------------------------
# TestStaleDispatchDispositions (#1862)
# ---------------------------------------------------------------------------


class TestStaleDispatchDispositions:
    """The two #1862 disposition literals and their lockstep guards.

    The agent-emitted sentinel path and the code-side pre-dispatch gate park
    use DISTINCT literals (plan adopted assumption 7, #1729 precedent). One
    literal for both would put a gate-class park -- which hardcodes
    ``breadcrumbs=""`` and structurally cannot carry one -- into
    ``BREADCRUMB_ELIGIBLE_PAUSED_STATUSES``.
    """

    def test_status_derived_disposition_value(self) -> None:
        from cw.dev_queue import STALE_DISPATCH_DISPOSITION

        assert STALE_DISPATCH_DISPOSITION == "stale_dispatch"

    def test_status_derived_disposition_is_a_status_member(self) -> None:
        from typing import get_args

        from cw.auto_dev_result import Status
        from cw.dev_queue import STALE_DISPATCH_DISPOSITION

        assert STALE_DISPATCH_DISPOSITION in get_args(Status)

    def test_gate_disposition_value(self) -> None:
        from cw.dev_queue import STALE_DISPATCH_GATE_DISPOSITION

        assert STALE_DISPATCH_GATE_DISPOSITION == "stale_dispatch_gate"

    def test_gate_disposition_is_never_a_status_member(self) -> None:
        """Inverse lockstep guard: the gate literal must not collapse into
        the Status-derived set (adopted assumption 7)."""
        from typing import get_args

        from cw.auto_dev_result import Status
        from cw.dev_queue import STALE_DISPATCH_GATE_DISPOSITION

        assert STALE_DISPATCH_GATE_DISPOSITION not in get_args(Status)

    def test_gate_disposition_is_not_breadcrumb_eligible(self) -> None:
        from cw.dev_queue import STALE_DISPATCH_GATE_DISPOSITION
        from cw.dispatch import BREADCRUMB_ELIGIBLE_PAUSED_STATUSES

        assert (
            STALE_DISPATCH_GATE_DISPOSITION not in BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
        )

    def test_pre_dispatch_stale_pr_reason_value(self) -> None:
        from cw.dev_queue.lifecycle import _PRE_DISPATCH_STALE_PR_REASON

        assert _PRE_DISPATCH_STALE_PR_REASON == "pr_already_open_pre_dispatch"

    def test_gate_park_stamps_disposition_without_charging_attempt(self) -> None:
        """The code-side park: BLOCKED_ON_USER, no unproductive charge, no pr_url."""
        from cw.dev_queue import (
            STALE_DISPATCH_GATE_DISPOSITION,
            transition_task_status,
        )
        from cw.dev_queue.lifecycle import _PRE_DISPATCH_STALE_PR_REASON

        task = TicketTask(
            ticket_id="GEN-1862-park",
            client="test-client",
            status=QueueItemStatus.PENDING,
        )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=STALE_DISPATCH_GATE_DISPOSITION,
            blocked_reason=_PRE_DISPATCH_STALE_PR_REASON,
            unproductive=False,
        )
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "stale_dispatch_gate"
        assert task.blocked_reason == _PRE_DISPATCH_STALE_PR_REASON
        assert task.completed_at is not None
        assert task.pr_url is None
        assert task.unproductive_attempts == 0


# ---------------------------------------------------------------------------
# TestHoldAwareDisposition
# ---------------------------------------------------------------------------


class TestHoldAwareDisposition:
    """Unit tests for _hold_aware_disposition and the HOLD_DISPOSITIONS namespace.

    RFC 0011 A1 (#1254): a strict superset of _derive_disposition -- an
    operator-unavailable blocker reason wins over the verbatim status mapping.
    """

    def setup_method(self) -> None:
        from cw.dev_queue import _hold_aware_disposition

        self._derive = _hold_aware_disposition

    def test_operator_unavailable_reason_returns_awaiting_operator(self) -> None:
        assert self._derive("blocked", "operator_unavailable") == "awaiting_operator"

    def test_push_auth_failed_reason_returns_awaiting_operator(self) -> None:
        assert (
            self._derive("merge_gate_blocked", "push_auth_failed")
            == "awaiting_operator"
        )

    def test_non_hold_blocker_reason_falls_through_to_derive_disposition(self) -> None:
        assert self._derive("blocked", "agent_block") == "blocked"

    def test_none_blocker_reason_falls_through(self) -> None:
        assert self._derive("blocked", None) == "blocked"

    def test_hold_dispositions_contains_awaiting_operator(self) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION, HOLD_DISPOSITIONS

        assert AWAITING_OPERATOR_DISPOSITION in HOLD_DISPOSITIONS
        # RFC 0011 A3 (#1160) extended this frozenset in place with
        # FINALIZE_GATE_HELD_DISPOSITION rather than adding a parallel set.
        assert len(HOLD_DISPOSITIONS) == 2

    def test_awaiting_operator_disposition_value(self) -> None:
        from cw.dev_queue import AWAITING_OPERATOR_DISPOSITION

        assert AWAITING_OPERATOR_DISPOSITION == "awaiting_operator"


# ---------------------------------------------------------------------------
# TestMustFixMechanicallyRejectedDisposition (#1714)
# ---------------------------------------------------------------------------


class TestMustFixMechanicallyRejectedDisposition:
    """Set-membership contract for REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION.

    Deliberately NOT inside ``TestHoldAwareDisposition``: #1714 does not extend
    ``_hold_aware_disposition``. The new disposition is stamped directly by
    ``dispatch.routing._park_must_fix_mechanically_rejected``, never derived
    from a (status, blocker_reason) pair.
    """

    def test_disposition_value(self) -> None:
        from cw.dev_queue import REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION

        assert (
            REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION
            == "codex_must_fix_mechanically_rejected"
        )

    def test_disposition_excluded_from_hold(self) -> None:
        # R2.2: a hold means "parked pending a human/dependency, not pending a
        # fix". A dropped MUST_FIX IS pending a fix (re-run review with the
        # finding adjudicated), so it must not join the hold namespace — which
        # would also make it concierge-false-park-eligible.
        from cw.dev_queue import (
            HOLD_DISPOSITIONS,
            REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
        )

        assert (
            REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION not in HOLD_DISPOSITIONS
        )
        # Unchanged by #1714 — this ticket adds no HOLD_DISPOSITIONS member.
        assert len(HOLD_DISPOSITIONS) == 2

    def test_hold_aware_disposition_is_not_extended(self) -> None:
        # Negative proof that the round-1 mechanism was NOT implemented: the
        # generic verbatim stamp still applies to this reason, which is exactly
        # why routing.py needs its own dedicated park helper.
        from cw.dev_queue import _hold_aware_disposition

        assert (
            _hold_aware_disposition("blocked", "codex_must_fix_mechanically_rejected")
            == "blocked"
        )


# ---------------------------------------------------------------------------
# TestExtractPrUrl
# ---------------------------------------------------------------------------


class TestExtractPrUrl:
    """Unit tests for _extract_pr_url."""

    def setup_method(self) -> None:
        from cw.dev_queue import _extract_pr_url

        self._extract = _extract_pr_url

    def test_extracts_url_from_pr_dict(self) -> None:
        result: dict[str, object] = {
            "pr": {"url": "https://github.com/foo/bar/pull/42"}
        }
        assert self._extract(result) == "https://github.com/foo/bar/pull/42"

    def test_none_when_no_pr(self) -> None:
        assert self._extract({"status": "no_op"}) is None

    def test_none_when_pr_is_none(self) -> None:
        assert self._extract({"pr": None}) is None

    def test_none_for_none_input(self) -> None:
        assert self._extract(None) is None

    def test_none_when_pr_url_missing(self) -> None:
        assert self._extract({"pr": {}}) is None


class TestExtractPrUrlOrInfo:
    """Unit tests for _extract_pr_url_or_info (#1713)."""

    def setup_method(self) -> None:
        from cw.dev_queue import _extract_pr_url_or_info

        self._extract = _extract_pr_url_or_info

    def test_prefers_pr_dict_over_pr_info(self) -> None:
        """When `pr` is already populated (shipped/merge_pending shape), it
        wins over pr_info -- the fallback is never consulted."""
        result: dict[str, object] = {
            "pr": {"url": "https://github.com/foo/bar/pull/1"},
            "pr_info": {"url": "https://github.com/foo/bar/pull/2"},
        }
        assert self._extract(result) == "https://github.com/foo/bar/pull/1"

    def test_falls_back_to_pr_info_when_pr_is_null(self) -> None:
        """automerge_not_armed shape: pr=null (schema-forbidden non-null),
        pr_info carries the real PR."""
        result: dict[str, object] = {
            "pr": None,
            "pr_info": {"url": "https://github.com/foo/bar/pull/77"},
        }
        assert self._extract(result) == "https://github.com/foo/bar/pull/77"

    def test_none_for_none_input(self) -> None:
        assert self._extract(None) is None

    def test_none_when_pr_info_missing(self) -> None:
        assert self._extract({"pr": None}) is None

    def test_none_when_pr_info_not_a_dict(self) -> None:
        assert self._extract({"pr": None, "pr_info": "not-a-dict"}) is None

    def test_none_when_pr_info_url_missing(self) -> None:
        assert self._extract({"pr": None, "pr_info": {"number": 5}}) is None


# ---------------------------------------------------------------------------
# TestStageRegress
# ---------------------------------------------------------------------------


def _make_stage_task(
    stage: Stage = Stage.FINALIZE,
    worktree_path: Path | None = None,
    stage_high_water: Stage | None = None,
) -> TicketTask:
    return _make_ticket_task(
        ticket_id="REGRESS-1",
        client="test-client",
        status=QueueItemStatus.RUNNING,
        stage=stage,
        worktree_path=worktree_path,
        stage_high_water=stage_high_water,
    )


class TestStageRegress:
    """Unit tests for _stage_regress (GitHub #770)."""

    def test_sets_target_stage(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task(stage=Stage.FINALIZE)
        _stage_regress(task, Stage.IMPL)
        assert task.stage == Stage.IMPL

    def test_increments_regress_attempts(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task()
        assert task.regress_attempts == 0
        _stage_regress(task, Stage.IMPL)
        assert task.regress_attempts == 1
        task.status = QueueItemStatus.RUNNING
        _stage_regress(task, Stage.IMPL)
        assert task.regress_attempts == 2

    def test_sets_pending(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task()
        _stage_regress(task, Stage.IMPL)
        assert task.status == QueueItemStatus.PENDING

    def test_sets_regressed_into_stage(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task(stage=Stage.FINALIZE)
        assert task.regressed_into_stage is None
        _stage_regress(task, Stage.IMPL)
        assert task.regressed_into_stage == Stage.IMPL

    def test_sets_pending_operator_comment(self) -> None:
        """#1730: the shared stamp point also raises the pending-send-back marker."""
        from cw.dev_queue import _stage_regress

        task = _make_stage_task(stage=Stage.FINALIZE)
        assert task.pending_operator_comment is False
        _stage_regress(task, Stage.IMPL)
        assert task.pending_operator_comment is True

    def test_pending_operator_comment_stamped_regardless_of_target_stage(self) -> None:
        """#1730: the stamp is unconditional (like regressed_into_stage) -- the
        stage gate lives at the CONSUMPTION site (dispatch/claim.py), not here."""
        from cw.dev_queue import _stage_regress

        task = _make_stage_task(stage=Stage.REVIEW)
        _stage_regress(task, Stage.PLAN)
        assert task.pending_operator_comment is True

    def test_regress_attempts_sticky_but_regressed_into_stage_is_not(self) -> None:
        """#1794 R1: regress_attempts is cumulative/sticky across a ticket's life
        (bounds the FINALIZE self-heal cap, dispatch/routing.py);
        regressed_into_stage is per-arrival and must NOT leak into a later,
        unrelated forward advance into the same stage. This is the exact
        false-positive the prior (rejected) draft's design would have produced.
        """
        from cw.dev_queue import _advance_task_pointer, _stage_regress

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.REVIEW)

        # Operator regress: REVIEW -> PLAN.
        _stage_regress(task, Stage.PLAN)
        assert task.regress_attempts == 1
        assert task.regressed_into_stage == Stage.PLAN

        # Dispatch spawns the PLAN session (src/cw/dispatch/claim.py consumes
        # and clears the per-arrival marker before the session ever runs).
        task.regressed_into_stage = None

        # Ordinary forward advance (approve_ticket's path): PLAN -> IMPL.
        task.status = QueueItemStatus.RUNNING
        _advance_task_pointer(task, stages)

        assert task.stage == Stage.IMPL
        # Cumulative counter correctly still nonzero (bounds FINALIZE self-heal).
        assert task.regress_attempts == 1
        # Per-arrival marker correctly absent: this IMPL entry is an ordinary
        # forward approve, not a regress.
        assert task.regressed_into_stage is None

    def test_sets_finalize_regress_branch_head_only_from_finalize_origin(self) -> None:
        """#1717: regressing FROM FINALIZE stamps finalize_regress_branch_head
        with the pre-regress stage_base_ref (the branch-head oracle used later
        by REVIEW's repeat-detection read); regressing from any other stage
        leaves the field untouched (None), since only the FINALIZE self-heal
        round trip (#770) is what #1717 needs to detect a repeat for."""
        from cw.dev_queue import _stage_regress

        finalize_task = _make_stage_task(stage=Stage.FINALIZE)
        finalize_task.stage_base_ref = "deadbeef"
        assert finalize_task.finalize_regress_branch_head is None
        _stage_regress(finalize_task, Stage.IMPL)
        assert finalize_task.finalize_regress_branch_head == "deadbeef"
        # stage_base_ref itself is still cleared, same as before #1717.
        assert finalize_task.stage_base_ref is None

        review_task = _make_stage_task(stage=Stage.REVIEW)
        review_task.stage_base_ref = "cafef00d"
        _stage_regress(review_task, Stage.PLAN)
        assert review_task.finalize_regress_branch_head is None

    def test_clears_session_id(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task()
        task.session_id = "abc123"
        _stage_regress(task, Stage.IMPL)
        assert task.session_id is None

    def test_clears_stage_base_ref(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task()
        task.stage_base_ref = "deadbeef"
        _stage_regress(task, Stage.IMPL)
        assert task.stage_base_ref is None

    def test_preserves_worktree_path(self) -> None:
        from pathlib import Path

        from cw.dev_queue import _stage_regress

        wt = Path("/some/worktree/path")
        task = _make_stage_task(worktree_path=wt)
        _stage_regress(task, Stage.IMPL)
        assert task.worktree_path == wt

    def test_emits_stage_changed_regress(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """_stage_regress emits exactly one task.stage_changed direction=regress."""
        from cw.dev_queue import _stage_regress

        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_stage_task(stage=Stage.FINALIZE)
        _stage_regress(task, Stage.IMPL)
        assert len(events) == 1
        etype, payload, corr = events[0]
        assert etype == OrchestratorEventType.TASK_STAGE_CHANGED
        assert corr == "REGRESS-1"
        assert payload["ticket_id"] == "REGRESS-1"
        assert payload["client"] == "test-client"
        assert payload["old_stage"] == Stage.FINALIZE
        assert payload["new_stage"] == Stage.IMPL
        assert payload["direction"] == "regress"


class TestStageHighWaterStamping:
    """Unit tests for stage_high_water stamping (GitHub #1361)."""

    def test_advance_raises_high_water_from_none(self) -> None:
        from cw.dev_queue import _advance_task_pointer

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.HARDEN, stage_high_water=None)
        _advance_task_pointer(task, stages)
        assert task.stage_high_water == Stage.PLAN

    def test_advance_harden_plan_impl_sets_high_water_to_impl(self) -> None:
        from cw.dev_queue import _advance_task_pointer

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.HARDEN, stage_high_water=None)
        _advance_task_pointer(task, stages)  # HARDEN -> PLAN
        task.status = QueueItemStatus.RUNNING
        _advance_task_pointer(task, stages)  # PLAN -> IMPL
        assert task.stage_high_water == Stage.IMPL

    def test_advance_does_not_lower_high_water(self) -> None:
        from cw.dev_queue import _advance_task_pointer

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.HARDEN, stage_high_water=Stage.REVIEW)
        _advance_task_pointer(task, stages)  # HARDEN -> PLAN
        assert task.stage_high_water == Stage.REVIEW

    def test_stage_regress_leaves_high_water_unchanged(self) -> None:
        from cw.dev_queue import _stage_regress

        task = _make_stage_task(stage=Stage.IMPL, stage_high_water=Stage.IMPL)
        _stage_regress(task, Stage.HARDEN)
        assert task.stage_high_water == Stage.IMPL

    def test_apply_requeue_stage_backward_leaves_high_water_unchanged(self) -> None:
        from pathlib import Path

        from cw.dev_queue import _apply_requeue_stage

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.IMPL, stage_high_water=Stage.IMPL)
        task.status = QueueItemStatus.BLOCKED_ON_USER
        client_cfg = ClientConfig(
            name="test-client", workspace_path=Path("test-workspace")
        )
        _apply_requeue_stage(
            task,
            stages,
            stage_override="harden",
            client_cfg=client_cfg,
            allow_regress=True,
        )
        assert task.stage_high_water == Stage.IMPL

    def test_apply_requeue_stage_forward_raises_high_water(self) -> None:
        from pathlib import Path

        from cw.dev_queue import _apply_requeue_stage

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.PLAN, stage_high_water=Stage.PLAN)
        client_cfg = ClientConfig(
            name="test-client", workspace_path=Path("test-workspace")
        )
        _apply_requeue_stage(
            task,
            stages,
            stage_override="review",
            client_cfg=client_cfg,
            allow_regress=False,
        )
        assert task.stage_high_water == Stage.REVIEW

    def test_apply_requeue_stage_forward_does_not_lower_high_water(self) -> None:
        from pathlib import Path

        from cw.dev_queue import _apply_requeue_stage

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.PLAN, stage_high_water=Stage.FINALIZE)
        client_cfg = ClientConfig(
            name="test-client", workspace_path=Path("test-workspace")
        )
        _apply_requeue_stage(
            task,
            stages,
            stage_override="review",
            client_cfg=client_cfg,
            allow_regress=False,
        )
        assert task.stage_high_water == Stage.FINALIZE


class TestStampSalvageStage:
    """Unit tests for _stamp_salvage_stage (GitHub #1629)."""

    def test_forces_finalize_and_does_not_raise_high_water(self) -> None:
        from cw.dev_queue import _stamp_salvage_stage

        task = _make_stage_task(stage=Stage.IMPL, stage_high_water=Stage.IMPL)
        _stamp_salvage_stage(task)
        assert task.stage == Stage.FINALIZE
        # R1: high water stays put so stage_high_water != stage marks a row
        # salvaged before it ever reached finalize.
        assert task.stage_high_water == Stage.IMPL

    def test_does_not_touch_regressed_into_stage(self) -> None:
        """#1794: a terminal salvage stamp leaves any latent per-arrival marker
        inert -- the row is headed to COMPLETED, never re-dispatched off it."""
        from cw.dev_queue import _stamp_salvage_stage

        task = _make_stage_task(stage=Stage.IMPL, stage_high_water=Stage.IMPL)
        task.regressed_into_stage = Stage.IMPL
        _stamp_salvage_stage(task)
        assert task.regressed_into_stage == Stage.IMPL  # untouched, not read again

    def test_no_op_at_finalize(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        from cw.dev_queue import _stamp_salvage_stage

        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_stage_task(stage=Stage.FINALIZE, stage_high_water=Stage.FINALIZE)
        _stamp_salvage_stage(task)
        assert task.stage == Stage.FINALIZE
        assert events == []

    def test_emits_task_stage_changed(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        from cw.dev_queue import _stamp_salvage_stage

        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )
        task = _make_stage_task(stage=Stage.PLAN, stage_high_water=Stage.PLAN)
        _stamp_salvage_stage(task)
        assert len(events) == 1
        etype, payload, corr = events[0]
        assert etype == OrchestratorEventType.TASK_STAGE_CHANGED
        assert corr == "REGRESS-1"
        assert payload["old_stage"] == Stage.PLAN
        assert payload["new_stage"] == Stage.FINALIZE
        assert payload["direction"] == "advance"

    def test_normally_routed_completion_stage_equals_high_water_at_finalize(
        self,
    ) -> None:
        """The routed path is unaffected: walking the pointer to FINALIZE
        leaves stage == stage_high_water, so only salvaged rows diverge."""
        from cw.dev_queue import _advance_task_pointer

        stages = [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
        task = _make_stage_task(stage=Stage.HARDEN, stage_high_water=None)
        for _ in range(len(stages) - 1):
            task.status = QueueItemStatus.RUNNING
            _advance_task_pointer(task, stages)
        assert task.stage == Stage.FINALIZE
        assert task.stage_high_water == Stage.FINALIZE


class TestRegisterWatchedPr:
    """register_watched_pr idempotent insert (GitHub #1154, RFC 0011 S2, R7)."""

    def _watched(self, pr_number: int = 5, status: str = "active") -> WatchedPr:
        return WatchedPr(
            pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
            repo="foo/bar",
            pr_number=pr_number,
            source="cli",
            status=status,  # type: ignore[arg-type]
        )

    def test_register_inserts_new_watched_pr(self, tmp_config_dir: Path) -> None:
        assert register_watched_pr(self._watched()) is True
        store = load_dev_queue()
        assert len(store.watched_prs) == 1
        assert store.watched_prs[0].pr_number == 5

    def test_register_idempotent_on_same_repo_pr_number(
        self, tmp_config_dir: Path
    ) -> None:
        assert register_watched_pr(self._watched()) is True
        assert register_watched_pr(self._watched()) is False
        assert len(load_dev_queue().watched_prs) == 1

    def test_register_allows_reregistration_after_dismissed(
        self, tmp_config_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[self._watched(status="dismissed")]))
        assert register_watched_pr(self._watched()) is True
        store = load_dev_queue()
        assert len(store.watched_prs) == 2
        assert any(w.status == "active" for w in store.watched_prs)


class TestRegisterOrAdoptWatchedPr:
    """register_or_adopt_watched_pr (GitHub #1927): the ``client``-aware
    dedup ``register_watched_pr`` cannot provide on its own -- see the
    function's docstring for the silent-collision bug this closes."""

    def _watched(
        self,
        client: str | None,
        pr_number: int = 70,
        repo: str = "foo/bar",
    ) -> WatchedPr:
        return WatchedPr(
            pr_url=f"https://github.com/{repo}/pull/{pr_number}",
            repo=repo,
            pr_number=pr_number,
            client=client,
            source="stale_dispatch_park",
        )

    def test_inserts_when_no_existing_watch(self, tmp_config_dir: Path) -> None:
        assert register_or_adopt_watched_pr(self._watched("client-a")) == "inserted"
        store = load_dev_queue()
        assert len(store.watched_prs) == 1
        assert store.watched_prs[0].client == "client-a"

    def test_adopts_preexisting_null_client_watch_in_place(
        self, tmp_config_dir: Path
    ) -> None:
        """The common real-world collision: a pre-#1927 webhook/cli watch
        already exists for the same (repo, pr_number). Tagging it in place
        (rather than shadowing it) is what actually lets the park self-
        release, not merely what avoids a crash."""
        save_dev_queue(DevQueueStore(watched_prs=[self._watched(None)]))
        assert register_or_adopt_watched_pr(self._watched("client-a")) == "adopted"
        store = load_dev_queue()
        assert len(store.watched_prs) == 1
        assert store.watched_prs[0].client == "client-a"
        assert store.watched_prs[0].source == "stale_dispatch_park"

    def test_already_active_for_same_client_is_a_no_op(
        self, tmp_config_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[self._watched("client-a")]))
        assert (
            register_or_adopt_watched_pr(self._watched("client-a")) == "already_active"
        )
        assert len(load_dev_queue().watched_prs) == 1

    def test_collision_with_different_client_neither_inserts_nor_mutates(
        self, tmp_config_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[self._watched("client-a")]))
        assert register_or_adopt_watched_pr(self._watched("client-b")) == "collision"
        store = load_dev_queue()
        assert len(store.watched_prs) == 1
        assert store.watched_prs[0].client == "client-a"

    def test_collision_emits_watched_pr_collision_event(
        self,
        tmp_config_dir: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dev_queue.crud", OrchestratorEventType.WATCHED_PR_COLLISION
        )
        save_dev_queue(
            DevQueueStore(watched_prs=[self._watched("client-a", repo="acme/shared")])
        )
        register_or_adopt_watched_pr(self._watched("client-b", repo="acme/shared"))
        assert len(events) == 1
        etype, payload, corr = events[0]
        assert etype == OrchestratorEventType.WATCHED_PR_COLLISION
        assert corr == "client-b"
        assert payload["client"] == "client-b"
        assert payload["repo"] == "acme/shared"
        assert payload["pr_number"] == 70
        assert payload["colliding_client"] == "client-a"
        assert payload["colliding_source"] == "stale_dispatch_park"

    def test_adoption_emits_no_event(
        self,
        tmp_config_dir: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events("cw.dev_queue.crud")
        save_dev_queue(DevQueueStore(watched_prs=[self._watched(None)]))
        register_or_adopt_watched_pr(self._watched("client-a"))
        assert events == []


class TestLaneStatsUnaffectedByWatchedPr:
    """Registering a watched PR must not shift lane slot accounting (R10)."""

    def test_lane_stats_identical_before_and_after_registration(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        client = ClientConfig(
            name="acme",
            workspace_path=tmp_path / "ws",
            default_branch="main",
            lanes=[LaneConfig(name="impl", max_parallel=2)],
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-1",
                        client="acme",
                        lane="impl",
                        status=QueueItemStatus.RUNNING,
                    ),
                    TicketTask(
                        ticket_id="GEN-2",
                        client="acme",
                        lane="impl",
                        status=QueueItemStatus.PENDING,
                    ),
                ]
            )
        )
        before = _lane_stats_for_client(client, load_dev_queue())
        register_watched_pr(
            WatchedPr(
                pr_url="https://github.com/foo/bar/pull/9",
                repo="foo/bar",
                pr_number=9,
                source="webhook",
            )
        )
        after = _lane_stats_for_client(client, load_dev_queue())
        assert before == after


# ---------------------------------------------------------------------------
# TestUnproductiveAttempts (GitHub #1750)
# ---------------------------------------------------------------------------


class TestUnproductiveAttempts:
    """The second, narrower attempt counter and its single increment seam.

    ``task.attempts`` counts every claim (and still gates the #756 per-stage
    validation_failed cap); ``task.unproductive_attempts`` counts only claims
    that exited RUNNING with no evidence of progress, and is what the global
    dispatch ceiling reads. See GitHub #1750 / #1727 / #1653.
    """

    def test_field_defaults_to_zero(self) -> None:
        task = TicketTask(ticket_id="GEN-1", client="acme")
        assert task.unproductive_attempts == 0

    def test_schema_version_bumped_to_32(self) -> None:
        assert DEV_QUEUE_SCHEMA_VERSION == 33

    def test_migrate_fills_unproductive_attempts_default(self) -> None:
        """migrate_dev_queue fills unproductive_attempts=0 on legacy rows (v32)."""
        raw: dict[str, object] = {
            "schema_version": 31,
            "tasks": [
                {
                    "ticket_id": "GEN-40",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["unproductive_attempts"] == 0
        assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION

    def test_migrate_preserves_existing_unproductive_attempts(self) -> None:
        """A non-zero counter survives a second migration pass (idempotent)."""
        raw: dict[str, object] = {
            "schema_version": 32,
            "tasks": [
                {
                    "ticket_id": "GEN-41",
                    "client": "test-client",
                    "priority": 0,
                    "status": "pending",
                    "unproductive_attempts": 4,
                }
            ],
        }
        migrated = migrate_dev_queue(raw)
        assert migrated["tasks"][0]["unproductive_attempts"] == 4

    # -- the transition_task_status increment seam --------------------------

    def test_running_exit_charges_by_default(self) -> None:
        """The ~30 non-sentinel RUNNING-exit call sites charge with no change."""
        task = TicketTask(
            ticket_id="GEN-2", client="acme", status=QueueItemStatus.RUNNING
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.unproductive_attempts == 1

    def test_running_exit_does_not_charge_when_productive(self) -> None:
        task = TicketTask(
            ticket_id="GEN-3", client="acme", status=QueueItemStatus.RUNNING
        )
        transition_task_status(task, QueueItemStatus.PENDING, unproductive=False)
        assert task.unproductive_attempts == 0

    def test_non_running_exit_never_charges(self) -> None:
        """Only a RUNNING exit can charge — a park→park move must not."""
        task = TicketTask(
            ticket_id="GEN-4", client="acme", status=QueueItemStatus.BLOCKED_ON_USER
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.unproductive_attempts == 0

    def test_running_to_running_never_charges(self) -> None:
        """new_status == RUNNING is not an exit, so no charge."""
        task = TicketTask(
            ticket_id="GEN-5", client="acme", status=QueueItemStatus.RUNNING
        )
        transition_task_status(task, QueueItemStatus.RUNNING)
        assert task.unproductive_attempts == 0

    def test_charge_accumulates_across_exits(self) -> None:
        task = TicketTask(
            ticket_id="GEN-6", client="acme", status=QueueItemStatus.RUNNING
        )
        for _ in range(3):
            transition_task_status(task, QueueItemStatus.PENDING)
            task.status = QueueItemStatus.RUNNING
        assert task.unproductive_attempts == 3

    def test_attempts_is_untouched_by_the_seam(self) -> None:
        """The new counter is additive — raw attempts stays the claim counter."""
        task = TicketTask(
            ticket_id="GEN-7",
            client="acme",
            status=QueueItemStatus.RUNNING,
            attempts=5,
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.attempts == 5

    # -- durable audit record on the existing TASK_TRANSITION event ----------

    def test_transition_event_carries_charge_keys(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """Every charge leaves a durable audit record on the existing event."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="GEN-8", client="acme", status=QueueItemStatus.RUNNING
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["unproductive_attempts"] == 1
        assert payload["unproductive_charge"] is True

    def test_transition_event_records_a_declined_charge(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="GEN-9", client="acme", status=QueueItemStatus.RUNNING
        )
        transition_task_status(task, QueueItemStatus.COMPLETED, unproductive=False)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["unproductive_attempts"] == 0
        assert payload["unproductive_charge"] is False

    def test_transition_event_charge_is_none_off_a_non_running_exit(
        self, capture_events: Callable[..., list[CapturedEvent]]
    ) -> None:
        """None distinguishes "guard never fired" from "declined to charge"."""
        events = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_TRANSITION
        )
        task = TicketTask(
            ticket_id="GEN-10",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            unproductive_attempts=2,
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert len(events) == 1
        _, payload, _ = events[0]
        assert payload["unproductive_attempts"] == 2
        assert payload["unproductive_charge"] is None

    # -- the two stage-mutation chokepoints ---------------------------------

    def test_advance_task_pointer_never_charges(self) -> None:
        """A forward stage advance is productive by construction."""
        task = TicketTask(
            ticket_id="GEN-11",
            client="acme",
            status=QueueItemStatus.RUNNING,
            stage=Stage.PLAN,
        )
        _advance_task_pointer(task, [Stage.PLAN, Stage.IMPL, Stage.REVIEW])
        assert task.unproductive_attempts == 0
        assert task.stage == Stage.IMPL

    def test_advance_task_pointer_preserves_a_nonzero_counter(self) -> None:
        task = TicketTask(
            ticket_id="GEN-12",
            client="acme",
            status=QueueItemStatus.RUNNING,
            stage=Stage.PLAN,
            unproductive_attempts=3,
        )
        _advance_task_pointer(task, [Stage.PLAN, Stage.IMPL, Stage.REVIEW])
        assert task.unproductive_attempts == 3

    def test_stage_regress_never_charges(self) -> None:
        """A deliberate backward move is a stage change, not a wasted claim."""
        task = TicketTask(
            ticket_id="GEN-13",
            client="acme",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
        )
        _stage_regress(task, Stage.IMPL)
        assert task.unproductive_attempts == 0
        assert task.stage == Stage.IMPL

    def test_stage_regress_preserves_a_nonzero_counter(self) -> None:
        task = TicketTask(
            ticket_id="GEN-14",
            client="acme",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
            unproductive_attempts=2,
        )
        _stage_regress(task, Stage.IMPL)
        assert task.unproductive_attempts == 2
        assert task.regress_attempts == 1
