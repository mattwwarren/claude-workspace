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
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import (
    ARM_VIEW_ARMED,
    ARM_VIEW_OPEN,
    _assert_gh_calls,
    _gh_calls,
    _shim_env,
    _stub_gh_arm,
    _write_project_config_yaml,
)

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


def test_resolve_project_config_auto_merge_without_source_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed standalone script still honors a disabled project config."""
    monkeypatch.setattr(_mod, "_project_config", None)
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


def _run_verify_with_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    state: str,
    auto_merge_request: dict[str, str] | None,
    config_yaml: str | None = None,
) -> tuple[int, dict[str, object]]:
    """Run `verify --require-automerge --json` against a faked `gh pr view`.

    Returns (exit_code, parsed JSON payload).
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    if config_yaml is not None:
        _write_project_config_yaml(tmp_path, config_yaml)

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
                "state": state,
                "title": "test PR",
                "autoMergeRequest": auto_merge_request,
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
    payload: dict[str, object] = json.loads(captured.out)
    return exit_code, payload


def _find_check(payload: dict[str, object], name: str) -> dict[str, object]:
    """Return the check dict named `name` from a verify JSON payload."""
    checks = payload["checks"]
    assert isinstance(checks, list)
    return next(c for c in checks if c["name"] == name)


def test_cmd_verify_downgrades_automerge_when_config_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: pr.auto_merge: false downgrades automerge-enabled to optional
    and the overall verify status stays ok despite auto-merge not being enabled.
    """
    exit_code, payload = _run_verify_with_pr(
        monkeypatch,
        tmp_path,
        capsys,
        state="OPEN",
        auto_merge_request=None,
        config_yaml="pr:\n  auto_merge: false\n",
    )

    assert exit_code == 0
    assert payload["status"] == "ok"
    automerge_check = _find_check(payload, "automerge-enabled")
    assert automerge_check["required"] is False
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert any("pr.auto_merge: false" in w for w in warnings)


# --- MERGED PR accepted by --require-automerge (#2163) ---


def test_check_automerge_passes_when_pr_already_merged() -> None:
    summary = _mod.ShipSummary(pr_state="MERGED")

    result = _mod.check_automerge(summary, required=True)

    assert result.passed is True
    assert result.name == "automerge-enabled"
    assert result.required is True
    assert "merged" in result.detail.lower()
    assert summary.automerge_enabled is False


def test_check_automerge_fails_when_pr_closed_unmerged() -> None:
    summary = _mod.ShipSummary(pr_state="CLOSED")

    result = _mod.check_automerge(summary, required=True)

    assert result.passed is False
    assert result.detail == "auto-merge is not enabled on the PR"


def test_check_automerge_fails_when_pr_open_and_not_armed() -> None:
    summary = _mod.ShipSummary(pr_state="OPEN")

    result = _mod.check_automerge(summary, required=True)

    assert result.passed is False


@pytest.mark.parametrize(
    ("pr_state", "method", "expected_detail"),
    [
        pytest.param("OPEN", "SQUASH", "SQUASH", id="open-armed-method"),
        pytest.param("OPEN", "", "enabled", id="open-armed-no-method"),
        pytest.param("MERGED", "SQUASH", "SQUASH", id="merged-armed-wins"),
    ],
)
def test_check_automerge_armed_path_unchanged(
    pr_state: str, method: str, expected_detail: str
) -> None:
    summary = _mod.ShipSummary(
        pr_state=pr_state, automerge_enabled=True, automerge_method=method
    )

    result = _mod.check_automerge(summary, required=True)

    assert result.passed is True
    assert result.detail == expected_detail


def test_cmd_verify_require_automerge_accepts_merged_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _run_verify_with_pr(
        monkeypatch, tmp_path, capsys, state="MERGED", auto_merge_request=None
    )

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["pr_state"] == "MERGED"
    assert payload["automerge_enabled"] is False
    automerge_check = _find_check(payload, "automerge-enabled")
    assert automerge_check["passed"] is True
    assert automerge_check["required"] is True
    detail = automerge_check["detail"]
    assert isinstance(detail, str)
    assert "merged" in detail.lower()


def test_cmd_verify_require_automerge_fails_closed_unmerged_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _run_verify_with_pr(
        monkeypatch, tmp_path, capsys, state="CLOSED", auto_merge_request=None
    )

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["pr_state"] == "CLOSED"
    automerge_check = _find_check(payload, "automerge-enabled")
    assert automerge_check["passed"] is False
    assert automerge_check["required"] is True


def test_cmd_verify_require_automerge_armed_open_pr_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _run_verify_with_pr(
        monkeypatch,
        tmp_path,
        capsys,
        state="OPEN",
        auto_merge_request={"mergeMethod": "SQUASH"},
    )

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["automerge_enabled"] is True
    assert payload["automerge_method"] == "SQUASH"
    assert _find_check(payload, "automerge-enabled")["detail"] == "SQUASH"


@pytest.mark.parametrize(
    ("pr_state", "armed", "expected"),
    [
        pytest.param("OPEN", True, "enabled (SQUASH)", id="open-armed"),
        pytest.param("MERGED", True, "enabled (SQUASH)", id="merged-armed"),
        pytest.param("MERGED", False, "n/a (PR already merged)", id="merged-unarmed"),
        pytest.param("OPEN", False, "disabled", id="open-unarmed"),
        pytest.param("CLOSED", False, "disabled", id="closed-unarmed"),
    ],
)
def test_render_markdown_auto_merge_line(
    pr_state: str, armed: bool, expected: str
) -> None:
    summary = _mod.ShipSummary(
        pr_state=pr_state,
        automerge_enabled=armed,
        automerge_method="SQUASH" if armed else "",
    )

    rendered = _mod.render_markdown(summary)

    line = next(
        ln for ln in rendered.splitlines() if ln.startswith("- **Auto-merge:**")
    )
    assert line == f"- **Auto-merge:** {expected}"


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


def test_cmd_check_automerge_allowed_reads_repo_path_outside_cwd(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    outside_repo = tmp_path / "outside"
    outside_repo.mkdir()
    _write_project_config_yaml(repo, "pr:\n  auto_merge: false\n")

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "check-automerge-allowed",
            "--repo-path",
            str(repo),
        ],
        cwd=outside_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == "false\n"


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


# --- module-load fallback + config_path fix (#2373) ---


def test_resolve_project_config_auto_merge_module_unavailable_and_config_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shared cw.project_config module unavailable -> read config_path directly."""
    monkeypatch.setattr(_mod, "_project_config", None)
    monkeypatch.setattr(_mod, "_load_project_config_module", lambda: None)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is False


