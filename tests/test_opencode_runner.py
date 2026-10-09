"""Tests for cw.opencode_runner — OpencodeRunner + JSONL sentinel harvest (#1669)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_STATUS_UNKNOWN,
    AutoDevResult,
    BlockedResult,
    StageReached,
    parse_stdout,
)
from cw.models import Stage
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    OPENCODE_NO_OUTPUT,
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    SUPPORTED_STAGES,
    RealOpencodeRunner,
    _supports_v1_flags,
    build_argv,
    build_env,
    build_stage_prompt,
    extract_text_from_jsonl,
    make_blocked,
    opencode_available,
    parse_last_sentinel,
    resolve_finalize_command_file,
    stage_entry_marker,
    synthesize_opencode_result,
)
from cw.worktree import resolve_worker_tmpdir
from tests._opencode_helpers import (
    earlier_stage_then_final_log,
    framed,
    log_content,
    text_event,
    write_opencode_log,
)

if TYPE_CHECKING:
    from tests._opencode_helpers import LogLine


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
    """build_argv includes --model when provided; v2 omits --pure/--dir (#2654)."""
    argv = build_argv("genhealth/glm-5.2", tmp_path, "do the thing", v1_flags=False)
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--format" in argv
    assert "json" in argv
    assert "--auto" in argv
    assert "--pure" not in argv
    assert "--dir" not in argv
    assert str(tmp_path) not in argv
    assert "--model" in argv
    assert "genhealth/glm-5.2" in argv
    assert argv[-1] == "do the thing"


def test_build_argv_without_model(tmp_path: Path) -> None:
    """build_argv omits --model when None."""
    argv = build_argv(None, tmp_path, "do the thing", v1_flags=False)
    assert "--model" not in argv
    assert argv[-1] == "do the thing"


def test_build_argv_v1_flags(tmp_path: Path) -> None:
    """opencode 1.x keeps --pure and --dir <worktree>."""
    argv = build_argv(None, tmp_path, "p", v1_flags=True)
    assert "--pure" in argv
    assert argv[argv.index("--dir") + 1] == str(tmp_path)


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        ("--format --pure --auto --dir", True),
        ("--standalone --format --auto --model", False),
    ],
)
def test_supports_v1_flags_probe(help_text: str, expected: bool) -> None:
    """The help probe detects flags from stdout/stderr and is cached."""
    _supports_v1_flags.cache_clear()
    done = subprocess.CompletedProcess([], 0, stdout="", stderr=help_text)
    with patch("cw.opencode_runner.subprocess.run", return_value=done):
        assert _supports_v1_flags() is expected
    _supports_v1_flags.cache_clear()


def test_supports_v1_flags_probe_failure_assumes_v2() -> None:
    """Missing binary / timeout falls back to the v2 (flag-free) argv."""
    _supports_v1_flags.cache_clear()
    with patch("cw.opencode_runner.subprocess.run", side_effect=OSError):
        assert _supports_v1_flags() is False
    _supports_v1_flags.cache_clear()


def test_build_argv_default_probes(tmp_path: Path) -> None:
    """v1_flags=None defers to the probe."""
    with patch("cw.opencode_runner._supports_v1_flags", return_value=False):
        assert "--pure" not in build_argv(None, tmp_path, "p")


# ---------------------------------------------------------------------------
# build_env
# ---------------------------------------------------------------------------


def test_build_env_filters_secrets(tmp_path: Path) -> None:
    """build_env excludes non-allowlisted vars (e.g. AWS_SECRET_KEY)."""
    slack_id_key = "SLACK_MCP_CLIENT_ID"
    slack_secret_key = "SLACK_MCP_" + "CLIENT_SECRET"
    env_patch = {
        "AWS_SECRET_KEY": "leaked",
        "HOME": "/tmp",
        slack_id_key: "test-id",
        slack_secret_key: "test-secret-value",
    }
    with patch.dict("os.environ", env_patch, clear=False):
        env = build_env(tmp_path)
    assert "AWS_SECRET_KEY" not in env
    assert env["HOME"] == "/tmp"
    assert env[slack_id_key] == "test-id"
    assert env[slack_secret_key] == "test-secret-value"


