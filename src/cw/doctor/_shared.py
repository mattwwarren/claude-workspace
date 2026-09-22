"""Shared dataclasses for the ``cw doctor`` package.

These result types are consumed by every check cluster (``config_checks``,
``linkage``, ``core``) and rendered by ``report``. They live here — rather than
in any one cluster — because they are genuinely multi-consumer state, mirroring
the ``cw.reconcile._shared`` precedent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SettingsReadFailure:
    """Why a user-level settings file could not be read as a JSON object.

    ``reason`` is a short class label (``invalid UTF-8``, ``malformed JSON``,
    ``not a JSON object``, ``unreadable: <ExcName>``), never exception text,
    which can quote the file's contents. ``missing`` marks the ordinary
    file-not-there case so each caller can decide whether that is silent (the
    Stop-hook scan) or worth a note (the bypass-disclaimer check).
    """

    reason: str
    missing: bool = False


def _read_settings(path: Path) -> dict[str, object] | SettingsReadFailure:
    """Read a user-level settings file defensively (#2226).

    The one reader for every doctor check that opens a settings file. A check
    whose purpose is to diagnose a broken install must survive that broken
    install, so ``UnicodeDecodeError``, ``OSError`` (a directory, a permission
    error), ``json.JSONDecodeError`` and valid JSON that is not an object all
    come back as a :class:`SettingsReadFailure` for the caller to turn into a
    WARN. Nothing here raises out of ``run_doctor``.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SettingsReadFailure("not found", missing=True)
    except UnicodeDecodeError:
        return SettingsReadFailure("invalid UTF-8")
    except OSError as exc:
        return SettingsReadFailure(f"unreadable: {type(exc).__name__}")

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        return SettingsReadFailure("malformed JSON")

    if not isinstance(data, dict):
        return SettingsReadFailure("not a JSON object")
    return data


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
