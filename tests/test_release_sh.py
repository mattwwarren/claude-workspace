"""Guard tests for `scripts/release.sh`'s commit-subject pre-flight (#1707).

`scripts/release.sh` is the manual release-tagging path; `.github/workflows/
release-tag.yml` is the automated one. Both were, until this change, silently
independent — `release.sh` had zero awareness that a commit-subject contract
(`chore(release): vX.Y.Z` / `chore(release): bump version to X.Y.Z`, both
optionally suffixed with a squash-merge `(#<PR number>)`) exists at all, which
let an author run `release.sh` successfully on a commit whose subject would
then make `release-tag.yml` fail (see #1707's incident: v1.27.0/v1.28.0 used
`chore(release): cut vX.Y.Z`, matching neither accepted form).

This suite exercises the new guard `release.sh` runs *before* any of its
existing checks (pyproject.toml version, installed package version, quality
gates). Running those existing checks for real would be slow and recursive,
so every test here uses a temp repo with **no `pyproject.toml` at all** — a
rejected subject must exit before the script ever looks for one, and an
accepted (or skipped) subject can be proven to "pass the guard" by asserting
the script's *next* observable failure is the pyproject-missing error (a
`sed: can't read pyproject.toml` failure under `set -e`), not the guard's own
rejection message.

Structure mirrors `tests/test_release_tag_workflow.py`: reuses `make_git_repo`
and `_clean_git_env` from `tests/conftest.py`, and the subprocess-driven
shell-exercise convention from that file's Group B.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from tests.conftest import _clean_git_env
from tests.test_release_tag_workflow import GUARD_STEP_ID, _script

ROOT = Path(__file__).parent.parent
RELEASE_SH = ROOT / "scripts" / "release.sh"

# Distinctive substring emitted only by the guard's rejection branch — used to
# assert the guard did (or did not) fire, independent of the later,
# unrelated pyproject-missing failure every "guard let it through" test hits.
GUARD_REJECTION_MARKER = "matches neither accepted release-commit form"
ACCEPTED_FORM_V = "chore(release): vX.Y.Z"
ACCEPTED_FORM_BUMP = "chore(release): bump version to X.Y.Z"
WORKFLOW_POINTER = "release-tag.yml"
PYPROJECT_MISSING_MARKER = "pyproject.toml"


def _run_release_sh(
    repo: Path, version: str, *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**_clean_git_env(), **(extra_env or {})}
    return subprocess.run(
        ["/bin/bash", str(RELEASE_SH), version],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _commit_head_subject(repo: Path, subject: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", subject],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    )


def _tags(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list"],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    return [line for line in result.stdout.splitlines() if line]


def test_guard_rejects_chore_release_subject_matching_neither_form(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "chore(release): cut v1.28.0")

    result = _run_release_sh(repo, "1.28.0")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert ACCEPTED_FORM_V in output
    assert ACCEPTED_FORM_BUMP in output
    assert WORKFLOW_POINTER in output
    assert _tags(repo) == []


def test_guard_accepts_bump_version_to_subject(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "chore(release): bump version to 1.28.0")

    result = _run_release_sh(repo, "1.28.0")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert GUARD_REJECTION_MARKER not in output
    assert PYPROJECT_MISSING_MARKER in output


def test_guard_accepts_v_prefixed_subject(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "chore(release): v1.28.0")

    result = _run_release_sh(repo, "1.28.0")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert GUARD_REJECTION_MARKER not in output
    assert PYPROJECT_MISSING_MARKER in output


def test_guard_accepts_squash_merge_pr_suffix(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "chore(release): bump version to 1.28.0 (#1706)")

    result = _run_release_sh(repo, "1.28.0")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert GUARD_REJECTION_MARKER not in output
    assert PYPROJECT_MISSING_MARKER in output


def test_guard_override_env_var_skips_check(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "chore(release): cut v1.28.0")

    result = _run_release_sh(
        repo, "1.28.0", extra_env={"RELEASE_SH_SKIP_SUBJECT_GUARD": "1"}
    )

    output = result.stdout + result.stderr
    assert GUARD_REJECTION_MARKER not in output
    assert PYPROJECT_MISSING_MARKER in output
    assert _tags(repo) == []


def test_guard_non_release_subject_passes_through_unchecked(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("workspace")
    _commit_head_subject(repo, "fix: unrelated change")

    result = _run_release_sh(repo, "1.28.0")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert GUARD_REJECTION_MARKER not in output
    assert PYPROJECT_MISSING_MARKER in output


def _pattern_from_workflow() -> str:
    script = _script(GUARD_STEP_ID)
    match = re.search(r'=~ (.+?) \]\]; then', script)
    assert match, f"could not find guard regex in {GUARD_STEP_ID!r} step"
    return match.group(1)


def _pattern_from_release_sh() -> str:
    text = RELEASE_SH.read_text(encoding="utf-8")
    match = re.search(r"SUBJECT_PATTERN='([^']*)'", text)
    assert match, "could not find SUBJECT_PATTERN in scripts/release.sh"
    return match.group(1)


def test_release_sh_guard_regex_matches_release_tag_workflow() -> None:
    """Anti-drift pin: the two copies of the accepted-pattern regex must never diverge."""
    assert _pattern_from_release_sh() == _pattern_from_workflow()
