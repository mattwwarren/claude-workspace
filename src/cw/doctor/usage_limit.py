"""Active usage-limit back-off windows for cw doctor (#3547).

One ``CheckResult`` named ``usage-limit/<client>`` per client whose
``usage_limited_until`` window is still active, ``ok=True, warn=True``
(advisory), with the command that lifts it early. Keyed by the window's
client name, which may be absent from ``clients.yaml``. No window, no check,
so default doctor output is unchanged. Best effort: a sidecar read error
yields nothing. Leaf module -- no cross-``doctor`` dependencies.
"""

from __future__ import annotations

from cw.doctor._shared import CheckResult
from cw.lane_pause import iso_z
from cw.usage_limit_window import usage_limit_window_views

_CHECK_NAME = "usage-limit"

_ACTIVE = (
    "usage-limit window for {client} is active until {until};"
    " lift it early with: cw dev-queue usage-limit clear -c {client}"
)


def _check_usage_limit_windows() -> list[CheckResult]:
    """Report each client whose usage-limit window is still active."""
    try:
        views = usage_limit_window_views()
    except OSError:
        return []
    return [
        CheckResult(
            f"{_CHECK_NAME}/{view.client}",
            ok=True,
            warn=True,
            detail=_ACTIVE.format(client=view.client, until=iso_z(view.until)),
        )
        for view in views
    ]
