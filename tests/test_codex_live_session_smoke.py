"""Offline tests for scripts/probe_codex_live_session.py (RFC 0018 D1, #3064).

Every subprocess boundary is mocked at ``probe.subprocess.run``; nothing here
launches codex or git. The one real-process test runs the script with the
opt-in unset, which launches nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import probe_codex_live_session as probe

_VERSION = "codex-cli 0.156.1"
_MODEL = "gpt-test"
_CREATE_ARGV_TAIL = [
    "--json",
    "--sandbox",
    "read-only",
    "--ignore-user-config",
    "--model",
    _MODEL,
    probe.CREATE_PROMPT,
]
_RESUME_ARGV_TAIL = [
    "--json",
    "--ignore-user-config",
    "--model",
    _MODEL,
    probe.RESUME_PROMPT,
]
_FORBIDDEN = (
    "--last",
    "--ephemeral",
    "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox",
    "workspace-write",
    "-o",
)
_OPT_OUT = (
    '{"status":"skipped","cli_version":null,"model":null,"session_id":null,'
    '"create":null,"resume":null,"error_code":"opt_in_required"}'
)


def _started(thread_id: str) -> dict[str, object]:
    return {"type": "thread.started", "thread_id": thread_id}


def _done() -> dict[str, object]:
    return {"type": "turn.completed"}


def _failed() -> dict[str, object]:
    return {"type": "turn.failed"}


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


_OK_VERSION = _completed(_VERSION + "\n")
_OK_CREATE = _completed(_stream(_started("thr-1"), _done()))
_OK_RESUME = _completed(_stream(_started("thr-1"), _done()))


class FakeRun:
    """A ``subprocess.run`` double routed by argv; records every call."""

    def __init__(
        self,
        *,
        version: subprocess.CompletedProcess[str] | Exception = _OK_VERSION,
        create: subprocess.CompletedProcess[str] | Exception = _OK_CREATE,
        resume: subprocess.CompletedProcess[str] | Exception = _OK_RESUME,
        git: subprocess.CompletedProcess[str] | Exception | None = None,
    ) -> None:
        self.version = version
        self.create = create
        self.resume = resume
        self.git = git if git is not None else _completed()
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[:2] == ["git", "init"]:
            chosen: subprocess.CompletedProcess[str] | Exception = self.git
        elif argv == ["codex", "--version"]:
            chosen = self.version
        elif argv[:3] == ["codex", "exec", "resume"]:
            chosen = self.resume
        else:
            chosen = self.create
        if isinstance(chosen, Exception):
            raise chosen
        return chosen

    def codex_calls(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [call for call in self.calls if call[0][0] == "codex"]


@pytest.fixture
def opted_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Opt in, with the temp parent and CODEX_HOME both under tmp_path."""
    parent = tmp_path / "parent"
    monkeypatch.setenv(probe.OPT_IN_ENV, "1")
    monkeypatch.setenv(probe.TMP_PARENT_ENV, str(parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return parent


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(probe.subprocess, "run", fake)
    return fake


def _run(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> tuple[int, dict[str, Any]]:
    code = probe.main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    return code, json.loads(captured.out)


def _ok_result() -> dict[str, object]:
    return {
        "status": "passed",
        "cli_version": _VERSION,
        "model": _MODEL,
        "session_id": "thr-1",
        "create": {"exit_code": 0, "terminal_event": "turn.completed"},
        "resume": {
            "exit_code": 0,
            "terminal_event": "turn.completed",
            "id_matches": True,
        },
        "error_code": None,
    }


# ---- parser -----------------------------------------------------------------


_PARSE_CASES: list[tuple[str, tuple[str | None, str | None, str | None]]] = [
    (_stream(_started("thr-1"), _done()), ("thr-1", "turn.completed", None)),
    (
        "\n" + _stream(_started("thr-1"), {"type": "item.completed"}, _done()) + "\n",
        ("thr-1", "turn.completed", None),
    ),
    ("not json\n", (None, None, "malformed_jsonl")),
    ("[1,2]\n", (None, None, "malformed_jsonl")),
    ('{"type": 5}\n', (None, None, "malformed_jsonl")),
    (_stream(_done()), (None, "turn.completed", "missing_thread")),
    (_stream(_started("")), (None, None, "invalid_thread_id")),
    (
        _stream({"type": "thread.started", "thread_id": 7}),
        (None, None, "invalid_thread_id"),
    ),
    (_stream(_started("<THREAD>")), (None, None, "invalid_thread_id")),
    (_stream(_started("a" * 129)), (None, None, "invalid_thread_id")),
    (
        _stream(_started("thr-1"), _started("thr-2"), _done()),
        ("thr-1", None, "duplicate_thread_started"),
    ),
    (_stream(_started("thr-1"), _failed()), ("thr-1", "turn.failed", None)),
    (
        _stream(_started("thr-1"), _done(), _done()),
        ("thr-1", "turn.completed", "invalid_terminal"),
    ),
    (
        _stream(_started("thr-1"), {"type": "turn.canceled"}),
        ("thr-1", None, "invalid_terminal"),
    ),
    (_stream(_started("thr-1")), ("thr-1", None, "invalid_terminal")),
    (_stream(_done(), _started("thr-1")), (None, "turn.completed", "invalid_terminal")),
]


@pytest.mark.parametrize(("stdout", "expected"), _PARSE_CASES)
def test_parse_stream_classifies_each_shape(
    stdout: str, expected: tuple[str | None, str | None, str | None]
) -> None:
    parsed = probe.parse_stream(stdout)
    fault = parsed.fault.value if parsed.fault is not None else None
    assert (parsed.thread_id, parsed.terminal, fault) == expected


# ---- argv, timeouts, cwd --------------------------------------------------


def test_create_and_resume_argv_are_exact_and_read_only_on_create(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeRun())
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 0
    assert out == _ok_result()
    argv_by_phase = {tuple(a[:3]): a for a, _ in fake.codex_calls()}
    create = argv_by_phase[("codex", "exec", "--json")]
    resume = argv_by_phase[("codex", "exec", "resume")]
    assert create == ["codex", "exec", *_CREATE_ARGV_TAIL]
    assert resume == ["codex", "exec", "resume", "thr-1", *_RESUME_ARGV_TAIL]
    assert "--sandbox" not in resume
    for argv, _ in fake.codex_calls():
        for token in _FORBIDDEN:
            assert token not in argv, token
        assert "-o" not in argv


def test_version_argv_and_every_run_carry_timeout_and_same_cwd(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeRun())
    _run(capsys, ["--model", _MODEL])
    argvs = [call[0] for call in fake.calls]
    assert ["codex", "--version"] in argvs
    cwds = set()
    for argv, kwargs in fake.calls:
        assert kwargs["timeout"] == 120
        cwds.add(str(kwargs["cwd"]))
        if argv[0] == "codex":
            # codex inherits the operator environment unchanged (no env= kwarg).
            assert "env" not in kwargs
    assert len(cwds) == 1


# ---- version and model ------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "error"),
    [
        (_completed("codex 0.156.1\n"), "version_invalid"),
        (_completed("codex-cli 0.156\n"), "version_invalid"),
        (_completed("codex-cli " + "1" * 60 + ".0.0\n"), "version_invalid"),
        (_completed(_VERSION + "\n", returncode=1), "version_unavailable"),
        (subprocess.TimeoutExpired(cmd=["codex"], timeout=120), "version_unavailable"),
        (FileNotFoundError("codex"), "cli_unavailable"),
    ],
)
def test_version_failures_map_to_their_codes(
    opted_in: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: subprocess.CompletedProcess[str] | Exception,
    error: str,
) -> None:
    fake = _install(monkeypatch, FakeRun(version=version))
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out == {
        "status": "failed",
        "cli_version": None,
        "model": _MODEL,
        "session_id": None,
        "create": None,
        "resume": None,
        "error_code": error,
    }
    assert [argv for argv, _ in fake.codex_calls()] == [["codex", "--version"]]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--model"],
        ["--model", _MODEL, "--extra"],
        ["--model", "bad model"],
        ["--model", "x" * 65],
        ["--model", "--last"],
        ["-m", _MODEL],
    ],
)
def test_invalid_model_is_never_passed_to_the_cli(
    opted_in: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    fake = _install(monkeypatch, FakeRun())
    code, out = _run(capsys, args)
    assert code == 1
    assert out["error_code"] == "invalid_model"
    assert out["model"] is None
    assert fake.calls == []


# ---- outcomes -----------------------------------------------------------------


def _create_failure(
    exit_code: int | None, terminal: str | None, session: str | None, error: str
) -> dict[str, object]:
    return {
        "status": "failed",
        "cli_version": _VERSION,
        "model": _MODEL,
        "session_id": session,
        "create": {"exit_code": exit_code, "terminal_event": terminal},
        "resume": None,
        "error_code": error,
    }


_CREATE_CASES: list[tuple[str, Any, dict[str, object]]] = [
    (
        "timeout",
        subprocess.TimeoutExpired(cmd=["codex"], timeout=120),
        _create_failure(None, None, None, "create_timeout"),
    ),
    (
        "nonzero",
        _completed(_stream(_started("thr-1"), _done()), returncode=2),
        _create_failure(2, None, None, "create_nonzero_exit"),
    ),
    (
        "malformed",
        _completed("not json\n"),
        _create_failure(0, None, None, "create_malformed_jsonl"),
    ),
    (
        "missing-thread",
        _completed(_stream(_done())),
        _create_failure(0, "turn.completed", None, "create_missing_thread"),
    ),
    (
        "invalid-thread",
        _completed(_stream(_started("<THREAD>"), _done())),
        _create_failure(0, None, None, "create_invalid_thread_id"),
    ),
    (
        "duplicate-thread",
        _completed(_stream(_started("thr-1"), _started("thr-2"), _done())),
        _create_failure(0, None, "thr-1", "create_duplicate_thread_started"),
    ),
    (
        "turn-failed",
        _completed(_stream(_started("thr-1"), _failed())),
        _create_failure(0, "turn.failed", "thr-1", "create_invalid_terminal"),
    ),
]


@pytest.mark.parametrize(
    ("label", "create", "expected"), _CREATE_CASES, ids=[c[0] for c in _CREATE_CASES]
)
def test_create_failures_report_the_exact_schema(
    opted_in: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    create: Any,
    expected: dict[str, object],
) -> None:
    fake = _install(monkeypatch, FakeRun(create=create))
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out == expected
    assert all(
        argv[:3] != ["codex", "exec", "resume"] for argv, _ in fake.codex_calls()
    )


_RESUME_CASES: list[tuple[str, Any, dict[str, object] | None]] = [
    (
        "timeout",
        subprocess.TimeoutExpired(cmd=["codex"], timeout=120),
        {"exit_code": None, "terminal_event": None, "id_matches": False},
    ),
    (
        "nonzero",
        _completed(_stream(_started("thr-1"), _done()), returncode=3),
        {"exit_code": 3, "terminal_event": None, "id_matches": False},
    ),
    (
        "malformed",
        _completed("not json\n"),
        {"exit_code": 0, "terminal_event": None, "id_matches": False},
    ),
    (
        "missing-thread",
        _completed(_stream(_done())),
        {"exit_code": 0, "terminal_event": "turn.completed", "id_matches": False},
    ),
    (
        "invalid-thread",
        _completed(_stream(_started("<THREAD>"), _done())),
        {"exit_code": 0, "terminal_event": None, "id_matches": False},
    ),
    (
        "duplicate-thread",
        _completed(_stream(_started("thr-1"), _started("thr-1"), _done())),
        {"exit_code": 0, "terminal_event": None, "id_matches": False},
    ),
    (
        "id-mismatch",
        _completed(_stream(_started("thr-2"), _done())),
        {"exit_code": 0, "terminal_event": "turn.completed", "id_matches": False},
    ),
    (
        "turn-failed",
        _completed(_stream(_started("thr-1"), _failed())),
        {"exit_code": 0, "terminal_event": "turn.failed", "id_matches": True},
    ),
]
_RESUME_ERRORS = {
    "timeout": "resume_timeout",
    "nonzero": "resume_nonzero_exit",
    "malformed": "resume_malformed_jsonl",
    "missing-thread": "resume_missing_thread",
    "invalid-thread": "resume_invalid_thread_id",
    "duplicate-thread": "resume_duplicate_thread_started",
    "id-mismatch": "resume_id_mismatch",
    "turn-failed": "resume_invalid_terminal",
}


@pytest.mark.parametrize(
    ("label", "resume", "expected"), _RESUME_CASES, ids=[c[0] for c in _RESUME_CASES]
)
def test_resume_failures_report_the_exact_schema(
    opted_in: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    resume: Any,
    expected: dict[str, object] | None,
) -> None:
    _install(monkeypatch, FakeRun(resume=resume))
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out == {
        "status": "failed",
        "cli_version": _VERSION,
        "model": _MODEL,
        "session_id": "thr-1",
        "create": {"exit_code": 0, "terminal_event": "turn.completed"},
        "resume": expected,
        "error_code": _RESUME_ERRORS[label],
    }


def test_full_pass_result_is_the_exact_schema(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeRun())
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 0
    assert list(out) == [
        "status",
        "cli_version",
        "model",
        "session_id",
        "create",
        "resume",
        "error_code",
    ]
    assert out == _ok_result()


def test_repo_setup_failures_map_to_repo_setup_failed(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeRun(git=_completed(returncode=128)))
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out["error_code"] == "repo_setup_failed"
    assert out["create"] is None
    assert fake.codex_calls() == []


def test_git_init_raising_maps_to_repo_setup_failed(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeRun(git=OSError("git missing")))
    _, out = _run(capsys, ["--model", _MODEL])
    assert out["error_code"] == "repo_setup_failed"


def test_cleanup_failure_is_reported_and_stays_failed(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, FakeRun())

    def _refuse(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        msg = "held open"
        raise OSError(msg)

    monkeypatch.setattr(probe.shutil, "rmtree", _refuse)
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out["status"] == "failed"
    assert out["error_code"] == "cleanup_failed"
    assert out["session_id"] == "thr-1"


# ---- opt-out and launch-free paths -------------------------------------------


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_opt_out_launches_nothing_and_prints_the_exact_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    parent = tmp_path / "parent"
    monkeypatch.setenv(probe.TMP_PARENT_ENV, str(parent))
    if value is None:
        monkeypatch.delenv(probe.OPT_IN_ENV, raising=False)
    else:
        monkeypatch.setenv(probe.OPT_IN_ENV, value)
    fake = _install(monkeypatch, FakeRun())
    code = probe.main(["--model", _MODEL])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == _OPT_OUT + "\n"
    assert captured.err == ""
    assert fake.calls == []
    assert not parent.exists()


def test_internal_error_hook_prints_only_the_fixed_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe._sanitized_excepthook(RuntimeError, RuntimeError("secret"), None)
    captured = capsys.readouterr()
    assert captured.out == (
        '{"status":"failed","cli_version":null,"model":null,"session_id":null,'
        '"create":null,"resume":null,"error_code":"internal_error"}\n'
    )
    assert captured.err == ""
    assert "secret" not in captured.out


def test_end_to_end_opt_out_runs_the_real_script(tmp_path: Path) -> None:
    script = Path(probe.__file__)
    env = {k: v for k, v in os.environ.items() if k != probe.OPT_IN_ENV}
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0
    assert proc.stdout == _OPT_OUT + "\n"
    assert proc.stderr == ""


def test_no_workflow_sets_the_opt_in() -> None:
    workflows = Path(probe.__file__).resolve().parents[1] / ".github" / "workflows"
    for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
        assert probe.OPT_IN_ENV not in path.read_text(encoding="utf-8"), path.name


# ---- preflight ----------------------------------------------------------------


def _inside_checkout() -> Path:
    return probe._CHECKOUT_ROOT / "codex-state"


def test_codex_home_inside_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(probe.OPT_IN_ENV, "1")
    monkeypatch.setenv(probe.TMP_PARENT_ENV, str(tmp_path / "parent"))
    monkeypatch.setenv("CODEX_HOME", str(_inside_checkout()))
    fake = _install(monkeypatch, FakeRun())
    _, out = _run(capsys, ["--model", _MODEL])
    assert out["error_code"] == "repo_setup_failed"
    assert fake.calls == []


def test_sessions_dir_symlinked_into_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "sessions").symlink_to(probe._CHECKOUT_ROOT)
    monkeypatch.setenv(probe.OPT_IN_ENV, "1")
    monkeypatch.setenv(probe.TMP_PARENT_ENV, str(tmp_path / "parent"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    _install(monkeypatch, FakeRun())
    _, out = _run(capsys, ["--model", _MODEL])
    assert out["error_code"] == "repo_setup_failed"


def test_temp_parent_inside_checkout_through_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    link = tmp_path / "link"
    link.symlink_to(probe._CHECKOUT_ROOT, target_is_directory=True)
    monkeypatch.setenv(probe.OPT_IN_ENV, "1")
    monkeypatch.setenv(probe.TMP_PARENT_ENV, str(link / "scratch"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    fake = _install(monkeypatch, FakeRun())
    _, out = _run(capsys, ["--model", _MODEL])
    assert out["error_code"] == "repo_setup_failed"
    assert not (probe._CHECKOUT_ROOT / "scratch").exists()
    assert fake.calls == []


def test_operator_codex_home_reaches_the_child_unchanged(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeRun())
    _run(capsys, ["--model", _MODEL])
    assert os.environ["CODEX_HOME"].endswith("codex-home")
    assert all("env" not in kwargs for _, kwargs in fake.codex_calls())


def test_child_output_never_reaches_stdout_or_stderr(
    opted_in: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leaked = "SECRET-TOKEN /home/someone/prompt raw"
    leak = _completed(f"{leaked}\n", returncode=0, stderr=leaked)
    _install(monkeypatch, FakeRun(create=leak))
    code, out = _run(capsys, ["--model", _MODEL])
    assert code == 1
    assert out["error_code"] == "create_malformed_jsonl"
    assert "SECRET" not in json.dumps(out)
