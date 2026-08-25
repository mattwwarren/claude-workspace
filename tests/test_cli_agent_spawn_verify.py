"""CLI tests for ``cw agent-spawn-verify`` (#2012).

The fix-loop dispatch verifier: after the review stage spawns its async fix
agent it runs this command *in the same turn* to prove a subagent transcript
actually appeared. Exit 0 means the dispatch is real and the orchestrator may
end its turn to await the completion notification; exit 1 means the spawn
produced nothing and the stage must fail loudly (``blocker.reason:
fix_loop_dispatch_unverified``) rather than awaiting a notification that will
never arrive.

Unlike the hook commands (``cw guard-cwd``, ``cw agent-spawn-pre``) this is an
operator/orchestrator-facing command with **no** fail-open contract: an
unreadable or missing project dir must exit 1 with a diagnostic, not exit 0.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.config import orchestrator_config_file
from tests.conftest import _write_idle_transcript

if TYPE_CHECKING:
    from pathlib import Path

_MAIN_CSID = "aaaa1111-0000-0000-0000-000000000000"
# Far enough in the past that any file written during the test run has an
# mtime strictly after it, without freezing the clock.
_SINCE = "2020-01-01T00:00:00Z"
_FUTURE_SINCE = "2999-01-01T00:00:00Z"
# Upper bound asserted on wall-clock runtime for the "config drives the poll
# window" cases. Generous enough to survive a loaded CI box, but far below the
# 20s default the tests exist to prove is NOT in force.
_FAST_TIMEOUT_CEILING_SECONDS = 10.0


def _write_orchestrator_config(**fields: int) -> None:
    """Write an orchestrator.yaml carrying only *fields*."""
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}: {value}\n" for key, value in fields.items()),
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so ``claude_project_dir`` resolves under tmp_path."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _invoke(worktree: Path, *extra: str) -> Result:
    runner = CliRunner()
    return runner.invoke(
        main,
        [
            "agent-spawn-verify",
            "--since",
            _SINCE,
            "--exclude-session",
            _MAIN_CSID,
            "--worktree",
            str(worktree),
            *extra,
        ],
    )


def test_exit_zero_when_subagent_transcript_present(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A fresh subagent transcript in the project dir → exit 0, path printed."""
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")
    expected = _write_idle_transcript(
        home, worktree, filename="subagents/bbbb2222-sub.jsonl"
    )

    result = _invoke(worktree, "--poll-seconds", "5")

    assert result.exit_code == 0, result.output
    assert str(expected) in result.output


def test_exit_one_when_no_subagent_transcript_appears(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """R7: a dispatch producing no subagent transcript fails loudly.

    Only the caller's own transcript exists, so the poll window elapses with
    no candidate and the command exits 1 naming the project dir and --since.
    """
    worktree = tmp_path / "wt"
    own = _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")

    result = _invoke(worktree, "--poll-seconds", "0")

    assert result.exit_code == 1
    assert str(own.parent) in result.output
    assert _SINCE in result.output


def test_own_transcript_write_does_not_count_as_verification(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """--exclude-session's transcript is excluded even when freshly rewritten."""
    worktree = tmp_path / "wt"
    own = _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")
    own.write_text(own.read_text() + own.read_text(), encoding="utf-8")

    result = _invoke(worktree, "--poll-seconds", "0")

    assert result.exit_code == 1


def test_missing_project_dir_exits_one_not_zero(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """No fail-open: an absent project dir is a verification failure, not a pass."""
    worktree = tmp_path / "never-used"

    result = _invoke(worktree, "--poll-seconds", "0")

    assert result.exit_code == 1
    assert "No new subagent transcript" in result.output


def test_unparseable_since_exits_one(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A malformed --since is a usage error surfaced as exit 1, not a crash."""
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "agent-spawn-verify",
            "--since",
            "not-a-timestamp",
            "--worktree",
            str(tmp_path / "wt"),
        ],
    )

    assert result.exit_code == 1
    assert "not-a-timestamp" in result.output


def test_transcript_older_than_since_does_not_verify(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A pre-existing sibling transcript never satisfies the dispatch check."""
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename="subagents/pre-existing.jsonl")
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "agent-spawn-verify",
            "--since",
            _FUTURE_SINCE,
            "--exclude-session",
            _MAIN_CSID,
            "--worktree",
            str(worktree),
            "--poll-seconds",
            "0",
        ],
    )

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Poll window is config-driven (RISK mitigation, R10)
# ---------------------------------------------------------------------------


def test_poll_window_comes_from_orchestrator_config(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """No --poll-seconds flag → the configured window governs, not a constant.

    Proves the command reads ``agent_spawn_verify_poll_seconds`` rather than a
    baked-in ~20s default: with the config at 1s the timeout lands far inside
    the ceiling, and the diagnostic names the effective window.
    """
    _write_orchestrator_config(
        agent_spawn_verify_poll_seconds=1,
        agent_spawn_verify_poll_interval_seconds=1,
    )
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")

    started = time.monotonic()
    result = _invoke(worktree)
    elapsed = time.monotonic() - started

    assert result.exit_code == 1
    assert elapsed < _FAST_TIMEOUT_CEILING_SECONDS
    assert "poll window 1s" in result.output


def test_poll_seconds_flag_overrides_config(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """An explicit --poll-seconds wins over a wider configured window."""
    _write_orchestrator_config(
        agent_spawn_verify_poll_seconds=600,
        agent_spawn_verify_poll_interval_seconds=1,
    )
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")

    started = time.monotonic()
    result = _invoke(worktree, "--poll-seconds", "0")
    elapsed = time.monotonic() - started

    assert result.exit_code == 1
    assert elapsed < _FAST_TIMEOUT_CEILING_SECONDS
    assert "poll window 0s" in result.output


def test_exclude_session_defaults_to_session_env_var(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$CLAUDE_CODE_SESSION_ID`` supplies the exclusion when the flag is absent."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_CSID)
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "agent-spawn-verify",
            "--since",
            _SINCE,
            "--worktree",
            str(worktree),
            "--poll-seconds",
            "0",
        ],
    )

    assert result.exit_code == 1
    assert _MAIN_CSID in result.output


def test_exit_zero_when_transcript_appears_mid_poll(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The transcript arriving partway through the poll window is detected.

    Proves the loop actually re-polls rather than only checking once at the
    start — the literal scenario an async subagent dispatch produces.
    """
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")

    def _write_late_transcript() -> None:
        time.sleep(0.5)
        _write_idle_transcript(home, worktree, filename="subagents/cccc3333-sub.jsonl")

    writer = threading.Thread(target=_write_late_transcript)
    writer.start()
    try:
        result = _invoke(
            worktree, "--poll-seconds", "5", "--poll-interval-seconds", "1"
        )
    finally:
        writer.join()

    assert result.exit_code == 0, result.output
    assert "cccc3333-sub" in result.output


def test_since_without_timezone_is_treated_as_utc(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A naive --since is normalized to UTC rather than raising on compare."""
    worktree = tmp_path / "wt"
    _write_idle_transcript(home, worktree, filename=f"{_MAIN_CSID}.jsonl")
    expected = _write_idle_transcript(
        home, worktree, filename="subagents/bbbb2222-sub.jsonl"
    )
    naive_since = (
        dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=1)
    ).isoformat()
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "agent-spawn-verify",
            "--since",
            naive_since,
            "--exclude-session",
            _MAIN_CSID,
            "--worktree",
            str(worktree),
            "--poll-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(expected) in result.output
