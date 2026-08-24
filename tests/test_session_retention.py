"""Tests for cw.session_retention — sessions.json retention/archival (#1983)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.config import load_state, save_state, sessions_lock, state_dir
from cw.dev_queue import save_dev_queue
from cw.exceptions import SessionsLockReentryError
from cw.models import DevQueueStore, Session, SessionStatus
from cw.session_retention import (
    _SESSION_RETENTION_DAYS,
    find_session_by_id,
    prune_sessions,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(days=_SESSION_RETENTION_DAYS + 10)
_RECENT = _NOW - timedelta(days=1)


def _seed_sessions(*sessions: Session) -> None:
    """Persist *sessions* into sessions.json in a single save_state call."""
    state = load_state()
    state.sessions.extend(sessions)
    save_state(state)


def _archive_files() -> list[Path]:
    return sorted(state_dir().glob("sessions.*.json"))


def _archived_ids() -> list[str]:
    ids: list[str] = []
    for path in _archive_files():
        ids.extend(
            json.loads(line)["id"]
            for line in path.read_text().splitlines()
            if line.strip()
        )
    return ids


class TestPruneSessions:
    def test_prune_sessions_archives_terminal_sessions_older_than_cutoff(
        self, tmp_config_dir: Path
    ) -> None:
        """Old terminal sessions archive; recent terminal and old live stay."""
        old_done = _make_daemon_session(
            id="old00001",
            name="client-a/auto-dev/T-old",
            status=SessionStatus.COMPLETED,
            completed_at=_OLD,
            started_at=_OLD,
        )
        recent_done = _make_daemon_session(
            id="new00001",
            name="client-a/auto-dev/T-new",
            status=SessionStatus.COMPLETED,
            completed_at=_RECENT,
            started_at=_RECENT,
        )
        old_active = _make_daemon_session(
            id="act00001",
            name="client-a/auto-dev/T-act",
            status=SessionStatus.ACTIVE,
            started_at=_OLD,
        )
        _seed_sessions(old_done, recent_done, old_active)

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 1
        assert result.deleted_count == 0
        assert result.kept_count == 2
        remaining = {s.id for s in load_state().sessions}
        assert remaining == {"new00001", "act00001"}
        assert _archived_ids() == ["old00001"]
        assert result.archive_path is not None

    def test_prune_sessions_timed_out_uses_completed_at_or_started_at_fallback(
        self, tmp_config_dir: Path
    ) -> None:
        """A TIMED_OUT session with completed_at=None ages off started_at."""
        session = _make_daemon_session(
            id="to000001",
            name="client-a/auto-dev/T-to",
            status=SessionStatus.TIMED_OUT,
            completed_at=None,
            started_at=_OLD,
        )
        _seed_sessions(session)

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 1
        assert load_state().sessions == []
        assert _archived_ids() == ["to000001"]

    def test_prune_sessions_default_cutoff_uses_retention_constant(
        self, tmp_config_dir: Path
    ) -> None:
        """before=None falls back to now - _SESSION_RETENTION_DAYS exactly."""
        just_inside = _make_daemon_session(
            id="inside01",
            name="client-a/auto-dev/T-in",
            status=SessionStatus.COMPLETED,
            completed_at=_NOW - timedelta(days=_SESSION_RETENTION_DAYS - 1),
            started_at=_OLD,
        )
        just_outside = _make_daemon_session(
            id="outsid01",
            name="client-a/auto-dev/T-out",
            status=SessionStatus.COMPLETED,
            completed_at=_NOW - timedelta(days=_SESSION_RETENTION_DAYS + 1),
            started_at=_OLD,
        )
        _seed_sessions(just_inside, just_outside)

        with freeze_time(_NOW):
            result = prune_sessions(before=None)

        assert result.archived_count == 1
        assert [s.id for s in load_state().sessions] == ["inside01"]
        assert _archived_ids() == ["outsid01"]

    def test_prune_sessions_delete_flag_discards_without_archiving(
        self, tmp_config_dir: Path
    ) -> None:
        """archive=False drops the sessions without writing an archive file."""
        _seed_sessions(
            _make_daemon_session(
                id="del00001",
                name="client-a/auto-dev/T-del",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )

        with freeze_time(_NOW):
            result = prune_sessions(archive=False)

        assert result.archived_count == 0
        assert result.deleted_count == 1
        assert result.archive_path is None
        assert _archive_files() == []
        assert load_state().sessions == []

    def test_prune_sessions_archive_appends_across_multiple_runs_same_day(
        self, tmp_config_dir: Path
    ) -> None:
        """Two prunes on the same date append into one archive file."""
        with freeze_time(_NOW):
            _seed_sessions(
                _make_daemon_session(
                    id="batch001",
                    name="client-a/auto-dev/T-b1",
                    status=SessionStatus.COMPLETED,
                    completed_at=_OLD,
                    started_at=_OLD,
                )
            )
            prune_sessions()
            _seed_sessions(
                _make_daemon_session(
                    id="batch002",
                    name="client-a/auto-dev/T-b2",
                    status=SessionStatus.COMPLETED,
                    completed_at=_OLD,
                    started_at=_OLD,
                )
            )
            prune_sessions()

        assert len(_archive_files()) == 1
        assert _archived_ids() == ["batch001", "batch002"]

    def test_prune_sessions_empty_when_nothing_to_prune(
        self, tmp_config_dir: Path
    ) -> None:
        """An empty state prunes nothing and writes no archive."""
        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert result.deleted_count == 0
        assert result.kept_count == 0
        assert result.archive_path is None
        assert _archive_files() == []

    def test_prune_sessions_holds_sessions_lock(self, tmp_config_dir: Path) -> None:
        """prune_sessions takes sessions_lock itself, so nesting is refused."""
        with sessions_lock(), pytest.raises(SessionsLockReentryError):
            prune_sessions()

    def test_prune_sessions_non_terminal_sessions_never_pruned_regardless_of_age(
        self, tmp_config_dir: Path
    ) -> None:
        """ACTIVE/IDLE/BACKGROUNDED sessions survive any cutoff."""
        _seed_sessions(
            _make_daemon_session(
                id="live0001",
                name="client-a/auto-dev/T-l1",
                status=SessionStatus.ACTIVE,
                started_at=_OLD,
            ),
            _make_daemon_session(
                id="live0002",
                name="client-a/auto-dev/T-l2",
                status=SessionStatus.IDLE,
                started_at=_OLD,
            ),
            _make_daemon_session(
                id="live0003",
                name="client-a/auto-dev/T-l3",
                status=SessionStatus.BACKGROUNDED,
                started_at=_OLD,
            ),
        )

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert result.kept_count == 3
        assert len(load_state().sessions) == 3

    def test_prune_sessions_exempts_terminal_session_with_live_dev_queue_row(
        self, tmp_config_dir: Path
    ) -> None:
        """A live dev-queue row pins its ticket's terminal sessions in the hot file.

        Load-bearing: spawn._collect_prior_attempts_summary never reads
        archives, so this exemption is the entire completeness guarantee for
        retry history.
        """
        from cw import spawn

        client = "client-a"
        ticket_id = "T-live"
        _seed_sessions(
            _make_daemon_session(
                id="pinned01",
                name=f"{client}/auto-dev/{ticket_id}",
                client=client,
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
                last_result={"status": "blocked", "stage_reached": "implement"},
            )
        )
        save_dev_queue(
            DevQueueStore(tasks=[_make_ticket_task(ticket_id=ticket_id, client=client)])
        )

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert result.kept_count == 1
        assert [s.id for s in load_state().sessions] == ["pinned01"]
        summaries = spawn._collect_prior_attempts_summary(ticket_id, client=client)
        assert len(summaries) == 1
        assert summaries[0]["status"] == "blocked"

    def test_prune_sessions_prunes_when_dev_queue_row_is_for_other_ticket(
        self, tmp_config_dir: Path
    ) -> None:
        """The exemption is keyed on (client, ticket_id), not queue non-emptiness."""
        _seed_sessions(
            _make_daemon_session(
                id="unpin001",
                name="client-a/auto-dev/T-gone",
                client="client-a",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[_make_ticket_task(ticket_id="T-other", client="client-a")]
            )
        )

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 1
        assert load_state().sessions == []


class TestFindSessionById:
    def test_find_session_by_id_hits_hot_file_first(self, tmp_config_dir: Path) -> None:
        """A session still in sessions.json resolves without touching archives."""
        _seed_sessions(
            _make_daemon_session(id="hot00001", name="client-a/auto-dev/T-hot")
        )
        found = find_session_by_id("hot0")
        assert found is not None
        assert found.id == "hot00001"

    def test_find_session_by_id_matches_claude_session_id_prefix(
        self, tmp_config_dir: Path
    ) -> None:
        """claude_session_id prefixes resolve too, same as the old _resolve_session."""
        _seed_sessions(
            _make_daemon_session(
                id="cs000001",
                name="client-a/auto-dev/T-cs",
                claude_session_id="deadbeefcafe",
            )
        )
        found = find_session_by_id("deadbeef")
        assert found is not None
        assert found.id == "cs000001"

    def test_find_session_by_id_scans_newest_archive_first(
        self, tmp_config_dir: Path
    ) -> None:
        """An ambiguous prefix resolves against the newest archive, not the oldest."""
        self._seed_two_archives()
        assert len(_archive_files()) == 2

        found = find_session_by_id("arch")
        assert found is not None
        assert found.id == "arch0002"

    def test_find_session_by_id_still_reaches_older_archives(
        self, tmp_config_dir: Path
    ) -> None:
        """A session only present in the oldest archive is still resolvable."""
        self._seed_two_archives()

        found = find_session_by_id("arch0001")
        assert found is not None
        assert found.id == "arch0001"

    @staticmethod
    def _seed_two_archives() -> None:
        """Produce two dated archive files, oldest holding arch0001."""
        day_one = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        day_two = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
        for stamp, session_id in ((day_one, "arch0001"), (day_two, "arch0002")):
            with freeze_time(stamp):
                _seed_sessions(
                    _make_daemon_session(
                        id=session_id,
                        name=f"client-a/auto-dev/T-{session_id}",
                        status=SessionStatus.COMPLETED,
                        completed_at=stamp - timedelta(days=365),
                        started_at=stamp - timedelta(days=365),
                    )
                )
                prune_sessions()

    def test_find_session_by_id_returns_none_when_absent_everywhere(
        self, tmp_config_dir: Path
    ) -> None:
        """No hot match and no archive match returns None."""
        _seed_sessions(
            _make_daemon_session(
                id="gone0001",
                name="client-a/auto-dev/T-gone",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        with freeze_time(_NOW):
            prune_sessions()
        assert _archive_files() != []
        assert find_session_by_id("zzzz") is None


class TestCliSessionPrune:
    def test_cli_session_prune_basic(self, tmp_config_dir: Path) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="cli00001",
                name="client-a/auto-dev/T-cli",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        runner = CliRunner()
        with freeze_time(_NOW):
            result = runner.invoke(main, ["session", "prune"])
        assert result.exit_code == 0, result.output
        assert "Archived 1" in result.output
        assert load_state().sessions == []

    def test_cli_session_prune_json_output(self, tmp_config_dir: Path) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="cli00002",
                name="client-a/auto-dev/T-cli2",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        runner = CliRunner()
        with freeze_time(_NOW):
            result = runner.invoke(main, ["session", "prune", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["archived_count"] == 1
        assert payload["kept_count"] == 0
        assert payload["archive_path"] is not None

    def test_cli_session_prune_delete_flag(self, tmp_config_dir: Path) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="cli00003",
                name="client-a/auto-dev/T-cli3",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        runner = CliRunner()
        with freeze_time(_NOW):
            result = runner.invoke(main, ["session", "prune", "--delete", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["deleted_count"] == 1
        assert payload["archive_path"] is None
        assert _archive_files() == []

    def test_cli_session_prune_explicit_before(self, tmp_config_dir: Path) -> None:
        """An explicit --before overrides the default retention window."""
        _seed_sessions(
            _make_daemon_session(
                id="cli00004",
                name="client-a/auto-dev/T-cli4",
                status=SessionStatus.COMPLETED,
                completed_at=_RECENT,
                started_at=_RECENT,
            )
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "prune", "--before", _NOW.isoformat(), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["archived_count"] == 1

    def test_cli_session_prune_before_naive_timestamp_warns(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="cli00005",
                name="client-a/auto-dev/T-cli5",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "prune", "--before", "2026-06-01T12:00:00"]
        )
        assert result.exit_code == 0, result.output
        assert "no timezone; assuming UTC" in result.output

    def test_cli_session_prune_invalid_before_errors(
        self, tmp_config_dir: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["session", "prune", "--before", "not-a-date"])
        assert result.exit_code != 0
        assert "Cannot parse --before" in result.output
