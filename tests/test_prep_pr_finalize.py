"""Tests for .claude/scripts/prep_pr_finalize.py monitor invocation.

Uses importlib to load the script directly (it lives outside the src/ tree),
following tests/test_prep_pr_state.py's convention.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "prep_pr_finalize.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("prep_pr_finalize", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("prep_pr_finalize", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def test_review_monitor_direct_exec_clean_exit(tmp_path: Path) -> None:
    """review_monitor.py must be directly executable on Linux (correct shebang)."""
    result = subprocess.run(
        [str(_mod.MONITOR_SCRIPT), "status", "--repo", "fake/repo", "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, "GLOBAL_CLAUDE_REVIEW_MONITOR_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)


def test_review_monitor_shebang_is_env_python3() -> None:
    """review_monitor.py's shebang uses env python3, not a Homebrew-only path."""
    review_monitor = _REPO_ROOT / ".claude" / "scripts" / "review_monitor.py"
    with review_monitor.open() as f:
        first_line = f.readline()
    assert first_line == "#!/usr/bin/env python3\n"


def test_monitor_call_site_uses_sys_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_monitor_registered invokes review_monitor.py via sys.executable."""
    calls: list[list[str]] = []

    repo_view_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="owner/repo\n", stderr=""
    )
    monitor_status_result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"monitored": {"owner/repo#7": {}}}),
        stderr="",
    )

    def _fake_run(
        cmd: list[str], check: bool = False, capture: bool = True
    ) -> subprocess.CompletedProcess:
        calls.append(list(cmd))
        if len(calls) == 1:
            return repo_view_result
        return monitor_status_result

    monkeypatch.setattr(_mod, "run", _fake_run)

    summary = _mod.ShipSummary()
    summary.pr_number = 7

    result = _mod.check_monitor_registered(summary, required=True)

    assert result.passed is True
    assert len(calls) == 2
    assert calls[1] == [
        sys.executable,
        str(_mod.MONITOR_SCRIPT),
        "status",
        "--repo",
        "owner/repo",
        "--json",
    ]


def test_monitor_registered_catches_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError raised invoking review_monitor.py is caught, not propagated."""
    calls: list[list[str]] = []
    repo_view_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="owner/repo\n", stderr=""
    )

    def _fake_run(
        cmd: list[str], check: bool = False, capture: bool = True
    ) -> subprocess.CompletedProcess:
        calls.append(list(cmd))
        if len(calls) == 1:
            return repo_view_result
        raise OSError("[Errno 2] No such file or directory: 'review_monitor.py'")

    monkeypatch.setattr(_mod, "run", _fake_run)

    summary = _mod.ShipSummary()
    summary.pr_number = 7

    result = _mod.check_monitor_registered(summary, required=True)

    assert result.passed is False
    assert result.required is True
    assert "No such file or directory" in result.detail
