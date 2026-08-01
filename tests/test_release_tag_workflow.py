"""Guard tests for `.github/workflows/release-tag.yml` (#1531).

Two concerns live here:

* the pre-existing `guard` step's decision chain (match / loud-fail /
  silent-skip), which had zero coverage despite being the single point where
  a mis-shaped `chore(release):` subject is supposed to fail loudly; and
* the new `version-drift-warning` step, which turns the silent-skip arm into
  an observable one — if `pyproject.toml`'s version has moved past the latest
  tag on a push whose subject was *not* recognized as a release commit, the
  release very likely failed to tag and nobody found out.

Structure mirrors `test_pr_events_workflow.py`: Group A does `yaml.safe_load`
plus shape/index assertions, Group B executes the literal `run:` blocks via
`subprocess.run(["/bin/bash", "-c", ...])`.  Unlike that file, these scripts
call real `git` subcommands, so they need a real repo: `_repo_with_remote`
builds one on the `make_git_repo` fixture plus the separate-bare-origin shape
from `tests/test_worktree.py`'s
`test_new_branch_base_is_origin_main_not_operator_head`.  A bare remote (not a
self-remote) is required because
`test_warn_step_only_reads_pyproject_and_remote_tags_not_local_tags` must
plant a local tag that diverges from the pushed tag set, which a shared ref
store cannot represent.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tests.conftest import _clean_git_env

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-tag.yml"
JOB = "tag-release"
GUARD_STEP_ID = "guard"
WARN_STEP_ID = "version-drift-warning"
WARNING_PREFIX = "::warning::"
ERROR_PREFIX = "::error::"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW_PATH.read_text())
    return workflow


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][JOB]["steps"]
    return steps


def _step_index(step_id: str) -> int:
    for index, step in enumerate(_steps()):
        if step.get("id") == step_id:
            return index
    msg = f"No step with id {step_id!r} in {WORKFLOW_PATH}"
    raise AssertionError(msg)


def _step(step_id: str) -> dict[str, Any]:
    return _steps()[_step_index(step_id)]


def _script(step_id: str) -> str:
    script: str = _step(step_id)["run"]
    return script


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    )


def _repo_with_remote(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    *,
    head_subject: str,
    pyproject_version: str,
    remote_tags: list[str],
) -> Path:
    """Working repo plus a genuinely separate bare `origin` carrying the tags.

    Tags are created locally, pushed, then deleted locally, so the working
    copy's own tag namespace stays empty and `git ls-remote` is the only way
    to see them.
    """
    repo = make_git_repo("workspace")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "cw"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", head_subject)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")

    for tag in remote_tags:
        _git(repo, "tag", tag)
        _git(repo, "push", "origin", tag)
        _git(repo, "tag", "-d", tag)

    return repo


def _run_step(
    step_id: str, repo: Path
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a step's literal `run:` block in `repo`; return result + outputs."""
    script = _script(step_id)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env") as handle:
        output_path = Path(handle.name)
    try:
        env = {**_clean_git_env(), "GITHUB_OUTPUT": str(output_path)}
        result = subprocess.run(
            # -eo pipefail matches GitHub Actions' actual default `run:` shell
            # (`bash --noprofile --norc -eo pipefail {0}`) -- without it, a
            # grep-no-match mid-pipeline is silently swallowed here even
            # though it aborts the step for real on the runner (#1531).
            ["/bin/bash", "-eo", "pipefail", "-c", script],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        outputs: dict[str, str] = {}
        for line in output_path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return result, outputs
    finally:
        output_path.unlink(missing_ok=True)


# --- Group A: YAML shape ---


def test_guard_step_precedes_warn_step() -> None:
    assert _step_index(GUARD_STEP_ID) < _step_index(WARN_STEP_ID)


def test_warn_step_gated_on_match_false() -> None:
    assert "steps.guard.outputs.match == 'false'" in _step(WARN_STEP_ID)["if"]


def test_warn_step_does_not_gate_on_dry_run() -> None:
    # Read-only/advisory by construction, so unlike the tag/release-creation
    # steps it must still report during a dry run.
    assert "dry_run" not in _step(WARN_STEP_ID)["if"]


# --- Group B: literal shell exercise ---


def test_guard_valid_chore_release_subject_matches(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="chore(release): bump version to 1.24.1",
        pyproject_version="1.24.1",
        remote_tags=[],
    )
    result, outputs = _run_step(GUARD_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert outputs["match"] == "true"
    assert outputs["version"] == "1.24.1"


def test_guard_chore_release_bad_version_still_hard_fails(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="chore(release): oops",
        pyproject_version="1.24.1",
        remote_tags=[],
    )
    result, outputs = _run_step(GUARD_STEP_ID, repo)
    assert result.returncode == 1
    assert ERROR_PREFIX in result.stdout + result.stderr
    assert "match" not in outputs


def test_guard_non_release_subject_match_false(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """R7 item 5's other half: the guard itself, not just the warn step.

    The warn-step tests below prove the warn script stays silent on an
    ordinary merge, but none of them ever invoke the guard step itself for
    that subject -- so a regression that made the guard start matching
    ordinary commits would go undetected. Close that loop directly.
    """
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="merge widget refactor",
        pyproject_version="1.24.0",
        remote_tags=["v1.24.0"],
    )
    result, outputs = _run_step(GUARD_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert outputs["match"] == "false"


def test_warn_no_warning_when_pyproject_matches_latest_tag(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="merge widget refactor",
        pyproject_version="1.24.0",
        remote_tags=["v1.24.0"],
    )
    result, _outputs = _run_step(WARN_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert WARNING_PREFIX not in result.stdout + result.stderr


def test_warn_emits_when_pyproject_ahead_of_latest_tag(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The case the whole ticket exists for: bump landed, tagging never fired."""
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="merge widget refactor",
        pyproject_version="1.24.1",
        remote_tags=["v1.24.0"],
    )
    result, _outputs = _run_step(WARN_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert WARNING_PREFIX in result.stdout
    assert "1.24.1" in result.stdout
    assert "v1.24.0" in result.stdout


def test_warn_skips_cleanly_with_no_tags_yet(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="merge widget refactor",
        pyproject_version="1.24.1",
        remote_tags=[],
    )
    result, _outputs = _run_step(WARN_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert WARNING_PREFIX not in result.stdout + result.stderr


def test_warn_step_only_reads_pyproject_and_remote_tags_not_local_tags(
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A local-only tag must not be mistaken for the latest released version.

    The runner's default shallow `actions/checkout` populates no tags at all,
    so `git tag --list` is not merely a different answer here -- it is empty
    in production. Pin the `ls-remote` contract.
    """
    repo = _repo_with_remote(
        make_git_repo,
        tmp_path,
        head_subject="merge widget refactor",
        pyproject_version="1.24.0",
        remote_tags=["v1.24.0"],
    )
    _git(repo, "tag", "v2.0.0")

    result, _outputs = _run_step(WARN_STEP_ID, repo)
    assert result.returncode == 0, result.stderr
    assert WARNING_PREFIX not in result.stdout + result.stderr
    assert "v2.0.0" not in result.stdout
