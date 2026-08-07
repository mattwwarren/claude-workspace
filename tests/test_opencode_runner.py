"""Tests for cw.opencode_runner — OpencodeRunner + JSONL sentinel harvest (#1669)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from cw.auto_dev_result import AutoDevResult
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    OPENCODE_NO_OUTPUT,
    OPENCODE_NOT_FOUND,
    FakeOpencodeRunner,
    RealOpencodeRunner,
    build_argv,
    build_env,
    extract_text_from_jsonl,
    make_blocked,
    opencode_available,
    synthesize_opencode_result,
)

if TYPE_CHECKING:
    pass


def _make_task(ticket_id: str = "T-1") -> MagicMock:
    task = MagicMock()
    task.ticket_id = ticket_id
    task.scope_hint = None
    return task


# ---------------------------------------------------------------------------
# opencode_available
# ---------------------------------------------------------------------------


def test_opencode_available_returns_bool() -> None:
    """opencode_available() returns a bool (True or False depending on PATH)."""
    with patch("cw.opencode_runner.shutil.which", return_value="/usr/bin/opencode"):
        assert opencode_available() is True
    with patch("cw.opencode_runner.shutil.which", return_value=None):
        assert opencode_available() is False


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


def test_build_argv_with_model(tmp_path: Path) -> None:
    """build_argv includes --model when provided."""
    argv = build_argv("genhealth/glm-5.2", tmp_path, "do the thing")
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--format" in argv
    assert "json" in argv
    assert "--pure" in argv
    assert "--dir" in argv
    assert str(tmp_path) in argv
    assert "--model" in argv
    assert "genhealth/glm-5.2" in argv
    assert argv[-1] == "do the thing"


def test_build_argv_without_model(tmp_path: Path) -> None:
    """build_argv omits --model when None."""
    argv = build_argv(None, tmp_path, "do the thing")
    assert "--model" not in argv
    assert argv[-1] == "do the thing"


# ---------------------------------------------------------------------------
# build_env
# ---------------------------------------------------------------------------


def test_build_env_filters_secrets() -> None:
    """build_env excludes non-allowlisted vars (e.g. AWS_SECRET_KEY)."""
    env_patch = {"AWS_SECRET_KEY": "leaked", "HOME": "/tmp"}
    with patch.dict("os.environ", env_patch, clear=False):
        env = build_env()
    assert "AWS_SECRET_KEY" not in env
    assert env["HOME"] == "/tmp"


# ---------------------------------------------------------------------------
# make_blocked
# ---------------------------------------------------------------------------


def test_make_blocked_returns_valid_auto_dev_result(tmp_path: Path) -> None:
    """make_blocked returns a schema-valid AutoDevResult with the given reason."""
    result = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason=OPENCODE_NOT_FOUND,
    )
    assert isinstance(result, AutoDevResult)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert "user_resolve_opencode_executor_failure" in result.next_actions


# ---------------------------------------------------------------------------
# extract_text_from_jsonl
# ---------------------------------------------------------------------------


def test_extract_text_from_jsonl_returns_text_content() -> None:
    """extract_text_from_jsonl concatenates text event payloads."""
    events = [
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "text", "part": {"text": "hello "}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        json.dumps({"type": "text", "part": {"text": "world"}}),
    ]
    log_content = "\n".join(events)
    assert extract_text_from_jsonl(log_content) == "hello world"


def test_extract_text_from_jsonl_empty_content() -> None:
    """extract_text_from_jsonl returns empty string for no text events."""
    events = [
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]
    assert extract_text_from_jsonl("\n".join(events)) == ""


def test_extract_text_from_jsonl_malformed_lines() -> None:
    """extract_text_from_jsonl skips unparseable lines."""
    log_content = "\n".join(
        [
            "not json",
            json.dumps({"type": "text", "part": {"text": "ok"}}),
            "",
            "{broken",
        ]
    )
    assert extract_text_from_jsonl(log_content) == "ok"


def test_extract_text_from_jsonl_empty_string() -> None:
    """extract_text_from_jsonl returns empty string for empty input."""
    assert extract_text_from_jsonl("") == ""


# ---------------------------------------------------------------------------
# FakeOpencodeRunner
# ---------------------------------------------------------------------------


def test_fake_runner_records_call_and_returns_live_proc(tmp_path: Path) -> None:
    """FakeOpencodeRunner.launch() records argv/cwd/env and returns a live process."""
    runner = FakeOpencodeRunner()
    argv = ["opencode", "run", "--format", "json", "do stuff"]
    env = {"HOME": "/tmp", "PATH": "/usr/bin"}

    proc = runner.launch(tmp_path, argv, env)
    try:
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call["argv"] == argv
        assert call["cwd"] == tmp_path
        assert call["env"] == env
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# RealOpencodeRunner
# ---------------------------------------------------------------------------


def test_real_runner_creates_log_file(tmp_path: Path) -> None:
    """RealOpencodeRunner.launch() creates .cw/opencode.log and redirects stdout."""
    runner = RealOpencodeRunner()
    argv = ["echo", "test-output"]
    env = {"HOME": "/tmp", "PATH": "/usr/bin:/bin"}

    proc = runner.launch(tmp_path, argv, env)
    proc.wait()
    log_path = tmp_path / OPENCODE_LOG_RELATIVE_PATH
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "test-output" in content


# ---------------------------------------------------------------------------
# synthesize_opencode_result
# ---------------------------------------------------------------------------


def test_synthesize_opencode_result_sentinel_found(
    tmp_path: Path,
) -> None:
    """Sentinel in log → parsed AutoDevResult returned."""
    blocked = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="test-reason")
    sentinel_json = blocked.model_dump_json()
    sentinel_text = f"<<<AUTO_DEV_RESULT\n{sentinel_json}\nAUTO_DEV_RESULT>>>"
    text_event = json.dumps({"type": "text", "part": {"text": sentinel_text}})
    log_path = tmp_path / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text_event, encoding="utf-8")

    result = synthesize_opencode_result(
        task=_make_task(),
        worktree=tmp_path,
        session_id="test-sid",
    )
    assert isinstance(result, AutoDevResult)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == "test-reason"


def test_synthesize_opencode_result_no_sentinel(
    tmp_path: Path,
) -> None:
    """No sentinel in log → OPENCODE_NO_OUTPUT blocked result."""
    log_content = json.dumps({"type": "text", "part": {"text": "no sentinel here"}})
    log_path = tmp_path / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_content, encoding="utf-8")

    result = synthesize_opencode_result(
        task=_make_task(),
        worktree=tmp_path,
        session_id="test-sid",
    )
    assert isinstance(result, AutoDevResult)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT
    assert result.blocker.retry_eligible is True


def test_synthesize_opencode_result_missing_log(
    tmp_path: Path,
) -> None:
    """synthesize_opencode_result returns OPENCODE_NO_OUTPUT when log is missing."""
    result = synthesize_opencode_result(
        task=_make_task(),
        worktree=tmp_path,
        session_id=None,
    )
    assert isinstance(result, AutoDevResult)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT


def test_synthesize_opencode_result_empty_log(
    tmp_path: Path,
) -> None:
    """synthesize_opencode_result returns OPENCODE_NO_OUTPUT when log is empty."""
    log_path = tmp_path / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    result = synthesize_opencode_result(
        task=_make_task(),
        worktree=tmp_path,
        session_id=None,
    )
    assert isinstance(result, AutoDevResult)
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT
