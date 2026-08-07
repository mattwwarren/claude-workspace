"""Unit tests for ``cw.statusline.render_work_segment`` (#1644).

Exercises the R2 three-step resolution ladder directly (no CLI/subprocess
layer): (1) the session's focused client/lane from ``focus.json``, (2) a
cwd-based aggregate across a client's lanes, (3) the empty string. Also pins
R3's never-raise contract and R5's exact output shape.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import yaml

from cw.config import (
    _save_concurrency_overrides,
    clients_file,
    focus_file,
)
from cw.dev_queue import save_dev_queue
from cw.focus import set_focus
from cw.models import (
    ConcurrencyOverrides,
    DevQueueStore,
    LaneConcurrencyOverride,
    PrState,
    QueueItemStatus,
)
from cw.statusline import render_work_segment, resolve_client_for_cwd
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_SESSION = "sess-statusline-1"
# Wall-clock ceiling at R1's real budget: the 300ms statusline debounce.
#
# A CPU-time assertion (process_time) lived here briefly and was removed: it
# was introduced to dodge scheduler contention, but coverage instrumentation
# burns real CPU, so process_time does not escape it. Two successive budgets
# (0.05s wall, then 0.15s CPU) were both set against a fast local baseline and
# both failed on CI under --cov (0.1938s, then 0.2367s / 0.1740s). A guard that
# has to be widened every time it fires is measuring the runner, not the code.
#
# 300ms is the number the design actually specifies, and it catches the
# regression class that matters: a subprocess / network / gh / git call, which
# R1 forbids and which would blow far past the debounce budget.
_WALL_CLOCK_BUDGET_SECONDS = 0.3


def _write_clients(tmp_path: Path) -> Path:
    """Write clients.yaml with ``client-a`` (lanes impl/debt) and ``client-b``.

    Returns ``client-a``'s workspace path so cwd-based tests can point at it.
    """
    ws_a = tmp_path / "ws" / "client-a"
    ws_b = tmp_path / "ws" / "client-b"
    ws_a.mkdir(parents=True, exist_ok=True)
    ws_b.mkdir(parents=True, exist_ok=True)
    lanes = [{"name": "impl"}, {"name": "debt"}]
    clients_file().write_text(
        yaml.safe_dump(
            {
                "clients": {
                    "client-a": {
                        "workspace_path": str(ws_a),
                        "lanes": lanes,
                    },
                    "client-b": {"workspace_path": str(ws_b)},
                }
            }
        )
    )
    return ws_a


def _attention_task(**overrides: object) -> TicketTask:
    """A task whose hydrated PR state carries a non-null attention_state."""
    kwargs: dict[str, object] = {"pr_state": PrState(attention_state="ci_failed")}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _seed_queue(*tasks: TicketTask) -> None:
    save_dev_queue(DevQueueStore(tasks=list(tasks)))


def _pause_lane(key: str) -> None:
    _save_concurrency_overrides(
        ConcurrencyOverrides(lanes={key: LaneConcurrencyOverride(paused=True)})
    )


class TestStepOneFocused:
    def test_lane_focused_counts(self, tmp_config_dir: Path) -> None:
        """R5's first example line, byte-for-byte."""
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-2",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-3",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
            _attention_task(
                ticket_id="T-4",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.COMPLETED,
            ),
        )
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 2▶ 1⧗ !1"

    def test_lane_focus_excludes_other_lanes(self, tmp_config_dir: Path) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-2",
                client="client-a",
                lane="debt",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-3",
                client="client-b",
                status=QueueItemStatus.RUNNING,
            ),
        )
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 1▶ 0⧗"

    def test_client_only_focus_aggregates_across_lanes(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-2",
                client="client-a",
                lane="debt",
                status=QueueItemStatus.PENDING,
            ),
            _make_ticket_task(
                ticket_id="T-3",
                client="client-b",
                status=QueueItemStatus.RUNNING,
            ),
        )
        set_focus(_SESSION, "client-a")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a 1▶ 1⧗"

    def test_circuit_paused_lane_with_pending_work(self, tmp_config_dir: Path) -> None:
        """#1630 shape — R5's second example line, byte-for-byte."""
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
        )
        _pause_lane("client-a/impl")
        set_focus(_SESSION, "client-a", "impl")

        assert (
            render_work_segment(_SESSION, tmp_config_dir)
            == "client-a/impl PAUSED 0▶ 1⧗"
        )

    def test_paused_marker_is_independent_of_pending_count(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue()
        _pause_lane("client-a/impl")
        set_focus(_SESSION, "client-a", "impl")

        assert (
            render_work_segment(_SESSION, tmp_config_dir)
            == "client-a/impl PAUSED 0▶ 0⧗"
        )

    def test_client_only_focus_never_shows_paused(self, tmp_config_dir: Path) -> None:
        """Adopted Assumption 3: the aggregate view carries no pause detail."""
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
        )
        _pause_lane("client-a/impl")
        set_focus(_SESSION, "client-a")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a 0▶ 1⧗"

    def test_zero_counts_suppress_the_attention_suffix(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue()
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 0▶ 0⧗"

    def test_unhydrated_pr_state_renders_no_attention(
        self, tmp_config_dir: Path
    ) -> None:
        """Documented ``!N`` staleness window: ``pr_state`` is None until the
        async ``cw.pr_hydrate`` pass runs, so a fresh task renders ``!0`` (i.e.
        no suffix) even if it would count once hydrated. Pinned, not fixed."""
        _write_clients(tmp_config_dir)
        fresh = _make_ticket_task(
            ticket_id="T-1",
            client="client-a",
            lane="impl",
            status=QueueItemStatus.RUNNING,
        )
        assert fresh.pr_state is None
        _seed_queue(fresh)
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 1▶ 0⧗"


class TestStepOneFallsThrough:
    def test_focus_on_removed_client_falls_through_to_cwd(
        self, tmp_config_dir: Path
    ) -> None:
        """Adopted Assumption 6: config drift under R6's no-pruning policy is
        treated like R3's unknown-session case — fall through to step 2."""
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
        )
        set_focus(_SESSION, "client-gone", "impl")

        assert render_work_segment(_SESSION, ws_a) == "client-a 1▶ 0⧗"

    def test_focus_on_removed_lane_falls_through_to_cwd(
        self, tmp_config_dir: Path
    ) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
        )
        set_focus(_SESSION, "client-a", "lane-gone")

        assert render_work_segment(_SESSION, ws_a) == "client-a 0▶ 1⧗"

    def test_absent_focus_file_falls_through(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )
        assert not focus_file().exists()

        assert render_work_segment(_SESSION, ws_a) == "client-a 1▶ 0⧗"

    def test_malformed_focus_file_falls_through(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )
        focus_file().parent.mkdir(parents=True, exist_ok=True)
        focus_file().write_text("}}}not json")

        assert render_work_segment(_SESSION, ws_a) == "client-a 1▶ 0⧗"

    def test_no_session_id_falls_through(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )
        set_focus(_SESSION, "client-b")

        assert render_work_segment(None, ws_a) == "client-a 1▶ 0⧗"


