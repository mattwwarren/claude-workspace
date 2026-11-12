"""Tests for ``cw.reconcile.harvest_usage``: the opencode/local marker (#3063, R9).

No opencode output is read anywhere: no real capture exists, so the stamp is a
pure in-memory assignment from data the harvest already holds. The real reader
is follow-up #3552.
"""

from __future__ import annotations

import ast
import builtins
import logging
import subprocess
from pathlib import Path

import pytest

from cw._lock_guard import LockRank, is_rank_held
from cw.models import LocalLivenessHandle, Session
from cw.models.usage import (
    USAGE_UNAVAILABLE_FRICTION_PREFIX,
    SessionUsage,
    UsageState,
    UsageUnavailableReason,
)
from cw.reconcile.harvest_usage import (
    stamp_backend_usage_unavailable,
    usage_unavailable_friction,
)
from tests._shared.session_seeding import _make_daemon_session

_DETAIL = "no pinned output shape for this backend; see #3552"
_SRC = Path(__file__).resolve().parents[1] / "src" / "cw" / "reconcile"
_LOGGER = "cw.reconcile.harvest_usage"


def _session(backend: str | None, **overrides: object) -> Session:
    handle = (
        None
        if backend is None
        else LocalLivenessHandle.model_validate(
            {"pid": 1, "start_time_ns": 1, "backend": backend}
        )
    )
    return _make_daemon_session(
        id="sid-hv",
        # A worktree that does not exist: any file read would fail.
        worktree_path=Path("/nonexistent/worktree"),
        local_liveness=handle,
        **overrides,
    )


@pytest.mark.parametrize("backend", ["opencode", "aider"])
def test_stamp_marks_usage_unavailable_with_one_warning(
    backend: str, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(backend)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        stamp_backend_usage_unavailable(session, "T-1")

    assert session.usage == SessionUsage(
        state=UsageState.UNAVAILABLE,
        unavailable_reason=UsageUnavailableReason.BACKEND_OUTPUT_UNPINNED,
    )
    assert session.usage.unavailable_turns == 0
    assert session.cost_breakdown is None
    messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert messages == [
        f"usage_unavailable: role={backend} session=sid-hv ticket=T-1 "
        "stage=session reason=backend_output_unpinned"
    ]


def test_a_missing_ticket_id_is_named_none(caplog: pytest.LogCaptureFixture) -> None:
    session = _session("opencode")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        stamp_backend_usage_unavailable(session, None)

    assert "ticket=none " in caplog.messages[-1]


def test_second_stamp_is_a_no_op(caplog: pytest.LogCaptureFixture) -> None:
    session = _session("opencode")
    stamp_backend_usage_unavailable(session, "T-1")
    first = session.usage
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        stamp_backend_usage_unavailable(session, "T-1")

    assert session.usage is first
    assert not caplog.records


def test_an_existing_usage_is_never_overwritten() -> None:
    existing = SessionUsage(state=UsageState.RECORDED)
    session = _session("opencode", usage=existing)

    stamp_backend_usage_unavailable(session, "T-1")

    assert session.usage is existing


def test_a_session_without_a_liveness_handle_is_untouched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(None)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        stamp_backend_usage_unavailable(session, "T-1")

    assert session.usage is None
    assert not caplog.records
    assert usage_unavailable_friction(session) == USAGE_UNAVAILABLE_FRICTION_PREFIX


@pytest.mark.parametrize("backend", ["opencode", "aider"])
def test_friction_names_the_backend_and_the_follow_up(backend: str) -> None:
    friction = usage_unavailable_friction(_session(backend))

    assert friction == (
        f"usage_unavailable: {backend} sessions record no token usage "
        f"(reason=backend_output_unpinned: {_DETAIL})"
    )
    assert friction.startswith(USAGE_UNAVAILABLE_FRICTION_PREFIX)


def test_the_stamp_reads_no_file_and_launches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "the marker must not read a file or launch a process"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "open", _boom)
    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    session = _session("opencode")

    stamp_backend_usage_unavailable(session, "T-1")
    usage_unavailable_friction(session)

    assert session.usage is not None


def test_the_stamp_runs_under_the_sessions_lock_and_nothing_else_does() -> None:
    """Entered under the harvest's lock; the stamp itself only assigns."""
    from cw.config import sessions_lock

    session = _session("opencode")
    held: list[bool] = []

    with sessions_lock():
        held.append(is_rank_held(LockRank.SESSIONS))
        stamp_backend_usage_unavailable(session, "T-1")

    assert held == [True]
    assert session.usage is not None


def _imports_and_calls(path: Path) -> set[str]:
    """Names imported or called in *path*, from the AST (docstrings cannot trip it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.mark.parametrize("module", ["harvest_usage.py", "emitted_result.py"])
def test_source_guard_no_file_read_or_opencode_reader(module: str) -> None:
    names = _imports_and_calls(_SRC / module)

    forbidden = {
        "open",
        "read_text",
        "subprocess",
        "HarvestFacts",
        "opencode_runner",
        "harvest_synthesis",
    }
    assert not names & forbidden
