"""Guard tests for `.claude/commands/ship-it.md`'s Step 3 title tier ladder (#1531).

A release branch's `chore(release):` version-bump commit was losing the PR
title to a sibling `docs(...)` fixup commit, because the ladder's first two
tiers deliberately exclude `chore` and the `test|docs` tier therefore won.
This file pins the new `chore(release):`-wins-outright tier ahead of the rest
of the ladder, and pins the pre-existing tiers against regression.

Group A follows `test_pr_events_workflow.py`'s `read_text()` +
index-ordering convention for prose/shape assertions. Group B deliberately
goes beyond the five-file prose-only precedent for `.claude/commands/*.md`
(`test_auto_dev_finalize_early_push.py` et al.) and *executes* the extracted
bash fence against a real temp git repo -- a prose assertion can confirm the
regex text changed but cannot falsify which commit the ladder actually picks
out of real branch history, which is the entire bug.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import _clean_git_env

ROOT = Path(__file__).parent.parent
SHIP_IT_PATH = ROOT / ".claude" / "commands" / "ship-it.md"
FENCE = "```bash"
RELEASE_TIER_PATTERN = r"^chore\(release\):"


def _ship_it_text() -> str:
    return SHIP_IT_PATH.read_text(encoding="utf-8")


def _title_tier_script() -> str:
    """Extract the Step 3 title-derivation bash fence verbatim."""
    content = _ship_it_text()
    start = content.index(FENCE, content.index("## Step 3"))
    end = content.index("```", start + len(FENCE))
    return content[start + len(FENCE) : end]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    )


def _run_title_tiers(
    make_git_repo: Callable[..., Path],
    commits: list[str],
) -> str:
    """Run the extracted tier ladder over a synthetic branch history.

    Built on the ``make_git_repo`` fixture (which already supplies a git
    identity and a base commit) plus the self-remote shape used by
    ``tests/test_reconcile_local.py``'s ``_local_git_worktree``: adding the
    repo itself as ``origin`` and fetching pins ``refs/remotes/origin/main``
    at the base commit, so every commit in ``commits`` lands in the
    ``origin/main..HEAD`` range the ladder scans.
    """
    repo = make_git_repo("title-tiers")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main")
    for subject in commits:
        _git(repo, "commit", "--allow-empty", "-m", subject)

    script = _title_tier_script() + '\nprintf "TITLE=%s\\n" "$TITLE"\n'
    env = {**_clean_git_env(), "EXPLICIT_TITLE": "", "ARGUMENTS": ""}
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    titles = [
        line.removeprefix("TITLE=")
        for line in result.stdout.splitlines()
        if line.startswith("TITLE=")
    ]
    assert len(titles) == 1, result.stdout
    return titles[0]


# --- Group A: prose/shape assertions ---


def test_new_tier_precedes_renumbered_tier3() -> None:
    content = _ship_it_text()
    indices = [content.index(f"Tier {n}:") for n in range(1, 7)]
    assert indices == sorted(indices)


def test_new_tier_matches_chore_release_prefix_only() -> None:
    # Guards against a future edit loosening this back to bare `chore`,
    # which would let ordinary housekeeping commits win the title again.
    assert RELEASE_TIER_PATTERN in _title_tier_script()


# --- Group B: runtime execution against real branch history ---


def test_chore_release_wins_over_docs_sibling(
    make_git_repo: Callable[..., Path],
) -> None:
    """The exact repro from #1530: docs fixup must not out-rank the bump."""
    title = _run_title_tiers(
        make_git_repo,
        [
            "chore(release): bump version to 1.24.1",
            "docs(release): correct changelog claim",
        ],
    )
    assert title == "chore(release): bump version to 1.24.1"


def test_chore_release_as_sole_commit(make_git_repo: Callable[..., Path]) -> None:
    title = _run_title_tiers(make_git_repo, ["chore(release): bump version to 1.24.1"])
    assert title == "chore(release): bump version to 1.24.1"


def test_chore_release_wins_regardless_of_commit_order(
    make_git_repo: Callable[..., Path],
) -> None:
    """Whole-branch scan, not a "first commit" heuristic."""
    title = _run_title_tiers(
        make_git_repo,
        [
            "docs(release): correct changelog claim",
            "chore(release): bump version to 1.24.1",
        ],
    )
    assert title == "chore(release): bump version to 1.24.1"


@pytest.mark.parametrize(
    "prefix",
    ["feat", "fix", "refactor", "build", "perf", "ci"],
)
def test_tier2_prefixes_still_win_no_regression(
    make_git_repo: Callable[..., Path],
    prefix: str,
) -> None:
    """The new tier must not fire on ordinary (non-release) `chore:` commits."""
    title = _run_title_tiers(
        make_git_repo,
        ["chore: bump lockfile", f"{prefix}: do the thing"],
    )
    assert title == f"{prefix}: do the thing"


def test_docs_only_branch_still_falls_to_tier4(
    make_git_repo: Callable[..., Path],
) -> None:
    title = _run_title_tiers(make_git_repo, ["docs: fix typo"])
    assert title == "docs: fix typo"
