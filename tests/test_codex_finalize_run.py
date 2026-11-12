"""Tests for cw.codex_finalize._run: the codex FINALIZE stage end to end (#3062).

Every run goes through the ``cw codex run --stage finalize`` driver core
(``run_codex_finalize_stage``) against a ``ScriptedCodexRunner`` and a scripted
``gh``: no real ``codex`` or ``gh`` runs. The worktree is a real, sandboxed git
repo pushed to a bare origin with ``.cw/`` excluded. The repo's quality gates,
``prep_pr_finalize.py`` and the override script are stubs under the per-test
HOME; ``cw review ...`` and ``cw dev-queue adopted-prs`` are a scripted
:class:`FakeCwCli`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_driver import run_codex_finalize_stage
from cw.codex_finalize._run import _context
from cw.codex_review import _CODEX_FINALIZE_BLOCKED_NEXT_ACTIONS
from cw.codex_runner import CodexRunResult
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.local_runner import UNEXPECTED_ERROR
from cw.merge_probe import MergeProbe
from cw.models import (
    ClientConfig,
    OrchestratorEventType,
    QueueItemStatus,
    StageExecutorConfig,
)
from cw.models.orchestrator_config.stage import ModelPrice
from cw.models.usage import UsageStage, UsageState, UsageUnavailableReason
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import TrackerIoContext, plan_turn_parts
from tests._backend_mcp_helpers import codex_tracker_client, stage_mcp_config
from tests._codex_review_helpers import (
    fresh_finalize_repo,
    install_home_prep_pr_state,
    seed_codex_finalize_run,
)
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.git_repos import git_in
from tests._tool_fakes import (
    FakeCwCli,
    FakeGhCli,
    GhReply,
    ScriptedCodexRunner,
    ScriptStub,
    any_call,
    codex_usage_stdout,
    finalize_document,
    finalize_turn_result,
    stdin_contains,
    stub_cw_cli,
    stub_prep_pr_finalize,
    tracker_io_turn_result,
)
from tests.test_codex_plan_run import SID, TICKET, persisted_result, script_gh

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

BRANCH = f"dev/{TICKET}"
REPO = "o/r"
PR_NUMBER = 7
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
FINALIZE_TURN = "Codex FINALIZE addendum"
PLAN = "## Summary\n\nAdd the widget.\n\n**Scope tier:** small\n"
LARGE_PLAN = PLAN.replace("small", "large")
TITLE = "Add the widget"
BODY = "## Summary\n\nAdds the widget.\n"

_BLOCKING = GhReply(0, json.dumps({"convergence": "blocking"}), "")
_CLEAN = GhReply(0, json.dumps({"convergence": "clean"}), "")
_PARK_MISSING = GhReply(0, "REVIEW_APPROVAL_PARK: missing — nothing\n", "")
_TRANSIENT = "transient"
_FENCE_EVENTS = [
    OrchestratorEventType.STAGE_ENTERED,
    OrchestratorEventType.STAGE_ERRORED,
    OrchestratorEventType.SESSION_COMPLETED,
]
_COMMENT_EVENTS = [
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_POSTED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED,
]
_TRACED = (
    "run_approval_fence",
    "run_finalize_quality_gates",
    "probe_default",
    "push_ahead",
    "create_pr_for",
    "check_automerge_allowed",
    "arm",
)


def _view(**overrides: object) -> str:
    body: dict[str, object] = {
        "number": PR_NUMBER,
        "url": PR_URL,
        "headRefName": BRANCH,
        "headRefOid": "",
    }
    body.update(overrides)
    return json.dumps(body)


def _pr_list(state: str) -> str:
    return json.dumps(
        [{"number": PR_NUMBER, "url": PR_URL, "state": state, "mergedAt": None}]
    )


@dataclass
class Harness:
    """One seeded FINALIZE run: worktree, origin, scripted codex, gh and cw."""

    worktree: Path
    origin: Path
    runner: ScriptedCodexRunner
    gh: FakeGhCli
    arm: ScriptStub
    gates: Any
    head: str
    monkeypatch: pytest.MonkeyPatch
    client: ClientConfig | None = None
    cw_replies: dict[str, Any] = field(default_factory=dict)
    cw: FakeCwCli = field(default_factory=lambda: FakeCwCli({}))
    order: list[str] = field(default_factory=list)

    def set_cw(self, prefix: str, reply: Any) -> Harness:
        """(Re)script one ``cw`` CLI prefix; a list is consumed in order."""
        self.cw_replies[prefix] = reply
        self.cw = stub_cw_cli(self.monkeypatch, self.cw_replies)
        return self

    def run(self) -> AutoDevResult:
        if self.client is None:
            run_codex_finalize_stage(
                ticket_id=TICKET,
                session_id=SID,
                wall_clock_budget_seconds=None,
                runner=self.runner,
            )
        else:
            with patch("cw.codex_driver.get_client", return_value=self.client):
                run_codex_finalize_stage(
                    ticket_id=TICKET,
                    session_id=SID,
                    wall_clock_budget_seconds=None,
                    runner=self.runner,
                )
        return persisted_result()

    def raw(self) -> dict[str, Any]:
        """The persisted result as the door stored it (``pr_info`` and all)."""
        last = load_state().sessions[0].last_result
        assert last is not None
        return dict(last)

    def turn(
        self,
        document: str | None = None,
        *,
        result: CodexRunResult | None = None,
    ) -> Harness:
        """Script the one read-only turn (recorded in :attr:`order`)."""
        reply = result or finalize_turn_result(document)

        def _answer(_argv: list[str], _stdin: str | None) -> CodexRunResult:
            self.order.append("turn")
            return reply

        self.runner.add(stdin_contains(FINALIZE_TURN), _answer)
        return self

    @property
    def turns(self) -> list[dict[str, object]]:
        return [c for c in self.runner.calls if FINALIZE_TURN in str(c["stdin"])]

    def gh_argvs(self, *prefix: str) -> list[list[str]]:
        return [a for a in self.gh.argvs() if a[1 : 1 + len(prefix)] == list(prefix)]

    def created(self) -> list[Any]:
        return [r for r in self.gh.records if r.argv[1:3] == ["pr", "create"]]

    def origin_tip(self) -> str:
        return git_in(self.origin, "rev-parse", BRANCH)

    def local_head(self) -> str:
        return git_in(self.worktree, "rev-parse", "HEAD")

    def trace(self) -> Harness:
        """Record the order of the flow's mechanical steps in :attr:`order`."""
        from cw.codex_finalize import _run

        for name in _TRACED:
            real = getattr(_run, name)

            def _wrapper(
                *args: Any, _real: Any = real, _name: str = name, **kw: Any
            ) -> Any:
                self.order.append(_name)
                return _real(*args, **kw)

            self.monkeypatch.setattr(f"cw.codex_finalize._run.{name}", _wrapper)
        return self


