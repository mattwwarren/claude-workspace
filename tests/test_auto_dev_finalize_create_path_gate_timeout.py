"""Guard tests: the create-path ``/prep-pr`` ``gate_timeout`` promotion (#3380).

On the finalize create path, headless ``/prep-pr`` Step 7 returns a nested
``<<<PREP_PR_BLOCK reason: gate_timeout`` marker when the executor's foreground
timeout cuts a quality gate off. Finalize's Create-path gate-timeout classifier
promotes it to the same top-level ``blocker.reason: "gate_timeout"`` park the
reuse path (#3372) emits, on a clean worktree only. Pure-markdown assertions
over the finalize command, its appendix and ``/prep-pr``, a real-git check of
the classifier's dirty-tree command, and a routing check that the no-PR
sentinel parks at FINALIZE without regressing or charging an attempt.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from cw.auto_dev_result import (
    FINALIZE_REGRESS_CAP,
    GATE_TIMEOUT_BLOCKER_REASON,
    AutoDevResult,
)
from cw.dev_queue import GATE_TIMEOUT_DISPOSITION
from cw.models import QueueItemStatus, Stage, TicketTask
from tests import _git_sandbox
from tests._shared.git_repos import commit_tracked_file
from tests._shared.session_seeding import _make_ticket_task, _route_blocked
from tests._shared.workflow_text import (
    _appendix,
    _cmd,
    _section_between,
    _template_for,
)

_CLASSIFIER_TAG = "<!-- el: finalize.create-path-gate-timeout-classifier -->"
_CLASSIFIER_ID = "finalize.create-path-gate-timeout-classifier"
_UNAVAILABILITY_END = "<!-- el-end: finalize.unavailability-classifier -->"
_REVERIFY_TAG = "<!-- el: finalize.main-session-re-verification -->"
_DENIED_ARM_LEAD = "**Classifier-denied arm (#2625) — checked first.**"
_TIMEOUT_HEADING = "## Step 4c.6 reuse-path gate timeout"
_CREATE_SUBHEADING = "### Create path variant (#3380)"
_HEADLESS_DETAILS = (
    "details: foreground ceiling <foreground_ceiling_s>s exceeded at <started>;"
    " not backgrounded (headless). Last captured output:"
)
_STATE_FILE = ".claude/prep-pr-state.json"


def _finalize_raw() -> str:
    return _cmd("auto-dev-finalize.md", raw=True)


def _classifier_paragraph() -> str:
    raw = _finalize_raw()
    start = raw.index(_CLASSIFIER_TAG)
    return raw[start : raw.index("\n\n", start)]


def _timeout_section() -> str:
    return _section_between(_appendix("finalize"), _TIMEOUT_HEADING)


def _create_template() -> dict[str, object]:
    parsed: dict[str, object] = json.loads(_template_for(_timeout_section(), fence=1))
    return parsed


def _dirty_probe_argv() -> list[str]:
    match = re.search(r"`(git status --porcelain [^`]+)`", _classifier_paragraph())
    assert match is not None
    return shlex.split(match.group(1))


class TestClassifierParagraph:
    def test_tag_is_unranged_and_appears_once(self) -> None:
        raw = _finalize_raw()
        assert raw.count(_CLASSIFIER_TAG) == 1
        assert f"el-begin: {_CLASSIFIER_ID}" not in raw
        assert f"el-end: {_CLASSIFIER_ID}" not in raw

    def test_sits_between_unavailability_and_reverification(self) -> None:
        raw = _finalize_raw()
        assert (
            raw.index(_UNAVAILABILITY_END)
            < raw.index(_CLASSIFIER_TAG)
            < raw.index(_REVERIFY_TAG)
        )
        assert f"{_UNAVAILABILITY_END}\n{_CLASSIFIER_TAG}\n" in raw

    def test_names_the_outermost_headless_key(self) -> None:
        paragraph = _classifier_paragraph()
        for token in (
            "<<<PREP_PR_BLOCK",
            "`reason: gate_timeout`",
            "outermost block",
            "the closing `PREP_PR_BLOCK>>>` line is the last non-blank line",
            "never prose inside `details:`",
            "`details: foreground ceiling `",
            "`s exceeded at `",
            "`not backgrounded (headless)`",
            "`Last captured output:`",
            "interactive poll-ceiling marker",
            "is never promoted",
        ):
            assert token in paragraph, token

    def test_names_the_dirty_tree_probe_and_fallback(self) -> None:
        paragraph = _classifier_paragraph()
        assert (
            "`git status --porcelain -- . ':(exclude).claude/prep-pr-state.json'`"
        ) in paragraph
        assert "falls back to `agent_block`" in paragraph
        assert "untracked `.claude/` file the repo does not ignore" in paragraph
        assert "`dirty-worktree`" in paragraph

    def test_defers_denial_and_runs_no_verify_merge_or_pr(self) -> None:
        paragraph = _classifier_paragraph()
        for token in (
            "classifier-denial phrase",
            "`arm-automerge blocked by the auto-mode permission classifier`",
            "defers to the denied-arm check (#2625)",
            "skip the verify-script gate and Step 4c.5",
            "run no `gh pr merge`",
            "record no gate-pass",
            "no PR exists",
            "`HEAD_SHA=$(git rev-parse HEAD)`",
            "Create path variant (#3380)",
        ):
            assert token in paragraph, token

    def test_denied_arm_order_paragraph_places_the_classifier(self) -> None:
        raw = _finalize_raw()
        start = raw.index(_DENIED_ARM_LEAD)
        bullet = raw[start : raw.index("\n", start)]
        order = bullet[bullet.index("Order of evaluation (#2625):") :]
        assert "#3380" in order
        assert "before (3) and (4)" in order
        assert "before the verify gate" in order


class TestMarkerCoupling:
    """The classifier's key must match the marker `/prep-pr` actually emits."""

    def test_headless_emission_carries_the_keyed_fields(self) -> None:
        assert _HEADLESS_DETAILS in _cmd("prep-pr.md")

    def test_interactive_marker_lacks_the_headless_form(self) -> None:
        prep_pr = _cmd("prep-pr.md")
        line = next(
            ln
            for ln in prep_pr.splitlines()
            if ln.strip().startswith("details: foreground ceiling")
        )
        assert "; backgrounded; poll ceiling" in line
        assert "not backgrounded (headless)" not in line


