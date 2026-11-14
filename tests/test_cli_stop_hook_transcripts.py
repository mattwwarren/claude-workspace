"""Tests for the shared transcript-candidate helper (#3623).

Pins the single definition of ``_transcript_candidates`` in
``cw.cli.stop_hook.transcripts`` and the two call sites that read it: the #2135
park (``park._sentinel_frame_follows_marker``) and the #2348 continuation
(``continuation._find_transcripts``). Each call site must resolve candidates
through its OWN module-global ``claude_project_dir``.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.cli.stop_hook as stop_hook_pkg
from cw._util import claude_project_dir
from cw.cli.stop_hook import continuation, park, transcripts
from cw.cli.stop_hook.transcripts import _transcript_candidates
from cw.models import ParkCommentMarker, Stage
from tests._shared.session_seeding import _make_daemon_session
from tests._shared.stop_hook_context import _write_stop_hook_transcript

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import Session

CSID = "csid-3623"
MARKER = ParkCommentMarker(
    ticket_id="940",
    stage=Stage.PLAN,
    session_id="sess-1",
    posted_at=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
)


def _session(worktree: Path | None) -> Session:
    return _make_daemon_session(
        id="sess-1", worktree_path=worktree, claude_session_id=CSID
    )


def _recording_project_dir(tmp_path: Path, calls: list[str]) -> Callable[[str], Path]:
    def _project_dir(path: str) -> Path:
        calls.append(path)
        return tmp_path / "proj" / path.replace("/", "_")

    return _project_dir


class TestTranscriptCandidates:
    """The pure helper: order, de-duplication, bad-id guard, no filesystem."""

    def test_cwd_equal_to_worktree_yields_one_candidate(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        calls: list[str] = []
        result = _transcript_candidates(
            _session(wt), str(wt), CSID, _recording_project_dir(tmp_path, calls)
        )
        assert result == (
            tmp_path / "proj" / str(wt).replace("/", "_") / f"{CSID}.jsonl",
        )
        assert calls == [str(wt)]

    def test_distinct_cwd_and_worktree_yield_cwd_then_worktree(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        calls: list[str] = []
        result = _transcript_candidates(
            _session(wt), str(cwd), CSID, _recording_project_dir(tmp_path, calls)
        )
        assert result == (
            tmp_path / "proj" / str(cwd).replace("/", "_") / f"{CSID}.jsonl",
            tmp_path / "proj" / str(wt).replace("/", "_") / f"{CSID}.jsonl",
        )
        assert calls == [str(cwd), str(wt)]

    def test_no_worktree_yields_cwd_only(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"
        calls: list[str] = []
        result = _transcript_candidates(
            _session(None), str(cwd), CSID, _recording_project_dir(tmp_path, calls)
        )
        assert len(result) == 1
        assert calls == [str(cwd)]

    @pytest.mark.parametrize("bad_id", [None, "", 0, 123, b"x"])
    def test_bad_claude_session_id_yields_nothing_and_no_lookup(
        self, tmp_path: Path, bad_id: object
    ) -> None:
        calls: list[str] = []
        result = _transcript_candidates(
            _session(tmp_path / "wt"),
            str(tmp_path),
            bad_id,
            _recording_project_dir(tmp_path, calls),
        )
        assert result == ()
        assert calls == []


class TestSingleSourceOfCandidateDiscovery:
    """One definition, one object: the two readers share the helper."""

    def test_exactly_one_definition_in_the_package(self) -> None:
        pkg_dir = Path(stop_hook_pkg.__file__).parent
        hits: list[Path] = []
        for path in sorted(pkg_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if re.findall(r"^def _transcript_candidates\b", text, re.MULTILINE):
                hits.append(path)
        assert hits == [pkg_dir / "transcripts.py"]

    def test_both_readers_hold_the_same_function_object(self) -> None:
        assert park._transcript_candidates is _transcript_candidates
        assert continuation._transcript_candidates is _transcript_candidates
        assert transcripts._transcript_candidates is _transcript_candidates

    def test_readers_do_not_reimplement_the_dedupe(self) -> None:
        # Brittle by design: the inline ``dict.fromkeys`` copy is what drifted.
        assert "dict.fromkeys" not in inspect.getsource(park)
        assert "dict.fromkeys" not in inspect.getsource(continuation)


class TestBothCallSitesResolveTheSameCandidates:
    """Acceptance: park and continuation see the same candidate paths."""

    @pytest.mark.parametrize(
        ("cwd_is_worktree", "worktree_set"),
        [(True, True), (False, True), (False, False)],
        ids=["same-dir", "distinct-dirs", "no-worktree"],
    )
    def test_park_and_continuation_agree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cwd_is_worktree: bool,
        worktree_set: bool,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        cwd = wt if cwd_is_worktree else tmp_path / "cwd"
        session = _session(wt if worktree_set else None)
        expected = claude_project_dir(str(cwd)) / f"{CSID}.jsonl"

        seen: dict[str, list[tuple[Path, ...]]] = {"park": [], "continuation": []}
        real = _transcript_candidates

        def _spy(
            key: str,
        ) -> Callable[[Session, str, object, Callable[[str], Path]], tuple[Path, ...]]:
            def _wrapped(
                session_arg: Session,
                cwd_arg: str,
                csid_arg: object,
                project_dir_arg: Callable[[str], Path],
            ) -> tuple[Path, ...]:
                result = real(session_arg, cwd_arg, csid_arg, project_dir_arg)
                seen[key].append(result)
                return result

            return _wrapped

        monkeypatch.setattr(
            "cw.cli.stop_hook.park._transcript_candidates", _spy("park"), raising=True
        )
        monkeypatch.setattr(
            "cw.cli.stop_hook.continuation._transcript_candidates",
            _spy("continuation"),
            raising=True,
        )
        _write_stop_hook_transcript(home, cwd, CSID)
        park._sentinel_frame_follows_marker(session, str(cwd), CSID, MARKER)
        continuation._find_transcripts(session, str(cwd), CSID)

        assert seen["park"] == seen["continuation"]
        assert expected in seen["park"][0]

    @pytest.mark.parametrize("distinct", [True, False], ids=["distinct", "same"])
    def test_each_site_resolves_through_its_own_project_dir_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, distinct: bool
    ) -> None:
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd" if distinct else wt
        expected_lookups = 2 if distinct else 1
        session = _session(wt)

        def _counter(counts: list[str]) -> Callable[[str], Path]:
            def _counting(path: str | Path) -> Path:
                counts.append(str(path))
                return claude_project_dir(path)

            return _counting

        park_counts: list[str] = []
        cont_counts: list[str] = []
        monkeypatch.setattr(
            "cw.cli.stop_hook.park.claude_project_dir",
            _counter(park_counts),
            raising=True,
        )
        monkeypatch.setattr(
            "cw.cli.stop_hook.continuation.claude_project_dir",
            _counter(cont_counts),
            raising=True,
        )
        monkeypatch.setattr(
            "cw.cli.stop_hook.park._sentinel_frame_after",
            lambda _p, _t: False,
            raising=True,
        )
        park._sentinel_frame_follows_marker(session, str(cwd), CSID, MARKER)
        continuation._find_transcripts(session, str(cwd), CSID)

        assert len(park_counts) == expected_lookups
        assert len(cont_counts) == expected_lookups


class TestParkHotPath:
    """park's loop: lazy per-path reads, early return, bad-id short circuit."""

    def _patch_frame(
        self, monkeypatch: pytest.MonkeyPatch, answer: list[bool]
    ) -> list[Path]:
        recorded: list[Path] = []

        def _recorder(path: Path, _posted_at: object) -> bool:
            recorded.append(path)
            return answer.pop(0) if answer else False

        monkeypatch.setattr(
            "cw.cli.stop_hook.park._sentinel_frame_after", _recorder, raising=True
        )
        return recorded

    def test_same_dir_transcript_read_once_no_frame_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        path = _write_stop_hook_transcript(home, wt, CSID)
        recorded = self._patch_frame(monkeypatch, [False])
        lookups: list[str] = []
        real = claude_project_dir

        monkeypatch.setattr(
            "cw.cli.stop_hook.park.claude_project_dir",
            lambda p: (lookups.append(str(p)), real(p))[1],
            raising=True,
        )
        result = park._sentinel_frame_follows_marker(
            _session(wt), str(wt), CSID, MARKER
        )

        assert result is False
        assert recorded == [path]
        assert len(lookups) == 1

    def test_distinct_dirs_both_present_no_frame_reads_both_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        cwd_path = _write_stop_hook_transcript(home, cwd, CSID)
        wt_path = _write_stop_hook_transcript(home, wt, CSID)
        recorded = self._patch_frame(monkeypatch, [False, False])

        result = park._sentinel_frame_follows_marker(
            _session(wt), str(cwd), CSID, MARKER
        )

        assert result is False
        assert recorded == [cwd_path, wt_path]

    def test_frame_in_first_transcript_returns_true_with_one_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        _write_stop_hook_transcript(home, cwd, CSID)
        _write_stop_hook_transcript(home, wt, CSID)
        recorded = self._patch_frame(monkeypatch, [True, False])

        result = park._sentinel_frame_follows_marker(
            _session(wt), str(cwd), CSID, MARKER
        )

        assert result is True
        assert len(recorded) == 1

    def test_no_transcript_anywhere_returns_true_without_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        recorded = self._patch_frame(monkeypatch, [])

        result = park._sentinel_frame_follows_marker(
            _session(tmp_path / "wt"), str(tmp_path / "cwd"), CSID, MARKER
        )

        assert result is True
        assert recorded == []

    @pytest.mark.parametrize("bad_id", [None, ""])
    def test_bad_claude_session_id_returns_true_with_no_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_id: object
    ) -> None:
        lookups: list[str] = []
        monkeypatch.setattr(
            "cw.cli.stop_hook.park.claude_project_dir",
            lambda p: (lookups.append(str(p)), tmp_path)[1],
            raising=True,
        )
        result = park._sentinel_frame_follows_marker(
            _session(tmp_path / "wt"), str(tmp_path), bad_id, MARKER
        )
        assert result is True
        assert lookups == []


