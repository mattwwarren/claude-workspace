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
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from scripts import probe_codex_live_session as probe

MODEL = "gpt-5.6-luna"
SESSION = "thread.smoke-2463"
VERSION = "codex-cli 0.156.1"
EXPECTED_CREATE_PROMPT = (
    "Reply exactly cw-session-smoke-ok. Do not use tools or modify files."
)
EXPECTED_RESUME_PROMPT = (
    "Reply exactly cw-session-smoke-resumed-ok. Do not use tools or modify files."
)
EXPECTED_ERROR_CODES = {
    "opt_in_required",
    "invalid_model",
    "version_unavailable",
    "version_invalid",
    "cli_unavailable",
    "repo_setup_failed",
    "internal_error",
    "create_timeout",
    "create_nonzero_exit",
    "create_malformed_jsonl",
    "create_missing_thread",
    "create_invalid_thread_id",
    "create_duplicate_thread_started",
    "create_invalid_terminal",
    "resume_timeout",
    "resume_nonzero_exit",
    "resume_malformed_jsonl",
    "resume_missing_thread",
    "resume_invalid_thread_id",
    "resume_duplicate_thread_started",
    "resume_id_mismatch",
    "resume_invalid_terminal",
    "cleanup_failed",
}
EXPECTED_CHILD_ENV_KEYS = frozenset(
    {
        "path",
        "home",
        "userprofile",
        "systemroot",
        "windir",
        "openai_api_key",
        "codex_api_key",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "ssl_cert_file",
        "ssl_cert_dir",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "lang",
        "lc_all",
        "lc_ctype",
        "git_config_nosystem",
    }
)
UNAVAILABLE_MESSAGE = "unavailable-secret"
_REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the probe's snap-visible temporary root inside pytest's sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)


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


