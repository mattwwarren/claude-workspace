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

from tests.conftest import _clean_git_env, _stub_gh

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-tag.yml"
JOB = "tag-release"
GUARD_STEP_ID = "guard"
WARN_STEP_ID = "version-drift-warning"
CHECKPOINT_STEP_ID = "notes-checkpoint"
CLOSE_DRIFT_STEP_ID = "close-drift-issues"
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
    step_id: str, repo: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a step's literal `run:` block in `repo`; return result + outputs.

    `extra_env` stands in for the step's `env:` block, which the literal
    `run:` text cannot supply on its own (see the checkpoint tests below).
    """
    script = _script(step_id)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env") as handle:
        output_path = Path(handle.name)
    try:
        env = {
            **_clean_git_env(),
            "GITHUB_OUTPUT": str(output_path),
            **(extra_env or {}),
        }
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


# --- Group C: release-notes checkpoint (#1609) ---
#
# The checkpoint step reads its inputs from `env:` (not inline `${{ }}`
# interpolation), so these tests can drive all five branches of its
# conditional by injecting VERSION / EXISTS / NOTES_FOUND directly.  Each
# test owns its own `$GITHUB_STEP_SUMMARY` file (`_run_checkpoint`) because
# `_run_step` deliberately does not -- the seven guard/warn call sites above
# must stay untouched.

CHECKPOINT_VERSION = "1.24.1"
NOTES_TEXT = "### Fixed\n\n- Surface discarded release notes (#1609)\n"
DISCARDED_HEADING = f"#### Discarded CHANGELOG notes for v{CHECKPOINT_VERSION}"
SUMMARY_HEADER = f"### Release notes checkpoint: v{CHECKPOINT_VERSION}"
UNEXPECTED_STATE = "unknown"

EXISTS_AND_NOTES_WARNING = (
    f"::warning::Release v{CHECKPOINT_VERSION} already existed when this run "
    "reached release creation. The CHANGELOG-derived notes this run extracted "
    "were not applied to the release body — verify the published notes for "
    f"v{CHECKPOINT_VERSION}."
)
EXISTS_AND_NOTES_NOTICE = (
    "The release already existed when this run reached release creation, so "
    "the CHANGELOG-derived notes this run extracted were not applied to the "
    "release body. Verify the published notes for "
    f"v{CHECKPOINT_VERSION} match CHANGELOG.md."
)
EXISTS_NO_NOTES_WARNING = (
    f"::warning::Release v{CHECKPOINT_VERSION} already existed when this run "
    "reached release creation — this run took no release-creation action. No "
    f"CHANGELOG section was found for v{CHECKPOINT_VERSION} either; verify the "
    "published release notes directly."
)
NO_NOTES_WARNING = (
    f"::warning::No CHANGELOG section found for v{CHECKPOINT_VERSION} "
    f"(expected heading '## [{CHECKPOINT_VERSION}] — <date>'). Release "
    f"v{CHECKPOINT_VERSION} shipped with autogenerated (--generate-notes) "
    "notes instead of CHANGELOG content."
)
SHIPPED_NOTICE = (
    f"CHANGELOG-derived notes for v{CHECKPOINT_VERSION} shipped as the release body."
)
UNEXPECTED_STATE_WARNING = (
    "::warning::Unexpected release-notes-checkpoint state "
    f"(exists={UNEXPECTED_STATE}, notes_found={UNEXPECTED_STATE}) for "
    f"v{CHECKPOINT_VERSION} — verify the published release manually."
)


def _runner_temp(tmp_path: Path, notes: str | None) -> Path:
    """A `$RUNNER_TEMP` dir; `notes=None` leaves `changelog-notes.txt` absent."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    if notes is not None:
        (runner_temp / "changelog-notes.txt").write_text(notes, encoding="utf-8")
    return runner_temp


