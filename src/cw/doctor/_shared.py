"""Shared dataclasses for the ``cw doctor`` package.

These result types are consumed by every check cluster (``config_checks``,
``linkage``, ``core``) and rendered by ``report``. They live here — rather than
in any one cluster — because they are genuinely multi-consumer state, mirroring
the ``cw.reconcile._shared`` precedent.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import load_orchestrator_config
from cw.exceptions import CwError
from cw.models import OrchestratorConfig

if TYPE_CHECKING:
    from pathlib import Path


# The global reap command. The detector recipes HARD-CODE this phrase (they do
# not import this constant); it anchors the scoped re-run hint
# (`ReapScope.reap_command`, `_reap_timeout_check`) and the scoped-recipe rewrite
# regex in `core.py`, which only works while every detector literal stays
# byte-equal to it. `TestRecipeLiteralDriftGuard` fails when one drifts (#2716).
REAP_COMMAND = "cw doctor --reap"


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


def orchestrator_config_or_default() -> OrchestratorConfig:
    """Load orchestrator.yaml, degrading to defaults on any load failure.

    Degrade rather than raise: a bad orchestrator.yaml is already reported by
    ``_check_orchestrator_config()``. Letting it propagate from a later check
    would crash ``run_doctor()`` before that ``ok=False`` result is ever
    printed. See GitHub #1200. The one loader for every doctor check that only
    needs the thresholds; shared in #3162 from three verbatim copies.
    """
    try:
        return load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        return OrchestratorConfig()


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
    # Daemon roster short id (#2480), set only by
    # ``wedge/leaked-daemon-worker`` findings -- that class keys off a live
    # roster entry, which may have no matching cw ``session_id`` at all (a
    # leaked worker that was never tracked, or whose record is gone).
    daemon_short_id: str | None = None


@dataclass(frozen=True)
class ReapScope:
    """The one ``(client, ticket)`` a scoped ``cw doctor --reap`` may touch (#2716).

    Carried from ``cw doctor --reap --ticket <T> --client <C>`` through
    :func:`cw.doctor.core.run_doctor` into the reap, so every mutation is
    checked against one frozen value rather than a loose pair of strings.
    Ticket numbers repeat across clients, so a ticket alone is never a scope.
    A session's ticket comes from its name; callers compute it with
    ``cw.reconcile.ticket_id_for_session`` (this module stays a leaf and does
    not import ``cw.reconcile``).
    """

    client: str
    ticket_id: str

    @property
    def key(self) -> tuple[str, str]:
        """``(client, ticket_id)``, the row key the scope names."""
        return (self.client, self.ticket_id)

    def reap_command(self) -> str:
        """The scoped re-run command, shell-quoted (#2716).

        The one builder for every scoped report text that tells a reader what
        to run next: the bare :data:`REAP_COMMAND` would run the global reap
        and mutate every client.
        """
        ticket = shlex.quote(self.ticket_id)
        client = shlex.quote(self.client)
        return f"{REAP_COMMAND} --ticket {ticket} -c {client}"

    def covers(self, client: str, ticket_id: str | None) -> bool:
        """True only for this exact ``(client, ticket_id)``; ``None`` never matches."""
        return ticket_id is not None and (client, ticket_id) == self.key


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
