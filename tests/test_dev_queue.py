"""Tests for cw.dev_queue and related CLI commands."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import load_orchestrator_config
from cw.dev_queue import add_ticket, list_tickets, load_dev_queue, resolve_client
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    TicketTask,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dev_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect dev queue file and lock to tmp_path."""
    dev_queue_file = tmp_path / "dev_queue.json"
    dev_queue_lock = tmp_path / ".dev_queue.lock"

    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", dev_queue_lock)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_LOCK", dev_queue_lock)

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
            except Exception as e:
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
        monkeypatch.setattr("cw.cli.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--priority", "3"],
        )
        assert result.exit_code == 0, result.output
        store = load_dev_queue()
        assert store.tasks[0].priority == 3


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
