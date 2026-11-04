"""Freshness preflight gate tests for ``dispatch_tick``.

Covers ``cw.dispatch.gating.freshness``: the stale-main gate
and the auto-fast-forward path with its non-main-head, diverged, dirty
and detached refusals. Split out of ``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    concurrency_override_file,
)
from cw.dev_queue import (
    add_ticket,
    load_dev_queue,
)
from cw.dispatch import (
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
    dispatch_tick,
)
from cw.dispatch.gating.freshness import _resolve_freshness
from cw.events import read_events
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConcurrencyOverride,
    ClientConfig,
    ConcurrencyOverrides,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.worktree import TickFetchGate
from cw.worktree._tick_fetch import BACKOFF_SKIP_REASON
from tests._clients_yaml import write_clients_yaml
from tests._worktree_helpers import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.worktree import FetchWarningKey


# ---------------------------------------------------------------------------
# TestDispatchTickFreshnessGate
# ---------------------------------------------------------------------------


class TestDispatchTickFreshnessGate:
    """Freshness-gate tests: stale main blocks dispatch and emits ticket.needs_sync."""

    def test_stale_main_skips_dispatch_and_keeps_pending(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale client: dispatch returns 0, task stays PENDING, event emitted."""
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-1", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0

        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.PENDING

        assert len(daemon.spawn_calls) == 0

        events = read_events(
            consumer="test-freshness",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "CW-1"
        assert events[0].payload["client"] == "test-client"

    def test_stale_main_emits_event_once_per_pending_ticket(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two PENDING tasks emit two ticket.needs_sync events (one per task)."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-10", client="test-client"))
        add_ticket(TicketTask(ticket_id="CW-11", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-freshness-multi",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 2
        ticket_ids = {e.payload["ticket_id"] for e in events}
        assert ticket_ids == {"CW-10", "CW-11"}

    def test_stale_check_skips_only_stale_client(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Stale client A skipped; fresh client B dispatches normally."""
        write_clients_yaml(sample_client_config)

        # Create second fresh client
        fresh_ws = make_git_repo("workspace/fresh-project")
        fresh_client = ClientConfig(
            name="fresh-client",
            workspace_path=fresh_ws,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-fresh",
        )
        # Append fresh-client to clients.yaml
        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        clients_file = config_dir / "clients.yaml"
        existing = clients_file.read_text()
        existing += (
            f"  {fresh_client.name}:\n"
            f"    workspace_path: {fresh_client.workspace_path}\n"
            f"    default_branch: {fresh_client.default_branch}\n"
            f"    worktree_base: {fresh_client.worktree_base}\n"
        )
        clients_file.write_text(existing)

        add_ticket(TicketTask(ticket_id="CW-20", client="test-client"))
        add_ticket(TicketTask(ticket_id="CW-21", client="fresh-client"))

        def _freshness_check(
            client: ClientConfig,
            warned_fetch_fail: set[FetchWarningKey] | None = None,
        ) -> tuple[bool, str, str, int]:
            if client.name == "test-client":
                return (True, "aaa", "bbb", 2)
            return (False, "abc", "abc", 0)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin", _freshness_check
        )

        # fresh-client also needs cap=1
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"test-client": 1, "fresh-client": 1},
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(config, native_daemon=daemon).spawned

        assert spawned == 1  # only fresh-client

        events = read_events(
            consumer="test-freshness-split",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["client"] == "test-client"

    def test_fresh_main_dispatches_normally(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh main: existing dispatch behaviour unchanged."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-30", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1

        events = read_events(
            consumer="test-freshness-no-event",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 0, "Fresh main should not emit ticket.needs_sync"

    def test_freshness_check_called_once_per_client_per_tick(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """is_main_behind_origin called exactly once per client even with 3 tasks."""
        write_clients_yaml(sample_client_config)
        for i in range(3):
            add_ticket(TicketTask(ticket_id=f"CW-4{i}", client="test-client"))

        call_count = 0

        def _counting(
            _client: ClientConfig,
            warned_fetch_fail: set[FetchWarningKey] | None = None,
        ) -> tuple[bool, str, str, int]:
            nonlocal call_count
            call_count += 1
            return (False, "abc", "abc", 0)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin", _counting
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert call_count == 1

    def test_freshness_check_missing_workspace_no_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """dispatch_tick with missing workspace_path: WARNING logged, no traceback."""
        missing_dir = tmp_path / "nonexistent"  # intentionally not created
        missing_client = ClientConfig(
            name="missing-ws",
            workspace_path=missing_dir,
            default_branch="main",
        )
        write_clients_yaml(missing_client)
        add_ticket(TicketTask(ticket_id="CW-99", client="missing-ws"))

        daemon = FakeNativeDaemonClient()
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        caplog.set_level(logging.WARNING, logger="cw.worktree")

        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"missing-ws": 1},
        )
        # Should not raise even with missing workspace_path
        dispatch_tick(config, native_daemon=daemon)

        # No exc_info on freshness-related log records.
        # (dispatch may log other errors if it proceeds to create_worktree with
        # the missing path; those are separate concerns from the freshness gate.)
        freshness_records = [
            r
            for r in caplog.records
            if r.name in ("cw.dispatch", "cw.worktree._freshness")
            and "freshness" in r.message.lower()
        ]
        assert not any(r.exc_info for r in freshness_records), (
            "No traceback should appear for missing workspace freshness check — "
            "got exc_info on: "
            + str([r.message for r in freshness_records if r.exc_info])
        )
        # The freshness skip warning should appear
        assert any("freshness_check_skip" in r.message for r in caplog.records), (
            "Expected freshness_check_skip warning for missing workspace"
        )

    def test_freshness_check_failure_does_not_block_dispatch(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RuntimeError from freshness check: WARNING logged, dispatch proceeds."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-50", client="test-client"))

        def _boom(
            _client: ClientConfig,
            warned_fetch_fail: set[FetchWarningKey] | None = None,
        ) -> tuple[bool, str, str, int]:
            msg = "network unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.gating.freshness.is_main_behind_origin", _boom)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1  # dispatch proceeded
        assert any(
            "freshness check failed" in r.message.lower() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestFreshnessGateAutoFF
# ---------------------------------------------------------------------------


class TestFreshnessGateAutoFF:
    """Auto-ff tests: stale+behind triggers fast-forward; other states block."""

    def test_auto_ff_behind_succeeds_claims_ticket(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' + successful ff → task claimed.

        TICKET_NEEDS_SYNC must NOT be emitted; spawned=1.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-100", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "abc12345" * 5, "def67890" * 5, 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.fast_forward_main",
            lambda _client, **_kwargs: ("abc12345" * 5, "def67890" * 5),
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        # ff succeeded → stale cleared → task should be spawned
        assert result.spawned == 1

        events = read_events(
            consumer="test-auto-ff-behind",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        # TICKET_NEEDS_SYNC must NOT be emitted after a successful auto-ff.
        assert len(events) == 0

    def test_auto_ff_ahead_skips_with_ticket_needs_sync(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='ahead' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-101", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "ahead",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-ahead",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "CW-101"

    def test_auto_ff_diverged_skips(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='diverged' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-102", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-diverged",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_detached_skips(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='detached' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-103", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "detached",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-detached",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_ff_raises_falls_through_to_ticket_needs_sync(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' but fast_forward_main raises WorktreeError.

        Exception must be swallowed; TICKET_NEEDS_SYNC emitted as fallback.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-104", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        def _boom(_client: object, **_kwargs: object) -> tuple[str, str]:
            msg = "git pull failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.dispatch.gating.freshness.fast_forward_main", _boom)

        daemon = FakeNativeDaemonClient()
        # Exception must be swallowed; falls through to TICKET_NEEDS_SYNC.
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-raises",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_non_main_head_skips_fast_forward_emits_non_main_head_detail(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns non-default branch → dispatch bails before ff.

        When the dispatch repo's HEAD is on a non-default branch and the repo is
        stale, dispatch must:
        - emit skip_reason=freshness_gate with freshness_detail="non_main_head"
        - still emit TICKET_NEEDS_SYNC for the blocked task
        - NOT call fast_forward_main
        - not spawn any sessions (spawned==0)
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-110", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch",
            lambda _client: "feature/xyz",
        )

        ff_called = {"count": 0}

        def _ff_spy(_client: object, **_kwargs: object) -> tuple[str, str]:
            ff_called["count"] += 1
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.dispatch.gating.freshness.fast_forward_main", _ff_spy)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert ff_called["count"] == 0

        tick_events = read_events(
            consumer="test-auto-ff-non-main-head-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        )
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_NON_MAIN_HEAD

        sync_events = read_events(
            consumer="test-auto-ff-non-main-head-sync",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(sync_events) == 1
        assert sync_events[0].payload["ticket_id"] == "CW-110"

    def test_auto_ff_detached_head_uses_normal_path(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns None (detached) → normal auto-ff path proceeds.

        A detached HEAD is not the non-main-HEAD case; fast_forward_main should
        be attempted (check_main_ff_safety gates it appropriately).
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-111", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch",
            lambda _client: None,  # detached HEAD
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        ff_called = {"count": 0}

        def _ff_spy(_client: object, **_kwargs: object) -> tuple[str, str]:
            ff_called["count"] += 1
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.dispatch.gating.freshness.fast_forward_main", _ff_spy)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert ff_called["count"] == 1

    def test_auto_ff_on_default_branch_uses_normal_path(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns default_branch → normal path (not non_main_head).

        When HEAD == default_branch and the repo is stale with diverged safety,
        dispatch emits freshness_detail="main_diverged_from_origin" — NOT
        "non_main_head" (which would be wrong when we ARE on the default branch).
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-112", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch",
            lambda _client: "main",  # on default branch
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "diverged",  # unsafe, so auto-ff skipped
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        tick_events = read_events(
            consumer="test-auto-ff-default-branch-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        )
        # Key assertion: not NON_MAIN_HEAD — we ARE on the default branch.
        # With diverged safety, the new distinct detail is FRESHNESS_MAIN_DIVERGED.
        assert tick_events[0].payload["freshness_detail"] != FRESHNESS_NON_MAIN_HEAD
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED

    def test_auto_ff_non_main_head_detached_at_emit_time_shows_detached(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TOCTOU: get_head_branch returns None in _emit_stale_skip → "(detached)".

        _resolve_freshness detects a non-default branch and returns
        freshness_detail="non_main_head".  By the time _emit_stale_skip calls
        get_head_branch a second time the HEAD has moved to detached; the WARN
        message should fall back to "(detached)".
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-113", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )

        call_count: list[int] = [0]

        def _get_head_toctou(_client: object) -> str | None:
            call_count[0] += 1
            if call_count[0] == 1:
                return "feature/xyz"  # _resolve_freshness: non-default → bail
            return None  # _emit_stale_skip: HEAD detached (TOCTOU)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch", _get_head_toctou
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        dispatch_tick(
            simple_config,
            native_daemon=daemon,
            emit=emitted.append,
        )

        assert any("(detached)" in m for m in emitted)

    def test_auto_ff_false_keeps_ticket_needs_sync(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """auto_ff=False preserves legacy block-only behavior even when 'behind'."""
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-105", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        # check_main_ff_safety must NOT be called; if it is called that's a bug
        check_called = [False]

        def _check_boom(_client: object) -> str:
            check_called[0] = True
            return "behind"

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety", _check_boom
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, auto_ff=False, native_daemon=daemon)

        assert result.spawned == 0
        # check_main_ff_safety must NOT be called when auto_ff=False.
        assert not check_called[0]
        events = read_events(
            consumer="test-auto-ff-disabled",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_ahead_emits_diverged_detail(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='ahead' → freshness_detail='main_diverged_from_origin' (#766).

        When local main is ahead of origin (unpushed commits exist), the
        dispatch loop should emit a distinct freshness_detail so the operator
        can distinguish "ahead" from "behind" in the status output.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-120", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "ahead",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-ahead-diverged-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED
        assert any("diverged" in ln for ln in emitted)

    def test_auto_ff_diverged_emits_diverged_detail(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='diverged' → freshness_detail='main_diverged_from_origin' (#766).

        When local main has diverged from origin (has both local and remote
        commits), a distinct freshness_detail tells the operator to reconcile
        rather than just wait for auto-ff.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-121", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-diverged-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED
        assert any("diverged" in ln for ln in emitted)

    def test_auto_ff_behind_dirty_emits_dirty_checkout_detail(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' + dirty checkout → freshness_detail='main_dirty_checkout'.

        When local main is behind origin but the working tree has uncommitted
        tracked changes, auto-ff is blocked.  A distinct freshness_detail
        tells the operator to commit or stash — not wait for auto-ff.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-122", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_checkout_dirty",
            lambda _client: True,
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-dirty-checkout-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIRTY_CHECKOUT
        )
        assert any("dirty" in ln or "uncommitted" in ln for ln in emitted)

    def test_auto_ff_diverged_warn_advises_inspect_not_rebase(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#940: diverged WARN advises inspect-first, not ``pull --rebase``.

        A diverged main may carry stray commits from an isolation breach; the
        operator must inspect before touching it, so the advice points at a
        read-only ``git log origin/<default_branch>..HEAD`` and explicitly warns
        against auto-rebase/reset.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-940", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch",
            lambda _client: "main",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        diverged_warns = [ln for ln in emitted if "diverged" in ln]
        assert diverged_warns, f"no diverged WARN emitted: {emitted}"
        warn = diverged_warns[0]
        assert "log origin/" in warn
        assert "do NOT auto-rebase" in warn
        assert "pull --rebase" not in warn

    def test_auto_ff_detached_emits_detached_detail(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='detached' → freshness_detail='main_detached_head' (#964).

        When the client's main checkout HEAD is detached, dispatch should
        emit a distinct freshness_detail so the operator WARN gives accurate
        checkout advice instead of falling through to the generic
        "main behind origin" message.
        """
        write_clients_yaml(sample_client_config)
        task = TicketTask(ticket_id="CW-964", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.get_head_branch",
            lambda _client: None,  # detached HEAD
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "detached",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-detached-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DETACHED
        detached_warns = [ln for ln in emitted if "detached" in ln]
        assert detached_warns, f"no detached WARN emitted: {emitted}"
        warn = detached_warns[0]
        assert "checkout" in warn


# ---------------------------------------------------------------------------
# TestTickFetchGateDegrade (#2910)
# ---------------------------------------------------------------------------


class _HungFetch:
    """``subprocess.run``: every ``git fetch`` hangs until its timer fires.

    Advances *clock* by the granted timeout and raises ``TimeoutExpired``; a
    fetch with no timeout fails the test. Every other command runs for real.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.fetch_cwds: list[object] = []
        self._real = subprocess.run

    def __call__(self, cmd: object, **kwargs: object) -> object:
        if isinstance(cmd, list) and cmd[:2] == ["git", "fetch"]:
            timeout = kwargs.get("timeout")
            if not isinstance(timeout, int | float):
                pytest.fail(f"unbounded in-tick fetch: {cmd}")
            self.fetch_cwds.append(kwargs.get("cwd"))
            self.clock.advance(timeout)
            raise subprocess.TimeoutExpired(cmd, timeout)
        return self._real(cmd, **kwargs)


def _spy_on(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Record each call to ``cw.dispatch.tick.<name>`` by client, then run it."""
    import cw.dispatch.tick as tick_mod

    real = getattr(tick_mod, name)
    calls: list[str] = []

    def _spy(client_name: str, *args: object, **kwargs: object) -> object:
        calls.append(client_name)
        return real(client_name, *args, **kwargs)

    monkeypatch.setattr(tick_mod, name, _spy)
    return calls


def _seed_latch(client_name: str, count: int) -> bytes:
    _save_concurrency_overrides(
        ConcurrencyOverrides(
            clients={
                client_name: ClientConcurrencyOverride(
                    consecutive_freshness_blocks=count
                )
            }
        )
    )
    return concurrency_override_file().read_bytes()


_SEEDED_LATCH = 3


class TestTickFetchGateDegrade:
    """An expired or skipped in-tick fetch is freshness-unknown (#2910).

    The client is dispatched as for any failed fetch: no ``ticket.needs_sync``,
    no latch increment, and no override-file write at all.
    """

    def test_an_expired_fetch_dispatches_and_writes_nothing(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-2910", client="test-client"))
        before = _seed_latch("test-client", _SEEDED_LATCH)
        clock = FakeClock()
        gate = TickFetchGate(warned=set(), clock=clock)
        hung = _HungFetch(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", hung)
        recorded = _spy_on(monkeypatch, "_record_client_freshness_block")
        reset = _spy_on(monkeypatch, "_reset_client_freshness_blocks")

        daemon = FakeNativeDaemonClient()
        with gate.tick():
            result = dispatch_tick(
                simple_config, native_daemon=daemon, warned_fetch_fail=gate.warned
            )

        assert result.spawned == 1
        assert hung.fetch_cwds  # the freshness fetch did run, bounded
        assert recorded == []
        assert reset == []
        assert concurrency_override_file().read_bytes() == before
        assert (
            read_events(
                consumer="test-2910-degrade",
                event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
            )
            == []
        )
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.RUNNING
        assert task.disposition is None

    def test_control_a_fresh_tick_still_resets_the_latch(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the byte-identical check above can fail."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-2911", client="test-client"))
        before = _seed_latch("test-client", _SEEDED_LATCH)
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        gate = TickFetchGate(warned=set(), clock=FakeClock())

        with gate.tick():
            dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert concurrency_override_file().read_bytes() != before
        latch = _load_concurrency_overrides().clients["test-client"]
        assert latch.consecutive_freshness_blocks == 0

    def test_three_hung_clients_spend_at_most_the_budget_and_all_dispatch(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        names = ("c1", "c2", "c3")
        clients = [
            ClientConfig(
                name=name,
                workspace_path=make_git_repo(f"workspace/{name}"),
                default_branch="main",
                worktree_base=tmp_path / "worktrees" / name,
            )
            for name in names
        ]
        write_clients_yaml(*clients)
        for name in names:
            add_ticket(TicketTask(ticket_id=f"CW-{name}", client=name))
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel=dict.fromkeys(names, 1),
        )
        clock = FakeClock()
        gate = TickFetchGate(warned=set(), clock=clock)
        hung = _HungFetch(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", hung)

        with gate.tick():
            result = dispatch_tick(
                config,
                native_daemon=FakeNativeDaemonClient(),
                warned_fetch_fail=gate.warned,
            )

        assert result.spawned == len(names)
        # Two freshness fetches time out; the third client, and every
        # claim-path fetch, is skipped by the budget or the backoff.
        assert len(hung.fetch_cwds) == len(names) - 1
        assert clock() <= gate.budget_s

    def test_auto_ff_runs_a_local_merge_not_a_pull(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-2912", client="test-client"))
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "a" * 40, "b" * 40, 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        ff_kwargs: list[dict[str, object]] = []

        def _ff(_client: ClientConfig, **kwargs: object) -> tuple[str, str]:
            ff_kwargs.append(kwargs)
            return ("a" * 40, "b" * 40)

        monkeypatch.setattr("cw.dispatch.gating.freshness.fast_forward_main", _ff)

        dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert ff_kwargs == [{"ignore_untracked": True, "fetch_first": False}]

    def test_an_identical_timeout_reason_warns_once(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pins same-reason dedup only: advancing by ``backoff_s`` makes tick 2 a
        second real timeout. The real 30 s cadence is the timeline test below."""
        clock = FakeClock()
        gate = TickFetchGate(warned=set(), clock=clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", _HungFetch(clock))

        caplog.set_level(logging.WARNING, logger="cw.worktree")
        results = []
        for _ in range(2):
            with gate.tick():
                results.append(
                    _resolve_freshness(
                        sample_client_config,
                        auto_ff=True,
                        warned_fetch_fail=gate.warned,
                    )
                )
            clock.advance(gate.backoff_s)

        assert results == [(False, None), (False, None)]
        skips = [r for r in caplog.records if "freshness_check_skip" in r.message]
        assert len(skips) == 1

    def test_the_default_cadence_timeline_warns_for_the_timeout_then_the_backoff(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At the real 30 s tick cadence the ticks after a timeout are backoff
        skips (their own dedup key, so one WARNING of their own), and a second
        real timeout only happens once the 120 s backoff lapses.

        Timeline (fake clock): tick 1 at t=0 times out after the 15 s grant;
        the backoff then runs until t=135. Ticks at t=45, 75 and 105 are skipped;
        the tick at t=135 is a second real timeout with tick 1's key (silent).
        """
        tick_interval = 30.0
        clock = FakeClock()
        gate = TickFetchGate(warned=set(), clock=clock)
        hung = _HungFetch(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", hung)
        timed_out = (
            f"git fetch origin {sample_client_config.default_branch} --quiet timed out"
        )

        def _warnings() -> list[str]:
            return [
                r.getMessage()
                for r in caplog.records
                if r.levelno == logging.WARNING
                and "freshness_check_skip" in r.getMessage()
            ]

        caplog.set_level(logging.WARNING, logger="cw.worktree")
        seen: list[int] = []
        fetches: list[int] = []
        for _ in range(5):
            with gate.tick():
                result = _resolve_freshness(
                    sample_client_config, auto_ff=True, warned_fetch_fail=gate.warned
                )
            assert result == (False, None)
            seen.append(len(_warnings()))
            fetches.append(len(hung.fetch_cwds))
            clock.advance(tick_interval)

        # Tick 1 warns for the timeout; tick 2 warns once for the backoff skip;
        # ticks 3-4 stay silent (same skip key); tick 5 is a second real timeout
        # (the fetch ran again) whose key was already warned, so still silent.
        assert seen == [1, 2, 2, 2, 2]
        assert fetches == [1, 1, 1, 1, 2]
        warnings = _warnings()
        assert len(warnings) == 2
        assert warnings[0].endswith(timed_out)
        assert warnings[1].endswith(BACKOFF_SKIP_REASON)
