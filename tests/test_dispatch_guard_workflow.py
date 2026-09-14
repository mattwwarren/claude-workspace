"""Guard tests for `.github/workflows/dispatch-guard.yml` (#2151)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

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


def _stub_gh_create(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" != "issue create" ]; then\n'
        '  echo "unexpected gh invocation: $@" >&2\n'
        "  exit 2\n"
        "fi\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--body" ]; then\n'
        "    shift\n"
        '    printf "%s" "$1" > "$(dirname "$0")/../created-body.txt"\n'
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        'echo "missing --body" >&2\n'
        "exit 2\n"
    )
    fake_gh.chmod(0o755)
    return fake_bin


def _stub_gh_issue_list(tmp_path: Path, *, list_json: str) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" != "issue list" ]; then\n'
        '  echo "unexpected gh invocation: $@" >&2\n'
        "  exit 2\n"
        "fi\n"
        "jq_filter=\n"
        "app_filter=\n"
        "previous=\n"
        'for arg in "$@"; do\n'
        '  if [ "$previous" = "--app" ]; then\n'
        '    app_filter="$arg"\n'
        "  fi\n"
        '  if [ "$previous" = "--jq" ]; then\n'
        '    jq_filter="$arg"\n'
        "    break\n"
        "  fi\n"
        '  previous="$arg"\n'
        "done\n"
        'if [ "$app_filter" != "github-actions" ]; then\n'
        '  echo "missing --app github-actions filter" >&2\n'
        "  exit 2\n"
        "fi\n"
        'if [ -z "$jq_filter" ]; then\n'
        '  echo "missing --jq filter" >&2\n'
        "  exit 2\n"
        "fi\n"
        "/usr/bin/jq "
        f"'map(select(.author.login == \"{DISPATCH_DRIFT_AUTO_AUTHOR}\"))' "
        "<<'GH_LIST_EOF' | /usr/bin/jq -r \"$jq_filter\"\n"
        f"{list_json}\nGH_LIST_EOF\n"
    )
    fake_gh.chmod(0o755)
    return fake_bin


def test_open_issue_step_executed_body_contains_marker(tmp_path: Path) -> None:
    fake_bin = _stub_gh_create(tmp_path)
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
    fake_bin = _stub_gh_issue_list(
        tmp_path,
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
