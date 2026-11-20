"""Runtime-capability slugs an operator declares for a review runner (#3790).

A review engine can require runtime capabilities (for example ``jvm`` or
``cuda``) that a runner must declare. Both sides are operator-owned config in
``clients.yaml``: ``review.engine.requires_runtime_capabilities`` on the
requirement side and ``pipeline.executors.review.runtime_capabilities`` on the
runner side. Nothing here reads the engine manifest or any PR-branch file.

This leaf module imports only ``re`` and nothing from ``cw``, so both
``cw.models.client`` and ``cw.models.orchestrator_config.stage`` can use it
without a cycle. Import it by submodule path; it is not re-exported from
``cw.models``.
"""

from __future__ import annotations

import re

RUNTIME_CAPABILITY_RE = r"^[a-z0-9][a-z0-9_.-]*$"
MAX_RUNTIME_CAPABILITY_LENGTH = 64
MAX_RUNTIME_CAPABILITIES = 16


def normalize_runtime_capabilities(value: list[str], *, field: str) -> list[str]:
    """Validate, deduplicate and sort a capability list.

    The raw length is checked against the cap before deduplication, so a list
    of repeats cannot slip past it. Each slug must fullmatch
    ``RUNTIME_CAPABILITY_RE`` and stay within the length cap.
    """
    if len(value) > MAX_RUNTIME_CAPABILITIES:
        msg = (
            f"{field} has {len(value)} entries; the cap is "
            f"{MAX_RUNTIME_CAPABILITIES} (first excess entry: index "
            f"{MAX_RUNTIME_CAPABILITIES}, "
            f"{value[MAX_RUNTIME_CAPABILITIES]!r})"
        )
        raise ValueError(msg)
    for entry in value:
        _check_slug(entry, field=field)
    return sorted(set(value))


def _check_slug(entry: str, *, field: str) -> None:
    if (
        not isinstance(entry, str)
        or len(entry) > MAX_RUNTIME_CAPABILITY_LENGTH
        or re.fullmatch(RUNTIME_CAPABILITY_RE, entry) is None
        or ".." in entry
    ):
        msg = (
            f"{field} entry {entry!r} is not a runtime capability slug; use "
            "lowercase letters, digits, '_', '.' and '-' (no '/', whitespace or "
            "'..', starting with a letter or digit), at most "
            f"{MAX_RUNTIME_CAPABILITY_LENGTH} characters"
        )
        raise ValueError(msg)
