"""Pytest entry point: bootstrap and plugin registration only (#2668).

The shared fixtures and helpers live in concern modules under
``tests/_shared/`` (its ``__init__`` docstring indexes concern -> module).
Import helpers from their ``tests._shared.<module>`` home; never add a
definition here (pinned by ``tests/test_shared_layer.py``).
"""

from __future__ import annotations

import pytest

# Helper asserts in ``tests/_shared`` keep pytest's assertion introspection;
# this must run before any ``tests._shared`` module is imported.
pytest.register_assert_rewrite("tests._shared")

# Must run before the first ``cw`` import: ``_session_home`` redirects HOME/XDG
# so every import-time ``Path.home()`` constant in src/ binds to a throwaway
# home (#1756), and importing ``_git_sandbox`` scrubs a leaked ``GIT_DIR`` and
# the other repo-location variables from ``os.environ`` (#3078). pytest imports
# this list in order after this body runs, so the two bootstrap modules load
# ahead of every ``tests._shared`` module (where the first ``cw`` import is);
# listed here, not imported, so ruff F401 cannot strip them.
# tests/test_conftest.py::TestHomeRedirectByConstruction and
# tests/test_git_sandbox.py pin the ordering.
#
# Plugin order has no autouse effect: function-scope autouse fixtures live
# only in ``autouse_isolation`` and session-scope ones only in
# ``session_guards``, which keeps their setup order fixed.
pytest_plugins = [
    "tests._session_home",
    "tests._git_sandbox",
    "tests._shared.lock_helpers",
    "tests._shared.autouse_isolation",
    "tests._shared.session_guards",
    "tests._shared.git_repos",
    "tests._shared.daemon_fakes",
    "tests._shared.dispatch_fixtures",
    "tests._shared.step25_gates",
]
