"""Shared helpers for tests that consume ``EventBus`` subscriber queues.

Plain functions, no ``test_`` prefix, so pytest does not collect this module.
"""

from __future__ import annotations

import queue
from typing import Any


def drain(q: queue.SimpleQueue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pop every item currently queued on ``q`` and return them in order."""
    items: list[dict[str, Any]] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items