class TestContinuationFindTranscripts:
    """continuation's reader: existence filter, ordering, OSError swallow."""

    def test_same_dir_present_returns_single_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        path = _write_stop_hook_transcript(home, wt, CSID)
        assert continuation._find_transcripts(_session(wt), str(wt), CSID) == (path,)

    def test_distinct_dirs_both_present_returns_pair_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        cwd_path = _write_stop_hook_transcript(home, cwd, CSID)
        wt_path = _write_stop_hook_transcript(home, wt, CSID)
        assert continuation._find_transcripts(_session(wt), str(cwd), CSID) == (
            cwd_path,
            wt_path,
        )

    def test_only_worktree_present_returns_worktree_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        wt = tmp_path / "wt"
        cwd = tmp_path / "cwd"
        wt_path = _write_stop_hook_transcript(home, wt, CSID)
        assert continuation._find_transcripts(_session(wt), str(cwd), CSID) == (
            wt_path,
        )

    def test_none_present_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert (
            continuation._find_transcripts(
                _session(tmp_path / "wt"), str(tmp_path / "cwd"), CSID
            )
            == ()
        )

    def test_oserror_from_candidates_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = "candidates unreadable"

        def _raiser(*_args: object) -> tuple[Path, ...]:
            raise OSError(boom)

        monkeypatch.setattr(
            "cw.cli.stop_hook.continuation._transcript_candidates",
            _raiser,
            raising=True,
        )
        assert continuation._find_transcripts(_session(None), str(tmp_path), CSID) == ()
