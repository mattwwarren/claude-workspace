"""``tool_denied`` dead-session park recipe (#2617).

1:1 test module of the provenance branch in
``_check_wedge_dead_session_blocked_on_user`` (``cw.doctor.wedge.blocked_on_user``).
A ``tool_denied`` row keeps its class-5 finding and reap selection, but its
recipe points at the Comment provenance rule instead of the generic reap hint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result.schema import KNOWN_BLOCKER_REASONS
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.dev_queue.dispositions import FINALIZE_GATE_HELD_DISPOSITION
from cw.doctor import WedgeFinding
from cw.doctor.wedge._constants import (
    _TOOL_DENIED_BLOCKER_REASON,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_OPERATOR_GATED_PARK,
)
from cw.doctor.wedge.blocked_on_user import (
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_operator_gated_park,
)
from cw.models import DevQueueStore
from tests._doctor_helpers import stub_daemon_roster
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    make_blocked,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

_GENERIC_RECIPE = (
    "BLOCKED_ON_USER task with dead session holds lane slot. "
    "Run: cw doctor --reap to revert task to PENDING."
)


def _dead_findings() -> list[WedgeFinding]:
    return list(
        _check_wedge_dead_session_blocked_on_user(load_state(), load_dev_queue())
    )


def test_tool_denied_pin() -> None:
    """Drift guard: the doctor-local constant mirrors the schema reason literal."""
    assert _TOOL_DENIED_BLOCKER_REASON == "tool_denied"
    assert _TOOL_DENIED_BLOCKER_REASON in KNOWN_BLOCKER_REASONS


def test_tool_denied_dead_session_row_gets_provenance_recipe(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_daemon_roster(tmp_path, monkeypatch)
    task = make_blocked(
        session_id="sess-gone",
        blocked_reason=_TOOL_DENIED_BLOCKER_REASON,
        attempts=3,
    )
    seed_queue([task])

    (finding,) = _dead_findings()
    assert finding.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
    recipe = finding.recipe
    assert "tool_denied" in recipe
    assert "Comment provenance rule" in recipe
    assert f"cw dev-queue resolve {ACME_TICKET} -c {ACME}" in recipe
    assert f"cw dev-queue requeue {ACME_TICKET} -c {ACME}" in recipe
    assert "3 attempt(s)" in recipe
    assert "reap" not in recipe


@pytest.mark.parametrize("reason", [None, "agent_block", "plan_unsound"])
def test_other_blocked_reasons_keep_the_generic_recipe(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str | None,
) -> None:
    stub_daemon_roster(tmp_path, monkeypatch)
    seed_queue([make_blocked(session_id="sess-gone", blocked_reason=reason)])

    (finding,) = _dead_findings()
    assert finding.recipe == _GENERIC_RECIPE


def test_tool_denied_row_with_live_session_is_not_a_finding(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead-session heuristic is unchanged: a live surface is not a finding."""
    daemon = stub_daemon_roster(tmp_path, monkeypatch)
    surface_ref = daemon.seed_live_worker(tmp_path)
    task = make_blocked(blocked_reason=_TOOL_DENIED_BLOCKER_REASON)
    seed_queue([task], [bound_session_for(task, surface_ref=surface_ref)])

    assert _dead_findings() == []


def test_tool_denied_row_still_excluded_when_operator_gated(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-gated disposition wins: class 5 excludes it, class 13 advises it."""
    stub_daemon_roster(tmp_path, monkeypatch)
    task = make_blocked(
        session_id=None,
        disposition=FINALIZE_GATE_HELD_DISPOSITION,
        blocked_reason=_TOOL_DENIED_BLOCKER_REASON,
    )
    seed_queue([task])

    assert _dead_findings() == []
    advisory = _check_wedge_operator_gated_park(
        load_state(), DevQueueStore(tasks=[task])
    )
    assert [f.wedge_class for f in advisory] == [_WEDGE_OPERATOR_GATED_PARK]


def test_reap_still_selects_the_same_row_set(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization (#2617): a hint-only change. The finding class, and so the
    reap selector, is unchanged for a ``tool_denied`` dead-session row."""
    stub_daemon_roster(tmp_path, monkeypatch)
    seed_queue(
        [
            make_blocked(
                session_id="sess-gone", blocked_reason=_TOOL_DENIED_BLOCKER_REASON
            )
        ]
    )

    (finding,) = _dead_findings()
    assert finding.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
    assert finding.ticket_id == ACME_TICKET
