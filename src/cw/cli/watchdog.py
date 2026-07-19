"""``cw watchdog`` CLI group: tick/install/uninstall/status (RFC 0008, #1015)."""

from __future__ import annotations

import click

from cw.cli._base import main


@main.group(name="watchdog")
def watchdog() -> None:
    """Mainstream watchdog: standalone detect+notify tick + service management."""


@watchdog.command(name="tick")
def watchdog_tick() -> None:
    """Run one watchdog tick: escalation sweep, dispatch-liveness, cycling."""
    from cw.watchdog import run_tick

    result = run_tick()
    click.echo(f"escalated: {result.escalated_ticket_ids}")
    click.echo(f"dispatch_loop_dead: {result.dispatch_loop_dead}")
    click.echo(f"cycling: {result.cycling_ticket_ids}")


@watchdog.command(name="install")
def watchdog_install() -> None:
    """Install the per-user systemd timer (Linux) or launchd agent (macOS).

    Only writes the unit file(s) — does not activate them. Prints the
    activation command to run afterward.
    """
    import platform

    from cw.watchdog import install

    paths = install()
    for path in paths:
        click.echo(f"wrote {path}")
    if platform.system() == "Darwin":
        click.echo(f"Run: launchctl load {paths[0]}")
    else:
        click.echo(
            "Run: systemctl --user daemon-reload"
            " && systemctl --user enable --now cw-watchdog.timer"
        )


@watchdog.command(name="uninstall")
def watchdog_uninstall() -> None:
    """Remove the installed watchdog unit file(s)."""
    from cw.watchdog import uninstall

    paths = uninstall()
    if not paths:
        click.echo("nothing installed")
        return
    for path in paths:
        click.echo(f"removed {path}")


@watchdog.command(name="status")
def watchdog_status() -> None:
    """Show whether the watchdog unit file(s) are installed."""
    from cw.watchdog import status

    result = status()
    click.echo(f"platform: {result.platform}")
    click.echo(f"installed: {result.installed}")
    for path in result.paths:
        click.echo(f"  {path}")
