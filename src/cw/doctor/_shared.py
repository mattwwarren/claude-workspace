"""Shared dataclasses for the ``cw doctor`` package.

These result types are consumed by every check cluster (``config_checks``,
``linkage``, ``core``) and rendered by ``report``. They live here — rather than
in any one cluster — because they are genuinely multi-consumer state, mirroring
the ``cw.reconcile._shared`` precedent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckResult:
    """One preflight check and whether it passed."""

    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass(frozen=True)
class WedgeFinding:
    """A detected wedge condition with an actionable recipe."""

    wedge_class: str
    session_id: str | None
    ticket_id: str | None
    recipe: str
    state_file: str


@dataclass
class DoctorReport:
    """Aggregated output from :func:`run_doctor`."""

    version: str
    checks: list[CheckResult] = field(default_factory=list)
    wedge_findings: list[WedgeFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def clean(self) -> bool:
        """True only when every check is both ok and not warned."""
        return all(c.ok and not c.warn for c in self.checks)
