"""Tests for cw.codex_finalize._gates: override, branch, fence, gates (#3062).

Real ``git`` repos through ``tests/_git_sandbox`` and stubs under the per-test
HOME. The ``cw review ...`` CLI the fence shells out to is replaced by
the shared :class:`FakeCwCli`; no real ``gh``, ``codex`` or repo gate runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_finalize import _gates
from cw.codex_finalize._ci_evidence import CiContext, DeferredGate
from cw.codex_finalize._gates import (
    APPROVAL_CHECK_PREFIX,
    APPROVAL_NOT_REQUIRED,
    APPROVAL_RECORDED,
    OVERRIDE_ABSENT_FRICTION,
    OVERRIDE_FRICTION,
    OVERRIDE_SCRIPT_NAME,
    TIP_CHECK_PREFIX,
    FinalizeGateRun,
    check_branch,
    gate_scope,
    resolve_tier,
    run_approval_fence,
    run_cw_cli,
    run_finalize_quality_gates,
    run_must_fix_override,
)
from cw.codex_finalize._result import (
    AGENT_BLOCK,
    CODEX_MUST_FIX_FINDINGS,
    OPERATOR_UNAVAILABLE,
    SCOPE_TIER_UNRESOLVABLE,
    STAGE4_MUST_FIX_OVERRIDE,
    FinalizeBlock,
    FinalizeFacts,
)
from cw.codex_fix_loop.gates import GateFailure, GatePlan, PlannedGate
from cw.codex_review import CODEX_FIX_DIRTY_START
from cw.dev_queue.gates import APPROVAL_PARK_FRICTION_PREFIX, STALE_TIP_FRICTION_PREFIX
from cw.local_runner import git_facts
from cw.models import ClientConfig, TicketTask
from cw.models.branch import resolve_feature_branch
from tests._codex_review_helpers import fresh_impl_repo, install_home_prep_pr_state
from tests._shared.git_repos import git_in
from tests._tool_fakes import FakeCwCli, GhReply, stub_cw_cli

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import ScopeTier

_BRANCH = "dev/T-1"
_TICKET = "T-1"
_CLIENT = "acme"
_SCOPE = gate_scope(_TICKET, _CLIENT, "sess-1")
_CHECK = ["review", "check-approval", "--tier", "large"]
_CONFIG = ["review", "converge", "config", "--loop-backend", "codex"]
_VERIFY = ["review", "converge", "verify-tip"]
_HEAD = "a" * 40


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    worktree, _origin = fresh_impl_repo(make_git_repo, "gates-wt", _BRANCH)
    return worktree


def _facts(repo: Path, tier: ScopeTier = "small") -> FinalizeFacts:
    return FinalizeFacts(_TICKET, repo, git_facts(repo, "main"), tier, "generated")


def _park_word(payload: dict[str, Any]) -> str:
    return str(payload["friction_highlights"][0])


# --- run_cw_cli ---------------------------------------------------------------


class TestRunCwCli:
    def test_runs_python_dash_m_cw_in_the_worktree(self, tmp_path: Path) -> None:
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with patch("cw.codex_finalize._gates.subprocess.run", return_value=done) as run:
            assert run_cw_cli(tmp_path, ["review", "x"]) is done
        argv = run.call_args.args[0]
        assert argv == [sys.executable, "-m", "cw", "review", "x"]
        assert run.call_args.kwargs["cwd"] == tmp_path

    @pytest.mark.parametrize(
        "error", [OSError("boom"), subprocess.TimeoutExpired("cw", 1)]
    )
    def test_a_launch_failure_or_timeout_is_none(
        self, tmp_path: Path, error: BaseException
    ) -> None:
        with patch("cw.codex_finalize._gates.subprocess.run", side_effect=error):
            assert run_cw_cli(tmp_path, ["review"]) is None


# --- tier ---------------------------------------------------------------------


class TestResolveTier:
    def test_plan_marker_wins(self, repo: Path) -> None:
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT, scope_hint="small")
        plan = "**Scope tier:** large\n"
        assert resolve_tier(task, plan, git_facts(repo, "main")) == "large"

    def test_scope_hint_is_used_without_a_marker(self, repo: Path) -> None:
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT, scope_hint="large")
        assert resolve_tier(task, None, git_facts(repo, "main")) == "large"

    def test_an_unresolved_tier_is_a_block_never_a_guess(self, repo: Path) -> None:
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT)
        with patch(
            "cw.codex_finalize._gates.resolve_review_scope_tier", return_value=None
        ):
            block = resolve_tier(task, None, git_facts(repo, "main"))
        assert isinstance(block, FinalizeBlock)
        assert block.reason == SCOPE_TIER_UNRESOLVABLE
        assert block.retry_eligible is True
        assert "never guesses" in block.details


# --- check_branch -------------------------------------------------------------


class TestCheckBranch:
    @pytest.fixture(autouse=True)
    def _client(self, tmp_path: Path) -> None:
        self.client = ClientConfig(name=_CLIENT, workspace_path=tmp_path)

    def _expected(self, task: TicketTask) -> str:
        return resolve_feature_branch(task.ticket_id, self.client, task)

    def test_the_prefix_branch_continues(self, repo: Path) -> None:
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT)
        assert check_branch(repo, self._expected(task)) is None

    def test_a_recorded_branch_with_the_worktree_on_it_continues(
        self, repo: Path
    ) -> None:
        git_in(repo, "checkout", "-b", "dev/recorded")
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT, branch="dev/recorded")
        assert self._expected(task) == "dev/recorded"
        assert check_branch(repo, self._expected(task)) is None

    def test_another_branch_blocks_and_names_both(self, repo: Path) -> None:
        git_in(repo, "checkout", "-b", "dev/elsewhere")
        block = check_branch(repo, _BRANCH)
        assert block is not None
        assert block.reason == "external_state_block"
        assert block.retry_eligible is False
        assert "'dev/elsewhere'" in block.details
        assert repr(_BRANCH) in block.details
        assert "nothing was pushed" in block.details

    def test_a_recorded_branch_the_worktree_is_not_on_blocks(self, repo: Path) -> None:
        task = TicketTask(ticket_id=_TICKET, client=_CLIENT, branch="dev/recorded")
        block = check_branch(repo, self._expected(task))
        assert block is not None
        assert block.reason == "external_state_block"
        assert "'dev/recorded'" in block.details

    def test_a_detached_head_blocks(self, repo: Path) -> None:
        git_in(repo, "checkout", "--detach")
        block = check_branch(repo, _BRANCH)
        assert block is not None
        assert block.reason == "external_state_block"
        assert block.retry_eligible is False
        assert "detached HEAD" in block.details

    def test_a_git_failure_blocks_naming_the_failure(self, tmp_path: Path) -> None:
        block = check_branch(tmp_path / "not-a-repo", _BRANCH)
        assert block is not None
        assert block.reason == "external_state_block"
        assert "git failed" in block.details


# --- must-fix override --------------------------------------------------------


def _override_script(
    *,
    code: int,
    stdout: str = "",
    stderr: str = "",
    marker: str | None = "# cw-script-version: 1",
    record: Path | None = None,
) -> Path:
    path = Path.home() / ".claude" / "scripts" / OVERRIDE_SCRIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/usr/bin/env python3"]
    if marker is not None:
        lines.append(marker)
    lines += ["import sys"]
    if record is not None:
        lines.append(f"open({str(record)!r}, 'w').write(' '.join(sys.argv[1:]))")
    lines += [
        f"sys.stdout.write({stdout!r})",
        f"sys.stderr.write({stderr!r})",
        f"sys.exit({code})",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestMustFixOverride:
    def test_exit_zero_clean_continues(self, repo: Path) -> None:
        _override_script(code=0, stdout=json.dumps({"status": "clean"}))
        outcome = run_must_fix_override(repo, _HEAD)
        assert outcome.block is None
        assert outcome.friction == []
        assert outcome.override_json is None

    @pytest.mark.parametrize("stdout", ["not json", "[1, 2]", '{"status": 3}', ""])
    def test_exit_zero_without_an_overridden_status_is_clean(
        self, repo: Path, stdout: str
    ) -> None:
        _override_script(code=0, stdout=stdout)
        outcome = run_must_fix_override(repo, _HEAD)
        assert outcome.block is None
        assert outcome.override_json is None
        assert outcome.friction == []

    def test_exit_zero_overridden_keeps_the_output_and_logs_friction(
        self, repo: Path
    ) -> None:
        body = json.dumps({"status": "overridden", "reason": "operator"})
        _override_script(code=0, stdout=body + "\n")
        outcome = run_must_fix_override(repo, _HEAD)
        assert outcome.block is None
        assert outcome.friction == [OVERRIDE_FRICTION] == ["must_fix_operator_override"]
        assert outcome.override_json == body

    def test_exit_one_parks_codex_must_fix_findings(self, repo: Path) -> None:
        _override_script(code=1, stdout="2 MUST_FIX findings are unresolved")
        outcome = run_must_fix_override(repo, _HEAD)
        block = outcome.block
        assert block is not None
        assert block.reason == CODEX_MUST_FIX_FINDINGS
        assert (
            block.blocker_stage
            == STAGE4_MUST_FIX_OVERRIDE
            == "stage4_must_fix_override"
        )
        assert block.retry_eligible is False
        assert "2 MUST_FIX findings are unresolved" in block.details
        assert "--override-must-fix" in block.details

    def test_exit_one_with_only_stderr_uses_it(self, repo: Path) -> None:
        _override_script(code=1, stderr="stderr says no")
        block = run_must_fix_override(repo, _HEAD).block
        assert block is not None
        assert "stderr says no" in block.details

    def test_other_nonzero_is_agent_block(self, repo: Path) -> None:
        _override_script(code=2, stderr="usage error")
        block = run_must_fix_override(repo, _HEAD).block
        assert block is not None
        assert block.reason == AGENT_BLOCK
        assert block.blocker_stage is None
        assert "exited 2" in block.details
        assert "usage error" in block.details

    def test_script_absent_is_friction_only(self, repo: Path) -> None:
        outcome = run_must_fix_override(repo, _HEAD)
        assert outcome.block is None
        assert outcome.friction == [OVERRIDE_ABSENT_FRICTION]

    @pytest.mark.parametrize("marker", [None, "# cw-script-version: 0", "# nope"])
    def test_a_stale_or_missing_marker_is_agent_block(
        self, repo: Path, marker: str | None
    ) -> None:
        _override_script(code=0, marker=marker)
        block = run_must_fix_override(repo, _HEAD).block
        assert block is not None
        assert block.reason == AGENT_BLOCK
        assert "cw-script-version" in block.details

    def test_the_repo_local_script_wins_over_home(self, repo: Path) -> None:
        _override_script(code=1, stdout="home")
        local = repo / ".claude" / "scripts" / OVERRIDE_SCRIPT_NAME
        local.parent.mkdir(parents=True)
        local.write_text(
            "#!/usr/bin/env python3\n# cw-script-version: 1\nimport sys\nsys.exit(0)\n",
            encoding="utf-8",
        )
        assert run_must_fix_override(repo, _HEAD).block is None

    def test_the_script_gets_verdict_context_and_head(
        self, repo: Path, tmp_path: Path
    ) -> None:
        record = tmp_path / "argv.txt"
        _override_script(code=0, record=record)
        run_must_fix_override(repo, _HEAD)
        assert record.read_text(encoding="utf-8") == (
            f"--verdict {repo}/.claude/review-verdict.json "
            f"--context {repo}/.claude/cw-context.json --head {_HEAD}"
        )

    @pytest.mark.parametrize(
        "error", [OSError("boom"), subprocess.TimeoutExpired("script", 1)]
    )
    def test_a_script_that_cannot_finish_is_retryable_unavailable(
        self, repo: Path, error: BaseException
    ) -> None:
        _override_script(code=0)
        with patch("cw.codex_finalize._gates.subprocess.run", side_effect=error):
            block = run_must_fix_override(repo, _HEAD).block
        assert block is not None
        assert block.reason == OPERATOR_UNAVAILABLE
        assert block.retry_eligible is True


# --- Step 4.0 approval fence --------------------------------------------------

_UNSET: Any = object()
_BLOCKING = GhReply(0, json.dumps({"convergence": "blocking"}), "")
_CLEAN = GhReply(0, json.dumps({"convergence": "clean"}), "")
_RECORDED = GhReply(0, f"{APPROVAL_RECORDED}\n", "")


def _cw(
    monkeypatch: pytest.MonkeyPatch,
    *,
    check: GhReply | None = _UNSET,
    config: GhReply | None = _UNSET,
    verify: GhReply | None = _UNSET,
) -> FakeCwCli:
    """Script ``cw review ...``; a ``None`` reply is a call that never finishes."""
    replies: dict[str, GhReply | None] = {}
    if check is not _UNSET:
        replies["review check-approval"] = check
    if config is not _UNSET:
        replies["review converge config"] = config
    if verify is not _UNSET:
        replies["review converge verify-tip"] = verify
    return stub_cw_cli(monkeypatch, replies)


def _fence(repo: Path, tier: ScopeTier = "large") -> _gates.FenceOutcome:
    return run_approval_fence(
        repo,
        _facts(repo, tier),
        tier=tier,
        ticket_id=_TICKET,
        client_name=_CLIENT,
    )


class TestApprovalFence:
    def test_small_is_not_required_without_calling_check_approval(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(monkeypatch, config=_BLOCKING)
        outcome = _fence(repo, "small")
        assert outcome.park is None
        assert outcome.friction == [f"{APPROVAL_CHECK_PREFIX}{APPROVAL_NOT_REQUIRED}"]
        assert not any(call[:2] == ["review", "check-approval"] for call in cw.calls)

    def test_large_recorded_continues(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(monkeypatch, check=_RECORDED, config=_BLOCKING)
        outcome = _fence(repo)
        assert outcome.park is None
        assert outcome.friction == [f"{APPROVAL_CHECK_PREFIX}{APPROVAL_RECORDED}"]
        assert cw.calls[0] == _CHECK

    def test_large_without_approval_parks_for_review(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(
            monkeypatch,
            check=GhReply(
                0, "REVIEW_APPROVAL_PARK: missing — no approval recorded\n", ""
            ),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["status"] == "review_pending_approval"
        assert park["stage_reached"] == "stage4a_merge_gate"
        assert park["scope"]["tier"] == "large"
        assert park["pr"] is None
        assert park["blocker"] is None
        assert park["next_actions"] == ["requeue_stage_review"]
        first, second = park["friction_highlights"]
        assert first.startswith(f"{APPROVAL_PARK_FRICTION_PREFIX} missing")
        assert f"cw dev-queue requeue {_TICKET} -c {_CLIENT} --stage review" in first
        assert second == f"{APPROVAL_CHECK_PREFIX}missing"
        AutoDevResult.model_validate(park)
        assert _CONFIG not in cw.calls

    @pytest.mark.parametrize("code", [127, 2])
    def test_a_missing_or_old_cw_is_an_unreadable_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        _cw(monkeypatch, check=GhReply(code, "", ""))
        park = _fence(repo).park
        assert park is not None
        assert park["scope"]["tier"] == "large"
        assert f"{APPROVAL_PARK_FRICTION_PREFIX} unreadable" in _park_word(park)
        assert park["friction_highlights"][1] == f"{APPROVAL_CHECK_PREFIX}unreadable"

    @pytest.mark.parametrize("code", [1, 5])
    def test_a_nonzero_with_no_output_is_an_unreadable_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        _cw(monkeypatch, check=GhReply(code, "", ""))
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == f"{APPROVAL_CHECK_PREFIX}unreadable"

    def test_a_nonzero_exit_is_never_trusted_as_a_verdict(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, check=GhReply(1, f"{APPROVAL_RECORDED}\n", ""))
        assert _fence(repo).park is not None

    def test_a_check_that_never_finishes_is_an_unreadable_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, check=None)
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == f"{APPROVAL_CHECK_PREFIX}unreadable"

    def test_a_park_line_with_no_token_is_unreadable(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, check=GhReply(0, "REVIEW_APPROVAL_PARK:\n", ""))
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == f"{APPROVAL_CHECK_PREFIX}unreadable"

    def test_unresolved_tier_token_parks_large(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(
            monkeypatch,
            check=GhReply(0, "REVIEW_APPROVAL_PARK: unresolved_tier — x\n", ""),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["scope"]["tier"] == "large"
        assert park["friction_highlights"][1] == (
            f"{APPROVAL_CHECK_PREFIX}unresolved_tier"
        )


# --- Step 4.0b clean-mode tip -------------------------------------------------


def _stale(reason: str | None, detail: str | None = None) -> GhReply:
    data: dict[str, object] = {"ok": False}
    if reason is not None:
        data["reason"] = reason
    if detail is not None:
        data["detail"] = detail
    return GhReply(1, json.dumps(data), "")


class TestCleanModeTip:
    def test_blocking_skips_the_tip_check(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(monkeypatch, check=_RECORDED, config=_BLOCKING)
        assert _fence(repo).park is None
        assert _VERIFY not in cw.calls

    def test_clean_and_accepted_continues(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(
            monkeypatch, check=_RECORDED, config=_CLEAN, verify=GhReply(0, "{}", "")
        )
        outcome = _fence(repo)
        assert outcome.park is None
        assert outcome.friction == [
            f"{APPROVAL_CHECK_PREFIX}{APPROVAL_RECORDED}",
            f"{TIP_CHECK_PREFIX}accepted",
        ]
        assert cw.calls == [_CHECK, _CONFIG, _VERIFY]

    def test_stale_parks_with_the_exact_friction_and_the_resolved_tier(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(
            monkeypatch,
            check=_RECORDED,
            config=_CLEAN,
            verify=_stale("commits_after_clean_round", "abc123 landed"),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["scope"]["tier"] == "large"
        assert park["friction_highlights"] == [
            f"{APPROVAL_CHECK_PREFIX}{APPROVAL_RECORDED}",
            f"{STALE_TIP_FRICTION_PREFIX} (commits_after_clean_round: abc123 landed)",
        ]
        assert park["next_actions"] == ["requeue_stage_review"]
        assert park["status"] == "review_pending_approval"
        assert park["blocker"] is None

    def test_a_small_ticket_keeps_its_small_tier_on_a_stale_tip(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, config=_CLEAN, verify=_stale("no_clean_round"))
        park = _fence(repo, "small").park
        assert park is not None
        assert park["scope"]["tier"] == "small"
        assert park["friction_highlights"] == [
            f"{APPROVAL_CHECK_PREFIX}{APPROVAL_NOT_REQUIRED}",
            f"{STALE_TIP_FRICTION_PREFIX} (no_clean_round)",
        ]

    def test_fetch_failure_requeues_finalize(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(
            monkeypatch,
            check=_RECORDED,
            config=_CLEAN,
            verify=_stale("tip_check_fetch_failed", "no network"),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["next_actions"] == ["requeue_stage_finalize"]
        assert park["friction_highlights"][1] == (
            f"{STALE_TIP_FRICTION_PREFIX} (tip_check_fetch_failed: no network)"
        )

    @pytest.mark.parametrize(
        "stdout", ["not json", "[]", json.dumps({"ok": False}), ""]
    )
    def test_a_verify_with_no_readable_reason_is_the_bare_stale_token(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        _cw(
            monkeypatch,
            check=_RECORDED,
            config=_CLEAN,
            verify=GhReply(1, stdout, ""),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == STALE_TIP_FRICTION_PREFIX

    def test_a_verify_that_never_finishes_is_the_bare_stale_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, check=_RECORDED, config=_CLEAN, verify=None)
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == STALE_TIP_FRICTION_PREFIX
        assert park["next_actions"] == ["requeue_stage_review"]

    def test_a_reason_without_detail_has_no_detail_clause(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(
            monkeypatch,
            check=_RECORDED,
            config=_CLEAN,
            verify=_stale("head_not_origin_tip", ""),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1].endswith("(head_not_origin_tip)")

    @pytest.mark.parametrize(
        "stdout", ["not json", "[]", json.dumps({"convergence": 3}), ""]
    )
    def test_a_converge_config_error_is_a_stale_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        cw = _cw(monkeypatch, check=_RECORDED, config=GhReply(0, stdout, ""))
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == STALE_TIP_FRICTION_PREFIX
        assert _VERIFY not in cw.calls

    @pytest.mark.parametrize("code", [1, 127])
    def test_a_failing_converge_config_is_a_stale_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        _cw(
            monkeypatch,
            check=_RECORDED,
            config=GhReply(code, json.dumps({"convergence": "clean"}), ""),
        )
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == STALE_TIP_FRICTION_PREFIX

    def test_a_converge_config_that_never_finishes_is_a_stale_park(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cw(monkeypatch, check=_RECORDED, config=None)
        park = _fence(repo).park
        assert park is not None
        assert park["friction_highlights"][1] == STALE_TIP_FRICTION_PREFIX

    def test_the_fence_is_idempotent_across_runs(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cw = _cw(
            monkeypatch, check=_RECORDED, config=_CLEAN, verify=GhReply(0, "{}", "")
        )
        assert _fence(repo) == _fence(repo)
        assert cw.calls == [_CHECK, _CONFIG, _VERIFY] * 2


# --- quality gates ------------------------------------------------------------


@pytest.fixture
def gated(repo: Path) -> Path:
    """*repo* with a committed ``src/a.py`` a gate can dirty."""
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "add a")
    return repo


_PR_URL = "https://github.com/o/r/pull/7"


def _ci(path: str) -> CiContext:
    """The CI context a path carries: the create path has no PR yet."""
    if path == "create":
        return CiContext(base="main", head=_HEAD)
    return CiContext(base="main", pr_number=7, pr_url=_PR_URL, head=_HEAD)


def _gate_run(repo: Path, path: str) -> FinalizeGateRun:
    return run_finalize_quality_gates(repo, scope=_SCOPE, path=path, ci=_ci(path))


def _gates_run(repo: Path, path: str) -> tuple[FinalizeBlock | None, list[str]]:
    run = _gate_run(repo, path)
    return run.block, run.friction


@pytest.mark.parametrize("path", ["reuse", "create"])
class TestQualityGates:
    def test_a_green_gate_passes_with_the_path_literal(
        self, gated: Path, tmp_path: Path, path: str
    ) -> None:
        install_home_prep_pr_state(tmp_path / "state", ("lint", "true", 5.0))
        head = git_in(gated, "rev-parse", "HEAD")
        block, friction = _gates_run(gated, path)
        assert block is None
        assert friction == [f"{path}_path_gates: passed"]
        assert git_in(gated, "rev-parse", "HEAD") == head

    def test_zero_gates_is_none_detected(
        self, gated: Path, tmp_path: Path, path: str
    ) -> None:
        install_home_prep_pr_state(tmp_path / "state")
        block, friction = _gates_run(gated, path)
        assert block is None
        assert friction == [f"{path}_path_gates: none_detected"]

    def test_a_red_gate_is_agent_block_with_its_output(
        self, gated: Path, tmp_path: Path, path: str
    ) -> None:
        install_home_prep_pr_state(
            tmp_path / "state", ("lint", "echo boom-output-xyz; exit 3", 5.0)
        )
        block, friction = _gates_run(gated, path)
        assert block is not None
        assert friction == []
        assert block.reason == AGENT_BLOCK
        assert block.retry_eligible is False
        assert f"{path}-path quality gate `lint`" in block.details
        assert "exit code 3" in block.details
        assert "boom-output-xyz" in block.details
        assert "Nothing was pushed, created or armed" in block.details

    def test_a_dirtied_tree_is_a_failed_gate(
        self, gated: Path, tmp_path: Path, path: str
    ) -> None:
        install_home_prep_pr_state(
            tmp_path / "state", ("fmt", "echo changed > src/a.py", 5.0)
        )
        block, _ = _gates_run(gated, path)
        assert block is not None
        assert block.reason == AGENT_BLOCK
        assert "dirtied the worktree" in block.details

    def test_an_unavailable_detector_is_external_state_block(
        self, gated: Path, path: str
    ) -> None:
        block, friction = _gates_run(gated, path)
        assert block is not None
        assert friction == []
        assert block.reason == "external_state_block"
        assert "could not be run" in block.details

    def test_a_dirty_start_blocks_before_any_gate_runs(
        self, gated: Path, tmp_path: Path, path: str
    ) -> None:
        state = tmp_path / "state"
        stub = install_home_prep_pr_state(state, ("lint", "true", 5.0))
        (gated / "junk.txt").write_text("x", encoding="utf-8")
        block, friction = _gates_run(gated, path)
        assert block is not None
        assert friction == []
        assert block.reason == CODEX_FIX_DIRTY_START
        assert "nothing was pushed or created" in block.details
        assert stub.calls() == []

    def test_autofix_is_forced_off_and_nothing_is_committed(
        self, gated: Path, path: str
    ) -> None:
        plan = GatePlan(
            script=None,
            gates=(PlannedGate("lint", "true", 5.0, autofix="make fmt"),),
            why_unavailable=None,
        )
        seen: list[GatePlan] = []

        def _capture(given: GatePlan, **_kwargs: object) -> None:
            seen.append(given)

        head = git_in(gated, "rev-parse", "HEAD")
        with (
            patch("cw.codex_finalize._gates.plan_cycle_gates", return_value=plan),
            patch("cw.codex_finalize._gates.run_cycle_gates", side_effect=_capture),
        ):
            block, friction = _gates_run(gated, path)
        assert block is None
        assert friction == [f"{path}_path_gates: passed"]
        (given,) = seen
        assert [gate.autofix for gate in given.gates] == [None]
        assert [gate.name for gate in given.gates] == ["lint"]
        assert git_in(gated, "rev-parse", "HEAD") == head

    def test_a_failure_with_no_reason_or_output_still_blocks(
        self, gated: Path, path: str
    ) -> None:
        plan = GatePlan(script=None, gates=(), why_unavailable="detector missing")
        failure = GateFailure(
            kind="unavailable",
            gate="",
            command="",
            exit_code=None,
            seconds=0.0,
            output="",
            why=None,
        )
        with (
            patch("cw.codex_finalize._gates.plan_cycle_gates", return_value=plan),
            patch("cw.codex_finalize._gates.run_cycle_gates", return_value=failure),
        ):
            block, _ = _gates_run(gated, path)
        assert block is not None
        assert "no reason recorded" in block.details


def test_the_two_paths_use_different_friction_literals(
    gated: Path, tmp_path: Path
) -> None:
    install_home_prep_pr_state(tmp_path / "state", ("lint", "true", 5.0))
    assert _gates_run(gated, "reuse")[1] == ["reuse_path_gates: passed"]
    assert _gates_run(gated, "create")[1] == ["create_path_gates: passed"]


def test_gate_scope_labels_cycle_zero() -> None:
    assert _SCOPE.cycle == 0
    assert (_SCOPE.ticket_id, _SCOPE.client, _SCOPE.session_id) == (
        _TICKET,
        _CLIENT,
        "sess-1",
    )


# --- CI evidence (#3618) ------------------------------------------------------


def _logging_gates(log: Path, *names: str) -> tuple[tuple[str, str, float], ...]:
    """Gates that append their name to *log* (outside the worktree) when run."""
    return tuple((name, f"echo {name} >> {log}", 5.0) for name in names)


def _ran(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.exists() else []


class TestCreateCiEvidence:
    def test_a_deferred_gate_is_dropped_and_reported(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint", "fmt")
        )
        stub.defer(("lint", ("ci-lint", "ci-lint-2")))
        run = _gate_run(gated, "create")
        assert run.block is None
        assert run.deferred == (DeferredGate("lint", ("ci-lint", "ci-lint-2")),)
        assert run.friction == [
            "create_path_gate_ci_deferred: lint via ci-lint, ci-lint-2",
            "create_path_gates: passed",
        ]
        assert _ran(log) == ["fmt"]
        assert "ci-deferral --base main" in stub.calls()

    def test_every_gate_deferred_reports_only_the_deferral(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint")
        )
        stub.defer(("lint", ("ci-lint",)))
        run = _gate_run(gated, "create")
        assert run.block is None
        assert run.friction == ["create_path_gate_ci_deferred: lint via ci-lint"]
        assert [d.gate for d in run.deferred] == ["lint"]
        assert _ran(log) == []

    def test_no_map_runs_every_gate_with_nothing_deferred(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        install_home_prep_pr_state(tmp_path / "state", *_logging_gates(log, "lint"))
        run = _gate_run(gated, "create")
        assert run.deferred == ()
        assert run.friction == ["create_path_gates: passed"]
        assert _ran(log) == ["lint"]

    def test_an_unavailable_consult_runs_every_gate_locally(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint", "fmt")
        )
        stub.set(deferral_exit=1)
        run = _gate_run(gated, "create")
        assert run.block is None
        assert run.deferred == ()
        assert run.friction == [
            "ci_evidence_unavailable: ci-deferral exit 1",
            "create_path_gates: passed",
        ]
        assert _ran(log) == ["lint", "fmt"]

    def test_zero_detected_gates_never_consult(
        self, gated: Path, tmp_path: Path
    ) -> None:
        stub = install_home_prep_pr_state(tmp_path / "state")
        run = _gate_run(gated, "create")
        assert run.friction == ["create_path_gates: none_detected"]
        assert not any(call.startswith("ci-") for call in stub.calls())

    def test_a_red_local_gate_still_blocks_beside_a_deferred_one(
        self, gated: Path, tmp_path: Path
    ) -> None:
        stub = install_home_prep_pr_state(
            tmp_path / "state", ("lint", "true", 5.0), ("fmt", "exit 3", 5.0)
        )
        stub.defer(("lint", ("ci-lint",)))
        run = _gate_run(gated, "create")
        assert run.block is not None
        assert run.block.reason == AGENT_BLOCK
        assert "`fmt`" in run.block.details


def _park(flavor: str, state: str, url: str | None = None) -> dict[str, object]:
    return {
        "flavor": flavor,
        "gate": "lint",
        "checks": ["ci-lint"],
        "failed_check": "ci-lint",
        "check_state": state,
        "failed_url": url,
        "sha": _HEAD,
    }


class TestReuseCiEvidence:
    def test_a_pending_park_blocks_without_running_any_gate(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint", "fmt")
        )
        stub.reuse_plan(park=_park("pending", "in_progress"))
        run = _gate_run(gated, "reuse")
        block = run.block
        assert block is not None
        assert block.reason == "awaiting_ci_evidence"
        assert block.retry_eligible is True
        assert block.stage == "stage5_post_create"
        assert block.details == (
            f"ci_evidence_pending: PR #7 ({_PR_URL}) at head {_HEAD}: gate lint "
            "mapped to check ci-lint is in_progress; cw did not arm auto-merge "
            "and did not run the gate locally."
        )
        assert block.pr_info == {
            "number": 7,
            "url": _PR_URL,
            "auto_merge": False,
            "base": "main",
        }
        assert _ran(log) == []
        assert "ci-reuse-plan --pr 7 --base main" in stub.calls()

    def test_a_red_park_is_not_retry_eligible(
        self, gated: Path, tmp_path: Path
    ) -> None:
        stub = install_home_prep_pr_state(tmp_path / "state", ("lint", "true", 5.0))
        stub.reuse_plan(park=_park("red", "failure", "https://ci/run/1"))
        block = _gate_run(gated, "reuse").block
        assert block is not None
        assert block.reason == "awaiting_ci_evidence"
        assert block.retry_eligible is False
        assert block.details.startswith(
            f"ci_evidence_red: PR #7 ({_PR_URL}) at head {_HEAD}: gate lint mapped "
            "to check ci-lint concluded failure (https://ci/run/1). This is a "
            "failing CI check, not a gate failure;"
        )

    def test_a_skipped_gate_is_dropped_and_logged(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint", "fmt")
        )
        stub.reuse_plan(skip=(("lint", ("ci-lint",)),))
        run = _gate_run(gated, "reuse")
        assert run.block is None
        assert run.deferred == ()
        assert run.friction == [
            "reuse_path_gate_ci_covered: lint via ci-lint",
            "reuse_path_gates: passed",
        ]
        assert _ran(log) == ["fmt"]

    def test_every_gate_covered_passes_without_running_any(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint")
        )
        stub.reuse_plan(skip=(("lint", ("ci-lint",)),))
        run = _gate_run(gated, "reuse")
        assert run.block is None
        assert run.friction == [
            "reuse_path_gate_ci_covered: lint via ci-lint",
            "reuse_path_gates: passed",
        ]
        assert _ran(log) == []

    def test_an_unavailable_plan_runs_every_gate_locally(
        self, gated: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "ran.log"
        stub = install_home_prep_pr_state(
            tmp_path / "state", *_logging_gates(log, "lint")
        )
        stub.set(reuse_plan_raw="not json")
        run = _gate_run(gated, "reuse")
        assert run.block is None
        assert run.friction == [
            "ci_evidence_unavailable: ci-reuse-plan output is not JSON",
            "reuse_path_gates: passed",
        ]
        assert _ran(log) == ["lint"]
