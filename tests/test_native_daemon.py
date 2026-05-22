"""Tests for cw.native_daemon."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.native_daemon import (
    FakeNativeDaemonClient,
    RealNativeDaemonClient,
    get_native_daemon_client,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess used by patched subprocess.run."""

    def __init__(
        self, *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestRealNativeDaemonClientSpawn:
    """spawn_bg shells out to claude --bg and parses the short id."""

    def test_parses_short_id_from_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            captured["cwd"] = kwargs.get("cwd")
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        short_id = client.spawn_bg(cwd=worktree, prompt="do it")

        assert short_id == "a1b2c3d4"
        args = captured["args"]
        assert isinstance(args, list)
        assert args[:5] == [
            "claude",
            "--bg",
            "--permission-mode",
            "auto",
            "do it",
        ]
        assert captured["cwd"] == worktree

    def test_missing_short_id_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(stdout="unexpected output"),
        )
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="recognizable session id"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_missing_binary_raises_cwerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise FileNotFoundError("no claude")

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="claude binary not on PATH"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_nonzero_exit_raises_cwerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                returncode=2,
                cmd=["claude"],
                output="",
                stderr="boom",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="claude --bg exited 2"):
            client.spawn_bg(cwd=tmp_path, prompt="x")


class TestRealNativeDaemonClientRoster:
    """list_live_session_short_ids reads roster.json."""

    def test_returns_worker_keys(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(
            json.dumps(
                {
                    "workers": {
                        "aaaa1111": {"pid": 1},
                        "bbbb2222": {"pid": 2},
                    }
                }
            )
        )
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == {"aaaa1111", "bbbb2222"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        client = RealNativeDaemonClient(roster_path=tmp_path / "nope.json")
        assert client.list_live_session_short_ids() == set()

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text("{not json")
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()

    def test_workers_not_a_dict_returns_empty(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": ["a", "b"]}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()


class TestRealNativeDaemonClientStop:
    """stop is best-effort and swallows expected failure modes."""

    def test_invokes_claude_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", fake_run)
        RealNativeDaemonClient().stop("deadbeef")
        assert captured["args"] == ["claude", "stop", "deadbeef"]

    def test_missing_binary_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Must not raise — best-effort cleanup.
        RealNativeDaemonClient().stop("deadbeef")

    def test_timeout_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        RealNativeDaemonClient().stop("deadbeef")


class TestFakeNativeDaemonClient:
    """FakeNativeDaemonClient records calls and maintains a live set."""

    def test_spawn_records_and_returns_short_id(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        first = client.spawn_bg(cwd=tmp_path, prompt="a")
        second = client.spawn_bg(cwd=tmp_path, prompt="b")

        assert first != second
        assert len(first) == 8
        assert client.spawn_calls == [(tmp_path, "a"), (tmp_path, "b")]
        assert client.list_live_session_short_ids() == {first, second}

    def test_stop_drops_from_live_set(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        short_id = client.spawn_bg(cwd=tmp_path, prompt="x")
        client.stop(short_id)
        assert client.stop_calls == [short_id]
        assert client.list_live_session_short_ids() == set()


def test_get_native_daemon_client_returns_real_instance() -> None:
    assert isinstance(get_native_daemon_client(), RealNativeDaemonClient)
