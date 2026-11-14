"""Per-client effort level for DAEMON-origin Claude workers (#2350).

``ClientConfig.worker_effort`` takes one of these values. Explicit levels are
forwarded to ``claude --bg`` as ``--effort <level>``; ``INHERIT`` emits no flag
and lets the operator's effort apply. The set is closed, so a typo fails at
config load. Per-level model support is not validated here.

This module is a DAG root: it imports only the standard library.
"""

from __future__ import annotations

from enum import StrEnum


class WorkerEffort(StrEnum):
    """Effort level for an autonomous Claude worker, or ``INHERIT`` to opt out."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    INHERIT = "inherit"
