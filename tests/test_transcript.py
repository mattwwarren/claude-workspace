"""Tests for cw._transcript.locate_transcript and find_new_subagent_transcript."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from cw._transcript import find_new_subagent_transcript, locate_transcript
from tests.conftest import _write_idle_transcript

_EPOCH = dt.datetime.fromtimestamp(0, tz=dt.UTC)
_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
_PAST = dt.datetime(2026, 6, 20, 11, 0, 0, tzinfo=dt.UTC)


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _seed_transcript(
    home: Path,
    worktree: Path,
    filename: str,
    *,
    mtime: dt.datetime,
) -> Path:
    """Write a transcript under *worktree*'s project dir at a controlled mtime.

    Reuses ``tests.conftest._write_idle_transcript`` (the suite's canonical
    "write a .jsonl transcript under the encoded project dir" helper) rather
    than hand-rolling a parallel writer, then stamps the mtime the way
    ``tests/test_reconcile_liveness.py`` does.
    """
    path = _write_idle_transcript(home, worktree, filename=filename)
    ts = mtime.timestamp()
    os.utime(str(path), (ts, ts))
    return path


class TestLocateTranscript:
    def test_none_project_dir_returns_none(self) -> None:
        result = locate_transcript(
            project_dir=None,
            claude_session_id="abc",
            surface_ref=None,
            started_at=_EPOCH,
        )
        assert result is None

    def test_missing_project_dir_returns_none(self, tmp_path: Path) -> None:
        result = locate_transcript(
            project_dir=tmp_path / "nonexistent",
            claude_session_id=None,
            surface_ref=None,
            started_at=_EPOCH,
        )
        assert result is None

    def test_csid_exact_hit(self, tmp_path: Path) -> None:
        csid = "aaaa1111-0000-0000-0000-000000000000"
        expected = _touch(tmp_path / f"{csid}.jsonl")
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=csid,
            surface_ref=None,
            started_at=_EPOCH,
        )
        assert result == expected

    def test_csid_file_missing_returns_none(self, tmp_path: Path) -> None:
        """csid set but file absent → None; no surface_ref fallthrough in pure."""
        _touch(tmp_path / "bbbb2222.jsonl")  # surface_ref match would exist
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id="aaaa1111-0000-0000-0000-000000000000",
            surface_ref="bbbb2222",
            started_at=_EPOCH,
        )
        assert result is None

    def test_csid_takes_priority_over_surface_ref(self, tmp_path: Path) -> None:
        csid = "aaaa1111-0000-0000-0000-000000000000"
        csid_file = _touch(tmp_path / f"{csid}.jsonl")
        _touch(tmp_path / "surf_ref-abc.jsonl")
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=csid,
            surface_ref="surf_ref",
            started_at=_EPOCH,
        )
        assert result == csid_file

    def test_surface_ref_fresh_transcript(self, tmp_path: Path) -> None:
        fresh_mtime = _NOW.timestamp() + 60  # 1 minute after _NOW
        expected = _touch(tmp_path / "abcd1234-full.jsonl", mtime=fresh_mtime)
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=None,
            surface_ref="abcd1234",
            started_at=_PAST,
        )
        assert result == expected

    def test_surface_ref_stale_returns_none(self, tmp_path: Path) -> None:
        stale_mtime = _PAST.timestamp() - 60  # before started_at
        _touch(tmp_path / "abcd1234-full.jsonl", mtime=stale_mtime)
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=None,
            surface_ref="abcd1234",
            started_at=_NOW,
        )
        assert result is None

    def test_surface_ref_newest_only_stale_newest_returns_none(
        self, tmp_path: Path
    ) -> None:
        """When the *newest* candidate mtime ≤ started_at, return None."""
        started_at = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
        stale_mtime = started_at.timestamp() - 10  # just before
        _touch(tmp_path / "surf-latest.jsonl", mtime=stale_mtime)
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=None,
            surface_ref="surf",
            started_at=started_at,
        )
        assert result is None

    def test_surface_ref_no_candidates_returns_none(self, tmp_path: Path) -> None:
        _touch(tmp_path / "other-session.jsonl")
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=None,
            surface_ref="abcd1234",
            started_at=_EPOCH,
        )
        assert result is None

    def test_both_ids_absent_returns_none(self, tmp_path: Path) -> None:
        """Both csid and surface_ref None → no degraded fallback in pure helper."""
        _touch(tmp_path / "some-transcript.jsonl")
        result = locate_transcript(
            project_dir=tmp_path,
            claude_session_id=None,
            surface_ref=None,
            started_at=_EPOCH,
        )
        assert result is None

    def test_oserror_returns_none(self, tmp_path: Path) -> None:
        """OSError during glob/stat → None (not raised)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        proj.chmod(0o000)
        try:
            result = locate_transcript(
                project_dir=proj,
                claude_session_id=None,
                surface_ref="abcd",
                started_at=_EPOCH,
            )
            assert result is None
        finally:
            proj.chmod(0o755)


