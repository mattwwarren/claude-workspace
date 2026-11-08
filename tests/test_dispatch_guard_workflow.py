"""Guard tests for `.github/workflows/dispatch-guard.yml` (#2151)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tests._tool_fakes import stub_gh_issue_create, stub_gh_issue_list
from tests.conftest import _clean_git_env, _load_workflow

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "dispatch-guard.yml"
JOB = "check-drift"
OPEN_ISSUE_STEP_NAME = "Open dispatch-drift issue"
EXISTING_ISSUE_STEP_NAME = "Check for existing open dispatch-drift issue"
DISPATCH_DRIFT_AUTO_MARKER = "<!-- dispatch-guard-auto -->"
DISPATCH_DRIFT_AUTO_AUTHOR = "app/github-actions"
DISPATCH_DRIFT_LEGACY_MARKER = (
    "Opened automatically by the [Dispatch Guard workflow]"
    "(/.github/workflows/dispatch-guard.yml). Closes when a release tag is pushed."
)


def _workflow() -> dict[Any, Any]:
    return _load_workflow(WORKFLOW_PATH)


def _open_issue_step() -> dict[str, Any]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][JOB]["steps"]
    return next(step for step in steps if step.get("name") == OPEN_ISSUE_STEP_NAME)


def _existing_issue_step() -> dict[str, Any]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][JOB]["steps"]
    return next(step for step in steps if step.get("name") == EXISTING_ISSUE_STEP_NAME)


def test_job_env_declares_dispatch_drift_marker() -> None:
    assert (
        _workflow()["jobs"][JOB]["env"]["DISPATCH_DRIFT_AUTO_MARKER"]
        == DISPATCH_DRIFT_AUTO_MARKER
    )
    assert (
        _workflow()["jobs"][JOB]["env"]["DISPATCH_DRIFT_LEGACY_MARKER"]
        == DISPATCH_DRIFT_LEGACY_MARKER
    )


def test_open_issue_step_body_references_marker_env_var() -> None:
    assert "${DISPATCH_DRIFT_AUTO_MARKER}" in _open_issue_step()["run"]


def test_existing_issue_step_filters_by_provenance() -> None:
    script = _existing_issue_step()["run"]
    assert "--app github-actions" in script
    assert "--limit 1000" in script
    assert "--json number,body" in script
    assert "$DISPATCH_DRIFT_AUTO_MARKER" in script
    assert "$DISPATCH_DRIFT_LEGACY_MARKER" in script


def test_open_issue_step_executed_body_contains_marker(tmp_path: Path) -> None:
    fake_bin = stub_gh_issue_create(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "-eo", "pipefail", "-c", _open_issue_step()["run"]],
        cwd=tmp_path,
        env={
            **_clean_git_env(),
            "LAST_TAG": "v1.45.8",
            "CHANGED_FILES": "src/cw/dispatch/claim.py\nsrc/cw/spawn.py",
            "DISPATCH_DRIFT_AUTO_MARKER": DISPATCH_DRIFT_AUTO_MARKER,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        (tmp_path / "created-body.txt").read_text().endswith(DISPATCH_DRIFT_AUTO_MARKER)
    )


def test_existing_issue_step_ignores_human_labels_and_keeps_legacy_guard_issue(
    tmp_path: Path,
) -> None:
    fake_bin = stub_gh_issue_list(
        tmp_path,
        author=DISPATCH_DRIFT_AUTO_AUTHOR,
        list_json=json.dumps(
            [
                {
                    "number": 101,
                    "body": f"{DISPATCH_DRIFT_AUTO_MARKER}\n",
                    "author": {"login": DISPATCH_DRIFT_AUTO_AUTHOR},
                },
                {
                    "number": 202,
                    "body": f"Human-applied label\n{DISPATCH_DRIFT_AUTO_MARKER}",
                    "author": {"login": "human-user"},
                },
                {
                    "number": 303,
                    "body": DISPATCH_DRIFT_LEGACY_MARKER,
                    "author": {"login": DISPATCH_DRIFT_AUTO_AUTHOR},
                },
            ]
        ),
    )
    output_file = tmp_path / "github-output"
    output_file.write_text("")
    result = subprocess.run(
        ["/bin/bash", "-eo", "pipefail", "-c", _existing_issue_step()["run"]],
        cwd=tmp_path,
        env={
            **_clean_git_env(),
            "GITHUB_OUTPUT": str(output_file),
            "DISPATCH_DRIFT_AUTO_MARKER": DISPATCH_DRIFT_AUTO_MARKER,
            "DISPATCH_DRIFT_LEGACY_MARKER": DISPATCH_DRIFT_LEGACY_MARKER,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output_file.read_text().strip() == "count=2"