def _script_default_gh(gh: FakeGhCli, head: str) -> None:
    script_gh(gh)
    gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": REPO}))
    gh.reply("pr", "list", "--head", BRANCH, stdout="[]")
    gh.reply("pr", "list", "--state", "open", stdout="[]")
    gh.reply("pr", "create", stdout=f"{PR_URL}\n")
    gh.reply("pr", "edit", stdout="")
    gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefOid=head),
    )
    gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "mergeable,mergeStateStatus",
        stdout=json.dumps({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}),
    )
    gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "headRefName,headRefOid,baseRefName,state,autoMergeRequest",
        stdout=json.dumps(
            {
                "headRefName": BRANCH,
                "headRefOid": head,
                "baseRefName": "main",
                "state": "OPEN",
                "autoMergeRequest": {"mergeMethod": "SQUASH"},
            }
        ),
    )
    gh.reply("pr", "checks", PR_URL, stdout="[]")


@pytest.fixture
def harness(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Harness]:
    """Build a seeded FINALIZE run on the create path (no PR yet)."""

    def _build(
        *,
        plan: str | None = PLAN,
        task_updates: Mapping[str, object] | None = None,
        gates: tuple[tuple[str, str, float], ...] = (("lint", "true", 5.0),),
        unpushed: int = 0,
        branch: str = BRANCH,
    ) -> Harness:
        worktree, origin = fresh_finalize_repo(
            make_git_repo, "wt-finalize", branch, unpushed_commits=unpushed
        )
        updates: dict[str, object] = {"plan_source": "generated"}
        if branch != BRANCH:
            updates["branch"] = branch
        updates.update(task_updates or {})
        seed_codex_finalize_run(
            worktree=worktree,
            ticket_id=TICKET,
            session_id=SID,
            task_updates=updates,
        )
        if plan is not None:
            (worktree / ".cw").mkdir(exist_ok=True)
            (worktree / ".cw" / "plan.md").write_text(plan, encoding="utf-8")
        head = git_in(worktree, "rev-parse", "HEAD")
        stub = install_home_prep_pr_state(tmp_path / "gate-state", *gates)
        arm = stub_prep_pr_finalize(Path.home())
        gh = FakeGhCli().install(monkeypatch)
        _script_default_gh(gh, head)
        h = Harness(
            worktree=worktree,
            origin=origin,
            runner=ScriptedCodexRunner(),
            gh=gh,
            arm=arm,
            gates=stub,
            head=head,
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr("cw.tracker_post._read_back", lambda *_a, **_k: None)
        h.set_cw("review converge config", _BLOCKING)
        h.set_cw("dev-queue adopted-prs", GhReply(0, "", ""))
        return h

    return _build


def _events(types: list[OrchestratorEventType]) -> list[tuple[str, dict[str, Any]]]:
    return [(e.type.value, e.payload) for e in read_events(event_types=types)]


def stage_events() -> list[tuple[str, dict[str, Any]]]:
    return _events(_FENCE_EVENTS)


def comment_events() -> list[tuple[str, dict[str, Any]]]:
    return _events(_COMMENT_EVENTS)


def _assert_block(
    result: AutoDevResult,
    reason: str,
    *,
    stage: str = "stage4a_merge_gate",
    blocker_stage: str | None = None,
) -> None:
    assert result.status == "blocked"
    assert result.stage_reached == stage
    assert result.blocker is not None
    assert result.blocker.reason == reason
    assert result.blocker.stage == (blocker_stage or stage)
    assert result.next_actions == _CODEX_FINALIZE_BLOCKED_NEXT_ACTIONS
    errored = [e for e in stage_events() if e[0] == "stage.errored"]
    assert len(errored) == 1
    assert errored[0][1]["error_kind"] == reason
    assert errored[0][1]["stage"] == "s4_pr_created"
    assert errored[0][1]["prev_stage"] == "s3_review_complete"
    assert errored[0][1]["ticket_id"] == TICKET


def _assert_nothing_created(h: Harness, tip_before: str | None = None) -> None:
    assert h.created() == []
    assert h.arm.calls() == []
    if tip_before is not None:
        assert h.origin_tip() == tip_before


# --- AC1: the create path ------------------------------------------------------


def test_create_path_creates_arms_through_the_script_and_ships(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_title=TITLE, pr_body=BODY))
    head = h.head
    result = h.run()
    assert result.status == "shipped"
    assert result.stage_reached == "stage5_post_create"
    assert result.pr is not None
    assert (result.pr.number, result.pr.url) == (PR_NUMBER, PR_URL)
    assert result.pr.auto_merge is True
    assert result.pr.base == "main"
    assert result.next_actions == ["wait_for_ci"]
    assert result.branch == BRANCH
    assert result.plan_source == "generated"
    assert result.scope.tier == "small"
    (call,) = h.created()
    assert call.argv[1:] == [
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        BRANCH,
        "--title",
        f"{TITLE} (#{TICKET})",
        "--body-file",
        call.argv[-1],
    ]
    assert "--draft" not in call.argv
    assert call.body_file == BODY
    assert [c[0] for c in h.arm.calls()] == ["check-automerge-allowed", "arm-automerge"]
    assert h.arm.calls()[1][1:] == [
        str(PR_NUMBER),
        "--repo-path",
        str(h.worktree),
        "--head-sha",
        head,
    ]
    assert not any(a[1:3] == ["pr", "merge"] for a in h.gh.argvs())
    assert h.local_head() == head
    assert git_in(h.worktree, "status", "--porcelain") == ""