def test_build_env_delegates_tmpdir_to_shared_helper(tmp_path: Path) -> None:
    """An ambient TMPDIR is overridden by the per-worktree one (#2470).

    Before #2470 the allowlist passed the orchestrator's own TMPDIR straight
    through -- the regression this asserts against.
    """
    with patch.dict("os.environ", {"TMPDIR": "/var/folders/xx/T/"}, clear=False):
        env = build_env(tmp_path)
    assert env["TMPDIR"] == str(resolve_worker_tmpdir(tmp_path))


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


def test_real_runner_launch_passes_start_new_session(tmp_path: Path) -> None:
    """RealOpencodeRunner.launch() passes start_new_session=True to Popen."""
    runner = RealOpencodeRunner()
    with patch("cw.executor_launch.subprocess.Popen") as mock_popen:
        runner.launch(tmp_path, ["echo", "test-output"], {})
    assert mock_popen.call_args.kwargs["start_new_session"] is True


def test_real_runner_launch_child_gets_own_process_group(tmp_path: Path) -> None:
    """The real child's pgid differs from the test process's own pgid."""
    runner = RealOpencodeRunner()
    proc = runner.launch(tmp_path, ["sleep", "60"], {"PATH": os.environ["PATH"]})
    try:
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        proc.kill()
        proc.wait()


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
# Last-sentinel selection (#2490)
#
# Fixture provenance (composed arrangement, HYPOTHESIZED robustness lines, no
# capture of the failing session): see tests/_opencode_helpers.py.
# ---------------------------------------------------------------------------


def _finalize_blocked(
    worktree: Path,
    *,
    reason: str,
    stage: StageReached = "stage4a_merge_gate",
    ticket_id: str = "T-1",
) -> AutoDevResult:
    """A finalize-stage ``blocked`` sentinel shaped like the #2490 variant.

    Mirrors the ticket's second report: ``blocker.stage: stage4a_merge_gate``,
    ``reason: prior_pipeline_pr_open``, a ``recovery_hint`` and
    ``retry_eligible: true``.
    """
    base = make_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=reason,
        details="blocked by PR #2468 which is still open",
        retry_eligible=True,
        stage_reached=stage,
    )
    assert base.blocker is not None
    blocker = base.blocker.model_copy(
        update={"recovery_hint": "merge PR #2468 then requeue"}
    )
    return base.model_copy(update={"blocker": blocker})


def _stage_walk_log(worktree: Path) -> list[LogLine]:
    """A finalize session log: an earlier stage's sentinel quoted, then the final."""
    earlier = make_blocked(
        ticket_id="T-1", worktree=worktree, reason="impl_failed"
    )  # stage2_impl
    final = _finalize_blocked(worktree, reason="prior_pipeline_pr_open")
    return earlier_stage_then_final_log(earlier, final)


def _events_log(*texts: str) -> str:
    """JSONL log with one ``text`` event per *texts* entry."""
    return log_content([text_event(t) for t in texts])


def _assert_reason(result: AutoDevResult | BlockedResult | None, reason: str) -> None:
    """*result* is a block whose ``blocker.reason`` is *reason*."""
    assert result is not None
    assert result.blocker is not None
    assert result.blocker.reason == reason


def test_parse_last_sentinel_takes_final_block_over_earlier_stage(
    tmp_path: Path,
) -> None:
    """An earlier stage's quoted sentinel neither shadows nor poisons the last."""
    log = log_content(_stage_walk_log(tmp_path))

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.blocker is not None
    assert result.blocker.reason == "prior_pipeline_pr_open"