def _run_checkpoint(
    repo: Path, extra_env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the checkpoint step with a caller-owned `$GITHUB_STEP_SUMMARY`.

    Returns the process result plus the summary file's accumulated text.
    """
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".md") as handle:
        summary_path = Path(handle.name)
    try:
        result, _outputs = _run_step(
            CHECKPOINT_STEP_ID,
            repo,
            extra_env={"GITHUB_STEP_SUMMARY": str(summary_path), **extra_env},
        )
        return result, summary_path.read_text()
    finally:
        summary_path.unlink(missing_ok=True)


def _checkpoint_env(runner_temp: Path, exists: str, notes_found: str) -> dict[str, str]:
    return {
        "VERSION": CHECKPOINT_VERSION,
        "EXISTS": exists,
        "NOTES_FOUND": notes_found,
        "RUNNER_TEMP": str(runner_temp),
    }


def test_checkpoint_exists_true_notes_found_true_emits_content_and_warning(
    tmp_path: Path,
) -> None:
    """The case the ticket exists for: notes were extracted, then discarded.

    Naming the file in the log is not enough -- the runner's `$RUNNER_TEMP` is
    gone by the time anyone reads the run, so the notes text itself has to
    land in the job summary or it is unrecoverable.
    """
    runner_temp = _runner_temp(tmp_path, NOTES_TEXT)
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "true", "true")
    )
    assert result.returncode == 0, result.stderr
    assert EXISTS_AND_NOTES_WARNING in result.stdout
    assert EXISTS_AND_NOTES_NOTICE in summary
    assert DISCARDED_HEADING in summary
    assert NOTES_TEXT in summary


def test_checkpoint_exists_true_notes_found_false_warning_byte_identical(
    tmp_path: Path,
) -> None:
    runner_temp = _runner_temp(tmp_path, NOTES_TEXT)
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "true", "false")
    )
    assert result.returncode == 0, result.stderr
    assert EXISTS_NO_NOTES_WARNING in result.stdout
    # A populated notes file must not leak into a branch that never extracted one.
    assert DISCARDED_HEADING not in summary
    assert NOTES_TEXT not in summary


def test_checkpoint_notes_found_false_exists_false_warning_byte_identical(
    tmp_path: Path,
) -> None:
    runner_temp = _runner_temp(tmp_path, NOTES_TEXT)
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "false", "false")
    )
    assert result.returncode == 0, result.stderr
    assert NO_NOTES_WARNING in result.stdout
    assert DISCARDED_HEADING not in summary


def test_checkpoint_exists_false_notes_found_true_no_warning_no_new_content(
    tmp_path: Path,
) -> None:
    """The happy path: notes shipped as the release body, nothing to surface."""
    runner_temp = _runner_temp(tmp_path, NOTES_TEXT)
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "false", "true")
    )
    assert result.returncode == 0, result.stderr
    assert WARNING_PREFIX not in result.stdout + result.stderr
    assert summary.splitlines() == [SUMMARY_HEADER, SHIPPED_NOTICE]


def test_checkpoint_unexpected_state_warning_byte_identical(tmp_path: Path) -> None:
    runner_temp = _runner_temp(tmp_path, NOTES_TEXT)
    result, summary = _run_checkpoint(
        tmp_path,
        _checkpoint_env(runner_temp, UNEXPECTED_STATE, UNEXPECTED_STATE),
    )
    assert result.returncode == 0, result.stderr
    assert UNEXPECTED_STATE_WARNING in result.stdout
    assert DISCARDED_HEADING not in summary


def test_checkpoint_missing_notes_file_no_bare_heading(tmp_path: Path) -> None:
    """`notes_found=true` with no file on disk must not emit an empty section."""
    runner_temp = _runner_temp(tmp_path, None)
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "true", "true")
    )
    assert result.returncode == 0, result.stderr
    assert EXISTS_AND_NOTES_NOTICE in summary
    assert EXISTS_AND_NOTES_WARNING in result.stdout
    assert DISCARDED_HEADING not in summary


def test_checkpoint_empty_notes_file_no_bare_heading(tmp_path: Path) -> None:
    runner_temp = _runner_temp(tmp_path, "")
    result, summary = _run_checkpoint(
        tmp_path, _checkpoint_env(runner_temp, "true", "true")
    )
    assert result.returncode == 0, result.stderr
    assert EXISTS_AND_NOTES_NOTICE in summary
    assert EXISTS_AND_NOTES_WARNING in result.stdout
    assert DISCARDED_HEADING not in summary


# --- Group D: dispatch-drift closer (#1799) ---
#
# `dispatch-guard.yml` opens `dispatch-drift` issues; only `release.yml`'s
# manual-tag path ever closed them, so the automated path this workflow owns
# left them open forever. The steps below pin the closer's placement, its
# gate, the `issues: write` grant it needs, and the literal shell it runs.

DRY_RUN_SUMMARY_NAME = "Dry-run summary"
WARN_STEP_NAME = "Warn if dispatch-drift closer failed"
DRIFT_LABEL = "dispatch-drift"
CLOSE_RELEASE_TAG = "v1.33.0"
CLOSE_COMMENT = (
    f"Closed by release {CLOSE_RELEASE_TAG} — "
    "dispatch/reconcile/spawn changes are now shipped."
)
NO_DRIFT_LINE = "No open dispatch-drift issues — WOULD close 0 issues"


def _step_index_by_name(name: str) -> int:
    for index, step in enumerate(_steps()):
        if step.get("name") == name:
            return index
    msg = f"No step named {name!r} in {WORKFLOW_PATH}"
    raise AssertionError(msg)


def test_close_drift_step_is_inserted_between_checkpoint_and_dry_run_summary() -> None:
    steps = _steps()
    assert _step_index(CHECKPOINT_STEP_ID) + 1 == _step_index(CLOSE_DRIFT_STEP_ID)
    assert steps[-1]["name"] == DRY_RUN_SUMMARY_NAME


def test_warn_step_is_inserted_between_close_drift_and_dry_run_summary() -> None:
    steps = _steps()
    assert _step_index(CLOSE_DRIFT_STEP_ID) + 1 == _step_index_by_name(WARN_STEP_NAME)
    assert _step_index_by_name(WARN_STEP_NAME) + 1 == len(steps) - 1
    assert steps[-1]["name"] == DRY_RUN_SUMMARY_NAME


def test_warn_step_gated_on_close_drift_failure() -> None:
    """`continue-on-error` on the closer step means its failure is otherwise
    invisible behind a green job — this step is the only surfaced signal."""
    step = next(s for s in _steps() if s.get("name") == WARN_STEP_NAME)
    assert step["if"] == "steps.close-drift-issues.outcome == 'failure'"


def test_warn_step_emits_warning_annotation() -> None:
    step = next(s for s in _steps() if s.get("name") == WARN_STEP_NAME)
    assert "::warning::" in step["run"]


def test_close_drift_step_gated_on_match_true_and_not_dry_run() -> None:
    condition = _step(CLOSE_DRIFT_STEP_ID)["if"]
    assert "steps.guard.outputs.match == 'true'" in condition
    assert "inputs.dry_run != true" in condition


def test_close_drift_step_tags_release_from_guard_output_not_ref_name() -> None:
    """`release.yml` can use `github.ref_name`; this workflow cannot.

    That workflow triggers on `push: tags: ['v*']`, so its `github.ref_name`
    genuinely is the tag. Here the trigger is a push to `main` (or a manual
    dispatch), where `github.ref_name` is a branch — the tag only exists as
    `v` + the guard step's parsed version.
    """
    release_tag = _step(CLOSE_DRIFT_STEP_ID)["env"]["RELEASE_TAG"]
    assert release_tag == "v${{ steps.guard.outputs.version }}"
    assert "github.ref_name" not in release_tag


def test_permissions_grant_issues_write_alongside_contents_write() -> None:
    # Closing an issue needs `issues: write`; the workflow-level block stays
    # at `contents: read` and only this job widens it.
    assert _workflow()["jobs"][JOB]["permissions"] == {
        "contents": "write",
        "issues": "write",
    }
    assert _workflow()["permissions"] == {"contents": "read"}


def _stub_gh_close_flow(tmp_path: Path, *, list_stdout: str) -> Path:
    """`gh` stub answering `issue list` and logging every `issue close`.

    Distinct from `conftest._stub_gh` (one fixed payload for every
    invocation): the closer step calls two different subcommands in one
    pipeline, so the stub has to branch on argv and record what it was asked
    to close. Single consumer, so it stays file-local.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "issue list" ]; then\n'
        f"  cat <<'GH_LIST_EOF'\n{list_stdout}GH_LIST_EOF\n"
        "else\n"
        '  echo "$@" >> "$(dirname "$0")/../gh-calls.log"\n'
        "fi\n"
    )
    fake_gh.chmod(0o755)
    return fake_bin