def test_create_path_events_and_session_completion_in_order(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.run()
    events = stage_events()
    assert [e[0] for e in events] == [
        "stage.entered",
        "stage.entered",
        "stage.entered",
        "session.completed",
    ]
    entered, ci, done, completed = events
    assert (entered[1]["stage"], entered[1]["prev_stage"]) == (
        "s4_pr_created",
        "s3_review_complete",
    )
    assert (ci[1]["stage"], ci[1]["prev_stage"]) == ("s5_ci_waiting", "s4_pr_created")
    assert (done[1]["stage"], done[1]["prev_stage"]) == ("done", "s5_ci_waiting")
    assert completed[1]["session_id"] == SID
    assert completed[1]["ticket_id"] == TICKET
    for _name, payload in events[:3]:
        assert payload["ticket_id"] == TICKET
        assert payload["session_id"] == SID
        assert payload["started_at"].endswith("Z")


def test_the_fence_and_the_steps_run_in_the_documented_order_and_the_fence_twice(
    harness: Callable[..., Harness],
) -> None:
    h = harness().trace()
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    assert h.order == [
        "run_approval_fence",
        "run_finalize_quality_gates",
        "probe_default",
        "push_ahead",
        "turn",
        "create_pr_for",
        "run_approval_fence",
        "check_automerge_allowed",
        "arm",
    ]
    assert h.order.count("run_approval_fence") == 2


def test_the_create_path_gates_run_with_the_create_label_and_make_no_commit(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    result = h.run()
    assert "create_path_gates: passed" in result.friction_highlights
    assert h.gates.calls()[0] == "detect-gates"
    assert h.local_head() == h.head
    assert git_in(h.worktree, "status", "--porcelain") == ""


def test_the_friction_carries_the_fence_tokens_and_the_absent_script_notes(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    result = h.run()
    friction = result.friction_highlights
    assert "must_fix_operator_override" not in friction
    assert "check_must_fix_override: script absent, skipped" in friction
    assert "review_approval_check: REVIEW_APPROVAL_NOT_REQUIRED" in friction
    assert "check_merge_gate_overlap: script absent, skipped" in friction


def test_the_ahead_push_lands_before_the_pr_is_created(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    h.turn()
    stale_tip = h.origin_tip()
    assert stale_tip != h.local_head()
    result = h.run()
    assert result.status == "shipped"
    assert h.origin_tip() == h.local_head()


def test_the_prompt_carries_the_addendum_and_never_the_appendix_or_auto_dev(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.run()
    (call,) = h.turns
    stdin = str(call["stdin"])
    assert FINALIZE_TURN in stdin
    assert "Companion appendix to /auto-dev-finalize" not in stdin
    assert (
        'description: "Automated Linear→plan→implement→review→ship pipeline'
        not in stdin
    )
    assert "Add the widget." in stdin
    assert "branch: dev/42" in stdin


def test_the_turn_is_read_only_and_has_no_timeout(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.run()
    (call,) = h.turns
    argv = call["argv"]
    assert isinstance(argv, list)
    assert "read-only" in argv
    assert "--output-schema" in argv
    assert call["timeout"] is None
    assert call["cwd"] == h.worktree


def test_a_document_with_an_adjudication_section_lands_in_the_pr_body(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(
        finalize_document(
            pr_title=TITLE,
            pr_body=BODY,
            adjudication_section="## Review adjudication\n\nAll clear.\n",
        )
    )
    h.run()
    (call,) = h.created()
    assert (
        call.body_file == BODY.rstrip() + "\n\n## Review adjudication\n\nAll clear.\n"
    )


def test_a_free_text_title_gets_no_ticket_suffix(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_title="x" * 90))
    h.run()
    (call,) = h.created()
    title = call.argv[call.argv.index("--title") + 1]
    assert len(title) <= 72
    assert title.endswith(f"(#{TICKET})")


# --- AC1: the reuse and MERGED paths --------------------------------------------


def _set_head(h: Harness, **overrides: object) -> None:
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "headRefName,headRefOid,baseRefName,state,autoMergeRequest",
        stdout=json.dumps(
            {
                "headRefName": BRANCH,
                "headRefOid": h.head,
                "baseRefName": "main",
                "state": "OPEN",
                "autoMergeRequest": {"mergeMethod": "SQUASH"},
            }
            | overrides
        ),
    )


def test_open_pr_is_reused_with_the_reuse_gates_and_no_turn(
    harness: Callable[..., Harness],
) -> None:
    h = harness().trace()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.number == PR_NUMBER
    assert "reuse_path_gates: passed" in result.friction_highlights
    assert "create_path_gates: passed" not in result.friction_highlights
    assert h.runner.calls == []
    assert h.created() == []
    assert h.order == [
        "run_approval_fence",
        "run_finalize_quality_gates",
        "probe_default",
        "run_approval_fence",
        "check_automerge_allowed",
        "arm",
    ]
    assert not any(a[0:2] == ["gh", "pr"] and a[2] == "merge" for a in h.gh.argvs())


def test_a_reused_conflicting_pr_is_not_armed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    conflict = MergeProbe("conflict", "a" * 40, ("clash.py",), "")
    with patch("cw.codex_impl._feedback.probe_default_merge", return_value=conflict):
        result = h.run()
    _assert_block(result, "merge_conflict_post_push", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert "clash.py" in result.blocker.details
    assert h.arm.calls() == []


def test_the_reuse_path_skips_the_merge_gate_and_the_ahead_push(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    stale_tip = h.origin_tip()
    h.run()
    assert h.gh_argvs("pr", "list", "--state", "open") == []
    assert h.origin_tip() == stale_tip


def test_a_merged_pr_ships_without_gates_or_arm(
    harness: Callable[..., Harness],
) -> None:
    h = harness().trace()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("MERGED"))
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefOid="b" * 40),
    )
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.number == PR_NUMBER
    assert h.gates.calls() == []
    assert h.arm.calls() == []
    assert h.runner.calls == []
    assert h.order == []


def test_a_closed_pr_blocks_pr_already_terminal(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("CLOSED"))
    result = h.run()
    _assert_block(result, "pr_already_terminal")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    _assert_nothing_created(h)


def test_a_pr_lookup_failure_is_operator_unavailable(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, returncode=1, stderr="HTTP 502")
    result = h.run()
    _assert_block(result, "operator_unavailable")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    _assert_nothing_created(h)


def test_the_detector_guard_looks_up_the_recorded_branch_never_the_checkout(
    harness: Callable[..., Harness],
) -> None:
    h = harness(branch="dev/custom")
    h.gh.reply("pr", "list", "--head", "dev/custom", stdout="[]")
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefName="dev/custom", headRefOid=h.head),
    )
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    assert result.branch == "dev/custom"
    (listing,) = h.gh_argvs("pr", "list", "--head")
    assert listing[4] == "dev/custom"
    (call,) = h.created()
    assert call.argv[call.argv.index("--head") + 1] == "dev/custom"


def test_the_reuse_path_re_checks_the_fence_and_parks_before_any_gate(
    harness: Callable[..., Harness],
) -> None:
    h = harness(plan=LARGE_PLAN)
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.set_cw("review check-approval", _PARK_MISSING)
    result = h.run()
    assert result.status == "review_pending_approval"
    assert result.scope.tier == "large"
    assert h.gates.calls() == []
    assert h.arm.calls() == []


def test_the_reuse_path_with_an_override_edits_the_operator_override_section(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "body",
        stdout=json.dumps({"body": "Intro\n\n## Operator override\n\nold\n"}),
    )
    _override_script(
        0, json.dumps({"status": "overridden", "reason": "operator said so"})
    )
    h.turn(finalize_document(override_section="## Operator override\n\nWaived: ok.\n"))
    result = h.run()
    assert result.status == "shipped"
    assert "must_fix_operator_override" in result.friction_highlights
    (turn,) = h.turns
    assert "operator said so" in str(turn["stdin"])
    (edit,) = [r for r in h.gh.records if r.argv[1:3] == ["pr", "edit"]]
    assert edit.body_file is not None
    assert "Waived: ok." in edit.body_file
    assert "old" not in edit.body_file
    assert h.created() == []


def test_the_create_path_with_an_override_puts_the_section_in_the_body(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _override_script(0, json.dumps({"status": "overridden", "reason": "r"}))
    h.turn(
        finalize_document(
            pr_body=BODY,
            override_section="## Operator override\n\nWaived: ok.\n",
        )
    )
    h.run()
    (call,) = h.created()
    assert call.body_file is not None
    assert call.body_file.count("## Operator override") == 1
    assert "Waived: ok." in call.body_file


def _override_script(code: int, stdout: str = "") -> Path:
    path = Path.home() / ".claude" / "scripts" / "check_must_fix_override.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n# cw-script-version: 1\nimport sys\n"
        f"sys.stdout.write({stdout!r})\nsys.exit({code})\n",
        encoding="utf-8",
    )
    return path


# --- AC1/AC2: the live bind ------------------------------------------------------


def test_a_bind_with_a_moved_head_blocks_before_gates_on_reuse(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefOid="c" * 40),
    )
    result = h.run()
    _assert_block(result, "external_state_block", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert h.gates.calls() == []
    assert h.arm.calls() == []


def test_a_foreign_head_branch_on_the_live_pr_is_not_retryable(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefName="dev/other", headRefOid=h.head),
    )
    result = h.run()
    _assert_block(result, "external_state_block", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    assert h.arm.calls() == []


def test_a_created_pr_whose_head_is_not_ours_blocks_before_the_arm(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "number,url,headRefName,headRefOid",
        stdout=_view(headRefOid="d" * 40),
    )
    result = h.run()
    _assert_block(result, "external_state_block", stage="stage5_post_create")
    assert len(h.created()) == 1
    assert h.arm.calls() == []


# --- AC2: override, 4.0, 4.0b and the re-fence ---------------------------------


def test_a_must_fix_override_refusal_parks_before_anything_is_touched(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    tip = h.origin_tip()
    _override_script(1, "1 MUST_FIX finding unresolved")
    result = h.run()
    _assert_block(
        result, "codex_must_fix_findings", blocker_stage="stage4_must_fix_override"
    )
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    assert h.gh.calls == []
    assert h.runner.calls == []
    _assert_nothing_created(h, tip)


def test_the_override_refusal_keeps_the_doc_blocker_stage_label(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _override_script(1, "findings")
    h.run()
    blocker = h.raw()["blocker"]
    assert blocker["reason"] == "codex_must_fix_findings"
    assert blocker["stage"] == "stage4_must_fix_override"


def test_an_override_tooling_failure_is_agent_block(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _override_script(2, "")
    result = h.run()
    _assert_block(result, "agent_block")
    assert h.gh.calls == []


def test_large_tier_without_approval_parks_review_pending_approval(
    harness: Callable[..., Harness],
) -> None:
    h = harness(plan=LARGE_PLAN, unpushed=1)
    tip = h.origin_tip()
    h.set_cw("review check-approval", _PARK_MISSING)
    result = h.run()
    assert result.status == "review_pending_approval"
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.scope.tier == "large"
    assert result.pr is None
    assert result.blocker is None
    assert result.next_actions == ["requeue_stage_review"]
    assert any(
        f.startswith("review_approval_park: missing")
        for f in result.friction_highlights
    )
    assert h.runner.calls == []
    _assert_nothing_created(h, tip)
    assert h.gates.calls() == []


def test_large_tier_with_a_recorded_approval_proceeds(
    harness: Callable[..., Harness],
) -> None:
    h = harness(plan=LARGE_PLAN)
    h.set_cw("review check-approval", GhReply(0, "REVIEW_APPROVAL_RECORDED\n", ""))
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    assert result.scope.tier == "large"
    assert (
        "review_approval_check: REVIEW_APPROVAL_RECORDED" in result.friction_highlights
    )


def test_a_stale_clean_mode_tip_parks_without_creating_anything(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    tip = h.origin_tip()
    h.set_cw("review converge config", _CLEAN)
    h.set_cw(
        "review converge verify-tip",
        GhReply(1, json.dumps({"reason": "commits_after_clean_round"}), ""),
    )
    result = h.run()
    assert result.status == "review_pending_approval"
    assert result.scope.tier == "small"
    assert result.friction_highlights == [
        "review_approval_check: REVIEW_APPROVAL_NOT_REQUIRED",
        "review_tip_check: stale (commits_after_clean_round)",
    ]
    _assert_nothing_created(h, tip)


def test_a_fetch_failed_tip_parks_for_a_finalize_requeue(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.set_cw("review converge config", _CLEAN)
    h.set_cw(
        "review converge verify-tip",
        GhReply(1, json.dumps({"reason": "tip_check_fetch_failed"}), ""),
    )
    result = h.run()
    assert result.status == "review_pending_approval"
    assert result.next_actions == ["requeue_stage_finalize"]


def test_the_re_fence_parks_after_the_pr_exists_and_never_arms(
    harness: Callable[..., Harness],
) -> None:
    h = harness().trace()
    h.turn()
    h.set_cw("review converge config", _CLEAN)
    h.set_cw(
        "review converge verify-tip",
        [
            GhReply(0, "{}", ""),
            GhReply(1, json.dumps({"reason": "commits_after_clean_round"}), ""),
        ],
    )
    result = h.run()
    assert result.status == "review_pending_approval"
    # The harness turn reports no usage, so the unavailable token follows it;
    # assert on the specific token, not its position (#3063).
    assert "review_tip_check: stale (commits_after_clean_round)" in (
        result.friction_highlights
    )
    assert len(h.created()) == 1
    assert h.arm.calls() == []
    assert h.order.count("run_approval_fence") == 2
    assert "check_automerge_allowed" not in h.order


def test_a_scope_tier_that_cannot_be_resolved_blocks_never_guesses(
    harness: Callable[..., Harness],
) -> None:
    h = harness(plan=None)
    with patch("cw.codex_finalize._gates.resolve_review_scope_tier", return_value=None):
        result = h.run()
    _assert_block(result, "scope_tier_unresolvable")
    assert h.gh.calls == []


# --- checked-out branch ---------------------------------------------------------


def test_a_worktree_on_the_wrong_branch_blocks_before_the_detector_guard(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    tip = h.origin_tip()
    git_in(h.worktree, "checkout", "-q", "-b", "dev/elsewhere")
    result = h.run()
    _assert_block(result, "external_state_block")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    assert "dev/elsewhere" in result.blocker.details
    assert h.gh.calls == []
    _assert_nothing_created(h, tip)


def test_a_detached_head_blocks_the_same_way(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    git_in(h.worktree, "checkout", "-q", "--detach")
    result = h.run()
    _assert_block(result, "external_state_block")
    assert h.gh.calls == []


# --- create-path quality gates --------------------------------------------------


def test_a_red_gate_on_the_create_path_pushes_and_creates_nothing(
    harness: Callable[..., Harness],
) -> None:
    h = harness(gates=(("lint", "echo boom; exit 1", 5.0),), unpushed=1)
    tip = h.origin_tip()
    result = h.run()
    _assert_block(result, "agent_block", stage="stage4b_pr_create")
    assert result.blocker is not None
    assert "create-path quality gate `lint`" in result.blocker.details
    assert h.origin_tip() == tip
    assert h.created() == []
    assert h.arm.calls() == []
    assert h.runner.calls == []
    assert h.local_head() != tip


def test_a_red_gate_on_the_reuse_path_never_arms(
    harness: Callable[..., Harness],
) -> None:
    h = harness(gates=(("lint", "exit 1", 5.0),))
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    result = h.run()
    _assert_block(result, "agent_block", stage="stage5_post_create")
    assert "reuse-path quality gate" in str(result.blocker)
    assert h.arm.calls() == []


def test_the_default_branch_conflict_blocks_before_the_push(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    tip = h.origin_tip()
    conflict = MergeProbe("conflict", "a" * 40, ("clash.py",), "")
    with patch("cw.codex_impl._feedback.probe_default_merge", return_value=conflict):
        result = h.run()
    _assert_block(result, "merge_conflict_post_push", stage="stage4b_pr_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False
    assert "clash.py" in result.blocker.details
    assert h.origin_tip() == tip
    assert h.runner.calls == []


def test_an_unavailable_probe_is_friction_and_the_run_continues(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    unavailable = MergeProbe("unavailable", None, (), "merge-tree broke")
    with patch("cw.codex_impl._feedback.probe_default_merge", return_value=unavailable):
        result = h.run()
    assert result.status == "shipped"
    assert any(
        f.startswith("merge_probe_unavailable") for f in result.friction_highlights
    )


def test_a_failed_ahead_push_is_impl_not_pushed(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    git_in(h.worktree, "remote", "set-url", "origin", str(h.worktree / "nowhere"))
    result = h.run()
    _assert_block(result, "impl_not_pushed", stage="stage4b_pr_create")
    assert h.created() == []
    assert h.runner.calls == []


# --- the merge gate -------------------------------------------------------------


def test_the_merge_gate_runs_on_the_create_path_only_and_can_be_disabled(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    with patch(
        "cw.codex_finalize._run._context",
        return_value={"merge_gate_enabled": False},
    ):
        result = h.run()
    assert result.status == "shipped"
    assert (
        "MERGE_GATE_DISABLED: merge gate disabled by client config"
        in result.friction_highlights
    )
    assert h.gh_argvs("pr", "list", "--state", "open") == []


def test_a_conflicting_sibling_pr_parks_merge_gate_blocked(
    harness: Callable[..., Harness],
) -> None:
    h = harness(unpushed=1)
    tip = h.origin_tip()
    h.gh.reply(
        "pr",
        "list",
        "--state",
        "open",
        "--author",
        "@me",
        stdout=json.dumps([{"number": 9, "headRefName": "dev/other"}]),
    )
    h.gh.reply("pr", "diff", "9", stdout="feature.py\n")
    with patch("cw.codex_finalize._merge_gate._merge_tree", return_value="1"):
        result = h.run()
    assert result.status == "merge_gate_blocked"
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.blocker is not None
    assert result.blocker.reason == "prior_pipeline_pr_open"
    assert result.blocker.retry_eligible is True
    assert "PR #9 (dev/other)" in result.blocker.details
    errored = [e for e in stage_events() if e[0] == "stage.errored"]
    assert [e[1]["error_kind"] for e in errored] == ["prior_pipeline_pr_open"]
    _assert_nothing_created(h, tip)
    assert h.gates.calls() == []


def test_a_failed_sibling_inventory_fails_closed_as_agent_block(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--state", "open", returncode=1)
    result = h.run()
    _assert_block(result, "agent_block")
    assert h.created() == []


# --- gh pr create / mergeability -------------------------------------------------


def test_a_failed_pr_create_is_retryable_operator_unavailable(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply("pr", "create", returncode=1, stderr="HTTP 502")
    result = h.run()
    _assert_block(result, "operator_unavailable", stage="stage4b_pr_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert h.arm.calls() == []


def test_a_conflicting_pr_after_create_blocks_before_the_re_fence(
    harness: Callable[..., Harness],
) -> None:
    h = harness().trace()
    h.turn()
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "mergeable,mergeStateStatus",
        stdout=json.dumps({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}),
    )
    result = h.run()
    _assert_block(result, "merge_conflict_post_push", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert h.order.count("run_approval_fence") == 1
    assert h.arm.calls() == []


def test_a_behind_pr_ships_with_the_behind_friction(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "mergeable,mergeStateStatus",
        stdout=json.dumps({"mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}),
    )
    result = h.run()
    assert result.status == "shipped"
    assert "behind_default_no_action" in result.friction_highlights


# --- the turn -------------------------------------------------------------------


def test_a_missing_document_is_no_result_emitted(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(
        result=CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content=None
        )
    )
    result = h.run()
    _assert_block(result, "no_result_emitted", stage="stage4b_pr_create")
    assert h.created() == []


def test_an_empty_title_is_validation_failed(harness: Callable[..., Harness]) -> None:
    h = harness()
    h.turn(finalize_document(pr_title="  "))
    result = h.run()
    _assert_block(result, "validation_failed", stage="stage4b_pr_create")


def test_model_capacity_is_retry_eligible(harness: Callable[..., Harness]) -> None:
    h = harness()
    capacity = CodexRunResult(
        returncode=1,
        stdout=(
            '{"type":"turn.failed","error":{"message":'
            '"Selected model is at capacity. Please try a different model."}}\n'
        ),
        stderr="",
    )
    h.turn(result=capacity)
    result = h.run()
    _assert_block(result, "codex_model_capacity", stage="stage4b_pr_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert h.created() == []


def test_a_turn_failure_is_codex_error(harness: Callable[..., Harness]) -> None:
    h = harness()
    h.turn(result=CodexRunResult(returncode=2, stdout="", stderr="exploded"))
    result = h.run()
    _assert_block(result, "codex_error", stage="stage4b_pr_create")
    assert result.blocker is not None
    assert "exploded" in result.blocker.details


def test_docs_root_missing_blocks_plan_missing_context(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    with patch("cw.codex_finalize._run.command_docs_root", return_value=None):
        result = h.run()
    _assert_block(result, "plan_missing_context", stage="stage4b_pr_create")
    assert h.runner.calls == []
    assert h.created() == []


def test_a_render_error_is_plan_missing_context(
    harness: Callable[..., Harness],
) -> None:
    from cw.contract_manifest import RenderError

    h = harness()
    with patch(
        "cw.codex_finalize._run.render_finalize_prompt",
        side_effect=RenderError("bad doc"),
    ):
        result = h.run()
    _assert_block(result, "plan_missing_context", stage="stage4b_pr_create")
    assert "bad doc" in str(result.blocker)
    assert h.runner.calls == []


# --- the arm and the verification (AC1, AC5) -----------------------------------


def test_auto_merge_disallowed_ships_unarmed_with_no_arm_call(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.arm = stub_prep_pr_finalize(Path.home(), check_exit=1)
    _set_head(h, autoMergeRequest=None)
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is False
    assert [c[0] for c in h.arm.calls()] == ["check-automerge-allowed"]


def test_an_arm_exit_three_ships_unarmed(harness: Callable[..., Harness]) -> None:
    h = harness()
    h.turn()
    h.arm = stub_prep_pr_finalize(Path.home(), arm_exit=3)
    _set_head(h, autoMergeRequest=None)
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is False


def test_an_arm_refusal_blocks_automerge_not_armed_with_pr_info(
    harness: Callable[..., Harness],
) -> None:
    from cw.codex_stage_completion import complete_session_through_door as real

    h = harness()
    h.turn()
    h.arm = stub_prep_pr_finalize(
        Path.home(), arm_exit=1, arm_gh_stderr="refusing: .github/workflows"
    )
    handed: list[dict[str, Any]] = []

    def _spy(sid: str, payload: dict[str, Any], **kwargs: Any) -> None:
        handed.append(payload)
        real(sid, payload, **kwargs)

    with patch("cw.codex_finalize._run.complete_session_through_door", _spy):
        result = h.run()
    _assert_block(result, "automerge_not_armed", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert "refusing: .github/workflows" in result.blocker.details
    (payload,) = handed
    assert payload["pr_info"] == {"number": PR_NUMBER, "url": PR_URL}
    assert payload["pr"] is None


def test_an_invocation_error_in_the_check_is_not_retryable(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.arm = stub_prep_pr_finalize(Path.home(), check_exit=2)
    result = h.run()
    _assert_block(result, "automerge_not_armed", stage="stage5_post_create")
    assert result.blocker is not None
    assert result.blocker.retry_eligible is False


def test_a_shipped_claim_whose_pr_state_is_unreadable_becomes_blocked(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        "headRefName,headRefOid,baseRefName,state,autoMergeRequest",
        returncode=1,
    )
    result = h.run()
    _assert_block(result, "operator_unavailable", stage="stage5_post_create")
    assert result.pr is None
    assert [e[0] for e in stage_events() if e[0] == "stage.entered"] == [
        "stage.entered"
    ]


def test_a_shipped_claim_whose_ci_is_unverifiable_becomes_blocked(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply("pr", "checks", PR_URL, returncode=4, stderr="HTTP 502")
    result = h.run()
    _assert_block(result, "operator_unavailable", stage="stage5_post_create")
    assert "CI unverifiable" in str(result.blocker)
    assert result.pr is None


def test_failing_required_checks_ship_with_friction(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply(
        "pr",
        "checks",
        PR_URL,
        returncode=1,
        stdout=json.dumps(
            [{"name": "unit", "workflow": "ci", "bucket": "fail", "link": ""}]
        ),
    )
    result = h.run()
    assert result.status == "shipped"
    assert "required_checks_failing_at_emission: unit" in result.friction_highlights


def test_a_head_that_moved_after_the_arm_becomes_blocked(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    _set_head(h, headRefOid="e" * 40)
    result = h.run()
    _assert_block(result, "external_state_block", stage="stage5_post_create")
    assert result.pr is None


def test_an_unarmed_pr_that_cw_claimed_armed_becomes_blocked(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    _set_head(h, autoMergeRequest=None)
    result = h.run()
    _assert_block(result, "automerge_not_armed", stage="stage5_post_create")


# --- the reuse-path override edit failures ---------------------------------------


@pytest.mark.parametrize(
    ("reply", "needle"),
    [
        (("pr", "view", PR_URL, "--json", "body"), "could not read the PR body"),
        (("pr", "edit"), "could not write the operator override section"),
    ],
)
def test_an_override_edit_failure_blocks_without_arming(
    harness: Callable[..., Harness], reply: tuple[str, ...], needle: str
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.gh.reply("pr", "view", PR_URL, "--json", "body", stdout='{"body": "x"}')
    _override_script(0, json.dumps({"status": "overridden"}))
    h.turn()
    h.gh.reply(*reply, returncode=1)
    result = h.run()
    _assert_block(result, "operator_unavailable", stage="stage5_post_create")
    assert needle in str(result.blocker)
    assert h.arm.calls() == []


def test_an_override_turn_failure_on_reuse_blocks(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    _override_script(0, json.dumps({"status": "overridden"}))
    h.turn(result=CodexRunResult(returncode=2, stdout="", stderr="x"))
    result = h.run()
    _assert_block(result, "codex_error", stage="stage5_post_create")
    assert h.arm.calls() == []


# --- AC4: the PR-link comment ---------------------------------------------------


def test_the_pr_link_is_posted_to_a_github_ticket_after_the_door(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    (post,) = [r for r in h.gh.records if r.argv[1:3] == ["issue", "comment"]]
    assert post.argv[3] == TICKET
    assert PR_URL in str(post.stdin)
    (event,) = comment_events()
    assert event[1]["kind"] == "pr_link"
    assert event[1]["reviewed_sha"] is None
    assert event[1]["artifact_path"] is None


def test_the_comment_is_posted_after_the_session_is_completed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    seen: list[str] = []

    def _watch(*_a: object, **_k: object) -> None:
        session = load_state().sessions[0]
        seen.append("persisted" if session.last_result else "pending")

    with patch("cw.codex_finalize._run.post_pr_link_comment", side_effect=_watch):
        h.run()
    assert seen == ["persisted"]


def test_a_blocked_run_posts_no_comment(harness: Callable[..., Harness]) -> None:
    h = harness()
    _override_script(1, "x")
    h.run()
    assert [r for r in h.gh.records if r.argv[1:3] == ["issue", "comment"]] == []
    assert comment_events() == []


def test_a_free_text_ticket_skips_the_comment_with_a_reason(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={"plan_source": "free_text"})
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    assert result.plan_source == "free_text"
    assert [r for r in h.gh.records if r.argv[1:3] == ["issue", "comment"]] == []
    (event,) = comment_events()
    assert event[0] == "review.verdict_comment_skipped"
    assert event[1]["reason"] == "free_text_ticket"
    assert event[1]["kind"] == "pr_link"


def test_an_absent_plan_source_is_friction_and_the_run_still_ships(
    harness: Callable[..., Harness],
) -> None:
    h = harness(plan=None, task_updates={"plan_source": None, "scope_hint": "small"})
    h.turn()
    result = h.run()
    assert result.status == "shipped"
    assert result.plan_source == "none"
    assert any(
        f.startswith("codex_plan_source_none") for f in result.friction_highlights
    )


def _linear_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worktree: Path, state: str
) -> ClientConfig:
    client = codex_tracker_client(tmp_path, {"codex": "linear"})
    _write_tracker_config(client.workspace_path, TRACKER_LINEAR)
    stage_mcp_config(monkeypatch, tmp_path, "codex", state)
    return client.model_copy(update={"name": "test"})


def test_a_non_github_tracker_posts_the_link_through_the_a2_turn(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    h.client = _linear_client(tmp_path, monkeypatch, h.worktree, "enabled")
    h.turn()
    parts = plan_turn_parts(
        f"PR: {PR_URL}\n",
        TrackerIoContext(runner=h.runner, client=h.client, worktree=h.worktree),
    )
    h.runner.add(
        any_call,
        CodexRunResult(
            returncode=0,
            stdout="",
            stderr="",
            output_file_content=tracker_io_turn_result(TICKET, parts),
        ),
    )
    result = h.run()
    assert result.status == "shipped"
    assert [r for r in h.gh.records if r.argv[1:3] == ["issue", "comment"]] == []
    assert h.gh_argvs("issue", "view") == []
    (event,) = comment_events()
    assert event[0] == "review.verdict_comment_posted"
    assert event[1]["kind"] == "pr_link"
    assert event[1]["tracker"] == TRACKER_LINEAR
    assert len(h.runner.calls) == 2


def test_a_non_github_tracker_with_a_disabled_server_parks_pre_dispatch(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    h.client = _linear_client(tmp_path, monkeypatch, h.worktree, "disabled")
    result = h.run()
    _assert_block(result, "tracker_mcp_unavailable_pre_dispatch")
    assert h.gh.calls == []
    assert h.runner.calls == []


def test_a_non_github_tracker_with_no_server_parks_pre_dispatch(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    client = codex_tracker_client(tmp_path, None)
    h.client = client.model_copy(update={"name": "test"})
    _write_tracker_config(client.workspace_path, TRACKER_LINEAR)
    result = h.run()
    _assert_block(result, "tracker_mcp_unavailable_pre_dispatch")
    assert h.runner.calls == []


# --- the door and the guard -----------------------------------------------------


def test_a_transient_door_failure_is_retried_so_an_armed_pr_is_not_stranded(
    harness: Callable[..., Harness],
) -> None:
    from cw.codex_stage_completion import complete_session_through_door as real

    h = harness()
    h.turn()
    attempts: list[int] = []

    def _flaky(sid: str, payload: dict[str, Any], **kwargs: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(_TRANSIENT)
        real(sid, payload, **kwargs)

    with patch("cw.codex_finalize._run.complete_session_through_door", _flaky):
        result = h.run()
    assert len(attempts) == 2
    assert result.status == "shipped"


def test_a_stage_fault_completes_as_unexpected_error_and_requeues_the_row(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    with patch(
        "cw.codex_finalize._run.detect_branch_pr",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        result = h.run()
    assert result.status == "blocked"
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.next_actions == _CODEX_FINALIZE_BLOCKED_NEXT_ACTIONS
    (task,) = load_dev_queue().tasks
    assert task.status is QueueItemStatus.PENDING


def test_the_scratch_directory_is_removed_after_the_run(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    scratch: list[Path] = []
    real = Path.write_text

    def _spy(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "pr-body.md":
            scratch.append(self.parent)
        return real(self, *args, **kwargs)

    with patch.object(Path, "write_text", _spy):
        h.run()
    assert scratch
    assert not scratch[0].exists()


class TestContext:
    """``_run._context``: the cw-context.json reader never raises."""

    def test_absent_is_empty(self, tmp_path: Path) -> None:
        assert _context(tmp_path) == {}

    @pytest.mark.parametrize("text", ["not json", "[1]", '"x"', ""])
    def test_malformed_or_non_object_is_empty(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / ".claude" / "cw-context.json"
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
        assert _context(tmp_path) == {}

    def test_an_object_is_returned(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "cw-context.json"
        path.parent.mkdir()
        path.write_text('{"merge_gate_enabled": false}', encoding="utf-8")
        assert _context(tmp_path) == {"merge_gate_enabled": False}

    def test_undecodable_bytes_are_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "cw-context.json"
        path.parent.mkdir()
        path.write_bytes(b"\xff\xfe")
        assert _context(tmp_path) == {}


_PRICE = ModelPrice(
    input_usd_per_mtok=1.0, cached_input_usd_per_mtok=0.5, output_usd_per_mtok=2.0
)


def test_recorded_usage_is_persisted_for_the_finalize_turn(
    harness: Callable[..., Harness],
) -> None:
    """#3063: the FINALIZE turn records under the configured model and prices it."""
    h = harness()
    reply = replace(
        finalize_turn_result(finalize_document(pr_title=TITLE, pr_body=BODY)),
        stdout=codex_usage_stdout(input_tokens=1000, output_tokens=100),
    )
    h.turn(result=reply)
    config = StageExecutorConfig(
        backend="codex", model="gpt-x", model_prices={"gpt-x": _PRICE}
    )
    with patch("cw.codex_driver.resolve_executor_config", return_value=config):
        result = h.run()

    assert result.status == "shipped"
    session = load_state().sessions[0]
    assert session.usage is not None
    assert session.usage.state is UsageState.RECORDED
    assert list(session.usage.by_stage) == [UsageStage.FINALIZE]
    assert list(session.usage.by_model) == ["gpt-x"]
    assert session.cost_breakdown == pytest.approx({"gpt-x": 0.0012})
    assert not any(
        f.startswith("usage_unavailable") for f in result.friction_highlights
    )


def test_a_finalize_turn_without_usage_is_unavailable_with_a_friction_token(
    harness: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = harness()
    h.turn(finalize_document(pr_title=TITLE, pr_body=BODY))
    with caplog.at_level("WARNING", logger="cw.codex_usage"):
        result = h.run()

    session = load_state().sessions[0]
    assert session.usage is not None
    assert session.usage.state is UsageState.UNAVAILABLE
    assert session.usage.unavailable_reason is UsageUnavailableReason.NO_OUTPUT
    assert [m for m in caplog.messages if "role=finalize " in m]
    assert any(f.startswith("usage_unavailable: ") for f in result.friction_highlights)
