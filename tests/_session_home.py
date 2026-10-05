"""Redirect ``HOME`` for the whole test session, at import time (#1756).

Imported by ``tests/conftest.py`` ahead of every ``cw`` import. ``src/cw`` binds
many ``Path.home()``-derived constants at import time (``cw.config``'s
``_REAL_STATE_DIR``/``_REAL_CONFIG_DIR``, ``queue_peek.CLAUDE_PROJECTS``,
``native_daemon._JOBS_PATH``, the ``doctor`` seams, ...), and ``cw.config``
also reads ``XDG_*`` at import time. A fixture or ``pytest_configure`` hook
runs too late to reach them, so this module mutates ``os.environ`` as a side
effect of being imported: ``HOME`` points at a throwaway directory holding a
minimal ``.gitconfig``, and the ``XDG_*`` base-directory overrides are dropped.

The autouse ``_isolate_home`` fixture in ``conftest.py`` then narrows ``HOME``
to a per-test directory, so tests that seed ``~/.claude/...`` cannot see each
other's files.

Opt-out is whole-process: live and integration runs that need the operator's
real credentials (``claude``/``codex`` auth, ``~/.codex/sessions``) set
``CW_TEST_REAL_HOME`` or one of the ``INTEGRATION_*`` live gates, and then no
redirect happens at all (``SESSION_HOME is None``).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Any of these set to a value other than "" / "0" keeps the real HOME.
OPT_OUT_ENV_VARS = (
    "CW_TEST_REAL_HOME",
    "INTEGRATION_CODEX_LIVE",
    "INTEGRATION_OPENCODE_LIVE",
    "INTEGRATION_REAL_API",
)

# Base-directory overrides that would otherwise steer ``cw.config`` (and git,
# for ``XDG_CONFIG_HOME``) at the operator's real directories.
XDG_ENV_VARS = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)

# Same identity ``make_git_repo`` writes per-repo, so commits made outside it
# (plain ``git init``, clones, bare origins) look identical. ``_clean_git_env``
# strips every ``GIT_*`` variable, so ``$HOME/.gitconfig`` is the only global
# config that reaches those subprocesses. Nothing else is set, so CI's git
# defaults (e.g. ``init.defaultBranch``) stay unmasked.
TEST_GIT_USER_NAME = "cw test"
TEST_GIT_USER_EMAIL = "test@example.com"
_MINIMAL_GITCONFIG = (
    f"[user]\n\tname = {TEST_GIT_USER_NAME}\n\temail = {TEST_GIT_USER_EMAIL}\n"
)


def wants_real_home(environ: Mapping[str, str]) -> bool:
    """Return True when *environ* opts this process out of the HOME redirect."""
    return any(
        environ.get(name, "").strip() not in ("", "0") for name in OPT_OUT_ENV_VARS
    )


def write_minimal_gitconfig(home: Path) -> None:
    """Write the test git identity to ``home / ".gitconfig"``."""
    (home / ".gitconfig").write_text(_MINIMAL_GITCONFIG, encoding="utf-8")


REAL_HOME: Path = Path.home()
SESSION_HOME: Path | None = None

if not wants_real_home(os.environ):
    SESSION_HOME = Path(tempfile.mkdtemp(prefix="cw-test-home-"))
    write_minimal_gitconfig(SESSION_HOME)
    os.environ["HOME"] = str(SESSION_HOME)
    for _name in XDG_ENV_VARS:
        os.environ.pop(_name, None)
    # Runs after pytest's session teardown, so the session-scoped leak guard
    # has already compared the real tree by the time this is removed.
    atexit.register(shutil.rmtree, SESSION_HOME, ignore_errors=True)
