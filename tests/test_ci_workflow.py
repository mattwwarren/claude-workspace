"""Guard tests for the CHANGELOG-freeze wiring in CI and pre-commit (#2304).

`.claude/scripts/check_changelog_frozen.py` needs every release tag present
locally, so the CI checkout must fetch tags and the CI step must pass
`--require-tags` (a missing tag fails there). The local pre-commit hook must
NOT pass it: an unfetched tag on a dev checkout warns and passes instead of
blocking the commit.

It also guards that every job in every `.github/workflows/*.yml` carries a
bounded `timeout-minutes` (#1937), and that `actionlint` is pinned as an
upstream pre-commit hook which CI's all-hooks pre-commit step runs (#1626).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tests.conftest import _load_workflow

ROOT = Path(__file__).parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"

JOB_ID = "test"
# The nightly ceiling, far below GitHub's 360-minute default.
MAX_JOB_TIMEOUT_MINUTES = 30
FREEZE_STEP_NAME = "Freeze released CHANGELOG sections"
FREEZE_HOOK_ID = "changelog-frozen"
SCRIPT = ".claude/scripts/check_changelog_frozen.py"
REQUIRE_TAGS = "--require-tags"
ACTIONLINT_HOOK_ID = "actionlint"
ACTIONLINT_REPO = "https://github.com/rhysd/actionlint"
PINNED_REV = re.compile(r"v\d+\.\d+\.\d+")
PRE_COMMIT_STEP_NAME = "Validate pre-commit hook config"


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load_workflow(CI_WORKFLOW)["jobs"][JOB_ID]["steps"]
    return steps


def _checkout_step() -> dict[str, Any]:
    for step in _steps():
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            return step
    message = "ci.yml has no actions/checkout step"
    raise AssertionError(message)


def _hook_repo(hook_id: str) -> dict[str, Any]:
    """The `.pre-commit-config.yaml` `repos:` entry declaring `hook_id`."""
    config: dict[str, Any] = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    for repo in config["repos"]:
        if any(hook.get("id") == hook_id for hook in repo.get("hooks", [])):
            return dict(repo)
    message = f"no {hook_id!r} hook in .pre-commit-config.yaml"
    raise AssertionError(message)


def _hook(hook_id: str) -> dict[str, Any]:
    for hook in _hook_repo(hook_id)["hooks"]:
        if hook.get("id") == hook_id:
            return dict(hook)
    message = f"no {hook_id!r} hook in .pre-commit-config.yaml"
    raise AssertionError(message)


def test_ci_workflow_fetches_tags_and_requires_flag() -> None:
    with_block = _checkout_step()["with"]
    assert with_block["fetch-depth"] == 0
    assert with_block["fetch-tags"] is True

    freeze = [step for step in _steps() if step.get("name") == FREEZE_STEP_NAME]
    assert len(freeze) == 1, f"expected one {FREEZE_STEP_NAME!r} step in ci.yml"
    run = freeze[0]["run"]
    assert SCRIPT in run
    assert REQUIRE_TAGS in run.split()


def test_pre_commit_hook_omits_require_tags() -> None:
    hook = _hook(FREEZE_HOOK_ID)
    assert SCRIPT in hook["entry"]
    assert REQUIRE_TAGS not in hook["entry"]
    assert hook["pass_filenames"] is False
    assert hook["files"] == r"^CHANGELOG\.md$"


def test_every_ci_job_has_a_bounded_timeout() -> None:
    workflow_files = sorted(
        [*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")]
    )
    assert workflow_files, f"no workflow files under {WORKFLOWS_DIR}"

    seen: set[tuple[str, str]] = set()
    violations: list[str] = []
    for path in workflow_files:
        jobs = _load_workflow(path)["jobs"]
        assert jobs, f"{path.name} defines no jobs"
        for job_id, job in jobs.items():
            seen.add((path.name, job_id))
            timeout = job.get("timeout-minutes")
            # YAML `true` is a bool, and bool is an int subclass, so exclude it.
            bounded = (
                isinstance(timeout, int)
                and not isinstance(timeout, bool)
                and 0 < timeout <= MAX_JOB_TIMEOUT_MINUTES
            )
            if not bounded:
                violations.append(f"{path.name}:{job_id} timeout-minutes={timeout!r}")

    required = {("ci.yml", "test"), ("ci.yml", "package-smoke")}
    assert required <= seen, f"required ci.yml jobs missing: {sorted(required - seen)}"
    assert not violations, (
        "jobs without an integer timeout-minutes in "
        f"1..{MAX_JOB_TIMEOUT_MINUTES}: {violations}"
    )


def test_pre_commit_declares_actionlint_from_pinned_upstream() -> None:
    """The official hook repo, pinned to a release tag, using the `golang`
    hook -- not `actionlint-system` (needs a preinstalled binary) or
    `actionlint-docker` (needs a daemon)."""
    repo = _hook_repo(ACTIONLINT_HOOK_ID)
    assert repo["repo"] == ACTIONLINT_REPO
    assert PINNED_REV.fullmatch(repo["rev"]), repo["rev"]
    assert [hook["id"] for hook in repo["hooks"]] == [ACTIONLINT_HOOK_ID]


def test_ci_pre_commit_step_runs_actionlint() -> None:
    """CI has no dedicated actionlint step: the all-hooks pre-commit step runs
    it. Pin that the step still runs every hook and never skips this one."""
    assert _hook(ACTIONLINT_HOOK_ID)
    steps = [step for step in _steps() if step.get("name") == PRE_COMMIT_STEP_NAME]
    assert len(steps) == 1, f"expected one {PRE_COMMIT_STEP_NAME!r} step in ci.yml"
    argv = steps[0]["run"].split()
    # `uv run pre-commit run ...`: the first `run` is uv's, so anchor on pre-commit.
    run_at = argv.index("pre-commit") + 1
    assert argv[run_at] == "run", argv
    assert "--all-files" in argv
    # A positional hook id after `run` would narrow the step to that one hook.
    assert all(arg.startswith("-") for arg in argv[run_at + 1 :]), argv
    skipped = str(steps[0].get("env", {}).get("SKIP", "")).split(",")
    assert ACTIONLINT_HOOK_ID not in [name.strip() for name in skipped]