def test_synthesize_opencode_result_keeps_final_blocked_sentinel_after_earlier_one(
    tmp_path: Path,
) -> None:
    """#2490: the final finalize ``blocked`` result survives an earlier sentinel.

    Before the fix the two blocks were concatenated and read as
    ``multiple_result_blocks``, so the real result became ``opencode_no_output``
    and its ``prior_pipeline_pr_open`` reason / ``recovery_hint`` were lost.
    """
    write_opencode_log(tmp_path, _stage_walk_log(tmp_path))

    result = synthesize_opencode_result(
        task=_make_task(), worktree=tmp_path, session_id=None
    )

    assert result.status == "blocked"
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.blocker is not None
    assert result.blocker.reason == "prior_pipeline_pr_open"
    assert result.blocker.recovery_hint == "merge PR #2468 then requeue"
    assert result.blocker.retry_eligible is True


def test_parse_last_sentinel_last_block_wins_within_one_event(
    tmp_path: Path,
) -> None:
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    final = _finalize_blocked(tmp_path, reason="merge_conflict_post_push")
    log = _events_log(f"{framed(earlier)}\n\n{framed(final)}")

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "merge_conflict_post_push")


_TEMPLATE_BLOCK = (
    '<<<AUTO_DEV_RESULT\n{"schema_version": 4, "ticket_id": "<ticket-id>", '
    '"status": "<stage_complete | blocked>"}\nAUTO_DEV_RESULT>>>'
)


def test_parse_last_sentinel_skips_placeholder_template_block(
    tmp_path: Path,
) -> None:
    """The command file's worked example (``<ticket-id>``) is never a result."""
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    log = _events_log(_TEMPLATE_BLOCK, framed(final), _TEMPLATE_BLOCK)

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_real_block_after_a_placeholder_event(
    tmp_path: Path,
) -> None:
    """A real result in an event AFTER a placeholder event is still returned."""
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    log = _events_log(_TEMPLATE_BLOCK, framed(final))

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_real_status_with_angle_ticket_id_is_not_a_placeholder(
    tmp_path: Path,
) -> None:
    """The placeholder gate needs BOTH fields templated: a real status survives."""
    odd = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open", ticket_id="<x>")
    log = _events_log(framed(odd))

    result = parse_last_sentinel(log, ticket_id="<x>")

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "<x>"


