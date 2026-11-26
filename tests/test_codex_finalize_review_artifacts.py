"""Step 4d through the codex FINALIZE driver core (#3543).

The same harness as ``test_codex_finalize_run.py``: a real sandboxed worktree on
a bare origin, a scripted codex runner and ``gh``, and a scripted ``cw`` CLI.
These tests cover what the artifacts step does to the run: the create-time
body, the one ``gh pr edit``, the debt block and the UI-evidence hold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.review_adjudication import REVIEW_ADJUDICATION_HEADING, Adjudication
from cw.stage_verification.finalize_pr import apply_body_section
from cw.stage_verification.finalize_review_artifacts import adjudication_section
from tests._review_debt_helpers import write_deferred_findings
from tests._shared.outbox_builders import entries_for
from tests._tool_fakes import GhReply, finalize_document
from tests.test_codex_finalize_conflicts import _branch_commit
from tests.test_codex_finalize_run import (
    BODY,
    BRANCH,
    PR_URL,
    TICKET,
    Harness,
    _arm_names,
    _assert_block,
    _gates_ran,
    _logged_gate,
    _pr_list,
    _run_handing,
    _set_head,
    harness,
    stage_events,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["harness"]

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_START = "<!-- cw-review-signoff:start -->"
_END = "<!-- cw-review-signoff:end -->"
_BLOCK = f"{_START}\n## Review sign-off\n{_END}"
_UI = "frontends/app/X.tsx"
_FINDING = "add alt text to `<img>`"
_BODY_FIELDS = "body,headRefOid"
_HOLD_ACTIONS = ["wait_for_ci", "attach_ui_evidence_and_enable_automerge"]
_ARMED_HOLD_ACTIONS = ["wait_for_ci", "attach_ui_evidence"]
_CLIS = ("review debt file", "review signoff render", "review debt link-pr")


def _defer(summary: str = "Helper swallows the error") -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/real.py",
        summary=summary,
        outcome="defer",
        rationale="Out of scope",
    )


def _signoff_block(args: list[str]) -> GhReply:
    """A sign-off reply: the ``--body-file`` text plus the sign-off block."""
    body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
    return GhReply(0, f"{body.rstrip()}\n\n{_BLOCK}\n")


def _view_body(h: Harness, body: str | None) -> None:
    """Script the Step 4d ``body,headRefOid`` read; ``None`` fails it."""
    if body is None:
        h.gh.reply("pr", "view", PR_URL, "--json", _BODY_FIELDS, returncode=1)
        return
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        _BODY_FIELDS,
        stdout=json.dumps({"body": body, "headRefOid": h.head}),
    )


def _edits(h: Harness) -> list[str]:
    return [r.body_file or "" for r in h.gh.records if r.argv[1:3] == ["pr", "edit"]]


def _cw_calls(h: Harness) -> list[str]:
    return [" ".join(c[:3]) for c in h.cw.calls if " ".join(c[:3]) in _CLIS]


def _reuse(h: Harness) -> None:
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))


def _ui_unarmed(h: Harness) -> None:
    """A UI commit on the branch, read back as an unarmed PR."""
    _branch_commit(h, _UI, "export const X = 1\n")
    _set_head(h, autoMergeRequest=None)


def _artifact_view(h: Harness, summary: str = "Helper swallows the error") -> str:
    """The deferred artifact and a PR body that already carries its section."""
    path = write_deferred_findings(h.worktree, _defer(summary))
    return apply_body_section(
        BODY,
        adjudication_section(path),
        heading=REVIEW_ADJUDICATION_HEADING,
    )


# --- the create-time body and the one edit ------------------------------------


def test_a_create_run_copies_the_adjudication_and_writes_one_edit(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.set_cw("review signoff render", _signoff_block)
    write_deferred_findings(h.worktree, _defer())
    h.turn(finalize_document(pr_body=BODY))
    result = h.run()
    assert result.status == "shipped"
    (created,) = h.created()
    assert created.body_file is not None
    assert created.body_file.count(REVIEW_ADJUDICATION_HEADING) == 1
    assert "Helper swallows the error" in created.body_file
    assert "# Deferred Review Findings" not in created.body_file
    (edit,) = _edits(h)
    assert edit.count(REVIEW_ADJUDICATION_HEADING) == 1
    assert _START in edit
    assert _cw_calls(h) == list(_CLIS)


def test_the_closes_line_precedes_every_cw_section(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.set_cw("review signoff render", _signoff_block)
    write_deferred_findings(h.worktree, _defer())
    h.turn(finalize_document(pr_body=BODY))
    result = h.run()
    assert result.status == "shipped"
    (created,) = h.created()
    assert created.body_file is not None
    assert created.body_file.startswith(f"Closes #{TICKET}\n\n")
    assert created.body_file.count(REVIEW_ADJUDICATION_HEADING) == 1


def test_the_one_edit_keeps_the_closes_line_first(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.set_cw("review signoff render", _signoff_block)
    write_deferred_findings(h.worktree, _defer())
    h.turn(finalize_document(pr_body=BODY))
    # The default Step 4d read returns BODY with no Closes line; override it so
    # the edit path reads back a body already led by the line.
    _view_body(h, f"Closes #{TICKET}\n\n{BODY}")
    h.run()
    (edit,) = _edits(h)
    assert edit.startswith(f"Closes #{TICKET}\n\n")
    assert edit.count(f"Closes #{TICKET}") == 1


def test_a_ci_deferred_park_keeps_the_adjudication_in_the_created_body(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(gates=_logged_gate(tmp_path, "lint"))
    h.gates.defer(("lint", ("ci-lint",)))
    write_deferred_findings(h.worktree, _defer())
    h.turn(finalize_document(pr_body=BODY))
    result = h.run()
    _assert_block(result, "awaiting_ci_evidence", stage="stage5_post_create")
    (created,) = h.created()
    assert created.body_file is not None
    assert "DEFERRED-REVIEW-FINDINGS" in created.body_file
    assert _cw_calls(h) == []
    assert _gates_ran(tmp_path) == []


def test_a_fixture_only_artifact_puts_no_section_in_the_body(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    write_deferred_findings(
        h.worktree,
        Adjudication(
            severity="SHOULD_FIX",
            file="src/cw/foo.py",
            summary="fixture",
            outcome="defer",
            rationale="r",
        ),
    )
    h.turn(finalize_document(pr_body=BODY))
    h.run()
    (created,) = h.created()
    assert created.body_file is not None
    assert REVIEW_ADJUDICATION_HEADING not in created.body_file


def test_a_reuse_run_writes_the_adjudication_with_no_turn(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.set_cw("review signoff render", _signoff_block)
    _reuse(h)
    write_deferred_findings(h.worktree, _defer())
    result = h.run()
    assert result.status == "shipped"
    assert h.runner.calls == []
    assert h.created() == []
    (edit,) = _edits(h)
    assert edit.count(REVIEW_ADJUDICATION_HEADING) == 1
    assert _START in edit


def test_a_rerun_over_a_complete_body_makes_no_edit(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _reuse(h)
    _view_body(h, _artifact_view(h))
    result = h.run()
    assert result.status == "shipped"
    assert _edits(h) == []
    assert "arm-automerge" in _arm_names(h)


def test_a_merged_pr_runs_none_of_the_three_clis(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("MERGED"))
    result = h.run()
    assert result.status == "shipped"
    assert _cw_calls(h) == []
    assert _edits(h) == []


# --- the review-debt block ------------------------------------------------------


def _fail_debt(h: Harness, status: str = "incomplete") -> None:
    h.set_cw(
        "review debt file",
        GhReply(1, json.dumps({"status": status, "unticketed": 2}), ""),
    )


def test_a_debt_failure_on_a_create_run_blocks_before_the_arm(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    _fail_debt(h)
    result, payload = _run_handing(h)
    _assert_block(
        result,
        "review_debt_unfiled",
        stage="stage5_post_create",
        blocker_stage="stage4_review_debt",
    )
    assert result.blocker is not None
    assert result.blocker.retry_eligible is True
    assert len(h.created()) == 1
    assert h.arm.calls() == []
    assert payload["pr_info"]["number"] == 7
    assert payload["pr_info"]["auto_merge"] is True
    assert _cw_calls(h) == ["review debt file"]


def test_a_debt_failure_reports_an_unarmed_pr_as_unarmed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    _set_head(h, autoMergeRequest=None)
    _fail_debt(h)
    _result, payload = _run_handing(h)
    assert payload["pr_info"]["auto_merge"] is False
    assert h.arm.calls() == []


def test_a_debt_failure_on_an_armed_reused_pr_leaves_it_armed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _reuse(h)
    _fail_debt(h, "mcp_pending")
    result, payload = _run_handing(h)
    _assert_block(
        result,
        "review_debt_unfiled",
        stage="stage5_post_create",
        blocker_stage="stage4_review_debt",
    )
    assert payload["pr_info"]["auto_merge"] is True
    assert _arm_names(h) == ["ci-hold"]
    assert not any(a[1:3] == ["pr", "merge"] for a in h.gh.argvs())
    assert result.blocker is not None
    assert "cw review debt record" in result.blocker.details


# --- the UI-evidence hold ---------------------------------------------------------


def test_a_ui_hold_on_a_create_run_ships_unarmed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    _ui_unarmed(h)
    result, payload = _run_handing(h)
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is False
    assert result.next_actions == _HOLD_ACTIONS
    assert "ui_evidence_missing" in result.friction_highlights
    assert h.arm.calls() == []
    assert payload["status"] == "shipped"
    (produced,) = entries_for("test", ticket=TICKET, producer="codex-pr-link")
    assert PR_URL in "".join(produced.parts)


def test_a_ui_hold_on_an_already_armed_reused_pr_does_not_disarm(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _reuse(h)
    _branch_commit(h, _UI, "export const X = 1\n")
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is True
    assert "ui_evidence_missing" in result.friction_highlights
    assert "ui_evidence_missing_on_armed_pr" in result.friction_highlights
    assert result.next_actions == _ARMED_HOLD_ACTIONS
    assert _arm_names(h) == ["ci-hold"]
    assert not any(a[1:3] == ["pr", "merge"] for a in h.gh.argvs())


def test_finding_text_is_not_evidence_on_a_create_run(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    _ui_unarmed(h)
    body = _artifact_view(h, _FINDING)
    assert "<img" in body
    _view_body(h, body)
    result = h.run()
    assert result.status == "shipped"
    assert result.next_actions == _HOLD_ACTIONS
    assert h.arm.calls() == []
    assert "ui_evidence_missing" in result.friction_highlights


def test_finding_text_is_not_evidence_on_a_reused_unarmed_pr(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _reuse(h)
    _ui_unarmed(h)
    body = _artifact_view(h, _FINDING)
    assert "<img" in body
    _view_body(h, body)
    result = h.run()
    assert result.next_actions == _HOLD_ACTIONS
    assert _arm_names(h) == ["ci-hold"]


def test_real_media_in_the_description_clears_the_gate(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _reuse(h)
    _branch_commit(h, _UI, "export const X = 1\n")
    section = _artifact_view(h, _FINDING).removeprefix(BODY)
    _view_body(h, f"![shot](https://x/y.png)\n\n{BODY}{section}")
    result = h.run()
    assert result.status == "shipped"
    assert not any(f.startswith("ui_evidence") for f in result.friction_highlights)
    assert "arm-automerge" in _arm_names(h)
    assert result.next_actions == ["wait_for_ci"]


def test_a_failed_body_read_with_a_ui_diff_holds_the_arm(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    _ui_unarmed(h)
    _view_body(h, None)
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is False
    assert result.next_actions == _HOLD_ACTIONS
    assert "ui_evidence_unreadable" in result.friction_highlights
    assert (
        "friction: review_signoff: skipped_pr_head_unreadable"
        in result.friction_highlights
    )
    assert h.arm.calls() == []
    assert _edits(h) == []


def test_a_failed_body_read_with_only_a_signoff_to_write_ships(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    _view_body(h, None)
    result = h.run()
    assert result.status == "shipped"
    assert (
        "friction: review_signoff: skipped_pr_head_unreadable"
        in result.friction_highlights
    )
    assert _edits(h) == []
    assert "review signoff render" not in _cw_calls(h)
    assert "arm-automerge" in _arm_names(h)


def test_a_failed_body_read_with_a_deferred_artifact_blocks(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    write_deferred_findings(h.worktree, _defer())
    _view_body(h, None)
    result = h.run()
    _assert_block(result, "operator_unavailable", stage="stage5_post_create")
    assert h.arm.calls() == []
    assert _edits(h) == []


def test_a_ui_hold_with_an_unreadable_read_back_blocks(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    _branch_commit(h, _UI, "export const X = 1\n")
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
    assert h.arm.calls() == []


def test_an_unresolved_fork_point_holds_the_arm(
    harness: Callable[..., Harness],
) -> None:
    from cw.codex_finalize import _run

    h = harness()
    h.turn(finalize_document(pr_body=BODY))
    real: Any = _run.git_facts

    def _unresolved(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**real(*args, **kwargs), "fork_point": ""}

    h.monkeypatch.setattr("cw.codex_finalize._run.git_facts", _unresolved)
    _set_head(h, autoMergeRequest=None)
    result = h.run()
    assert result.status == "shipped"
    assert result.pr is not None
    assert result.pr.auto_merge is False
    assert "ui_evidence_unreadable" in result.friction_highlights
    assert result.next_actions == _HOLD_ACTIONS
    assert h.arm.calls() == []


def test_the_stage_events_stay_one_errored_event_on_a_debt_block(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    _fail_debt(h)
    h.run()
    assert [e[0] for e in stage_events()].count("stage.errored") == 1
