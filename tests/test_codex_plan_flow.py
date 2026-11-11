"""The large-tier two-run codex PLAN flow with a non-echoing model (#3060 R12).

Run 1 parks a large plan for approval and persists the draft with its
fingerprint; the operator approves that fingerprint on the row; run 2's model
returns a DIFFERENT text, and cw takes the approved draft verbatim. The
helpers are the run tests' own (``tests.test_codex_plan_run``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.codex_plan._result import (
    MODEL_TEXT_DISCARDED_FRICTION,
    PLAN_MD_RELATIVE_PATH,
    RESUME_FRICTION,
    ROUND_ZERO_LINE,
)
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.models import Stage
from cw.plan_fingerprint import PLAN_DRAFT_RELATIVE_PATH, compute_plan_draft_fingerprint
from cw.reconcile.codex_boot import _worktree_porcelain_clean_except_verdict
from tests._codex_review_helpers import exclude_cw_dir, seed_codex_stage_run
from tests._tool_fakes import FakeGhCli, ScriptedCodexRunner
from tests.test_codex_plan_run import (
    LARGE_PLAN,
    SID,
    TICKET,
    Harness,
    add_stations,
    add_turn,
    script_gh,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

P1 = LARGE_PLAN.replace("Add the widget.", "P1-DISTINCTIVE: add the widget.")
P2 = LARGE_PLAN.replace("Add the widget.", "P2-REGENERATED: build a new widget.")
_LAST_EVALUATED = (
    "<!-- plan-stage-last-evaluated: operator_comment=none|body_sha="
    + "a" * 64
    + " -->\n"
)


@pytest.fixture
def flow(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Harness]:
    """One worktree; each call re-seeds the row and scripts a fresh run."""
    del tmp_config_dir
    worktree = make_git_repo("wt-flow")
    exclude_cw_dir(worktree)

    def _run(plan: str, *, task_updates: Mapping[str, object] | None = None) -> Harness:
        seed_codex_stage_run(
            worktree=worktree,
            stage=Stage.PLAN,
            ticket_id=TICKET,
            session_id=SID,
            task_updates=task_updates,
        )
        gh = script_gh(FakeGhCli().install(monkeypatch))
        runner = add_stations(add_turn(ScriptedCodexRunner(), plan, tier="large"))
        return Harness(worktree=worktree, runner=runner, gh=gh)

    return _run


def _approve(fingerprint: str) -> dict[str, object]:
    return {
        "plan_approved_at": datetime(2026, 11, 10, tzinfo=UTC),
        "plan_approved_fingerprint": fingerprint,
    }


def _run1(flow: Callable[..., Harness]) -> tuple[Harness, AutoDevResult]:
    h = flow(P1)
    result = h.run()
    return h, result


def _draft(h: Harness) -> str:
    return (h.worktree / PLAN_DRAFT_RELATIVE_PATH).read_text(encoding="utf-8")


def test_approved_draft_is_taken_verbatim_over_a_regenerated_plan(
    flow: Callable[..., Harness],
) -> None:
    h1, run1 = _run1(flow)
    assert run1.status == "plan_pending_approval"
    draft = _draft(h1)
    assert draft.splitlines()[0] == ROUND_ZERO_LINE.rstrip("\n")
    f1 = run1.plan_draft_fingerprint
    assert f1 == compute_plan_draft_fingerprint(draft)
    assert not (h1.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert _worktree_porcelain_clean_except_verdict(h1.worktree) is True

    assert f1 is not None
    h2 = flow(P2, task_updates=_approve(f1))
    run2 = h2.run()
    (call,) = h2.plan_calls
    stdin = str(call["stdin"])
    assert "P1-DISTINCTIVE" in stdin
    assert "approval: matches" in stdin
    assert run2.status == "stage_complete"
    plan = (h2.worktree / PLAN_MD_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "P1-DISTINCTIVE" in plan
    assert "P2-REGENERATED" not in plan
    assert plan.endswith(P1)
    assert _plan_body_signoff_ok(plan)
    assert MODEL_TEXT_DISCARDED_FRICTION in run2.friction_highlights
    assert RESUME_FRICTION in run2.friction_highlights
    assert not (h2.worktree / PLAN_DRAFT_RELATIVE_PATH).exists()
    assert run2.plan_draft_fingerprint == f1
    assert _worktree_porcelain_clean_except_verdict(h2.worktree) is True


def test_without_approval_the_model_text_reparks(flow: Callable[..., Harness]) -> None:
    _run1(flow)
    h2 = flow(P2)
    run2 = h2.run()
    (call,) = h2.plan_calls
    assert "approval: none" in str(call["stdin"])
    assert run2.status == "plan_pending_approval"
    assert "P2-REGENERATED" in _draft(h2)
    assert run2.plan_draft_fingerprint == compute_plan_draft_fingerprint(_draft(h2))


def test_a_stale_approval_reparks_quoting_both_fingerprints(
    flow: Callable[..., Harness],
) -> None:
    _, run1 = _run1(flow)
    stale = "c" * 64
    h2 = flow(P2, task_updates=_approve(stale))
    run2 = h2.run()
    assert run2.status == "plan_pending_approval"
    assert run1.plan_draft_fingerprint != stale
    (post,) = h2.posts()
    comment = (post.stdin or b"").decode("utf-8")
    assert "fingerprint mismatch" in comment
    assert stale in comment
    assert str(run2.plan_draft_fingerprint) in comment


def test_an_on_disk_leading_block_survives_the_rewrite(
    flow: Callable[..., Harness],
) -> None:
    h = flow(P1)
    leading = "<!-- plan-stage-scan-round: 1 -->\n" + _LAST_EVALUATED
    path = h.worktree / PLAN_DRAFT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(leading + "older body\n", encoding="utf-8")
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert _draft(h) == leading + P1
