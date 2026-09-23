"""Guard tests for the CHANGELOG-freeze wiring in CI and pre-commit (#2304).

`.claude/scripts/check_changelog_frozen.py` needs every release tag present
locally, so the CI checkout must fetch tags and the CI step must pass
`--require-tags` (a missing tag fails there). The local pre-commit hook must
NOT pass it: an unfetched tag on a dev checkout warns and passes instead of
blocking the commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.conftest import _load_workflow

ROOT = Path(__file__).parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"

JOB_ID = "test"
FREEZE_STEP_NAME = "Freeze released CHANGELOG sections"
FREEZE_HOOK_ID = "changelog-frozen"
SCRIPT = ".claude/scripts/check_changelog_frozen.py"
REQUIRE_TAGS = "--require-tags"


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load_workflow(CI_WORKFLOW)["jobs"][JOB_ID]["steps"]
    return steps


def _checkout_step() -> dict[str, Any]:
    for step in _steps():
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            return step
    message = "ci.yml has no actions/checkout step"
    raise AssertionError(message)


def _freeze_hook() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == FREEZE_HOOK_ID:
                return dict(hook)
    message = f"no {FREEZE_HOOK_ID!r} hook in .pre-commit-config.yaml"
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
    hook = _freeze_hook()
    assert SCRIPT in hook["entry"]
    assert REQUIRE_TAGS not in hook["entry"]
    assert hook["pass_filenames"] is False
    assert hook["files"] == r"^CHANGELOG\.md$"
