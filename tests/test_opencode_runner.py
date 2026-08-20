"""Tests for cw.opencode_runner — OpencodeRunner + JSONL sentinel harvest (#1669)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.models import Stage
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    OPENCODE_NO_OUTPUT,
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    SUPPORTED_STAGES,
    FakeOpencodeRunner,
    RealOpencodeRunner,
    build_argv,
    build_env,
    build_stage_prompt,
    extract_text_from_jsonl,
    make_blocked,
    opencode_available,
    resolve_finalize_command_file,
    stage_entry_marker,
    synthesize_opencode_result,
)

if TYPE_CHECKING:
    pass


def _make_task(ticket_id: str = "T-1", stage: Stage = Stage.FINALIZE) -> MagicMock:
    task = MagicMock()
    task.ticket_id = ticket_id
    task.scope_hint = None
    task.stage = stage
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
    assert "--auto" in argv
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
    slack_id_key = "SLACK_MCP_CLIENT_ID"
    slack_secret_key = "SLACK_MCP_" + "CLIENT_SECRET"
    env_patch = {
        "AWS_SECRET_KEY": "leaked",
        "HOME": "/tmp",
        "TMPDIR": "/var/folders/xx/T/",
        slack_id_key: "test-id",
        slack_secret_key: "test-secret-value",
    }
    with patch.dict("os.environ", env_patch, clear=False):
        env = build_env()
    assert "AWS_SECRET_KEY" not in env
    assert env["HOME"] == "/tmp"
    assert env["TMPDIR"] == "/var/folders/xx/T/"
    assert env[slack_id_key] == "test-id"
    assert env[slack_secret_key] == "test-secret-value"


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


@pytest.mark.parametrize(
    ("stage", "expected_marker"),
    [
        (Stage.PLAN, "stage1_plan"),
        (Stage.IMPL, "stage2_impl"),
        (Stage.REVIEW, "stage3_review"),
        (Stage.FINALIZE, "stage4a_merge_gate"),
    ],
)
def test_synthesize_opencode_result_no_output_carries_task_stage(
    tmp_path: Path,
    stage: Stage,
    expected_marker: str,
) -> None:
    """A no-output failure reports the dispatched stage's own entry marker.

    A stage2_impl default on a PLAN-stage failure classifies as a later-stage
    self-escalation and dispatch walks task.stage forward past planning
    (_resolve_stage_walk) — the failure marker must be the stage the task
    was AT.
    """
    result = synthesize_opencode_result(
        task=_make_task(stage=stage),
        worktree=tmp_path,
        session_id=None,
    )
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT
    assert result.stage_reached == expected_marker
    assert result.blocker.stage == expected_marker


# ---------------------------------------------------------------------------
# stage_entry_marker + STAGE4A_MERGE_GATE
# ---------------------------------------------------------------------------


def test_stage4a_merge_gate_constant() -> None:
    """STAGE4A_MERGE_GATE is the canonical FINALIZE entry-point marker."""
    assert STAGE4A_MERGE_GATE == "stage4a_merge_gate"


def test_stage_entry_marker_maps_each_supported_stage() -> None:
    """Each supported stage maps to its own entry marker — never a later one."""
    assert stage_entry_marker("plan") == "stage1_plan"
    assert stage_entry_marker("impl") == "stage2_impl"
    assert stage_entry_marker("review") == "stage3_review"
    assert stage_entry_marker("finalize") == STAGE4A_MERGE_GATE


def test_stage_entry_marker_unsupported_stage_falls_back() -> None:
    """Unsupported stage values keep the #1670 R5 STAGE4A_MERGE_GATE block."""
    assert stage_entry_marker("harden") == STAGE4A_MERGE_GATE


# ---------------------------------------------------------------------------
# resolve_finalize_command_file (worktree-first, home fallback)
# ---------------------------------------------------------------------------


def test_resolve_finalize_command_file_prefers_worktree_copy(
    tmp_path: Path,
) -> None:
    """A worktree-tracked auto-dev-finalize.md wins over the global tree."""
    worktree_copy = tmp_path / ".claude" / "commands" / "auto-dev-finalize.md"
    worktree_copy.parent.mkdir(parents=True)
    worktree_copy.write_text("# finalize", encoding="utf-8")
    assert resolve_finalize_command_file(tmp_path) == worktree_copy


