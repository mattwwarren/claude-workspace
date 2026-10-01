"""Offline contract tests for the Codex CLI session smoke probe."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from scripts import probe_codex_live_session as probe

MODEL = "gpt-5.6-luna"
SESSION = "thread.smoke-2463"
VERSION = "codex-cli 0.156.1"


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    partial_create_stdout = cast(
        "str | bytes", options.get("partial_create_stdout", "")
    )
    stderr = cast("str", options.get("stderr", ""))

    def run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: int,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "stdin": stdin,
                "capture_output": capture_output,
                "text": text,
                "check": check,
                "timeout": timeout,
            }
        )
        if argv == ["codex", "--version"]:
            return completed(argv, stdout=f"{VERSION}\n", stderr=stderr)
        if argv[:2] == ["git", "init"]:
            if timeout_stage == "git":
                raise subprocess.TimeoutExpired(argv, timeout)
            return completed(argv, code=git_code, stderr=stderr)
        if argv == probe._resume_argv(SESSION, MODEL):
            if unavailable_stage == "resume":
                raise FileNotFoundError
            if timeout_stage == "resume":
                raise subprocess.TimeoutExpired(argv, timeout)
            return completed(
                argv, code=resume_code, stdout=resume_stdout, stderr=stderr
            )
        if argv == probe._create_argv(MODEL):
            if unavailable_stage == "create":
                raise FileNotFoundError
            if timeout_stage == "create":
                raise subprocess.TimeoutExpired(
                    argv, timeout, output=partial_create_stdout
                )
            return completed(
                argv, code=create_code, stdout=create_stdout, stderr=stderr
            )
        message = f"unexpected subprocess argv: {argv!r}"
        raise AssertionError(message)

    return run


def set_runner(monkeypatch: pytest.MonkeyPatch, runner: Callable[..., object]) -> None:
    monkeypatch.setattr("scripts.probe_codex_live_session.subprocess.run", runner)


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

    def test_success_ignores_unrelated_events_before_thread_started(self) -> None:
        parsed = probe._parse_stream(
            jsonl(
                {"type": "turn.started"},
                {"type": "item.started"},
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
            )
        )
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

    @pytest.mark.parametrize(
        ("events", "error"),
        [
            (({"type": "error"},), "malformed_jsonl"),
            (({"type": "error", "message": 7},), "malformed_jsonl"),
            (
                (
                    {"type": "error", "message": "failed"},
                    {"type": "error", "message": "again"},
                ),
                "malformed_jsonl",
            ),
            (
                (
                    {"type": "error", "message": "failed"},
                    {"type": "turn.completed"},
                ),
                "invalid_terminal",
            ),
        ],
    )
    def test_error_events_are_validated(
        self, events: tuple[dict[str, object], ...], error: str
    ) -> None:
        parsed = probe._parse_stream(
            jsonl({"type": "thread.started", "thread_id": SESSION}, *events)
        )
        assert parsed.error == error


class TestProbe:
    def test_opt_out_is_exact_and_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_CODEX_LIVE_SESSION_SMOKE", raising=False)
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
        "model", [None, "bad/model", "x" * 65, "read-only", "workspace-write"]
    )
    def test_invalid_model_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, model: str | None
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))
        assert probe.run_probe(model)["error_code"] == "invalid_model"

    def test_success_has_exact_commands_policy_cwd_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run(calls))
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
        assert len(calls) == 4
        assert calls[0]["cwd"] == calls[1]["cwd"] == calls[2]["cwd"] == calls[3]["cwd"]
        repo = cast("Path", calls[0]["cwd"])
        assert repo.name == "repo"
        assert not probe._is_within(repo, probe._checkout_root())
        assert not repo.parent.exists()
        assert all(call["timeout"] == 120 for call in calls)
        assert all(call["stdin"] == subprocess.DEVNULL for call in calls)
        assert all(
            call["capture_output"] and call["text"] and not call["check"]
            for call in calls
        )
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

    def test_existing_codex_home_is_used_and_never_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / "existing-codex-home"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        sentinel = sessions / "existing-session.jsonl"
        sentinel.write_text("keep this session", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
        monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        calls: list[dict[str, object]] = []
        set_runner(monkeypatch, fake_run(calls))

        result = probe.run_probe(MODEL)

        assert result["status"] == "passed"
        assert str(codex_home) not in json.dumps(result)
        assert sentinel.read_text(encoding="utf-8") == "keep this session"
        for call in calls:
            env = cast("dict[str, str]", call["env"])
            if call["argv"] in (["codex", "--version"], ["git", "init", "--quiet"]):
                assert "CODEX_HOME" not in env
                assert "OPENAI_API_KEY" not in env
                assert "CODEX_API_KEY" not in env
            else:
                assert env["CODEX_HOME"] == str(codex_home.resolve())
                assert env["OPENAI_API_KEY"] == "openai-secret"
                assert env["CODEX_API_KEY"] == "codex-secret"

    def test_codex_home_inside_checkout_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(probe._checkout_root() / ".codex"))
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))

        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

    def test_sessions_symlink_into_checkout_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        external_home = tmp_path / "external-codex-home"
        external_home.mkdir()
        (external_home / "sessions").symlink_to(
            probe._checkout_root() / "tests", target_is_directory=True
        )
        monkeypatch.setenv("CODEX_HOME", str(external_home))
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))

        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

    def test_temporary_parent_inside_checkout_fails_before_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(probe._checkout_root()))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "external-codex-home"))
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, lambda *_a, **_k: pytest.fail("launched"))

        assert probe.run_probe(MODEL)["error_code"] == "repo_setup_failed"

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

    def test_create_timeout_preserves_only_a_valid_partial_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        partial = jsonl(
            {
                "type": "thread.started",
                "thread_id": SESSION,
                "text": "timeout-transcript-secret",
            }
        ).encode()
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(
            monkeypatch,
            fake_run([], timeout_stage="create", partial_create_stdout=partial),
        )

        result = probe.run_probe(MODEL)

        assert result["status"] == "failed"
        assert result["error_code"] == "create_timeout"
        assert result["session_id"] == SESSION
        assert result["create"] == {"exit_code": None, "terminal_event": None}
        assert result["resume"] is None
        assert "timeout-transcript-secret" not in json.dumps(result)

    @pytest.mark.parametrize("stage", ["create", "resume"])
    def test_session_cli_unavailable_is_sanitized(
        self, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run([], unavailable_stage=stage))

        result = probe.run_probe(MODEL)

        assert result["status"] == "failed"
        assert result["error_code"] == "cli_unavailable"
        if stage == "create":
            assert result["session_id"] is None
            assert result["create"] == {"exit_code": None, "terminal_event": None}
            assert result["resume"] is None
        else:
            assert result["session_id"] == SESSION
            assert result["create"] == {
                "exit_code": 0,
                "terminal_event": "turn.completed",
            }
            assert result["resume"] == {
                "exit_code": None,
                "terminal_event": None,
                "id_matches": False,
            }

    @pytest.mark.parametrize(
        ("kwargs", "error", "expected_resume"),
        [
            (
                {"resume_stdout": "garbage\n"},
                "resume_malformed_jsonl",
                {"exit_code": 0, "terminal_event": None, "id_matches": False},
            ),
            (
                {"resume_stdout": jsonl({"type": "turn.completed"})},
                "resume_missing_thread",
                {"exit_code": 0, "terminal_event": None, "id_matches": False},
            ),
            (
                {"resume_stdout": stream("bad/id")},
                "resume_invalid_thread_id",
                {"exit_code": 0, "terminal_event": None, "id_matches": False},
            ),
            (
                {"resume_stdout": stream() + jsonl({"type": "thread.started"})},
                "resume_duplicate_thread_started",
                {
                    "exit_code": 0,
                    "terminal_event": "turn.completed",
                    "id_matches": False,
                },
            ),
            (
                {"resume_stdout": stream(terminal="turn.failed")},
                "resume_invalid_terminal",
                {
                    "exit_code": 0,
                    "terminal_event": "turn.failed",
                    "id_matches": True,
                },
            ),
            (
                {"resume_code": 9},
                "resume_nonzero_exit",
                {
                    "exit_code": 9,
                    "terminal_event": "turn.completed",
                    "id_matches": True,
                },
            ),
            (
                {"timeout_stage": "resume"},
                "resume_timeout",
                {"exit_code": None, "terminal_event": None, "id_matches": False},
            ),
            (
                {"resume_stdout": stream("other-thread")},
                "resume_id_mismatch",
                {
                    "exit_code": 0,
                    "terminal_event": "turn.completed",
                    "id_matches": False,
                },
            ),
        ],
    )
    def test_resume_failures_are_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, object],
        error: str,
        expected_resume: dict[str, object],
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        set_runner(monkeypatch, fake_run(calls, **kwargs))
        result = probe.run_probe(MODEL)
        assert result["status"] == "failed"
        assert result["error_code"] == error
        assert result["resume"] == expected_resume
        assert set(result) == {
            "status",
            "cli_version",
            "model",
            "session_id",
            "create",
            "resume",
            "error_code",
        }

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
                    raise subprocess.TimeoutExpired(argv, 120)
                return completed(argv, code=_code)

            monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
            set_runner(monkeypatch, run)
            result = probe.run_probe(MODEL)
            assert result["error_code"] == "version_unavailable"

    def test_cleanup_failure_is_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class BrokenTemporaryDirectory:
            name = str(tmp_path)

            def __init__(self, **_kwargs: object) -> None:
                Path(self.name).mkdir(exist_ok=True)

            def cleanup(self) -> None:
                cleanup_error = "cleanup-secret"
                raise OSError(cleanup_error)

        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            BrokenTemporaryDirectory,
        )

        def fail_directory(**_kwargs: object) -> probe.SmokeResult:
            message = "private-secret"
            raise RuntimeError(message)

        monkeypatch.setattr(probe, "_run_in_disposable_directory", fail_directory)
        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        result = json.loads(captured.out)
        assert result["status"] == "failed"
        assert result["error_code"] == "cleanup_failed"
        assert set(result) == {
            "status",
            "cli_version",
            "model",
            "session_id",
            "create",
            "resume",
            "error_code",
        }
        assert "cleanup-secret" not in json.dumps(result)
        assert "private-secret" not in json.dumps(result)

    def test_unexpected_cleanup_error_is_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class BrokenTemporaryDirectory:
            name = str(tmp_path)

            def __init__(self, **_kwargs: object) -> None:
                Path(self.name).mkdir(exist_ok=True)

            def cleanup(self) -> None:
                cleanup_error = "cleanup-secret"
                raise RuntimeError(cleanup_error)

        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
        monkeypatch.setattr(
            "scripts.probe_codex_live_session.tempfile.TemporaryDirectory",
            BrokenTemporaryDirectory,
        )
        monkeypatch.setattr(
            probe,
            "_run_in_disposable_directory",
            lambda **_kwargs: probe._result(
                probe.STATUS_PASSED,
                cli_version=VERSION,
                model=MODEL,
                session_id=SESSION,
                create={"exit_code": 0, "terminal_event": "turn.completed"},
                resume={
                    "exit_code": 0,
                    "terminal_event": "turn.completed",
                    "id_matches": True,
                },
                error_code=None,
            ),
        )

        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        result = json.loads(captured.out)
        assert result["error_code"] == "internal_error"
        assert "cleanup-secret" not in captured.out

    def test_unexpected_error_is_reported_as_internal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")

        def fail_directory(**_kwargs: object) -> probe.SmokeResult:
            message = "private-secret"
            raise RuntimeError(message)

        monkeypatch.setattr(probe, "_run_in_disposable_directory", fail_directory)

        assert probe.main(["--model", MODEL]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        assert "private-secret" not in captured.out
        assert json.loads(captured.out)["error_code"] == "internal_error"

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


def test_main_emits_one_json_line_and_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CW_CODEX_LIVE_SESSION_SMOKE", raising=False)
    assert probe.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error_code"] == "opt_in_required"


def test_sigterm_returns_a_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def terminate(_model: str | None) -> probe.SmokeResult:
        probe.signal.raise_signal(probe.signal.SIGTERM)
        pytest.fail("SIGTERM handler should interrupt the probe")

    monkeypatch.setattr(probe, "run_probe", terminate)

    assert probe.main(["--model", MODEL]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error_code"] == "repo_setup_failed"


def test_keyboard_interrupt_cleans_temporary_directory_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class TrackingTemporaryDirectory:
        name = str(tmp_path)
        cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    temporary_directory = TrackingTemporaryDirectory()
    monkeypatch.setenv("CW_CODEX_LIVE_SESSION_SMOKE", "1")
    monkeypatch.setattr(
        probe,
        "_new_temporary_directory",
        lambda _checkout: temporary_directory,
    )

    def interrupt(**_kwargs: object) -> probe.SmokeResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(probe, "_run_in_disposable_directory", interrupt)

    assert probe.main(["--model", MODEL]) == 1
    captured = capsys.readouterr()
    assert temporary_directory.cleaned
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error_code"] == "repo_setup_failed"
