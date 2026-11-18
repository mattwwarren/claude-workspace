"""The IMPL stage's cw-decided park: blocker reasons and ``ImplBlock`` (#3710).

Carved out of ``cw.codex_impl._result`` so the stage-verification modules can
build an IMPL park without importing the codex IMPL package, which would cycle
through its ``__init__``. ``_result`` re-imports these names for its own use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Existing blocker reasons the IMPL stage reuses (none is new to the vocabulary).
IMPL_FAILED: Final = "impl_failed"
PLAN_MISSING: Final = "plan_missing"
IMPL_BLOCKED: Final = "blocked"


@dataclass(frozen=True)
class ImplBlock:
    """A cw-decided IMPL park: the reason, details and status.

    The one block value the guard, gate and feedback functions return, so no
    REVIEW-worded fence or gate text reaches an IMPL result. *status* is
    ``blocked`` except for the empty-diff park (``empty_diff_blocked``).
    """

    reason: str
    details: str
    retry_eligible: bool = False
    status: str = IMPL_BLOCKED
