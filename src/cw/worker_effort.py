"""Effort flag for DAEMON-origin ``claude --bg`` workers (#2350).

One pure helper, called from both spawn chokepoints
(:func:`cw.spawn.spawn_create_impl` and :func:`cw.session._resume_spawn_args`),
so the flag cannot drift between the initial spawn and a resume re-spawn.

Rules, in order:

1. ``WorkerEffort.INHERIT`` emits no flag, even on an Opus 5.5 pin.
2. Any other explicit level is forwarded as ``--effort <level>``.
3. Unset (``None``) forwards ``medium`` only when the model is a full
   ``claude-opus-5-5*`` id. Aliases such as ``opus`` get no default.
4. Otherwise no flag, so the operator's effort is inherited.
"""

from __future__ import annotations

import logging
from typing import Final

from cw.models.worker_effort import WorkerEffort

# Verified against `claude --help` (claude 2.1.323): `--effort <level>`, with
# levels low, medium, high, xhigh, max. Change only together with the enum.
WORKER_EFFORT_FLAG: Final = "--effort"

_DEFAULT_LEVEL: Final = WorkerEffort.MEDIUM
# Full model ids only (lower-cased, stripped, matched by prefix so dated
# suffixes work). Aliases are deliberately absent: see CONFIG_REFERENCE.
_DEFAULT_MODEL_PREFIXES: Final[tuple[str, ...]] = ("claude-opus-5-5",)

_log = logging.getLogger(__name__)


def _default_applies(worker_model: str | None) -> bool:
    if not worker_model:
        return False
    normalized = worker_model.strip().lower()
    return normalized.startswith(_DEFAULT_MODEL_PREFIXES)


def resolve_worker_effort_args(
    worker_model: str | None, worker_effort: WorkerEffort | None
) -> list[str]:
    """Return the ``--effort`` argv pair for a worker, or ``[]`` for no flag.

    Logs one INFO record when a flag is emitted. The record carries only the
    model, level and source, not any client or session identity; it is a
    debugging aid, not a persisted measurement.
    """
    if worker_effort is WorkerEffort.INHERIT:
        return []
    if worker_effort is not None:
        level, source = worker_effort, "explicit"
    elif _default_applies(worker_model):
        level, source = _DEFAULT_LEVEL, "default"
    else:
        return []
    _log.info(
        "worker effort: worker_model=%s level=%s source=%s",
        worker_model,
        level.value,
        source,
    )
    return [WORKER_EFFORT_FLAG, level.value]
