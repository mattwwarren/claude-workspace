"""The session-scope autouse fixtures: the git-sandbox root binding and the
real ``~/.claude/projects`` write guard. Moved from ``tests/conftest.py``
(#2668).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import tests._session_home
from tests import _git_sandbox

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session", autouse=True)
def _git_sandbox_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Bind the pytest basetemp as the git runner's sandbox root (#3078).

    Session-scoped so module- and session-scoped repo fixtures can use the
    runner too. Not exempt for ``integration`` tests: the runner must work
    there (``_git_sandbox_policy`` only relaxes its containment).
    """
    root = tmp_path_factory.getbasetemp()
    _git_sandbox.bind(root)
    yield root
    _git_sandbox.unbind()


@pytest.fixture(scope="session", autouse=True)
def _guard_no_real_claude_projects_writes() -> Iterator[None]:
    """Fail the suite if a test leaked a directory into the REAL projects dir.

    ``cw._util.claude_project_dir()`` resolves via ``Path.home()`` directly,
    not through ``queue_peek.locate.CLAUDE_PROJECTS`` /
    ``queue_peek.locate.CW_STATE`` — so a test fixture that redirects only
    those two module constants (as ``patched_peek`` did before this guard
    existed) leaves that call path
    writing into the real ``~/.claude/projects/`` (GH #1736).

    The fix is the suite-wide ``HOME`` redirect (``tests/_session_home.py``
    plus the autouse ``_isolate_home``, #1756), which makes this leak class
    impossible by construction. This fixture stays as the backstop behind it:
    it watches the real home captured before the redirect
    (``tests._session_home.REAL_HOME``), not ``Path.home()``, which now points
    at the throwaway home. It snapshots the real directory's entries at
    session start and again at session end, then splits any new entries by
    whether their name matches the ``tmp-pytest``/``pytest-of`` signature this
    bug class produces (``tmp_path``-rooted worktrees run through
    ``claude_project_dir`` against the real home):

    - Entries matching the signature fail the suite — this is the regression
      guard for #1736.
    - Entries not matching it only warn, since concurrent Claude Code sessions
      routinely write into this same real directory while this suite runs,
      and failing on that would make suite exit status depend on unrelated
      activity outside this repo.

    Nothing is deleted here (out of scope).
    """
    real_projects = tests._session_home.REAL_HOME / ".claude" / "projects"
    before = (
        {p.name for p in real_projects.iterdir()} if real_projects.exists() else set()
    )
    yield
    after = (
        {p.name for p in real_projects.iterdir()} if real_projects.exists() else set()
    )
    leaked = after - before
    suspect = {name for name in leaked if "tmp-pytest" in name or "pytest-of" in name}
    other = leaked - suspect
    if other:
        warnings.warn(
            f"New entries appeared under the real {real_projects} during the "
            f"test session that don't match the known GH #1736 leak "
            f"signature: {sorted(other)}. Not failing the suite on these since "
            "concurrent Claude Code activity routinely writes here.",
            stacklevel=2,
        )
    assert not suspect, (
        f"Test suite leaked directories into the REAL {real_projects} "
        f"(GH #1736): {sorted(suspect)}. A test reached the real home "
        "despite the suite-wide HOME redirect (#1756) -- check for a test "
        "that restores the real HOME or reads tests._session_home.REAL_HOME, "
        "and for an import that ran before tests/_session_home.py."
    )
