"""Guard tests for the CHANGELOG-freeze wiring in CI and pre-commit (#2304).

`.claude/scripts/check_changelog_frozen.py` needs every release tag present
locally, so the CI checkout must fetch tags and the CI step must pass
`--require-tags` (a missing tag fails there). The local pre-commit hook must
NOT pass it: an unfetched tag on a dev checkout warns and passes instead of
blocking the commit.

It also guards that every job in every `.github/workflows/*.yml` carries a
bounded `timeout-minutes` (#1937), and that `actionlint` is pinned as an
upstream pre-commit hook which CI's all-hooks pre-commit step runs (#1626).

Finally it pins the coverage wiring (#2249): the unit run measures both
`src/cw` and `.claude/scripts`, each tree has its own floor (never one blended
figure), and CI, the pre-push hook and CLAUDE.md gate 10 agree on both.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from cw.codex_review._context import _load_claude_md_quality_gates
from tests.conftest import _bash_fences, _clean_git_env, _load_workflow

if TYPE_CHECKING:
    from collections.abc import Callable

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
PYTEST_HOOK_ID = "pytest"
DIFF_COVER_HOOK_ID = "diff-cover"
# `uv run --extra mcp pytest ...`: the tokens right after `uv run`.
MCP_PYTEST_PREFIX = ["--extra", "mcp", "pytest"]
UNIT_STEP_NAME = "Test (unit) with coverage"
FLOORS_STEP_NAME = "Coverage floors (per source tree)"
PATCH_STEP_NAME = "Patch coverage (diff-cover vs origin/main)"
CLAUDE_MD = ROOT / "CLAUDE.md"
# `--cov=` targets every coverage-measuring pytest run must carry (#2249).
COV_TARGETS = {"cw", ".claude/scripts"}
# Per-tree `coverage report --include=<tree> --fail-under=<floor>` floors.
EXPECTED_FLOORS = {"src/cw/*": 88, ".claude/scripts/*": 50}
DIFF_COVER_COMMAND = (
    "uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90"
)
COV_FLAG = "--cov="
INCLUDE_FLAG = "--include="
FAIL_UNDER_FLAG = "--fail-under="
COVERAGE_REPORT_PREFIX = ["uv", "run", "coverage", "report"]
FLOOR_PAIR = re.compile(r"--include='([^']+)'\s+--fail-under=(\d+)")
FENCE_COV_TARGET = re.compile(r"--cov=(\S+)")
# The CLAUDE.md `## Development` block's coverage command (outside Quality Gates).
DEV_BLOCK_COVERAGE_PREFIX = "uv run pytest tests/ --cov="


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


def _uv_run_pytest_argvs(entry: str) -> list[list[str]]:
    """Every `uv run ... pytest ...` invocation inside a hook `entry`.

    A hook entry is either a bare command or a `bash -c '<a && b && c>'`
    script; the latter is split on `&&` so each chained command is checked.
    """
    argv = shlex.split(entry)
    if argv[:2] == ["bash", "-c"]:
        commands = [shlex.split(part) for part in argv[2].split("&&")]
    else:
        commands = [argv]
    return [
        command
        for command in commands
        if command[:2] == ["uv", "run"] and "pytest" in command
    ]


def test_pre_commit_pytest_hook_syncs_mcp_extra() -> None:
    """The pytest hook mirrors CLAUDE.md gate 10 (`uv run --extra mcp pytest`).

    Bare `uv run pytest` does not upgrade an already-installed but stale
    `mcp` extra, so the hook could fail on code CI passes (#2242).
    """
    invocations = _uv_run_pytest_argvs(_hook(PYTEST_HOOK_ID)["entry"])
    assert invocations, "pytest hook has no `uv run ... pytest` invocation"
    for argv in invocations:
        assert argv[2:5] == MCP_PYTEST_PREFIX, argv


def test_pre_commit_diff_cover_hook_syncs_mcp_extra() -> None:
    """The pre-push diff-cover hook runs pytest too, so it needs the same
    `--extra mcp` as the pytest hook and CLAUDE.md gate 10 (#2242)."""
    hook = _hook(DIFF_COVER_HOOK_ID)
    assert "pre-push" in hook["stages"]
    invocations = _uv_run_pytest_argvs(hook["entry"])
    assert invocations, "diff-cover hook has no `uv run ... pytest` invocation"
    for argv in invocations:
        assert argv[2:5] == MCP_PYTEST_PREFIX, argv


# ---------------------------------------------------------------------------
# #2249: both source trees measured, one floor per tree
# ---------------------------------------------------------------------------


def _step(name: str) -> dict[str, Any]:
    """The single ci.yml `test` job step named *name*."""
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r} step in ci.yml, got {matches}"
    return matches[0]


def _cov_targets(argv: list[str]) -> set[str]:
    return {arg.removeprefix(COV_FLAG) for arg in argv if arg.startswith(COV_FLAG)}


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [arg.removeprefix(flag) for arg in argv if arg.startswith(flag)]


def _ci_floors() -> dict[str, int]:
    """`{tree glob: floor}` parsed from the floors step's `coverage report` lines."""
    floors: dict[str, int] = {}
    for line in _step(FLOORS_STEP_NAME)["run"].splitlines():
        if not line.strip():
            continue
        argv = shlex.split(line)
        assert argv[:4] == COVERAGE_REPORT_PREFIX, argv
        include = _flag_values(argv, INCLUDE_FLAG)
        floor = _flag_values(argv, FAIL_UNDER_FLAG)
        assert len(include) == len(floor) == 1, argv
        floors[include[0]] = int(floor[0])
    return floors


def _claude_md_gate_fence() -> str:
    """The single bash fence of CLAUDE.md's `## Quality Gates` section."""
    section = _load_claude_md_quality_gates(ROOT)
    assert section is not None, "CLAUDE.md has no `## Quality Gates` section"
    fences = _bash_fences(section)
    assert len(fences) == 1, f"expected one bash fence in Quality Gates, got {fences}"
    return fences[0]


def test_unit_step_measures_both_trees_without_blended_floor() -> None:
    argv = shlex.split(_step(UNIT_STEP_NAME)["run"])
    assert _cov_targets(argv) == COV_TARGETS, argv
    assert "--cov-report=xml" in argv
    assert not [arg for arg in argv if arg.startswith("--cov-fail-under")], (
        "a blended --cov-fail-under hides a src/cw regression behind the scripts "
        "tree; per-tree floors live in the floors step (#2249)"
    )
    marker_at = argv.index("-m") + 1
    assert argv[marker_at] == "not integration", argv


def test_ci_has_per_tree_coverage_floors_step() -> None:
    names = [step.get("name") for step in _steps()]
    assert names.count(FLOORS_STEP_NAME) == 1, names
    floors_at = names.index(FLOORS_STEP_NAME)
    assert floors_at == names.index(UNIT_STEP_NAME) + 1, (
        "the floors step must read `.coverage` right after the unit step"
    )
    assert floors_at < names.index(PATCH_STEP_NAME), names
    # No `if:`: the floors run on both OS legs, as --cov-fail-under=88 did.
    assert "if" not in _step(FLOORS_STEP_NAME)
    assert _ci_floors() == EXPECTED_FLOORS


def test_ci_diff_cover_invocation_unchanged() -> None:
    assert DIFF_COVER_COMMAND in _step(PATCH_STEP_NAME)["run"]


def test_cov_targets_and_floors_agree_across_ci_hook_and_claude_md() -> None:
    ci_targets = _cov_targets(shlex.split(_step(UNIT_STEP_NAME)["run"]))
    hook_invocations = _uv_run_pytest_argvs(_hook(DIFF_COVER_HOOK_ID)["entry"])
    assert len(hook_invocations) == 1, hook_invocations
    hook_targets = _cov_targets(hook_invocations[0])
    # Scoped to the fence: the prose around it must not feed either regex.
    fence = _claude_md_gate_fence()
    fence_targets = set(FENCE_COV_TARGET.findall(fence))
    assert ci_targets == hook_targets == fence_targets == COV_TARGETS, (
        ci_targets,
        hook_targets,
        fence_targets,
    )

    fence_floors = {tree: int(floor) for tree, floor in FLOOR_PAIR.findall(fence)}
    assert fence_floors == _ci_floors() == EXPECTED_FLOORS

    # The `## Development` block sits outside `## Quality Gates`.
    dev_lines = [
        line.split(" #", 1)[0].strip()
        for line in CLAUDE_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith(DEV_BLOCK_COVERAGE_PREFIX)
    ]
    assert len(dev_lines) == 1, dev_lines
    assert _cov_targets(dev_lines[0].split()) == COV_TARGETS, dev_lines[0]


def test_pre_push_diff_cover_hook_measures_scripts_tree() -> None:
    entry = _hook(DIFF_COVER_HOOK_ID)["entry"]
    invocations = _uv_run_pytest_argvs(entry)
    assert len(invocations) == 1, invocations
    argv = invocations[0]
    assert _cov_targets(argv) == COV_TARGETS, argv
    assert "--cov-report=xml" in argv
    script = shlex.split(entry)[2]
    assert script.split("&&")[-1].strip() == DIFF_COVER_COMMAND, script


PROBE_RELPATH = ".claude/scripts/probe_script.py"
PROBE_SOURCE = "x = 1\ny = 2\n"
PROBE_DIFF = f"""\
diff --git a/{PROBE_RELPATH} b/{PROBE_RELPATH}
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/{PROBE_RELPATH}
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""
DIFF_COVER_FAIL_EXIT = 1


def _two_source_cobertura(repo: Path, hits: int) -> str:
    """Cobertura XML shaped as `--cov=cw --cov=.claude/scripts` writes it.

    coverage.py emits one `<source>` per measured root and gives a script's
    class a `filename` relative to its own root: just `probe_script.py`.
    """
    lines = "".join(f'<line number="{n}" hits="{hits}"/>' for n in (1, 2))
    return (
        '<?xml version="1.0" ?>\n'
        '<coverage version="7" line-rate="0" branch-rate="0">'
        f"<sources><source>{repo}</source>"
        f"<source>{repo / '.claude' / 'scripts'}</source></sources>"
        '<packages><package name="." line-rate="0" branch-rate="0"><classes>'
        '<class name="probe_script.py" filename="probe_script.py" '
        f'line-rate="0" branch-rate="0"><methods/><lines>{lines}</lines></class>'
        "</classes></package></packages></coverage>\n"
    )


@pytest.mark.parametrize(("hits", "expected_exit"), [(1, 0), (0, DIFF_COVER_FAIL_EXIT)])
def test_diff_cover_resolves_scripts_tree_path_from_two_source_xml(
    make_git_repo: Callable[..., Path], hits: int, expected_exit: int
) -> None:
    """diff-cover maps a script-root-relative class onto the diff's repo path.

    Without that mapping gate 12 reports "No lines with coverage information"
    and passes by vacuum on `.claude/scripts` diffs (#2249).
    """
    # git reports a symlink-resolved toplevel; the XML sources must match it.
    repo = make_git_repo("repo").resolve()
    probe = repo / PROBE_RELPATH
    probe.parent.mkdir(parents=True)
    probe.write_text(PROBE_SOURCE)
    (repo / "cov.xml").write_text(_two_source_cobertura(repo, hits))
    (repo / "change.diff").write_text(PROBE_DIFF)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "diff_cover.diff_cover_tool",
            "cov.xml",
            "--diff-file",
            "change.diff",
            "--fail-under=90",
        ],
        cwd=repo,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == expected_exit, output
    # Listed by path (covered or not), so the class resolved: not a vacuous pass.
    assert PROBE_RELPATH in result.stdout, output
    assert "No lines with coverage information" not in output, output