def test_automerge_allowed_false_when_module_unavailable_and_config_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_mod, "_project_config", None)
    monkeypatch.setattr(_mod, "_load_project_config_module", lambda: None)
    monkeypatch.chdir(tmp_path)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    assert _mod.automerge_allowed() is False


def test_resolve_project_config_auto_merge_module_unavailable_and_config_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shared module unavailable and no config file present -> None, no crash."""
    monkeypatch.setattr(_mod, "_project_config", None)
    monkeypatch.setattr(_mod, "_load_project_config_module", lambda: None)
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_resolve_project_config_auto_merge_module_unavailable_malformed_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shared module unavailable and malformed YAML -> None, no exception."""
    monkeypatch.setattr(_mod, "_project_config", None)
    monkeypatch.setattr(_mod, "_load_project_config_module", lambda: None)
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: [false\n  unterminated")
    config_path = tmp_path / ".claude" / "project-config.yaml"
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_resolve_project_config_auto_merge_reads_exactly_config_path_when_shape_differs(
    tmp_path: Path,
) -> None:
    """config_path need not sit two levels under a `.claude` dir for the read
    to work — the previous config_path.parent.parent guess silently missed
    this shape and returned None instead of the actual False.
    """
    config_dir = tmp_path / "not-dot-claude"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "project-config.yaml"
    config_path.write_text("pr:\n  auto_merge: false\n", encoding="utf-8")
    assert _mod.resolve_project_config_auto_merge(config_path) is False


def test_resolve_project_config_auto_merge_pyyaml_unavailable_shape_differs_no_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `yaml is None` guard must stay first in the fallback body — without
    it, the direct-read branch would call `yaml.safe_load` on `None` and
    raise AttributeError instead of degrading to None.
    """
    config_dir = tmp_path / "not-dot-claude"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "project-config.yaml"
    config_path.write_text("pr:\n  auto_merge: false\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "yaml", None)
    assert _mod.resolve_project_config_auto_merge(config_path) is None


def test_pr_state_merged_matches_cw_gh_constant() -> None:
    """The script keeps its own MERGED literal; pin it to the cw.gh source."""
    from cw.gh import _GH_PR_STATE_MERGED

    assert _mod.PR_STATE_MERGED == _GH_PR_STATE_MERGED


def test_script_runs_without_cw_importable() -> None:
    """The script is exec'd via its shebang interpreter, where `cw` is absent.

    `-S` skips site processing (so the editable `.pth` does not expose `cw`)
    on the SAME interpreter, keeping compiled deps loadable.
    """
    result = subprocess.run(
        [sys.executable, "-S", str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "verify" in result.stdout


# --- arm-automerge: bounded retry + read-back (#2576) ---

_ARM_PR = "42"
_VIEW = ["pr", "view", _ARM_PR, "--json", "state,autoMergeRequest"]
_MERGE = ["pr", "merge", _ARM_PR, "--auto", "--squash"]
_ARM_VIEW_MERGED = '{"state":"MERGED","autoMergeRequest":null}'
_GRAPHQL_ERROR = "GraphQL: Pull request is not mergeable (enablePullRequestAutoMerge)"
_SEAM_DISALLOWED_DETAIL = (
    "pr.auto_merge: false in .claude/project-config.yaml (#2046) -- no gh call made"
)


def _run_arm(
    repo: Path, fake_bin: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "arm-automerge",
            _ARM_PR,
            "--backoff-seconds",
            "0",
            "--repo-path",
            str(repo),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_shim_env(fake_bin)},
    )


def _merge_count(fake_bin: Path) -> int:
    return sum(1 for call in _gh_calls(fake_bin) if call == _MERGE)


def test_arm_automerge_transient_error_then_success(tmp_path: Path) -> None:
    """Ticket repro: first merge fails with a GraphQL error, the retry arms it."""
    fake_bin = _stub_gh_arm(
        tmp_path, [(1, _GRAPHQL_ERROR, None), (0, "", ARM_VIEW_ARMED)]
    )

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["status"] == "armed"
    assert out["attempts"] == 2
    assert out["pr_number"] == int(_ARM_PR)
    # pre-read, merge 1 (fails), post-read, [sleep], pre-read, merge 2, post-read
    _assert_gh_calls(fake_bin, [_VIEW, _MERGE, _VIEW, _VIEW, _MERGE, _VIEW])


def test_arm_automerge_already_armed_is_noop_success(tmp_path: Path) -> None:
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", None)], initial_view=ARM_VIEW_ARMED)

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["status"] == "armed"
    assert out["attempts"] == 0
    _assert_gh_calls(fake_bin, [_VIEW])
    assert _merge_count(fake_bin) == 0


def test_arm_automerge_exit_zero_null_readback_then_armed(tmp_path: Path) -> None:
    """#1140 shape: gh exits 0 without arming; the read-back drives the retry."""
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", None), (0, "", ARM_VIEW_ARMED)])

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["status"] == "armed"
    assert out["attempts"] == 2
    assert _merge_count(fake_bin) == 2


def test_arm_automerge_persistent_failure_carries_gh_stderr(tmp_path: Path) -> None:
    fake_bin = _stub_gh_arm(tmp_path, [(1, _GRAPHQL_ERROR, None)])

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 1
    out = json.loads(result.stdout)
    assert out["status"] == "failed"
    assert out["attempts"] == out["max_attempts"] == _mod.ARM_MAX_ATTEMPTS
    assert out["gh_exit_code"] == 1
    assert out["gh_stderr"] == _GRAPHQL_ERROR
    assert (
        f"ERROR: gh pr merge --auto failed after 4/4 attempts (exit 1): "
        f"{_GRAPHQL_ERROR}" in result.stderr
    )
    assert _merge_count(fake_bin) == _mod.ARM_MAX_ATTEMPTS


def test_arm_automerge_persistent_exit_zero_null_readback_fails(
    tmp_path: Path,
) -> None:
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", None)])

    result = _run_arm(tmp_path, fake_bin, "--attempts", "2")

    assert result.returncode == 1
    out = json.loads(result.stdout)
    assert out["status"] == "failed"
    assert out["gh_exit_code"] == 0
    assert out["gh_stderr"] == ""
    assert "autoMergeRequest read back null (#1140)" in out["detail"]
    assert _merge_count(fake_bin) == 2


def test_arm_automerge_already_queued_nonzero_exit_counts_as_armed(
    tmp_path: Path,
) -> None:
    """gh exits 1 ("already queued") but the read-back shows it armed."""
    fake_bin = _stub_gh_arm(
        tmp_path, [(1, "Pull request is already queued", ARM_VIEW_ARMED)]
    )

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["status"] == "armed"
    assert out["attempts"] == 1
    _assert_gh_calls(fake_bin, [_VIEW, _MERGE, _VIEW])


def test_arm_automerge_merged_readback_is_success(tmp_path: Path) -> None:
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", _ARM_VIEW_MERGED)])

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["status"] == "merged"
    assert out["pr_state"] == "MERGED"
    assert out["attempts"] == 1


def test_arm_automerge_refused_by_seam_makes_zero_gh_calls(tmp_path: Path) -> None:
    """#2046: pr.auto_merge: false must never reach `gh pr merge --auto`."""
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", ARM_VIEW_ARMED)])

    result = _run_arm(tmp_path, fake_bin)

    assert result.returncode == _mod.EXIT_ARM_DISALLOWED == 3
    assert json.loads(result.stdout) == {
        "status": "skipped",
        "pr_number": int(_ARM_PR),
        "attempts": 0,
        "max_attempts": _mod.ARM_MAX_ATTEMPTS,
        "pr_state": "",
        "gh_exit_code": None,
        "gh_stderr": "",
        "detail": _SEAM_DISALLOWED_DETAIL,
    }
    assert _gh_calls(fake_bin) == []


def test_arm_automerge_zero_attempts_is_invocation_error(tmp_path: Path) -> None:
    fake_bin = _stub_gh_arm(tmp_path, [(0, "", ARM_VIEW_ARMED)])

    result = _run_arm(tmp_path, fake_bin, "--attempts", "0")

    assert result.returncode == 2
    assert "--attempts must be >= 1" in result.stderr
    assert _gh_calls(fake_bin) == []


def test_arm_automerge_unknown_subcommand_from_stale_copy_exits_two() -> None:
    """A stale script copy lacking the subcommand fails in argparse with exit 2."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "no-such-subcommand", _ARM_PR],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


# --- arm-automerge in-process (monkeypatched run, injected sleep) ---


def _cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], subprocess.CompletedProcess[str]],
    cwds: list[Path | None] | None = None,
) -> list[list[str]]:
    """Patch ``run``; ``cwds``, when given, receives each call's ``cwd`` in order."""
    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        check: bool = False,
        capture: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cwds is not None:
            cwds.append(cwd)
        return handler(cmd)

    monkeypatch.setattr(_mod, "run", _fake_run)
    return calls


def _always_failing_gh(
    merge: subprocess.CompletedProcess[str],
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def _handler(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            return merge
        return _cp(stdout=ARM_VIEW_OPEN)

    return _handler


def test_arm_automerge_backoff_is_linear_with_no_trailing_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, _always_failing_gh(_cp(returncode=1, stderr="nope")))
    sleeps: list[float] = []

    result = _mod.arm_automerge(
        42, attempts=4, backoff_seconds=2.0, sleep=sleeps.append
    )

    assert result.status == "failed"
    assert result.attempts == 4
    assert sleeps == [2.0, 4.0, 6.0]


def test_arm_automerge_single_attempt_never_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, _always_failing_gh(_cp(returncode=1, stderr="nope")))
    sleeps: list[float] = []

    result = _mod.arm_automerge(
        42, attempts=1, backoff_seconds=5.0, sleep=sleeps.append
    )

    assert result.attempts == 1
    assert sleeps == []


def test_arm_automerge_readback_failure_counts_as_not_armed_with_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _cp()
        return _cp(returncode=1, stderr="HTTP 502 from gh pr view")

    _patch_run(monkeypatch, _handler)

    result = _mod.arm_automerge(
        42, attempts=2, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert result.status == "failed"
    assert result.gh_exit_code == 0
    assert result.gh_stderr == "HTTP 502 from gh pr view"
    assert "read-back failed" in result.detail


def test_arm_automerge_truncates_gh_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    long_stderr = "x" * (_mod.GH_STDERR_LIMIT * 2)
    _patch_run(monkeypatch, _always_failing_gh(_cp(returncode=1, stderr=long_stderr)))

    result = _mod.arm_automerge(
        42, attempts=1, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert len(result.gh_stderr) == _mod.GH_STDERR_LIMIT


@pytest.mark.parametrize(
    ("view", "expected_error"),
    [
        (_cp(returncode=1, stderr=""), "gh pr view exited 1"),
        (_cp(stdout="not json"), "could not parse gh pr view output"),
        (_cp(stdout="[]"), "not a JSON object"),
    ],
)
def test_read_back_automerge_failure_shapes(
    monkeypatch: pytest.MonkeyPatch,
    view: subprocess.CompletedProcess[str],
    expected_error: str,
) -> None:
    _patch_run(monkeypatch, lambda _cmd: view)

    state, armed, error = _mod._read_back_automerge(42)

    assert (state, armed) == ("", False)
    assert expected_error in error


def test_read_back_automerge_ignores_non_string_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, lambda _cmd: _cp(stdout='{"state": 7}'))

    assert _mod._read_back_automerge(42) == ("", False, "")


def test_cmd_arm_automerge_gh_missing_is_invocation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: None)
    args = _mod.build_parser().parse_args(["arm-automerge", _ARM_PR])

    assert _mod.cmd_arm_automerge(args) == 2
    assert "`gh` CLI not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--attempts", "0", "--attempts must be >= 1"),
        ("--backoff-seconds", "-1", "--backoff-seconds must be >= 0"),
    ],
)
def test_cmd_arm_automerge_rejects_out_of_range_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    calls = _patch_run(monkeypatch, lambda _cmd: _cp())
    args = _mod.build_parser().parse_args(["arm-automerge", _ARM_PR, flag, value])

    assert _mod.cmd_arm_automerge(args) == 2
    assert message in capsys.readouterr().err
    assert calls == []


