"""Rendering helpers for a :class:`DoctorReport` (human-readable and JSON)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.doctor._shared import DoctorReport


def format_report(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as a human-readable block."""
    lines = [f"cw {report.version}"]
    for check in report.checks:
        if check.warn:
            mark = "WARN"
        elif check.ok:
            mark = "OK"
        else:
            mark = "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
    if report.wedge_findings:
        lines.append("")
        lines.append("wedge findings:")
        for wf in report.wedge_findings:
            lines.append(f"  [{wf.wedge_class}] ticket={wf.ticket_id}")
            lines.append(f"    {wf.recipe}")
    lines.append("")
    if not report.ok:
        footer = "status: problems detected"
    elif report.clean:
        footer = "status: healthy"
    else:
        footer = "status: healthy — advisory warnings"
    lines.append(footer)
    return "\n".join(lines)


def format_report_json(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as JSON."""
    return json.dumps(
        {
            "version": 1,
            "ok": report.ok,
            "clean": report.clean,
            "checks": [
                {"name": c.name, "ok": c.ok, "warn": c.warn, "detail": c.detail}
                for c in report.checks
            ],
            "wedge_findings": [
                {
                    "wedge_class": f.wedge_class,
                    "session_id": f.session_id,
                    "ticket_id": f.ticket_id,
                    "recipe": f.recipe,
                    "state_file": f.state_file,
                }
                for f in report.wedge_findings
            ],
        },
        indent=2,
    )