@dataclass(frozen=True)
class FakeRunOptions:
    create_stdout: str = field(default_factory=stream)
    resume_stdout: str = field(default_factory=stream)
    expected_resume_id: str = SESSION
    create_code: int = 0
    resume_code: int = 0
    git_code: int = 0
    timeout_stage: str | None = None
    unavailable_stage: str | None = None
    stderr: str = ""
    real_git: bool = False
    system_git_config: Path | None = None


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
    options: FakeRunOptions | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    options = options or FakeRunOptions()

    def run_git_init(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if options.timeout_stage == "git":
            raise subprocess.TimeoutExpired(argv, 0)
        if not options.real_git:
            return completed(argv, code=options.git_code, stderr=options.stderr)
        assert cwd.is_dir()
        git_env = dict(env)
        if options.system_git_config is not None:
            git_env["GIT_CONFIG_SYSTEM"] = str(options.system_git_config)
        proc = _REAL_POPEN(
            argv,
            cwd=cwd,
            env=git_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=probe.PROCESS_TIMEOUT_SECONDS)
        if options.system_git_config is not None:
            assert not (cwd / "cw-system-template-marker").exists()
        return completed(argv, code=cast("int", proc.returncode))

    def run_resume(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        assert argv == [
            "codex",
            "exec",
            "resume",
            options.expected_resume_id,
            "--json",
            "--ignore-user-config",
            "--model",
            MODEL,
            EXPECTED_RESUME_PROMPT,
        ]
        if options.real_git:
            assert (cwd / ".git").is_dir()
        if options.unavailable_stage == "resume":
            raise FileNotFoundError(UNAVAILABLE_MESSAGE)
        if options.timeout_stage == "resume":
            raise subprocess.TimeoutExpired(argv, 0)
        return completed(
            argv,
            code=options.resume_code,
            stdout=options.resume_stdout,
            stderr=options.stderr,
        )

    def run_create(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if options.real_git:
            assert (cwd / ".git").is_dir()
        if options.unavailable_stage == "create":
            raise FileNotFoundError(UNAVAILABLE_MESSAGE)
        if options.timeout_stage == "create":
            raise subprocess.TimeoutExpired(argv, 0)
        return completed(
            argv,
            code=options.create_code,
            stdout=options.create_stdout,
            stderr=options.stderr,
        )

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
            return completed(argv, stdout=f"{VERSION}\n", stderr=options.stderr)
        if argv == ["git", "init", "--quiet"]:
            return run_git_init(argv, cwd=cwd, env=env)
        if argv[:3] == ["codex", "exec", "resume"]:
            return run_resume(argv, cwd=cwd)
        if argv == [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--model",
            MODEL,
            EXPECTED_CREATE_PROMPT,
        ]:
            return run_create(argv, cwd=cwd)
        message = f"unexpected subprocess argv: {argv!r}"
        raise AssertionError(message)

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
        return True
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            process_stat = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            process_stat = ""
        except OSError:
            process_stat = "?"
        return _proc_stat_is_running(process_stat)
    return True


def test_proc_stat_liveness_treats_zombies_as_exited() -> None:
    assert not _proc_stat_is_running("123 (codex smoke (reader)) Z 1 2 3")
    assert _proc_stat_is_running("123 (codex smoke (reader)) S 1 2 3")
    assert not _proc_stat_is_running("")
    assert _proc_stat_is_running("?")


def _proc_stat_is_running(process_stat: str) -> bool:
    if process_stat == "":
        return False
    if process_stat == "?":
        return True
    # The command name is parenthesized and may itself contain spaces or
    # parentheses; the state is the first field after the final `)`.
    fields = process_stat[process_stat.rfind(")") + 1 :].split()
    return not (fields and fields[0] in {"Z", "X"})


def wait_for_process_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not process_is_running(pid)


def cleanup_process(pid: int | None) -> None:
    if pid is None:
        return
    if process_is_running(pid):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert wait_for_process_exit(pid)


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
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["HTTPS_PROXY"] == "https://proxy.example:8443"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "LC_SECRET" not in env
    assert "CODEX_SANDBOX" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert "NODE_OPTIONS" not in env
    if call["argv"] in (
        ["git", "init", "--quiet"],
        ["codex", "--version"],
    ):
        assert "CODEX_HOME" not in env
        assert "CODEX_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
    else:
        assert env["CODEX_HOME"] == str(configured_codex_home)
        assert env["CODEX_API_KEY"] == "auth-material-secret"
        assert env["OPENAI_API_KEY"] == "openai-auth-secret"
    assert all(
        key.casefold() in EXPECTED_CHILD_ENV_KEYS
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
                "malformed_jsonl",
            ),
            (
                json.dumps({"type": "thread.started", "thread_id": SESSION}) + "\n",
                "invalid_terminal",
            ),
        ],
    )
    def test_structural_errors(self, payload: str, error: str) -> None:
        assert probe._parse_stream(payload).error == error

    @pytest.mark.parametrize(
        "event_type",
        [
            "turn.started",
            "item.started",
            "item.updated",
            "item.completed",
            "future.notice",
        ],
    )
    def test_nonterminal_event_before_thread_is_rejected(self, event_type: str) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": event_type},
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "malformed_jsonl"

    def test_success_ignores_blank_and_unrelated_events(self) -> None:
        parsed = probe._parse_stream("\n" + stream())
        assert parsed == probe.ParsedStream(SESSION, "turn.completed", None)

    def test_failed_turn_is_terminal_but_not_success(self) -> None:
        assert (
            probe._parse_stream(stream(terminal="turn.failed")).terminal_event
            == "turn.failed"
        )

    @pytest.mark.parametrize(
        "session", ["", ".bad", "_bad", "-bad", "bad/id", "x" * 129]
    )
    def test_invalid_thread_ids_are_rejected(self, session: str) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": session},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "invalid_thread_id"

    def test_maximum_length_thread_id_is_accepted(self) -> None:
        session = "a" + "x" * 127
        assert probe._parse_stream(stream(session)) == probe.ParsedStream(
            session, "turn.completed", None
        )

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
        assert parsed.error == "malformed_jsonl"

    def test_future_unrelated_nonterminal_event_is_ignored(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "future.notice", "detail": "ignored"},
                {"type": "turn.completed"},
            )
        )
        assert parsed == probe.ParsedStream(SESSION, "turn.completed", None)

    def test_error_event_before_failed_turn_is_structurally_valid(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": "Codex failure"},
                {"type": "turn.failed"},
            )
        )
        assert parsed == probe.ParsedStream(SESSION, "turn.failed", None)

    @pytest.mark.parametrize(
        "events",
        [
            ({"type": "error", "message": "Codex failure"},),
            (
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error"},
            ),
            (
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": 7},
            ),
            (
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": "first"},
                {"type": "error", "message": "second"},
            ),
        ],
    )
    def test_malformed_error_events_are_rejected(
        self, events: tuple[dict[str, object], ...]
    ) -> None:
        assert probe._parse_stream(jsonl(*events)).error == "malformed_jsonl"

    def test_error_event_does_not_allow_a_success_terminal(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": "Codex failure"},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "invalid_terminal"

    def test_unknown_terminal_suffix_is_rejected(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "session.completed"},
                {"type": "turn.completed"},
            )
        )
        assert parsed.error == "malformed_jsonl"


class TestProbe:
    @pytest.mark.parametrize("model", [None, MODEL])
    @pytest.mark.parametrize("opt_in", [None, "", "0", "true"])
    def test_opt_out_is_exact_and_launches_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: str | None,
        opt_in: str | None,
    ) -> None:
        if opt_in is None:
            monkeypatch.delenv("CW_CODEX_LIVE_SESSION_SMOKE", raising=False)
        else:
            monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", opt_in)
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(model) == {
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

    @pytest.mark.parametrize("model", [".leading-model", "_leading-model"])
    def test_model_pattern_accepts_dot_or_underscore_prefix(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        expected = probe._result(
            probe.STATUS_PASSED,
            cli_version=VERSION,
            model=model,
            error_code=None,
        )

        def run_disposable(*, model: str) -> probe.SmokeResult:
            assert model == expected["model"]
            return expected

        monkeypatch.setattr(probe, "_run_disposable", run_disposable)
        assert probe.run_probe(model) == expected

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

    @pytest.mark.parametrize("argv", [[], ["--help"]])
    def test_main_missing_model_and_help_emit_one_json_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
        set_runner(monkeypatch, lambda *_args, **_kwargs: pytest.fail("launched"))
        assert probe.main(argv) == 1
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
            "error_code": "invalid_model",
        }

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
            EXPECTED_CREATE_PROMPT,
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
            EXPECTED_RESUME_PROMPT,
        ]
        assert calls[0]["cwd"] == calls[1]["cwd"] == calls[2]["cwd"] == calls[3]["cwd"]
        checkout = probe._checkout_root()
        for call in calls:
            assert_isolated_child_environment(call, checkout, configured_codex_home)
        assert probe.PROCESS_TIMEOUT_SECONDS == 120
        assert len(wait_timeouts) == len(calls)
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

    def test_create_session_id_is_passed_verbatim_to_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created_id = "thread.created-session-2463"
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(
            monkeypatch,
            fake_run(
                calls,
                FakeRunOptions(
                    create_stdout=stream(created_id),
                    resume_stdout=stream(created_id),
                    expected_resume_id=created_id,
                ),
            ),
        )

        result = probe.run_probe(MODEL)

        assert result["status"] == "passed"
        assert result["session_id"] == created_id
        resume_argv = cast("list[str]", calls[3]["argv"])
        assert resume_argv[3] == created_id

    def test_git_init_ignores_system_init_template(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        template_dir = tmp_path / "system-template"
        template_dir.mkdir()
        (template_dir / "cw-system-template-marker").write_text(
            "must not be copied", encoding="utf-8"
        )
        system_config = tmp_path / "system-gitconfig"
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(system_config),
                "init.templateDir",
                str(template_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(
            monkeypatch,
            fake_run(
                calls,
                FakeRunOptions(real_git=True, system_git_config=system_config),
            ),
        )

        result = probe.run_probe(MODEL)

        assert result["status"] == "passed"
        git_call = next(
            call for call in calls if call["argv"] == ["git", "init", "--quiet"]
        )
        assert cast("dict[str, str]", git_call["env"])["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_process_wait_uses_full_configured_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        class AdvancingProcess:
            pid = None

            def __init__(self, clock: Clock) -> None:
                self.clock = clock
                self.killed = False
                self.wait_timeouts: list[float | None] = []

            def wait(self, timeout: float | None = None) -> int:
                self.wait_timeouts.append(timeout)
                if self.killed:
                    return -9
                assert timeout is not None
                self.clock.now += timeout
                raise subprocess.TimeoutExpired(["codex"], timeout)

            def kill(self) -> None:
                self.killed = True

        clock = Clock()
        process = AdvancingProcess(clock)
        monkeypatch.setattr(probe, "time", clock)
        reader: Future[None] = Future()
        reader.set_result(None)
        outcome = probe._wait_for_process(
            cast("subprocess.Popen[bytes]", process),
            reader,
            probe._OutputCapture(bytearray()),
        )

        assert probe.PROCESS_TIMEOUT_SECONDS == 120
        assert probe.PROCESS_POLL_INTERVAL_SECONDS == 0.25
        assert outcome == (-9, True, False, False)
        process_waits = [
            timeout for timeout in process.wait_timeouts[:-1] if timeout is not None
        ]
        assert sum(process_waits) == pytest.approx(120)
        assert process.wait_timeouts[-1] == probe.PROCESS_CLEANUP_TIMEOUT_SECONDS

    def test_process_cleanup_retries_wait_until_child_is_reaped(
        self,
    ) -> None:
        class DelayedReapProcess:
            pid = None

            def __init__(self) -> None:
                self.wait_attempts = 0
                self.kill_attempts = 0

            def wait(self, timeout: float | None = None) -> int:
                assert timeout == probe.PROCESS_CLEANUP_TIMEOUT_SECONDS
                self.wait_attempts += 1
                if self.wait_attempts == 1:
                    assert timeout is not None
                    raise subprocess.TimeoutExpired(["codex"], timeout)
                return -9

            def kill(self) -> None:
                self.kill_attempts += 1

        process = DelayedReapProcess()
        outcome = probe._kill_and_wait(
            cast("subprocess.Popen[bytes]", process), timed_out=True
        )

        assert outcome == (-9, True, False, False)
        assert process.wait_attempts == 2
        assert process.kill_attempts == 2

    def test_exhausted_process_cleanup_maps_to_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class NeverReapedProcess:
            pid = None
            stdout = io.BytesIO()

            def wait(self, timeout: float | None = None) -> int:
                assert timeout == probe.PROCESS_CLEANUP_TIMEOUT_SECONDS
                raise subprocess.TimeoutExpired(["codex"], timeout)

            def kill(self) -> None:
                pass

        monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(
            probe,
            "_launch_process",
            lambda *_args, **_kwargs: NeverReapedProcess(),
        )
        monkeypatch.setattr(probe, "PROCESS_CLEANUP_ATTEMPTS", 2)
        attempt = probe._attempt(
            ["codex", "exec"],
            cwd=Path.cwd(),
            env={},
            stage=probe.ProbeStage.CREATE,
        )
        assert attempt.outcome.timed_out
        assert attempt.outcome.cleanup_failed
        assert attempt.error_code == probe.ERROR_INTERNAL_ERROR

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
            fake_run(calls, FakeRunOptions(create_code=create_code, real_git=True)),
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
            stage=probe.ProbeStage.CREATE,
        )
        assert attempt.error_code == "create_malformed_jsonl"
        assert attempt.outcome.output_exceeded_limit
        assert attempt.outcome.stdout == ""
        assert attempt.stream.error == "malformed_jsonl"

    @pytest.mark.skipif(os.name != "posix", reason="pthread signal masks are POSIX")
    def test_launch_child_inherits_parent_interrupt_signal_mask(
        self, tmp_path: Path
    ) -> None:
        parent_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        code = (
            "import signal; "
            "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set()); "
            "print(f'{signal.SIGINT in blocked},{signal.SIGTERM in blocked}')"
        )
        outcome = probe._run_process(
            [sys.executable, "-c", code], cwd=tmp_path, env=os.environ.copy()
        )
        assert outcome.exit_code == 0
        expected = f"{signal.SIGINT in parent_mask},{signal.SIGTERM in parent_mask}"
        assert outcome.stdout.strip() == expected

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    def test_timeout_kills_and_reaps_real_process_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        code = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "print(f'DESCENDANT_PID={child.pid}', flush=True); "
            "time.sleep(30)"
        )
        monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 1)
        pid: int | None = None
        started = time.monotonic()
        try:
            outcome = probe._run_process(
                [sys.executable, "-c", code], cwd=tmp_path, env=os.environ.copy()
            )
            elapsed = time.monotonic() - started
            pid_line = next(
                line
                for line in outcome.stdout.splitlines()
                if line.startswith("DESCENDANT_PID=")
            )
            pid = int(pid_line.split("=", 1)[1])
            assert outcome.timed_out
            assert outcome.exit_code is not None
            assert elapsed < 10
            assert wait_for_process_exit(pid)
        finally:
            cleanup_process(pid)

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
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
            assert elapsed < 10
            assert wait_for_process_exit(pid)
        finally:
            cleanup_process(pid)

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
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

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    def test_interrupt_during_launch_kills_and_reaps_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        child_pids: list[int] = []

        def interrupt_parent(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        def launch_then_interrupt(
            argv: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            stdin: int | None,
            stdout: int | None,
            stderr: int | None,
            start_new_session: bool,
        ) -> subprocess.Popen[bytes]:
            child = _REAL_POPEN(
                argv,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=start_new_session,
            )
            child_pids.append(child.pid)
            os.kill(os.getpid(), signal.SIGINT)
            return child

        previous_handler = signal.signal(signal.SIGINT, interrupt_parent)
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.subprocess.Popen", launch_then_interrupt
        )
        try:
            outcome = probe._run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                env=os.environ.copy(),
            )
            assert outcome.timed_out
            assert len(child_pids) == 1
            assert wait_for_process_exit(child_pids[0])
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            for child_pid in child_pids:
                cleanup_process(child_pid)

    def test_signal_during_handler_restoration_kills_launched_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class DeferredSignalContext:
            def __init__(self, handler: probe._SignalHandler) -> None:
                self.handler = cast("Callable[[int, object | None], object]", handler)

            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> None:
                self.handler(signal.SIGTERM, None)

        class Child:
            pid = None
            stdout = None

            def __init__(self) -> None:
                self.killed = False

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                assert timeout == probe.PROCESS_CLEANUP_TIMEOUT_SECONDS
                if not self.killed:
                    raise subprocess.TimeoutExpired(["codex"], timeout)
                return -9

        child = Child()
        monkeypatch.setattr(
            probe,
            "_install_parent_signal_handlers",
            DeferredSignalContext,
        )
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.subprocess.Popen",
            lambda *_args, **_kwargs: child,
        )

        outcome = probe._launch_process(["codex", "--version"], cwd=tmp_path, env={})

        assert isinstance(outcome, probe.ProcessOutcome)
        assert outcome.timed_out
        assert not outcome.cleanup_failed
        assert child.killed
        assert outcome.exit_code == -9

    @pytest.mark.parametrize(
        ("options", "error"),
        [
            (
                FakeRunOptions(create_stdout="garbage\n"),
                "create_malformed_jsonl",
            ),
            (
                FakeRunOptions(
                    create_stdout=jsonl(
                        {"type": "thread.started", "thread_id": SESSION},
                        {"type": "turn.cancelled"},
                    )
                ),
                "create_malformed_jsonl",
            ),
            (
                FakeRunOptions(
                    create_stdout=jsonl(
                        {"type": "item.completed"},
                        {"type": "thread.started", "thread_id": SESSION},
                        {"type": "turn.completed"},
                    )
                ),
                "create_malformed_jsonl",
            ),
            (
                FakeRunOptions(create_stdout=jsonl({"type": "turn.completed"})),
                "create_missing_thread",
            ),
            (
                FakeRunOptions(create_stdout=stream("bad/id")),
                "create_invalid_thread_id",
            ),
            (
                FakeRunOptions(
                    create_stdout=stream() + jsonl({"type": "thread.started"})
                ),
                "create_duplicate_thread_started",
            ),
            (
                FakeRunOptions(create_stdout=stream(terminal="turn.failed")),
                "create_invalid_terminal",
            ),
            (FakeRunOptions(create_code=9), "create_nonzero_exit"),
            (FakeRunOptions(timeout_stage="create"), "create_timeout"),
            (FakeRunOptions(unavailable_stage="create"), "cli_unavailable"),
            (FakeRunOptions(git_code=9), "repo_setup_failed"),
            (FakeRunOptions(timeout_stage="git"), "repo_setup_failed"),
        ],
    )
    def test_create_failures_are_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        options: FakeRunOptions,
        error: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        if options.timeout_stage is not None:
            monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 0.01)
        set_runner(monkeypatch, fake_run(calls, options))
        result = probe.run_probe(MODEL)
        assert result["status"] == "failed"
        assert result["error_code"] == error
        assert result["resume"] is None
        assert "TimeoutExpired" not in json.dumps(result)
        assert UNAVAILABLE_MESSAGE not in json.dumps(result)
        if options.unavailable_stage == "create":
            assert result["create"] is not None
            assert result["create"]["exit_code"] is None

    @pytest.mark.parametrize(
        ("capture_failure", "expected_error"),
        [
            ("reader_failed", "internal_error"),
            ("cleanup_failed", "internal_error"),
            ("output_exceeded_limit", "repo_setup_failed"),
            ("reader_incomplete", "repo_setup_failed"),
        ],
    )
    def test_git_setup_capture_failures_stop_before_codex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture_failure: str,
        expected_error: str,
    ) -> None:
        calls: list[list[str]] = []

        def run_process(
            argv: list[str], *, cwd: Path, env: dict[str, str]
        ) -> probe.ProcessOutcome:
            del cwd, env
            calls.append(argv)
            if argv == ["codex", "--version"]:
                return probe.ProcessOutcome(0, f"{VERSION}\n", False)
            if argv == ["git", "init", "--quiet"]:
                return probe.ProcessOutcome(
                    0,
                    "",
                    False,
                    output_exceeded_limit=capture_failure == "output_exceeded_limit",
                    reader_incomplete=capture_failure == "reader_incomplete",
                    reader_failed=capture_failure == "reader_failed",
                    cleanup_failed=capture_failure == "cleanup_failed",
                )
            pytest.fail(f"unexpected process launch: {argv!r}")

        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(probe, "_run_process", run_process)
        result = probe.run_probe(MODEL)
        assert result["error_code"] == expected_error
        assert calls == [["codex", "--version"], ["git", "init", "--quiet"]]

    @pytest.mark.parametrize(
        ("options", "error"),
        [
            (
                FakeRunOptions(resume_stdout="garbage\n"),
                "resume_malformed_jsonl",
            ),
            (
                FakeRunOptions(
                    resume_stdout=jsonl(
                        {"type": "thread.started", "thread_id": SESSION},
                        {"type": "session.completed"},
                    )
                ),
                "resume_malformed_jsonl",
            ),
            (
                FakeRunOptions(resume_stdout=jsonl({"type": "turn.completed"})),
                "resume_missing_thread",
            ),
            (
                FakeRunOptions(resume_stdout=stream("bad/id")),
                "resume_invalid_thread_id",
            ),
            (
                FakeRunOptions(
                    resume_stdout=stream() + jsonl({"type": "thread.started"})
                ),
                "resume_duplicate_thread_started",
            ),
            (
                FakeRunOptions(resume_stdout=stream(terminal="turn.failed")),
                "resume_invalid_terminal",
            ),
            (FakeRunOptions(resume_code=9), "resume_nonzero_exit"),
            (FakeRunOptions(timeout_stage="resume"), "resume_timeout"),
            (FakeRunOptions(unavailable_stage="resume"), "cli_unavailable"),
            (
                FakeRunOptions(resume_stdout=stream("other-thread")),
                "resume_id_mismatch",
            ),
        ],
    )
    def test_resume_failures_are_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        options: FakeRunOptions,
        error: str,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        if options.timeout_stage is not None:
            monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 0.01)
        set_runner(monkeypatch, fake_run(calls, options))
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
        assert result["resume"] is not None
        assert result["resume"]["id_matches"] == (
            error in {"resume_invalid_terminal", "resume_nonzero_exit"}
        )
        if options.unavailable_stage == "resume":
            assert result["resume"]["exit_code"] is None

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

    def test_version_cleanup_failure_is_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            probe,
            "_run_process",
            lambda *_args, **_kwargs: probe.ProcessOutcome(
                None, "", True, cleanup_failed=True
            ),
        )
        assert probe._version_result(cwd=Path.cwd(), env={}) == probe._VersionFailure(
            probe.ERROR_INTERNAL_ERROR
        )

    @pytest.mark.parametrize(
        "stdout",
        [
            "codex-cli 0.156.1\n\n",
            "codex-cli 0.156.1\nsecret-transcript\n",
            "codex-cli 0.156.1" + "x" * 65,
        ],
    )
    def test_version_requires_one_valid_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        stdout: str,
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
        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        assert "auth-secret" not in captured.out
        assert "secret-transcript" not in captured.out
        assert "codex-cli 0.156.1" not in captured.out
        assert json.loads(captured.out)["error_code"] == "version_invalid"
        assert calls == [["codex", "--version"]]

    def test_version_timeout_and_nonzero_are_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 0.01)
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
    def test_temporary_parent_inside_checkout_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        operator_home = tmp_path / "operator-home"
        cache_directory = operator_home / ".cache"
        cache_directory.mkdir(parents=True)
        (cache_directory / "cw-live-tests").symlink_to(
            probe._checkout_root(), target_is_directory=True
        )
        monkeypatch.setenv("HOME", str(operator_home))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            forbid_temporary_directory,
        )
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
    def test_codex_home_sessions_symlink_outside_checkout_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        external_sessions = tmp_path / "session-store"
        external_sessions.mkdir()
        (codex_home / probe.CODEX_SESSION_ARTIFACT_DIR).symlink_to(
            external_sessions, target_is_directory=True
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        assert probe._codex_home(probe._checkout_root()) == codex_home.resolve()

    def test_codex_home_defaults_to_home_dot_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEX_HOME", raising=False)
        assert probe._codex_home(probe._checkout_root()) == Path.home() / ".codex"

    def test_codex_home_expands_operator_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", "~/codex-state")
        assert (
            probe._codex_home(probe._checkout_root())
            == (Path.home() / "codex-state").resolve()
        )

    def test_invalid_utf8_is_replaced_at_process_boundary(self, tmp_path: Path) -> None:
        result = probe._run_process(
            [sys.executable, "-c", "import os; os.write(1, b'\\xff')"],
            cwd=tmp_path,
            env=os.environ.copy(),
        )
        assert result.exit_code == 0
        assert result.stdout == "\ufffd"

    def test_unexpected_stdout_reader_error_is_sanitized_and_propagated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        failure_marker = "private-reader-exception"

        def fail_reader() -> None:
            raise LookupError(failure_marker)

        monkeypatch.setattr(probe, "_make_stdout_reader", lambda *_args: fail_reader)
        monkeypatch.setattr(probe, "PROCESS_TIMEOUT_SECONDS", 5)
        started = time.monotonic()
        outcome = probe._run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=os.environ.copy(),
        )
        assert outcome.reader_failed
        assert outcome.exit_code is not None
        assert time.monotonic() - started < 3
        assert failure_marker not in capsys.readouterr().err

        monkeypatch.setattr(
            probe,
            "_run_process",
            lambda *_args, **_kwargs: outcome,
        )
        with pytest.raises(
            RuntimeError, match="unexpected probe stdout reader failure"
        ):
            probe._attempt(
                ["codex", "--version"],
                cwd=Path.cwd(),
                env={},
                stage=probe.ProbeStage.CREATE,
            )

    def test_finished_parent_reader_error_still_kills_process_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeProcess:
            stdout = io.BytesIO()
            pid = 2463

        failure = LookupError("reader failure")
        reader: Future[None] = Future()
        reader.set_exception(failure)
        capture = probe._OutputCapture(bytearray())
        killed: list[object] = []
        monkeypatch.setattr(probe, "_kill_process_group", killed.append)

        assert probe._finish_stdout_capture(
            cast("subprocess.Popen[bytes]", FakeProcess()), reader, capture
        ) == ("", False)
        assert capture.reader_failed
        assert len(killed) == 1

    def test_base_exception_from_reader_stays_on_interrupt_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        def interrupt_reader() -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(
            probe, "_make_stdout_reader", lambda *_args: interrupt_reader
        )
        outcome = probe._run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=os.environ.copy(),
        )

        assert outcome.timed_out
        assert not outcome.reader_failed
        assert outcome.exit_code is not None
        assert capsys.readouterr().err == ""

    def test_process_kill_failure_is_not_silenced(self) -> None:
        failure_message = "kill denied"

        class BrokenProcess:
            pid = None

            def kill(self) -> None:
                raise PermissionError(failure_message)

        with pytest.raises(PermissionError, match=failure_message):
            probe._kill_process_group(cast("subprocess.Popen[bytes]", BrokenProcess()))

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
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
            assert elapsed < 10
            assert wait_for_process_exit(pid)
        finally:
            cleanup_process(pid)

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX sessions")
    def test_detached_stdout_writer_cannot_block_probe_shutdown(
        self, tmp_path: Path
    ) -> None:
        code = (
            "import subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'], start_new_session=True); "
            "print(f'DETACHED_PID={child.pid}', flush=True); "
            "print('parent-finished', flush=True)"
        )
        pid: int | None = None
        try:
            started = time.monotonic()
            result = probe._run_process(
                [sys.executable, "-c", code], cwd=tmp_path, env=os.environ.copy()
            )
            elapsed = time.monotonic() - started
            pid_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("DETACHED_PID=")
            )
            pid = int(pid_line.split("=", 1)[1])
            assert result.exit_code == 0
            assert result.reader_incomplete
            assert "parent-finished" in result.stdout
            assert process_is_running(pid)
            assert elapsed < 10
        finally:
            cleanup_process(pid)
        deadline = time.monotonic() + 1
        while any(thread.name == "cw-codex-stdout" for thread in threading.enumerate()):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert not any(
            thread.name == "cw-codex-stdout" for thread in threading.enumerate()
        )

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
            fake_run(
                calls,
                FakeRunOptions(create_code=create_code, stderr="stderr-secret"),
            ),
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
            "error_code": probe.ERROR_CLEANUP_FAILED,
        }
        if create_code == 0:
            expected["resume"] = {
                "exit_code": 0,
                "terminal_event": "turn.completed",
                "id_matches": True,
            }
        assert result == expected
        assert "cleanup-secret" not in json.dumps(result)

    def test_interrupt_during_cleanup_is_deferred_until_directory_is_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        temporary_roots: list[Path] = []
        real_temporary_directory = tempfile.TemporaryDirectory

        class InterruptingTemporaryDirectory:
            def __init__(self, **kwargs: str) -> None:
                assert set(kwargs) == {"prefix", "dir"}
                self._directory = real_temporary_directory(
                    prefix=kwargs["prefix"], dir=kwargs["dir"]
                )
                self.name = self._directory.name
                temporary_roots.append(Path(self.name))

            def cleanup(self) -> None:
                signal.raise_signal(signal.SIGINT)
                self._directory.cleanup()

        calls: list[dict[str, object]] = []
        previous_handler = signal.getsignal(signal.SIGINT)
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            InterruptingTemporaryDirectory,
        )
        set_runner(monkeypatch, fake_run(calls))

        result = probe.run_probe(MODEL)

        assert result["status"] == "failed"
        assert result["error_code"] == "repo_setup_failed"
        assert result["session_id"] == SESSION
        assert len(temporary_roots) == 1
        assert not temporary_roots[0].exists()
        assert signal.getsignal(signal.SIGINT) is previous_handler

    def test_failed_cli_probe_exits_nonzero_with_one_sanitized_json_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run([], FakeRunOptions(create_code=7)))
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
                FakeRunOptions(
                    create_stdout=create_stdout,
                    resume_stdout=create_stdout,
                    stderr=auth,
                ),
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
        options = FakeRunOptions(stderr=stderr_marker)
        if stage == "create":
            options = FakeRunOptions(
                create_code=7,
                create_stdout=stream_with_secret,
                stderr=stderr_marker,
            )
        else:
            options = FakeRunOptions(
                resume_code=7,
                resume_stdout=stream_with_secret,
                stderr=stderr_marker,
            )
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run([], options))

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


