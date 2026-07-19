"""Tests for the shared ``make_git_repo`` fixture factory (#1238).

The ``base=`` keyword is additive: the live codex contract suite must build
fixture repos under a home-tree base dir because snap-confined
codex cannot reach ``/tmp``. Every pre-existing positional caller
(``make_git_repo("name")``) must keep its exact ``tmp_path``-relative
behavior.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from cw.config import load_state
from cw.models import (
    QueueItemStatus,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from tests.conftest import (
    _make_daemon_session,
    _make_ticket_task,
    _seed_daemon_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _head_ok(repo: Path) -> bool:
    """True when *repo* is a git repo with a resolvable HEAD commit."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


class TestMakeGitRepoBase:
    """The additive ``base=`` keyword-only argument on ``make_git_repo``."""

    def test_default_base_is_tmp_path(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """No ``base=`` → repo created under ``tmp_path`` (unchanged behavior)."""
        repo = make_git_repo("wt-x")
        assert repo == tmp_path / "wt-x"
        assert _head_ok(repo)

    def test_explicit_base_overrides_tmp_path(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """``base=`` → repo created under the given dir, not ``tmp_path``."""
        other = tmp_path / "elsewhere"
        other.mkdir()
        repo = make_git_repo("wt-y", base=other)
        assert repo == other / "wt-y"
        assert repo.parent == other
        assert _head_ok(repo)


class TestMakeDaemonSession:
    """The widened ``_make_daemon_session(**overrides)`` factory (#1308)."""

    def test_no_overrides_pins_baseline(self) -> None:
        """No overrides → exact baseline field values (regression pin)."""
        sess = _make_daemon_session()
        assert sess.id == "sess-1"
        assert sess.name == "client-a/auto-dev/T-1"
        assert sess.client == "client-a"
        assert sess.purpose is SessionPurpose.IMPL
        assert sess.origin is SessionOrigin.DAEMON
        assert sess.status is SessionStatus.ACTIVE
        assert sess.workspace_path == Path("/tmp/ws")
        assert sess.worktree_path == Path("/tmp/wt")
        assert sess.surface_ref == "live-ref"
        assert sess.claude_session_id is None
        assert sess.started_at == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_overrides_touch_only_named_fields(self) -> None:
        """Overrides replace only the named fields; the rest stay at baseline."""
        sess = _make_daemon_session(status=SessionStatus.COMPLETED, client="acme")
        assert sess.status is SessionStatus.COMPLETED
        assert sess.client == "acme"
        # Unnamed fields keep their baseline values.
        assert sess.id == "sess-1"
        assert sess.purpose is SessionPurpose.IMPL
        assert sess.surface_ref == "live-ref"

    def test_legacy_keyword_overrides_still_apply(self) -> None:
        """The pre-widen ``claude_session_id`` / ``surface_ref`` kwargs still work."""
        sess = _make_daemon_session(claude_session_id="cs-99", surface_ref="ref-x")
        assert sess.claude_session_id == "cs-99"
        assert sess.surface_ref == "ref-x"

    def test_invalid_enum_value_still_raises(self) -> None:
        """model_validate keeps constructor-strict validation (fix didn't loosen)."""
        with pytest.raises(ValidationError):
            _make_daemon_session(status="not-a-real-status")


class TestMakeTicketTask:
    """The new shared ``_make_ticket_task(**overrides)`` factory (#1308)."""

    def test_minimal_is_valid_pending(self) -> None:
        """No overrides → a valid PENDING task with defaulted ticket_id/client."""
        task = _make_ticket_task()
        assert task.ticket_id == "T-1"
        assert task.client == "test-client"
        assert task.status is QueueItemStatus.PENDING

    def test_overrides_apply(self) -> None:
        """Overrides replace only the named fields."""
        task = _make_ticket_task(status=QueueItemStatus.RUNNING, session_id="s1")
        assert task.status is QueueItemStatus.RUNNING
        assert task.session_id == "s1"
        # Defaulted required fields survive.
        assert task.ticket_id == "T-1"
        assert task.client == "test-client"

    def test_invalid_ticket_id_still_raises(self) -> None:
        """The ticket_id field validator still fires under model_validate."""
        with pytest.raises(ValidationError):
            _make_ticket_task(ticket_id="../escape")


class TestSeedDaemonSession:
    """The widened ``_seed_daemon_session(..., **overrides)`` helper (#1308)."""

    def test_persists_and_reflects_overrides(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Overrides land on the returned Session and the persisted state."""
        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            purpose=SessionPurpose.IDEA,
            worktree_path=Path("/tmp/seed-wt"),
        )
        assert sess.purpose is SessionPurpose.IDEA
        assert sess.worktree_path == Path("/tmp/seed-wt")
        # The session was saved to state.
        loaded = load_state()
        assert len(loaded.sessions) == 1
        assert loaded.sessions[0].id == sess.id
        assert loaded.sessions[0].purpose is SessionPurpose.IDEA
