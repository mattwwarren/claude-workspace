"""Tests for cw.codex_finalize._result: turn document and results (#3062)."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import cw.codex_finalize._arm as arm_module
import cw.codex_finalize._gates as gates_module
import cw.codex_finalize._merge_gate as merge_gate_module
import cw.codex_finalize._pr as pr_module
import cw.codex_finalize._result as result_module
from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.parse import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_VALIDATION_FAILED,
)
from cw.auto_dev_result.schema._vocab import KNOWN_BLOCKER_REASONS
from cw.codex_finalize._result import (
    AGENT_BLOCK,
    AUTOMERGE_NOT_ARMED,
    CODEX_MUST_FIX_FINDINGS,
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    PR_ALREADY_TERMINAL,
    PR_TITLE_MAX_CHARS,
    PRIOR_PIPELINE_PR_OPEN,
    PUSH_AUTH_FAILED,
    REQUEUE_STAGE_FINALIZE,
    REQUEUE_STAGE_REVIEW,
    SCOPE_TIER_UNRESOLVABLE,
    STAGE4_MUST_FIX_OVERRIDE,
    WAIT_FOR_CI,
    FinalizeBlock,
    FinalizeFacts,
    FinalizeTurnDocument,
    classify_unavailability,
    compose_pr_title,
    finalize_approval_park,
    finalize_blocked,
    finalize_merge_gate_blocked,
    finalize_shipped,
    parse_finalize_turn,
)
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    STAGE4A_MERGE_GATE,
    STAGE4B_PR_CREATE,
    STAGE5_POST_CREATE,
)
from cw.dev_queue.gates import FINALIZE_PARK_STAGE_REACHED
from cw.unavailability import UNAVAILABILITY_SIGNATURES
from tests._tool_fakes import finalize_document

if TYPE_CHECKING:
    from types import ModuleType

    from cw.auto_dev_result import StageReached
    from cw.local_runner import GitFacts

_WORKTREE = Path("/wt/finalize")
_FORK = "f" * 40
_COMMIT = "c" * 40
_PR_URL = "https://github.com/o/r/pull/7"


def _facts(friction: list[str] | None = None) -> FinalizeFacts:
    git: GitFacts = {
        "branch": "dev/4242",
        "fork_point": _FORK,
        "commits": [_COMMIT],
        "files": 2,
        "lines_actual": 9,
    }
    return FinalizeFacts(
        ticket_id="4242",
        worktree=_WORKTREE,
        git=git,
        tier="small",
        plan_source="generated",
        friction=friction or [],
    )


class TestParseFinalizeTurn:
    def test_a_valid_document_parses(self) -> None:
        document = parse_finalize_turn(finalize_document(pr_title="feat: widget"))
        assert isinstance(document, FinalizeTurnDocument)
        assert document.pr_title == "feat: widget"
        assert document.override_section == ""

    @pytest.mark.parametrize("content", [None, "", "  \n\t"])
    def test_a_missing_or_empty_document_is_no_result_emitted(
        self, content: str | None
    ) -> None:
        block = parse_finalize_turn(content)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == BLOCKER_REASON_NO_RESULT_EMITTED
        assert block.retry_eligible is False

    @pytest.mark.parametrize("content", ["not json", "[1, 2]", '"text"', "{}"])
    def test_a_non_document_is_no_result_emitted(self, content: str) -> None:
        block = parse_finalize_turn(content)
        assert isinstance(block, FinalizeBlock)
        assert block.reason == BLOCKER_REASON_NO_RESULT_EMITTED

    def test_an_extra_key_is_no_result_emitted(self) -> None:
        block = parse_finalize_turn(finalize_document(sentinel="x"))
        assert isinstance(block, FinalizeBlock)
        assert block.reason == BLOCKER_REASON_NO_RESULT_EMITTED

    def test_a_non_string_field_is_no_result_emitted(self) -> None:
        block = parse_finalize_turn(finalize_document(pr_body=7))
        assert isinstance(block, FinalizeBlock)
        assert block.reason == BLOCKER_REASON_NO_RESULT_EMITTED

    @pytest.mark.parametrize("title", ["", "   \n"])
    def test_an_empty_title_is_validation_failed(self, title: str) -> None:
        block = parse_finalize_turn(finalize_document(pr_title=title))
        assert isinstance(block, FinalizeBlock)
        assert block.reason == BLOCKER_REASON_VALIDATION_FAILED


class TestComposePrTitle:
    def test_a_github_numeric_id_gets_the_suffix(self) -> None:
        assert compose_pr_title("feat(x): add widget", "4242") == (
            "feat(x): add widget (#4242)"
        )

    @pytest.mark.parametrize("ticket_id", ["ABC-12", "T-1", "42a", ""])
    def test_a_non_numeric_id_gets_no_suffix(self, ticket_id: str) -> None:
        assert compose_pr_title("feat: add widget", ticket_id) == "feat: add widget"

    def test_the_suffix_is_not_doubled(self) -> None:
        assert compose_pr_title("feat: add widget (#4242)", "4242") == (
            "feat: add widget (#4242)"
        )

    def test_a_multi_line_title_collapses_to_one_line(self) -> None:
        assert compose_pr_title("feat:\n  add   widget\n", "T-1") == "feat: add widget"

    def test_a_long_title_is_truncated_so_the_whole_title_fits(self) -> None:
        title = compose_pr_title("x" * 200, "4242")
        assert len(title) == PR_TITLE_MAX_CHARS == 72
        assert title.endswith(" (#4242)")

    def test_a_long_title_without_a_suffix_is_cut_at_72(self) -> None:
        assert compose_pr_title("y" * 200, "ABC-1") == "y" * 72

    def test_truncation_never_leaves_trailing_space(self) -> None:
        raw = "a" * 71 + " " + "b" * 20
        assert compose_pr_title(raw, "ABC-1") == "a" * 71
        spaced = "a" * 63 + " " + "b" * 20
        assert compose_pr_title(spaced, "4242") == "a" * 63 + " (#4242)"


class TestShipped:
    def test_shipped_carries_the_verified_pr_and_wait_for_ci(self) -> None:
        payload = finalize_shipped(
            _facts(), pr_number=7, pr_url=_PR_URL, auto_merge=True, base="main"
        )
        assert payload["status"] == "shipped"
        assert payload["stage_reached"] == STAGE5_POST_CREATE
        assert payload["pr"] == {
            "number": 7,
            "url": _PR_URL,
            "auto_merge": True,
            "base": "main",
        }
        assert payload["next_actions"] == [WAIT_FOR_CI]
        assert payload["blocker"] is None

    def test_shipped_stamps_cw_measured_facts(self) -> None:
        payload = finalize_shipped(
            _facts(["note"]), pr_number=7, pr_url=_PR_URL, auto_merge=False, base="dev"
        )
        assert payload["ticket_id"] == "4242"
        assert payload["branch"] == "dev/4242"
        assert payload["worktree_path"] == str(_WORKTREE)
        assert payload["fork_point_sha"] == _FORK
        assert payload["commits"] == [_COMMIT]
        assert payload["scope"]["files"] == 2
        assert payload["scope"]["lines_actual"] == 9
        assert payload["friction_highlights"] == ["note"]
        assert payload["pr"]["auto_merge"] is False

    def test_the_schema_requires_a_pr_and_wait_for_ci(self) -> None:
        payload = finalize_shipped(
            _facts(), pr_number=7, pr_url=_PR_URL, auto_merge=True, base="main"
        )
        with pytest.raises(ValidationError, match="pr must be non-null"):
            AutoDevResult.model_validate({**payload, "pr": None})
        with pytest.raises(ValidationError, match="wait_for_ci"):
            AutoDevResult.model_validate({**payload, "next_actions": []})


class TestBlocked:
    def test_a_block_carries_reason_details_and_the_finalize_label(self) -> None:
        payload = finalize_blocked(
            FinalizeBlock(AGENT_BLOCK, "gate red", retry_eligible=False),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=_facts(["gate: red"]),
        )
        assert payload["status"] == "blocked"
        assert payload["stage_reached"] == STAGE4A_MERGE_GATE
        assert payload["blocker"]["reason"] == AGENT_BLOCK
        assert payload["blocker"]["details"] == "gate red"
        assert payload["blocker"]["retry_eligible"] is False
        assert payload["next_actions"] == ["user_resolve_codex_finalize_failure"]
        assert payload["branch"] == "dev/4242"
        assert payload["commits"] == [_COMMIT]
        assert payload["friction_highlights"] == ["gate: red"]

    @pytest.mark.parametrize(
        "stage", [STAGE4A_MERGE_GATE, STAGE4B_PR_CREATE, STAGE5_POST_CREATE]
    )
    def test_every_finalize_stage_gets_the_finalize_label(
        self, stage: StageReached
    ) -> None:
        payload = finalize_blocked(
            FinalizeBlock(PUSH_AUTH_FAILED, "d", stage=stage),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=None,
        )
        assert payload["stage_reached"] == stage
        assert payload["next_actions"] == ["user_resolve_codex_finalize_failure"]

    def test_without_facts_the_result_is_the_bare_blocked_result(self) -> None:
        payload = finalize_blocked(
            FinalizeBlock(CODEX_MUST_FIX_FINDINGS, "d"),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=None,
        )
        assert payload["branch"] is None
        assert payload["commits"] == []
        assert payload["worktree_path"] == str(_WORKTREE)

    def test_blocker_stage_overrides_the_wire_blocker_stage_only(self) -> None:
        payload = finalize_blocked(
            FinalizeBlock(
                CODEX_MUST_FIX_FINDINGS,
                "d",
                blocker_stage=STAGE4_MUST_FIX_OVERRIDE,
            ),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=_facts(),
        )
        assert STAGE4_MUST_FIX_OVERRIDE == "stage4_must_fix_override"
        assert payload["blocker"]["stage"] == STAGE4_MUST_FIX_OVERRIDE
        assert payload["stage_reached"] == STAGE4A_MERGE_GATE
        assert payload["blocker"]["retry_eligible"] is False

    def test_automerge_not_armed_injects_pr_info(self) -> None:
        pr_info = {"number": 7, "url": _PR_URL, "head": _COMMIT}
        payload = finalize_blocked(
            FinalizeBlock(
                AUTOMERGE_NOT_ARMED, "d", retry_eligible=True, pr_info=pr_info
            ),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=_facts(),
        )
        assert payload["pr_info"] == pr_info
        assert payload["pr_info"] is not pr_info
        assert payload["blocker"]["retry_eligible"] is True
        # ``AutoDevResult`` has no ``pr_info`` field: cw adds it after validation.
        assert "pr_info" not in AutoDevResult.model_fields
        assert "pr_info" not in AutoDevResult.model_validate(payload).model_dump()

    def test_other_blocks_carry_no_pr_info(self) -> None:
        payload = finalize_blocked(
            FinalizeBlock(AGENT_BLOCK, "d"),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=_facts(),
        )
        assert "pr_info" not in payload

    def test_the_payload_is_json_serialisable(self) -> None:
        payload = finalize_blocked(
            FinalizeBlock(AGENT_BLOCK, "d"),
            ticket_id="4242",
            worktree=_WORKTREE,
            facts=_facts(),
        )
        assert json.loads(json.dumps(payload)) == payload


class TestParks:
    def test_the_approval_park_shape(self) -> None:
        friction = ["review_approval_check: unresolved_tier"]
        payload = finalize_approval_park(
            _facts(["dropped"]), tier="large", friction=friction
        )
        assert payload["status"] == "review_pending_approval"
        assert payload["stage_reached"] == STAGE4A_MERGE_GATE
        assert payload["stage_reached"] == FINALIZE_PARK_STAGE_REACHED
        assert payload["pr"] is None
        assert payload["blocker"] is None
        assert payload["scope"]["tier"] == "large"
        assert payload["next_actions"] == [REQUEUE_STAGE_REVIEW]
        assert REQUEUE_STAGE_REVIEW == "requeue_stage_review"

    def test_the_park_friction_replaces_the_facts_friction_wholesale(self) -> None:
        friction = [
            "review_approval_check: stale",
            "review_tip_check: stale (head_moved: abc)",
        ]
        payload = finalize_approval_park(
            _facts(["earlier"]), tier="small", friction=friction
        )
        assert payload["friction_highlights"] == friction

    def test_the_tip_fetch_park_requeues_finalize(self) -> None:
        payload = finalize_approval_park(
            _facts(),
            tier="small",
            friction=["review_tip_check: tip_check_fetch_failed"],
            next_action=REQUEUE_STAGE_FINALIZE,
        )
        assert payload["next_actions"] == ["requeue_stage_finalize"]

    def test_the_park_stamps_cw_facts(self) -> None:
        payload = finalize_approval_park(_facts(), tier="small", friction=[])
        assert payload["branch"] == "dev/4242"
        assert payload["worktree_path"] == str(_WORKTREE)
        assert payload["commits"] == [_COMMIT]

    def test_the_merge_gate_block_names_the_prior_pipeline_pr(self) -> None:
        payload = finalize_merge_gate_blocked(_facts(), details="PR #3 and PR #4")
        assert payload["status"] == "merge_gate_blocked"
        assert payload["stage_reached"] == STAGE4A_MERGE_GATE
        assert payload["blocker"]["reason"] == PRIOR_PIPELINE_PR_OPEN
        assert payload["blocker"]["retry_eligible"] is True
        assert payload["blocker"]["details"] == "PR #3 and PR #4"
        assert payload["pr"] is None


class TestClassifyUnavailability:
    @pytest.mark.parametrize(
        "stderr",
        [
            "git@github.com: Permission denied (publickey).",
            "fatal: could not read Username for 'https://github.com'",
            "Host key verification failed.",
            "remote: Authentication failed for repo",
            "ssh: Could not resolve host: github.com",
            "connect: Network is unreachable",
            "fatal: unable to access: Failed to connect to github.com",
            "gh: Server Error (HTTP 502)",
            "You have exceeded a secondary rate limit",
        ],
    )
    def test_a_signature_is_push_auth_failed(self, stderr: str) -> None:
        assert classify_unavailability(stderr) == PUSH_AUTH_FAILED

    @pytest.mark.parametrize(
        "stderr",
        [
            "",
            "error: failed to push some refs",
            "! [rejected] main -> main (non-fast-forward)",
            "pre-receive hook declined",
        ],
    )
    def test_no_signature_is_none(self, stderr: str) -> None:
        assert classify_unavailability(stderr) is None

    def test_every_shared_signature_classifies(self) -> None:
        for signature, _family in UNAVAILABILITY_SIGNATURES:
            assert classify_unavailability(f"x {signature} y") == PUSH_AUTH_FAILED


class TestVocabularyLockstep:
    @pytest.mark.parametrize(
        ("constant", "literal"),
        [
            (PR_ALREADY_TERMINAL, "pr_already_terminal"),
            (MERGE_CONFLICT_POST_PUSH, "merge_conflict_post_push"),
            (SCOPE_TIER_UNRESOLVABLE, "scope_tier_unresolvable"),
        ],
    )
    def test_the_three_new_reasons_are_registered_literals(
        self, constant: str, literal: str
    ) -> None:
        assert constant == literal
        assert literal in KNOWN_BLOCKER_REASONS


# Group (b) of plan 2.8: emitted through the codex-side constants, warn-only
# unregistered as in C1 and C2.
_UNREGISTERED_BY_DESIGN = frozenset(
    {CODEX_ERROR, CODEX_MODEL_CAPACITY, "unexpected_error"}
)
_SWEPT_MODULES = (
    result_module,
    gates_module,
    pr_module,
    arm_module,
    merge_gate_module,
)
# Reasons that reach a block through a variable, not a literal call argument.
_DYNAMIC_REASONS = frozenset({PUSH_AUTH_FAILED, IMPL_NOT_PUSHED})


def _block_reasons(module: ModuleType) -> set[str]:
    """Every reason a ``FinalizeBlock(...)`` call in *module* passes first."""
    source = inspect.getsource(module)
    reasons: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FinalizeBlock"
            and node.args
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            reasons.add(first.value)
        elif isinstance(first, ast.Name) and hasattr(module, first.id):
            value = getattr(module, first.id)
            if isinstance(value, str):
                reasons.add(value)
    return reasons


class TestBlockerReasonSweep:
    def test_the_sweep_finds_the_reasons_it_is_meant_to_confirm(self) -> None:
        found: set[str] = set()
        for module in _SWEPT_MODULES:
            found |= _block_reasons(module)
        assert {
            AGENT_BLOCK,
            AUTOMERGE_NOT_ARMED,
            CODEX_MUST_FIX_FINDINGS,
            PR_ALREADY_TERMINAL,
            MERGE_CONFLICT_POST_PUSH,
            SCOPE_TIER_UNRESOLVABLE,
            "external_state_block",
            "operator_unavailable",
            "codex_fix_dirty_start",
        } <= found

    @pytest.mark.parametrize("module", _SWEPT_MODULES, ids=lambda m: m.__name__)
    def test_every_emitted_reason_is_registered(self, module: ModuleType) -> None:
        reasons = _block_reasons(module) | (
            _DYNAMIC_REASONS if module is pr_module else set()
        )
        if module is result_module:
            reasons |= {
                PRIOR_PIPELINE_PR_OPEN,
                BLOCKER_REASON_NO_RESULT_EMITTED,
                BLOCKER_REASON_VALIDATION_FAILED,
            }
        unregistered = reasons - KNOWN_BLOCKER_REASONS - _UNREGISTERED_BY_DESIGN
        assert not unregistered

    def test_the_codex_side_group_is_not_emitted_by_the_swept_modules(self) -> None:
        # Those reasons come from ``_run.py`` through the codex constants.
        for module in _SWEPT_MODULES:
            assert not (_block_reasons(module) & _UNREGISTERED_BY_DESIGN)
