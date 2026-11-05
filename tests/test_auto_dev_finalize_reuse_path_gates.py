"""Guard tests: quality gates on the finalize PR-reuse path (#2974).

The reuse path (an OPEN PR found by the Pre-Stage Detector Guard) skips
``/prep-pr``, so ``/prep-pr`` Step 7's quality gates never run there. Step 4c.6
runs them before Step 4d arms auto-merge. Pure-markdown assertions over the
finalize command, its appendix, the runbook and the headless contract, plus a
routing check that a failing gate never ships, adopted rows included.
"""

from __future__ import annotations

import json
from pathlib import Path

from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.schema._vocab import FINALIZE_REGRESS_BLOCKER_REASONS
from cw.dev_queue import save_dev_queue
from cw.dispatch import apply_staged_decision
from cw.models import (
    ClientConfig,
    DevQueueStore,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from tests.conftest import _appendix, _cmd, _make_ticket_task, _section_between
from tests.test_stage import _guard_bullet

ROOT = Path(__file__).parent.parent
_RUNBOOK = (ROOT / "docs" / "dispatch-runbook.md").read_text(encoding="utf-8")
_HEADLESS_CONTRACT = (ROOT / "docs" / "headless-contract.md").read_text(
    encoding="utf-8"
)
_AUTO_DEV = _cmd("auto-dev.md")
_FINALIZE = _cmd("auto-dev-finalize.md")
_APPENDIX = _appendix("finalize")
_STEP_4C6_HEADING = "### Step 4c.6: Reuse-Path Quality Gates"
_APPENDIX_HEADING = "## Step 4c.6 reuse-path gate failure"


def _step_4c6() -> str:
    return _section_between(_FINALIZE, _STEP_4C6_HEADING, "### Step 4d:")


def _appendix_section() -> str:
    return _section_between(_APPENDIX, _APPENDIX_HEADING)


def _template() -> str:
    """Return the first ```json fence in the appendix section, placeholders filled."""
    section = _appendix_section()
    start = section.index("```json\n") + len("```json\n")
    raw = section[start : section.index("\n```", start)]
    return (
        raw.replace("<count>", "0")
        .replace("<pr_number>", "7")
        .replace("<ticket-id>", "2974")
        .replace("<branch-name>", "dev/2974")
        .replace("<pr_url>", "https://github.com/acme/widgets/pull/7")
        .replace("<base_ref>", "dev")
    )


class TestGuardBullet:
    def test_open_pr_bullet_names_gate_step_between_4_0b_and_4d(self) -> None:
        bullet = _guard_bullet("PR exists and is OPEN")
        headless, _, interactive = bullet.partition("**Interactive:**")
        assert "Step 4c.6" in headless
        assert "run Step 4.0, then Step 4.0b" in headless
        assert (
            headless.index("Step 4.0b")
            < headless.index("Step 4c.6")
            < headless.index("Step 4d")
        )
        assert "Step 4c.6" in interactive

    def test_no_pr_bullet_does_not_name_gate_step(self) -> None:
        assert "Step 4c.6" not in _guard_bullet("No PR exists")


class TestStep4c6Placement:
    def test_sits_between_4c5_and_4d(self) -> None:
        assert (
            _FINALIZE.index("### Step 4c.5")
            < _FINALIZE.index(_STEP_4C6_HEADING)
            < _FINALIZE.index("### Step 4d:")
        )

    def test_runs_before_ready_and_arm_in_4d(self) -> None:
        d_start = _FINALIZE.index("### Step 4d:")
        step_4d = _FINALIZE[d_start:]
        enable = step_4d.index("3. **Enable auto-merge:**")
        assert enable > 0
        assert _FINALIZE.index(_STEP_4C6_HEADING) < d_start

    def test_names_head_sha_and_arm_pin(self) -> None:
        section = _step_4c6()
        assert "git rev-parse HEAD" in section
        assert "--head-sha" in section
        assert "--match-head-commit" in section
        assert "makes no commit" in section


class TestStep4c6Runner:
    def test_reuses_prep_pr_state_gate_runner(self) -> None:
        section = _step_4c6()
        for token in ("detect-gates", "gate-timeout", "gate-pass check", "/prep-pr"):
            assert token in section
        assert 'PREP_PR_STATE=""' not in section
        assert "Step 4c.5 item 5" in section
        assert "Shell state does not persist between Bash tool calls" in section
        assert "unchanged" not in section

    def test_inherits_only_listed_step7_pieces(self) -> None:
        section = _step_4c6()
        assert "start-time" in section
        assert "foreground-timeout" in section

    def test_headless_keeps_foreground_ceiling_rules(self) -> None:
        section = _step_4c6()
        for token in (
            "foreground ceiling",
            "total budget",
            "run_in_background",
            "Worker Execution Discipline",
            "gate_timeout",
            "git status --porcelain",
        ):
            assert token in section
        assert "never" in section.lower()
        assert "dirtied" in section or "dirties" in section

    def test_no_aggregate_budget_rule(self) -> None:
        section = _step_4c6()
        assert "aggregate gate budget" not in section
        assert "no aggregate bound" in section
        assert "ADR-0014" in section


class TestStep4c6Failure:
    def test_never_mutates_or_ships_over_a_failing_gate(self) -> None:
        section = _step_4c6()
        for token in (
            "no autofix",
            "no fix loop",
            "makes no commit",
            "no `gate-pass record`",
            "no backgrounding",
            "agent_block",
        ):
            assert token in section
        assert "ship anyway" not in section.lower() or "never" in section.lower()

    def test_trigger_points_at_appendix_section(self) -> None:
        section = _step_4c6()
        assert "Read it now if" in section
        assert _APPENDIX_HEADING in _APPENDIX

    def test_adopted_limitation_documented(self) -> None:
        for text in (_step_4c6(), _appendix_section()):
            assert "plan_missing" in text
            assert "#2944" in text
            assert "FINALIZE_REGRESS_CAP" in text

    def test_runbook_adopted_section_carries_limitation(self) -> None:
        start = _RUNBOOK.index("### Adopt an open human PR")
        end = _RUNBOOK.index("\n### ", start + 1)
        section = _RUNBOOK[start:end]
        assert "#2974" in section
        assert "plan_missing" in section
        assert "#2944" in section
        assert "FINALIZE_REGRESS_CAP" in section
        assert "never a ship" in section


class TestSentinelContract:
    def test_gate_failure_template_is_valid_blocked_agent_block(self) -> None:
        result = AutoDevResult.model_validate_json(_template())
        assert result.status == "blocked"
        assert result.pr is None
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.details.startswith("Step 4c.6")
        pr_info = json.loads(_template())["pr_info"]
        assert pr_info["auto_merge"] is False
        assert result.stage_reached == "stage5_post_create"

    def test_template_never_shipped_and_base_is_placeholder(self) -> None:
        raw = _appendix_section()
        assert '"status": "shipped"' not in raw
        assert '"base": "<base_ref>"' in raw
        assert '"base": "main"' not in raw

    def test_no_aggregate_wording_in_appendix(self) -> None:
        assert "aggregate" not in _appendix_section()

    def test_agent_block_is_regress_eligible_and_no_new_reason(self) -> None:
        assert "agent_block" in FINALIZE_REGRESS_BLOCKER_REASONS
        assert "no new `blocker.reason`" in _appendix_section()

    def test_gate_failure_rows_in_collapse_tables(self) -> None:
        assert "S4c.6" in _HEADLESS_CONTRACT
        assert "S4c.6" in _AUTO_DEV
        for doc in (_HEADLESS_CONTRACT, _AUTO_DEV):
            row = next(line for line in doc.splitlines() if "S4c.6" in line)
            assert "agent_block" in row

    def test_interactive_prompt_has_no_ship_anyway(self) -> None:
        section = _appendix_section()
        assert "Fix" in section
        assert "Leave open" in section
        assert "Abort" in section
        assert "ship anyway" not in section.lower()
        assert "foreground ceiling" in section or "ceiling" in section


class TestRoutingNeverShips:
    def _route(self, adopted: bool, attempts: int) -> TicketTask:
        extra = {"adopted_pr_url": "https://github.com/acme/widgets/pull/7"}
        task = _make_ticket_task(
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
            regress_attempts=attempts,
            **(extra if adopted else {}),
        )
        clients = {
            "test-client": ClientConfig(
                name="test-client", workspace_path=Path("/nonexistent/repo")
            )
        }
        save_dev_queue(DevQueueStore(tasks=[task]))
        last = json.loads(_template())
        apply_staged_decision(task, "blocked", last, clients)
        return task

    def test_plain_row_regresses_to_impl(self) -> None:
        task = self._route(adopted=False, attempts=0)
        assert task.stage == Stage.IMPL
        assert task.status == QueueItemStatus.PENDING
        assert task.regress_attempts == 1
        assert task.disposition != "shipped"

    def test_adopted_row_regresses_to_impl(self) -> None:
        task = self._route(adopted=True, attempts=0)
        assert task.stage == Stage.IMPL
        assert task.regress_attempts == 1
        assert task.status != QueueItemStatus.COMPLETED
        assert task.disposition != "shipped"
