"""Guard tests: quality gates on the finalize PR-reuse path (#2974).

The reuse path (an OPEN PR found by the Pre-Stage Detector Guard) skips
``/prep-pr``, so ``/prep-pr`` Step 7's quality gates never run there. Step 4c.6
runs them before Step 4d arms auto-merge. Pure-markdown assertions over the
finalize command, its appendix, the runbook and the headless contract, plus a
routing check that a failing gate never ships, adopted rows included.
The #3618 CI-evidence deferral and park pins live in
``test_auto_dev_finalize_ci_deferral.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw.auto_dev_result import (
    EXTERNAL_STATE_BLOCKER_REASON,
    FINALIZE_REGRESS_CAP,
    GATE_TIMEOUT_BLOCKER_REASON,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    AutoDevResult,
)
from cw.auto_dev_result.schema._vocab import FINALIZE_REGRESS_BLOCKER_REASONS
from cw.claim_evidence import extract_claim_evidence, is_unproductive
from cw.dev_queue import GATE_TIMEOUT_DISPOSITION, save_dev_queue
from cw.dispatch import apply_staged_decision
from cw.models import (
    ClientConfig,
    DevQueueStore,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from tests._shared.session_seeding import _make_ticket_task
from tests._shared.workflow_text import _appendix, _cmd, _section_between
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
_TIMEOUT_HEADING = "## Step 4c.6 reuse-path gate timeout"
_TIMEOUT_TAG = "<!-- el: finalize.sentinel-gate-timeout -->"
_CLASSIFICATION_LEAD = "**Timeout classification (#3372).**"
_DEFINING_PHRASE = "per-call foreground timeout"
_PR_URL = "https://github.com/acme/widgets/pull/7"
_SKILLS_ROOT = ROOT / ".claude" / "skills"
_COMMANDS_ROOT = ROOT / ".claude" / "commands"
_DISARM_LITERAL = "disable-auto"


def _step_4c6() -> str:
    return _section_between(_FINALIZE, _STEP_4C6_HEADING, "### Step 4d:")


def _appendix_section() -> str:
    return _section_between(_APPENDIX, _APPENDIX_HEADING)


def _timeout_section() -> str:
    return _section_between(_APPENDIX, _TIMEOUT_HEADING)


def _timeout_classification() -> str:
    """The single timeout-classification definition under Headless ceiling."""
    return _section_between(_step_4c6(), _CLASSIFICATION_LEAD, "\n\nRead it now if")


def _template(
    *, tier: str = "small", plan_source: str = "none", forbidden_touched: bool = False
) -> str:
    """The failure section's sentinel template, placeholders filled."""
    return _template_for(
        _appendix_section(),
        tier=tier,
        plan_source=plan_source,
        forbidden_touched=forbidden_touched,
    )


