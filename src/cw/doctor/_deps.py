"""Cross-cluster external callables for the ``cw doctor`` package.

Both ``load_clients`` and ``load_dev_queue`` are called from more than one
check cluster (``config_checks``, ``linkage``, ``core``). Consuming modules
reach them through this module object — ``from cw.doctor import _deps`` then
``_deps.load_clients(...)`` — so a single ``monkeypatch.setattr`` at
``cw.doctor._deps.NAME`` intercepts every caller, preserving the
single-patch-point property the flat ``doctor.py`` module gave for free.
This mirrors the ``cw.reconcile._deps`` precedent.
"""

from __future__ import annotations

from cw.config import load_clients
from cw.dev_queue import load_dev_queue

__all__ = ["load_clients", "load_dev_queue"]
