"""Sessions auto-prune status for cw doctor (#2965).

Reads the ``sessions_auto_prune.json`` sidecar that
:mod:`cw.session_auto_prune` writes after each due run and reports the last
run: counts, cutoff, ``sessions.json`` size before and after, age, and whether
the per-run cap left more eligible sessions behind. Advisory only, like
``sessions-size`` and ``lock-holds``: always ``ok=True``; ``warn=True`` only
when the last run failed. Read-only: it never calls ``load_state()`` and never
prunes (ADR-0014). Leaf module -- no cross-``doctor`` dependencies beyond
``_shared``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw._util import format_compact_age
from cw.doctor._shared import CheckResult, orchestrator_config_or_default
from cw.session_auto_prune import AutoPruneStatus, read_auto_prune_status

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig

_CHECK_NAME = "sessions-auto-prune"
_ENABLE_KEY = "sessions_auto_prune_enabled"


def _schedule_summary(config: OrchestratorConfig) -> str:
    return (
        f"terminal sessions older than {config.sessions_auto_prune_age_days}d,"
        f" up to {config.sessions_auto_prune_max_per_run} per run, every"
        f" {config.sessions_auto_prune_interval_minutes}m"
    )


def _last_run_detail(status: AutoPruneStatus, now: datetime) -> str:
    age = format_compact_age(now - status.last_run_at)
    if status.status == "failed":
        return f"last run {age} ago FAILED: {status.error}"
    parts = [
        f"last run {age} ago: archived {status.archived_count},"
        f" deleted {status.deleted_count}, kept {status.kept_count}"
    ]
    if status.cutoff is not None:
        parts.append(f"cutoff {status.cutoff.isoformat()}")
    parts.append(f"sessions.json {status.bytes_before}B->{status.bytes_after}B")
    if status.capped:
        parts.append("capped; more eligible")
    return "; ".join(parts)


def _check_sessions_auto_prune() -> CheckResult:
    """Report the last automatic sessions prune run (advisory, read-only)."""
    config = orchestrator_config_or_default()
    status = read_auto_prune_status()
    if status is None:
        if not config.sessions_auto_prune_enabled:
            detail = f"disabled via {_ENABLE_KEY}"
        else:
            detail = f"not run yet ({_schedule_summary(config)})"
        return CheckResult(_CHECK_NAME, ok=True, detail=detail)

    detail = _last_run_detail(status, datetime.now(UTC))
    if not config.sessions_auto_prune_enabled:
        detail = f"disabled via {_ENABLE_KEY}; {detail}"
    return CheckResult(
        _CHECK_NAME, ok=True, warn=status.status == "failed", detail=detail
    )
