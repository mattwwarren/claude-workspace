"""Running dispatch loop's code identity versus the checkout, for cw doctor (#2729).

One ``CheckResult`` named ``dispatch-loop-code``. A loop that started on other
code than the checkout's (STALE) is ``ok=True, warn=True``: advisory, exit code
unchanged, the remedy is restarting ``cw dev-queue serve``. No loop, a match,
and a loop that recorded no identity (it predates this check) are plain OK.
The comparison and its wording live in :mod:`cw.dispatch_loop_identity`, shared
with the ``cw dev-queue resolve``/``approve`` stderr line. Read-only; no
logging, no events. Leaf module -- no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from cw.dispatch_loop_identity import LoopStatus, assess_dispatch_loop
from cw.doctor._shared import CheckResult

_CHECK_NAME = "dispatch-loop-code"


def _check_loop_code_identity() -> CheckResult:
    """Warn when the live dispatch loop runs code other than the checkout's."""
    assessment = assess_dispatch_loop()
    return CheckResult(
        _CHECK_NAME,
        ok=True,
        warn=assessment.status is LoopStatus.STALE,
        detail=assessment.message,
    )