class TestFindNewSubagentTranscript:
    """Unit tests for ``find_new_subagent_transcript`` (#2012).

    The dispatch-verification leaf behind ``cw agent-spawn-verify``: did a
    subagent transcript that is neither the caller's own nor a pre-existing
    sibling appear in this worktree's project dir after the dispatch stamp?
    """

    _MAIN_CSID = "aaaa1111-0000-0000-0000-000000000000"

    def test_none_when_only_excluded_transcript_is_new(self, tmp_path: Path) -> None:
        """The caller's own (actively-growing) transcript never counts."""
        home = tmp_path / "home"
        worktree = tmp_path / "wt"
        own = _seed_transcript(
            home, worktree, f"{self._MAIN_CSID}.jsonl", mtime=_NOW + dt.timedelta(1)
        )

        result = find_new_subagent_transcript(
            own.parent, since=_NOW, exclude_stem=self._MAIN_CSID
        )

        assert result is None

    def test_returns_new_subagent_transcript(self, tmp_path: Path) -> None:
        """A fresh non-excluded ``*.jsonl`` under the project dir is the hit."""
        home = tmp_path / "home"
        worktree = tmp_path / "wt"
        own = _seed_transcript(
            home, worktree, f"{self._MAIN_CSID}.jsonl", mtime=_NOW + dt.timedelta(1)
        )
        expected = _seed_transcript(
            home,
            worktree,
            "subagents/bbbb2222-0000-0000-0000-000000000000.jsonl",
            mtime=_NOW + dt.timedelta(minutes=1),
        )

        result = find_new_subagent_transcript(
            own.parent, since=_NOW, exclude_stem=self._MAIN_CSID
        )

        assert result == expected

    def test_ignores_transcript_older_than_since(self, tmp_path: Path) -> None:
        """A pre-existing sibling (mtime <= since) is not this dispatch's."""
        home = tmp_path / "home"
        worktree = tmp_path / "wt"
        own = _seed_transcript(
            home, worktree, f"{self._MAIN_CSID}.jsonl", mtime=_NOW + dt.timedelta(1)
        )
        _seed_transcript(home, worktree, "stale-sibling.jsonl", mtime=_PAST)

        result = find_new_subagent_transcript(
            own.parent, since=_NOW, exclude_stem=self._MAIN_CSID
        )

        assert result is None

    def test_returns_newest_when_several_candidates(self, tmp_path: Path) -> None:
        """Deterministic pick: the newest qualifying candidate, not glob order."""
        home = tmp_path / "home"
        worktree = tmp_path / "wt"
        older = _seed_transcript(
            home, worktree, "sub-a.jsonl", mtime=_NOW + dt.timedelta(minutes=1)
        )
        newest = _seed_transcript(
            home, worktree, "sub-b.jsonl", mtime=_NOW + dt.timedelta(minutes=5)
        )

        result = find_new_subagent_transcript(
            older.parent, since=_NOW, exclude_stem=self._MAIN_CSID
        )

        assert result == newest

    def test_none_exclude_stem_matches_any_new_transcript(self, tmp_path: Path) -> None:
        """exclude_stem=None (no ``$CLAUDE_CODE_SESSION_ID``) excludes nothing."""
        home = tmp_path / "home"
        worktree = tmp_path / "wt"
        expected = _seed_transcript(
            home, worktree, "sub-a.jsonl", mtime=_NOW + dt.timedelta(minutes=1)
        )

        result = find_new_subagent_transcript(
            expected.parent, since=_NOW, exclude_stem=None
        )

        assert result == expected

    def test_missing_project_dir_returns_none(self, tmp_path: Path) -> None:
        """A project dir that does not exist → None (matches sibling helpers)."""
        result = find_new_subagent_transcript(
            tmp_path / "nonexistent", since=_NOW, exclude_stem=None
        )

        assert result is None

    def test_oserror_returns_none(self, tmp_path: Path) -> None:
        """An unreadable project dir → None, never raises."""
        proj = tmp_path / "proj"
        proj.mkdir()
        proj.chmod(0o000)
        try:
            result = find_new_subagent_transcript(proj, since=_NOW, exclude_stem=None)
            assert result is None
        finally:
            proj.chmod(0o755)
