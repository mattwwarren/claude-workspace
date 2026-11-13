"""Advisory ``session-outcomes`` doctor check (#2840).

One line: the no-result rate over the last 7 days, the largest failure cause
and the command that prints the full table. Informational on the rate: a
successful read is always ``ok=True, warn=False``, whatever the rate
(decision D1). ``warn=True`` only when the inbox or history cannot be read.
Never raises out of ``run_doctor``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.doctor._shared import CheckResult
from cw.session_outcomes import OutcomeReport, build_report, live_ids_from_state

if TYPE_CHECKING:
    from cw.models import CwState

_CHECK_NAME = "session-outcomes"
_WINDOW_DAYS = 7
_SAMPLES = 0
_COMMAND = f"cw event session-outcomes --days {_WINDOW_DAYS}"


def _summary(report: OutcomeReport) -> str:
    if report.spawned == 0:
        return f"{_WINDOW_DAYS}d window: no worker sessions spawned; run `{_COMMAND}`"
    largest = report.largest_cause
    largest_text = largest.value if largest is not None else "none"
    return (
        f"{_WINDOW_DAYS}d window: {report.without_result} of {report.spawned}"
        f" spawned without a result ({report.no_result_rate:.1%});"
        f" largest cause {largest_text}; run `{_COMMAND}`"
    )


def _check_session_outcomes(state: CwState | None) -> CheckResult:
    """Report the recent no-result breakdown (advisory, read-only)."""
    if state is None:
        return CheckResult(_CHECK_NAME, ok=True, detail="skipped: state did not load")
    try:
        report = build_report(
            days=_WINDOW_DAYS,
            now=datetime.now(UTC),
            client_names=None,
            live_session_ids=live_ids_from_state(state),
            samples=_SAMPLES,
        )
    except (OSError, ValueError) as exc:
        detail = (
            f"could not read the event inbox or history ({type(exc).__name__}: {exc});"
            f" breakdown unavailable; run `{_COMMAND}` to reproduce the error"
        )
        return CheckResult(_CHECK_NAME, ok=True, warn=True, detail=detail)
    return CheckResult(_CHECK_NAME, ok=True, detail=_summary(report))