def test_signal_restore_errors_are_raised_after_all_handlers_are_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signums = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {signum: signal.getsignal(signum) for signum in signums}
    real_signal = signal.signal

    def fail_after_sigint_restore(
        signum: signal.Signals, handler: probe._SignalHandler
    ) -> probe._SignalHandler:
        previous = real_signal(signum, handler)
        if signum is signal.SIGINT and handler is previous_handlers[signum]:
            message = "private restoration failure"
            raise OSError(message)
        return previous

    monkeypatch.setattr(signal, "signal", fail_after_sigint_restore)
    with (
        pytest.raises(ExceptionGroup, match="failed to restore parent signal handlers"),
        probe._install_parent_signal_handlers(lambda _signum, _frame: None),
    ):
        pass

    assert {signum: signal.getsignal(signum) for signum in signums} == previous_handlers


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


def test_main_does_not_mask_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exception_marker = "private-operational-marker"

    def raise_unexpected(_model: str | None) -> probe.SmokeResult:
        raise RuntimeError(exception_marker)

    monkeypatch.setattr(probe, "run_probe", raise_unexpected)
    with pytest.raises(RuntimeError, match=exception_marker):
        probe.main(["--model", MODEL])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_unhandled_exception_hook_emits_sanitized_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exception_marker = "private-operational-marker"
    probe._reset_unhandled_exception_result()
    probe._handle_unhandled_exception(
        RuntimeError, RuntimeError(exception_marker), None
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert exception_marker not in captured.out
    result = json.loads(captured.out)
    assert result == {
        "status": "failed",
        "cli_version": None,
        "model": None,
        "session_id": None,
        "create": None,
        "resume": None,
        "error_code": "internal_error",
    }
    assert {error_code.value for error_code in probe.ErrorCode} == EXPECTED_ERROR_CODES


def test_entrypoint_converts_unhandled_exception_to_one_json_result() -> None:
    exception_marker = "private-operational-marker"
    code = "\n".join(
        [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location(",
            "    'probe_entrypoint_test', sys.argv[1]",
            ")",
            "assert spec is not None and spec.loader is not None",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "module._run_disposable = lambda *, model: module._run_session(",
            "    worktree=module.Path.cwd(), codex_env={}, git_env={},",
            "    cli_version='codex-cli 1.2.3', model=model)",
            "module._run_process = lambda *a, **k: module.ProcessOutcome(0, '', False)",
            "def attempt(argv, *, cwd, env, stage):",
            "    if stage == 'create':",
            "        return module._Attempt(",
            "            module.ProcessOutcome(0, '', False),",
            "            module.ParsedStream('thread.smoke-2463',",
            "                module.TURN_COMPLETED_EVENT, None), None)",
            f"    raise RuntimeError({exception_marker!r})",
            "module._attempt = attempt",
            "sys.argv = ['probe', '--model', 'gpt-5.6-luna']",
            "module._entrypoint()",
        ]
    )
    child_env = os.environ.copy()
    child_env["CW_CODEX_LIVE_SESSION_SMOKE"] = "1"
    completed_process = subprocess.run(
        [sys.executable, "-c", code, str(Path(probe.__file__))],
        capture_output=True,
        check=False,
        text=True,
        env=child_env,
        timeout=5,
    )
    assert completed_process.returncode == 1
    assert completed_process.stderr == ""
    assert completed_process.stdout.count("\n") == 1
    assert exception_marker not in completed_process.stdout
    assert json.loads(completed_process.stdout) == {
        "status": "failed",
        "cli_version": "codex-cli 1.2.3",
        "model": MODEL,
        "session_id": SESSION,
        "create": {"exit_code": 0, "terminal_event": "turn.completed"},
        "resume": None,
        "error_code": "internal_error",
    }


def test_entrypoint_reports_cleanup_failure_over_unhandled_exception() -> None:
    exception_marker = "private-operational-marker"
    cleanup_marker = "private-cleanup-marker"
    code = "\n".join(
        [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location(",
            "    'probe_cleanup_entrypoint_test', sys.argv[1]",
            ")",
            "assert spec is not None and spec.loader is not None",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "class BrokenTemporaryDirectory:",
            "    name = '/tmp/cw-codex2463-cleanup-failure'",
            "    def cleanup(self):",
            f"        raise OSError({cleanup_marker!r})",
            "module._codex_home = lambda *_args: module.Path('/tmp/codex-home')",
            (
                "module._new_temporary_directory = lambda _checkout: "
                "BrokenTemporaryDirectory()"
            ),
            "def fail_directory(**kwargs):",
            "    module._record_unhandled_exception_progress(",
            "        cli_version='codex-cli 1.2.3', model=kwargs['model'],",
            "        session_id='thread.smoke-2463',",
            "        create={'exit_code': 0,",
            "            'terminal_event': module.TURN_COMPLETED_EVENT})",
            f"    raise RuntimeError({exception_marker!r})",
            "module._run_in_disposable_directory = fail_directory",
            "sys.argv = ['probe', '--model', 'gpt-5.6-luna']",
            "module._entrypoint()",
        ]
    )
    child_env = os.environ.copy()
    child_env["CW_CODEX_LIVE_SESSION_SMOKE"] = "1"
    completed_process = subprocess.run(
        [sys.executable, "-c", code, str(Path(probe.__file__))],
        capture_output=True,
        check=False,
        text=True,
        env=child_env,
        timeout=5,
    )
    assert completed_process.returncode == 1
    assert completed_process.stderr == ""
    assert completed_process.stdout.count("\n") == 1
    assert exception_marker not in completed_process.stdout
    assert cleanup_marker not in completed_process.stdout
    assert json.loads(completed_process.stdout) == {
        "status": "failed",
        "cli_version": "codex-cli 1.2.3",
        "model": MODEL,
        "session_id": SESSION,
        "create": {"exit_code": 0, "terminal_event": "turn.completed"},
        "resume": None,
        "error_code": "cleanup_failed",
    }
