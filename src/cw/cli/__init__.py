"""Click CLI dispatcher for cw commands.

``cli`` is a package: the root ``main`` group and shared framework helpers live
in :mod:`cw.cli._base`, and each command area is implemented in a submodule
that registers its commands onto ``main`` when imported.

Nothing here imports those submodules. ``main`` is a
:class:`~cw.cli._lazy.LazyGroup` that resolves each top-level command from the
static :data:`cw.cli._lazy.LAZY_COMMANDS` map on first use, so ``cw --version``
and ``cw --help`` do not pay for the whole command tree (#2934). A new
top-level command needs an entry in that map.

``main`` is re-exported as the console-script entry point (``cw.cli:main``)
and the ``python -m cw`` target.
"""

from __future__ import annotations

from cw.cli._base import main

__all__ = ["main"]
