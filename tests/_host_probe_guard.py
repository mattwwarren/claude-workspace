"""Keep unit tests from reading host state they do not own (#2664).

Two probes escaped the suite's existing optional-binary isolation and made
tests pass or fail according to the machine they ran on:

* ``opencode run --help`` (``cw.opencode_runner._supports_v1_flags``) is a bare
  ``subprocess.run`` -- ``shutil.which`` masking never sees it, so an installed
  opencode 1.x answered it and put ``--pure``/``--dir`` into an argv a test
  asserted was flag-free.
* ``psutil.process_iter`` (``cw.reconcile.codex_boot._codex_processes_in``)
  scans the *whole* host table and fails closed (``None``, inconclusive) when
  any process is unreadable. A single foreign or zombie ``codex`` with no
  readable cwd therefore parked every scan on the host, whatever the test
  meant to prove.

This module holds the logic; ``tests/conftest.py`` wires it into autouse
fixtures, the same split as ``tests/_git_ceiling.py``.

Binary launch guard
-------------------

:func:`install_binary_launch_guard` wraps ``subprocess.Popen.__init__`` (the
layer ``tests/_git_ceiling.py`` and ``tests/_lock_invariants.py`` hook) and
raises ``FileNotFoundError(ENOENT)`` for a launch of a denylisted binary by
bare name. That is exactly what ``Popen`` raises on a CI runner that lacks the
binary, so code written for the absent case behaves identically here. The
exception is raised *before* delegating, which is safe: ``Popen._child_created``
is a class attribute (``False``), so the half-built object's ``__del__`` has
nothing to clean up.

Coverage limits, by design. The guard looks only at the first element of a
list or tuple ``args`` and only when it is a bare denylisted string. These
pass through untouched:

* ``shell=True`` string commands (``args`` is a ``str``);
* the ``executable=`` keyword (the launched program is not ``args[0]``);
* absolute- or relative-path launches, so a test's own stub still runs;
* names forced present by ``@pytest.mark.binary_on_path`` -- such a test that
  does not mock its launch remains host-dependent by design, because the
  marker is the test saying "this binary exists".

The denylist is passed in rather than imported, since ``tests/conftest.py``
owns it and importing it here would be circular.

Process table
-------------

:func:`empty_process_table` is the suite-wide default for
``psutil.process_iter``. A test that wants the real reader scopes its results
to pids it owns with :func:`scoped_process_iter`, which wraps the real
``process_iter`` (captured at import, before any patch) and filters by pid:
the real scan still enumerates the table, but what it returns -- and so what
the code under test can conclude -- never depends on other host processes.
"""

from __future__ import annotations

import errno
import os
import subprocess
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    import pytest

# ``psutil.process_iter`` as it was when this module was first imported, i.e.
# before any fixture or test patched it. ``scoped_process_iter`` reads through
# this, so it keeps working while the autouse default is the empty table.
REAL_PROCESS_ITER = psutil.process_iter

# The ``attrs`` types ``psutil.process_iter`` itself accepts.
ProcessAttrs = list[str] | tuple[str, ...] | set[str] | frozenset[str]


def blocked_binary(
    args: object, denylist: Collection[str], forced_present: Collection[str]
) -> str | None:
    """Return the denylisted binary *args* would launch, or ``None`` to allow.

    Only a list or tuple whose first element is a bare string naming a
    denylisted binary that is not *forced_present* is blocked; see the module
    docstring for what deliberately passes through.
    """
    if not isinstance(args, (list, tuple)) or not args:
        return None
    first = args[0]
    if not isinstance(first, str):
        return None
    if first in denylist and first not in forced_present:
        return first
    return None


def install_binary_launch_guard(
    monkeypatch: pytest.MonkeyPatch,
    denylist: Collection[str],
    forced_present: Collection[str],
) -> None:
    """Make launching a denylisted binary by bare name raise ``ENOENT``.

    Installs chain with the other ``Popen.__init__`` wrappers: this one runs
    first when installed last, and delegates to whatever was there before.
    """
    real_init: Callable[..., None] = subprocess.Popen.__init__

    def _guarded_init(
        self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object
    ) -> None:
        name = blocked_binary(args, denylist, forced_present)
        if name is not None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
        real_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


def empty_process_table(
    _attrs: ProcessAttrs | None = None, _ad_value: object = None
) -> Iterator[psutil.Process]:
    """Stand in for ``psutil.process_iter`` over a host with no processes."""
    return iter(())


def scoped_process_iter(
    pids: Collection[int],
) -> Callable[..., Iterator[psutil.Process]]:
    """Return a ``process_iter`` whose results are scoped to the owned *pids*.

    The real iterator still runs -- real kernel entries, real ``.info`` -- but
    only processes whose pid is in *pids* are yielded. A pid that is not in the
    table is simply absent.
    """
    owned = frozenset(pids)

    def _scoped(
        attrs: ProcessAttrs | None = None, ad_value: object = None
    ) -> Iterator[psutil.Process]:
        for process in REAL_PROCESS_ITER(attrs, ad_value):
            if process.pid in owned:
                yield process

    return _scoped
