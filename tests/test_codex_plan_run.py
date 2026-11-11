"""Tests for cw.codex_plan._run: the codex PLAN stage end to end (#3060).

Every run goes through the ``cw codex run --stage plan`` driver core
(``run_codex_plan_stage``) against a ``ScriptedCodexRunner`` and a scripted
``gh``: no real ``codex`` or ``gh`` runs, and the worktree is a ``tmp_path``
git repo with ``.cw/`` excluded.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from cw.auto_dev_result import AutoDevResult
from cw.codex_driver import run_codex_plan_stage
from cw.codex_plan._result import PLAN_MD_RELATIVE_PATH
from cw.codex_plan._stations import PLAN_REVIEWER, PLAN_SOUNDNESS_REVIEWER
from cw.codex_review import _CODEX_PLAN_BLOCKED_NEXT_ACTIONS
from cw.codex_runner import CodexRunResult
from cw.config import load_state, state_dir
from cw.contract_manifest import RenderError
from cw.dev_queue import load_dev_queue
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.events import read_events
from cw.exceptions import CwError
from cw.local_runner import UNEXPECTED_ERROR
from cw.models import ClientConfig, OrchestratorEventType, QueueItemStatus, Stage
from cw.plan_fingerprint import PLAN_DRAFT_RELATIVE_PATH
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR
from tests._backend_mcp_helpers import (
    McpStage,
    codex_tracker_client,
    stage_mcp_config,
)
from tests._codex_review_helpers import exclude_cw_dir, seed_codex_stage_run
from tests._tool_fakes import (
    FakeGhCli,
    ScriptedCodexRunner,
    plan_sentinel,
    plan_turn_result,
    station_findings_doc,
    stdin_contains,
)
from tests.conftest import _write_tracker_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

TICKET = "42"
SID = "sess-1"
NOW = "2026-11-10 12:00:00"
STARTED_AT = "2026-11-10T12:00:00Z"
PLAN_TURN = "Codex PLAN addendum"
OPERATOR_MARKER = "<!-- auto-dev-preflight-resolutions -->"

SMALL_PLAN = (
    "## Summary\n\nAdd the widget.\n\n**Scope tier:** small\n\n"
    "## Files Modified\n\n- src/cw/widget.py\n\n"
    "## Adopted Assumptions\n\n1. **Widgets are blue.** Adopted.\n"
)
LARGE_PLAN = SMALL_PLAN.replace("small", "large")

_STAGE_EVENTS = [
    OrchestratorEventType.STAGE_ENTERED,
    OrchestratorEventType.STAGE_ERRORED,
    OrchestratorEventType.SESSION_COMPLETED,
]


@dataclass
class Harness:
    """One seeded PLAN run: worktree, scripted codex and gh, and the client."""

    worktree: Path
    runner: ScriptedCodexRunner
    gh: FakeGhCli
    client: ClientConfig | None = None
    comments: list[dict[str, object]] = field(default_factory=list)

    def run(self) -> AutoDevResult:
        if self.client is None:
            run_codex_plan_stage(
                ticket_id=TICKET,
                session_id=SID,
                wall_clock_budget_seconds=None,
                runner=self.runner,
            )
        else:
            with patch("cw.codex_driver.get_client", return_value=self.client):
                run_codex_plan_stage(
                    ticket_id=TICKET,
                    session_id=SID,
                    wall_clock_budget_seconds=None,
                    runner=self.runner,
                )
        return persisted_result()

    @property
    def plan_calls(self) -> list[dict[str, object]]:
        return [c for c in self.runner.calls if PLAN_TURN in str(c["stdin"])]

    def posts(self) -> list[Any]:
        """Every ``gh issue comment`` launch, in order."""
        return [r for r in self.gh.records if list(r.argv[1:3]) == ["issue", "comment"]]

    def plan_md(self) -> str:
        return (self.worktree / PLAN_MD_RELATIVE_PATH).read_text(encoding="utf-8")

    def draft_path(self) -> Path:
        return self.worktree / PLAN_DRAFT_RELATIVE_PATH


def persisted_result() -> AutoDevResult:
    session = load_state().sessions[0]
    assert session.last_result is not None
    return AutoDevResult.model_validate(session.last_result)


def stage_events() -> list[tuple[str, dict[str, Any], str | None]]:
    return [
        (e.type.value, e.payload, e.correlation_id)
        for e in read_events(event_types=_STAGE_EVENTS)
    ]


def script_gh(
    gh: FakeGhCli, comments: list[dict[str, object]] | None = None
) -> FakeGhCli:
    gh.reply("issue", "view", TICKET, "--json", "body", stdout='{"body": "BODY"}')
    gh.reply(
        "issue",
        "view",
        TICKET,
        "--json",
        "comments",
        stdout=json.dumps({"comments": comments or []}),
    )
    gh.reply(
        "issue",
        "comment",
        stdout="https://github.com/o/r/issues/42#issuecomment-9\n",
    )
    return gh


def add_turn(
    runner: ScriptedCodexRunner,
    plan: str = SMALL_PLAN,
    *,
    tier: str = "small",
    times: int | None = None,
    **sentinel: Any,
) -> ScriptedCodexRunner:
    return runner.add(
        stdin_contains(PLAN_TURN),
        plan_turn_result(plan, plan_sentinel(tier=tier, ticket_id=TICKET, **sentinel)),
        times=times,
    )


def add_stations(
    runner: ScriptedCodexRunner,
    *,
    reviewer: CodexRunResult | None = None,
    soundness: CodexRunResult | None = None,
) -> ScriptedCodexRunner:
    runner.add(
        stdin_contains(f"You are the {PLAN_SOUNDNESS_REVIEWER}"),
        soundness or station_findings_doc(PLAN_SOUNDNESS_REVIEWER),
    )
    return runner.add(
        stdin_contains(f"You are the {PLAN_REVIEWER}"),
        reviewer or station_findings_doc(PLAN_REVIEWER),
    )


@pytest.fixture
def harness(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Harness]:
    """Build a seeded PLAN run; *task_updates* reach the row."""

    def _build(
        *,
        task_updates: Mapping[str, object] | None = None,
        comments: list[dict[str, object]] | None = None,
    ) -> Harness:
        worktree = make_git_repo("wt-plan")
        exclude_cw_dir(worktree)
        seed_codex_stage_run(
            worktree=worktree,
            stage=Stage.PLAN,
            ticket_id=TICKET,
            session_id=SID,
            task_updates=task_updates,
        )
        gh = script_gh(FakeGhCli().install(monkeypatch), comments)
        return Harness(worktree=worktree, runner=ScriptedCodexRunner(), gh=gh)

    del tmp_config_dir
    return _build


# --- completing and pass-through exits ---------------------------------------


@freeze_time(NOW)
def test_small_stage_complete_writes_the_marked_plan(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    result = h.run()
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage1_plan"
    assert result.ticket_id == TICKET
    plan = h.plan_md()
    assert _plan_body_signoff_ok(plan)
    assert plan.splitlines()[0] == "<!-- plan-spec-reviewed: 2026-11-10 v2 -->"
    assert "Add the widget." in plan
    assert not h.draft_path().exists()
    assert result.plan_draft_fingerprint is not None
    posted = h.posts()
    assert len(posted) == 1


@freeze_time(NOW)
def test_small_complete_records_the_stage_events(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    h.run()
    session_name = load_state().sessions[0].name
    assert stage_events() == [
        (
            "stage.entered",
            {
                "session_id": SID,
                "ticket_id": TICKET,
                "stage": "s1_plan_generated",
                "prev_stage": "s0_intake",
                "started_at": STARTED_AT,
            },
            TICKET,
        ),
        (
            "stage.entered",
            {
                "session_id": SID,
                "ticket_id": TICKET,
                "stage": "s1_plan_reviewed",
                "prev_stage": "s1_plan_generated",
                "started_at": STARTED_AT,
            },
            TICKET,
        ),
        (
            "session.completed",
            {"session_id": SID, "ticket_id": TICKET, "session_name": session_name},
            TICKET,
        ),
    ]


def test_the_turn_is_read_only_with_no_timeout(
    harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = uuid.UUID("12345678123456781234567812345678")
    monkeypatch.setattr("cw.codex_plan._run.uuid.uuid4", lambda: fixed)
    h = harness()
    add_stations(add_turn(h.runner))
    h.run()
    (call,) = h.plan_calls
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[2:6] == ["--sandbox", "read-only", "-c", "approval_policy=never"]
    assert call["timeout"] is None
    assert call["cwd"] == h.worktree
    scratch = state_dir() / "codex-review" / fixed.hex
    assert argv[argv.index("-o") + 1] == str(scratch / "plan-output.json")
    assert not scratch.exists()
    assert not any("enabled_tools" in a for a in argv)


def test_malformed_result_is_blocked_at_stage1_plan(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.runner.add(
        stdin_contains(PLAN_TURN),
        CodexRunResult(returncode=0, stdout="", stderr="", output_file_content="{"),
    )
    result = h.run()
    assert result.status == "blocked"
    assert result.stage_reached == "stage1_plan"
    assert result.blocker is not None
    assert result.blocker.reason == "no_result_emitted"
    assert result.next_actions == _CODEX_PLAN_BLOCKED_NEXT_ACTIONS
    kinds = [(t, p.get("stage"), p.get("error_kind")) for t, p, _ in stage_events()]
    assert kinds[0] == ("stage.errored", "s1_plan_generated", "no_result_emitted")


def test_invalid_sentinel_is_validation_failed(harness: Callable[..., Harness]) -> None:
    h = harness()
    h.runner.add(stdin_contains(PLAN_TURN), plan_turn_result(SMALL_PLAN, "nope"))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "validation_failed"
    assert result.blocker.retry_eligible is False


def test_ambiguities_park_passes_through_and_posts_the_park_comment(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(
        add_turn(
            h.runner,
            status="ambiguities_pending_resolution",
            ambiguities=[{"question": "Blue or red?"}],
            next_actions=["user_resolve_ambiguities"],
        )
    )
    result = h.run()
    assert result.status == "ambiguities_pending_resolution"
    assert result.ambiguities == [{"question": "Blue or red?"}]
    assert h.draft_path().is_file()
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert result.plan_draft_fingerprint is not None
    posted = h.posts()
    (record,) = posted
    assert "## Pending Verification Scan" in str(record.stdin)
    assert "plan-spec-reviewed" not in str(record.stdin)
    kinds = [(t, p.get("stage")) for t, p, _ in stage_events()]
    assert ("stage.errored", "s1_plan_generated") not in kinds


def test_park_completes_with_null_fingerprint_when_draft_write_fails(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(
        h.runner,
        plan=LARGE_PLAN,
        tier="large",
        status="plan_pending_approval",
        next_actions=["user_decide_plan_approval"],
    )
    add_stations(h.runner)
    with patch("cw.codex_plan._run.write_plan_draft", side_effect=OSError("read-only")):
        result = h.run()
    assert result.status == "plan_pending_approval"
    assert result.plan_draft_fingerprint is None


def test_invalid_existing_plan_is_replaced(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    old_plan = "## Summary\n\nOld plan.\n\n**Scope tier:** small\n"
    plan_md_path = h.worktree / PLAN_MD_RELATIVE_PATH
    plan_md_path.parent.mkdir(parents=True, exist_ok=True)
    plan_md_path.write_text(old_plan, encoding="utf-8")
    add_stations(add_turn(h.runner))
    result = h.run()
    assert result.status == "stage_complete"
    assert _plan_body_signoff_ok(plan_md_path.read_text(encoding="utf-8"))
    assert "Old plan." not in plan_md_path.read_text(encoding="utf-8")


def test_no_op_passes_through_and_posts_nothing(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, status="no_op")
    result = h.run()
    assert result.status == "no_op"
    assert not h.draft_path().exists()
    assert not h.posts()
    assert len(h.runner.calls) == 1


def test_model_blocked_passes_through(harness: Callable[..., Harness]) -> None:
    h = harness()
    blocker = {"stage": "stage1_plan", "reason": "plan_ambiguous", "details": "x"}
    add_turn(h.runner, status="blocked", blocker=blocker)
    result = h.run()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == "plan_ambiguous"
    errored = [p for t, p, _ in stage_events() if t == "stage.errored"]
    assert [p["error_kind"] for p in errored] == ["plan_ambiguous"]


# --- plan decisions ----------------------------------------------------------


def test_plan_decisions_are_recorded_on_the_row(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    result = h.run()
    (task,) = load_dev_queue().tasks
    assert any("Widgets are blue" in c.text for c in task.operator_constraints)
    assert not any(
        f.startswith("plan_decisions_not_recorded") for f in result.friction_highlights
    )


def test_a_recorder_failure_is_a_friction_token(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    with patch(
        "cw.codex_plan._run.record_plan_constraints",
        side_effect=CwError("row not at plan"),
    ):
        result = h.run()
    assert result.status == "stage_complete"
    assert "plan_decisions_not_recorded: row not at plan" in result.friction_highlights


def test_an_unrecorded_cap_is_a_friction_token(harness: Callable[..., Harness]) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    outcome = {
        "recorded": [],
        "duplicates": 0,
        "unrecorded": [("adopted_assumption", "x")],
        "unrecorded_reason": "cap reached",
    }
    with patch("cw.codex_plan._run.record_plan_constraints", return_value=outcome):
        result = h.run()
    assert "plan_decisions_not_recorded: cap reached" in result.friction_highlights


# --- failure paths -----------------------------------------------------------


def test_a_blocking_station_parks_and_keeps_the_draft(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(
        add_turn(h.runner),
        reviewer=station_findings_doc(PLAN_REVIEWER, severity="MUST_FIX"),
    )
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_unreviewable"
    assert "MUST_FIX" in result.blocker.details
    assert h.draft_path().is_file()
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    events = [(t, p.get("stage"), p.get("error_kind")) for t, p, _ in stage_events()]
    assert events[:2] == [
        ("stage.entered", "s1_plan_generated", None),
        ("stage.errored", "s1_plan_reviewed", "plan_unreviewable"),
    ]


def test_a_gate_failure_blocks_at_generated(harness: Callable[..., Harness]) -> None:
    h = harness()
    add_turn(h.runner, plan="## Summary\n\nNo tier, no files.\n")
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_plan_gate_failed"
    errored = [p for t, p, _ in stage_events() if t == "stage.errored"]
    assert errored[0]["stage"] == "s1_plan_generated"
    assert errored[0]["error_kind"] == "codex_plan_gate_failed"


def test_an_empty_plan_is_artifact_missing(harness: Callable[..., Harness]) -> None:
    h = harness()
    add_turn(h.runner, plan="   ")
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_plan_artifact_missing"


def test_a_plan_write_failure_is_artifact_missing(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    with patch("cw.codex_plan._run.write_plan_md", side_effect=OSError("ro")):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_plan_artifact_missing"
    errored = [p for t, p, _ in stage_events() if t == "stage.errored"]
    assert errored[0]["stage"] == "s1_plan_reviewed"


def test_an_unmarked_written_plan_is_artifact_missing(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    with patch("cw.codex_plan._run.stamp_markers", side_effect=lambda text, **_: text):
        result = h.run()
    assert result.blocker is not None
    assert "sign-off markers" in result.blocker.details


def test_an_unexpected_error_completes_and_reverts_the_row(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner)
    with patch("cw.codex_plan._run.run_plan_stations", side_effect=OSError("boom")):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.stage_reached == "stage1_plan"
    assert result.next_actions == _CODEX_PLAN_BLOCKED_NEXT_ACTIONS
    (task,) = load_dev_queue().tasks
    assert task.status is QueueItemStatus.PENDING
    assert not [t for t, _, _ in stage_events() if t == "stage.errored"]


def test_an_event_write_failure_is_swallowed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_stations(add_turn(h.runner))
    with patch("cw.codex_plan._run.record_event", side_effect=OSError("disk")):
        result = h.run()
    assert result.status == "stage_complete"


def test_a_post_refusal_leaves_the_result_and_plan(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("issue", "comment", returncode=1, stderr="refused")
    add_stations(add_turn(h.runner))
    result = h.run()
    assert result.status == "stage_complete"
    assert persisted_result() == result
    assert _plan_body_signoff_ok(h.plan_md())
    (task,) = load_dev_queue().tasks
    assert task.status is QueueItemStatus.RUNNING


def test_prefetch_failure_is_plan_missing_context(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("issue", "view", TICKET, "--json", "body", returncode=1)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_missing_context"
    assert h.runner.calls == []


def test_no_docs_root_is_plan_missing_context(
    harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cw.codex_plan._prompt._packaged_commands_dir", lambda: None)
    monkeypatch.setattr("cw.codex_plan._prompt._checkout_commands_dir", lambda: None)
    h = harness()
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_missing_context"
    assert result.blocker.retry_eligible is False
    assert h.runner.calls == []


def test_a_render_error_is_plan_missing_context(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    with patch(
        "cw.codex_plan._run.render_plan_prompt", side_effect=RenderError("bad doc")
    ):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_missing_context"
    assert "bad doc" in result.blocker.details
    assert h.runner.calls == []


def test_model_capacity_is_the_only_retryable_turn_failure(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    capacity = CodexRunResult(
        returncode=1,
        stdout=(
            '{"type":"turn.failed","error":{"message":'
            '"Selected model is at capacity. Please try a different model."}}\n'
        ),
        stderr="",
    )
    h.runner.add(stdin_contains(PLAN_TURN), capacity)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_model_capacity"
    assert result.blocker.retry_eligible is True


def test_a_nonzero_turn_is_codex_error(harness: Callable[..., Harness]) -> None:
    h = harness()
    h.runner.add(
        stdin_contains(PLAN_TURN), CodexRunResult(returncode=2, stdout="", stderr="x")
    )
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_error"
    assert result.blocker.retry_eligible is False
    first = stage_events()[0]
    assert (first[0], first[1]["stage"]) == ("stage.errored", "s1_plan_generated")


# --- tracker paths -----------------------------------------------------------


def _other_tracker(
    h: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    servers: dict[str, str] | None,
    tracker: str = TRACKER_LINEAR,
    mcp: McpStage = "enabled",
) -> None:
    client = codex_tracker_client(tmp_path, servers)
    h.client = client.model_copy(update={"name": "test"})
    _write_tracker_config(client.workspace_path, tracker)
    stage_mcp_config(monkeypatch, tmp_path, "codex", mcp)


@pytest.mark.parametrize(
    ("servers", "tracker", "mcp"),
    [
        (None, TRACKER_LINEAR, "enabled"),
        ({"opencode": "linear"}, TRACKER_LINEAR, "enabled"),
        ({"codex": "bad.name"}, TRACKER_LINEAR, "enabled"),
        ({"codex": "linear"}, "jira", "enabled"),
        ({"codex": "linear"}, TRACKER_LINEAR, "absent"),
        ({"codex": "linear"}, TRACKER_LINEAR, "disabled"),
    ],
    ids=[
        "gate-unset",
        "codex-absent",
        "invalid-name",
        "no-allowlist",
        "mcp-absent",
        "mcp-disabled",
    ],
)
def test_non_github_tracker_backstop_blocks(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: dict[str, str] | None,
    tracker: str,
    mcp: McpStage,
) -> None:
    h = harness()
    _other_tracker(h, tmp_path, monkeypatch, servers=servers, tracker=tracker, mcp=mcp)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "tracker_mcp_unavailable_pre_dispatch"
    assert result.blocker.retry_eligible is False
    assert h.runner.calls == []


def test_unknown_mcp_state_fails_open_with_the_read_allowlist(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    _other_tracker(h, tmp_path, monkeypatch, servers={"codex": "linear"}, mcp="missing")
    add_stations(add_turn(h.runner))
    h.runner.add(
        lambda argv, _stdin: "--skip-git-repo-check" in argv,
        CodexRunResult(returncode=1, stdout="", stderr="post refused"),
    )
    result = h.run()
    assert result.status == "stage_complete"
    (call,) = h.plan_calls
    raw_argv = call["argv"]
    assert isinstance(raw_argv, list)
    argv = [str(a) for a in raw_argv]
    assert any("mcp_servers.linear.enabled_tools=" in a for a in argv)
    assert "=== BEGIN CW-INJECTED: Ticket ===" not in str(call["stdin"])
    assert not [r for r in h.gh.records if "view" in r.argv]


@pytest.mark.parametrize("tracker", [TRACKER_GITHUB_ISSUES, None])
def test_github_and_unset_launch_with_no_mcp_tool(
    harness: Callable[..., Harness], tracker: str | None
) -> None:
    h = harness()
    if tracker is not None:
        _write_tracker_config(h.worktree, tracker)
    add_stations(add_turn(h.runner))
    h.run()
    (call,) = h.plan_calls
    argv = call["argv"]
    assert isinstance(argv, list)
    assert not any("enabled_tools" in a for a in argv)
    assert "=== BEGIN CW-INJECTED: Ticket ===" in str(call["stdin"])


# --- run-time delivery of operator resolutions --------------------------------


def test_row_delivered_resolutions_are_read_from_cw_context(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    context = h.worktree / ".claude" / "cw-context.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    context.write_text(
        json.dumps({"queue_metadata": {"operator_resolutions": "1. Use blue."}}),
        encoding="utf-8",
    )
    add_stations(add_turn(h.runner))
    result = h.run()
    assert result.status == "stage_complete"
    (call,) = h.plan_calls
    assert ".claude/cw-context.json" in str(call["stdin"])


def test_comment_channel_resolutions_reach_the_prompt(
    harness: Callable[..., Harness],
) -> None:
    comment = {
        "author": {"login": "operator"},
        "body": f"## Pre-flight Resolutions\n\n1. Use blue.\n\n{OPERATOR_MARKER}",
        "createdAt": "2026-11-10T00:00:00Z",
    }
    h = harness(comments=[comment])
    add_stations(add_turn(h.runner))
    result = h.run()
    assert result.status == "stage_complete"
    (call,) = h.plan_calls
    assert OPERATOR_MARKER in str(call["stdin"])
