"""Acceptance-level proofs for the red-shipped-PR recovery (GitHub #2945).

"A green PR behaves exactly as today" and "a fix after a clean review round is
still re-reviewed" are claims about the whole pipeline, not one module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import load_dev_queue
from cw.dev_queue.lifecycle import _advance_task_pointer
from cw.dev_queue.red_pr_recovery import RedPrRecovery, recover_red_pr_ticket
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    OrchestratorConfig,
    PrState,
    Stage,
)
from cw.pr_hydrate import hydrate_pr_states
from cw.shipped_pr_signal import scan_shipped_red_prs
from tests._adopt_pr_helpers import (
    ADOPT_PR_URL,
    FIXTURE_FAILING_CLOSED,
    load_adopt_fixture,
    make_repair_feedback,
)
from tests._shipped_red_helpers import HEAD_SHA, install_red_gh, make_red_row
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

STAGES = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)


def test_green_pr_hydrates_without_a_latch_and_the_sweep_makes_no_gh_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head, req = install_red_gh(monkeypatch)
    green = load_adopt_fixture(FIXTURE_FAILING_CLOSED, state="OPEN")
    green["statusCheckRollup"] = []  # mutated from capture: no failing checks
    monkeypatch.setattr("cw.pr_hydrate.fetch_pr_view", lambda *_a, **_k: green)
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
    seed_queue([make_red_row(pr_state=PrState(state="OPEN", ci_ok=True))])
    config = OrchestratorConfig(pr_hydration_interval_seconds=0)

    hydrate_pr_states(config)
    scan_shipped_red_prs(config)

    row = load_dev_queue().tasks[0]
    assert row.pr_state is not None
    assert row.pr_state.ci_ok is True
    assert row.shipped_red is None
    assert (head.log, req.calls) == ([], 0)


def test_recovered_row_walks_impl_review_finalize_and_loses_its_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_red_row(
                review_approved_at=datetime(2026, 11, 1, tzinfo=UTC),
                review_approver_kind="operator",
                review_approved_sha="c" * 40,
            )
        ]
    )
    bundle = RedPrRecovery(
        pr_url=ADOPT_PR_URL,
        repo="acme/widgets",
        pr_number=7,
        head_sha=HEAD_SHA,
        checks=("x",),
        feedback=make_repair_feedback(reviewed_sha=HEAD_SHA),
        was_armed=True,
        stages=STAGES,
        backend=CLAUDE_NATIVE_BACKEND,
        lane="default",
    )

    recover_red_pr_ticket(ACME_TICKET, ACME, bundle, native_daemon=daemon)

    row = load_dev_queue().tasks[0]
    assert row.stage is Stage.IMPL
    # Finalize Step 4.0 needs the approval recorded on the row; it is gone.
    assert (
        row.review_approved_at,
        row.review_approver_kind,
        row.review_approved_sha,
    ) == (None, None, None)
    _advance_task_pointer(row, list(STAGES))
    assert row.stage is Stage.REVIEW
    _advance_task_pointer(row, list(STAGES))
    assert row.stage is Stage.FINALIZE