def test_resolve_finalize_command_file_falls_back_to_home(
    tmp_path: Path,
) -> None:
    """Without a worktree copy, resolution falls back to ~/.claude/commands/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with patch("cw.opencode_runner.Path.home", return_value=fake_home):
        resolved = resolve_finalize_command_file(worktree)
    assert resolved == fake_home / ".claude" / "commands" / "auto-dev-finalize.md"


# ---------------------------------------------------------------------------
# build_stage_prompt (all stages)
# ---------------------------------------------------------------------------


def test_supported_stages_contains_plan_impl_review_finalize() -> None:
    """SUPPORTED_STAGES includes the four auto-dev pipeline stages."""
    assert "plan" in SUPPORTED_STAGES
    assert "impl" in SUPPORTED_STAGES
    assert "review" in SUPPORTED_STAGES
    assert "finalize" in SUPPORTED_STAGES
    assert "harden" not in SUPPORTED_STAGES


def test_build_stage_prompt_plan_is_self_contained(tmp_path: Path) -> None:
    """The plan prompt carries the stage contract, not a command-file pointer."""
    prompt = build_stage_prompt("plan", "T-1", tmp_path)
    assert "auto-dev-plan.md" not in prompt
    assert "T-1" in prompt
    assert "--headless" in prompt
    assert "stage1_plan" in prompt
    assert ".cw/plan.md" in prompt
    assert "## Files Modified" in prompt
    assert "**Scope tier:**" in prompt
    assert "no_op" in prompt
    assert '"lines_actual": null' in prompt
    assert "<<<AUTO_DEV_RESULT" in prompt
    assert "AUTO_DEV_RESULT>>>" in prompt


def test_build_stage_prompt_impl_is_self_contained(tmp_path: Path) -> None:
    """The impl prompt carries the stage contract, not a command-file pointer."""
    prompt = build_stage_prompt("impl", "T-1", tmp_path)
    assert "auto-dev-impl.md" not in prompt
    assert "T-1" in prompt
    assert "--headless" in prompt
    assert "stage2_impl" in prompt
    assert ".cw/plan.md" in prompt
    assert "plan_missing" in prompt
    assert "Auto-Dev-Stage: impl-complete" in prompt
    assert "HEAD:refs/heads/" in prompt
    assert "<<<AUTO_DEV_RESULT" in prompt
    assert "AUTO_DEV_RESULT>>>" in prompt


def test_build_stage_prompt_review_is_self_contained(tmp_path: Path) -> None:
    """The review prompt carries the stage contract, not a command-file pointer."""
    prompt = build_stage_prompt("review", "T-1", tmp_path)
    assert "auto-dev-review.md" not in prompt
    assert "T-1" in prompt
    assert "--headless" in prompt
    assert "stage3_review" in prompt
    assert "empty_diff_blocked" in prompt
    assert "review_blocked" in prompt
    assert "MUST_FIX" in prompt
    assert "<<<AUTO_DEV_RESULT" in prompt
    assert "AUTO_DEV_RESULT>>>" in prompt


def test_build_stage_prompt_finalize_points_at_worktree_command_file(
    tmp_path: Path,
) -> None:
    """The finalize prompt points at the resolved (worktree-first) command file."""
    worktree_copy = tmp_path / ".claude" / "commands" / "auto-dev-finalize.md"
    worktree_copy.parent.mkdir(parents=True)
    worktree_copy.write_text("# finalize", encoding="utf-8")
    prompt = build_stage_prompt("finalize", "PROJ-42", tmp_path)
    assert str(worktree_copy) in prompt
    assert "PROJ-42" in prompt
    assert "--headless" in prompt
    assert "stage4a_merge_gate" in prompt
    assert "stage4b_pr_create" in prompt
    assert "stage5_post_create" in prompt
    assert "<<<AUTO_DEV_RESULT>>>" in prompt


def test_build_stage_prompt_unsupported_stage_raises(tmp_path: Path) -> None:
    """build_stage_prompt raises KeyError for unsupported stage."""
    with pytest.raises(KeyError):
        build_stage_prompt("harden", "T-1", tmp_path)
