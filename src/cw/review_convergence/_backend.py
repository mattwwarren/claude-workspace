"""Confirm-backend resolution for the converge-to-clean review (#2787).

The independent confirmation round must be a different reviewer from the loop's
own (R7): a clean loop round is only believed once a second, independent
backend agrees. :func:`resolve_confirm_backend` turns the configured choice
(``review.confirm_backend``, possibly unset) into the concrete backend the
ledger stores, and says whether that backend is NOT independent of the loop.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from cw.review_convergence._models import ConfirmBackend

#: Loop backend name -> the ``review.confirm_backend`` value that denotes the
#: same reviewer family. ``none`` is never a family.
_LOOP_FAMILY: dict[str, ConfirmBackend] = {
    "claude-native": "claude",
    "codex": "codex",
}


class ResolvedBackend(NamedTuple):
    """A resolved confirm backend and whether it equals the loop's own family."""

    name: ConfirmBackend
    not_independent: bool


def resolve_confirm_backend(
    configured: ConfirmBackend | None, loop_backend: str
) -> ResolvedBackend:
    """Resolve ``review.confirm_backend`` against the loop backend.

    ``None`` (unset) resolves to the family that differs from the loop's
    (``codex`` for ``claude-native``, ``claude`` for ``codex``). ``codex`` and
    ``none`` pass through; ``none`` is the explicit opt-out and is never
    "not independent" (it runs no round at all). An explicit ``claude`` for a
    ``claude-native`` loop resolves to ``claude`` with ``not_independent=True``:
    the caller is told no independent round will run. There is no
    defaulted-versus-explicit input, so a default can never be mistaken for an
    opt-out.
    """
    family = _LOOP_FAMILY.get(loop_backend)
    if configured is None:
        return ResolvedBackend("claude" if family == "codex" else "codex", False)
    if configured == "none":
        return ResolvedBackend("none", False)
    return ResolvedBackend(configured, family == configured)
