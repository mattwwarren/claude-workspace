"""Acceptance: the unresolved-ref park fires for the REVIEW session (#2651).

``fix_dispatch`` parks a row whose remote ref cannot resolve only when the
row's stamped ``session_id`` equals the handoff's ``requested_by_session_id``
(``queue_rows``' ``expected_session_id`` guard). The handoff used to carry the
ambient ``$CW_SESSION``, which is empty or stale in every fence after intake,
so ``""`` mismatched the stamp and the park was silently skipped. The handoff
is now recorded by ``cw dev-queue record-fix-handoff`` from ``cw-context.json``;
this drives the whole path -- record, then ``_act_on_pending_fix_dispatches`` --
and asserts the park fires whatever ``CW_SESSION`` holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.events import read_events
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile import fix_dispatch
from tests._cli_review_helpers import _write_session_context
from tests._reconcile_helpers import (
    _FIX_LOOP_CLIENT,
    _FIX_LOOP_TICKET,
    _only_task,
    _raise_unresolved,
    _seed_task,
    install_stub_dispatch,
    make_fix_loop_client,
)

if TYPE_CHECKING:
    from pathlib import Path

#: The session dispatch stamped on the row and wrote into cw-context.json.
_REVIEW_SESSION = "428de73a"
_STALE_CW_SESSION = "stale-shared"
_FIX_CYCLE = "1"


@pytest.mark.parametrize("cw_session", [None, _STALE_CW_SESSION])
def test_unresolved_ref_park_fires_for_recording_session_despite_stale_cw_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cw_session: str | None,
) -> None:
    client = make_fix_loop_client(tmp_path, monkeypatch)
    recorder = install_stub_dispatch(monkeypatch)
    if cw_session is None:
        monkeypatch.delenv("CW_SESSION", raising=False)
    else:
        monkeypatch.setenv("CW_SESSION", cw_session)

    _seed_task(session_id=_REVIEW_SESSION)

    # The REVIEW session's worktree: dispatch wrote this context, and a nearer
    # one than any context the suite itself may run under.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_context(
        worktree,
        headless=True,
        client=_FIX_LOOP_CLIENT,
        ticket_id=_FIX_LOOP_TICKET,
        session_id=_REVIEW_SESSION,
    )
    prompt = worktree / "fix-agent-prompt.md"
    prompt.write_text("fix the MUST_FIX items\n", encoding="utf-8")
    monkeypatch.chdir(worktree)

    recorded = CliRunner().invoke(
        main,
        [
            "dev-queue",
            "record-fix-handoff",
            "--cycle",
            _FIX_CYCLE,
            "--prompt-file",
            str(prompt),
        ],
        catch_exceptions=False,
    )
    assert recorded.exit_code == 0, recorded.stderr

    recorder.side_effect = _raise_unresolved
    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [
            fix_dispatch._FixDispatchCandidate(
                ticket_id=_FIX_LOOP_TICKET, client=_FIX_LOOP_CLIENT
            )
        ],
        clients={_FIX_LOOP_CLIENT: client},
    )

    assert acted == []
    task = _only_task()
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "fix_dispatch_ref_unresolved"
    assert task.pending_fix_dispatch is not None
    assert task.pending_fix_dispatch.requested_by_session_id == _REVIEW_SESSION
    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].payload["session_id"] == _REVIEW_SESSION
