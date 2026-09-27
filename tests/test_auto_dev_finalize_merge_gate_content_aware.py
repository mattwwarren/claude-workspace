"""Guard tests: content-aware Stage 4a merge gate (#2431).

Pins ``.claude/commands/auto-dev-finalize.md`` Step 4a's headless block, which
replaced a pure file-intersection park with two layers:

1. ``check_merge_gate_overlap.py filter`` drops the client's
   ``merge_gate_ignore_paths`` (read from ``.claude/cw-context.json``, schema
   v11) from the branch/PR intersection.
2. Any overlap that survives is escalated to ``git merge-tree --write-tree``;
   only a genuine textual conflict (or a tooling failure — fail closed) blocks.

Every open pipeline PR is evaluated (no early exit), and the gate blocks once,
after the loop, naming every conflicting PR.

Two kinds of test live here. The prose/structure assertions follow
``test_auto_dev_finalize_semantic_resolve.py``. The executable ones run the
doc's own fence — unmodified apart from ``<placeholder>`` substitution —
against a real git repo with a bare ``origin`` carrying ``refs/pull/<n>/head``
refs, the real filter script, and a stub ``gh`` that serves canned PR lists.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from cw.dispatch.routing.pr_refs import _extract_blocked_on_pr
from tests.conftest import (
    GUARD_FENCE_INVOKED,
    GUARD_MARKER_BAD_CASES,
    GUARD_MARKER_GOOD_CASES,
    _bash_fences,
    _clean_git_env,
    _cmd,
    _placement,
    commit_tracked_file,
    git_in,
    guard_candidate_path,
    run_guard_fence,
    substitute_fence_placeholders,
)

_SCRIPT = "check_merge_gate_overlap.py"
_SCRIPT_SOURCE = Path(__file__).resolve().parents[1] / ".claude" / "scripts" / _SCRIPT

_STEP4A = "### Step 4a: Merge Gate Check"
_STEP4B = "### Step 4b: Pipeline-Level PR Approval"
_HEADLESS = "**Headless:** (Small only"

_BLOCKED_PREFIX = "MERGE_GATE_BLOCKED: "
_CLEAR_MARKER = "MERGE_GATE_CLEAR"
_ABSENT_LOG = "check_merge_gate_overlap: script absent, skipped"

_PLACEHOLDERS = {"fork_point_sha": "<fork>", "branch-prefix": "dev"}

_PR_REF = re.compile(r"PR #(\d+)")


# ---------------------------------------------------------------------------
# Doc slicing
# ---------------------------------------------------------------------------


def _step4a() -> str:
    content = _cmd("auto-dev-finalize.md")
    start = content.index(_STEP4A)
    return content[start : content.index(_STEP4B, start)]


def _headless_block() -> str:
    section = _step4a()
    return section[section.index(_HEADLESS) :]


def _gate_fence() -> str:
    fences = [
        fence
        for fence in _bash_fences(_headless_block())
        if f"/.claude/scripts/{_SCRIPT}" in fence and "for candidate in" in fence
    ]
    assert len(fences) == 1, f"expected exactly one gate fence, got {len(fences)}"
    return fences[0]


def _loop_body(fence: str) -> tuple[int, int]:
    start = fence.index("while read -r PR_NUMBER HEAD_REF")
    end = fence.index("\ndone", start)
    return start, end


# ---------------------------------------------------------------------------
# Prose / structure
# ---------------------------------------------------------------------------


def test_headless_block_resolves_overlap_script_repo_local_then_global() -> None:
    fence = _gate_fence()
    repo_idx = fence.index(f'"$GUARD_ROOT/.claude/scripts/{_SCRIPT}"')
    global_idx = fence.index(f'"$HOME/.claude/scripts/{_SCRIPT}"')
    assert repo_idx < global_idx
    assert "MIN_VERSION=1" in fence
    assert "cw-script-version" in fence
    assert 'uv run python "$RESOLVED" filter' in fence


def test_fence_reads_ignore_paths_from_cw_context_as_repeated_flags() -> None:
    fence = _gate_fence()
    assert "jq -r '.merge_gate_ignore_paths // [] | .[]'" in fence
    assert '"$GUARD_ROOT/.claude/cw-context.json"' in fence
    assert "IGNORE_ARGS+=(--ignore-path " in fence
    assert '"${IGNORE_ARGS[@]}"' in fence


def test_ignore_paths_never_read_from_client_config_inside_agent_bash() -> None:
    """R1/R2: the worker's only path to the ClientConfig field is cw-context.json."""
    block = _headless_block()
    assert "cw config" not in block
    assert "python -c" not in block
    assert "clients.yaml" not in _gate_fence()


