"""Tests for cw.codex_finalize._arm: the seam check, the arm, verification (#3062).

``prep_pr_finalize.py`` is a stub under the per-test HOME (the arm goes through
that script only; cw never calls ``gh pr merge``). ``gh`` reads for the
verification come from a scripted :class:`FakeGhCli`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw.codex_finalize._arm import (
    FAILING_CHECKS_PREFIX,
    FREEZE_PREFIX,
    ArmResult,
    arm,
    check_automerge_allowed,
    verify_pr_state,
)
from cw.codex_finalize._result import FinalizeBlock
from cw.reconcile.review_recipes.repair_git import ArmOutcome
from tests._tool_fakes import FakeGhCli, ScriptStub, stub_prep_pr_finalize

_URL = "https://github.com/o/r/pull/7"
_HEAD = "a" * 40
_PR_INFO: dict[str, object] = {"number": 7, "url": _URL}
_HEAD_FIELDS = "headRefName,headRefOid,baseRefName,state,autoMergeRequest"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


def _stub(**kwargs: Any) -> ScriptStub:
    """The shared ``prep_pr_finalize.py`` stub, installed under the per-test HOME."""
    return stub_prep_pr_finalize(Path.home(), **kwargs)


def _blocked(value: bool | FinalizeBlock) -> FinalizeBlock:
    assert isinstance(value, FinalizeBlock)
    return value


# --- check-automerge-allowed --------------------------------------------------


class TestCheckAutomergeAllowed:
    def test_exit_zero_permits(self, repo: Path) -> None:
        stub = _stub(check_exit=0)
        assert check_automerge_allowed(repo, _PR_INFO) is True
        assert stub.calls() == [["check-automerge-allowed", "--repo-path", str(repo)]]

    def test_exit_one_means_auto_merge_false_with_no_block(self, repo: Path) -> None:
        _stub(check_exit=1)
        assert check_automerge_allowed(repo, _PR_INFO) is False

    @pytest.mark.parametrize("code", [2, 3, 99])
    def test_other_codes_are_an_invocation_error_block(
        self, repo: Path, code: int
    ) -> None:
        _stub(check_exit=code, check_stderr="usage: nope")
        block = _blocked(check_automerge_allowed(repo, _PR_INFO))
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is False
        assert f"arm-automerge invocation error (exit {code}, not a gh failure)" in (
            block.details
        )
        assert "usage: nope" in block.details
        assert block.pr_info == _PR_INFO

    def test_a_missing_script_is_an_invocation_error_block(self, repo: Path) -> None:
        block = _blocked(check_automerge_allowed(repo, _PR_INFO))
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is False
        assert "prep_pr_finalize.py" in block.details
        assert block.pr_info == _PR_INFO

    def test_the_repo_local_script_wins_over_home(self, repo: Path) -> None:
        _stub(check_exit=1)
        local = repo / ".claude" / "scripts" / "prep_pr_finalize.py"
        local.parent.mkdir(parents=True)
        local.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        assert check_automerge_allowed(repo, _PR_INFO) is True

    @pytest.mark.parametrize(
        "error", [OSError("boom"), subprocess.TimeoutExpired("check", 1)]
    )
    def test_a_check_that_cannot_finish_is_retryable_unavailable(
        self, repo: Path, error: BaseException
    ) -> None:
        _stub()
        with patch("cw.codex_finalize._arm.subprocess.run", side_effect=error):
            block = _blocked(check_automerge_allowed(repo, _PR_INFO))
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True


# --- arm-automerge ------------------------------------------------------------


def _arm(repo: Path) -> ArmResult:
    return arm(repo, pr_number=7, head_sha=_HEAD, pr_info=_PR_INFO)


class TestArm:
    def test_exit_zero_is_armed_and_pins_the_head(self, repo: Path) -> None:
        stub = _stub(arm_exit=0, arm_stdout=json.dumps({"status": "armed"}))
        assert _arm(repo) == ArmResult(armed=True)
        assert stub.calls() == [
            ["arm-automerge", "7", "--repo-path", str(repo), "--head-sha", _HEAD]
        ]

    def test_exit_one_is_a_retryable_refusal_with_verbatim_gh_stderr(
        self, repo: Path
    ) -> None:
        refusal = (
            "refusing to allow an OAuth App to create or update workflow "
            "`.github/workflows/ci.yml` without `workflow` scope"
        )
        _stub(
            arm_exit=1,
            arm_stdout=json.dumps({"status": "failed", "gh_stderr": refusal}),
        )
        result = _arm(repo)
        assert result.armed is False
        block = result.block
        assert block is not None
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is True
        assert refusal in block.details
        assert ".github/workflows" in block.details
        assert "changelog-sync" not in block.details
        assert block.pr_info == _PR_INFO

    def test_a_changelog_freeze_refusal_names_changelog_sync(self, repo: Path) -> None:
        stderr = f"{FREEZE_PREFIX} CHANGELOG.md drifted from main"
        _stub(arm_exit=1, arm_stdout=json.dumps({"gh_stderr": stderr}))
        block = _arm(repo).block
        assert block is not None
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is True
        assert stderr in block.details
        assert "cw worktree changelog-sync" in block.details

    def test_a_freeze_prefix_inside_the_stderr_tail_is_detected(
        self, repo: Path
    ) -> None:
        _stub(arm_exit=1, arm_stdout="not json", arm_stderr=f"x {FREEZE_PREFIX} y")
        block = _arm(repo).block
        assert block is not None
        assert "cw worktree changelog-sync" in block.details

    def test_exit_one_without_json_falls_back_to_the_stderr_tail(
        self, repo: Path
    ) -> None:
        _stub(arm_exit=1, arm_stdout="", arm_stderr="gh exploded")
        block = _arm(repo).block
        assert block is not None
        assert "gh_stderr: gh exploded" in block.details

    def test_exit_three_ships_unarmed_without_a_block(self, repo: Path) -> None:
        _stub(arm_exit=3, arm_stdout=json.dumps({"status": "auto_merge_false"}))
        assert _arm(repo) == ArmResult(armed=False, block=None)

    @pytest.mark.parametrize("code", [2, 4, 99])
    def test_other_codes_are_an_invocation_error_block(
        self, repo: Path, code: int
    ) -> None:
        _stub(arm_exit=code, arm_stderr="bad args")
        block = _arm(repo).block
        assert block is not None
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is False
        assert f"invocation error (exit {code}, not a gh failure)" in block.details
        assert "bad args" in block.details
        assert block.pr_info == _PR_INFO

    def test_a_missing_script_is_automerge_not_armed(self, repo: Path) -> None:
        result = _arm(repo)
        assert result.armed is False
        block = result.block
        assert block is not None
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is False
        assert "prep_pr_finalize.py" in block.details

    def test_an_arm_that_never_finishes_is_retryable_unavailable(
        self, repo: Path
    ) -> None:
        _stub()
        outcome = ArmOutcome(returncode=None, status="", stderr_tail="timed out")
        with patch("cw.codex_finalize._arm._run_arm", return_value=outcome):
            block = _arm(repo).block
        assert block is not None
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert "timed out" in block.details

    def test_cw_never_runs_gh_pr_merge_itself(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = FakeGhCli().install(monkeypatch)
        _stub(arm_exit=0)
        _arm(repo)
        check_automerge_allowed(repo, _PR_INFO)
        assert gh.calls == []


# --- verification -------------------------------------------------------------


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    return FakeGhCli().install(monkeypatch)


def _head(
    gh: FakeGhCli,
    *,
    state: str = "OPEN",
    oid: str = _HEAD,
    armed: bool = True,
) -> None:
    gh.reply(
        "pr",
        "view",
        _URL,
        stdout=json.dumps(
            {
                "headRefName": "dev/42",
                "headRefOid": oid,
                "baseRefName": "main",
                "state": state,
                "autoMergeRequest": {"mergeMethod": "SQUASH"} if armed else None,
            }
        ),
    )


def _checks(gh: FakeGhCli, failing: tuple[str, ...] = ()) -> None:
    gh.reply(
        "pr",
        "checks",
        _URL,
        returncode=1 if failing else 0,
        stdout=json.dumps(
            [
                {"name": name, "workflow": "ci", "bucket": "fail", "link": ""}
                for name in failing
            ]
            + [{"name": "ok", "workflow": "ci", "bucket": "pass", "link": ""}]
        ),
    )


def _verify(
    repo: Path, *, armed_claim: bool = True
) -> tuple[FinalizeBlock | None, list[str]]:
    return verify_pr_state(
        repo,
        pr_url=_URL,
        head_sha=_HEAD,
        armed_claim=armed_claim,
        pr_info=_PR_INFO,
    )


class TestVerifyPrState:
    def test_an_armed_open_pr_with_green_checks_verifies(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        _checks(gh)
        assert _verify(repo) == (None, [])
        heads = [a for a in gh.argvs() if a[3:5] == [_URL, "--json"] and a[2] == "view"]
        assert heads[0][5] == _HEAD_FIELDS

    def test_an_unarmed_claim_matches_an_unarmed_pr(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh, armed=False)
        _checks(gh)
        assert _verify(repo, armed_claim=False) == (None, [])

    def test_an_unreadable_pr_is_retryable_unavailable(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        gh.reply("pr", "view", returncode=1)
        block, friction = _verify(repo)
        assert block is not None
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert friction == []

    @pytest.mark.parametrize("state", ["CLOSED", "DRAFT"])
    def test_a_state_other_than_open_or_merged_blocks(
        self, repo: Path, gh: FakeGhCli, state: str
    ) -> None:
        _head(gh, state=state)
        block, _ = _verify(repo)
        assert block is not None
        assert block.reason == "external_state_block"
        assert block.retry_eligible is True
        assert state in block.details

    def test_a_merged_pr_verifies_whatever_the_armed_flag(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh, state="MERGED", armed=False)
        _checks(gh)
        assert _verify(repo, armed_claim=True) == (None, [])

    def test_a_head_oid_mismatch_blocks(self, repo: Path, gh: FakeGhCli) -> None:
        _head(gh, oid="b" * 40)
        block, _ = _verify(repo)
        assert block is not None
        assert block.reason == "external_state_block"
        assert block.retry_eligible is True
        assert "b" * 40 in block.details

    @pytest.mark.parametrize(("armed", "claim"), [(False, True), (True, False)])
    def test_an_armed_flag_that_contradicts_the_claim_blocks(
        self, repo: Path, gh: FakeGhCli, armed: bool, claim: bool
    ) -> None:
        _head(gh, armed=armed)
        block, _ = _verify(repo, armed_claim=claim)
        assert block is not None
        assert block.reason == "automerge_not_armed"
        assert block.retry_eligible is True
        assert f"auto_merge_armed={armed}" in block.details
        assert block.pr_info == _PR_INFO

    def test_unreadable_checks_are_unverifiable_ci(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        gh.reply("pr", "checks", returncode=4, stderr="HTTP 502")
        block, friction = _verify(repo)
        assert block is not None
        assert block.reason == "operator_unavailable"
        assert block.retry_eligible is True
        assert "CI unverifiable" in block.details
        assert friction == []

    def test_an_empty_failing_list_passes_cleanly(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        gh.reply("pr", "checks", stdout="[]")
        assert _verify(repo) == (None, [])

    def test_no_required_checks_reported_passes_cleanly(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        gh.reply(
            "pr",
            "checks",
            returncode=1,
            stderr="no required checks reported on the 'dev/42' branch",
        )
        assert _verify(repo) == (None, [])

    def test_failing_required_checks_are_friction_not_a_block(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        _checks(gh, failing=("lint", "unit"))
        block, friction = _verify(repo)
        assert block is None
        assert friction == [FAILING_CHECKS_PREFIX + "lint, unit"]

    def test_reads_address_the_pr_by_the_bound_url(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _head(gh)
        _checks(gh)
        _verify(repo)
        assert all(_URL in argv for argv in gh.argvs())
        assert gh.records[0].cwd == repo
