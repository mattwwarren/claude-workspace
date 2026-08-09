"""Tests for the shared ``make_git_repo`` fixture factory (#1238).

The ``base=`` keyword is additive: the live codex contract suite must build
fixture repos under a home-tree base dir because snap-confined
codex cannot reach ``/tmp``. Every pre-existing positional caller
(``make_git_repo("name")``) must keep its exact ``tmp_path``-relative
behavior.
"""

from __future__ import annotations

import os
import shutil
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
    _OPTIONAL_BINARY_DENYLIST,
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


def _make_fake_binary(tmp_path: Path, name: str) -> Path:
    """Create a real, executable *name* script under a fresh dir in *tmp_path*.

    Returns the containing directory (not the script itself), ready to be
    prepended to ``PATH`` so ``shutil.which(name)`` would resolve it for
    real absent the guard under test.
    """
    bin_dir = tmp_path / f"fakebin-{name}"
    bin_dir.mkdir()
    script = bin_dir / name
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | 0o111)
    return bin_dir


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


class TestOptionalBinaryAbsenceGuard:
    """The autouse ``_hide_optional_binaries`` fixture + ``binary_on_path`` (#1753).

    Regression coverage for the incident behind #1727/#1752: dispatch tests
    were only ever exercised on developer machines that happened to have the
    ``codex`` CLI installed, so ``CodexExecutor.spawn()``'s real
    ``shutil.which("codex")`` pre-flight (``src/cw/executor.py:884``) never
    ran its CODEX_NOT_FOUND branch locally — only in CI, where it shipped
    red. These tests prove the denylist genuinely masks a *present* binary
    (not just a naturally-absent one), that the escape hatch makes a binary
    look present without a real one on ``PATH``, that the denylist is scoped
    (not blanket), and that ``@pytest.mark.integration`` exempts the guard.
    """

    @pytest.mark.parametrize("binary", sorted(_OPTIONAL_BINARY_DENYLIST))
    def test_denylisted_binary_absent_by_default(
        self, binary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely-present denylisted binary on PATH still resolves to None.

        Parametrized over the whole denylist so adding a third entry gets
        this coverage for free instead of a copy-pasted test method.
        """
        bin_dir = _make_fake_binary(tmp_path, binary)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        assert shutil.which(binary) is None

    @pytest.mark.binary_on_path("codex")
    def test_binary_on_path_marker_forces_present(self) -> None:
        """The marker makes a denylisted binary look present with none on PATH."""
        result = shutil.which("codex")
        assert result is not None
        # Deterministic canned value, not a real resolution off a bare PATH.
        assert result == "/usr/bin/codex"

    def test_non_denylisted_binary_passes_through(self) -> None:
        """A non-denylisted binary (``git``) still resolves normally under the guard."""
        assert shutil.which("git") is not None

    @pytest.mark.integration
    def test_integration_marker_exempts_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``integration``-marked tests see the real, unguarded ``shutil.which``."""
        bin_dir = _make_fake_binary(tmp_path, "codex")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        result = shutil.which("codex")
        assert result == str(bin_dir / "codex")