def _template_for(
    section: str,
    *,
    tier: str = "small",
    plan_source: str = "none",
    forbidden_touched: bool = False,
) -> str:
    """Return the first ```json fence in *section*, placeholders filled."""
    start = section.index("```json\n") + len("```json\n")
    raw = section[start : section.index("\n```", start)]
    return (
        raw.replace("<count>", "0")
        .replace("<pr_number>", "7")
        .replace("<ticket-id>", "2974")
        .replace("<branch-name>", "dev/2974")
        .replace("<pr_url>", "https://github.com/acme/widgets/pull/7")
        .replace("<base_ref>", "dev")
        .replace(
            "<resolved scope.tier — see 'Resolve carried-through context' above>",
            tier,
        )
        .replace(
            "<resolved plan_source — see 'Resolve carried-through context' above>",
            plan_source,
        )
        .replace(
            "<resolved forbidden_touched — see "
            "'Resolve carried-through context' above>",
            json.dumps(forbidden_touched),
        )
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
        # #3618: the CI-evidence consult is no longer an inherited-list bullet;
        # it is its own paragraph, one `ci-reuse-plan` call headless and the
        # per-gate `gate-ci-evidence` call interactive.
        intro = _paragraph(section, "**Per-gate procedure.**")
        assert "adds the opt-in CI-evidence consult of its own" in intro
        listed = section[section.index(intro) : section.index("**CI evidence")]
        assert "gate-ci-evidence" not in listed
        assert "ci-reuse-plan" not in listed

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

    def test_template_carries_resolved_scope_and_plan_metadata(self) -> None:
        for tier, plan_source, forbidden_touched in (
            ("small", "none", False),
            ("large", "generated", True),
        ):
            result = AutoDevResult.model_validate_json(
                _template(
                    tier=tier,
                    plan_source=plan_source,
                    forbidden_touched=forbidden_touched,
                )
            )
            assert result.scope.tier == tier
            assert result.scope.forbidden_touched is forbidden_touched
            assert result.plan_source == plan_source

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
        assert "normal review path" in section
        assert "Do not re-enter Step 4c.6" in section


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


def _paragraph(text: str, lead: str) -> str:
    """The single-line paragraph of *text* that starts with *lead*."""
    start = text.index(lead)
    end = text.find("\n", start)
    return text[start:] if end == -1 else text[start:end]


def _runbook_adopt_section() -> str:
    start = _RUNBOOK.index("### Adopt an open human PR")
    return _RUNBOOK[start : _RUNBOOK.index("\n### ", start + 1)]


class TestStep4c6GateTimeout:
    """#3372: the reuse-path timeout is classified once and parks, never regresses."""

    def test_classification_defined_once_executor_neutral(self) -> None:
        text = _timeout_classification()
        for token in (
            _DEFINING_PHRASE,
            "cut the call off",
            "exit 124",
            "137",
            "`gate-elapsed`",
            "ADR-0014",
            "never",
            "agent_block",
        ):
            assert token in text, token
        assert "Bash tool" not in text
        assert _FINALIZE.count(_DEFINING_PHRASE) == 1

    def test_other_docs_point_at_headless_ceiling(self) -> None:
        for doc in (_APPENDIX, _HEADLESS_CONTRACT, _AUTO_DEV, _RUNBOOK):
            assert _DEFINING_PHRASE not in doc
            assert "Headless ceiling" in doc

    def test_timeout_leaves_auto_merge_as_found_and_stops(self) -> None:
        text = _timeout_classification()
        for token in (
            "no `gh pr merge` of any kind",
            "left as found",
            "not armed, not disarmed and not read back",
            "first timeout",
            "run no later gate",
            "gates not run",
            "make no fix commit",
            '`blocker.reason: "gate_timeout"`',
            "`pr: null`",
            "`pr_info.auto_merge: false`",
            "exactly as `detect-gates` printed it",
            "`HEAD_SHA`",
            "foreground ceiling",
        ):
            assert token in text, token

    def test_dirty_worktree_wins_over_timeout(self) -> None:
        text = _timeout_classification()
        assert "Dirty tree wins" in text
        assert "`END_STATUS` differs from `START_STATUS`" in text
        assert "an `agent_block` failure, not a timeout" in text

    def test_no_prompt_or_skill_names_the_disarm_literal(self) -> None:
        paths = [*_COMMANDS_ROOT.glob("*.md"), *_SKILLS_ROOT.rglob("*.md")]
        assert paths
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            assert _DISARM_LITERAL not in text, path

    def test_step_4c6_keeps_its_pins_and_drops_failed_gate_wording(self) -> None:
        section = _step_4c6()
        assert "A timeout is a failed gate" not in section
        for token in (
            "agent_block",
            "gate_timeout",
            "plan_missing",
            "#2944",
            "FINALIZE_REGRESS_CAP",
            "no aggregate bound",
            "Read it now if",
            '"Step 4c.6 reuse-path gate timeout"',
        ):
            assert token in section, token
        assert "unchanged" not in section

    def test_failure_paragraph_covers_fails_or_dirties_only(self) -> None:
        paragraph = _paragraph(_step_4c6(), "**Failure, headless.**")
        assert "Any gate fails or dirties the worktree" in paragraph
        assert "times out," not in paragraph
        assert "A timed-out gate is not a failure: see Headless ceiling." in paragraph
        assert "`gate_timeout`" not in paragraph
        assert "exit 126/127" in paragraph
        assert "to failures only" in paragraph

    def test_inherited_token_bullet_points_at_headless_ceiling(self) -> None:
        section = _step_4c6()
        assert "the `gate_timeout` token in failure `details`" not in section
        assert (
            "the `gate_timeout` token, which on a timeout is the top-level"
            " `blocker.reason` (see Headless ceiling)"
        ) in section

    def test_adopted_limitation_applies_to_failures_only(self) -> None:
        for text in (
            _paragraph(_step_4c6(), "**Adopted-row limitation"),
            _paragraph(_appendix_section(), "**Adopted-row limitation.**"),
            _runbook_adopt_section(),
        ):
            assert "parks `gate_timeout`" in text
            assert "never regresses" in text
            for token in ("plan_missing", "#2944", "FINALIZE_REGRESS_CAP"):
                assert token in text, token

    def test_interactive_timeout_sentences_stay(self) -> None:
        assert "A foreground timeout is treated as a failed gate" in _FINALIZE
        assert (
            "A foreground timeout is treated as a failed gate and shows this same"
            " prompt"
        ) in _appendix_section()

    def test_create_path_still_collapses_to_agent_block(self) -> None:
        prep_pr = _cmd("prep-pr.md")
        assert "reason: gate_timeout" in prep_pr
        assert (
            "on the create path (the reuse-path timeout in `auto-dev-finalize.md`"
            " Step 4c.6 is the one top-level `gate_timeout`, #3372)"
        ) in prep_pr
        for doc in (_HEADLESS_CONTRACT, _AUTO_DEV):
            assert "| Any other agent BLOCK (Plan / prep-pr / etc.) |" in doc
        assert "still collapses to `agent_block`" in _timeout_classification_tail()


def _timeout_classification_tail() -> str:
    """The pointer paragraph that follows the classification bullets."""
    section = _step_4c6()
    start = section.index("Read it now if a gate times out")
    return section[start : section.index("\n", start)]


class TestGateTimeoutSentinel:
    """#3372: the appendix's own `gate_timeout` sentinel section."""

    def _result(self) -> AutoDevResult:
        return AutoDevResult.model_validate_json(_template_for(_timeout_section()))

    def test_template_is_valid_blocked_gate_timeout(self) -> None:
        result = self._result()
        assert result.status == "blocked"
        assert result.pr is None
        assert result.stage_reached == "stage5_post_create"
        assert result.blocker is not None
        assert result.blocker.reason == GATE_TIMEOUT_BLOCKER_REASON
        assert result.blocker.stage == "stage5_post_create"
        assert result.blocker.retry_eligible is False
        assert result.next_actions == []
        raw = json.loads(_template_for(_timeout_section()))
        assert raw["pr_info"]["url"] == _PR_URL
        assert raw["pr_info"]["auto_merge"] is False

    def test_reason_is_neither_regress_nor_operator_unavailable(self) -> None:
        assert GATE_TIMEOUT_BLOCKER_REASON not in FINALIZE_REGRESS_BLOCKER_REASONS
        assert GATE_TIMEOUT_BLOCKER_REASON not in OPERATOR_UNAVAILABLE_BLOCKER_REASONS

    def test_details_carry_gate_head_ceiling_and_gates_not_run(self) -> None:
        result = self._result()
        assert result.blocker is not None
        details = result.blocker.details
        assert details.startswith("Step 4c.6")
        for token in (
            "exactly as detect-gates printed it",
            "<head_sha>",
            "foreground ceiling <N>s",
            "Gates not run:",
            "left as found",
        ):
            assert token in details, token

    def test_recovery_hint_names_the_release_and_the_stamp_caveat(self) -> None:
        result = self._result()
        assert result.blocker is not None
        hint = result.blocker.recovery_hint
        for token in (
            "required CI",
            "no time limit",
            "gate-pass record",
            "cw dev-queue requeue",
            "keeps the row's worktree",
            "only while the head is unchanged and the worktree is not removed",
            "re-run",
        ):
            assert token in hint, token
        assert hint.index("required CI") < hint.index("gate-pass record")
        assert hint.index("gate-pass record") < hint.index("cw dev-queue requeue")

    def test_section_has_no_aggregate_or_ship_anyway(self) -> None:
        section = _timeout_section()
        assert "aggregate" not in section
        assert "ship anyway" not in section.lower()
        assert "Headless ceiling" in section
        assert "FINALIZE_REGRESS_BLOCKER_REASONS" in section

    def test_section_is_its_own_tagged_heading(self) -> None:
        raw = _appendix("finalize", raw=True)
        lines = raw.splitlines()
        heading = next(
            i for i, line in enumerate(lines) if line.startswith(_TIMEOUT_HEADING)
        )
        assert lines[heading - 1] == _TIMEOUT_TAG
        assert _APPENDIX.index(_APPENDIX_HEADING) < _APPENDIX.index(_TIMEOUT_HEADING)

    def test_failure_section_carries_no_timeout_reason(self) -> None:
        section = _appendix_section()
        assert '"reason": "gate_timeout"' not in section
        assert "no new `blocker.reason`" in section
        assert "<dirtied worktree: <paths> | exit <code>>" in section
        assert "failed or dirtied the worktree" in section


def _s4c6_rows(doc: str) -> list[str]:
    return [line for line in doc.splitlines() if "S4c.6" in line]


class TestGateTimeoutCollapseRows:
    def test_timeout_rows_follow_failure_row(self) -> None:
        for doc in (_HEADLESS_CONTRACT, _AUTO_DEV):
            failure, timeout = _s4c6_rows(doc)[:2]
            assert "agent_block" in failure
            assert "exceeds its foreground ceiling" not in failure
            for token in (
                'blocker.reason: "gate_timeout"',
                "pr_info",
                "not regress-eligible",
                "Headless ceiling",
                "no `gh pr merge`",
            ):
                assert token in timeout, token

    def test_contract_reason_table_and_disposition_section(self) -> None:
        reason_table = _section_between(
            _HEADLESS_CONTRACT, "### 4.2 `blocker.reason`", "\n## "
        )
        row = next(
            line
            for line in reason_table.splitlines()
            if line.startswith("| `gate_timeout`")
        )
        assert "not regress-eligible" in row
        assert "gate-pass record" in row
        heading = "## The `gate_timeout` Disposition (#3372)"
        section = _section_between(_HEADLESS_CONTRACT, heading)
        for token in (
            "GATE_TIMEOUT_DISPOSITION",
            "_OPERATOR_GATED_PARK_DISPOSITIONS",
            "pr_url",
            "Variant A",
            "unproductive_attempts",
        ):
            assert token in section, token
        assert _HEADLESS_CONTRACT.index("## The Adoption Dispositions") < (
            _HEADLESS_CONTRACT.index(heading)
        )
        assert _HEADLESS_CONTRACT.index(heading) < _HEADLESS_CONTRACT.index(
            "## 9. Cross-References"
        )

    def test_auto_dev_reason_values_row(self) -> None:
        table = _section_between(_AUTO_DEV, "### `blocker.reason` Values", "\n### ")
        assert any(line.startswith("| `gate_timeout` |") for line in table.splitlines())

    def test_runbook_park_paragraph_and_doctor_list(self) -> None:
        doctor = _section_between(
            _RUNBOOK, "- `wedge/operator-gated-park` (#2739)", "\n- `"
        )
        assert "`gate_timeout`" in doctor
        start = _RUNBOOK.index("**Park: `gate_timeout` (#3372).**")
        park = _RUNBOOK[start : _RUNBOOK.index("\n\n**", start + 1)]
        for token in (
            "left as found",
            "Headless ceiling",
            "required CI",
            "no time limit",
            "gate-pass record",
            "cw dev-queue requeue <T> -c <C>",
        ):
            assert token in park, token


class TestGateTimeoutRouting:
    """#3372: the timeout parks at FINALIZE and never regresses or charges."""

    def _task(self, *, adopted: bool, attempts: int) -> TicketTask:
        extra = {"adopted_pr_url": _PR_URL}
        return _make_ticket_task(
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
            regress_attempts=attempts,
            **(extra if adopted else {}),
        )

    def _route(self, task: TicketTask, last: dict[str, object]) -> TicketTask:
        clients = {
            "test-client": ClientConfig(
                name="test-client", workspace_path=Path("/nonexistent/repo")
            )
        }
        save_dev_queue(DevQueueStore(tasks=[task]))
        apply_staged_decision(task, "blocked", last, clients)
        return task

    @pytest.mark.parametrize("adopted", [False, True], ids=["plain", "adopted"])
    @pytest.mark.parametrize(
        "attempts", [0, FINALIZE_REGRESS_CAP - 1, FINALIZE_REGRESS_CAP]
    )
    def test_parks_at_finalize_without_regress_or_charge(
        self, adopted: bool, attempts: int
    ) -> None:
        last = json.loads(_template_for(_timeout_section()))
        assert is_unproductive(extract_claim_evidence(last)) is True
        task = self._task(adopted=adopted, attempts=attempts)
        unproductive_before = task.unproductive_attempts

        self._route(task, last)

        assert task.stage == Stage.FINALIZE
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == GATE_TIMEOUT_DISPOSITION
        assert task.blocked_reason == GATE_TIMEOUT_BLOCKER_REASON
        assert task.pr_url == _PR_URL
        assert task.regress_attempts == attempts
        assert task.unproductive_attempts == unproductive_before

    def test_agent_block_at_cap_still_charges(self) -> None:
        last = json.loads(_template())
        task = self._task(adopted=False, attempts=FINALIZE_REGRESS_CAP)
        unproductive_before = task.unproductive_attempts

        self._route(task, last)

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.pr_url is None
        assert task.unproductive_attempts == unproductive_before + 1

    def test_gate_timeout_without_pr_info_leaves_pr_url_none(self) -> None:
        last = json.loads(_template_for(_timeout_section()))
        del last["pr_info"]
        task = self._route(self._task(adopted=False, attempts=0), last)
        assert task.disposition == GATE_TIMEOUT_DISPOSITION
        assert task.pr_url is None

    def test_pr_url_stamp_is_keyed_on_the_reason(self) -> None:
        last = json.loads(_template_for(_timeout_section()))
        blocker = last["blocker"]
        assert isinstance(blocker, dict)
        blocker["reason"] = EXTERNAL_STATE_BLOCKER_REASON
        task = self._route(self._task(adopted=False, attempts=0), last)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.pr_url is None
