"""Pre-lock verdicts for the REVIEW staleness tip check (#2994).

A leaf module: it imports nothing from ``review_gates`` (which imports it), so
the cycle cannot close. :func:`installed_tip_verdict` is the in-lock gate's only
read; it does no I/O. The verdicts come from
:mod:`cw.dispatch.review_tip_sweep`, which runs the git check before
``dev_queue_lock`` (ADR-0022 invariant 3) and installs them for the locked
section only. Reset on exit, so no verdict outlives its consume call.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from cw.models import ClientConfig

_log = logging.getLogger("cw.dispatch")


class TipKey(NamedTuple):
    """Identity of one tip check. A verdict applies only on an exact match."""

    client: str
    ticket_id: str
    reviewed_sha: str
    head_sha: str


@dataclass(frozen=True)
class TipCheck:
    """A row whose only undecided question is ``tolerates_review_move``.

    ``key`` names the verdict the gate will look for; ``client`` and ``worktree``
    are what the git check needs.
    """

    key: TipKey
    client: ClientConfig
    worktree: Path


_VERDICTS: ContextVar[Mapping[TipKey, bool] | None] = ContextVar(
    "_VERDICTS", default=None
)


@contextmanager
def precomputed_tip_verdicts(
    verdicts: Mapping[TipKey, bool] | None,
) -> Iterator[None]:
    """Install *verdicts* while the block runs; ``None`` installs no scope.

    Reset on exit (normal or not). Nesting restores the outer scope.
    """
    token = _VERDICTS.set(verdicts)
    try:
        yield
    finally:
        _VERDICTS.reset(token)


def installed_tip_verdict(key: TipKey) -> bool | None:
    """The installed ``tolerates_review_move`` answer for *key*, or ``None``.

    ``None`` with no scope active is silent: the gate runs its inline check.
    ``None`` under an active scope is a key miss, logged at WARNING, because
    the normal path must never miss (the sweep and the gate derive the key from
    the same plan over the same rows).
    """
    verdicts = _VERDICTS.get()
    if verdicts is None:
        return None
    verdict = verdicts.get(key)
    if verdict is None:
        _log.warning(
            "review_tip: no precomputed verdict for %s/%s (reviewed %s, head %s); "
            "checking inline",
            key.client,
            key.ticket_id,
            key.reviewed_sha,
            key.head_sha,
        )
    return verdict