def test_parse_last_sentinel_unusable_last_block_is_not_replaced_by_earlier(
    tmp_path: Path,
) -> None:
    """A malformed final block must not resurrect the earlier stage's result."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    broken = "<<<AUTO_DEV_RESULT\n{not json\nAUTO_DEV_RESULT>>>"

    result = parse_last_sentinel(_events_log(framed(earlier), broken), ticket_id="T-1")

    assert isinstance(result, BlockedResult)
    _assert_reason(result, BLOCKER_REASON_NO_RESULT_EMITTED)


def test_synthesize_unusable_last_block_is_opencode_no_output(
    tmp_path: Path,
) -> None:
    """The harvest of that log is the retry-eligible ``opencode_no_output``."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    broken = "<<<AUTO_DEV_RESULT\n{not json\nAUTO_DEV_RESULT>>>"
    write_opencode_log(tmp_path, [text_event(framed(earlier)), text_event(broken)])

    result = synthesize_opencode_result(
        task=_make_task(), worktree=tmp_path, session_id=None
    )

    _assert_reason(result, OPENCODE_NO_OUTPUT)
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True


def test_parse_last_sentinel_joins_a_block_split_across_events(
    tmp_path: Path,
) -> None:
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    text = framed(final)
    cut = len(text) // 2

    result = parse_last_sentinel(_events_log(text[:cut], text[cut:]), ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_split_final_block_is_not_shadowed_by_earlier(
    tmp_path: Path,
) -> None:
    """A complete earlier block must not win over a final block split in two."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    text = framed(final)
    cut = len(text) // 2

    result = parse_last_sentinel(
        _events_log(framed(earlier), text[:cut], text[cut:]), ticket_id="T-1"
    )

    assert isinstance(result, AutoDevResult)
    assert result.stage_reached == "stage4a_merge_gate"
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_truncated_final_frame_is_unusable(
    tmp_path: Path,
) -> None:
    """An open marker with no close is the unusable result, never the earlier block."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    truncated = framed(final)[: len(framed(final)) // 2]

    result = parse_last_sentinel(
        _events_log(framed(earlier), truncated), ticket_id="T-1"
    )

    assert isinstance(result, BlockedResult)
    _assert_reason(result, BLOCKER_REASON_NO_RESULT_EMITTED)


def test_parse_last_sentinel_truncated_frame_after_complete_one_in_same_event(
    tmp_path: Path,
) -> None:
    """[complete][open, no close] in ONE event: the truncated tail still decides."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    log = _events_log(f'{framed(earlier)}\n<<<AUTO_DEV_RESULT\n{{"schema_version": 4')

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, BlockedResult)
    _assert_reason(result, BLOCKER_REASON_NO_RESULT_EMITTED)


@pytest.mark.parametrize(
    "mention",
    [
        "I emitted the <<<AUTO_DEV_RESULT>>> sentinel above.",
        "Done; the <<<AUTO_DEV_RESULT>>>",
        "see <<<AUTO_DEV_RESULT",
    ],
    ids=["prose", "bare-close-marker", "nothing-after-marker"],
)
def test_parse_last_sentinel_later_marker_mention_does_not_replace_the_real_block(
    tmp_path: Path, mention: str
) -> None:
    """The finalize prompt says 'emit the <<<AUTO_DEV_RESULT>>> sentinel': a closing
    summary repeating that phrase is not a truncated frame (#2490 review).
    """
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    log = _events_log(framed(final), mention)

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


def test_synthesize_keeps_the_real_result_when_a_later_event_mentions_the_marker(
    tmp_path: Path,
) -> None:
    """End to end: the mention must not turn the result into ``opencode_no_output``."""
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    write_opencode_log(
        tmp_path,
        [
            text_event(framed(final)),
            text_event("I emitted the <<<AUTO_DEV_RESULT>>> sentinel above."),
        ],
    )

    result = synthesize_opencode_result(
        task=_make_task(), worktree=tmp_path, session_id=None
    )

    assert result.status == "blocked"
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.blocker is not None
    assert result.blocker.reason == "prior_pipeline_pr_open"


def test_parse_last_sentinel_truncated_close_marker_spelling_is_unusable(
    tmp_path: Path,
) -> None:
    """A final frame opened ``<<<AUTO_DEV_RESULT>>>`` and cut off is still truncated."""
    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    truncated = '<<<AUTO_DEV_RESULT>>>\n{"schema_version": 4, "ticket_id": "T-1", "sta'

    result = parse_last_sentinel(
        _events_log(framed(earlier), truncated), ticket_id="T-1"
    )

    assert isinstance(result, BlockedResult)
    _assert_reason(result, BLOCKER_REASON_NO_RESULT_EMITTED)


def test_parse_last_sentinel_foreign_block_split_across_events_keeps_the_real_one(
    tmp_path: Path,
) -> None:
    """The split frame is another ticket's: the real earlier block stands."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign = framed(
        _finalize_blocked(tmp_path, reason="merge_conflict_post_push", ticket_id="T-2")
    )
    cut = len(foreign) // 2

    result = parse_last_sentinel(
        _events_log(framed(real), foreign[:cut], foreign[cut:]), ticket_id="T-1"
    )

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "T-1"
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_ignores_foreign_block_quoted_after_the_real_one(
    tmp_path: Path,
) -> None:
    """A sibling ticket's result quoted AFTER this ticket's must not be applied."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign = _finalize_blocked(
        tmp_path, reason="merge_conflict_post_push", ticket_id="T-2"
    )
    log = _events_log(framed(real), f"the sibling reported:\n{framed(foreign)}")

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "T-1"
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_ignores_foreign_block_quoted_before_the_real_one(
    tmp_path: Path,
) -> None:
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign = _finalize_blocked(
        tmp_path, reason="merge_conflict_post_push", ticket_id="T-2"
    )
    log = _events_log(framed(foreign), framed(real))

    result = parse_last_sentinel(log, ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "T-1"


def test_parse_last_sentinel_foreign_block_in_the_same_event_as_the_real_one(
    tmp_path: Path,
) -> None:
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign = _finalize_blocked(
        tmp_path, reason="merge_conflict_post_push", ticket_id="T-2"
    )

    result = parse_last_sentinel(
        _events_log(f"{framed(real)}\n{framed(foreign)}"), ticket_id="T-1"
    )

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "T-1"


def test_parse_last_sentinel_foreign_blocks_only_is_none(tmp_path: Path) -> None:
    foreign = _finalize_blocked(
        tmp_path, reason="merge_conflict_post_push", ticket_id="T-2"
    )

    assert parse_last_sentinel(_events_log(framed(foreign)), ticket_id="T-1") is None


def test_parse_last_sentinel_malformed_block_of_another_ticket_is_ignored(
    tmp_path: Path,
) -> None:
    """A block claiming a different ticket is dropped even when it is malformed."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign_broken = (
        '<<<AUTO_DEV_RESULT\n{"schema_version": 4, "ticket_id": "T-2",'
        ' "status": "no_such_status"}\nAUTO_DEV_RESULT>>>'
    )

    result = parse_last_sentinel(
        _events_log(framed(real), foreign_broken), ticket_id="T-1"
    )

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


def test_parse_last_sentinel_undecodable_sibling_block_is_ignored_by_its_claim(
    tmp_path: Path,
) -> None:
    """A quoted sibling block with a ``...`` elision names T-2: not this ticket's."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    sibling = (
        '<<<AUTO_DEV_RESULT\n{"schema_version": 4, "ticket_id": "T-2",'
        ' "status": "blocked", ...}\nAUTO_DEV_RESULT>>>'
    )

    result = parse_last_sentinel(_events_log(framed(real), sibling), ticket_id="T-1")

    assert isinstance(result, AutoDevResult)
    _assert_reason(result, "prior_pipeline_pr_open")


@pytest.mark.parametrize("echoed", ["940", "#940", "GH-940"])
def test_synthesize_accepts_the_numeric_ticket_id_forms_a_worker_may_echo(
    tmp_path: Path, echoed: str
) -> None:
    """A bare numeric task id: ``940``, ``#940`` and ``GH-940`` name the same ticket."""
    final = _finalize_blocked(
        tmp_path, reason="prior_pipeline_pr_open", ticket_id=echoed
    )
    write_opencode_log(tmp_path, [text_event(framed(final))])

    result = synthesize_opencode_result(
        task=_make_task("940"), worktree=tmp_path, session_id=None
    )

    _assert_reason(result, "prior_pipeline_pr_open")
    assert result.stage_reached == "stage4a_merge_gate"


def test_synthesize_rejects_a_different_number_and_logs_the_discard(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    final = _finalize_blocked(
        tmp_path, reason="prior_pipeline_pr_open", ticket_id="941"
    )
    write_opencode_log(tmp_path, [text_event(framed(final))])

    with caplog.at_level("WARNING", logger="cw.auto_dev_result"):
        result = synthesize_opencode_result(
            task=_make_task("940"), worktree=tmp_path, session_id=None
        )

    _assert_reason(result, OPENCODE_NO_OUTPUT)
    messages = [r.getMessage() for r in caplog.records]
    assert any("'941'" in m and "'940'" in m for m in messages)


def test_parse_last_sentinel_malformed_block_for_this_ticket_is_not_replaced(
    tmp_path: Path,
) -> None:
    """A malformed LAST block for THIS ticket stays the final word (the rule)."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    own_broken = (
        '<<<AUTO_DEV_RESULT\n{"schema_version": 4, "ticket_id": "T-1",'
        ' "status": "no_such_status"}\nAUTO_DEV_RESULT>>>'
    )

    result = parse_last_sentinel(_events_log(framed(real), own_broken), ticket_id="T-1")

    assert isinstance(result, BlockedResult)
    _assert_reason(result, BLOCKER_REASON_STATUS_UNKNOWN)


def test_parse_last_sentinel_empty_ticket_id_disables_the_identity_check(
    tmp_path: Path,
) -> None:
    """A session with no associated ticket (``""``) has nothing to compare."""
    other = _finalize_blocked(
        tmp_path, reason="prior_pipeline_pr_open", ticket_id="T-9"
    )

    result = parse_last_sentinel(_events_log(framed(other)), ticket_id="")

    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "T-9"


def test_synthesize_never_applies_a_foreign_ticket_block_quoted_last(
    tmp_path: Path,
) -> None:
    """#2490 review: harvest keeps this ticket's result over a later foreign quote."""
    real = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    foreign = _finalize_blocked(
        tmp_path, reason="merge_conflict_post_push", ticket_id="T-2"
    )
    write_opencode_log(
        tmp_path, [text_event(framed(real)), text_event(framed(foreign))]
    )

    result = synthesize_opencode_result(
        task=_make_task("T-1"), worktree=tmp_path, session_id=None
    )

    assert result.ticket_id == "T-1"
    _assert_reason(result, "prior_pipeline_pr_open")


def test_synthesize_with_only_a_foreign_block_is_opencode_no_output(
    tmp_path: Path,
) -> None:
    """No block for this ticket -> the retry-eligible requeue, not a foreign result."""
    foreign = _finalize_blocked(
        tmp_path, reason="prior_pipeline_pr_open", ticket_id="T-2"
    )
    write_opencode_log(tmp_path, [text_event(framed(foreign))])

    result = synthesize_opencode_result(
        task=_make_task("T-1"), worktree=tmp_path, session_id=None
    )

    _assert_reason(result, OPENCODE_NO_OUTPUT)
    assert result.ticket_id == "T-1"


def _loose_fenced(result: AutoDevResult) -> str:
    """*result* as bare ```json fenced JSON with no AUTO_DEV_RESULT markers (#337)."""
    return f"```json\n{result.model_dump_json()}\n```"


def test_loose_fenced_result_parses_the_same_via_parse_stdout_and_last_sentinel(
    tmp_path: Path,
) -> None:
    """Parity (#337): a marker-less fenced result is not lost to ``no_output``."""
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    fenced = _loose_fenced(final)

    via_stdout = parse_stdout(fenced)
    via_log = parse_last_sentinel(_events_log(fenced), ticket_id="T-1")

    assert isinstance(via_stdout, AutoDevResult)
    assert via_log == via_stdout


def test_synthesize_returns_a_loose_fenced_result(tmp_path: Path) -> None:
    final = _finalize_blocked(tmp_path, reason="prior_pipeline_pr_open")
    write_opencode_log(tmp_path, [text_event(f"Done.\n{_loose_fenced(final)}")])

    result = synthesize_opencode_result(
        task=_make_task(), worktree=tmp_path, session_id=None
    )

    _assert_reason(result, "prior_pipeline_pr_open")
    assert result.stage_reached == "stage4a_merge_gate"


def test_loose_fenced_result_for_another_ticket_is_ignored(tmp_path: Path) -> None:
    foreign = _finalize_blocked(
        tmp_path, reason="prior_pipeline_pr_open", ticket_id="T-2"
    )

    assert (
        parse_last_sentinel(_events_log(_loose_fenced(foreign)), ticket_id="T-1")
        is None
    )


def test_loose_fenced_placeholder_is_never_a_result() -> None:
    placeholder = (
        '```json\n{"schema_version": 4, "ticket_id": "<ticket-id>",'
        ' "status": "<stage_complete | blocked>"}\n```'
    )

    assert parse_last_sentinel(_events_log(placeholder), ticket_id="T-1") is None


@pytest.mark.parametrize(
    "log",
    [
        "",
        "not json at all",
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        json.dumps(text_event("narrative with no sentinel")),
    ],
)
def test_parse_last_sentinel_none_when_no_block(log: str) -> None:
    assert parse_last_sentinel(log, ticket_id="T-1") is None


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
    # #2123: the executor must stamp the sha it actually reviewed, and must
    # capture it AFTER the fix loop -- a pre-fix capture would mismatch HEAD on
    # every round that fixed anything and park the dispatch-side gate on the
    # mainline path. The timing substring pins that against regression.
    assert "reviewed_sha" in prompt
    assert "rev-parse HEAD" in prompt
    assert "captured once, after any fix-cycle commits land" in prompt
    # Capturing the sha is only half the contract: an instruction that captures
    # REVIEWED_SHA but never says where it goes leaves the sentinel field unset,
    # which the dispatch-side gate reads as "never stamped" and fails closed on.
    # This pins the assignment itself, not just the mention of the field name.
    assert "Set review.reviewed_sha to REVIEWED_SHA (step 4)" in prompt
    assert "the post-fix-loop branch tip" in prompt


def test_review_sentinel_template_carries_reviewed_sha() -> None:
    """#2123: the sentinel template names the field, not just the prompt prose.

    The OpenCode REVIEW result is parsed straight from this template's shape,
    so a prompt instruction with no corresponding template key is a field the
    executor has no slot to fill.
    """
    from cw.opencode_runner import _REVIEW_SENTINEL_TEMPLATE

    assert "reviewed_sha" in _REVIEW_SENTINEL_TEMPLATE


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


# ---------------------------------------------------------------------------
# build_stage_prompt session_id threading (#2430) -- the sentinel rules switch
# from validate-only to an emit_cli push once a session id is known at spawn
# time.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["plan", "impl", "review"])
def test_build_stage_prompt_threads_session_id_into_emit_command(
    stage: str, tmp_path: Path
) -> None:
    """A known session_id threads into an emit_cli push, not the validate path."""
    prompt = build_stage_prompt(stage, "T-1", tmp_path, session_id="abc123")
    assert "cw result emit - --session-id abc123" in prompt
    assert "validate the JSON before emitting" not in prompt


@pytest.mark.parametrize("stage", ["plan", "impl", "review"])
def test_build_stage_prompt_session_id_none_falls_back_to_validate_only(
    stage: str, tmp_path: Path
) -> None:
    """No session_id (today's 3-arg call) keeps the validate-only contract.

    Regression guard for every existing 3-arg build_stage_prompt call in
    this file: the default must stay byte-identical to the pre-#2430 prompt.
    """
    prompt = build_stage_prompt(stage, "T-1", tmp_path)
    assert "cw result validate -" in prompt
    assert "--session-id" not in prompt


@pytest.mark.parametrize("stage", ["plan", "impl", "review"])
def test_build_stage_prompt_emit_form_still_carries_the_frame(
    stage: str, tmp_path: Path
) -> None:
    """The emit-form sentinel rules still carry the <<<AUTO_DEV_RESULT>>> frame."""
    prompt = build_stage_prompt(stage, "T-1", tmp_path, session_id="abc123")
    assert "<<<AUTO_DEV_RESULT" in prompt
    assert "AUTO_DEV_RESULT>>>" in prompt


@pytest.mark.parametrize("stage", ["plan", "impl", "review"])
def test_build_stage_prompt_emit_form_carries_fix_and_rerun_loop(
    stage: str, tmp_path: Path
) -> None:
    """A field.path: message validation failure instructs a fix-and-rerun loop."""
    prompt = build_stage_prompt(stage, "T-1", tmp_path, session_id="abc123")
    assert "field.path: message" in prompt
    assert "re-run" in prompt


@pytest.mark.parametrize("stage", ["plan", "impl", "review"])
def test_build_stage_prompt_emit_form_carries_fallback_friction_entry(
    stage: str, tmp_path: Path
) -> None:
    """A non-field.path emit failure records friction, then falls back to validate."""
    prompt = build_stage_prompt(stage, "T-1", tmp_path, session_id="abc123")
    assert "cw_result_emit_fallback:" in prompt
    assert "friction_highlights" in prompt
    assert "cw result validate -" in prompt
