"""Tests for cw.dev_queue and related CLI commands."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

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
    remove_ticket,
    resolve_client,
    save_dev_queue,
    save_plan,
    wait_for_terminal,
)
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    DevQueueStore,
    DispatchPlan,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)

if TYPE_CHECKING:
    from pathlib import Path


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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
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
        monkeypatch.setattr("cw.cli.dev_queue.record_event", lambda *_, **__: None)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "ABC-5", "--client", "genhealth", "--lane", "fast"],
        )
        assert result.exit_code != 0
        assert "fast" in result.output


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
            "cw.cli.dev_queue.latest_tick_summary_by_client",
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
            "cw.cli.dev_queue.latest_tick_summary_by_client",
            lambda: {"genhealth": tick},
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "[STALE" not in result.output


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
        monkeypatch.setattr("cw.dev_queue.consume_completed_sessions", lambda: 0)

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

        monkeypatch.setattr("cw.dev_queue.consume_completed_sessions", _side_effect)

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
        monkeypatch.setattr("cw.dev_queue.consume_completed_sessions", lambda: 0)

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
        expected_fields = {
            "ticket_id",
            "client",
            "status",
            "session_id",
            "attempts",
            "priority",
            "lane",
            "created_at",
            "total_cost_usd",
            "worktree_path",
        }
        assert set(tasks[0].keys()) == expected_fields

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
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=stage,
        session_id=session_id,
    )


def _make_session(
    session_id: str = "sess1234",
    last_result: dict[str, object] | None = None,
    reap_reason: object = None,
    workspace_path: object = None,
) -> object:
    """Build a Session with minimal required fields."""
    from pathlib import Path

    from cw.models import Session, SessionPurpose

    return Session(
        id=session_id,
        name=f"genhealth/impl-{session_id}",
        client="genhealth",
        purpose=SessionPurpose.IMPL,
        workspace_path=workspace_path or Path("/tmp/ws"),
        last_result=last_result,
        reap_reason=reap_reason,
    )


# ---------------------------------------------------------------------------
# TestApproveTicket — approve_ticket() mutation function
# ---------------------------------------------------------------------------


class TestApproveTicket:
    """Tests for approve_ticket()."""

    def test_approve_plan_pending_advances_to_impl(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """plan_pending_approval BLOCKED task advances to impl PENDING."""
        from cw.config import save_state
        from cw.dev_queue import approve_ticket
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess0001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess0001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

        result = approve_ticket("GEN-500", "genhealth")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        assert t.stage_base_ref is None

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

        with pytest.raises(ApproveGateError, match="not at an approval gate"):
            approve_ticket("GEN-500", "genhealth")

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
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--stage that is forward in pipeline moves task forward."""
        from cw.dev_queue import requeue_ticket

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess9002")
        save_dev_queue(DevQueueStore(tasks=[task]))

        result = requeue_ticket("GEN-500", "genhealth", stage_override="impl")

        assert result["from_stage"] == "plan"
        assert result["to_stage"] == "impl"
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "GEN-500")
        assert t.stage == Stage.IMPL
        assert t.status == QueueItemStatus.PENDING

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

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

    def test_approve_happy_path(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """CLI approve advances stage and prints confirmation."""
        from cw.config import save_state
        from cw.models import CwState

        _write_client_yaml(tmp_config_dir, tmp_path)
        task = _make_blocked_task(stage=Stage.PLAN, session_id="sess7001")
        save_dev_queue(DevQueueStore(tasks=[task]))
        session = _make_session(
            session_id="sess7001",
            last_result={"status": "plan_pending_approval"},
        )
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "approve", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0


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
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--stage impl from plan advances forward."""
        _write_client_yaml(tmp_config_dir, tmp_path)
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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

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
        save_state(CwState(sessions=[session]))  # type: ignore[list-item]

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "unblock", "GEN-500", "--client", "genhealth"],
        )
        assert result.exit_code != 0
