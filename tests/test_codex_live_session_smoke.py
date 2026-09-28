"""Offline contract tests for the Codex CLI session smoke probe."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest
from scripts import probe_codex_live_session as probe

MODEL = "gpt-5.6-luna"
SESSION = "thread.smoke-2463"
VERSION = "codex-cli 0.156.1"
UNAVAILABLE_MESSAGE = "unavailable-secret"
_REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the probe's snap-visible temporary root inside pytest's sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def stream(session: str = SESSION, terminal: str = "turn.completed") -> str:
    return (
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": session}),
                json.dumps({"type": "item.completed"}),
                json.dumps({"type": terminal}),
            ]
        )
        + "\n"
    )


def jsonl(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def completed(
    argv: list[str], *, code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakePopen:
    def __init__(
        self,
        argv: list[str],
        result: subprocess.CompletedProcess[str],
        *,
        timeout: bool = False,
        wait_timeouts: list[float | None] | None = None,
    ) -> None:
        self.argv = argv
        self.pid: int | None = None
        self.stdout = io.BytesIO(result.stdout.encode("utf-8", errors="replace"))
        self._returncode = result.returncode
        self._timeout = timeout
        self._killed = False
        self.wait_timeouts = wait_timeouts if wait_timeouts is not None else []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._timeout and timeout is not None and not self._killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return -9 if self._killed else self._returncode

    def kill(self) -> None:
        self._killed = True


def fake_run(
    calls: list[dict[str, object]],
    **options: object,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    create_stdout = cast("str", options.get("create_stdout", stream()))
    resume_stdout = cast("str", options.get("resume_stdout", stream()))
    create_code = cast("int", options.get("create_code", 0))
    resume_code = cast("int", options.get("resume_code", 0))
    git_code = cast("int", options.get("git_code", 0))
    timeout_stage = cast("str | None", options.get("timeout_stage"))
    unavailable_stage = cast("str | None", options.get("unavailable_stage"))
    stderr = cast("str", options.get("stderr", ""))
    real_git = cast("bool", options.get("real_git", False))

    def run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
            }
        )
        if argv == ["codex", "--version"]:
            return completed(argv, stdout=f"{VERSION}\n", stderr=stderr)
        if argv[:2] == ["git", "init"]:
            if timeout_stage == "git":
                raise subprocess.TimeoutExpired(argv, 0)
            if real_git:
                assert cwd.is_dir()
                proc = _REAL_POPEN(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                proc.wait(timeout=probe.PROCESS_TIMEOUT_SECONDS)
                return completed(argv, code=cast("int", proc.returncode))
            return completed(argv, code=git_code, stderr=stderr)
        if argv[:3] == ["codex", "exec", "resume"]:
            if real_git:
                assert (cwd / ".git").is_dir()
            if unavailable_stage == "resume":
                raise FileNotFoundError(UNAVAILABLE_MESSAGE)
            if timeout_stage == "resume":
                raise subprocess.TimeoutExpired(argv, 0)
            return completed(
                argv, code=resume_code, stdout=resume_stdout, stderr=stderr
            )
        if unavailable_stage == "create":
            raise FileNotFoundError(UNAVAILABLE_MESSAGE)
        if timeout_stage == "create":
            raise subprocess.TimeoutExpired(argv, 0)
        if real_git and argv[:2] == ["codex", "exec"]:
            assert (cwd / ".git").is_dir()
        return completed(argv, code=create_code, stdout=create_stdout, stderr=stderr)

    return run


def set_runner(
    monkeypatch: pytest.MonkeyPatch, runner: Callable[..., object]
) -> list[float | None]:
    wait_timeouts: list[float | None] = []

    def popen(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
        start_new_session: bool,
    ) -> FakePopen:
        assert stdin == subprocess.DEVNULL
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.DEVNULL
        assert start_new_session is (os.name == "posix")
        try:
            result = cast(
                "subprocess.CompletedProcess[str]",
                runner(argv, cwd=cwd, env=env),
            )
        except subprocess.TimeoutExpired:
            return FakePopen(
                argv, completed(argv), timeout=True, wait_timeouts=wait_timeouts
            )
        return FakePopen(argv, result, wait_timeouts=wait_timeouts)

    monkeypatch.setattr("scripts.probe_codex_live_session.subprocess.Popen", popen)
    return wait_timeouts


def forbid_temporary_directory(*_args: object, **_kwargs: object) -> None:
    pytest.fail("created a temporary repository before the probe was accepted")


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return state.rsplit(")", 1)[1].strip().split()[0] != "Z"


def wait_for_process_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not process_is_running(pid)


def cleanup_process(pid: int | None) -> None:
    if pid is not None and process_is_running(pid):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def assert_isolated_child_environment(
    call: dict[str, object], checkout: Path, configured_codex_home: Path
) -> None:
    cwd = cast("Path", call["cwd"])
    env = cast("dict[str, str]", call["env"])
    assert cwd.is_relative_to(Path.home() / ".cache" / "cw-live-tests")
    assert not probe._is_within(cwd, checkout)
    assert env["PWD"] == str(cwd)
    assert env["HOME"] == env["USERPROFILE"] == str(cwd.parent)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"]
    assert Path(env["TMPDIR"]).is_relative_to(cwd.parent)
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["HTTPS_PROXY"] == "https://proxy.example:8443"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "LC_SECRET" not in env
    assert "CODEX_SANDBOX" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert "NODE_OPTIONS" not in env
    if call["argv"] == ["git", "init", "--quiet"]:
        assert "CODEX_HOME" not in env
        assert "CODEX_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
    else:
        assert env["CODEX_HOME"] == str(configured_codex_home)
        assert env["CODEX_API_KEY"] == "auth-material-secret"
        assert env["OPENAI_API_KEY"] == "openai-auth-secret"
    assert all(
        key.casefold() in probe._CHILD_ENV_KEYS
        or key in {"CODEX_HOME", "PWD", "TMPDIR", "TMP", "TEMP"}
        for key in env
    )


class TestParser:
    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            ("garbage\n", "malformed_jsonl"),
            ("[]\n", "malformed_jsonl"),
            ('{"thread_id":"missing-type"}\n', "malformed_jsonl"),
            ('{"type":42}\n', "malformed_jsonl"),
            ("", "missing_thread"),
            ('{"type":"turn.completed"}\n', "missing_thread"),
            (stream("bad/id"), "invalid_thread_id"),
            (
                stream() + json.dumps({"type": "thread.started", "thread_id": "two"}),
                "duplicate_thread_started",
            ),
            (
                json.dumps({"type": "thread.started", "thread_id": SESSION})
                + '\n{"type":"turn.cancelled"}\n',
                "invalid_terminal",
            ),
            (
                json.dumps({"type": "thread.started", "thread_id": SESSION}) + "\n",
                "invalid_terminal",
            ),
        ],
    )
    def test_structural_errors(self, payload: str, error: str) -> None:
        assert probe._parse_stream(payload).error == error

    def test_success_ignores_blank_and_unrelated_events(self) -> None:
        parsed = probe._parse_stream("\n" + stream())
        assert parsed == probe.ParsedStream(SESSION, "turn.completed", None)

    def test_failed_turn_is_terminal_but_not_success(self) -> None:
        assert (
            probe._parse_stream(stream(terminal="turn.failed")).terminal_event
            == "turn.failed"
        )

    @pytest.mark.parametrize("session", ["", "bad/id", "x" * 129])
    def test_invalid_thread_ids_are_rejected(self, session: str) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": session},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "invalid_thread_id"

    def test_non_string_thread_id_is_rejected(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": 2463},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "invalid_thread_id"

    def test_event_after_terminal_is_rejected(self) -> None:
        parsed = probe._parse_stream(stream() + '{"type":"item.completed"}\n')
        assert parsed.error == "invalid_terminal"

    def test_duplicate_terminal_is_rejected(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
                {"type": "turn.failed"},
            )
        )
        assert parsed.error == "invalid_terminal"

    def test_unknown_turn_terminal_is_rejected(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.cancelled"},
            )
        )
        assert parsed.error == "invalid_terminal"

    def test_unknown_nonterminal_event_is_rejected(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": "future terminal error"},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "malformed_jsonl"


class TestProbe:
    def test_opt_out_is_exact_and_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_CODEX_LIVE_SESSION_SMOKE", raising=False)
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(None) == {
            "status": "skipped",
            "cli_version": None,
            "model": None,
            "session_id": None,
            "create": None,
            "resume": None,
            "error_code": "opt_in_required",
        }

    @pytest.mark.parametrize(
        "model",
        [
            None,
            "bad/model",
            "x" * 65,
            "--last",
            "--ephemeral",
            "--approve-for-me",
            "--dangerously-bypass-approvals-and-sandbox",
            "--sandbox",
            "--model=--last",
            "-m",
            "-o",
            "read-only",
            "workspace-write",
        ],
    )
    def test_invalid_model_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, model: str | None
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        result = probe.run_probe(model)
        assert result == {
            "status": "failed",
            "cli_version": None,
            "model": None,
            "session_id": None,
            "create": None,
            "resume": None,
            "error_code": "invalid_model",
        }

    @pytest.mark.parametrize(
        "model", ["--last", "--dangerously-bypass-approvals-and-sandbox"]
    )
    def test_main_sanitizes_option_like_model_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        model: str,
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
        assert probe.main(["--model", model]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        result = json.loads(captured.out)
        assert result["status"] == "failed"
        assert result["error_code"] == "invalid_model"

    def test_success_has_exact_commands_policy_cwd_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setenv("CODEX_API_KEY", "auth-material-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-auth-secret")
        monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example:8443")
        configured_codex_home = Path.home() / "codex-state"
        monkeypatch.setenv("CODEX_HOME", str(configured_codex_home))
        monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/untrusted/config")
        monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted")
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        monkeypatch.setenv("LC_SECRET", "untrusted-locale-secret")
        monkeypatch.setenv("GIT_DIR", "/checkout/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/checkout")
        monkeypatch.setenv("TMPDIR", "/checkout/tmp")
        wait_timeouts = set_runner(monkeypatch, fake_run(calls))
        result = probe.run_probe(MODEL)
        assert set(result) == {
            "status",
            "cli_version",
            "model",
            "session_id",
            "create",
            "resume",
            "error_code",
        }
        assert result == {
            "status": "passed",
            "cli_version": VERSION,
            "model": MODEL,
            "session_id": SESSION,
            "create": {"exit_code": 0, "terminal_event": "turn.completed"},
            "resume": {
                "exit_code": 0,
                "terminal_event": "turn.completed",
                "id_matches": True,
            },
            "error_code": None,
        }
        assert calls[0]["argv"] == ["codex", "--version"]
        assert calls[1]["argv"] == ["git", "init", "--quiet"]
        assert calls[2]["argv"] == [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--model",
            MODEL,
            probe.FIXED_PROMPT,
        ]
        assert calls[3]["argv"] == [
            "codex",
            "exec",
            "resume",
            SESSION,
            "--json",
            "--ignore-user-config",
            "--model",
            MODEL,
            probe.FIXED_RESUME_PROMPT,
        ]
        assert calls[0]["cwd"] == calls[1]["cwd"] == calls[2]["cwd"] == calls[3]["cwd"]
        checkout = probe._checkout_root()
        for call in calls:
            assert_isolated_child_environment(call, checkout, configured_codex_home)
        assert wait_timeouts == [probe.PROCESS_TIMEOUT_SECONDS] * len(calls)
        forbidden = {
            "--last",
            "--ephemeral",
            "--approve-for-me",
            "--dangerously-bypass-approvals-and-sandbox",
            "workspace-write",
            "-o",
        }
        assert forbidden.isdisjoint(calls[2]["argv"])
        assert forbidden.isdisjoint(calls[3]["argv"])

    @pytest.mark.parametrize(
        ("create_code", "expected_status"),
        [(0, "passed"), (9, "failed")],
    )
    def test_disposable_git_repo_is_initialized_and_removed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        create_code: int,
        expected_status: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        temporary_roots: list[Path] = []
        real_temporary_directory = tempfile.TemporaryDirectory

        def recording_temporary_directory(
            *, prefix: str, **kwargs: str
        ) -> tempfile.TemporaryDirectory[str]:
            temporary_directory = real_temporary_directory(
                prefix=prefix, dir=kwargs["dir"]
            )
            temporary_roots.append(Path(temporary_directory.name))
            return temporary_directory

        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            recording_temporary_directory,
        )
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(
            monkeypatch,
            fake_run(calls, create_code=create_code, real_git=True),
        )
        result = probe.run_probe(MODEL)
        assert result["status"] == expected_status
        assert len(temporary_roots) == 1
        assert not temporary_roots[0].exists()
        git_init_calls = [
            call for call in calls if call["argv"] == ["git", "init", "--quiet"]
        ]
        assert len(git_init_calls) == 1
        repo = cast("Path", git_init_calls[0]["cwd"])
        assert repo == temporary_roots[0] / "repo"
        assert not repo.exists()

    def test_stdout_limit_is_enforced_and_transcript_is_not_retained(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(probe, "MAX_PROCESS_OUTPUT_BYTES", 64)
        attempt = probe._attempt(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 8192)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            stage="create",
        )
        assert attempt.error_code == "create_malformed_jsonl"
        assert attempt.outcome.output_exceeded_limit
        assert attempt.outcome.stdout == ""
        assert attempt.stream.error == "malformed_jsonl"

    @pytest.mark.skipif(
        sys.platform != "linux", reason="process liveness check uses /proc"
    )
    def test_output_overflow_terminates_live_producer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(probe, "MAX_PROCESS_OUTPUT_BYTES", 1024)
        code = (
            "import os, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "print(f'DESCENDANT_PID={child.pid}', flush=True); "
            "os.write(1, b'x' * 8192); time.sleep(30)"
        )
        pid: int | None = None
        started = time.monotonic()
        try:
            result = probe._run_process(
                [sys.executable, "-c", code],
                cwd=tmp_path,
                env=os.environ.copy(),
            )
            elapsed = time.monotonic() - started
            pid_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("DESCENDANT_PID=")
            )
            pid = int(pid_line.split("=", 1)[1])
            assert result.output_exceeded_limit
            assert result.exit_code != 0
            assert elapsed < 5
            assert wait_for_process_exit(pid)
        finally:
            cleanup_process(pid)

    @pytest.mark.skipif(
        sys.platform != "linux", reason="process liveness check uses /proc"
    )
    def test_parent_interrupt_kills_and_reaps_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "child.pid"
        code = (
            "import os, time; "
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 2)

        def interrupt_parent(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        previous_handler = signal.signal(signal.SIGUSR1, interrupt_parent)
        timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGUSR1))
        pid: int | None = None
        try:
            timer.start()
            outcome = probe._run_process(
                [sys.executable, "-c", code], cwd=tmp_path, env=os.environ.copy()
            )
            assert outcome.timed_out
            pid = int(pid_file.read_text(encoding="utf-8"))
            assert wait_for_process_exit(pid)
        finally:
            timer.cancel()
            timer.join(timeout=1)
            signal.signal(signal.SIGUSR1, previous_handler)
            cleanup_process(pid)

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"create_stdout": "garbage\n"}, "create_malformed_jsonl"),
            (
                {"create_stdout": jsonl({"type": "turn.completed"})},
                "create_missing_thread",
            ),
            ({"create_stdout": stream("bad/id")}, "create_invalid_thread_id"),
            (
                {"create_stdout": stream() + jsonl({"type": "thread.started"})},
                "create_duplicate_thread_started",
            ),
            (
                {"create_stdout": stream(terminal="turn.failed")},
                "create_invalid_terminal",
            ),
            ({"create_code": 9}, "create_nonzero_exit"),
            ({"timeout_stage": "create"}, "create_timeout"),
            ({"unavailable_stage": "create"}, "cli_unavailable"),
            ({"git_code": 9}, "repo_setup_failed"),
            ({"timeout_stage": "git"}, "repo_setup_failed"),
        ],
    )
    def test_create_failures_are_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, object],
        error: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run(calls, **kwargs))
        result = probe.run_probe(MODEL)
        assert result["status"] == "failed"
        assert result["error_code"] == error
        assert result["resume"] is None
        assert "TimeoutExpired" not in json.dumps(result)
        assert UNAVAILABLE_MESSAGE not in json.dumps(result)
        if kwargs.get("unavailable_stage") == "create":
            assert result["create"] is not None
            assert result["create"]["exit_code"] is None

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"resume_stdout": "garbage\n"}, "resume_malformed_jsonl"),
            (
                {"resume_stdout": jsonl({"type": "turn.completed"})},
                "resume_missing_thread",
            ),
            ({"resume_stdout": stream("bad/id")}, "resume_invalid_thread_id"),
            (
                {"resume_stdout": stream() + jsonl({"type": "thread.started"})},
                "resume_duplicate_thread_started",
            ),
            (
                {"resume_stdout": stream(terminal="turn.failed")},
                "resume_invalid_terminal",
            ),
            ({"resume_code": 9}, "resume_nonzero_exit"),
            ({"timeout_stage": "resume"}, "resume_timeout"),
            ({"unavailable_stage": "resume"}, "cli_unavailable"),
            ({"resume_stdout": stream("other-thread")}, "resume_id_mismatch"),
        ],
    )
    def test_resume_failures_are_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, object],
        error: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run(calls, **kwargs))
        result = probe.run_probe(MODEL)
        assert result["status"] == "failed"
        assert result["error_code"] == error
        assert set(result) == {
            "status",
            "cli_version",
            "model",
            "session_id",
            "create",
            "resume",
            "error_code",
        }
        if kwargs.get("unavailable_stage") == "resume":
            assert result["resume"] is not None
            assert result["resume"]["exit_code"] is None
        if error == "resume_id_mismatch":
            assert result["resume"] is not None
            assert result["resume"]["id_matches"] is False

    @pytest.mark.parametrize(
        ("runner_error", "error"),
        [("invalid", "version_invalid"), ("unavailable", "cli_unavailable")],
    )
    def test_version_failures_do_not_create_repo(
        self, monkeypatch: pytest.MonkeyPatch, runner_error: str, error: str
    ) -> None:
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if runner_error == "unavailable":
                raise FileNotFoundError
            return completed(argv, stdout="codex-cli (dev)\n")

        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, run)
        assert probe.run_probe(MODEL)["error_code"] == error
        assert calls == [["codex", "--version"]]

    @pytest.mark.parametrize(
        "stdout",
        [
            "codex-cli 0.156.1\n\n",
            "codex-cli 0.156.1\nsecret-transcript\n",
            "codex-cli 0.156.1" + "x" * 65,
        ],
    )
    def test_version_requires_one_valid_line(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv == ["codex", "--version"]:
                return completed(argv, stdout=stdout, stderr="auth-secret")
            if argv[:2] == ["git", "init"]:
                return completed(argv)
            if argv[:3] == ["codex", "exec", "resume"]:
                return completed(argv, stdout=stream())
            return completed(argv, stdout=stream())

        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, run)
        result = probe.run_probe(MODEL)
        assert result["error_code"] == "version_invalid"
        assert calls == [["codex", "--version"]]

    def test_version_timeout_and_nonzero_are_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for code, timeout in [(9, False), (0, True)]:

            def run(
                argv: list[str],
                _code: int = code,
                _timeout: bool = timeout,
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if _timeout:
                    raise subprocess.TimeoutExpired(argv, 0)
                return completed(argv, code=_code)

            monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
            set_runner(monkeypatch, run)
            result = probe.run_probe(MODEL)
            assert result["error_code"] == "version_unavailable"

    def test_codex_home_inside_checkout_fails_setup_before_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setenv("CODEX_HOME", str(probe._checkout_root() / ".codex"))
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

    @pytest.mark.skipif(
        os.name == "nt", reason="directory symlink creation may require elevation"
    )
    def test_codex_home_sessions_symlink_into_checkout_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / probe.CODEX_SESSION_ARTIFACT_DIR).symlink_to(
            probe._checkout_root(), target_is_directory=True
        )
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

    @pytest.mark.skipif(
        os.name == "nt", reason="directory symlink creation may require elevation"
    )
    def test_codex_home_nested_sessions_symlink_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / "codex-home"
        nested_year = codex_home / probe.CODEX_SESSION_ARTIFACT_DIR / "2026"
        nested_year.mkdir(parents=True)
        (nested_year / "09").symlink_to(
            probe._checkout_root(), target_is_directory=True
        )
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

    def test_codex_home_defaults_to_home_dot_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEX_HOME", raising=False)
        assert probe._codex_home(probe._checkout_root()) == Path.home() / ".codex"

    def test_invalid_utf8_is_replaced_at_process_boundary(self, tmp_path: Path) -> None:
        result = probe._run_process(
            [sys.executable, "-c", "import os; os.write(1, b'\\xff')"],
            cwd=tmp_path,
            env=os.environ.copy(),
        )
        assert result.exit_code == 0
        assert result.stdout == "\ufffd"

    @pytest.mark.skipif(
        sys.platform != "linux", reason="process liveness check uses /proc"
    )
    def test_reader_shutdown_kills_descendant_holding_stdout(
        self, tmp_path: Path
    ) -> None:
        code = (
            "import os, subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "print(f'DESCENDANT_PID={child.pid}', flush=True); "
            "print('parent-finished', flush=True)"
        )
        pid: int | None = None
        started = time.monotonic()
        try:
            result = probe._run_process(
                [sys.executable, "-c", code], cwd=tmp_path, env=os.environ.copy()
            )
            elapsed = time.monotonic() - started
            pid_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("DESCENDANT_PID=")
            )
            pid = int(pid_line.split("=", 1)[1])
            assert result.exit_code == 0
            assert result.reader_incomplete
            assert "parent-finished" in result.stdout
            assert elapsed < 5
            assert wait_for_process_exit(pid)
        finally:
            cleanup_process(pid)

    @pytest.mark.parametrize("create_code", [0, 7])
    def test_cleanup_failure_preserves_sanitized_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        create_code: int,
    ) -> None:
        class BrokenTemporaryDirectory:
            name = str(tmp_path / "temp-root")

            def __init__(self, **_kwargs: object) -> None:
                Path(self.name).mkdir(parents=True, exist_ok=True)

            def cleanup(self) -> None:
                cleanup_error = "cleanup-secret"
                raise OSError(cleanup_error)

        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            BrokenTemporaryDirectory,
        )
        set_runner(
            monkeypatch,
            fake_run(calls, create_code=create_code, stderr="stderr-secret"),
        )
        result = probe.run_probe(MODEL)
        assert result["status"] == "failed"
        assert result["error_code"] == "cleanup_failed"
        expected: probe.SmokeResult = {
            "status": "failed",
            "cli_version": VERSION,
            "model": MODEL,
            "session_id": SESSION,
            "create": {
                "exit_code": create_code,
                "terminal_event": "turn.completed",
            },
            "resume": None,
            "error_code": "cleanup_failed",
        }
        if create_code == 0:
            expected["resume"] = {
                "exit_code": 0,
                "terminal_event": "turn.completed",
                "id_matches": True,
            }
        assert result == expected
        assert "cleanup-secret" not in json.dumps(result)

    def test_failed_cli_probe_exits_nonzero_with_one_sanitized_json_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run([], create_code=7))
        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        assert json.loads(captured.out)["error_code"] == "create_nonzero_exit"

    def test_result_does_not_expose_raw_output_or_environment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        raw = "raw-transcript-secret"
        auth = "auth-material-secret"
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setenv("CODEX_API_KEY", auth)
        create_stdout = jsonl(
            {"type": "thread.started", "thread_id": SESSION, "text": raw},
            {"type": "item.completed", "text": raw},
            {"type": "turn.completed", "text": raw},
        )
        set_runner(
            monkeypatch,
            fake_run(
                [],
                create_stdout=create_stdout,
                resume_stdout=create_stdout,
                stderr=auth,
            ),
        )
        assert probe.main(["--model", MODEL]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert raw not in captured.out
        assert auth not in captured.out

    @pytest.mark.parametrize(
        ("stage", "expected_error"),
        [
            ("create", "create_nonzero_exit"),
            ("resume", "resume_nonzero_exit"),
        ],
    )
    def test_failure_results_do_not_expose_raw_output_or_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        stage: str,
        expected_error: str,
    ) -> None:
        transcript_marker = "failure-transcript-marker"
        stderr_marker = "failure-stderr-marker"
        stream_with_secret = jsonl(
            {"type": "thread.started", "thread_id": SESSION, "text": transcript_marker},
            {"type": "item.completed", "text": transcript_marker},
            {"type": "turn.completed", "text": transcript_marker},
        )
        options: dict[str, object] = {
            "stderr": stderr_marker,
        }
        if stage == "create":
            options.update(create_code=7, create_stdout=stream_with_secret)
        else:
            options.update(resume_code=7, resume_stdout=stream_with_secret)
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run([], **options))

        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        result = json.loads(captured.out)
        assert result["error_code"] == expected_error
        assert transcript_marker not in captured.out
        assert stderr_marker not in captured.out


def test_main_emits_one_json_line_and_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CW_CODEX_LIVE_SESSION_SMOKE", raising=False)
    assert probe.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error_code"] == "opt_in_required"


def test_main_help_is_the_documented_human_readable_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        probe.main(["--help"])
    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert captured.out.startswith("usage:")
    assert "--model" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_main_maps_parent_signals_to_sanitized_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signum: signal.Signals,
) -> None:
    previous_handler = signal.getsignal(signum)

    def raise_signal(_model: str | None) -> probe.SmokeResult:
        signal.raise_signal(signum)
        message = "the installed signal handler should interrupt"
        raise AssertionError(message)

    monkeypatch.setattr(probe, "run_probe", raise_signal)
    assert probe.main(["--model", MODEL]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == {
        "status": "failed",
        "cli_version": None,
        "model": None,
        "session_id": None,
        "create": None,
        "resume": None,
        "error_code": "repo_setup_failed",
    }
    assert signal.getsignal(signum) is previous_handler