class TestDirtyTreeProbe:
    """The doc's probe agrees with `prep_pr_state._worktree_state`."""

    def _probe(self, repo: Path) -> str:
        argv = _dirty_probe_argv()
        assert argv[:3] == ["git", "status", "--porcelain"]
        return _git_sandbox.run(*argv[1:], cwd=repo).stdout

    def test_lone_state_file_is_clean(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("probe-state")
        (repo / ".claude").mkdir()
        (repo / _STATE_FILE).write_text("{}\n", encoding="utf-8")
        assert self._probe(repo) == ""

    def test_untracked_claude_scratch_file_is_dirty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("probe-scratch")
        (repo / ".claude").mkdir()
        (repo / _STATE_FILE).write_text("{}\n", encoding="utf-8")
        (repo / ".claude" / "scratch.md").write_text("x\n", encoding="utf-8")
        assert self._probe(repo) != ""

    def test_modified_tracked_file_is_dirty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("probe-tracked")
        commit_tracked_file(repo, "mod.py")
        (repo / "mod.py").write_text("x = 2\n", encoding="utf-8")
        assert self._probe(repo) != ""


class TestCreatePathSentinel:
    def _result(self) -> AutoDevResult:
        return AutoDevResult.model_validate_json(
            _template_for(_timeout_section(), fence=1)
        )

    def test_subsection_is_inside_the_sliced_section(self) -> None:
        section = _timeout_section()
        assert _CREATE_SUBHEADING in section
        assert section.index("```json\n") < section.index(_CREATE_SUBHEADING)

    def test_template_is_a_no_pr_blocked_gate_timeout(self) -> None:
        result = self._result()
        assert result.status == "blocked"
        assert result.pr is None
        assert result.stage_reached == "stage4b_pr_create"
        assert result.blocker is not None
        assert result.blocker.stage == "stage4b_pr_create"
        assert result.blocker.reason == GATE_TIMEOUT_BLOCKER_REASON
        assert result.blocker.retry_eligible is False
        assert "pr_info" not in _create_template()

    def test_details_and_message(self) -> None:
        result = self._result()
        assert result.blocker is not None
        assert result.blocker.details.startswith("Step 4c create path")
        assert "not backgrounded (headless)" in result.blocker.details
        assert result.blocker.message == (
            "create-path quality gate was cut off by the foreground timeout;"
            " not a gate failure; no PR exists"
        )

    def test_recovery_hint_names_the_no_pr_release(self) -> None:
        result = self._result()
        assert result.blocker is not None
        hint = result.blocker.recovery_hint
        for token in (
            "no PR exists",
            "no time limit",
            "gate-pass record",
            "cw dev-queue requeue",
            "keeps the row's worktree",
            "only while the head is unchanged and the worktree is not removed",
            "re-run",
        ):
            assert token in hint, token
        assert hint.index("gate-pass record") < hint.index("cw dev-queue requeue")
        assert "confirm required CI is green at the PR head" not in hint

    def test_section_and_hint_avoid_forbidden_words(self) -> None:
        result = self._result()
        assert result.blocker is not None
        for text in (_timeout_section(), result.blocker.recovery_hint):
            assert "aggregate" not in text
            assert "ship anyway" not in text.lower()

    def test_reuse_template_stays_first(self) -> None:
        reuse = AutoDevResult.model_validate_json(_template_for(_timeout_section()))
        assert reuse.stage_reached == "stage5_post_create"


class TestPrepPrSentence:
    def test_sentence_names_the_promotion(self) -> None:
        prep_pr = _cmd("prep-pr.md")
        assert "reason: gate_timeout" in prep_pr
        assert "still collapses to `agent_block`" not in prep_pr
        for token in (
            "create-path gate-timeout classifier (#3380)",
            "promotes the marker",
            "`auto-dev-finalize.md` Step 4c.6 is the other top-level source, #3372",
        ):
            assert token in prep_pr, token


class TestCreatePathRouting:
    def _task(self, attempts: int) -> TicketTask:
        return _make_ticket_task(
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
            regress_attempts=attempts,
        )

    @pytest.mark.parametrize(
        "attempts", [0, FINALIZE_REGRESS_CAP - 1, FINALIZE_REGRESS_CAP]
    )
    def test_parks_at_finalize_without_pr_regress_or_charge(
        self, attempts: int
    ) -> None:
        last = _create_template()
        assert "pr_info" not in last
        task = self._task(attempts)
        unproductive_before = task.unproductive_attempts

        _route_blocked(task, last)

        assert task.stage == Stage.FINALIZE
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == GATE_TIMEOUT_DISPOSITION
        assert task.blocked_reason == GATE_TIMEOUT_BLOCKER_REASON
        assert task.pr_url is None
        assert task.regress_attempts == attempts
        assert task.unproductive_attempts == unproductive_before

    def test_dirty_tree_fallback_agent_block_still_regresses(self) -> None:
        last = _create_template()
        blocker = last["blocker"]
        assert isinstance(blocker, dict)
        blocker["reason"] = "agent_block"
        task = _route_blocked(self._task(FINALIZE_REGRESS_CAP - 1), last)
        assert task.stage == Stage.IMPL
        assert task.regress_attempts == FINALIZE_REGRESS_CAP
