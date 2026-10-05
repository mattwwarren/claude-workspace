"""Tests for the shared ``make_git_repo`` fixture factory (#1238).

The ``base=`` keyword is additive: the live codex contract suite must build
fixture repos under a home-tree base dir because snap-confined
codex cannot reach ``/tmp``. Every pre-existing positional caller
(``make_git_repo("name")``) must keep its exact ``tmp_path``-relative
behavior.

Also pins the suite-wide ``HOME`` redirect (#1756) owned by
``tests/_session_home.py`` and the autouse ``_isolate_home`` fixture.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import cw.config
from cw import native_daemon, queue_peek
from cw.config import load_state
from cw.doctor import skills_drift, versions
from cw.models import (
    QueueItemStatus,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from tests._session_home import REAL_HOME, SESSION_HOME, wants_real_home
from tests.conftest import (
    _OPTIONAL_BINARY_DENYLIST,
    _clean_git_env,
    _guard_no_real_claude_projects_writes,
    _make_daemon_session,
    _make_ticket_task,
    _seed_daemon_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# The redirect is decided once per process at import time (#1756): an
# opted-out run (CW_TEST_REAL_HOME / INTEGRATION_*) has no redirect at all, so
# every assertion about the redirect is skipped there rather than failing.
requires_home_redirect = pytest.mark.skipif(
    SESSION_HOME is None, reason="HOME redirect opted out"
)


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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git under the same ``GIT_*``-stripped env the git fixtures use."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )


class TestHomeRedirectByConstruction:
    """The import-time + per-test ``HOME`` redirect (#1756).

    ``tests/_session_home.py`` points ``HOME`` at a throwaway directory before
    any ``cw`` module binds an import-time ``Path.home()`` constant, and the
    autouse ``_isolate_home`` fixture gives each test its own
    ``tmp_path / "_home"``. These pin that no test can reach the operator's
    real ``~/.cw`` or ``~/.claude`` (#1736, #2460).

    The real-home checks compare against the equivalent path under
    ``REAL_HOME`` rather than asserting ``REAL_HOME`` is not an ancestor: a
    dispatched worker's ``TMPDIR`` (and so ``tmp_path``) legitimately lives
    under the real ``~/.cw/wt/<worktree>/.cw/tmp``.
    """

    @requires_home_redirect
    def test_path_home_is_per_test_tmp_home(self, tmp_path: Path) -> None:
        """Each test sees ``tmp_path / "_home"`` as its home, never the real one."""
        home = Path.home()
        assert home == tmp_path / "_home"
        assert os.environ["HOME"] == str(home)
        assert home != REAL_HOME
        assert home / ".cw" != REAL_HOME / ".cw"
        assert home / ".claude" / "projects" != REAL_HOME / ".claude" / "projects"

    @requires_home_redirect
    @pytest.mark.parametrize(
        "constant",
        [
            pytest.param(lambda: cw.config._REAL_STATE_DIR, id="config-state"),
            pytest.param(lambda: cw.config._REAL_CONFIG_DIR, id="config-config"),
            pytest.param(lambda: queue_peek.CLAUDE_PROJECTS, id="queue-peek"),
            pytest.param(lambda: native_daemon._JOBS_PATH, id="daemon-jobs"),
            pytest.param(lambda: versions._CLAUDE_SETTINGS_PATH, id="doctor-versions"),
            pytest.param(lambda: skills_drift._CLAUDE_HOME, id="doctor-skills"),
        ],
    )
    def test_import_time_constants_follow_session_home(
        self, constant: Callable[[], Path]
    ) -> None:
        """Import-time ``Path.home()`` constants resolved under the session home.

        Pins the import ordering in ``tests/conftest.py``: if
        ``tests._session_home`` stops running before the first ``cw`` import,
        these bind to the real home and fail here.
        """
        assert SESSION_HOME is not None
        path = constant()
        assert path.is_relative_to(SESSION_HOME)
        assert path != REAL_HOME / path.relative_to(SESSION_HOME)

    @requires_home_redirect
    def test_xdg_overrides_not_inherited(self) -> None:
        """Operator ``XDG_*`` overrides cannot steer ``cw.config`` to real dirs."""
        assert not os.environ.get("XDG_CONFIG_HOME")
        assert not os.environ.get("XDG_DATA_HOME")

    @requires_home_redirect
    def test_git_identity_from_redirected_home(self, tmp_path: Path) -> None:
        """Git identity comes from ``$HOME/.gitconfig`` and survives the GIT_* strip."""
        email = _git(tmp_path, "config", "--global", "user.email")
        assert email.stdout.strip() == "test@example.com"

        origin = _git(tmp_path, "config", "--global", "--show-origin", "user.email")
        assert origin.returncode == 0
        origin_path = Path(origin.stdout.split("\t", 1)[0].removeprefix("file:"))
        assert origin_path.is_relative_to(Path.home())

        repo = tmp_path / "bare-init"
        repo.mkdir()
        assert _git(repo, "init", "-q").returncode == 0
        commit = _git(repo, "commit", "--allow-empty", "-q", "-m", "identity")
        assert commit.returncode == 0, commit.stderr

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [
            ({}, False),
            ({"CW_TEST_REAL_HOME": "1"}, True),
            ({"INTEGRATION_CODEX_LIVE": "1"}, True),
            ({"INTEGRATION_OPENCODE_LIVE": "1"}, True),
            ({"INTEGRATION_REAL_API": "1"}, True),
            ({"CW_TEST_REAL_HOME": "0"}, False),
            ({"CW_TEST_REAL_HOME": ""}, False),
            ({"INTEGRATION_CODEX_LIVE": " 0 "}, False),
            ({"UNRELATED": "1"}, False),
        ],
    )
    def test_wants_real_home_predicate(
        self, environ: dict[str, str], expected: bool
    ) -> None:
        """Opt-out gate uses the live tests' ``.strip() not in ("", "0")`` rule."""
        assert wants_real_home(environ) is expected

    @requires_home_redirect
    def test_guard_uses_captured_real_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak guard watches the captured real home, not ``Path.home()``."""
        fake_real = tmp_path / "fake-real-home"
        real_projects = fake_real / ".claude" / "projects"
        real_projects.mkdir(parents=True)
        monkeypatch.setattr("tests._session_home.REAL_HOME", fake_real)
        guard = _guard_no_real_claude_projects_writes.__wrapped__

        # A signature-matching entry under the redirected home is invisible.
        quiet = guard()
        next(quiet)
        (Path.home() / ".claude" / "projects" / "pytest-of-x").mkdir(parents=True)
        with pytest.raises(StopIteration):
            next(quiet)

        # The same entry under the captured real home fails the suite.
        loud = guard()
        next(loud)
        (real_projects / "pytest-of-x").mkdir()
        with pytest.raises(AssertionError, match=re.escape(str(real_projects))):
            next(loud)
