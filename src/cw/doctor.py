"""cw doctor preflight — report environment health in one place.

When a user's environment doesn't satisfy the chosen backend (no tmux,
cmux daemon not running, state file corrupted), every cw command fails
deep in an adapter with a cryptic error. `cw doctor` is the one place
to find out *what* is wrong before you start a session.

Returns structured results so the CLI can format them and tests can
assert on specific checks.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cw import __version__
from cw.cmux import _resolve_backend_name, get_cmux_adapter
from cw.config import (
    clients_file,
    load_clients,
    load_state,
    orchestrator_config_file,
    state_file,
)
from cw.dev_queue import load_dev_queue
from cw.models import BackendName
from cw.reconcile import reconcile


@dataclass(frozen=True)
class CheckResult:
    """One preflight check and whether it passed."""

    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    """Aggregated output from :func:`run_doctor`."""

    version: str
    backend: BackendName
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


_CMUX_SOCKET_PATH = (
    Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock"
)


def _check_backend_binary(backend: BackendName) -> CheckResult:
    """Check whether the chosen backend's binary/daemon is reachable."""
    if backend is BackendName.TMUX:
        path = shutil.which("tmux")
        if path:
            return CheckResult("tmux on PATH", ok=True, detail=path)
        return CheckResult(
            "tmux on PATH",
            ok=False,
            detail="tmux not found; install via brew/apt or set CW_BACKEND=cmux",
        )
    if backend is BackendName.CMUX:
        if sys.platform != "darwin":
            return CheckResult(
                "cmux daemon",
                ok=False,
                detail=f"cmux requires macOS; running on {sys.platform}",
            )
        if _CMUX_SOCKET_PATH.exists():
            return CheckResult(
                "cmux daemon socket", ok=True, detail=str(_CMUX_SOCKET_PATH)
            )
        return CheckResult(
            "cmux daemon socket",
            ok=False,
            detail=f"not found at {_CMUX_SOCKET_PATH}; is cmux running?",
        )
    # fake backend needs no binary
    return CheckResult("fake backend (no binary required)", ok=True, detail="")


def _check_config_file() -> CheckResult:
    """Verify the clients.yaml exists or that no clients is acceptable."""
    path = clients_file()
    if not path.exists():
        return CheckResult(
            "clients.yaml",
            ok=True,
            detail=f"not yet created at {path} (run `cw init`)",
        )
    try:
        load_clients()
    except Exception as exc:
        return CheckResult("clients.yaml", ok=False, detail=f"parse failed: {exc}")
    return CheckResult("clients.yaml", ok=True, detail=str(path))


def _check_orchestrator_config() -> CheckResult:
    path = orchestrator_config_file()
    if not path.exists():
        return CheckResult(
            "orchestrator.yaml",
            ok=True,
            detail=f"not yet created at {path} (will be generated on first use)",
        )
    return CheckResult("orchestrator.yaml", ok=True, detail=str(path))


def _check_state_file() -> CheckResult:
    path = state_file()
    try:
        load_state()
    except Exception as exc:
        return CheckResult("sessions.json", ok=False, detail=f"load failed: {exc}")
    return CheckResult("sessions.json", ok=True, detail=str(path))


def _check_dev_queue() -> CheckResult:
    try:
        load_dev_queue()
    except Exception as exc:
        return CheckResult("dev_queue.json", ok=False, detail=f"load failed: {exc}")
    return CheckResult("dev_queue.json", ok=True, detail="parseable")


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        adapter = get_cmux_adapter()
    except Exception as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"adapter unavailable: {exc}",
        )
    reconcile_report = reconcile(adapter)
    reaped = len(reconcile_report.phantom_session_ids)
    reverted = len(reconcile_report.reverted_ticket_ids)
    if reaped == 0 and reverted == 0:
        return CheckResult("reconciliation", ok=True, detail="no phantoms")
    return CheckResult(
        "reconciliation",
        ok=True,
        detail=(
            f"reaped {reaped} session(s), reverted {reverted} ticket(s); "
            f"ids: {reconcile_report.phantom_session_ids}"
        ),
    )


def run_doctor(*, reap: bool = False) -> DoctorReport:
    """Run every preflight check and return a populated report.

    When *reap* is True, also run multiplexer/state reconciliation and
    append a ``reconciliation`` check summarising the number of reaped
    sessions and reverted tickets.
    """
    backend = _resolve_backend_name()
    report = DoctorReport(version=__version__, backend=backend)
    report.checks.append(CheckResult("resolved backend", ok=True, detail=backend.value))
    report.checks.append(_check_backend_binary(backend))
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    report.checks.append(_check_state_file())
    report.checks.append(_check_dev_queue())
    if reap:
        report.checks.append(_check_reconcile())
    return report


def format_report(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as a human-readable block."""
    lines = [f"cw {report.version}"]
    for check in report.checks:
        mark = "OK" if check.ok else "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
    lines.append("")
    lines.append("status: healthy" if report.ok else "status: problems detected")
    return "\n".join(lines)
