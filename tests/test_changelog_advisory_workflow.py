"""Guard tests for `.github/workflows/changelog-advisory.yml` (#1532).

The workflow emits a non-blocking `::warning::` annotation when a PR's diff
touches `src/cw/**` without touching `CHANGELOG.md`. It is advisory ONLY:
docs-only, test-only, and dark-release plumbing PRs legitimately have nothing
to document, so the job must never fail.

Group A mirrors `tests/test_pr_events_workflow.py`'s `read_text()` +
literal-substring convention for YAML-shape assertions. Group B actually
executes the check step's shell script via `subprocess.run(["/bin/bash", ...])`
with a synthetic `CHANGED_FILES` -- the only way to falsify the "unguarded
`grep` under `set -e` fails the job on an empty diff" trap, which Group A can
see the shape of but not the runtime behavior.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "changelog-advisory.yml"
README_PATH = ROOT / ".github" / "workflows" / "README.md"

LIST_STEP_ID = "list-files"
CHECK_STEP_ID = "check"
WARNING_IDIOM = "::warning::"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW_PATH.read_text())
    return workflow


def _on_block() -> dict[str, Any]:
    # PyYAML's SafeLoader follows YAML 1.1, which parses the bare `on` scalar
    # key as the boolean `True` rather than the string "on" -- a well-known
    # GitHub Actions YAML gotcha.
    on_block: dict[str, Any] = _workflow()[True]
    return on_block


def _step(step_id: str) -> dict[str, Any]:
    workflow = _workflow()
    steps = workflow["jobs"]["check-changelog"]["steps"]
    step: dict[str, Any] = next(s for s in steps if s.get("id") == step_id)
    return step


def _check_script() -> str:
    script: str = _step(CHECK_STEP_ID)["run"]
    return script


def _list_script() -> str:
    script: str = _step(LIST_STEP_ID)["run"]
    return script


def _run_check_step(changed_files: str) -> subprocess.CompletedProcess[str]:
    """Run the check step's script with a synthetic `CHANGED_FILES`."""
    env = {
        "CHANGED_FILES": changed_files,
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/bash", "-c", _check_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _run_list_step(
    tmp_path: Path, *, gh_exit_code: int, gh_stdout: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the list-files step's script against a stubbed `gh` binary."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    # Quoted heredoc ('GH_STDOUT_EOF') -- no shell interpolation of gh_stdout's
    # contents, matching how a real `gh api --paginate` payload is opaque data.
    script = (
        f"#!/bin/sh\ncat <<'GH_STDOUT_EOF'\n{gh_stdout}GH_STDOUT_EOF\n"
        f"exit {gh_exit_code}\n"
    )
    fake_gh.write_text(script)
    fake_gh.chmod(0o755)
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    env = {
        "REPO": "example-org/example-repo",
        "PR_NUMBER": "1",
        "GITHUB_OUTPUT": str(output_file),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", _list_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, output_file


# --- Group A: YAML-level structural assertions ---


def test_trigger_is_pull_request() -> None:
    on_block = _on_block()
    assert "pull_request" in on_block
    assert "push" not in on_block


def test_job_never_fails_the_workflow() -> None:
    assert _check_script().strip().endswith("exit 0")


def test_uses_warning_annotation_idiom() -> None:
    assert WARNING_IDIOM in _check_script()


def test_readme_documents_new_workflow() -> None:
    rows = [
        line
        for line in README_PATH.read_text().splitlines()
        if line.startswith("|") and "`changelog-advisory.yml`" in line
    ]
    assert len(rows) == 1, rows


def test_list_files_uses_paginate_not_pr_view() -> None:
    """Static guard against reverting to the silently-truncating `gh pr view` form.

    `gh pr view --json files` caps at 100 changed files with no error or
    truncation signal (verified against kubernetes/kubernetes#137050: 219
    files, only 100 returned). `gh api ... --paginate` is the one command
    shape that doesn't have this cap -- pin it directly so a future "simplify
    this" edit can't silently reintroduce the truncation.
    """
    script = _list_script()
    assert "--paginate" in script
    assert "gh pr view" not in script


def test_list_pr_files_step_fails_open() -> None:
    """A `gh api` hiccup must not fail the job (regression guard).

    No trigger-level `paths:` filter means this workflow runs on every PR; a
    hard failure here would read as a real CI failure and risks the check
    eventually being marked "required" in branch protection.
    """
    script = _list_script()
    # Anchor on the actual guard operator, not the first bare "||" -- the
    # script's own explanatory comment ("Mirrors dispatch-guard.yml's
    # `|| true` fail-open idiom") contains a literal "||" earlier in the
    # string than the real `FILES=$(...) || {` guard.
    guard_index = script.index(") || {")
    guard_branch = script[guard_index:]
    assert "exit 0" in guard_branch
    assert WARNING_IDIOM in guard_branch


def test_list_pr_files_step_fails_open_on_gh_api_error(tmp_path: Path) -> None:
    """Executed regression guard: a real failing `gh api` call must not fail the job.

    `test_list_pr_files_step_fails_open` above only inspects the script's
    text; this actually runs the step's shell against a stubbed `gh` that
    exits non-zero, the way Group B exercises the check step's real logic.
    """
    result, output_file = _run_list_step(tmp_path, gh_exit_code=1)
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM in result.stdout

    lines = output_file.read_text().splitlines()
    assert lines[0].startswith("files<<")
    delim = lines[0].removeprefix("files<<")
    assert lines[1] == delim, "expected an empty multiline value between the delimiters"


def test_list_files_step_captures_full_paginated_output(tmp_path: Path) -> None:
    """Executed regression guard: the list-files step must capture ALL of a
    multi-page `gh api --paginate` response, not just the first 100 entries.

    `test_detects_src_cw_path_beyond_first_page` (Group B, below) only proves
    the *check* step's grep can scan a long string -- it never runs the
    list-files step's real `gh api --paginate` invocation. This test runs
    that step for real, against a stubbed `gh` emitting 105 filenames (one
    under `src/cw/`, positioned past entry 100), and asserts the full payload
    reaches `$GITHUB_OUTPUT` intact. This is the test that would have caught
    a regression to the truncating `gh pr view --json files` form.
    """
    filenames = [f"docs/filler_{index:03d}.md" for index in range(104)]
    filenames.insert(102, "src/cw/late.py")
    gh_stdout = "\n".join(filenames) + "\n"

    result, output_file = _run_list_step(tmp_path, gh_exit_code=0, gh_stdout=gh_stdout)
    assert result.returncode == 0, result.stderr

    lines = output_file.read_text().splitlines()
    assert lines[0].startswith("files<<")
    delim = lines[0].removeprefix("files<<")
    assert lines[-1] == delim
    captured = lines[1:-1]
    assert "src/cw/late.py" in captured
    assert len(captured) == len(filenames)


# --- Group B: literal shell exercise of the check logic ---


def test_warns_when_src_cw_changed_without_changelog() -> None:
    result = _run_check_step("src/cw/foo.py")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM in result.stdout


def test_no_warning_when_src_cw_and_changelog_both_changed() -> None:
    result = _run_check_step("src/cw/foo.py\nCHANGELOG.md")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM not in result.stdout


def test_no_warning_for_docs_or_tests_only() -> None:
    result = _run_check_step("docs/x.md\ntests/y.py")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM not in result.stdout


def test_no_warning_when_nothing_under_src_cw_touched() -> None:
    result = _run_check_step(".github/workflows/foo.yml\nREADME.md")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM not in result.stdout


def test_never_fails_even_on_empty_diff() -> None:
    """`grep` finding no match must not trip `set -e` (guarded by `if`)."""
    result = _run_check_step("")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM not in result.stdout


def test_changelog_touched_alone_without_src_cw_change() -> None:
    result = _run_check_step("CHANGELOG.md")
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM not in result.stdout


def test_detects_src_cw_path_beyond_first_page() -> None:
    """The check must scan the FULL file list, not just the first 100 entries.

    Regression guard on the check step's own grep logic. Live pagination
    correctness lives in the `gh api --paginate` flag, not here.
    """
    lines = [f"docs/filler_{index:03d}.md" for index in range(105)]
    lines.insert(103, "src/cw/late.py")
    result = _run_check_step("\n".join(lines))
    assert result.returncode == 0, result.stderr
    assert WARNING_IDIOM in result.stdout
