"""Maintenance and inspection commands: doctor, upgrade-workers, init, schema, board."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from cw.board import run_board
from cw.cli._base import _complete_client, handle_errors, main
from cw.config import init_client, load_clients
from cw.doctor import (
    DoctorReport,
    WedgeFinding,
    _reap_session_by_selector,
    format_report,
    format_report_json,
    run_doctor,
)
from cw.doctor.routed_result_wedge import (
    WEDGE_ROUTED_RESULT_STRANDED,
    reap_routed_result_findings,
)
from cw.exceptions import CwError
from cw.executor import ClaudeNativeExecutor
from cw.models import Stage, StageExecutorConfig
from cw.onboarding import (
    CW_ALLOWLIST_ENTRY,
    install_claude_md_snippet,
    install_cw_allowlist,
    install_sessionstart_hook,
    register_mcp_servers,
)
from cw.schema import REGISTRY, format_json, format_tldr


def _require_routed_confirmation(
    report: DoctorReport, routed: list[WedgeFinding], hint: str
) -> None:
    """Return a structured confirmation response for machine callers."""
    data = json.loads(format_report_json(report))
    data["confirmation_required"] = True
    data["routed_result_session_ids"] = [finding.session_id for finding in routed]
    data["confirmation_hint"] = hint
    click.echo(json.dumps(data, indent=2))
    raise click.exceptions.Exit(0 if report.ok else 1)


def _select_routed_findings(
    report: DoctorReport,
    routed: list[WedgeFinding],
    *,
    as_json: bool,
    yes: bool,
    requested_ids: set[str],
) -> list[WedgeFinding]:
    """Select class-11 records without granting an unscoped batch close."""
    if as_json and not yes:
        _require_routed_confirmation(
            report,
            routed,
            "rerun with --yes and --routed-session-id for each session",
        )
    if yes and len(routed) > 1 and not requested_ids:
        if as_json:
            _require_routed_confirmation(
                report,
                routed,
                "provide --routed-session-id for every session",
            )
        message = (
            "--yes requires --routed-session-id for each routed-result "
            "session when more than one is found"
        )
        raise click.ClickException(message)
    if requested_ids:
        return [finding for finding in routed if finding.session_id in requested_ids]
    if yes:
        return routed[:1]
    return [
        finding
        for finding in routed
        if click.confirm(
            f"Close routed-result session {finding.session_id}?", default=False
        )
    ]


def _reap_routed_report(
    report: DoctorReport,
    *,
    as_json: bool,
    yes: bool,
    routed_session_ids: tuple[str, ...],
) -> DoctorReport:
    """Run only the explicitly confirmed class-11 close path."""
    routed = [
        finding
        for finding in report.wedge_findings
        if finding.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
    ]
    if not as_json:
        click.echo("cw doctor --reap will close these routed-result sessions:")
        for finding in routed:
            click.echo(f"  session={finding.session_id} ticket={finding.ticket_id}")
    selected = _select_routed_findings(
        report,
        routed,
        as_json=as_json,
        yes=yes,
        requested_ids=set(routed_session_ids),
    )
    if selected:
        reap_routed_result_findings(selected)
    return report


@main.command()
@click.option(
    "--reap",
    is_flag=True,
    help="Also reconcile state with the live multiplexer and reap phantoms.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output report as JSON.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Confirm an explicitly scoped routed-result reap without prompting.",
)
@click.option(
    "--routed-session-id",
    "routed_session_ids",
    multiple=True,
    help="Class-11 session id to reap; may be repeated with --yes.",
)
@click.argument("session", required=False, default=None)
@handle_errors
def doctor(
    reap: bool,
    session: str | None,
    as_json: bool,
    yes: bool,
    routed_session_ids: tuple[str, ...],
) -> None:
    """Run environment preflight checks and print a health report.

    Reports daemon health, session count, and connectivity status.
    Also checks config file locations and validity, and state file parseability.
    Exits non-zero if any check fails.

    With ``--reap``, also detects and repairs the following wedge conditions:

    \b
    wedge/task-running-no-session
      Queue task is RUNNING but has no associated live session.
      Action: revert queue task to PENDING.
      Recipe: cw doctor --reap

    \b
    wedge/task-running-completed-session
      Queue task is RUNNING but its session is already COMPLETED.
      Action: revert queue task to PENDING.
      Recipe: cw doctor --reap

    \b
    wedge/repo-ahead-of-queue (advisory only)
      Branch is pushed to remote but queue task is still RUNNING.
      No automatic mutation — inspect and resolve manually.
      Recipe: cw spawn-complete <ticket_id> [--status shipped]

    \b
    wedge/active-no-daemon-entry
      ACTIVE/IDLE session has no matching daemon live entry — session crashed
      without writing a terminal sentinel (e.g. SSH push failure, OOM).
      Action: mark session COMPLETED, revert queue task to PENDING, release
      the hook context lock so the worktree can be reused.
      Recipe: cw doctor --reap

    \b
    wedge/active-routed-result-stranded
      ACTIVE/IDLE session whose result was already routed (its ticket's row
      advanced) but which never completed: no occupied row binds it, its
      transcript is stale, and it still holds a ceiling slot and worktree.
      Reported by every cw doctor run; nothing closes it automatically.
      Action (--reap only, after explicit per-session confirmation): mark
      confirmed sessions of this class COMPLETED and stop their daemon
      workers; never touches a queue row. JSON callers must provide --yes and
      explicit --routed-session-id values.
      Recipe: cw doctor --reap, or cw spawn close --confirmed-dead <id>

    ``--reap`` also reconciles session state against the native daemon
    roster, marking phantom sessions COMPLETED and reverting their tickets
    to PENDING.
    """
    report: DoctorReport | None = None
    if reap and session:
        scoped_report = run_doctor(reap=False)
        routed_for_session = [
            finding
            for finding in scoped_report.wedge_findings
            if finding.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
            and finding.session_id == session
        ]
        if routed_for_session:
            report = scoped_report
            routed_session_ids = (session,)
        else:
            ok = _reap_session_by_selector(session, bounded=True)
            if not ok:
                click.echo(f"No session found matching {session!r}", err=True)
                raise click.exceptions.Exit(1)
            return
    if session and not reap:
        click.echo("SESSION argument has no effect without --reap", err=True)
    if reap:
        # Class-11 is deliberately isolated from the general doctor reaper:
        # its confirmation can authorize only these session-only closes.
        if report is None:
            report = run_doctor(reap=False)
        routed = [
            finding
            for finding in report.wedge_findings
            if finding.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
        ]
        if routed:
            report = _reap_routed_report(
                report,
                as_json=as_json,
                yes=yes,
                routed_session_ids=routed_session_ids,
            )
        else:
            report = run_doctor(reap=True)
    else:
        report = run_doctor(reap=False)
    report = report or run_doctor(reap=False)
    if as_json:
        click.echo(format_report_json(report))
        raise click.exceptions.Exit(0 if report.ok else 1)
    click.echo(format_report(report))
    if not report.ok:
        raise click.exceptions.Exit(1)


@main.command(name="upgrade-workers")
@handle_errors
def upgrade_workers() -> None:
    """Restart all daemon-managed background sessions via ``claude respawn --all``.

    Run after upgrading cw or the Claude CLI so background workers pick up
    the current Claude binary. Wraps ``claude respawn --all`` (RFC 0001 Row 7);
    closes the gap previously filled by hand-rolled daemon-restart logic.

    Propagates the subprocess exit code. Surfaces stdout to the user; on
    non-zero exit also surfaces stderr.
    """
    try:
        result = subprocess.run(
            ["claude", "respawn", "--all"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        msg = (
            "claude binary not found on PATH. Install Claude Code, "
            "then re-run 'cw upgrade-workers'."
        )
        raise click.ClickException(msg) from e
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.returncode != 0:
        if result.stderr:
            click.echo(result.stderr, nl=False, err=True)
        raise click.exceptions.Exit(result.returncode)


def _run_onboarding_steps(workspace: Path, name: str) -> None:
    """Call all four onboarding functions and print the four status lines."""
    mcp_changed = register_mcp_servers(workspace, name)
    allow_changed = install_cw_allowlist()
    hook_changed = install_sessionstart_hook(workspace)
    md_changed = install_claude_md_snippet(workspace)
    mcp_status = "registered" if mcp_changed else "already configured"
    click.echo(f"  .mcp.json              — MCP servers {mcp_status}")
    click.echo(
        f"  ~/.claude/settings.json — {CW_ALLOWLIST_ENTRY} allow entry "
        f"{'added' if allow_changed else 'already configured'}"
    )
    click.echo(
        f"  .claude/settings.json  — SessionStart hook "
        f"{'added' if hook_changed else 'already configured'}"
    )
    click.echo(
        f"  .claude/CLAUDE.md      — cw snippet "
        f"{'written' if md_changed else 'already configured'}"
    )


def _onboard_or_warn(path: Path, name: str, *, no_onboarding: bool) -> None:
    """Run onboarding steps, or warn that onboarding was skipped."""
    if not no_onboarding:
        click.echo()
        click.echo("Agent onboarding:")
        _run_onboarding_steps(path, name)
    else:
        # defense-in-depth dispatch check deferred — file follow-on if wanted
        click.echo(
            f"Warning: client '{name}' will not be runnable until onboarding "
            f"is complete. Run: cw init {name} --onboard-only"
        )


def _print_init_next_steps(name: str, *, no_onboarding: bool) -> None:
    """Print post-init guidance, pointing at onboarding first if it was skipped."""
    click.echo()
    click.echo("Next steps:")
    if no_onboarding:
        click.echo(f"  cw init {name} --onboard-only  # Complete onboarding (required)")
    else:
        click.echo(f"  cw start {name}              # Start a session")
    click.echo("  cw config                    # View configuration")


@main.command(name="init")
@click.argument("name", required=False, default=None)
@click.option(
    "--path",
    "-p",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Path to the project repository.",
)
@click.option("--branch", "-b", default="main", help="Default branch name.")
@click.option(
    "--purposes",
    default=None,
    help="Comma-separated session purposes (e.g. impl,idea,debt).",
)
@click.option(
    "--no-onboarding/--onboarding",
    default=False,
    help="Skip all agent-onboarding steps (MCP servers, allowlist, hooks, CLAUDE.md).",
)
@click.option(
    "--onboard-only",
    is_flag=True,
    default=False,
    help="Run onboarding steps only; skip init_client (client must already exist).",
)
@handle_errors
def init(
    name: str | None,
    path: Path | None,
    branch: str,
    purposes: str | None,
    no_onboarding: bool,
    onboard_only: bool,
) -> None:
    """Initialize a new client configuration.

    \b
    Non-interactive (scriptable):
      cw init my-project --path /path/to/repo
      cw init my-project --path /path/to/repo --branch develop

    \b
    Interactive (human-friendly):
      cw init

    \b
    Re-run onboarding for an existing client:
      cw init my-project --onboard-only
    """
    if no_onboarding and onboard_only:
        msg = "--no-onboarding and --onboard-only are mutually exclusive"
        raise CwError(msg)

    if onboard_only:
        if name is None:
            msg = "Name is required with --onboard-only"
            raise CwError(msg)
        cfg = load_clients()
        client = cfg.get(name)
        if client is None:
            msg = (
                f"Client '{name}' not found — run 'cw init {name} --path <repo>' first"
            )
            raise CwError(msg)
        workspace = client.workspace_path
        _run_onboarding_steps(workspace, name)
        click.echo(f"Onboarding complete for '{name}'.")
        return

    if name is None:
        # Interactive mode
        name = click.prompt("Client name")
        if path is None:
            path_str = click.prompt("Repository path", type=str)
            resolved = Path(path_str).resolve()
            if not resolved.is_dir():
                msg = f"Path does not exist or is not a directory: {resolved}"
                raise CwError(msg)
            path = resolved
        branch = click.prompt("Default branch", default=branch)

    if path is None:
        msg = (
            "Path is required: use --path or run without arguments for interactive mode"
        )
        raise CwError(msg)

    purpose_list = None
    if purposes:
        purpose_list = [p.strip() for p in purposes.split(",")]

    init_client(name, path, default_branch=branch, auto_purposes=purpose_list)

    click.echo(f"Added client '{name}' to configuration.")

    _onboard_or_warn(path, name, no_onboarding=no_onboarding)
    _print_init_next_steps(name, no_onboarding=no_onboarding)


# --- Schema command group ---


@main.group()
def schema() -> None:
    """Inspect Pydantic model schemas for AutoDevResult, TicketTask, Session."""


@schema.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON array.")
def schema_list(as_json: bool) -> None:
    """List available schema names."""
    names = sorted(REGISTRY)
    if as_json:
        click.echo(json.dumps(names))
    else:
        for name in names:
            click.echo(name)


@schema.command(name="show")
@click.argument("name")
@click.option(
    "--format",
    "fmt",
    default="tldr",
    type=click.Choice(["json", "tldr"]),
    show_default=True,
    help="Output format. 'json' is raw model_json_schema() (no envelope).",
)
def schema_show(name: str, fmt: str) -> None:
    """Show schema for NAME.

    Available schemas: auto-dev-result, ticket-task, session.
    --format=json outputs raw model_json_schema() with no cw_version or
    other envelope.  --format=tldr (default) outputs a compact human-
    readable field table plus enum tables for Status / Tier fields.
    """
    if name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY))
        msg = f"Unknown schema {name!r}. Available: {available}"
        raise click.UsageError(msg)
    model_cls = REGISTRY[name]
    if fmt == "json":
        click.echo(format_json(model_cls))
    else:
        click.echo(format_tldr(model_cls))


@schema.command(name="stage-output")
@click.argument("stage")
def schema_stage_output(stage: str) -> None:
    """Show the sentinel JSON schema for STAGE.

    STAGE must be one of: harden, plan, impl, review, finalize.
    # Why: bridge — every stage returns the AutoDevResult contract until
    # per-stage models land (RFC 0005, post-A3)
    """
    try:
        stage_enum = Stage(stage)
    except ValueError as exc:
        valid = ", ".join(s.value for s in Stage)
        msg = f"Unknown stage {stage!r}. Valid stages: {valid}"
        raise click.UsageError(msg) from exc
    executor = ClaudeNativeExecutor(config=StageExecutorConfig())
    schema_dict = executor.stage_sentinel_schema(stage_enum)
    click.echo(json.dumps(schema_dict, indent=2))


@main.command(name="board")
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Print one frame and exit (for non-TTY/CI).",
)
@click.option(
    "--interval",
    type=int,
    default=5,
    show_default=True,
    help="Seconds between data refreshes (1-60).",
)
@click.option(
    "--client",
    "client_filter",
    default=None,
    shell_complete=_complete_client,
    help="Only render this client.",
)
@click.option(
    "--raw-events",
    is_flag=True,
    default=False,
    help="Show raw event stream instead of aggregated ticks.",
)
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help="Append a session-grouped detail panel (worktree contention).",
)
@handle_errors
def board(
    once: bool,
    interval: int,
    client_filter: str | None,
    raw_events: bool,
    detail: bool,
) -> None:
    """Lane x stage pipeline cockpit (RFC 0005 D1).

    Displays all dev-queue tickets grouped by client -> lane -> stage.
    Reads state lock-free; does not reconcile or mutate.

    Use --once for a static snapshot (CI-friendly).
    """
    run_board(
        once=once,
        interval=interval,
        client_filter=client_filter,
        raw_events=raw_events,
        detail=detail,
    )
