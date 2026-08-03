"""Guard tests for `.github/workflows/changelog-gate.yml` (#1612).

The gate is the *blocking* counterpart to `changelog-advisory.yml`: it fails
the job when a PR titled `feat(...)`/`fix(...)` touches `src/` without either
updating `CHANGELOG.md` or carrying the `no-changelog` opt-out label. The
advisory workflow stays advisory-only and is untouched by this ticket -- the
two coexist deliberately (the advisory nudges every `src/cw/**` PR; the gate
fails only the narrow user-visible-change slice).

Group A asserts YAML shape via `tests.conftest._load_workflow` / `_on_block`
(hoisted there rather than adding a third private copy of the pair). Group B
executes the real extracted `run:` scripts through
`subprocess.run(["/bin/bash", "-c", script])` with synthetic env and a stubbed
`gh` on `PATH` -- the shell logic is never reimplemented in Python, so a test
cannot pass by agreeing with itself.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tests.conftest import _load_workflow, _on_block

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "changelog-gate.yml"
ADVISORY_PATH = ROOT / ".github" / "workflows" / "changelog-advisory.yml"
README_PATH = ROOT / ".github" / "workflows" / "README.md"

JOB_ID = "gate-changelog"
ADVISORY_JOB_ID = "check-changelog"
LIST_STEP_ID = "list-files"
LABEL_STEP_ID = "ensure-label"
GATE_STEP_ID = "gate"

NO_CHANGELOG_LABEL = "no-changelog"
FAILURE_MESSAGE = "add an [Unreleased] entry or apply the no-changelog label"
REQUIRED_TRIGGER_TYPES = ("opened", "synchronize", "reopened", "labeled", "unlabeled")

# `labeled`/`unlabeled` are load-bearing, not decoration: without `labeled` an
# author who applies the opt-out label gets no re-run and the red check never
# clears (the opt-out becomes unusable without a push); without `unlabeled` a
# revoked label leaves a stale *passing* check on a PR that no longer qualifies
# for the waiver.
TRIGGER_TYPES_RATIONALE = (
    "dropping `labeled` makes the no-changelog opt-out unclearable without a "
    "new push; dropping `unlabeled` lets a revoked label leave a stale passing "
    "check"
)


def _steps(path: Path, job_id: str) -> list[dict[str, Any]]:
    workflow = _load_workflow(path)
    steps: list[dict[str, Any]] = workflow["jobs"][job_id]["steps"]
    return steps


def _step(step_id: str) -> dict[str, Any]:
    step: dict[str, Any] = next(
        s for s in _steps(WORKFLOW_PATH, JOB_ID) if s.get("id") == step_id
    )
    return step


def _script(step_id: str) -> str:
    script: str = _step(step_id)["run"]
    return script


def _run_gate_step(
    *,
    title: str,
    changed_files: str,
    labels: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the gate step's script with a synthetic PR context.

    `PR_LABELS` is shaped exactly as `toJSON(...labels.*.name)` renders it in
    a real run: a pretty-printed JSON array (`[]` when there are no labels).
    """
    env = {
        "PR_TITLE": title,
        "PR_LABELS": json.dumps(list(labels), indent=2),
        "CHANGED_FILES": changed_files,
        # jq lives in /usr/bin on the runner image and locally; the gate step
        # parses PR_LABELS with it rather than substring-matching a blob.
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/bash", "-c", _script(GATE_STEP_ID)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _stub_gh(tmp_path: Path, *, exit_code: int, stdout: str = "") -> Path:
    """Write an executable `gh` stub into a fresh bin dir and return that dir."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    # Quoted heredoc ('GH_STDOUT_EOF') -- no shell interpolation of `stdout`'s
    # contents, matching how a real `gh` payload is opaque data.
    fake_gh.write_text(
        f"#!/bin/sh\ncat <<'GH_STDOUT_EOF'\n{stdout}GH_STDOUT_EOF\nexit {exit_code}\n"
    )
    fake_gh.chmod(0o755)
    return fake_bin


def _run_list_step(
    tmp_path: Path, *, gh_exit_code: int, gh_stdout: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the list-files step's script against a stubbed `gh` binary."""
    fake_bin = _stub_gh(tmp_path, exit_code=gh_exit_code, stdout=gh_stdout)
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    env = {
        "REPO": "example-org/example-repo",
        "PR_NUMBER": "1",
        "GITHUB_OUTPUT": str(output_file),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", _script(LIST_STEP_ID)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, output_file


def _run_label_step(
    tmp_path: Path, *, gh_exit_code: int
) -> subprocess.CompletedProcess[str]:
    """Run the ensure-label step's script against a stubbed `gh` binary."""
    fake_bin = _stub_gh(tmp_path, exit_code=gh_exit_code)
    env = {
        "REPO": "example-org/example-repo",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/bash", "-c", _script(LABEL_STEP_ID)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


# --- Group A: YAML-level structural assertions ---


def test_trigger_declares_every_required_pull_request_type() -> None:
    types = _on_block(_load_workflow(WORKFLOW_PATH))["pull_request"]["types"]
    missing = [t for t in REQUIRED_TRIGGER_TYPES if t not in types]
    assert not missing, f"missing trigger types {missing}: {TRIGGER_TYPES_RATIONALE}"


def test_list_files_step_is_copied_verbatim_from_the_advisory() -> None:
    """The two workflows must list PR files identically (drift guard).

    The advisory's list-files step is the hardened one: randomized heredoc
    delimiter, `--paginate`, fail-open on a `gh api` hiccup. Copying it means a
    fix landing on one workflow is a one-line port to the other, and any
    accidental divergence (a "simplified" gh call, a dropped fail-open) fails
    here instead of silently weakening the *blocking* check.
    """
    advisory_step = next(
        s for s in _steps(ADVISORY_PATH, ADVISORY_JOB_ID) if s.get("id") == LIST_STEP_ID
    )
    assert _step(LIST_STEP_ID) == advisory_step


def test_list_files_uses_paginate_not_pr_view() -> None:
    """Static guard against the silently-truncating `gh pr view` form.

    `gh pr view --json files` caps at 100 changed files with no truncation
    signal; on a blocking check that cap would silently waive the gate for any
    large PR whose only `src/` touch sorts past entry 100.
    """
    script = _script(LIST_STEP_ID)
    assert "--paginate" in script
    assert "gh pr view" not in script


def test_ensure_label_step_self_heals_and_fails_open() -> None:
    """The `no-changelog` label is created on demand, and never fails the job."""
    script = _script(LABEL_STEP_ID)
    assert "gh label create" in script
    assert NO_CHANGELOG_LABEL in script
    assert "|| true" in script


def test_label_name_is_identical_between_creation_and_gate_waiver() -> None:
    """Drift guard: the label name is a hardcoded literal in two places (#1612 review).

    `ensure-label` creates it; the `gate` step's jq comparison waives on it.
    Nothing else ties the two together, so a rename of one without the other
    would silently break the opt-out on this blocking check.
    """
    created = re.search(r'gh label create "([^"]+)"', _script(LABEL_STEP_ID))
    waived = re.search(r"--arg want '([^']+)'", _script(GATE_STEP_ID))
    assert created is not None, "could not find the label name in the create step"
    assert waived is not None, "could not find the label name in the gate's jq check"
    assert created.group(1) == waived.group(1) == NO_CHANGELOG_LABEL


def test_gate_step_fails_with_the_exact_operator_message() -> None:
    script = _script(GATE_STEP_ID)
    assert "exit 1" in script
    assert FAILURE_MESSAGE in script


def test_permissions_are_least_privilege() -> None:
    """`issues: write` is required for label self-heal; nothing more is granted."""
    job = _load_workflow(WORKFLOW_PATH)["jobs"][JOB_ID]
    assert job["permissions"] == {"pull-requests": "read", "issues": "write"}


def test_readme_documents_new_workflow() -> None:
    rows = [
        line
        for line in README_PATH.read_text().splitlines()
        if line.startswith("|") and "`changelog-gate.yml`" in line
    ]
    assert len(rows) == 1, rows


# --- Group B: literal shell exercise ---


def test_list_step_fails_open_on_gh_api_error(tmp_path: Path) -> None:
    """A `gh api` hiccup must not fail the job -- even on the blocking check.

    A red gate here would read as "you forgot a CHANGELOG entry" when the real
    cause is a 5xx or a rate limit, which is exactly the failure mode that gets
    a required check disabled.
    """
    result, output_file = _run_list_step(tmp_path, gh_exit_code=1)
    assert result.returncode == 0, result.stderr

    lines = output_file.read_text().splitlines()
    assert lines[0].startswith("files<<")
    delim = lines[0].removeprefix("files<<")
    assert lines[1] == delim, "expected an empty multiline value between delimiters"


def test_list_step_captures_full_paginated_output(tmp_path: Path) -> None:
    """All of a multi-page `gh api --paginate` response reaches `$GITHUB_OUTPUT`."""
    filenames = [f"docs/filler_{index:03d}.md" for index in range(104)]
    filenames.insert(102, "src/cw/late.py")
    gh_stdout = "\n".join(filenames) + "\n"

    result, output_file = _run_list_step(tmp_path, gh_exit_code=0, gh_stdout=gh_stdout)
    assert result.returncode == 0, result.stderr

    lines = output_file.read_text().splitlines()
    assert lines[0].startswith("files<<")
    delim = lines[0].removeprefix("files<<")
    assert lines[-1] == delim
    assert lines[1:-1] == filenames


def test_gates_the_literal_1566_case() -> None:
    """#1566: `fix(...)` PR touching `src/`, no CHANGELOG, no label -> red."""
    result = _run_gate_step(
        title="fix(#1566): unify salvage/dispatch terminal sentinel classification",
        changed_files="src/cw/auto_dev_result/schema.py\ntests/test_reconcile.py",
    )
    assert result.returncode == 1, result.stdout
    assert FAILURE_MESSAGE in result.stdout


def test_passes_when_changelog_is_touched() -> None:
    result = _run_gate_step(
        title="fix(#1566): unify salvage/dispatch terminal sentinel classification",
        changed_files="src/cw/auto_dev_result/schema.py\nCHANGELOG.md",
    )
    assert result.returncode == 0, result.stdout
    assert FAILURE_MESSAGE not in result.stdout


def test_passes_the_literal_1597_case_via_the_opt_out_label() -> None:
    """#1597: a `feat(...)` PR waived by the `no-changelog` label -> green."""
    result = _run_gate_step(
        title="feat(#1597): guard breadcrumb-eligible paused statuses",
        changed_files="src/cw/reconcile/idle.py",
        labels=[NO_CHANGELOG_LABEL],
    )
    assert result.returncode == 0, result.stdout


def test_opt_out_label_is_matched_exactly_among_several_labels() -> None:
    result = _run_gate_step(
        title="feat(#1597): guard breadcrumb-eligible paused statuses",
        changed_files="src/cw/reconcile/idle.py",
        labels=["enhancement", NO_CHANGELOG_LABEL, "auto-dev"],
    )
    assert result.returncode == 0, result.stdout


def test_opt_out_label_match_is_not_a_substring_match() -> None:
    """A label merely *containing* `no-changelog` must not waive the gate."""
    result = _run_gate_step(
        title="feat(#1597): guard breadcrumb-eligible paused statuses",
        changed_files="src/cw/reconcile/idle.py",
        labels=["no-changelog-needed", "changelog"],
    )
    assert result.returncode == 1, result.stdout
    assert FAILURE_MESSAGE in result.stdout


def test_passes_the_literal_1567_refactor_case() -> None:
    """#1567: `refactor(...)` is an accepted false negative, per the ticket.

    The gate fires only on `feat`/`fix` titles. A refactor that quietly changes
    user-visible behavior slips through; widening the rule to every
    conventional-commit type would red-flag the `chore`/`docs`/`test` majority
    that genuinely has nothing to document.
    """
    result = _run_gate_step(
        title="refactor(#1567): move _marker_version into lifecycle.py",
        changed_files="src/cw/dev_queue/lifecycle.py",
    )
    assert result.returncode == 0, result.stdout


def test_chore_release_pr_passes() -> None:
    result = _run_gate_step(
        title="chore(release): v1.27.0",
        changed_files="src/cw/__init__.py\npyproject.toml",
    )
    assert result.returncode == 0, result.stdout


def test_feat_pr_that_touches_no_src_passes() -> None:
    result = _run_gate_step(
        title="feat(docs): document the dispatch runbook",
        changed_files="docs/dispatch-runbook.md\ntests/test_docs.py",
    )
    assert result.returncode == 0, result.stdout


def test_unscoped_conventional_title_passes() -> None:
    """`feat: ...` with no `(scope)` is outside the gated shape."""
    result = _run_gate_step(
        title="feat: add a thing",
        changed_files="src/cw/cli.py",
    )
    assert result.returncode == 0, result.stdout


def test_title_and_path_matches_are_anchored() -> None:
    """Neither match may float: title must start with the type, path with `src/`."""
    unanchored_title = _run_gate_step(
        title="wip: feat(cli) rework, not ready",
        changed_files="src/cw/cli.py",
    )
    assert unanchored_title.returncode == 0, unanchored_title.stdout

    unanchored_path = _run_gate_step(
        title="feat(cli): rework",
        changed_files="docs/src/cw/example.py\nvendor/src/thing.py",
    )
    assert unanchored_path.returncode == 0, unanchored_path.stdout


def test_empty_changed_files_does_not_crash_under_set_e() -> None:
    """A `grep` that matches nothing must not trip `set -e` (guarded by `if`)."""
    result = _run_gate_step(title="feat(cli): rework", changed_files="")
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr


def test_label_step_survives_a_failing_gh_label_create(tmp_path: Path) -> None:
    """Fork PRs get a read-only `GITHUB_TOKEN`; label creation fails there.

    The self-heal must degrade to a no-op rather than failing the *blocking*
    job for every fork contributor.
    """
    result = _run_label_step(tmp_path, gh_exit_code=1)
    assert result.returncode == 0, result.stderr
