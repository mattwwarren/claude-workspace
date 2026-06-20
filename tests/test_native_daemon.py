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
    read_supervisor_resume_session_id,
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


class TestRealNativeDaemonClientSpawnGitEnv:
    """spawn_bg must not leak GIT_* env vars into the worker subprocess."""

    def test_git_vars_stripped_from_subprocess_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GIT_DIR and GIT_INDEX_FILE must not appear in env passed to subprocess.run.

        Without the fix, spawn_bg passes no env= argument and the worker inherits
        the orchestrator's GIT_DIR / GIT_INDEX_FILE, misdirecting all git ops to
        the orchestrator's checkout (GitHub issue #766).
        """
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setenv("GIT_DIR", "/some/other/repo/.git")
        monkeypatch.setenv("GIT_INDEX_FILE", "/some/other/repo/.git/index")
        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict), "spawn_bg must pass env= to subprocess.run"
        git_keys = [k for k in env if k.startswith("GIT_")]
        assert not git_keys, f"GIT_* vars must be stripped; found: {git_keys}"
        assert "PATH" in env, "non-GIT env vars must be preserved (PATH missing)"


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

    def test_parses_short_id_from_ansi_coded_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression #203: Claude Code 2.1.150 wraps the short id in CSI SGR.

        Real captured output from claude --bg on 2026-05-23::

            'backgrounded \xc2\xb7 \\x1b[36m7719118f\\x1b[39m\\n'
            '\\x1b[2m  claude agents    list sessions\\x1b[22m\\n'
            ...

        The parser must strip ANSI escapes before searching, otherwise the
        ``\\x1b[36m`` between ``\xc2\xb7`` and the hex id breaks the match.
        """
        ansi_stdout = (
            "backgrounded · \x1b[36m7719118f\x1b[39m\n"
            "\x1b[2m  claude agents             list sessions\x1b[22m\n"
            "\x1b[2m  claude attach 7719118f    open in this terminal\x1b[22m\n"
            "\x1b[2m  claude logs 7719118f      show recent output\x1b[22m\n"
            "\x1b[2m  claude stop 7719118f      stop this session\x1b[22m\n"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(stdout=ansi_stdout),
        )
        client = RealNativeDaemonClient()

        assert client.spawn_bg(cwd=tmp_path, prompt="x") == "7719118f"

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
            msg = "no claude"
            raise FileNotFoundError(msg)

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

    def test_usage_limit_calledprocesserror_raises_usage_limit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CalledProcessError with usage-limit stderr raises UsageLimitError."""
        from cw.exceptions import UsageLimitError

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1,
                ["claude"],
                output="",
                stderr="You've hit your session limit · resets 3:45pm",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(UsageLimitError, match="usage limit"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_usage_limit_in_stdout_raises_usage_limit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exits 0 but stdout contains usage-limit text instead of session id."""
        from cw.exceptions import UsageLimitError

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(
                stdout="You've hit your weekly limit · resets Mon 12:00am"
            ),
        )
        client = RealNativeDaemonClient()
        with pytest.raises(UsageLimitError, match="usage limit"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_disclaimer_not_accepted_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses FULL verified stderr from claude binary 2.1.150."""
        from cw.exceptions import DisclaimerNotAcceptedError

        full_stderr = (
            "--bg with bypassPermissions requires accepting the disclaimer first. "
            "Run `claude --dangerously-skip-permissions` once interactively."
        )

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1, ["claude"], output="", stderr=full_stderr
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(DisclaimerNotAcceptedError, match="disclaimer"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_disclaimer_error_message_contains_verbatim_ac_substring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: error message must contain verbatim AC2 lowercase-r substring."""
        from cw.exceptions import DisclaimerNotAcceptedError

        full_stderr = (
            "--bg with bypassPermissions requires accepting the disclaimer first. "
            "Run `claude --dangerously-skip-permissions` once interactively."
        )

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1, ["claude"], output="", stderr=full_stderr
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        exc_info: pytest.ExceptionInfo[DisclaimerNotAcceptedError]
        with pytest.raises(DisclaimerNotAcceptedError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")
        # Verbatim AC2 substring (lowercase 'r') must appear in the message.
        assert "run `claude --dangerously-skip-permissions` once" in str(exc_info.value)

    def test_spawn_bg_permission_mode_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """permission_mode override replaces _DEFAULT_PERMISSION_MODE in cmd."""
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        client.spawn_bg(
            cwd=tmp_path, prompt="do it", permission_mode="bypassPermissions"
        )

        args = captured["args"]
        assert isinstance(args, list)
        assert args[:5] == [
            "claude",
            "--bg",
            "--permission-mode",
            "bypassPermissions",
            "do it",
        ]


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

    def test_raise_usage_limit_raises_before_counter(self, tmp_path: Path) -> None:
        """raise_usage_limit=True raises UsageLimitError before incrementing counter."""
        from cw.exceptions import UsageLimitError

        client = FakeNativeDaemonClient()
        client.raise_usage_limit = True
        with pytest.raises(UsageLimitError):
            client.spawn_bg(cwd=tmp_path, prompt="x")
        # Counter should not have been incremented — no slot consumed.
        assert client.spawn_calls == []
        assert client.list_live_session_short_ids() == set()

    def test_raise_usage_limit_false_by_default(self, tmp_path: Path) -> None:
        """raise_usage_limit defaults to False — normal spawn behavior."""
        client = FakeNativeDaemonClient()
        assert client.raise_usage_limit is False
        short_id = client.spawn_bg(cwd=tmp_path, prompt="x")
        assert len(short_id) == 8


def test_get_native_daemon_client_returns_real_instance() -> None:
    assert isinstance(get_native_daemon_client(), RealNativeDaemonClient)


class TestReadSupervisorResumeSessionId:
    """read_supervisor_resume_session_id reads ~/.claude/jobs/<id>/state.json."""

    def test_returns_resume_session_id(self, tmp_path: Path) -> None:
        short_id = "a1b2c3d4"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        full_uuid = "a1b2c3d4-0000-0000-0000-000000000001"
        (state_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": full_uuid, "sessionId": full_uuid}),
            encoding="utf-8",
        )
        assert (
            read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) == full_uuid
        )

    def test_missing_directory_returns_none(self, tmp_path: Path) -> None:
        assert read_supervisor_resume_session_id("deadbeef", jobs_path=tmp_path) is None

    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        (tmp_path / short_id).mkdir()
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{not json", encoding="utf-8")
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps({"sessionId": "abc"}), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_non_string_value_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": 42}), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps(["not", "a", "dict"]), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None