def _close_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_close_drift_step_closes_every_open_drift_issue_with_release_citation(
    tmp_path: Path,
) -> None:
    fake_bin = _stub_gh_close_flow(tmp_path, list_stdout="101\n202\n")
    result, _outputs = _run_step(
        CLOSE_DRIFT_STEP_ID,
        tmp_path,
        extra_env={
            "RELEASE_TAG": CLOSE_RELEASE_TAG,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _close_calls(tmp_path) == [
        f"issue close 101 --comment {CLOSE_COMMENT}",
        f"issue close 202 --comment {CLOSE_COMMENT}",
    ]


def test_close_drift_step_closes_nothing_when_no_drift_issues_open(
    tmp_path: Path,
) -> None:
    """An empty issue list must be a clean no-op, not a failed step.

    `run:` blocks execute under `-eo pipefail`, so a mis-shaped pipeline here
    would fail the whole job on the ordinary case (most releases close zero
    drift issues) rather than on the rare one.
    """
    fake_bin = _stub_gh_close_flow(tmp_path, list_stdout="")
    result, _outputs = _run_step(
        CLOSE_DRIFT_STEP_ID,
        tmp_path,
        extra_env={
            "RELEASE_TAG": CLOSE_RELEASE_TAG,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _close_calls(tmp_path) == []


def test_close_drift_step_queries_only_open_drift_labelled_issues() -> None:
    """The query is repo-global, so its filters are the only blast-radius guard."""
    script = _script(CLOSE_DRIFT_STEP_ID)
    assert '--label "$DISPATCH_DRIFT_LABEL"' in script
    assert "--state open" in script


def test_job_declares_dispatch_drift_label_once_for_both_consumers() -> None:
    """Single source of truth: the close step and the dry-run summary both
    read `$DISPATCH_DRIFT_LABEL` from job-level `env:` rather than each
    hardcoding the literal, so relabeling can't desync one from the other."""
    assert _workflow()["jobs"][JOB]["env"]["DISPATCH_DRIFT_LABEL"] == DRIFT_LABEL
    assert '--label "$DISPATCH_DRIFT_LABEL"' in _script(CLOSE_DRIFT_STEP_ID)
    assert '--label "$DISPATCH_DRIFT_LABEL"' in _dry_run_summary_script()


def test_close_drift_step_has_continue_on_error() -> None:
    """The tag/release are already shipped by earlier steps in this job; a
    transient gh API hiccup while closing housekeeping issues afterward
    shouldn't flip an otherwise-successful release job red."""
    assert _step(CLOSE_DRIFT_STEP_ID)["continue-on-error"] is True


def _dry_run_summary_script() -> str:
    step = next(s for s in _steps() if s.get("name") == DRY_RUN_SUMMARY_NAME)
    script: str = step["run"]
    return script


def _render_dry_run_summary(
    *,
    match: str,
    version: str,
    tag_exists: str,
    release_exists: str,
    notes_found: str,
) -> str:
    """Substitute the step's `${{ }}` tokens with literal values.

    Unlike every other step exercised in this file, "Dry-run summary" reads
    its inputs through inline `${{ steps.X.outputs.Y }}` interpolation, which
    GitHub Actions replaces server-side *before* bash ever sees the script.
    Plain bash would choke on the literal tokens ("bad substitution"), so the
    substitution has to be reproduced here.
    """
    script = _dry_run_summary_script()
    for token, value in (
        ("${{ steps.guard.outputs.match }}", match),
        ("${{ steps.guard.outputs.version }}", version),
        ("${{ steps.tag.outputs.exists }}", tag_exists),
        ("${{ steps.release.outputs.exists }}", release_exists),
        ("${{ steps.changelog.outputs.notes_found }}", notes_found),
    ):
        script = script.replace(token, value)
    assert "${{" not in script, f"unsubstituted expression left in script: {script}"
    return script


def _run_dry_run_summary(
    fake_bin: Path, *, match: str = "true"
) -> subprocess.CompletedProcess[str]:
    script = _render_dry_run_summary(
        match=match,
        version="1.33.0",
        tag_exists="true",
        release_exists="true",
        notes_found="true",
    )
    return subprocess.run(
        ["/bin/bash", "-eo", "pipefail", "-c", script],
        env={**_clean_git_env(), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_dry_run_summary_reports_would_close_open_drift_issues(tmp_path: Path) -> None:
    fake_bin = _stub_gh(tmp_path, exit_code=0, stdout="101\n202\n")
    result = _run_dry_run_summary(fake_bin)
    assert result.returncode == 0, result.stderr
    assert "WOULD close 2 open dispatch-drift issue(s): #101 #202" in result.stdout


def test_dry_run_summary_reports_zero_when_no_drift_issues_open(
    tmp_path: Path,
) -> None:
    fake_bin = _stub_gh(tmp_path, exit_code=0, stdout="")
    result = _run_dry_run_summary(fake_bin)
    assert result.returncode == 0, result.stderr
    assert NO_DRIFT_LINE in result.stdout


def test_dry_run_summary_never_closes_anything(tmp_path: Path) -> None:
    """The dry-run arm must stay read-only: `gh issue list`, never `close`.

    The query is un-scoped (every open `dispatch-drift` issue in the repo),
    so a `close` reachable from the dry-run path would sweep real issues on a
    run whose entire contract is "change nothing".
    """
    script = _dry_run_summary_script()
    assert "gh issue list" in script
    assert "gh issue close" not in script


def test_dry_run_summary_skips_drift_report_when_subject_did_not_match(
    tmp_path: Path,
) -> None:
    """The drift clause belongs inside the `match == true` arm, not after it."""
    fake_bin = _stub_gh(tmp_path, exit_code=0, stdout="101\n202\n")
    result = _run_dry_run_summary(fake_bin, match="false")
    assert result.returncode == 0, result.stderr
    assert "WOULD close" not in result.stdout
    assert NO_DRIFT_LINE not in result.stdout
    assert "no action would be taken" in result.stdout
