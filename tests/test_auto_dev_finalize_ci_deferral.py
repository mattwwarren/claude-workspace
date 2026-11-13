"""Doc pins: headless finalize defers ``ci_evidence``-mapped gates to CI (#3618).

When the base branch's committed ``quality_gates.ci_evidence`` maps a detected
gate, the headless create path (``/prep-pr`` Step 7) opens the PR unarmed under
a per-worktree ``ci-hold`` and finalize parks ``awaiting_ci_evidence``; the
requeued reuse pass (Step 4c.6) asks ``ci-reuse-plan`` and parks on pending,
missing or red mapped checks. Pure-markdown assertions over the finalize
command, its appendix, ``/prep-pr``, ``/auto-dev`` and the arm-seam callers;
the decision and the hold are code (``prep_pr_state.py``, ``prep_pr_finalize.py``)
and are tested there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cw.auto_dev_result import OPERATOR_UNAVAILABLE_BLOCKER_REASONS, AutoDevResult
from cw.auto_dev_result.schema._vocab import FINALIZE_REGRESS_BLOCKER_REASONS
from tests._shared.workflow_text import (
    _appendix,
    _cmd,
    _norm,
    _section_between,
    _skill,
)
from tests.test_auto_dev_finalize_reuse_path_gates import (
    _APPENDIX_HEADING,
    _TIMEOUT_HEADING,
    _paragraph,
    _step_4c6,
    _template_for,
)

_REASON = "awaiting_ci_evidence"
_ROOT = Path(__file__).parent.parent
_RUNBOOK = (_ROOT / "docs" / "dispatch-runbook.md").read_text(encoding="utf-8")
_HEADLESS_CONTRACT = (_ROOT / "docs" / "headless-contract.md").read_text(
    encoding="utf-8"
)
_CONFIG_REFERENCE = (_ROOT / "config" / "CONFIG_REFERENCE.md").read_text(
    encoding="utf-8"
)
_CHANGELOG = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
_FINALIZE_RAW = _cmd("auto-dev-finalize.md", raw=True)
_FINALIZE = _cmd("auto-dev-finalize.md")
_APPENDIX = _appendix("finalize")
_PREP_PR = _cmd("prep-pr.md")
_AUTO_DEV = _cmd("auto-dev.md")

_CREATE_TAG = "<!-- el: finalize.ci-deferred-create-path -->"
_SELF_HEAL_END = "<!-- el-end: finalize.self-heal-arm-only-when-automerge-enabled -->"
_POST_PUSH_BEGIN = "<!-- el-begin: finalize.post-push-mergeability-verification -->"
_SENTINEL_TAG = "<!-- el: finalize.sentinel-awaiting-ci-evidence -->"
_SENTINEL_HEADING = (
    "## Step 4c / 4c.6 CI-evidence park: the `awaiting_ci_evidence` sentinel (#3618)"
)
_STEP7_LEAD = "**Headless only: CI-evidence deferral (#3618).**"
_STEP7_RUN_EACH = "Run each gate detected in Step 2"
_RELEASE_CMD = '"$FINALIZE" ci-hold release --repo-path <worktree>'


def _create_path_element() -> str:
    raw = _FINALIZE_RAW
    start = raw.index(_CREATE_TAG)
    return raw[start : raw.index(_POST_PUSH_BEGIN, start)]


def _sentinel_section() -> str:
    return _section_between(_APPENDIX, _SENTINEL_HEADING)


def _step7() -> str:
    return _section_between(
        _PREP_PR, "## Step 7: Run Quality Gates", "### Loop Control"
    )


def _step7_deferral() -> str:
    step7 = _step7()
    return step7[step7.index(_STEP7_LEAD) : step7.index(_STEP7_RUN_EACH)]


def _passing_run() -> str:
    return _paragraph(_step_4c6(), "**Passing run.**")


class TestCreatePathElement:
    def test_sits_between_self_heal_and_post_push_verification(self) -> None:
        raw = _FINALIZE_RAW
        assert f"{_SELF_HEAL_END}\n{_CREATE_TAG}\n" in raw
        assert (
            raw.index(_SELF_HEAL_END)
            < raw.index(_CREATE_TAG)
            < raw.index(_POST_PUSH_BEGIN)
        )
        assert raw.count(_CREATE_TAG) == 1
        between = raw[raw.index(_CREATE_TAG) : raw.index(_POST_PUSH_BEGIN)]
        assert "<!-- el" not in between[len(_CREATE_TAG) :]

    def test_runs_headless_only_before_step_4c5(self) -> None:
        text = _norm(_create_path_element())
        assert "headless only" in text.lower()
        assert "Interactive runs skip" in text
        assert "before Step 4c.5" in text
        assert "after the verify gate" in text
        assert "self-heal" in text

    def test_reads_the_hold_with_ci_hold_status_json(self) -> None:
        text = _create_path_element()
        assert "ci-hold status --json --repo-path <worktree>" in text
        assert "grep -q 'ci-hold'" in text
        assert "repo-first probe from the self-heal fence" in text

    def test_parks_on_active_or_corrupt_and_continues_on_none_or_stale(self) -> None:
        text = _norm(_create_path_element())
        parks = text[text.index("`active` or `corrupt`") :]
        assert "`deferred`" in parks.split("`none` or `stale`")[0]
        assert "STOP" in parks.split("`none` or `stale`")[0]
        assert "`none` or `stale`: continue to Step 4c.5" in text
        assert "lacks `ci-hold`" in text
        assert "continue to Step 4c.5" in text

    def test_status_failure_is_hold_error_not_agent_block(self) -> None:
        text = _norm(_create_path_element())
        assert "exits non-zero or prints non-JSON" in text
        assert "`hold_error`" in text
        assert "agent_block" not in text
        assert _REASON in text

    def test_points_at_the_appendix_section(self) -> None:
        text = _create_path_element()
        assert "auto-dev-finalize-appendix.md" in text
        assert "Read it now" in text
        assert _SENTINEL_HEADING.removeprefix("## ") in _APPENDIX


class TestReusePassPlan:
    def test_scope_sentence_is_rewritten_for_deferred_gates(self) -> None:
        scope = _paragraph(_step_4c6(), "**Scope.**")
        assert "already ran every gate it did not defer to CI" in scope
        assert "parks the run at Step 4c as `awaiting_ci_evidence`" in scope
        assert "decided by this step on the requeue" in scope
        assert "because `/prep-pr` Step 7 already ran the gates." not in scope

    def test_detector_guard_names_deferred_gates(self) -> None:
        assert "or deferred to CI" in _FINALIZE
        guard = _FINALIZE[_FINALIZE.index("- **PR exists and is OPEN**") :]
        assert "or deferred to CI" in guard[: guard.index("\n")]

    def test_consumes_ci_reuse_plan_keys(self) -> None:
        section = _step_4c6()
        for token in (
            '"$PREP_PR_STATE" ci-reuse-plan --pr <pr_number>'
            " --repo <owner>/<repo> --base <base>",
            "`park`",
            "`skip`",
            "`run`",
            "`park.flavor`",
            "reuse_path_gate_ci_covered: <gate> via <checks>",
        ):
            assert token in section, token

    def test_pending_missing_red_never_run_locally_and_never_arm(self) -> None:
        text = _norm(_step_4c6())
        assert "pending, missing or red" in text
        assert "never run the gate locally and never arm" in text
        assert "make no `ci-hold release`" in text
        assert "Step 4d is not reached" in text

    def test_stale_script_or_non_json_runs_locally_with_friction(self) -> None:
        text = _norm(_step_4c6())
        assert "ci_evidence_unavailable: ci-reuse-plan <cause>" in text
        assert "run every gate locally" in text
        assert "predates the subcommand" in text
        assert "never retry" in text.lower()

    def test_headless_does_not_call_gate_ci_evidence_per_gate(self) -> None:
        text = _norm(_step_4c6())
        interactive = text[text.index("**Interactive.**") :]
        assert "gate-ci-evidence" in interactive
        assert "only `skip: true` skips" in interactive
        assert "Never parks" in interactive
        headless = text[: text.index("**Interactive.**")]
        assert "gate-ci-evidence" not in headless

    def test_release_precedes_step_4d_after_a_park_free_plan(self) -> None:
        passing = _norm(_passing_run())
        assert _RELEASE_CMD in passing
        assert passing.index("ci-hold release") < passing.index("continue to Step 4d")
        assert "no `park`" in passing or "returned no `park`" in passing
        assert "grep -q 'ci-hold'" in passing
        assert "reuse_path_gates: passed" in passing

    def test_release_failure_parks_hold_error_and_never_falls_through(self) -> None:
        passing = _norm(_passing_run())
        assert "non-zero exit" in passing
        assert "`hold_error`" in passing
        assert "never falls through to Step 4d" in passing
        assert "pr.auto_merge: false" in passing

    def test_zero_gates_still_releases_the_hold(self) -> None:
        section = _norm(_step_4c6())
        assert "reuse_path_gates: none_detected" in section
        zero = section[section.index("Zero gates detected") :]
        assert "Passing run" in zero[: zero.index("Script absent")]

    def test_a_head_move_never_releases_and_the_arm_pin_is_kept(self) -> None:
        passing = _norm(_passing_run())
        assert "a head move never releases it" in passing
        assert "--match-head-commit" in _step_4c6()

    def test_interactive_release_and_per_gate_call_are_stated(self) -> None:
        text = _norm(_step_4c6())
        assert "Interactive" in text
        assert "created headless-deferred" in text
        assert "finalized interactively" in text
        assert "per-gate `gate-ci-evidence` call" in text


class TestPrepPrStep7:
    def test_headless_paragraph_precedes_the_gate_loop(self) -> None:
        step7 = _step7()
        assert step7.index(_STEP7_LEAD) < step7.index(_STEP7_RUN_EACH)
        assert step7.index(_STEP7_LEAD) < step7.index("gate-pass check")

    def test_resolves_finalize_with_the_repo_first_probe(self) -> None:
        text = _step7_deferral()
        assert 'FINALIZE=""' in text
        assert (
            "for candidate in .claude/scripts/prep_pr_finalize.py"
            " scripts/prep_pr_finalize.py"
            ' "$HOME/.claude/scripts/prep_pr_finalize.py"; do'
        ) in text
        assert "grep -q 'ci-hold' \"$FINALIZE\"" in text

    def test_sets_the_hold_before_skipping_a_deferred_gate(self) -> None:
        text = _norm(_step7_deferral())
        assert '"$PREP_PR_STATE" ci-deferral --base <base>' in text
        assert "ci-hold set --base <base> --gates-json" in text
        assert "the deferred array verbatim" in text
        assert "FIRST" in text
        assert text.index("ci-hold set --base") < text.index(
            "create_path_gate_ci_deferred:"
        )
        assert "create_path_gate_ci_deferred: <gate> via <checks>" in text

    def test_failures_run_every_gate_locally_with_friction(self) -> None:
        text = _norm(_step7_deferral())
        assert "run every gate locally" in text
        assert "ci_evidence_unavailable: ci-deferral <cause>" in text
        assert "ci_evidence_unavailable: ci-hold set <cause>" in text

    def test_failed_deferral_makes_one_best_effort_release(self) -> None:
        text = _norm(_step7_deferral())
        assert "best-effort" in text
        assert "ci-hold release" in text
        assert "ci_evidence_unavailable: ci-hold release <cause>" in text
        assert "ignore" in text.lower()
        assert text.index("best-effort") < text.index(
            "ci_evidence_unavailable: ci-hold release"
        )

    def test_later_commits_leave_the_hold_in_force_with_no_second_set(self) -> None:
        text = _norm(_step7_deferral())
        for token in (
            "Step 7 autofix",
            "Step 8",
            "leave the hold `active` by ancestry",
            "no second `ci-hold set` before Step 8",
        ):
            assert token in text, token

    def test_empty_deferral_clears_any_stale_hold(self) -> None:
        text = _norm(_step7_deferral())
        assert "--gates-json '[]'" in text

    def test_interactive_step_7_has_no_deferral(self) -> None:
        rest = _PREP_PR.replace(_step7_deferral(), "")
        assert "ci-deferral" not in rest
        assert "ci-hold" not in rest
        assert "Interactive Step 7 skips" in _step7_deferral()

    def test_step_9_exception_names_the_hold(self) -> None:
        step9 = _section_between(
            _PREP_PR, "## Step 9: Finalize", "**If monitor registration"
        )
        text = _norm(step9)
        assert "can also mean an active CI-evidence hold" in text
        assert "do NOT run `gh pr merge --auto` then either" in text


class TestAwaitingCiEvidenceSentinel:
    def _raw(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(_template_for(_sentinel_section()))
        return parsed

    def test_section_is_its_own_tagged_heading_after_gate_timeout(self) -> None:
        lines = _appendix("finalize", raw=True).splitlines()
        heading = next(
            i for i, line in enumerate(lines) if line.startswith(_SENTINEL_HEADING)
        )
        assert lines[heading - 1] == _SENTINEL_TAG
        assert _APPENDIX.index(_TIMEOUT_HEADING) < _APPENDIX.index(_SENTINEL_HEADING)
        assert _APPENDIX.index(_SENTINEL_HEADING) < _APPENDIX.index(
            "## Step 4c.2: the `auto` permission-mode"
        )
        assert _APPENDIX.index(_APPENDIX_HEADING) < _APPENDIX.index(_SENTINEL_HEADING)

    def test_template_is_a_valid_blocked_sentinel(self) -> None:
        result = AutoDevResult.model_validate_json(_template_for(_sentinel_section()))
        assert result.status == "blocked"
        assert result.pr is None
        assert result.stage_reached == "stage5_post_create"
        assert result.next_actions == []
        blocker = result.blocker
        assert blocker is not None
        assert blocker.reason == _REASON
        assert blocker.stage == "stage5_post_create"
        assert blocker.exception_type is None
        assert blocker.retry_delay_seconds is None
        raw = self._raw()
        pr_info = raw["pr_info"]
        assert isinstance(pr_info, dict)
        assert pr_info["number"] == 7
        assert pr_info["auto_merge"] is False
        assert pr_info["url"] == "https://github.com/acme/widgets/pull/7"

    def test_template_matches_the_gate_timeout_template_but_blocker_and_pr_info(
        self,
    ) -> None:
        mine = self._raw()
        theirs = json.loads(
            _template_for(_section_between(_APPENDIX, _TIMEOUT_HEADING))
        )
        assert set(mine) == set(theirs)
        for key in set(mine) - {"blocker", "pr_info"}:
            assert mine[key] == theirs[key], key
        assert mine["pr_info"] == theirs["pr_info"]

    def test_details_prefixes_are_verbatim(self) -> None:
        text = _norm(_sentinel_section())
        for token in (
            "ci_evidence_deferred: PR #<n> (<url>) at head <sha>: gate <g> mapped"
            " to check(s) <checks> deferred to CI and not run locally",
            "cw did not arm auto-merge.",
            "ci_evidence_pending: PR #<n> (<url>) at head <sha>: gate <g> mapped"
            " to check <c> is <check_state>; cw did not arm auto-merge and did not"
            " run the gate locally.",
            "ci_evidence_red: PR #<n> (<url>) at head <sha>: gate <g> mapped to"
            " check <c> concluded <check_state>",
            "This is a failing CI check, not a gate failure; cw did not arm"
            " auto-merge and did not run the gate locally.",
            "ci_evidence_hold_error: PR #<n> (<url>) at head <sha>: ci-hold"
            " <status|release> failed (<cause>). cw did not arm auto-merge.",
        ):
            assert token in text, token

    def test_messages_and_retry_eligibility_per_flavor(self) -> None:
        text = _norm(_sentinel_section())
        for token in (
            "mapped CI checks have not all finished green at the PR head; cw did"
            " not arm auto-merge",
            "a mapped CI check is red at the PR head (a CI failure, not a gate"
            " failure); cw did not arm auto-merge",
            "the CI-evidence hold could not be read or released; cw did not arm"
            " auto-merge",
            "`retry_eligible` is `false` for `red`",
        ):
            assert token in text, token

    def test_recovery_hints_are_verbatim(self) -> None:
        text = _norm(_sentinel_section())
        for token in (
            "Orchestrator: once the mapped CI checks have finished, run cw"
            " dev-queue requeue <ticket> -c <client>; finalize arms only if every"
            " mapped check is green at the PR head.",
            "Orchestrator: a mapped CI check failed (not a gate failure). Follow"
            " the cw-orchestration ci_failing parked-row route: cw dev-queue"
            " requeue <ticket> -c <client> --stage impl --regress, adding"
            " --review-feedback-file <file> when you hold head-pinned evidence of"
            " the failure.",
            "Orchestrator: fix the cause in details (usually a stale"
            " prep_pr_finalize.py copy), then cw dev-queue requeue <ticket> -c"
            " <client>.",
        ):
            assert token in text, token

    def test_names_both_escapes_and_the_manual_hold_release(self) -> None:
        text = _norm(_sentinel_section())
        assert _RELEASE_CMD in text
        assert "ADR-0014" in text
        assert "quality_gates.ci_evidence" in text
        assert "origin/<base>" in text
        assert "cw dev-queue remove" in text
        assert "pr.auto_merge: false" in text
        assert "not offered" in text

    def test_reason_is_not_regress_or_operator_unavailable(self) -> None:
        assert sorted(FINALIZE_REGRESS_BLOCKER_REASONS) == ["agent_block"]
        assert _REASON not in FINALIZE_REGRESS_BLOCKER_REASONS
        assert _REASON not in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
        text = _norm(_sentinel_section())
        assert "Do not add `awaiting_ci_evidence` to" in text
        assert "FINALIZE_REGRESS_BLOCKER_REASONS" in text
        assert "no `schema_version` bump" in text

    def test_section_has_no_aggregate_or_ship_anyway(self) -> None:
        section = _sentinel_section()
        assert "aggregate" not in section
        assert "ship anyway" not in section.lower()


class TestCollapseTablesAndOrchestrationSkill:
    def test_auto_dev_has_s4c6_park_row_and_reason_value(self) -> None:
        rows = [line for line in _AUTO_DEV.splitlines() if "S4c.6" in line]
        row = next(line for line in rows if _REASON in line)
        for token in (
            "`pr_info`",
            "not regress-eligible",
            "ci-reuse-plan",
            "Step 4c create-path check",
            "no arm",
        ):
            assert token in row, token
        table = _section_between(_AUTO_DEV, "### `blocker.reason` Values", "\n### ")
        assert any(line.startswith(f"| `{_REASON}` |") for line in table.splitlines())

    def test_state_map_entry_exists_and_is_orchestrator_run(self) -> None:
        content = _skill("cw-orchestration/SKILL.md")
        bullets = [
            line
            for line in content.splitlines()
            if line.startswith(f"- **`{_REASON}`**")
        ]
        assert len(bullets) == 1
        entry = _norm(bullets[0])
        assert "Who: orchestrator" in entry
        assert "cw dev-queue requeue <T> -c <C>" in entry

    def test_arm_seam_callers_arm_only_through_arm_automerge(self) -> None:
        # The hold lives in the arm seam; a bare `gh pr merge --auto` would
        # bypass it, so the two seam callers must go through arm-automerge.
        for name, text in (
            ("review-monitor.md", _cmd("review-monitor.md")),
            ("cw-session-watch", _skill("cw-session-watch/SKILL.md")),
        ):
            assert "prep_pr_finalize.py arm-automerge" in text, name
            assert not re.search(r"(?m)^\s*(?:\$\s*)?gh pr merge", text), name


class TestOperatorDocs:
    """CONFIG_REFERENCE, the headless contract, the runbook and the changelog."""

    def test_config_reference_documents_create_path_and_parks(self) -> None:
        section = _norm(
            _section_between(
                _CONFIG_REFERENCE,
                "## Quality Gate CI Evidence (`quality_gates.ci_evidence`)",
            )
        )
        for token in (
            "`outcome`",
            "`green`",
            "`pending`",
            "`red`",
            "`local`",
            "ci-deferral",
            "ci-reuse-plan",
            "create path",
            _REASON,
            "never run locally and never arm",
            "never reports",
            "ADR-0014",
            "cw dev-queue remove",
            "survives later commits",
            "only by the green reuse pass",
            _RELEASE_CMD,
            "Interactive",
        ):
            assert token in section, token

    def test_config_reference_no_longer_says_pending_runs_locally(self) -> None:
        section = _norm(
            _section_between(
                _CONFIG_REFERENCE,
                "## Quality Gate CI Evidence (`quality_gates.ci_evidence`)",
            )
        )
        assert "check-not-success` (pending, red, cancelled, skipped, neutral)" not in (
            section
        )

    def test_contract_reason_row_collapse_row_and_disposition_section(self) -> None:
        reason_table = _section_between(
            _HEADLESS_CONTRACT, "### 4.2 `blocker.reason`", "\n## "
        )
        row = next(
            line
            for line in reason_table.splitlines()
            if line.startswith(f"| `{_REASON}`")
        )
        for token in ("#3618", "not regress-eligible", "ci-hold", "no version bump"):
            assert token in row, token
        collapse = next(
            line
            for line in _HEADLESS_CONTRACT.splitlines()
            if "S4c.6" in line and _REASON in line
        )
        assert "ci-reuse-plan" in collapse
        heading = "## The `awaiting_ci_evidence` Disposition (#3618)"
        section = _norm(_section_between(_HEADLESS_CONTRACT, heading))
        for token in (
            "AWAITING_CI_EVIDENCE_DISPOSITION",
            "_OPERATOR_GATED_PARK_DISPOSITIONS",
            "pr_url",
            "unproductive_attempts",
            "no automatic release",
            "cw dev-queue remove",
        ):
            assert token in section, token
        assert _HEADLESS_CONTRACT.index("## The `gate_timeout` Disposition") < (
            _HEADLESS_CONTRACT.index(heading)
        )
        assert _HEADLESS_CONTRACT.index(heading) < _HEADLESS_CONTRACT.index(
            "## 9. Cross-References"
        )

    def test_runbook_park_paragraph_and_doctor_list(self) -> None:
        doctor = _section_between(
            _RUNBOOK, "- `wedge/operator-gated-park` (#2739)", "\n- `"
        )
        assert f"`{_REASON}`" in doctor
        start = _RUNBOOK.index(f"**Park: `{_REASON}` (#3618).**")
        park = _norm(_RUNBOOK[start : _RUNBOOK.index("\n\n**", start + 1)])
        for token in (
            "ci_evidence_deferred:",
            "ci_evidence_pending:",
            "ci_evidence_red:",
            "ci_evidence_hold_error:",
            "cw dev-queue requeue <T> -c <C>",
            "--stage impl --regress",
            "never reports",
            "ADR-0014",
            "cw dev-queue remove",
            _RELEASE_CMD,
        ):
            assert token in park, token

    def test_changelog_added_entry(self) -> None:
        # The entry sits under [Unreleased] until a release cut moves it into
        # its version section, so find it wherever it lives and check that its
        # nearest heading is "### Added".
        entry_start = _CHANGELOG.index("- **Headless finalize defers")
        heading_start = _CHANGELOG.rindex("\n### ", 0, entry_start) + 1
        assert _CHANGELOG[heading_start:].startswith("### Added")
        entry = _CHANGELOG[entry_start:].split("\n", 1)[0]
        assert "#3618" in entry
        for token in (_REASON, "quality_gates.ci_evidence", "cw dev-queue requeue"):
            assert token in entry, token
