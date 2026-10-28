"""Text rendering for ``cw queue peek``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from collections.abc import Iterable


def print_table(rows: Iterable[dict[str, Any]]) -> None:
    """Print rows as a formatted text table with a suggested-stops footer."""
    rows = list(rows)
    if not rows:
        click.echo("No RUNNING tasks found.")
        return
    cols = [
        ("ticket", 7),
        ("session", 12),
        ("att", 3),
        ("age_m", 7),
        ("idle_m", 7),
        ("stage", 18),
        ("status", 22),
        ("pr", 6),
        ("pr_state", 9),
        ("recommend", 14),
    ]
    header = "  ".join(f"{name:<{w}}" for name, w in cols)
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        cells = []
        for name, w in cols:
            key = name.replace("_m", "_min").replace("att", "attempts")
            val = (
                r.get(key, "-")
                if name in {"age_m", "idle_m", "att"}
                else r.get(name, "-") or "-"
            )
            cells.append(f"{val!s:<{w}}")
        click.echo("  ".join(cells))
        if r.get("recommend") != "WAIT":
            click.echo(f"    └─ {r.get('reason')}")
    # AWAITING_OPERATOR rows are excluded by construction: a parked row is an
    # answer candidate (`cw session send`), not a stop candidate (#2212).
    actionable = [r for r in rows if r["recommend"].startswith("STOP")]
    if actionable:
        click.echo()
        click.echo("Suggested stops:")
        for r in actionable:
            click.echo(
                f"  cw spawn close {r['session']}  # #{r['ticket']} — {r['reason']}"
            )