def test_arm_automerge_parser_defaults() -> None:
    args = _mod.build_parser().parse_args(["arm-automerge", _ARM_PR])

    assert args.pr_number == int(_ARM_PR)
    assert args.attempts == _mod.ARM_MAX_ATTEMPTS == 4
    assert args.backoff_seconds == _mod.ARM_BACKOFF_SECONDS == 3.0
    assert args.repo_path is None
    assert args.func is _mod.cmd_arm_automerge


def test_cmd_arm_automerge_defaults_repo_path_to_git_toplevel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no --repo-path the seam is read from the repo gh acts on, not cwd."""
    repo = tmp_path / "repo"
    _write_project_config_yaml(repo, "pr:\n  auto_merge: false\n")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    calls = _patch_run(monkeypatch, lambda _cmd: _cp(stdout=f"{repo}\n"))
    args = _mod.build_parser().parse_args(["arm-automerge", _ARM_PR])

    assert _mod.cmd_arm_automerge(args) == _mod.EXIT_ARM_DISALLOWED
    assert calls == [["git", "rev-parse", "--show-toplevel"]]


def test_cmd_arm_automerge_without_repo_path_or_git_toplevel_is_invocation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    _patch_run(monkeypatch, lambda _cmd: _cp(returncode=128))
    args = _mod.build_parser().parse_args(["arm-automerge", _ARM_PR])

    assert _mod.cmd_arm_automerge(args) == 2
    assert "not in a git repository" in capsys.readouterr().err


def test_cmd_arm_automerge_warns_when_pyyaml_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_project_config_yaml(tmp_path, "pr:\n  auto_merge: false\n")
    monkeypatch.setattr(_mod, "yaml", None)
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    _patch_run(monkeypatch, lambda _cmd: _cp(stdout=ARM_VIEW_ARMED))
    args = _mod.build_parser().parse_args(
        ["arm-automerge", _ARM_PR, "--repo-path", str(tmp_path)]
    )

    assert _mod.cmd_arm_automerge(args) == 0
    captured = capsys.readouterr()
    assert (
        "WARNING: could not read pr.auto_merge: PyYAML unavailable; "
        "treating auto-merge as allowed" in captured.err
    )
    assert json.loads(captured.out)["status"] == "armed"


def test_arm_automerge_merged_pre_read_is_noop_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_run(monkeypatch, lambda _cmd: _cp(stdout=_ARM_VIEW_MERGED))

    result = _mod.arm_automerge(
        42, attempts=2, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert (result.status, result.attempts, result.pr_state) == ("merged", 0, "MERGED")
    assert len(calls) == 1


def test_arm_automerge_post_read_armed_returns_after_one_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views = iter([ARM_VIEW_OPEN, ARM_VIEW_ARMED])

    def _handler(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _cp(returncode=1, stderr="already queued")
        return _cp(stdout=next(views))

    _patch_run(monkeypatch, _handler)

    result = _mod.arm_automerge(
        42, attempts=3, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert (result.status, result.attempts) == ("armed", 1)
    assert result.gh_exit_code == 1


def test_arm_automerge_nonzero_exit_with_readback_failure_names_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _cp(returncode=1, stderr="merge boom")
        return _cp(returncode=1, stderr="view boom")

    _patch_run(monkeypatch, _handler)

    result = _mod.arm_automerge(
        42, attempts=1, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert result.gh_stderr == "merge boom"
    assert "exited 1" in result.detail
    assert "read-back failed: view boom" in result.detail


def test_arm_automerge_exit_zero_null_readback_detail_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, _always_failing_gh(_cp()))

    result = _mod.arm_automerge(
        42, attempts=1, backoff_seconds=0.0, sleep=lambda _s: None
    )

    assert result.gh_stderr == ""
    assert "read back null (#1140)" in result.detail


def test_cmd_arm_automerge_reports_failure_and_success_in_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(_mod.time, "sleep", lambda _s: None)
    argv = ["arm-automerge", _ARM_PR, "--repo-path", str(tmp_path), "--attempts", "1"]

    _patch_run(monkeypatch, _always_failing_gh(_cp(returncode=1, stderr="nope")))
    assert _mod.cmd_arm_automerge(_mod.build_parser().parse_args(argv)) == 1
    captured = capsys.readouterr()
    assert "failed after 1/1 attempts (exit 1): nope" in captured.err
    assert json.loads(captured.out)["status"] == "failed"

    _patch_run(monkeypatch, lambda _cmd: _cp(stdout=ARM_VIEW_ARMED))
    assert _mod.cmd_arm_automerge(_mod.build_parser().parse_args(argv)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "armed"


def test_cmd_arm_automerge_binds_gh_calls_to_repo_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gh acts on the repo whose seam was read, not on the process cwd."""
    repo = tmp_path / "repo"
    repo.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    views = iter([ARM_VIEW_OPEN, ARM_VIEW_ARMED])

    def _handler(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _cp()
        return _cp(stdout=next(views))

    cwds: list[Path | None] = []
    calls = _patch_run(monkeypatch, _handler, cwds)
    args = _mod.build_parser().parse_args(
        ["arm-automerge", _ARM_PR, "--repo-path", str(repo)]
    )

    assert _mod.cmd_arm_automerge(args) == 0
    assert [c[:3] for c in calls] == [
        ["gh", "pr", "view"],
        ["gh", "pr", "merge"],
        ["gh", "pr", "view"],
    ]
    assert cwds == [repo, repo, repo]


def test_cmd_arm_automerge_nonexistent_repo_path_is_invocation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", lambda _name: "/usr/bin/gh")
    calls = _patch_run(monkeypatch, lambda _cmd: _cp(stdout=ARM_VIEW_ARMED))
    missing = tmp_path / "missing"
    args = _mod.build_parser().parse_args(
        ["arm-automerge", _ARM_PR, "--repo-path", str(missing)]
    )

    assert _mod.cmd_arm_automerge(args) == 2
    assert f"--repo-path is not a directory: {missing}" in capsys.readouterr().err
    assert calls == []
