"""Tests for cw.codex_impl._gates: guards, gates 1-2, quality gates, mappers (#3061).

Real ``git`` repos through ``tests/_git_sandbox`` and stubs under the per-test
HOME: no real ``gh``, ``codex`` or repo gate runs.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.auto_dev_result.schema._vocab import (
    EMPTY_DIFF_BLOCKER_REASON,
    PLAN_SCOPE_DRIFT_BLOCKER_REASON,
)
from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    DirtyStart,
    capture_cycle_baseline,
)
from cw.codex_fix_loop.commit import _git_commit
from cw.codex_fix_loop.fence import FenceBreach, staged_set_breach
from cw.codex_fix_loop.gates import GateScope, plan_cycle_gates
from cw.codex_fix_loop.hook_failure import as_hook_failure
from cw.codex_fix_loop.push import push_and_verify_head
from cw.codex_impl._gates import (
    MIN_SCOPE_SCRIPT_VERSION,
    check_empty_diff,
    check_existing_branch_work,
    check_on_branch,
    check_plan_present,
    commit_failure_block,
    dirty_start_block,
    fence_block,
    run_quality_gates,
    run_scope_conformance,
    staged_totals,
)
from cw.codex_impl._result import IMPL_FAILED, PLAN_MISSING, ImplBlock
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_HOOK_FAILED,
    CODEX_FIX_REVERTED_BRANCH,
    CODEX_FIX_SCOPE_DRIFT,
)
from cw.local_runner import git_facts
from cw.models import TicketTask
from cw.models.review_feedback import ReviewFeedback
from tests._codex_review_helpers import (
    _install_pre_commit_hook,
    fresh_impl_repo,
    install_home_prep_pr_state,
    install_home_scope_script,
)
from tests._shared.git_repos import git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_BRANCH = "dev/T-1"
_PLAN = "## Files Modified\n\n- src/a.py\n"
_SCOPE = GateScope(ticket_id="T-1", client="test", session_id="s-1", cycle=0)
_VERDICT = {
    "triggered": True,
    "extra_files": ["z/one.py", "z/two.py"],
    "allowed_extra": 1,
    "plan_file_count": 1,
    "delivered_file_count": 3,
    "approved_extra_files": [],
}
_REVIEW_WORDING = ("requeue REVIEW", "cw review settle", "fix cycle")


def _feedback() -> ReviewFeedback:
    return ReviewFeedback(
        repo="o/r",
        pr_number=1,
        reviewed_sha="a" * 40,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )


def _task(**updates: Any) -> TicketTask:
    return TicketTask(ticket_id="T-1", client="test", **updates)


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    worktree, _origin = fresh_impl_repo(make_git_repo, "gates-wt", _BRANCH)
    return worktree


def _stage(repo: Path, relative: str = "src/a.py", text: str = "x = 1\n") -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git_in(repo, "add", "-A")


def _scope(
    repo: Path,
    tmp_path: Path,
    *,
    task: TicketTask | None = None,
    plan_text: str | None = _PLAN,
    touched: tuple[str, ...] = ("src/a.py",),
) -> tuple[ImplBlock | None, list[str]]:
    return run_scope_conformance(
        repo,
        task=task or _task(),
        plan_text=plan_text,
        plan_path=repo / ".cw" / "plan.md",
        touched=touched,
        scratch=tmp_path / "scratch",
    )


class TestPlanPresent:
    def test_plan_missing_blocks_a_non_repair_row(self) -> None:
        block = check_plan_present(None, _task())
        assert block is not None
        assert block.reason == PLAN_MISSING

    def test_plan_present_passes(self) -> None:
        assert check_plan_present("plan", _task()) is None

    def test_adopted_repair_carve_out_allows_no_plan(self) -> None:
        task = _task(branch=_BRANCH, review_feedback=_feedback())
        assert check_plan_present(None, task) is None

    def test_recover_red_pr_row_without_branch_still_blocks(self) -> None:
        block = check_plan_present(None, _task(review_feedback=_feedback()))
        assert block is not None
        assert block.reason == PLAN_MISSING


class TestPreTurnGuards:
    def test_existing_branch_work_on_a_fresh_row_blocks(self, repo: Path) -> None:
        _stage(repo)
        git_in(repo, "commit", "-m", "prior work")
        block = check_existing_branch_work(git_facts(repo, "main"), _task())
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert block.details.startswith("existing_branch_work")

    def test_existing_branch_work_not_checked_on_repair_rows(self, repo: Path) -> None:
        _stage(repo)
        git_in(repo, "commit", "-m", "prior work")
        task = _task(review_feedback=_feedback())
        assert check_existing_branch_work(git_facts(repo, "main"), task) is None

    def test_fresh_row_with_no_commits_passes(self, repo: Path) -> None:
        assert check_existing_branch_work(git_facts(repo, "main"), _task()) is None

    def test_unresolvable_fork_blocks_a_fresh_row(self, repo: Path) -> None:
        facts = git_facts(repo, "main")
        facts["fork_point"] = ""
        block = check_existing_branch_work(facts, _task())
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "fork" in block.details

    def test_on_the_expected_branch_passes(self, repo: Path) -> None:
        assert check_on_branch(repo, _BRANCH) is None

    def test_detached_head_blocks_before_the_turn(self, repo: Path) -> None:
        git_in(repo, "checkout", "--detach")
        block = check_on_branch(repo, _BRANCH)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "detached" in block.details

    def test_branch_mismatch_blocks(self, repo: Path) -> None:
        block = check_on_branch(repo, "dev/other")
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert _BRANCH in block.details
        assert "dev/other" in block.details

    def test_push_detached_head_fails_closed_via_synthetic_marker(
        self, repo: Path
    ) -> None:
        git_in(repo, "checkout", "--detach")
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            push_and_verify_head(repo, git_in(repo, "rev-parse", "HEAD"))
        assert "SYNTHETIC (detached HEAD" in str(excinfo.value.stderr)


class TestGate1:
    def test_empty_net_diff_is_empty_diff_blocked(self, repo: Path) -> None:
        base = git_facts(repo, "main")["fork_point"]
        block = check_empty_diff(repo, base)
        assert block is not None
        assert block.status == "empty_diff_blocked"
        assert block.reason == EMPTY_DIFF_BLOCKER_REASON

    def test_passes_on_nonempty_staged_diff_vs_fork(self, repo: Path) -> None:
        base = git_facts(repo, "main")["fork_point"]
        _stage(repo)
        assert check_empty_diff(repo, base) is None

    def test_staged_totals_count_files_and_lines(self, repo: Path) -> None:
        base = git_facts(repo, "main")["fork_point"]
        _stage(repo, "src/a.py", "a\nb\n")
        _stage(repo, "src/b.py", "c\n")
        assert staged_totals(repo, base) == (2, 3)


class TestGate2:
    def test_exit0_empty_extra_files_passes(self, repo: Path, tmp_path: Path) -> None:
        install_home_scope_script(
            0, json.dumps({**_VERDICT, "triggered": False, "extra_files": []}), ""
        )
        assert _scope(repo, tmp_path) == (None, [])

    def test_exit1_valid_verdict_parks_plan_scope_drift(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(1, json.dumps(_VERDICT), "")
        block, friction = _scope(repo, tmp_path)
        assert block is not None
        assert block.reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON
        assert "z/one.py" in block.details
        assert "z/two.py" in block.details
        assert friction == []

    def test_exit1_without_valid_json_is_impl_failed_tooling_failure(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(1, "not json", "traceback")
        block, _ = _scope(repo, tmp_path)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "traceback" in block.details

    def test_exit1_json_without_triggered_key_is_a_tooling_failure(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(1, json.dumps({"extra_files": []}), "")
        block, _ = _scope(repo, tmp_path)
        assert block is not None
        assert block.reason == IMPL_FAILED

    def test_exit0_with_extra_files_within_allowance_adds_growth_friction(
        self, repo: Path, tmp_path: Path
    ) -> None:
        verdict = {**_VERDICT, "triggered": False, "extra_files": ["z/one.py"]}
        install_home_scope_script(0, json.dumps(verdict), "")
        block, friction = _scope(repo, tmp_path)
        assert block is None
        assert friction == ["impl_scope_growth: z/one.py"]

    def test_exit0_with_unparseable_stdout_passes(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(0, "garbage", "")
        assert _scope(repo, tmp_path) == (None, [])

    def test_exit2_is_friction_unparsed_not_a_block(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(2, "", "no files-modified section")
        block, friction = _scope(repo, tmp_path)
        assert block is None
        assert friction == [
            "impl_scope_conformance_unparsed: no files-modified section"
        ]

    def test_exit_vocabulary_is_exhaustive(self, repo: Path, tmp_path: Path) -> None:
        install_home_scope_script(7, "", "weird")
        block, _ = _scope(repo, tmp_path)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "7" in block.details

    @pytest.mark.parametrize(
        "marker",
        [
            None,
            "# cw-script-version: 1.5",
            "# cw-script-version: abc",
            "# cw-script-version: 1234567",
            "# cw-script-version: 2",
            "# cw-script-version: 3 trailing",
        ],
    )
    def test_stale_or_missing_marker_blocks_impl_failed_headless_block(
        self, repo: Path, tmp_path: Path, marker: str | None
    ) -> None:
        script = install_home_scope_script(0, "{}", "")
        lines = script.read_text(encoding="utf-8").splitlines()
        lines[1] = marker if marker is not None else "# no marker"
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        block, _ = _scope(repo, tmp_path)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "HEADLESS BLOCK" in block.details
        assert str(MIN_SCOPE_SCRIPT_VERSION) in block.details

    def test_marker_on_line_six_is_missing(self, repo: Path, tmp_path: Path) -> None:
        script = install_home_scope_script(0, "{}", "")
        text = "#!/usr/bin/env python3\n" + "#\n" * 5 + "# cw-script-version: 3\n"
        script.write_text(text + "import sys\n", encoding="utf-8")
        block, _ = _scope(repo, tmp_path)
        assert block is not None
        assert "HEADLESS BLOCK" in block.details

    def test_script_absent_is_friction_and_skipped(
        self, repo: Path, tmp_path: Path
    ) -> None:
        block, friction = _scope(repo, tmp_path)
        assert block is None
        assert friction == ["check_plan_scope_conformance: script absent, skipped"]

    def test_repair_row_without_plan_skips_with_friction(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(1, json.dumps(_VERDICT), "")
        task = _task(branch=_BRANCH, review_feedback=_feedback())
        block, friction = _scope(repo, tmp_path, task=task, plan_text=None)
        assert block is None
        assert friction == ["impl_scope_conformance_skipped_repair_entry"]

    def test_repair_row_with_plan_still_runs(self, repo: Path, tmp_path: Path) -> None:
        install_home_scope_script(1, json.dumps(_VERDICT), "")
        task = _task(branch=_BRANCH, review_feedback=_feedback())
        block, _ = _scope(repo, tmp_path, task=task)
        assert block is not None
        assert block.reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON

    def test_repo_local_script_wins_over_the_home_copy(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(1, json.dumps(_VERDICT), "")
        local = repo / ".claude" / "scripts" / "check_plan_scope_conformance.py"
        local.parent.mkdir(parents=True)
        local.write_text(
            "# cw-script-version: 3\nimport sys\nsys.exit(0)\n", encoding="utf-8"
        )
        assert _scope(repo, tmp_path) == (None, [])

    def test_runs_with_sys_executable_and_scratch_files_outside_the_worktree(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "decoy.git"))
        log = tmp_path / "argv.json"
        script = Path.home() / ".claude" / "scripts" / "check_plan_scope_conformance.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "# cw-script-version: 3\n"
            "import json, os, sys\n"
            "seen = {'argv': sys.argv, 'exe': sys.executable, 'cwd': os.getcwd(),\n"
            "        'git_dir': 'GIT_DIR' in os.environ}\n"
            f"json.dump(seen, open({str(log)!r}, 'w'))\n"
            "print(json.dumps({'triggered': False, 'extra_files': []}))\n",
            encoding="utf-8",
        )
        assert _scope(repo, tmp_path, touched=("b.py", "a.py"))[0] is None
        seen = json.loads(log.read_text(encoding="utf-8"))
        argv = seen["argv"]
        assert seen["exe"]
        assert seen["cwd"] == str(repo.resolve())
        assert seen["git_dir"] is False
        touched_file = Path(argv[argv.index("--touched-files") + 1])
        assert touched_file.read_text(encoding="utf-8").splitlines() == ["a.py", "b.py"]
        assert repo not in touched_file.parents
        assert argv[argv.index("--plan") + 1] == str(repo / ".cw" / "plan.md")
        assert "--approved-extra-files" not in argv

    def test_approved_extra_files_bound_by_ancestry(
        self, repo: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "argv.json"
        script = Path.home() / ".claude" / "scripts" / "check_plan_scope_conformance.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "# cw-script-version: 3\n"
            "import json, sys\n"
            f"json.dump(sys.argv, open({str(log)!r}, 'w'))\n"
            "print(json.dumps({'triggered': False, 'extra_files': []}))\n",
            encoding="utf-8",
        )
        head = git_in(repo, "rev-parse", "HEAD")
        task = _task(
            scope_drift_approved_extra_files=["z/ok.py"],
            scope_drift_approved_head=head,
        )
        block, friction = _scope(repo, tmp_path, task=task)
        assert (block, friction) == (None, [])
        argv = json.loads(log.read_text(encoding="utf-8"))
        approved = Path(argv[argv.index("--approved-extra-files") + 1])
        assert approved.read_text(encoding="utf-8").splitlines() == ["z/ok.py"]

    @pytest.mark.parametrize("head", ["not-a-sha", "0" * 40, "A" * 40, ""])
    def test_non_ancestor_or_malformed_approved_head_is_dropped_with_friction(
        self, repo: Path, tmp_path: Path, head: str
    ) -> None:
        install_home_scope_script(0, json.dumps({"extra_files": []}), "")
        task = _task(
            scope_drift_approved_extra_files=["z/ok.py"],
            scope_drift_approved_head=head,
        )
        block, friction = _scope(repo, tmp_path, task=task)
        assert block is None
        assert len(friction) == 1
        assert friction[0].startswith("scope_drift_approval_stale: ")

    def test_approval_without_a_head_is_dropped_with_friction(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_scope_script(0, "{}", "")
        task = _task(scope_drift_approved_extra_files=["z/ok.py"])
        _, friction = _scope(repo, tmp_path, task=task)
        assert friction[0].startswith("scope_drift_approval_stale: ")


class TestQualityGates:
    def _run(
        self, repo: Path, tokens: list[str], *, stage: bool = True
    ) -> ImplBlock | None:
        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)
        plan = plan_cycle_gates(repo, _SCOPE)
        if stage:
            _stage(repo)
        return run_quality_gates(
            plan,
            worktree=repo,
            baseline=baseline,
            scope=_SCOPE,
            tokens=tokens,
            touched=frozenset({"src/a.py"}),
        )

    def test_red_parks_impl_failed_with_verbatim_gate_output(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_prep_pr_state(
            tmp_path / "state", ("lint", "echo boom-output-xyz; exit 1", 5.0)
        )
        block = self._run(repo, [])
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "`lint`" in block.details
        assert "exit code 1" in block.details
        assert "boom-output-xyz" in block.details
        assert not any(word in block.details for word in _REVIEW_WORDING)

    def test_dirtied_parks_impl_failed(self, repo: Path, tmp_path: Path) -> None:
        install_home_prep_pr_state(
            tmp_path / "state", ("fmt", "echo changed > src/a.py", 5.0)
        )
        block = self._run(repo, [])
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "changed files" in block.details

    def test_unavailable_script_missing_parks_impl_failed(self, repo: Path) -> None:
        block = self._run(repo, [])
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert "could not be run" in block.details

    def test_zero_gates_adds_none_detected_friction(
        self, repo: Path, tmp_path: Path
    ) -> None:
        install_home_prep_pr_state(tmp_path / "state")
        tokens: list[str] = []
        assert self._run(repo, tokens) is None
        assert tokens == ["codex_fix_gates_none_detected"]

    def test_green_gate_passes(self, repo: Path, tmp_path: Path) -> None:
        install_home_prep_pr_state(tmp_path / "state", ("lint", "true", 5.0))
        assert self._run(repo, []) is None

    @pytest.mark.parametrize("command", ["exit 1", "echo x > src/a.py"])
    def test_never_parks_codex_fix_gate_failed(
        self, repo: Path, tmp_path: Path, command: str
    ) -> None:
        install_home_prep_pr_state(tmp_path / "state", ("g", command, 5.0))
        block = self._run(repo, [])
        assert block is not None
        assert block.reason != "codex_fix_gate_failed"


def _breach(reason: str, *paths: str, restorable: bool = True) -> FenceBreach:
    return FenceBreach(
        reason,
        paths,
        "codex fix cycle 0 detail; requeue REVIEW or cw review settle",
        "requeue REVIEW, `cw review settle <payload>`",
        restorable=restorable,
    )


class TestFenceBlock:
    @pytest.mark.parametrize(
        ("breach", "needle"),
        [
            (_breach(CODEX_FIX_SCOPE_DRIFT, "src/x.py"), "Files Modified"),
            (_breach(CODEX_FIX_REVERTED_BRANCH, "src/x.py"), "default-branch"),
            (
                FenceBreach(
                    CODEX_FIX_GROWTH_BUDGET,
                    ("src/x.py",),
                    "added code\n- src/x.py:3 new function: def f()\n- net +99",
                    "hint: requeue REVIEW",
                ),
                "net +99",
            ),
            (
                _breach(CODEX_FIX_SCOPE_DRIFT, "src/x.py", restorable=False),
                "staged set",
            ),
            (_breach("something_new", "src/x.py"), "something_new"),
        ],
    )
    def test_maps_reason_to_impl_text_and_never_surfaces_review_hints(
        self, breach: FenceBreach, needle: str
    ) -> None:
        block = fence_block(breach)
        assert block.reason == breach.reason
        assert needle in block.details
        assert "src/x.py" in block.details
        assert not any(word in block.details for word in _REVIEW_WORDING)

    def test_real_staged_set_breach_never_leaks_review_text(self) -> None:
        breach = staged_set_breach(
            frozenset({"a.py"}), frozenset({"b.py"}), 0, start_head="a" * 40
        )
        block = fence_block(breach)
        assert "a.py" in block.details
        assert "b.py" in block.details
        assert not any(word in block.details for word in _REVIEW_WORDING)

    def test_dirty_start_text_lists_markers_unmerged_and_dirty_paths(
        self, repo: Path
    ) -> None:
        (repo / "stray.txt").write_text("x", encoding="utf-8")
        start = capture_cycle_baseline(repo)
        assert not isinstance(start, CycleBaseline)
        block = dirty_start_block(start)
        assert block.reason == CODEX_FIX_DIRTY_START
        assert "stray.txt" in block.details
        assert not any(word in block.details for word in _REVIEW_WORDING)

    def test_dirty_start_text_names_markers_and_unmerged(self) -> None:
        block = dirty_start_block(
            DirtyStart(
                markers=("MERGE_HEAD",), unmerged=("conflict.py",), dirty=(" M a.py",)
            )
        )
        assert "MERGE_HEAD" in block.details
        assert "conflict.py" in block.details
        assert " M a.py" in block.details


class TestCommitFailure:
    def _hook_failure(self, repo: Path, script: str) -> subprocess.CalledProcessError:
        _install_pre_commit_hook(repo, script)
        _stage(repo)
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _git_commit(repo, "msg")
        return as_hook_failure(repo, excinfo.value, 0)

    def test_hook_failure_parks_codex_fix_hook_failed_via_as_hook_failure(
        self, repo: Path
    ) -> None:
        exc = self._hook_failure(
            repo, "#!/bin/sh\necho 'lint: src/a.py is wrong' >&2\nexit 1\n"
        )
        block = commit_failure_block(exc)
        assert block.reason == CODEX_FIX_HOOK_FAILED
        assert "lint: src/a.py is wrong" in block.details
        assert "Nothing was committed or pushed" in block.details
        assert not any(w in block.details for w in ("requeue REVIEW", "settle"))

    def test_hook_secret_scanner_output_is_withheld(self, repo: Path) -> None:
        exc = self._hook_failure(
            repo, "#!/bin/sh\necho 'gitleaks: AKIAEXAMPLESECRET found' >&2\nexit 1\n"
        )
        block = commit_failure_block(exc)
        assert block.reason == CODEX_FIX_HOOK_FAILED
        assert "AKIAEXAMPLESECRET" not in block.details
        assert "secret-scanning" in block.details

    def test_plain_git_failure_is_codex_error(self) -> None:
        exc = subprocess.CalledProcessError(
            128, ["git", "commit"], output="", stderr="fatal: boom"
        )
        block = commit_failure_block(exc)
        assert block.reason == CODEX_ERROR
        assert "fatal: boom" in block.details
