"""Tests for cw.dev_queue and related CLI commands."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import load_orchestrator_config
from cw.dev_queue import (
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    load_dev_queue,
    load_plan,
    plan_path,
    remove_ticket,
    resolve_client,
    save_dev_queue,
    save_plan,
)
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    DispatchPlan,
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
    dev_plan_file = tmp_path / "dev_plan.json"
    dev_plan_lock = tmp_path / ".dev_plan.lock"

    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", dev_queue_lock)
    monkeypatch.setattr("cw.config.DEV_PLAN_FILE", dev_plan_file)
    monkeypatch.setattr("cw.config.DEV_PLAN_LOCK", dev_plan_lock)

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

    def test_allows_terminal_duplicate(self, tmp_dev_queue: Path) -> None:
        """Existing COMPLETED entry does NOT block re-adding."""
        completed = TicketTask(
            ticket_id="GEN-3", client="genhealth", status=QueueItemStatus.COMPLETED
        )
        save_dev_queue(DevQueueStore(tasks=[completed]))
        new_task = TicketTask(ticket_id="GEN-3", client="genhealth")
        result = add_ticket(new_task)
        assert result is True
        store2 = load_dev_queue()
        assert len(store2.tasks) == 2


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