def test_stale_marker_hard_stop_precedes_every_pr_evaluation() -> None:
    fence = _gate_fence()
    loop_start, _ = _loop_body(fence)
    assert fence.index("exit 3") < fence.index("gh pr list") < loop_start


def test_loop_evaluates_every_pr_and_blocks_once_after_it() -> None:
    fence = _gate_fence()
    loop_start, loop_end = _loop_body(fence)
    body = fence[loop_start:loop_end]
    assert "break" not in body
    assert re.search(r"\bexit\b", body) is None
    assert "MERGE_GATE_CONFLICTS+=(" in body
    assert "MERGE_GATE_BLOCKED" not in body
    assert fence.index(_BLOCKED_PREFIX.strip(), loop_end) > loop_end
    assert fence.index(_CLEAR_MARKER, loop_end) > loop_end


def test_blocking_verdict_escalates_to_merge_tree_probe() -> None:
    fence = _gate_fence()
    loop_start, loop_end = _loop_body(fence)
    body = fence[loop_start:loop_end]
    assert 'git fetch --quiet origin "pull/$PR_NUMBER/head"' in body
    assert "git merge-tree --write-tree FETCH_HEAD HEAD" in body
    assert "tooling error" in body


def test_every_conflict_clause_carries_exactly_one_pr_reference() -> None:
    """pr_refs.py's regex needs ``PR #<n>`` once per conflicting PR."""
    fence = _gate_fence()
    clauses = re.findall(r'MERGE_GATE_CONFLICTS\+=\("([^"]*)"\)', fence)
    assert len(clauses) >= 4, clauses
    for clause in clauses:
        assert clause.startswith("PR #$PR_NUMBER ($HEAD_REF)"), clause
        assert clause.count("PR #") == 1, clause


def test_sentinel_template_keeps_reason_stage_and_pr_substring() -> None:
    block = _headless_block()
    assert '"stage": "stage4a_merge_gate"' in block
    assert '"reason": "prior_pipeline_pr_open"' in block
    assert "PR #<number> (<headRefName>)" in block
    assert "EXIT `merge_gate_blocked`" in block


def test_stale_marker_bullet_is_a_headless_block_not_a_merge_gate_park() -> None:
    lines = [
        line
        for line in _headless_block().splitlines()
        if "HEADLESS BLOCK" in line and _SCRIPT in line
    ]
    assert len(lines) == 1
    assert 'blocker.reason: "agent_block"' in lines[0]
    assert "need >= 1" in lines[0]
    assert "prior_pipeline_pr_open" not in lines[0]


def test_absent_script_bullet_falls_back_to_raw_intersection() -> None:
    bullets = [
        line
        for line in _headless_block().splitlines()
        if "Absent from both locations" in line
    ]
    assert len(bullets) == 1
    assert _ABSENT_LOG in bullets[0]
    assert "friction_highlights" in bullets[0]
    assert "raw" in bullets[0]


def test_generated_file_reapply_guidance_scoped_to_ignored_paths() -> None:
    """R5: documentation only — ignored paths are unverified, so regenerate."""
    block = _headless_block()
    assert "has **not** verified" in block
    assert "merge_gate_ignore_paths" in block
    assert "regenerate" in block


