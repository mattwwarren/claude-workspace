"""Guard tests for `.github/workflows/release.yml` (#1841).

`release.yml` fires only on `push: tags: ['v*']`, and the tags the automated
release path pushes are authored by `release-tag.yml`'s own `GITHUB_TOKEN` —
which GitHub deliberately does not use to start new workflow runs. Its
`verify` job (ruff / ruff-format / mypy / pytest) was therefore stranded: it
gated only the hand-pushed-tag path, and no release cut by the automated path
ever ran it. Those gates now live in `release-tag.yml`'s `tag-release` job
(see `test_release_tag_workflow.py`'s Group E) and `verify` is deleted here.

YAML shape only — nothing left in this workflow has `run:` logic worth
exercising through a subprocess. Parsing goes through
`tests.conftest._load_workflow` rather than a local `yaml.safe_load` wrapper,
per the convention `test_changelog_gate_workflow.py` established.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import _load_workflow

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_JOB = "release"
DELETED_VERIFY_JOB = "verify"
CREATE_RELEASE_STEP_NAME = "Create GitHub Release"
CLOSE_DRIFT_STEP_NAME = "Close dispatch-drift issues"


def _workflow() -> dict[Any, Any]:
    return _load_workflow(WORKFLOW_PATH)


def _release_job() -> dict[str, Any]:
    job: dict[str, Any] = _workflow()["jobs"][RELEASE_JOB]
    return job


def _step_names() -> list[str]:
    return [step["name"] for step in _release_job()["steps"] if "name" in step]


def test_verify_job_is_deleted() -> None:
    """Its gates moved to `release-tag.yml`; a resurrected copy here would be
    a second, divergent definition of the same contract."""
    assert DELETED_VERIFY_JOB not in _workflow()["jobs"]


def test_release_job_no_longer_depends_on_verify() -> None:
    """A `needs:` pointing at a job that no longer exists is a hard workflow
    parse error on GitHub's side — the release would never start."""
    assert DELETED_VERIFY_JOB not in _release_job().get("needs", [])


def test_release_job_runs_on_ubuntu_latest() -> None:
    assert _release_job()["runs-on"] == "ubuntu-latest"


def test_release_job_retains_its_release_and_drift_closer_steps() -> None:
    """Guards against the `verify` deletion taking neighbouring steps with it.

    `release.yml` is still the closer for the manual-tag path (#1799), so its
    drift-closing step has to survive this ticket intact.
    """
    assert CREATE_RELEASE_STEP_NAME in _step_names()
    assert CLOSE_DRIFT_STEP_NAME in _step_names()


def test_release_job_keeps_the_permissions_its_surviving_steps_need() -> None:
    assert _release_job()["permissions"] == {
        "contents": "write",
        "issues": "write",
    }
