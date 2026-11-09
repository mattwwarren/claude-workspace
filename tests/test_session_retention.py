"""Tests for cw.session_retention — sessions.json retention/archival (#1983)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.config import load_state, sessions_lock, state_dir, state_file
from cw.dev_queue import save_dev_queue
from cw.exceptions import CwLockReentrancyError
from cw.models import CwState, DevQueueStore, QueueItemStatus, Session, SessionStatus
from cw.session_retention import (
    _SESSION_RETENTION_DAYS,
    _append_to_archive,
    find_session_by_id,
    prune_sessions,
)
from tests.conftest import _make_daemon_session, _make_ticket_task, _seed_sessions

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(days=_SESSION_RETENTION_DAYS + 10)
_RECENT = _NOW - timedelta(days=1)


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

    @pytest.mark.lock_violations_expected("reentry")
    def test_prune_sessions_holds_sessions_lock(self, tmp_config_dir: Path) -> None:
        """prune_sessions takes sessions_lock itself, so nesting is refused."""
        with sessions_lock(), pytest.raises(CwLockReentrancyError):
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

    def test_prune_sessions_fails_closed_when_dev_queue_load_raises(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A load_dev_queue() failure aborts before any mutation (#1983 review F2).

        The dev-queue exemption's fail-closed guarantee rests entirely on
        ``load_dev_queue()`` being called, inside the lock, before
        ``save_state()``. This locks that statement order down: if
        ``load_dev_queue`` raises, ``sessions.json`` must be left byte-for-byte
        untouched rather than silently falling back to "nothing looks live"
        and archiving everything.
        """
        seeded = _make_daemon_session(
            id="failclo1",
            name="client-a/auto-dev/T-failclosed",
            status=SessionStatus.COMPLETED,
            completed_at=_OLD,
            started_at=_OLD,
        )
        _seed_sessions(seeded)
        before = load_state().sessions

        def _raise() -> DevQueueStore:
            msg = "dev queue store unreadable"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.session_retention.load_dev_queue", _raise)

        with freeze_time(_NOW), pytest.raises(RuntimeError, match="unreadable"):
            prune_sessions()

        assert load_state().sessions == before
        assert _archive_files() == []


def _old_done(session_id: str, age_days: int, *, client: str = "client-a") -> Session:
    """An old COMPLETED daemon session whose age anchor is *age_days* before now."""
    stamp = _NOW - timedelta(days=age_days)
    return _make_daemon_session(
        id=session_id,
        name=f"{client}/auto-dev/T-{session_id}",
        client=client,
        status=SessionStatus.COMPLETED,
        completed_at=stamp,
        started_at=stamp,
    )


class TestPruneSessionsBounded:
    """#2965: the ``max_sessions`` cap, id-based exemption and result fields."""

    def test_prune_sessions_max_sessions_caps_and_takes_oldest_first(
        self, tmp_config_dir: Path
    ) -> None:
        ages = {"ses00001": 40, "ses00002": 90, "ses00003": 50, "ses00004": 70}
        sessions = [_old_done(sid, age) for sid, age in ages.items()]
        keep_live = _make_daemon_session(
            id="live0001",
            name="client-a/auto-dev/T-live",
            status=SessionStatus.ACTIVE,
            started_at=_OLD,
        )
        _seed_sessions(*sessions, keep_live)

        with freeze_time(_NOW):
            result = prune_sessions(max_sessions=2)

        assert result.archived_count == 2
        assert result.capped is True
        assert sorted(_archived_ids()) == ["ses00002", "ses00004"]
        assert [s.id for s in load_state().sessions] == [
            "ses00001",
            "ses00003",
            "live0001",
        ]
        assert result.kept_count == 3

    def test_prune_sessions_max_sessions_not_reached_is_not_capped(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(_old_done("ses00001", 40), _old_done("ses00002", 50))

        with freeze_time(_NOW):
            result = prune_sessions(max_sessions=2)

        assert result.archived_count == 2
        assert result.capped is False

    def test_prune_sessions_max_sessions_none_is_unbounded(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(*[_old_done(f"ses0000{i}", 40 + i) for i in range(5)])

        with freeze_time(_NOW):
            result = prune_sessions(max_sessions=None)

        assert result.archived_count == 5
        assert result.capped is False
        assert load_state().sessions == []

    @pytest.mark.parametrize("bad", [0, -1])
    def test_prune_sessions_max_sessions_nonpositive_raises_value_error(
        self, tmp_config_dir: Path, bad: int
    ) -> None:
        with pytest.raises(ValueError, match="max_sessions"):
            prune_sessions(max_sessions=bad)

    def test_prune_sessions_exempts_terminal_session_bound_by_row_session_id(
        self, tmp_config_dir: Path
    ) -> None:
        """An unparseable session name is still pinned by a row's session_id."""
        bound = _make_daemon_session(
            id="bound001",
            name="client-a/impl",
            client="client-a",
            status=SessionStatus.COMPLETED,
            completed_at=_OLD,
            started_at=_OLD,
        )
        _seed_sessions(bound)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="T-other", client="client-b", session_id="bound001"
                    )
                ]
            )
        )

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert [s.id for s in load_state().sessions] == ["bound001"]

    @pytest.mark.parametrize(
        "row_status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.RUNNING,
            QueueItemStatus.COMPLETED,
            QueueItemStatus.BLOCKED_ON_USER,
        ],
    )
    def test_prune_sessions_exempts_terminal_session_with_row_in_any_status(
        self, tmp_config_dir: Path, row_status: QueueItemStatus
    ) -> None:
        """A row in any status pins the session (completion may be unrouted)."""
        _seed_sessions(
            _make_daemon_session(
                id="routed01",
                name="client-a/auto-dev/T-routed",
                client="client-a",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
                last_result={"status": "shipped"},
            )
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="T-routed", client="client-a", status=row_status
                    )
                ]
            )
        )

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert [s.id for s in load_state().sessions] == ["routed01"]

    @pytest.mark.parametrize("status", [SessionStatus.ACTIVE, SessionStatus.IDLE])
    def test_prune_sessions_never_touches_active_or_idle_regardless_of_age(
        self, tmp_config_dir: Path, status: SessionStatus
    ) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="live0001",
                name="client-a/auto-dev/T-l1",
                status=status,
                started_at=_NOW - timedelta(days=3650),
            )
        )

        with freeze_time(_NOW):
            result = prune_sessions(max_sessions=500)

        assert result.archived_count == 0
        assert [s.id for s in load_state().sessions] == ["live0001"]

    def test_prune_sessions_result_records_bytes_before_after_and_cutoff(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(*[_old_done(f"ses0000{i}", 40 + i) for i in range(5)])
        _seed_sessions(
            _make_daemon_session(
                id="keep0001",
                name="client-a/auto-dev/T-keep",
                status=SessionStatus.ACTIVE,
                started_at=_RECENT,
            )
        )
        size_before = state_file().stat().st_size
        cutoff = _NOW - timedelta(days=7)

        with freeze_time(_NOW):
            result = prune_sessions(before=cutoff)

        assert result.cutoff == cutoff
        assert result.bytes_before == size_before
        assert result.bytes_after == state_file().stat().st_size
        assert result.bytes_after < result.bytes_before

    def test_prune_sessions_no_candidates_leaves_file_unchanged(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(
            _make_daemon_session(
                id="keep0001",
                name="client-a/auto-dev/T-keep",
                status=SessionStatus.COMPLETED,
                completed_at=_RECENT,
                started_at=_RECENT,
            )
        )
        before_bytes = state_file().read_bytes()
        before_mtime = state_file().stat().st_mtime_ns

        with freeze_time(_NOW):
            result = prune_sessions()

        assert result.archived_count == 0
        assert state_file().read_bytes() == before_bytes
        assert state_file().stat().st_mtime_ns == before_mtime
        assert result.bytes_after == result.bytes_before == len(before_bytes)

    def test_prune_sessions_save_failure_leaves_sessions_json_valid(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_sessions(_old_done("ses00001", 40), _old_done("ses00002", 50))
        before_bytes = state_file().read_bytes()

        def _boom(_state: CwState) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.session_retention.save_state", _boom)

        with freeze_time(_NOW), pytest.raises(OSError, match="disk full"):
            prune_sessions()

        assert state_file().read_bytes() == before_bytes
        assert {s.id for s in load_state().sessions} == {"ses00001", "ses00002"}

    def test_prune_sessions_atomic_replace_failure_leaves_sessions_json_valid(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure inside the real save_state's rename keeps the old file."""
        _seed_sessions(_old_done("ses00001", 40), _old_done("ses00002", 50))
        before_bytes = state_file().read_bytes()
        original_replace = Path.replace

        def _fake_replace(self: Path, target: Path) -> Path:
            if self.name.startswith("sessions.json.") and self.name.endswith(".tmp"):
                msg = "rename failed"
                raise OSError(msg)
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", _fake_replace)

        with freeze_time(_NOW), pytest.raises(OSError, match="rename failed"):
            prune_sessions()

        assert state_file().read_bytes() == before_bytes
        assert len(load_state().sessions) == 2
        assert list(state_dir().glob("sessions.json.*.tmp")) == []

    def test_prune_sessions_archive_append_failure_aborts_before_save(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_sessions(_old_done("ses00001", 40))
        before_bytes = state_file().read_bytes()

        def _boom(_sessions: list[Session]) -> Path:
            msg = "archive unwritable"
            raise OSError(msg)

        monkeypatch.setattr("cw.session_retention._append_to_archive", _boom)

        with freeze_time(_NOW), pytest.raises(OSError, match="archive unwritable"):
            prune_sessions()

        assert state_file().read_bytes() == before_bytes
        assert len(load_state().sessions) == 1

    def test_append_to_archive_writes_single_payload(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One write() for N sessions, so a mid-append failure cannot tear a line."""
        writes: list[str] = []
        original_open = Path.open

        class _CountingFile:
            def __init__(self, path: Path, mode: str) -> None:
                self._real = original_open(path, mode)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_exc: object) -> None:
                self._real.close()

            def write(self, data: str) -> int:
                writes.append(data)
                return self._real.write(data)

        def _open(self: Path, mode: str = "r") -> _CountingFile:
            return _CountingFile(self, mode)

        sessions = [_old_done(f"ses0000{i}", 40 + i) for i in range(3)]

        with freeze_time(_NOW), monkeypatch.context() as patch:
            patch.setattr(Path, "open", _open)
            path = _append_to_archive(sessions)

        assert len(writes) == 1
        assert writes[0].count("\n") == 3
        assert len(path.read_text().splitlines()) == 3


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

    def test_find_session_by_id_prefers_id_match_over_earlier_claude_session_id_match(
        self, tmp_config_dir: Path
    ) -> None:
        """A cross-field prefix collision resolves by field priority, not list order.

        Regression for #1983 review F1: an earlier single-pass implementation
        let list position decide a collision between one session's
        claude_session_id and a later session's id. The old (and restored)
        two-pass ``_resolve_session`` behavior checks every id first, so the
        id match wins even though it comes second in the list.
        """
        _seed_sessions(
            _make_daemon_session(
                id="byfield1",
                name="client-a/auto-dev/T-cs-collide",
                claude_session_id="collide99",
            ),
            _make_daemon_session(
                id="collide99",
                name="client-a/auto-dev/T-id-collide",
                claude_session_id=None,
            ),
        )
        found = find_session_by_id("collide9")
        assert found is not None
        assert found.id == "collide99"

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

    def test_find_session_by_id_tolerates_blank_lines_in_an_archive(
        self, tmp_config_dir: Path
    ) -> None:
        """A blank line (e.g. a torn append) does not break the archive scan."""
        _seed_sessions(
            _make_daemon_session(
                id="blank001",
                name="client-a/auto-dev/T-blank",
                status=SessionStatus.COMPLETED,
                completed_at=_OLD,
                started_at=_OLD,
            )
        )
        with freeze_time(_NOW):
            prune_sessions()
        archive = _archive_files()[0]
        archive.write_text("\n" + archive.read_text() + "\n\n")

        found = find_session_by_id("blank001")
        assert found is not None
        assert found.id == "blank001"

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

    def test_find_session_by_id_accepts_preloaded_state_and_skips_reload(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``state=`` snapshot resolves without ever calling ``load_state()``.

        The passed-in state carries a session never written to sessions.json —
        proof that resolution used the snapshot, not a disk re-read (#2149).
        """
        preloaded = CwState(
            sessions=[
                _make_daemon_session(id="pre00001", name="client-a/auto-dev/T-pre")
            ]
        )

        def _boom() -> CwState:
            msg = "load_state() must not be called when state= is passed"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.session_retention.load_state", _boom)

        found = find_session_by_id("pre0", state=preloaded)
        assert found is not None
        assert found.id == "pre00001"

    def test_find_session_by_id_default_state_arg_unchanged(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting ``state=`` still resolves via ``load_state()`` (no regression)."""
        _seed_sessions(
            _make_daemon_session(id="def00001", name="client-a/auto-dev/T-def")
        )
        calls: list[None] = []
        original_load_state = load_state

        def _tracking() -> CwState:
            calls.append(None)
            return original_load_state()

        monkeypatch.setattr("cw.session_retention.load_state", _tracking)

        found = find_session_by_id("def0")
        assert found is not None
        assert found.id == "def00001"
        assert len(calls) == 1


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

    def test_cli_session_prune_json_includes_new_fields(
        self, tmp_config_dir: Path
    ) -> None:
        """#2965: --json keeps the four original keys and gains additive ones."""
        _seed_sessions(
            _make_daemon_session(
                id="cli00006",
                name="client-a/auto-dev/T-cli6",
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
        for key in ("archived_count", "deleted_count", "archive_path", "kept_count"):
            assert key in payload
        assert payload["capped"] is False
        assert payload["bytes_before"] > payload["bytes_after"]
        expected_cutoff = _NOW - timedelta(days=_SESSION_RETENTION_DAYS)
        assert datetime.fromisoformat(payload["cutoff"]) == expected_cutoff

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
