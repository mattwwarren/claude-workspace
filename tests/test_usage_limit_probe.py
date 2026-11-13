"""Tests for cw.usage_limit_probe, the account usage-limit probe seam (#3547)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from cw.usage_limit_probe import (
    ProbeVerdict,
    probe_account_usage_limit,
)

from tests._tool_fakes import GhReply, fake_claude_cli, prepend_to_path

if TYPE_CHECKING:
    from pathlib import Path

    from tests._tool_fakes import RecordingShim

# Copied from tests/test_usage_limit_patterns.py's wording table.
_LIMIT_WORDING = "You've hit your session limit · resets 3:45pm"
_PROMPT = "Reply with exactly: WORKER-OK"


def _shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: GhReply
) -> RecordingShim:
    shim = fake_claude_cli(tmp_path, default=reply)
    prepend_to_path(monkeypatch, shim.bin_dir)
    return shim


def test_worker_ok_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shim(tmp_path, monkeypatch, GhReply(stdout="WORKER-OK\n"))

    assert probe_account_usage_limit(None).verdict is ProbeVerdict.AVAILABLE


@pytest.mark.parametrize(
    "reply",
    [
        GhReply(returncode=1, stdout=_LIMIT_WORDING),
        GhReply(returncode=1, stderr=_LIMIT_WORDING),
        GhReply(returncode=0, stdout=f"WORKER-OK\n{_LIMIT_WORDING}"),
    ],
)
def test_limit_wording_is_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: GhReply
) -> None:
    _shim(tmp_path, monkeypatch, reply)

    result = probe_account_usage_limit(None)

    assert result.verdict is ProbeVerdict.LIMITED
    assert result.detail


@pytest.mark.parametrize(
    "reply",
    [
        GhReply(returncode=0, stdout="something else"),
        GhReply(returncode=2, stdout="WORKER-OK", stderr="boom"),
    ],
)
def test_unclassifiable_output_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: GhReply
) -> None:
    _shim(tmp_path, monkeypatch, reply)

    result = probe_account_usage_limit(None)

    assert result.verdict is ProbeVerdict.ERROR
    assert result.detail


def test_missing_binary_is_error() -> None:
    """The suite default answers an unmocked ``claude`` launch with ENOENT."""
    result = probe_account_usage_limit(None)

    assert result.verdict is ProbeVerdict.ERROR
    assert "not found" in result.detail


def test_timeout_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr("cw.usage_limit_probe.subprocess.run", _timeout)

    result = probe_account_usage_limit(None, timeout_seconds=1)

    assert result.verdict is ProbeVerdict.ERROR
    assert "timed out" in result.detail


def test_other_os_error_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _denied(*_args: object, **_kwargs: object) -> object:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr("cw.usage_limit_probe.subprocess.run", _denied)

    result = probe_account_usage_limit(None)

    assert result.verdict is ProbeVerdict.ERROR
    assert "could not run" in result.detail


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (None, ["-p", _PROMPT]),
        ("claude-sonnet", ["-p", _PROMPT, "--model", "claude-sonnet"]),
    ],
)
def test_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    expected: list[str],
) -> None:
    shim = _shim(tmp_path, monkeypatch, GhReply(stdout="WORKER-OK"))

    probe_account_usage_limit(model)

    assert shim.calls == [expected]