def test_version_table_row_references_step_4a() -> None:
    impl = _cmd("auto-dev-impl.md")
    assert f"| `{_SCRIPT}` | 1 | `auto-dev-finalize.md` Step 4a |" in impl


# ---------------------------------------------------------------------------
# Marker gate, via the shared runner
# ---------------------------------------------------------------------------


def _plant_gh_stub(tmp_path: Path) -> None:
    """A ``gh`` answering with one open pipeline PR, on the runner's PATH dir.

    ``run_guard_fence`` builds ``<tmp_path>/bin`` with ``exist_ok=True`` and
    puts it first on PATH, so a stub planted there first is picked up too.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        '#!/bin/sh\ncase "$1 $2" in\n'
        '  "pr list") echo "42 dev/42" ;;\n'
        '  "pr diff") echo "src/a.py" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_marker_fence(
    tmp_path: Path, **placement: str | None
) -> subprocess.CompletedProcess[str]:
    _plant_gh_stub(tmp_path)
    return run_guard_fence(
        tmp_path,
        _gate_fence() + '\necho "${MERGE_GATE_OUTPUT-}"\n',
        _SCRIPT,
        create_base_commit=True,
        placeholders={"fork_point_sha": "HEAD", "branch-prefix": "dev"},
        **placement,
    )


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_GOOD_CASES)
def test_gate_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    result = _run_marker_fence(tmp_path, **_placement(location, script_body))
    assert result.returncode == 0, f"{location}/{label}: {result.stderr}"
    assert GUARD_FENCE_INVOKED in result.stdout
    assert guard_candidate_path(tmp_path, location, _SCRIPT) in result.stdout
    assert "STALE:" not in result.stdout


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_BAD_CASES)
def test_gate_fence_hard_stops_without_invoking(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    result = _run_marker_fence(tmp_path, **_placement(location, script_body))
    assert result.returncode == 3, f"{location}/{label}: {result.stdout}"
    assert GUARD_FENCE_INVOKED not in result.stdout
    assert "STALE:" in result.stdout
    assert _CLEAR_MARKER not in result.stdout


def test_gate_fence_absent_from_both_locations_is_non_blocking(
    tmp_path: Path,
) -> None:
    result = _run_marker_fence(tmp_path)
    assert result.returncode == 0, result.stderr
    assert GUARD_FENCE_INVOKED not in result.stdout
    assert "STALE:" not in result.stdout
    assert _ABSENT_LOG in result.stdout


# ---------------------------------------------------------------------------
# End-to-end: real git, real script, stub gh
# ---------------------------------------------------------------------------

_SHARED = "pyproject.toml"
_BASE_LINES = [
    "[project]",
    'name = "demo"',
    *[f"# filler {n}" for n in range(12)],
    "[tool.mypy]",
    "strict = true",
]
_TOP = 1
_BOTTOM = len(_BASE_LINES) - 1


def _shared_with(index: int, value: str) -> str:
    lines = list(_BASE_LINES)
    lines[index] = value
    return "\n".join(lines) + "\n"


class _GateRepo:
    """A repo on ``dev/100`` with a bare origin serving ``refs/pull/<n>/head``."""

    def __init__(self, tmp_path: Path, make_git_repo: Callable[[str], Path]) -> None:
        self.tmp_path = tmp_path
        self.repo = make_git_repo("repo")
        commit_tracked_file(self.repo, _SHARED, _shared_with(_TOP, _BASE_LINES[_TOP]))
        commit_tracked_file(self.repo, "uv.lock", "base\n")
        self.fork_point = git_in(self.repo, "rev-parse", "HEAD")
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(origin)],
            check=True,
            capture_output=True,
            env=_clean_git_env(),
        )
        git_in(self.repo, "remote", "add", "origin", str(origin))
        git_in(self.repo, "push", "-q", "origin", "main")
        self.open_prs: list[str] = []
        self.pr_files: dict[int, list[str]] = {}

    def add_pr(
        self,
        number: int,
        files: dict[str, str],
        *,
        head_ref: str | None = None,
        push: bool = True,
    ) -> None:
        ref = head_ref or f"dev/{number}"
        git_in(self.repo, "checkout", "-q", "-b", ref, "main")
        for path, content in files.items():
            commit_tracked_file(self.repo, path, content)
        if push:
            git_in(self.repo, "push", "-q", "origin", f"{ref}:refs/pull/{number}/head")
        git_in(self.repo, "checkout", "-q", "main")
        self.open_prs.append(f"{number} {ref}")
        self.pr_files[number] = sorted(files)

    def checkout_ours(self, files: dict[str, str]) -> None:
        git_in(self.repo, "checkout", "-q", "-b", "dev/100", "main")
        for path, content in files.items():
            commit_tracked_file(self.repo, path, content)

    def run(
        self,
        *,
        plant_script: bool = True,
        ignore_paths: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        claude = self.repo / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / "cw-context.json").write_text(
            json.dumps(
                {
                    "worktree_path": str(self.repo),
                    "merge_gate_ignore_paths": ignore_paths or [],
                }
            ),
            encoding="utf-8",
        )
        if plant_script:
            scripts = claude / "scripts"
            scripts.mkdir(exist_ok=True)
            (scripts / _SCRIPT).write_text(
                _SCRIPT_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
            )
        stub_dir = self.tmp_path / "gh-data"
        stub_dir.mkdir(exist_ok=True)
        (stub_dir / "open-prs").write_text(
            "".join(f"{line}\n" for line in self.open_prs), encoding="utf-8"
        )
        for number, files in self.pr_files.items():
            (stub_dir / f"pr-{number}-files").write_text(
                "".join(f"{f}\n" for f in files), encoding="utf-8"
            )
        bin_dir = self._stub_bin()
        home = self.tmp_path / "home"
        home.mkdir(exist_ok=True)
        fence = substitute_fence_placeholders(
            _gate_fence(), {**_PLACEHOLDERS, "fork_point_sha": self.fork_point}
        )
        env = {
            **_clean_git_env(),
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "GH_STUB_DIR": str(stub_dir),
        }
        return subprocess.run(
            ["bash", "-c", fence],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _stub_bin(self) -> Path:
        bin_dir = self.tmp_path / "stub-bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            '#!/bin/sh\ncase "$1 $2" in\n'
            '  "pr list") cat "$GH_STUB_DIR/open-prs" ;;\n'
            '  "pr diff") cat "$GH_STUB_DIR/pr-$3-files" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        # `uv run python <script> ...` -> the test interpreter, so the fence's
        # own invocation line runs the real stdlib-only script.
        uv = bin_dir / "uv"
        uv.write_text(
            '#!/bin/sh\n[ "$1" = run ] && shift\n[ "$1" = python ] && shift\n'
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        uv.chmod(0o755)
        return bin_dir


def _details(result: subprocess.CompletedProcess[str]) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith(_BLOCKED_PREFIX):
            return line[len(_BLOCKED_PREFIX) :]
    return None


@pytest.fixture
def gate(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> _GateRepo:
    return _GateRepo(tmp_path, make_git_repo)


def test_e2e_file_disjoint_prs_clear_without_probe(gate: _GateRepo) -> None:
    gate.add_pr(101, {"src/other.py": "y = 2\n"}, push=False)
    gate.checkout_ours({"src/mine.py": "x = 1\n"})
    result = gate.run()
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout
    assert _details(result) is None


def test_e2e_same_file_disjoint_hunks_is_non_blocking(gate: _GateRepo) -> None:
    """Shape 2 from the ticket: shared file, textually mergeable."""
    gate.add_pr(101, {_SHARED: _shared_with(_BOTTOM, "strict = false")})
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "renamed"')})
    result = gate.run()
    assert result.returncode == 0, result.stderr
    assert "merge-tree clean despite file overlap with PR #101" in result.stdout
    assert _CLEAR_MARKER in result.stdout
    assert _details(result) is None


def test_e2e_ignored_overlap_clears_without_fetching(gate: _GateRepo) -> None:
    """Shape 1: an ignore-listed file never reaches the probe.

    The PR's ref is never pushed, so a probe attempt would fail its fetch and
    fail closed — a clear verdict proves the probe was skipped.
    """
    gate.add_pr(101, {"uv.lock": "theirs\n"}, push=False)
    gate.checkout_ours({"uv.lock": "ours\n"})
    result = gate.run(ignore_paths=["uv.lock"])
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout


def test_e2e_same_overlap_blocks_without_ignore_list(gate: _GateRepo) -> None:
    gate.add_pr(101, {"uv.lock": "theirs\n"})
    gate.checkout_ours({"uv.lock": "ours\n"})
    details = _details(gate.run())
    assert details is not None
    assert details.startswith("PR #101 (dev/101)")
    assert "uv.lock" in details


def test_e2e_one_clean_one_conflicting_names_only_the_conflict(
    gate: _GateRepo,
) -> None:
    gate.add_pr(101, {_SHARED: _shared_with(_BOTTOM, "strict = false")})
    gate.add_pr(102, {_SHARED: _shared_with(_TOP, 'name = "theirs"')})
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "ours"')})
    result = gate.run()
    details = _details(result)
    assert details is not None, result.stdout
    assert _PR_REF.findall(details) == ["102"]
    assert _extract_blocked_on_pr(details) == 102
    assert _CLEAR_MARKER not in result.stdout


def test_e2e_two_conflicting_prs_are_both_named(gate: _GateRepo) -> None:
    gate.add_pr(101, {_SHARED: _shared_with(_TOP, 'name = "one"')})
    gate.add_pr(102, {_SHARED: _shared_with(_TOP, 'name = "two"')})
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "ours"')})
    details = _details(gate.run())
    assert details is not None
    assert _PR_REF.findall(details) == ["101", "102"]


def test_e2e_probe_tooling_failure_fails_closed_and_loop_continues(
    gate: _GateRepo,
) -> None:
    """A failed fetch for one PR blocks, and the next PR is still evaluated."""
    gate.add_pr(101, {_SHARED: _shared_with(_TOP, 'name = "one"')}, push=False)
    gate.add_pr(102, {_SHARED: _shared_with(_TOP, 'name = "two"')})
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "ours"')})
    details = _details(gate.run())
    assert details is not None
    assert _PR_REF.findall(details) == ["101", "102"]
    first, second = details.split("; PR #")
    assert "tooling error" in first
    assert "tooling error" not in second


def test_e2e_filter_usage_error_fails_closed(gate: _GateRepo) -> None:
    """No file list for the PR (gh and fallback both empty) -> exit 2 -> block."""
    gate.add_pr(101, {"src/other.py": "y = 2\n"}, push=False)
    gate.pr_files[101] = []
    gate.checkout_ours({"src/mine.py": "x = 1\n"})
    details = _details(gate.run())
    assert details is not None
    assert details.startswith("PR #101 (dev/101)")
    assert "exited 2" in details
    assert "tooling error" in details


def test_e2e_absent_script_uses_raw_intersection(gate: _GateRepo) -> None:
    """No script: the pre-#2431 park, even for a textually mergeable overlap."""
    gate.add_pr(101, {_SHARED: _shared_with(_BOTTOM, "strict = false")})
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "renamed"')})
    result = gate.run(plant_script=False, ignore_paths=[_SHARED])
    assert _ABSENT_LOG in result.stdout
    details = _details(result)
    assert details is not None
    assert (
        details
        == f"PR #101 (dev/101) is open and shares files with this branch: {_SHARED}"
    )


def test_e2e_skips_non_pipeline_and_current_branch_prs(gate: _GateRepo) -> None:
    gate.add_pr(101, {_SHARED: _shared_with(_TOP, 'name = "x"')}, head_ref="feature/x")
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "ours"')})
    gate.open_prs.append("100 dev/100")
    gate.pr_files[100] = [_SHARED]
    result = gate.run()
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout
