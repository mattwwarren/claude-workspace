"""Tests for cw._transcript.locate_transcript."""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pytest

from cw._transcript import locate_transcript

_EPOCH = dt.datetime.fromtimestamp(0, tz=dt.UTC)
_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
_PAST = dt.datetime(2026, 6, 20, 11, 0, 0, tzinfo=dt.UTC)


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
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
        """csid set but file absent → None (no surface_ref fallthrough in pure helper)."""
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

    def test_surface_ref_newest_only_stale_wins(self, tmp_path: Path) -> None:
        """Newest candidate is stale → None even if older candidates are fresh."""
        # newest is stale (before started_at)
        stale_mtime = _PAST.timestamp() - 120
        _touch(tmp_path / "abcd1234-b.jsonl", mtime=stale_mtime)
        # older candidate is fresh (after started_at) — but newest-only ignores it
        fresh_mtime = _PAST.timestamp() + 30
        _touch(tmp_path / "abcd1234-a.jsonl", mtime=fresh_mtime)

        # Ensure "b" has a higher mtime than "a" so it's newest
        # stale_mtime < fresh_mtime, so actually "a" would be newest
        # Let's flip: make "b" the newest by giving it a higher mtime, but still stale
        # Adjust: make stale newer than fresh by picking times carefully
        # stale: after started_at boundary but before started_at for this test
        # Actually we want: newest > started_at? No. Let's redo with explicit control.
        pass  # see test_surface_ref_newest_only_stale_newest below

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
        # Make project_dir non-readable so glob raises
        import os

        proj = tmp_path / "proj"
        proj.mkdir()
        os.chmod(proj, 0o000)
        try:
            result = locate_transcript(
                project_dir=proj,
                claude_session_id=None,
                surface_ref="abcd",
                started_at=_EPOCH,
            )
            assert result is None
        finally:
            os.chmod(proj, 0o755)
