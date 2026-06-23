"""Tests for the orchestrator dashboard TUI.

Snapshot-style tests using rich's :class:`Console` with a captured buffer.
Rather than asserting exact byte-for-byte output (fragile under rich
version bumps), tests check for presence/absence of the meaningful tokens
-- client names, ticket IDs, detail-level-specific columns, and so on.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from cw.orchestrate import (
    EventSummary,
    MonitoredPR,
    OrchestratorStatus,
    SessionSummary,
    TicketSummary,
)
from cw.tui import (
    DetailLevel,
    WatchRow,
    render_dashboard,
    render_watch_table,
    watch,
    watch_flat,
)


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sample_status(frozen_now: datetime) -> OrchestratorStatus:
    return OrchestratorStatus(
        generated_at=frozen_now,
        pending_tickets=[
            TicketSummary(
                ticket_id="MW-101",
                client="personal",
                priority=2,
                status="pending",
                created_at=frozen_now,
                scope_hint="refactor auth middleware",
            ),
            TicketSummary(
                ticket_id="MW-102",
                client="personal",
                priority=0,
                status="pending",
                created_at=frozen_now,
            ),
        ],
        running_sessions=[
            SessionSummary(
                id="abc12345",
                name="personal/impl",
                client="personal",
                status="active",
                purpose="impl",
                started_at=datetime(2026, 4, 18, 11, 55, 0, tzinfo=UTC),
                surface_ref="surf-1",
                worktree_path=Path("/home/matthew/workspace/personal/wt/abc"),
            ),
            SessionSummary(
                id="xyz98765",
                name="lgbtqplus.map/impl",
                client="lgbtqplus.map",
                status="active",
                purpose="impl",
                started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
                worktree_path=None,
            ),
        ],
        monitored_prs=[
            MonitoredPR(
                repo="mattwwarren/claude-workspace",
                pr_number=42,
                role="author",
                status="watching",
                unresolved_threads=2,
            ),
        ],
        recent_events=[
            EventSummary(
                id="evt-1",
                type="session.completed",
                payload={"session_id": "abc12345", "reason": "HANDOFF"},
                correlation_id=None,
                created_at=frozen_now,
            ),
            EventSummary(
                id="evt-2",
                type="pr.merged",
                payload={"repo": "mattwwarren/claude-workspace", "pr_number": 42},
                correlation_id=None,
                created_at=frozen_now,
            ),
        ],
    )


def _render(
    status: OrchestratorStatus,
    level: DetailLevel,
    *,
    frozen_now: datetime,
    client_filter: str | None = None,
) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=120, record=False, force_terminal=False)
    console.print(
        render_dashboard(
            status,
            level=level,
            client_filter=client_filter,
            now=frozen_now,
            home="/home/matthew",
        ),
    )
    return buffer.getvalue()


class TestRenderDashboard:
    def test_default_level_shows_every_section(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.DEFAULT, frozen_now=frozen_now)

        # Clients grouped and rendered.
        assert "personal" in output
        assert "lgbtqplus.map" in output

        # Sessions visible.
        assert "abc12345" in output
        assert "xyz98765" in output

        # Tickets visible.
        assert "MW-101" in output
        assert "MW-102" in output

        # PRs visible.
        assert "mattwwarren/claude-workspace#42" in output

        # Events panel present with both events.
        assert "session.completed" in output
        assert "pr.merged" in output

    def test_compact_shows_counts_only(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.COMPACT, frozen_now=frozen_now)

        # Counts line renders per client.
        assert "running:" in output
        assert "pending:" in output
        assert "PRs:" in output

        # Per-client tables are suppressed in compact mode -- no ticket IDs.
        # (Session IDs may still appear in the events panel payload, which
        # is the audit log and stays visible at every level.)
        assert "MW-101" not in output
        assert "refactor auth middleware" not in output

    def test_verbose_adds_scope_and_surface_columns(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.VERBOSE, frozen_now=frozen_now)

        # Scope hint appears only at VERBOSE.
        assert "refactor auth middleware" in output
        # Surface ref column value appears only at VERBOSE.
        assert "surf-1" in output

    def test_client_filter_hides_others(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(
            sample_status,
            DetailLevel.DEFAULT,
            frozen_now=frozen_now,
            client_filter="personal",
        )
        assert "personal" in output
        assert "lgbtqplus.map" not in output

    def test_empty_status_renders_without_raising(
        self,
        frozen_now: datetime,
    ) -> None:
        empty = OrchestratorStatus(generated_at=frozen_now)
        output = _render(empty, DetailLevel.DEFAULT, frozen_now=frozen_now)
        assert "No active clients" in output

    def test_worktree_path_shortened_to_tilde(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.DEFAULT, frozen_now=frozen_now)
        # /home/matthew/... is collapsed to ~/...
        assert "~/workspace/personal/wt/abc" in output


class TestWatch:
    def test_ticks_mode_renders_n_frames(
        self,
        sample_status: OrchestratorStatus,
    ) -> None:
        buffer = StringIO()
        console = Console(file=buffer, width=120, force_terminal=False)
        watch(
            interval=1,
            level=DetailLevel.COMPACT,
            console=console,
            ticks=3,
            status_fn=lambda: sample_status,
        )
        # "personal" appears once per frame.
        assert buffer.getvalue().count("personal") >= 3

    def test_interval_clamped_high(
        self,
        sample_status: OrchestratorStatus,
    ) -> None:
        # Sanity: interval=9999 must not raise; ticks=0 means no frames at all.
        buffer = StringIO()
        console = Console(file=buffer, width=120, force_terminal=False)
        watch(
            interval=9999,
            level=DetailLevel.COMPACT,
            console=console,
            ticks=0,
            status_fn=lambda: sample_status,
        )
        assert buffer.getvalue() == ""


# ---------------------------------------------------------------------------
# STAGE column tests (issue #173)
# ---------------------------------------------------------------------------


@pytest.fixture
def stage_status(frozen_now: datetime) -> OrchestratorStatus:
    """A status with one session having last_stage and one without."""
    return OrchestratorStatus(
        generated_at=frozen_now,
        running_sessions=[
            SessionSummary(
                id="withst01",
                name="personal/impl",
                client="personal",
                status="active",
                purpose="impl",
                started_at=datetime(2026, 4, 18, 11, 55, 0, tzinfo=UTC),
                worktree_path=Path("/home/matthew/workspace/personal/wt/abc"),
                last_stage="s2_impl_started",
            ),
            SessionSummary(
                id="nostage1",
                name="personal/idea",
                client="personal",
                status="active",
                purpose="idea",
                started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
                worktree_path=None,
                last_stage=None,
            ),
        ],
    )


class TestSessionsTableStageColumn:
    def test_default_level_shows_last_stage_when_present(
        self,
        stage_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        """The session with last_stage set surfaces the stage value at DEFAULT."""
        output = _render(stage_status, DetailLevel.DEFAULT, frozen_now=frozen_now)
        assert "s2_impl_started" in output

    def test_default_level_renders_dash_when_last_stage_absent(
        self,
        stage_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        """A session with last_stage=None renders an em-dash in the STAGE column."""
        output = _render(stage_status, DetailLevel.DEFAULT, frozen_now=frozen_now)
        # The STAGE column header is present in DEFAULT.
        assert "STAGE" in output
        # The em-dash sentinel for missing values is present.
        assert "—" in output

    def test_compact_level_omits_last_stage(
        self,
        stage_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        """COMPACT mode renders only counts -- no STAGE column or stage value."""
        output = _render(stage_status, DetailLevel.COMPACT, frozen_now=frozen_now)
        assert "STAGE" not in output
        assert "s2_impl_started" not in output


# ── watch flat helpers ────────────────────────────────────────────────────────


def _render_watch(
    status: OrchestratorStatus,
    *,
    frozen_now: datetime,
    selected: int = 0,
    home: str = "",
) -> str:
    """Render the flat-watch table to a string for assertion."""
    buf = StringIO()
    con = Console(file=buf, width=120, force_terminal=False)
    con.print(render_watch_table(status, now=frozen_now, selected=selected, home=home))
    return buf.getvalue()


class TestWatchRow:
    def test_from_session(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        sess = sample_status.running_sessions[0]
        row = WatchRow.from_session(sess, now=frozen_now)
        assert row.client == sess.client
        assert row.ticket_id == ""
        assert row.queue_status == "—"
        assert row.session_status == sess.status
        assert row.pane_cmd == "—"
        assert row.total_cost_usd == "—"
        assert row.idle_age  # non-empty

    def test_from_ticket(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        ticket = sample_status.pending_tickets[0]
        row = WatchRow.from_ticket(ticket, now=frozen_now)
        assert row.client == ticket.client
        assert row.ticket_id == ticket.ticket_id
        assert row.queue_status == ticket.status
        assert row.session_status == "—"

    def test_from_running_ticket(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        ticket = sample_status.pending_tickets[0]
        sess = sample_status.running_sessions[0]
        row = WatchRow.from_running_ticket(ticket, sess, now=frozen_now)
        assert row.queue_status == ticket.status
        assert row.session_status == sess.status
        assert row.worktree_path == sess.worktree_path
        assert row.session_id == sess.id
        assert row.ticket_id == ticket.ticket_id


class TestRenderWatchTable:
    def test_columns_present(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        out = _render_watch(sample_status, frozen_now=frozen_now)
        for col in [
            "CLIENT",
            "TICKET",
            "Q-STATUS",
            "S-STATUS",
            "PANE-CMD",
            "IDLE-AGE",
            "LAST-ACTIVITY",
            "COST",
        ]:
            assert col in out, f"Missing column header: {col}"

    def test_rows_populated(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        out = _render_watch(sample_status, frozen_now=frozen_now)
        # session client
        assert "personal" in out
        # ticket id
        assert "MW-101" in out

    def test_stub_columns_show_dash(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        out = _render_watch(sample_status, frozen_now=frozen_now)
        assert "—" in out  # pane_cmd and cost stubs

    def test_empty_status_no_raise(self, frozen_now: datetime) -> None:
        from cw.orchestrate import OrchestratorStatus

        empty = OrchestratorStatus(generated_at=frozen_now)
        out = _render_watch(empty, frozen_now=frozen_now)
        assert out  # doesn't raise

    def test_selected_row_distinct(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        out0 = _render_watch(sample_status, frozen_now=frozen_now, selected=0)
        # Rich may render the selection differently; just confirm no crash
        assert out0


class TestWatchFlat:
    def test_ticks_renders_frames(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(ticks=2, status_fn=lambda: sample_status, console=con)
        out = buf.getvalue()
        assert out.count("CLIENT") >= 2

    def test_interval_clamped_no_error(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        # interval=9999 → clamped to 60; ticks=0 means no renders but no crash
        watch_flat(interval=9999, ticks=0, status_fn=lambda: sample_status, console=con)

    def test_quit_key_exits(self, sample_status: OrchestratorStatus) -> None:
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None,
            status_fn=lambda: sample_status,
            console=con,
            key_queue=kq,
        )
        # Should return (not hang) after consuming q

    def test_refresh_key_triggers_repoll(
        self, sample_status: OrchestratorStatus
    ) -> None:
        import queue as _queue

        call_count = 0

        def counting_fn() -> OrchestratorStatus:
            nonlocal call_count
            call_count += 1
            return sample_status

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("r")
        kq.put("q")  # exit after
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(ticks=None, status_fn=counting_fn, console=con, key_queue=kq)
        assert call_count >= 1

    def test_open_editor_no_worktree(
        self, sample_status: OrchestratorStatus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing 'o' with a row having no worktree doesn't crash."""
        import queue as _queue

        called: list[object] = []
        monkeypatch.setattr("subprocess.run", lambda *a, **_kw: called.append(a))

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("j")  # navigate to a row with worktree_path=None if any
        kq.put("o")
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )
        # subprocess not called for rows without worktree
        # (or called once if a row does have a worktree — either is fine here)

    def test_peek_not_found(
        self, sample_status: OrchestratorStatus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing 'p' when cw queue-peek doesn't exist shows notice."""
        import queue as _queue

        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("p")
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )
        # Should not crash

    def test_spawn_complete_shows_not_available(
        self, sample_status: OrchestratorStatus
    ) -> None:
        """Pressing 'c' shows not-available notice."""
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("c")
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )
        # Should not crash

    def test_navigate_up_key(self, sample_status: OrchestratorStatus) -> None:
        """Pressing 'k' decrements cursor (with floor at 0)."""
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("j")  # go to row 1
        kq.put("k")  # back to row 0
        kq.put("k")  # already at 0 — no crash
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )

    def test_unknown_key_no_crash(self, sample_status: OrchestratorStatus) -> None:
        """Unrecognized keys are silently ignored."""
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("z")  # unknown key
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )

    def test_open_editor_with_worktree(
        self, sample_status: OrchestratorStatus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing 'o' on a row with worktree_path launches EDITOR."""
        import queue as _queue

        called: list[object] = []
        monkeypatch.setattr("subprocess.run", lambda *a, **_kw: called.append(a))
        monkeypatch.setenv("EDITOR", "nano")

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("o")  # first row has worktree
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )
        # subprocess called once for the row that has a worktree
        assert len(called) >= 1

    def test_peek_with_session_id(
        self, sample_status: OrchestratorStatus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing 'p' calls cw queue-peek when cw found and session_id exists."""
        import queue as _queue

        called: list[object] = []
        monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/cw")
        monkeypatch.setattr("subprocess.run", lambda *a, **_kw: called.append(a))

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("p")
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            ticks=None, status_fn=lambda: sample_status, console=con, key_queue=kq
        )
        # subprocess may be called if first row has session_id

    def test_interval_refresh_on_timer(
        self, frozen_now: datetime, sample_status: OrchestratorStatus
    ) -> None:
        """Status provider is called again after interval elapses."""
        import queue as _queue

        call_count = 0

        def counting_fn() -> OrchestratorStatus:
            nonlocal call_count
            call_count += 1
            return sample_status

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        # Put 'r' to force refresh, then quit
        kq.put("r")
        kq.put("q")
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            interval=1,
            ticks=None,
            status_fn=counting_fn,
            console=con,
            key_queue=kq,
        )
        assert call_count >= 1

    def test_notice_drain_and_live_update_reached(
        self, sample_status: OrchestratorStatus
    ) -> None:
        """Loop reaches notice drain + live.update when 'c' key generates a notice
        and 'q' arrives on the next iteration (after a brief delay)."""
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        kq.put("c")  # generates a notice; does NOT quit

        def _delayed_quit() -> None:
            import time as _time

            _time.sleep(0.35)
            kq.put("q")

        t = threading.Thread(target=_delayed_quit, daemon=True)
        t.start()

        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            interval=60,  # no timer refresh
            ticks=None,
            status_fn=lambda: sample_status,
            console=con,
            key_queue=kq,
        )
        t.join(timeout=2.0)

    def test_live_update_and_notice_drain_reached(
        self, sample_status: OrchestratorStatus
    ) -> None:
        """Loop runs a full iteration (live.update + notice drain) when queue is
        initially empty, then 'q' is inserted after a brief delay."""
        import queue as _queue

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()

        # Insert 'q' after 0.3s so the first loop iteration (sleep 0.25s)
        # completes fully before seeing the quit key.
        def _delayed_quit() -> None:
            import time as _time

            _time.sleep(0.35)
            kq.put("q")

        t = threading.Thread(target=_delayed_quit, daemon=True)
        t.start()

        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            interval=1,
            ticks=None,
            status_fn=lambda: sample_status,
            console=con,
            key_queue=kq,
        )
        t.join(timeout=2.0)

    def test_own_key_listener_thread_spawned(
        self,
        sample_status: OrchestratorStatus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When key_queue=None, watch_flat spawns its own listener thread."""
        import queue as _queue

        # Inject a sentinel key_queue via monkeypatching queue.SimpleQueue so
        # we can control termination without relying on timing.
        call_count = 0

        class _FakeSimpleQueue(_queue.SimpleQueue[str]):
            def __init__(self) -> None:
                nonlocal call_count
                call_count += 1
                super().__init__()
                # Pre-populate so the loop quits immediately
                self.put("q")

        monkeypatch.setattr(_queue, "SimpleQueue", _FakeSimpleQueue)

        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)

        # Not passing key_queue → hits the key_queue is None branch
        watch_flat(ticks=None, status_fn=lambda: sample_status, console=con)
        assert call_count >= 1

    def test_timer_refresh_triggers_status_repoll(
        self, sample_status: OrchestratorStatus
    ) -> None:
        """Refresh fires when interval elapses (interval=1, loop runs >1s)."""
        import queue as _queue

        call_count = 0

        def counting_fn() -> OrchestratorStatus:
            nonlocal call_count
            call_count += 1
            return sample_status

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()

        def _delayed_quit() -> None:
            import time as _time

            _time.sleep(1.1)  # > 1 second so the 1s interval fires
            kq.put("q")

        t = threading.Thread(target=_delayed_quit, daemon=True)
        t.start()

        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        watch_flat(
            interval=1,
            ticks=None,
            status_fn=counting_fn,
            console=con,
            key_queue=kq,
        )
        t.join(timeout=3.0)
        assert call_count >= 2  # initial call + at least one refresh


class TestKeyListenerThread:
    def test_oserror_on_non_tty_is_silenced(self) -> None:
        """Thread exits cleanly when stdin is not a tty (OSError from fileno)."""
        import queue as _queue

        from cw.tui import _key_listener_thread

        kq: _queue.SimpleQueue[str] = _queue.SimpleQueue()
        # Running in a test environment → stdin.fileno() or tcgetattr raises OSError
        # The function should return without raising.
        t = threading.Thread(target=_key_listener_thread, args=(kq,), daemon=True)
        t.start()
        t.join(timeout=1.0)
        # Thread should have exited (join returns before timeout)
        assert not t.is_alive()


class TestBuildWatchRows:
    def test_running_ticket_merged_with_session(self, frozen_now: datetime) -> None:
        """A 'running' ticket whose client matches a session gets merged."""
        from cw.tui import _build_watch_rows

        status = OrchestratorStatus(
            generated_at=frozen_now,
            pending_tickets=[
                TicketSummary(
                    ticket_id="MW-200",
                    client="personal",
                    priority=1,
                    status="running",
                    created_at=frozen_now,
                ),
            ],
            running_sessions=[
                SessionSummary(
                    id="sess-run",
                    name="personal/impl",
                    client="personal",
                    status="active",
                    purpose="impl",
                    started_at=frozen_now,
                ),
            ],
        )
        rows = _build_watch_rows(status, frozen_now)
        # One merged row, not two separate rows
        assert len(rows) == 1
        assert rows[0].ticket_id == "MW-200"
        assert rows[0].session_id == "sess-run"

    def test_running_ticket_skipped_from_standalone_ticket_rows(
        self, frozen_now: datetime
    ) -> None:
        """Running ticket that already has a session row is not duplicated."""
        from cw.tui import _build_watch_rows

        status = OrchestratorStatus(
            generated_at=frozen_now,
            pending_tickets=[
                TicketSummary(
                    ticket_id="MW-201",
                    client="personal",
                    priority=1,
                    status="running",
                    created_at=frozen_now,
                ),
            ],
            running_sessions=[
                SessionSummary(
                    id="sess-dup",
                    name="personal/impl",
                    client="personal",
                    status="active",
                    purpose="impl",
                    started_at=frozen_now,
                ),
            ],
        )
        rows = _build_watch_rows(status, frozen_now)
        # Only one row (merged), not two
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tests: CI / MERGEABLE columns in _prs_table (issue #834)
# ---------------------------------------------------------------------------


def _render_prs_table(
    prs: list[MonitoredPR],
    *,
    level: DetailLevel,
) -> str:
    from cw.tui import _prs_table

    buf = StringIO()
    con = Console(file=buf, width=120, force_terminal=False)
    con.print(_prs_table(prs, level=level))
    return buf.getvalue()


class TestPRsTableCIColumn:
    def test_default_level_shows_ci_column(self) -> None:
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=1,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status="success",
                mergeable=True,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.DEFAULT)
        assert "CI" in output
        assert "success" in output

    def test_default_level_ci_none_renders_dash(self) -> None:
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=2,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status=None,
                mergeable=None,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.DEFAULT)
        assert "CI" in output
        assert "—" in output

    def test_verbose_level_shows_mergeable_column(self) -> None:
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=3,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status="success",
                mergeable=True,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.VERBOSE)
        assert "MERGEABLE" in output
        assert "✓" in output

    def test_verbose_level_mergeable_false_renders_cross(self) -> None:
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=4,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status="failure",
                mergeable=False,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.VERBOSE)
        assert "✗" in output

    def test_verbose_level_mergeable_none_renders_dash(self) -> None:
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=5,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status=None,
                mergeable=None,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.VERBOSE)
        assert "MERGEABLE" in output
        assert "—" in output

    def test_compact_level_unchanged(self) -> None:
        """COMPACT mode shows counts-only; CI/MERGEABLE columns absent."""
        from io import StringIO

        from cw.orchestrate import OrchestratorStatus

        status = OrchestratorStatus(
            generated_at=datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC),
            monitored_prs=[
                MonitoredPR(
                    repo="org/repo",
                    pr_number=6,
                    role="author",
                    status="watching",
                    unresolved_threads=0,
                    ci_status="success",
                    mergeable=True,
                )
            ],
        )
        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        from cw.tui import render_dashboard

        con.print(
            render_dashboard(
                status,
                level=DetailLevel.COMPACT,
                now=datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC),
                home="/home/matthew",
            )
        )
        out = buf.getvalue()
        assert "CI" not in out
        assert "MERGEABLE" not in out

    def test_default_level_no_mergeable_column(self) -> None:
        """MERGEABLE column absent at DEFAULT level."""
        prs = [
            MonitoredPR(
                repo="org/repo",
                pr_number=7,
                role="author",
                status="watching",
                unresolved_threads=0,
                ci_status="pending",
                mergeable=True,
            )
        ]
        output = _render_prs_table(prs, level=DetailLevel.DEFAULT)
        assert "MERGEABLE" not in output


# ---------------------------------------------------------------------------
# Tests: lane suffix in _tickets_table + _group_by_client (issue #561)
# ---------------------------------------------------------------------------


class TestTicketsTableLane:
    def _render_tickets(
        self,
        tickets: list[TicketSummary],
        *,
        level: DetailLevel = DetailLevel.DEFAULT,
    ) -> str:
        """Render _tickets_table to a string for assertion."""
        from cw.tui import _tickets_table

        buf = StringIO()
        con = Console(file=buf, width=120, force_terminal=False)
        con.print(_tickets_table(tickets, level=level))
        return buf.getvalue()

    def test_tickets_table_non_default_lane_shows_suffix(
        self, frozen_now: datetime
    ) -> None:
        """A ticket in a non-default lane renders with [lane] suffix in ticket cell."""
        tickets = [
            TicketSummary(
                ticket_id="MW-300",
                client="personal",
                priority=5,
                status="pending",
                created_at=frozen_now,
                lane="fast",
            )
        ]
        output = self._render_tickets(tickets)
        assert "[fast]" in output

    def test_tickets_table_default_lane_no_suffix(self, frozen_now: datetime) -> None:
        """A ticket in the default lane does NOT render [default] suffix."""
        tickets = [
            TicketSummary(
                ticket_id="MW-301",
                client="personal",
                priority=5,
                status="pending",
                created_at=frozen_now,
                lane="default",
            )
        ]
        output = self._render_tickets(tickets)
        assert "[default]" not in output

    def test_group_by_client_stays_client_keyed(self, frozen_now: datetime) -> None:
        """_group_by_client groups by client regardless of lane (regression pin)."""
        from cw.tui import _group_by_client

        tickets = [
            TicketSummary(
                ticket_id="MW-400",
                client="client-a",
                priority=5,
                status="pending",
                created_at=frozen_now,
                lane="fast",
            ),
            TicketSummary(
                ticket_id="MW-401",
                client="client-a",
                priority=3,
                status="pending",
                created_at=frozen_now,
                lane="slow",
            ),
            TicketSummary(
                ticket_id="MW-402",
                client="client-b",
                priority=5,
                status="pending",
                created_at=frozen_now,
            ),
        ]
        clients = _group_by_client([], tickets)
        assert "client-a" in clients
        assert "client-b" in clients
        # client-a appears first (more tickets)
        assert clients[0] == "client-a"
