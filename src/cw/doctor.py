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
from cw.exceptions import CwError
from cw.models import BackendName, CwState, Session
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


def _check_state_file() -> tuple[CheckResult, CwState | None]:
    """Verify sessions.json parses, returning the loaded state for downstream consumers.

    Returning the parsed state avoids a second ``load_state()`` call in
    ``run_doctor``: linkage checks reuse the same parsed object. On parse
    failure the second tuple element is ``None`` and downstream checks that
    need state should skip themselves; the failure is already visible via
    the returned ``CheckResult``.
    """
    path = state_file()
    try:
        state = load_state()
    except Exception as exc:
        return (
            CheckResult("sessions.json", ok=False, detail=f"load failed: {exc}"),
            None,
        )
    return CheckResult("sessions.json", ok=True, detail=str(path)), state


def _check_dev_queue() -> CheckResult:
    try:
        load_dev_queue()
    except Exception as exc:
        return CheckResult("dev_queue.json", ok=False, detail=f"load failed: {exc}")
    return CheckResult("dev_queue.json", ok=True, detail="parseable")


def _check_linkage(state: CwState) -> list[CheckResult]:
    """Detect parent/worker linkage drift in session state.

    Returns one :class:`CheckResult` per drift type:

    * ``linkage/dangling-worker`` — an orchestrator's ``worker_session_ids``
      references a session ID absent from state.
    * ``linkage/dangling-parent`` — a worker's ``parent_session_id`` points at
      a session absent from state.
    * ``linkage/asymmetric`` — one side knows about the link but the other
      side doesn't (forward-only or reverse-only reference).

    All three results are always returned; each is ``ok=True`` when no drift
    of that type is detected.
    """
    # Indexes built once: O(1) membership and lookup throughout the function.
    session_ids = {s.id for s in state.sessions}
    session_by_id: dict[str, Session] = {s.id: s for s in state.sessions}

    # --- dangling-worker: orchestrator.worker_session_ids → missing session ---
    dangling_worker_msgs: list[str] = [
        f"orchestrator {sess.id!r} references missing worker {wid!r}"
        " — remove the stale ID from worker_session_ids"
        for sess in state.sessions
        for wid in sess.worker_session_ids
        if wid not in session_ids
    ]

    if dangling_worker_msgs:
        dw_detail = "; ".join(dangling_worker_msgs)
        dw_result = CheckResult("linkage/dangling-worker", ok=False, detail=dw_detail)
    else:
        dw_result = CheckResult("linkage/dangling-worker", ok=True, detail="")

    # --- dangling-parent: worker.parent_session_id → missing session ---
    dangling_parent_msgs: list[str] = [
        f"worker {sess.id!r} references missing parent {sess.parent_session_id!r}"
        " — clear parent_session_id or restore the parent session"
        for sess in state.sessions
        if sess.parent_session_id is not None
        and sess.parent_session_id not in session_ids
    ]

    if dangling_parent_msgs:
        dp_detail = "; ".join(dangling_parent_msgs)
        dp_result = CheckResult("linkage/dangling-parent", ok=False, detail=dp_detail)
    else:
        dp_result = CheckResult("linkage/dangling-parent", ok=True, detail="")

    # --- asymmetric: one side of the link is missing ---
    # Build a map: parent_id → {set of worker IDs that claim it as parent}
    claimed_by: dict[str, set[str]] = {}
    for sess in state.sessions:
        if sess.parent_session_id is not None and sess.parent_session_id in session_ids:
            claimed_by.setdefault(sess.parent_session_id, set()).add(sess.id)

    # Forward check: orchestrator lists a worker, but worker doesn't claim it back.
    # Workers already caught as dangling are skipped (wid not in session_by_id).
    fwd_msgs: list[str] = []
    for sess in state.sessions:
        for wid in sess.worker_session_ids:
            worker = session_by_id.get(wid)
            if worker is None or worker.parent_session_id == sess.id:
                continue
            fwd_msgs.append(
                f"orchestrator {sess.id!r} lists worker {wid!r},"
                f" but worker's parent_session_id is {worker.parent_session_id!r}"
                " — update parent_session_id on the worker"
            )

    # Reverse check: worker claims this session as parent, but session
    # doesn't list the worker in its worker_session_ids.
    rev_msgs: list[str] = [
        f"worker {wid!r} claims parent {sess.id!r},"
        f" but {sess.id!r}'s worker_session_ids does not include it"
        " — add the worker ID to worker_session_ids"
        for sess in state.sessions
        for wid in claimed_by.get(sess.id, set())
        if wid not in sess.worker_session_ids
    ]

    asym_msgs = fwd_msgs + rev_msgs

    if asym_msgs:
        asym_detail = "; ".join(asym_msgs)
        asym_result = CheckResult("linkage/asymmetric", ok=False, detail=asym_detail)
    else:
        asym_result = CheckResult("linkage/asymmetric", ok=True, detail="")

    return [dw_result, dp_result, asym_result]


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        adapter = get_cmux_adapter()
    except CwError as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"adapter unavailable: {exc}",
        )
    try:
        reconcile_report = reconcile(adapter)
    except CwError as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"reconcile failed: {exc}",
        )
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

    Linkage drift checks (parent/worker reference integrity) are always run,
    independent of the *reap* flag.
    """
    backend = _resolve_backend_name()
    report = DoctorReport(version=__version__, backend=backend)
    report.checks.append(CheckResult("resolved backend", ok=True, detail=backend.value))
    report.checks.append(_check_backend_binary(backend))
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    state_check, link_state = _check_state_file()
    report.checks.append(state_check)
    report.checks.append(_check_dev_queue())

    # Linkage checks reuse the state already loaded by _check_state_file.
    # If state failed to load, state_check is ok=False and the user sees the
    # underlying problem; skipping linkage is correct (cascading from a
    # failed parse would just spam noise).
    if link_state is not None:
        report.checks.extend(_check_linkage(link_state))

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
