"""The codex review thread stamps stage facts against the row's base (RFC 0020 S3b).

``_run_codex_review_and_complete`` hands ``stamp_stage_facts`` the row's merge
target (``resolve_base_branch(client, task)``), while the fix loop's
``default_branch`` stays on ``client.default_branch``: the review configuration is
the trust root (ADR 0024).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.codex_background import _run_codex_review_and_complete
from cw.config import save_state
from cw.local_runner import make_blocked
from cw.models import ClientConfig, CwState, Stage, TicketTask
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunResult


class _UnusedRunner:
    """CodexRunner stand-in: the fix loop is patched out, so run is never reached."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        raise AssertionError(self.run.__doc__)


@pytest.fixture(autouse=True)
def _hermetic_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` launch for the operator login or a snapshot refresh."""
    monkeypatch.setattr(
        "cw.ticket_snapshot._binding.resolve_operator_login",
        MagicMock(return_value="operator-login"),
    )
    monkeypatch.setattr(
        "cw.ticket_snapshot._fetch.fetch_issue_snapshot", lambda *_a, **_kw: None
    )


@pytest.mark.parametrize(
    ("row_base", "expected_base"),
    [("epic/x", "epic/x"), (None, "main")],
    ids=["based-row", "null-row"],
)
def test_review_thread_stamps_stage_facts_against_the_rows_base(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    row_base: str | None,
    expected_base: str,
) -> None:
    """stamp_stage_facts gets the row's base; the fix loop keeps the trust root."""
    worktree = make_git_repo("wt-bg-base")
    save_state(CwState(sessions=[_make_daemon_session(id="bg-base", client="test")]))
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(
        ticket_id="T-base", client="test", stage=Stage.REVIEW, base_branch=row_base
    )
    result = make_blocked(
        ticket_id="T-base",
        worktree=worktree,
        reason="codex_review_unparseable",
        stage_reached="stage3_review",
    )

    def _passthrough(res: AutoDevResult, **_kw: object) -> AutoDevResult:
        return res

    with (
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            return_value=(result, None),
        ) as fix_loop_mock,
        patch(
            "cw.codex_background.stamp_stage_facts", side_effect=_passthrough
        ) as stamp_spy,
        patch("cw.codex_background._record_orchestrator_event"),
        patch("cw.codex_background._post_daemon_verdict_comment"),
    ):
        _run_codex_review_and_complete(
            runner=_UnusedRunner(),
            task=task,
            worktree=worktree,
            client=client,
            wall_clock_budget_seconds=None,
            sid="bg-base",
            sess_name="test/auto-dev/T-base",
            config_model=None,
            config_reasoning_effort=None,
        )

    stamp_spy.assert_called_once()
    assert stamp_spy.call_args.kwargs["base_branch"] == expected_base
    fix_kwargs = fix_loop_mock.call_args.kwargs
    assert fix_kwargs["default_branch"] == client.default_branch
    assert fix_kwargs["base_branch"] == expected_base
