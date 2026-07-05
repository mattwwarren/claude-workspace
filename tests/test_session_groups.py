"""Tests for src/cw/session_groups.py — pure non-display grouping helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cw.orchestrate import SessionSummary, TicketSummary
from cw.session_groups import (
    group_by_client,
    sessions_by_client,
    worktree_contention,
)

NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)


def _session(
    session_id: str,
    client: str,
    *,
    worktree_path: Path | None = None,
) -> SessionSummary:
    return SessionSummary(
        id=session_id,
        name=f"{client}/impl",
        client=client,
        status="active",
        purpose="impl",
        started_at=NOW,
        worktree_path=worktree_path,
    )


def _ticket(ticket_id: str, client: str) -> TicketSummary:
    return TicketSummary(
        ticket_id=ticket_id,
        client=client,
        priority=1,
        status="pending",
        created_at=NOW,
    )


class TestGroupByClient:
    def test_stays_client_keyed_regardless_of_lane(self) -> None:
        """group_by_client groups by client regardless of lane (regression pin)."""
        tickets = [
            TicketSummary(
                ticket_id="MW-400",
                client="client-a",
                priority=5,
                status="pending",
                created_at=NOW,
                lane="fast",
            ),
            TicketSummary(
                ticket_id="MW-401",
                client="client-a",
                priority=3,
                status="pending",
                created_at=NOW,
                lane="slow",
            ),
            TicketSummary(
                ticket_id="MW-402",
                client="client-b",
                priority=5,
                status="pending",
                created_at=NOW,
            ),
        ]
        clients = group_by_client([], tickets)
        assert "client-a" in clients
        assert "client-b" in clients
        # client-a appears first (more tickets)
        assert clients[0] == "client-a"

    def test_activity_ordering_with_alpha_tiebreak(self) -> None:
        """Most-active client first; equal counts break alphabetically."""
        sessions = [
            _session("s1", "zeta"),
            _session("s2", "alpha"),
            _session("s3", "alpha"),
        ]
        # alpha has 2, zeta has 1 -> alpha first
        assert group_by_client(sessions, []) == ["alpha", "zeta"]

    def test_sessions_and_tickets_both_counted(self) -> None:
        sessions = [_session("s1", "acme")]
        tickets = [_ticket("MW-1", "acme"), _ticket("MW-2", "beta")]
        # acme: 1 session + 1 ticket = 2; beta: 1 -> acme first
        assert group_by_client(sessions, tickets) == ["acme", "beta"]

    def test_empty_inputs(self) -> None:
        assert group_by_client([], []) == []


class TestWorktreeContention:
    def test_shared_path_counts_two(self) -> None:
        shared = Path("/home/u/wt/shared")
        sessions = [
            _session("s1", "acme", worktree_path=shared),
            _session("s2", "acme", worktree_path=shared),
        ]
        counts = worktree_contention(sessions)
        assert counts[str(shared)] == 2

    def test_distinct_paths_count_one(self) -> None:
        sessions = [
            _session("s1", "acme", worktree_path=Path("/home/u/wt/a")),
            _session("s2", "acme", worktree_path=Path("/home/u/wt/b")),
        ]
        counts = worktree_contention(sessions)
        assert counts[str(Path("/home/u/wt/a"))] == 1
        assert counts[str(Path("/home/u/wt/b"))] == 1

    def test_none_worktree_excluded(self) -> None:
        sessions = [
            _session("s1", "acme", worktree_path=None),
            _session("s2", "acme", worktree_path=Path("/home/u/wt/a")),
        ]
        counts = worktree_contention(sessions)
        assert str(None) not in counts
        assert len(counts) == 1

    def test_empty_returns_empty_dict(self) -> None:
        assert worktree_contention([]) == {}


class TestSessionsByClient:
    def test_sessions_map_to_correct_buckets(self) -> None:
        sessions = [
            _session("s1", "acme"),
            _session("s2", "acme"),
            _session("s3", "beta"),
        ]
        grouped = sessions_by_client(sessions)
        assert {s.id for s in grouped["acme"]} == {"s1", "s2"}
        assert {s.id for s in grouped["beta"]} == {"s3"}

    def test_client_with_no_sessions_absent(self) -> None:
        grouped = sessions_by_client([_session("s1", "acme")])
        assert "beta" not in grouped

    def test_empty_returns_empty_dict(self) -> None:
        assert sessions_by_client([]) == {}
