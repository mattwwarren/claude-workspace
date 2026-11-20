"""A mid-review comment reaches the next review pass through the courier (#3760).

RFC 0019 T2b, acceptance 1 and 2, end to end with no fake resolver: the real
``context_for_client(refresh=True, courier=courier_refresher(runner))`` resolves
each pass by running one read-only refresh turn through a
:class:`ScriptedCodexRunner`. No real codex launches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_review import _prepare_review_pass
from cw.codex_runner import CodexRunResult
from cw.models import OrchestratorEventType
from cw.ticket_snapshot import context_for_client
from cw.tracker_io import courier_refresher
from cw.tracker_io._prompt import REFRESH_TURN_MARKER
from tests._backend_mcp_helpers import stage_mcp_config
from tests._shared.bindings_builders import bindings_linear_mcp
from tests._shared.git_repos import git_in
from tests._shared.outbox_builders import client_config
from tests._shared.ticket_context import make_snapshot_from, seeded_row
from tests._ticket_view_helpers import events_of
from tests._tool_fakes import (
    ScriptedCodexRunner,
    stdin_contains,
    tracker_io_refresh_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask
    from cw.ticket_snapshot import SnapshotContext

pytestmark = pytest.mark.usefixtures(
    "isolated_backend_layers", "project_walk_stops_at_state_dir"
)

_STEER = "COURIER-MID-REVIEW-STEER-3760"
_DEGRADED = OrchestratorEventType.TICKET_SNAPSHOT_DEGRADED


def _run(output: str | None = None, **kwargs: Any) -> CodexRunResult:
    return CodexRunResult(0, "", "", output_file_content=output, **kwargs)


def _repo(make_git_repo: Callable[[str], Path]) -> Path:
    repo = make_git_repo("wt-3760-courier")
    git_in(repo, "checkout", "-b", "feature")
    (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
    git_in(repo, "add", "mod.py")
    git_in(repo, "commit", "-m", "add mod.py")
    return repo


def _prompts(prepared: Any) -> str:
    return "\n".join(prepared.prompts_by_role.values())


class _Rig:
    """One Linear client, a seeded row, and a runner answering refresh turns."""

    def __init__(
        self,
        repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        replies: list[CodexRunResult],
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
        self.repo = repo
        self.runner = ScriptedCodexRunner()
        for reply in replies:
            self.runner.add(stdin_contains(REFRESH_TURN_MARKER), reply, times=1)
        self.task: TicketTask = seeded_row(make_snapshot_from([], title="STORED-TITLE"))
        client = client_config(
            tmp_path,
            bindings=bindings_linear_mcp(operator_ids=["op-1"]),
            tracker="linear",
        )
        self.ctx: SnapshotContext = context_for_client(
            client,
            repo,
            resolve_login=False,
            refresh=True,
            courier=courier_refresher(self.runner),
        )

    def run_pass(self) -> Any:
        return _prepare_review_pass(
            self.task,
            self.repo,
            "main",
            config_branch="main",
            runner=self.runner,
            session_id="s-3760",
            snapshots=self.ctx,
        )


def _first() -> CodexRunResult:
    return _run(tracker_io_refresh_result(title="FIRST-TITLE-3760"))


def _second(author_id: str = "op-1") -> CodexRunResult:
    return _run(
        tracker_io_refresh_result(
            title="SECOND-TITLE-3760",
            comments=[{"id": "c9", "author_id": author_id, "body": _STEER}],
        )
    )


def test_a_mid_review_comment_reaches_the_next_pass(
    make_git_repo: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(_repo(make_git_repo), tmp_path, monkeypatch, [_first(), _second()])

    before = _prompts(rig.run_pass())
    after = _prompts(rig.run_pass())

    assert "FIRST-TITLE-3760" in before
    assert _STEER not in before
    assert "SECOND-TITLE-3760" in after
    assert _STEER in after
    assert events_of(_DEGRADED) == []
    assert len(rig.runner.review_calls) == 2


def test_a_failed_second_turn_keeps_the_first_snapshot_and_does_not_raise(
    make_git_repo: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(
        _repo(make_git_repo),
        tmp_path,
        monkeypatch,
        [_first(), _run(timed_out=True)],
    )

    rig.run_pass()
    after = _prompts(rig.run_pass())

    assert "FIRST-TITLE-3760" in after
    assert _STEER not in after
    (event,) = events_of(_DEGRADED)
    assert event.payload["kind"] == "refresh_failed"
    assert event.payload["reason"] == "turn_failed"


def test_a_non_operator_comment_does_not_bind(
    make_git_repo: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(
        _repo(make_git_repo),
        tmp_path,
        monkeypatch,
        [_first(), _second(author_id="someone-else")],
    )

    rig.run_pass()
    after = _prompts(rig.run_pass())

    assert "SECOND-TITLE-3760" in after
    assert _STEER not in after
