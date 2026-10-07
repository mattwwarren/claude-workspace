"""Confine git repository discovery to the pytest basetemp (#2598).

A test that means "this directory is not a repository" is only true while no
ancestor of it is one. Every dispatched worker runs with ``TMPDIR`` inside its
own worktree (#2470), so ``tmp_path`` sits under a real checkout and git
happily ascends out of it into that checkout.

``GIT_CEILING_DIRECTORIES`` is git's own stop-boundary, but a plain ``setenv``
cannot carry it: every production git seam (``cw._git.git_clean_env`` and its
four siblings) strips *all* ``GIT_*`` variables before launching git, and test
helpers do the same. So :func:`install` wraps ``subprocess.Popen.__init__``
(the same layer ``tests/_lock_invariants.py`` hooks) and injects the ceiling
into each child's effective environment after any such stripping.

The ceiling is the resolved pytest basetemp rather than the per-test
``tmp_path``: git still inspects ``tmp_path`` itself, so a test may
``git init`` it, and git never ascends above basetemp into an enclosing
checkout. A ceiling the caller already set is left alone.

Limit: only filesystem walks that go through git are confined. Walks done in
Python (``find_cw_context``, ``repo_root``) are not; their tests use the
``ancestor_free_dir`` fixture instead.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

CEILING_ENV_VAR = "GIT_CEILING_DIRECTORIES"

# ``Popen``'s ``env`` is its 11th positional parameter counting ``args``, so a
# caller that passed it positionally supplied this many arguments after ``args``.
POSITIONAL_ENV_REST_LEN = 10


def confined_env(env: Mapping[str, str] | None, ceiling: Path) -> dict[str, str]:
    """Return a copy of *env* (``os.environ`` when ``None``) carrying the ceiling.

    The ceiling is only added when *env* lacks one, so a caller's own choice
    wins. The input is never mutated.
    """
    merged = dict(os.environ if env is None else env)
    merged.setdefault(CEILING_ENV_VAR, str(ceiling.resolve()))
    return merged


def install(monkeypatch: pytest.MonkeyPatch, ceiling: Path) -> None:
    """Make every ``Popen`` launched while *monkeypatch* is active ceiling-confined.

    Installs chain: a later :func:`install` runs first, and an earlier one then
    sees the key already present and leaves it alone.
    """
    real_init: Callable[..., None] = subprocess.Popen.__init__

    def _confining_init(
        self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object
    ) -> None:
        env = kwargs.get("env")
        positional_env = len(rest) >= POSITIONAL_ENV_REST_LEN
        if not positional_env and (env is None or isinstance(env, Mapping)):
            kwargs["env"] = confined_env(env, ceiling)
        real_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _confining_init)
