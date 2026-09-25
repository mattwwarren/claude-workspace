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

from tests.conftest import _write_project_config_yaml

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
        check=False,
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
    ) -> subprocess.CompletedProcess[str]:
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
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if len(calls) == 1:
            return repo_view_result
        msg = "[Errno 2] No such file or directory: 'review_monitor.py'"
        raise OSError(msg)

    monkeypatch.setattr(_mod, "run", _fake_run)

    summary = _mod.ShipSummary()
    summary.pr_number = 7

    result = _mod.check_monitor_registered(summary, required=True)

    assert result.passed is False
    assert result.required is True
    assert "No such file or directory" in result.detail


# --- pr.auto_merge config gating (#2046) ---


def test_resolve_project_config_auto_merge_absent_file(tmp_path: Path) -> None:
    """No .claude/project-config.yaml at all -> None (unknown, not False)."""
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_resolve_project_config_auto_merge_false(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is False


def test_resolve_project_config_auto_merge_true(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: true\n")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is True


def test_resolve_project_config_auto_merge_key_absent(tmp_path: Path) -> None:
    """Valid YAML, but no pr/auto_merge key -> None."""
    _write_project_config_yaml(tmp_path, "tracking:\n  primary:\n    system: github\n")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_resolve_project_config_auto_merge_malformed_yaml(tmp_path: Path) -> None:
    """Unparseable YAML -> None, no exception raised."""
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: [false\n  unterminated")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_resolve_project_config_auto_merge_non_dict_root(tmp_path: Path) -> None:
    """YAML root is a list, not a mapping -> None."""
    _write_project_config_yaml(tmp_path, "- one\n- two\n")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


@pytest.mark.parametrize(
    "config_content",
    [
        pytest.param(None, id="absent"),
        pytest.param("pr:\n  auto_merge: true\n", id="explicit-true"),
        pytest.param("pr:\n  auto_merge: [unterminated\n", id="malformed"),
    ],
)
def test_automerge_allowed_true_when_absent_or_true_or_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_content: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    if config_content is not None:
        _write_project_config_yaml(tmp_path, config_content)
    assert _mod.automerge_allowed() is True


def test_automerge_allowed_false_when_config_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    assert _mod.automerge_allowed() is False


def test_resolve_effective_automerge_required_downgrades_when_config_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    assert _mod.resolve_effective_automerge_required(base_required=True) is False


@pytest.mark.parametrize(
    "config_content",
    [
        pytest.param("pr:\n  auto_merge: true\n", id="explicit-true"),
        pytest.param(None, id="absent"),
    ],
)
def test_resolve_effective_automerge_required_stays_true_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_content: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    if config_content is not None:
        _write_project_config_yaml(tmp_path, config_content)
    assert _mod.resolve_effective_automerge_required(base_required=True) is True


def test_resolve_effective_automerge_required_false_base_stays_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    assert _mod.resolve_effective_automerge_required(base_required=False) is False


def test_cmd_verify_downgrades_automerge_when_config_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: pr.auto_merge: false downgrades automerge-enabled to optional
    and the overall verify status stays ok despite auto-merge not being enabled.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")

    head_sha = "a" * 40

    def _fake_git(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return head_sha
        if args == ("rev-parse", "origin/feature-branch"):
            return head_sha
        return ""

    def _fake_run(
        cmd: list[str], check: bool = False, capture: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            payload = {
                "number": 42,
                "url": "https://github.com/example/repo/pull/42",
                "headRefOid": head_sha,
                "state": "OPEN",
                "title": "test PR",
                "autoMergeRequest": None,
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_mod, "git", _fake_git)
    monkeypatch.setattr(_mod, "run", _fake_run)
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")

    args = _mod.build_parser().parse_args(
        ["verify", "--branch", "feature-branch", "--require-automerge", "--json"]
    )
    exit_code = _mod.cmd_verify(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    automerge_check = next(
        c for c in payload["checks"] if c["name"] == "automerge-enabled"
    )
    assert automerge_check["required"] is False
    assert any("pr.auto_merge: false" in w for w in payload["warnings"])


def test_cmd_check_automerge_allowed_prints_true_and_exits_0_when_config_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = _mod.main(["check-automerge-allowed"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "true\n"


def test_cmd_check_automerge_allowed_prints_false_and_exits_1_when_config_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    exit_code = _mod.main(["check-automerge-allowed"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "false\n"


def test_cmd_check_automerge_allowed_reads_requested_repo_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    allowed_repo = tmp_path / "allowed"
    blocked_repo = tmp_path / "blocked"
    _write_project_config_yaml(allowed_repo, "pr:\n  auto_merge: true\n")
    _write_project_config_yaml(blocked_repo, "pr:\n  auto_merge: false\n")

    args = _mod.build_parser().parse_args(
        ["check-automerge-allowed", "--repo-path", str(blocked_repo)]
    )
    assert _mod.cmd_check_automerge_allowed(args) == 1
    assert capsys.readouterr().out == "false\n"

    args = _mod.build_parser().parse_args(
        ["check-automerge-allowed", "--repo-path", str(allowed_repo)]
    )
    assert _mod.cmd_check_automerge_allowed(args) == 0
    assert capsys.readouterr().out == "true\n"


def test_cmd_check_automerge_allowed_warns_on_stderr_when_pyyaml_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    monkeypatch.setattr(_mod, "yaml", None)

    exit_code = _mod.main(["check-automerge-allowed"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "true\n"
    assert "PyYAML unavailable" in captured.err