class TestStepTwoCwd:
    def test_cwd_under_workspace_path(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
            _make_ticket_task(
                ticket_id="T-2",
                client="client-a",
                lane="debt",
                status=QueueItemStatus.PENDING,
            ),
            _attention_task(ticket_id="T-3", client="client-a", lane="debt"),
        )

        nested = ws_a / "src" / "deep"
        nested.mkdir(parents=True)

        assert render_work_segment(None, nested) == "client-a 1▶ 2⧗ !1"

    def test_cwd_under_worktree_base(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        # Default (non-hashed) worktree base: <parent>/.worktrees/<name>
        wt = ws_a.parent / ".worktrees" / ws_a.name / "dev-1"
        wt.mkdir(parents=True)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )

        assert render_work_segment(None, wt) == "client-a 1▶ 0⧗"

    def test_cwd_aggregate_never_shows_paused(self, tmp_config_dir: Path) -> None:
        ws_a = _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1",
                client="client-a",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
        )
        _pause_lane("client-a/impl")

        assert render_work_segment(None, ws_a) == "client-a 0▶ 1⧗"

    def test_cwd_under_a_worktree_client_repo_path(self, tmp_config_dir: Path) -> None:
        """A worktree-mode client is also findable through its backing repo."""
        repo = tmp_config_dir / "repos" / "client-c"
        repo.mkdir(parents=True)
        clients_file().write_text(
            yaml.safe_dump(
                {
                    "clients": {
                        "client-c": {
                            "workspace_path": str(tmp_config_dir / "ws" / "client-c"),
                            "repo_path": str(repo),
                            "branch": "main",
                        }
                    }
                }
            )
        )
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-c", status=QueueItemStatus.RUNNING
            ),
        )

        assert render_work_segment(None, repo / "src") == "client-c 1▶ 0⧗"

    def test_resolve_client_for_cwd_matches_workspace(
        self, tmp_config_dir: Path
    ) -> None:
        ws_a = _write_clients(tmp_config_dir)

        assert resolve_client_for_cwd(ws_a / "sub") == "client-a"

    def test_resolve_client_for_cwd_unmapped_returns_none(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients(tmp_config_dir)

        assert resolve_client_for_cwd(tmp_config_dir / "elsewhere") is None


class TestStepThreeEmpty:
    def test_no_focus_and_unmapped_cwd(self, tmp_config_dir: Path) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )

        assert render_work_segment(None, tmp_config_dir / "elsewhere") == ""

    def test_other_sessions_focus_does_not_leak(self, tmp_config_dir: Path) -> None:
        _write_clients(tmp_config_dir)
        _seed_queue(
            _make_ticket_task(
                ticket_id="T-1", client="client-a", status=QueueItemStatus.RUNNING
            ),
        )
        set_focus("some-other-session", "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir / "elsewhere") == ""


class TestNeverRaises:
    def test_malformed_dev_queue_degrades(self, tmp_config_dir: Path) -> None:
        """R3: the shared ``load_dev_queue`` raises on corruption by design;
        the render path wraps it locally rather than changing that contract."""
        from cw.config import dev_queue_file

        _write_clients(tmp_config_dir)
        dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
        dev_queue_file().write_text("{not json")
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 0▶ 0⧗"

    def test_unexpected_error_degrades_to_empty(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer guard mirrors ``cw guard-cwd``'s must-never-crash idiom."""

        def _boom() -> dict[str, object]:
            msg = "clients.yaml exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.statusline.load_clients", _boom)

        assert render_work_segment(_SESSION, tmp_config_dir) == ""

    def test_malformed_concurrency_overrides_degrades(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import concurrency_override_file

        _write_clients(tmp_config_dir)
        _seed_queue()
        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        concurrency_override_file().write_text("nope")
        set_focus(_SESSION, "client-a", "impl")

        assert render_work_segment(_SESSION, tmp_config_dir) == "client-a/impl 0▶ 0⧗"


class TestPerformance:
    def test_large_fixture_renders_well_inside_budget(
        self, tmp_config_dir: Path
    ) -> None:
        """~17 clients x ~10 lanes, several hundred tasks (R1's speed premise)."""
        n_clients = 17
        n_lanes = 10
        base = tmp_config_dir / "perf"
        base.mkdir(parents=True, exist_ok=True)
        lanes = [{"name": f"lane-{i}"} for i in range(n_lanes)]
        clients_file().write_text(
            yaml.safe_dump(
                {
                    "clients": {
                        f"client-{c}": {
                            "workspace_path": str(base / f"client-{c}"),
                            "lanes": lanes,
                        }
                        for c in range(n_clients)
                    }
                }
            )
        )
        tasks = [
            _make_ticket_task(
                ticket_id=f"T-{c}-{i}",
                client=f"client-{c}",
                lane=f"lane-{i % n_lanes}",
                status=QueueItemStatus.RUNNING if i % 2 else QueueItemStatus.PENDING,
            )
            for c in range(n_clients)
            for i in range(20)
        ]
        _seed_queue(*tasks)
        set_focus(_SESSION, "client-3", "lane-4")

        wall_started = time.perf_counter()
        segment = render_work_segment(_SESSION, tmp_config_dir)
        wall_elapsed = time.perf_counter() - wall_started

        assert segment.startswith("client-3/lane-4 ")
        assert wall_elapsed < _WALL_CLOCK_BUDGET_SECONDS, (
            f"wall clock {wall_elapsed:.4f}s"
        )

    def test_cwd_fallback_path_renders_well_inside_budget(
        self, tmp_config_dir: Path
    ) -> None:
        """Same fixture, but no focus set — forces resolve_client_for_cwd's
        step-2 walk (the more expensive path pre-`cw focus set` adoption,
        and the one that a redundant clients.yaml re-parse would hit)."""
        n_clients = 17
        n_lanes = 10
        base = tmp_config_dir / "perf"
        base.mkdir(parents=True, exist_ok=True)
        lanes = [{"name": f"lane-{i}"} for i in range(n_lanes)]
        clients_file().write_text(
            yaml.safe_dump(
                {
                    "clients": {
                        f"client-{c}": {
                            "workspace_path": str(base / f"client-{c}"),
                            "lanes": lanes,
                        }
                        for c in range(n_clients)
                    }
                }
            )
        )
        tasks = [
            _make_ticket_task(
                ticket_id=f"T-{c}-{i}",
                client=f"client-{c}",
                lane=f"lane-{i % n_lanes}",
                status=QueueItemStatus.RUNNING if i % 2 else QueueItemStatus.PENDING,
            )
            for c in range(n_clients)
            for i in range(20)
        ]
        _seed_queue(*tasks)

        wall_started = time.perf_counter()
        segment = render_work_segment(None, base / "client-3")
        wall_elapsed = time.perf_counter() - wall_started

        assert segment.startswith("client-3 ")
        assert wall_elapsed < _WALL_CLOCK_BUDGET_SECONDS, (
            f"wall clock {wall_elapsed:.4f}s"
        )
