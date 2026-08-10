"""Live opencode CLI contract suite (#1671 R5) — requires a real ``opencode`` CLI.

These tests drive ``opencode run --format json --pure`` against the real
backend to pin the JSONL event shape (text events, step_finish terminal
signal) that cw's harvest path depends on. They are excluded from PR CI and
the unit matrix: the module carries ``pytest.mark.integration`` and is gated
behind ``INTEGRATION_OPENCODE_LIVE``. The nightly ``nightly-opencode.yml``
workflow opts them in.

Run manually::

    INTEGRATION_OPENCODE_LIVE=1 \\
        uv run pytest tests/test_opencode_contract_live.py -v -m integration

Read-only: produces no PR, commit, push, rebase, merge, or auto-merge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    extract_text_from_jsonl,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_OPENCODE_LIVE = os.environ.get("INTEGRATION_OPENCODE_LIVE", "").strip() not in (
    "",
    "0",
)


def _git(repo: Path, *args: str) -> None:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=clean_env,
    )


@pytest.fixture(scope="module")
def live_base() -> Iterator[Path]:
    """Yield a fixture base dir, torn down after the module."""
    default_parent = str(Path.home() / ".cache" / "cw-live-tests")
    parent = Path(os.environ.get("CW_LIVE_TEST_TMPDIR", default_parent))
    base = parent / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def live_worktree(live_base: Path) -> Iterator[Path]:
    """Yield a minimal git repo for a live opencode run."""
    repo = live_base / "repo"
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _run_opencode(worktree: Path, prompt: str) -> str:
    """Run opencode in *worktree* and return the JSONL log content."""
    argv = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--dir",
        str(worktree),
        prompt,
    ]
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log_file:
        proc = subprocess.run(
            argv,
            stdout=log_file,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    if proc.returncode != 0:
        pytest.skip(
            f"opencode run failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:500]}"
        )
    return log_path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.skipif(
    not _OPENCODE_LIVE,
    reason="INTEGRATION_OPENCODE_LIVE not set",
)
class TestOpencodeLiveContract:
    """Live contract: verify opencode JSONL shape matches harvest expectations."""

    def test_opencode_emits_text_events(self, live_worktree: Path) -> None:
        """opencode --format json emits text events with part.text content."""
        log_content = _run_opencode(live_worktree, "Say exactly: hello world")
        text = extract_text_from_jsonl(log_content)
        assert len(text) > 0, "expected non-empty text output from opencode"

    def test_opencode_emits_step_finish(self, live_worktree: Path) -> None:
        """opencode --format json emits a terminal step_finish event."""
        log_content = _run_opencode(live_worktree, "Say exactly: done")
        has_step_finish = False
        for line in log_content.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "step_finish":
                has_step_finish = True
                break
        assert has_step_finish, "expected step_finish event in opencode JSONL"

    def test_opencode_sentinel_harvestable(self, live_worktree: Path) -> None:
        """Sentinel in opencode text is harvestable by extract_text_from_jsonl."""
        from cw.auto_dev_result import parse_stdout

        sentinel_text = (
            "<<<AUTO_DEV_RESULT\n"
            '{"status":"no_op","stage_reached":"stage1_pre_flight",'
            '"scope":{"tier":"small","files":0,"lines_estimate":0,'
            '"lines_actual":0,"forbidden_touched":false},'
            '"plan_source":"none","review":{"must_fix_initial":0,'
            '"should_fix":0,"fix_cycles_used":0},"health":{'
            '"lowest_agent_confidence":"HIGH","any_incomplete_risk":false,'
            '"shortcuts":[],"recommendation":"PROCEED",'
            '"downgrade_applied":false,"fix_loop_escalated":false},'
            '"friction_highlights":[],"blocker":null,"next_actions":[]}\n'
            "AUTO_DEV_RESULT>>>"
        )
        prompt = f"Output this exact text and nothing else:\n{sentinel_text}"
        log_content = _run_opencode(live_worktree, prompt)
        text = extract_text_from_jsonl(log_content)
        result = parse_stdout(text)
        assert result is not None, "sentinel not found in opencode text output"
