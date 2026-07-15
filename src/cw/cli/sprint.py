"""``cw sprint`` — RFC → GitHub sprint block: plan, then apply.

Thin CLI wrapper over the already-implemented :mod:`cw.sprint` mechanics
(parse, build_plan, apply_plan). ``plan`` renders a reviewable JSON artifact
from an RFC; ``apply`` idempotently files it to GitHub. Version resolution
(``_resolve_version``) lives here, not in ``cw.sprint``, because it is a CLI
policy decision (what to do when no ``--version`` is given), not a pure
transform.
"""

from __future__ import annotations

from pathlib import Path

import click

from cw import gh
from cw.cli._base import handle_errors, main
from cw.exceptions import SprintApplyError
from cw.sprint import (
    AppliedBuildout,
    BuildoutPlan,
    apply_plan,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)

_VERSION_PARTS = 3
_FALLBACK_VERSION = "0.0.0"


def _resolve_version(_root: Path) -> str:
    """Minor-bump the latest release tag, or fall back to ``"0.0.0"``.

    Falls back on any lookup failure (``ok=False``) or a tag that doesn't
    parse as a plain ``MAJOR.MINOR.PATCH`` (a pre-release/build suffix, a
    non-numeric part, etc.) — never guesses at what a malformed tag means.
    *_root* is accepted for signature symmetry with the other config/RFC
    loaders this command wires together; the version lookup itself talks to
    ``gh``, not the local checkout.
    """
    tag, ok = gh.latest_release_tag()
    if not ok or tag is None:
        return _FALLBACK_VERSION
    stripped = tag.removeprefix("v")
    parts = stripped.split(".")
    if len(parts) != _VERSION_PARTS or not all(part.isdigit() for part in parts):
        return _FALLBACK_VERSION
    major, minor, _patch = (int(part) for part in parts)
    return f"{major}.{minor + 1}.0"


@main.group()
def sprint() -> None:
    """RFC -> GitHub sprint block: plan, then apply."""


@sprint.command("plan")
@click.argument("rfc_path")
@click.option(
    "--out",
    required=True,
    type=click.Path(path_type=Path),
    help="Where to write the plan JSON.",
)
@click.option(
    "--root",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root (defaults to the current directory).",
)
@click.option(
    "--version",
    "version_override",
    default=None,
    help="Milestone version (defaults to a minor bump of the latest release tag).",
)
@handle_errors
def sprint_plan(
    rfc_path: str, out: Path, root: Path | None, version_override: str | None
) -> None:
    """Parse RFC_PATH and write a reviewable buildout plan to --out."""
    resolved_root = root if root is not None else Path.cwd()
    cfg = load_buildout_config(resolved_root)
    doc = parse_rfc(load_rfc_text(rfc_path, resolved_root))
    # version_override short-circuits: an explicit --version never triggers
    # a `gh` call, so a caller pinning a version doesn't need network/gh
    # access at all.
    version = version_override or _resolve_version(resolved_root)
    plan = build_plan(doc, cfg, version)
    out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    click.echo(f"Wrote plan: {out}")
    click.echo(f"Milestone: {plan.milestone_title}")
    click.echo(f"Epics: {len(plan.epics)}  Tickets: {len(plan.tickets)}")
    for sprint_num in sorted(plan.sprint_map):
        codes = ", ".join(plan.sprint_map[sprint_num])
        click.echo(f"  Sprint {sprint_num}: {codes}")


def _echo_applied(applied: AppliedBuildout) -> None:
    """Echo epic/ticket issue numbers and skipped items from an applied buildout."""
    click.echo(f"Milestone: #{applied.milestone_number}")
    for code, number in applied.epic_numbers.items():
        click.echo(f"  Epic {code}: #{number}")
    for code, number in applied.ticket_numbers.items():
        click.echo(f"  Ticket {code}: #{number}")
    if applied.skipped:
        click.echo(f"Skipped (already existed): {', '.join(applied.skipped)}")
    if applied.backfilled:
        click.echo(f"Backfilled children checklist: {', '.join(applied.backfilled)}")


@sprint.command("apply")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be created without calling gh.",
)
@handle_errors
def sprint_apply(plan_file: Path, dry_run: bool) -> None:
    """Idempotently apply PLAN_FILE (written by `cw sprint plan`) to GitHub."""
    plan = BuildoutPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))

    if dry_run:
        click.echo(f"Would create/reuse milestone: {plan.milestone_title}")
        for epic in plan.epics:
            click.echo(f"  Would create epic: {epic.title}")
        for ticket in plan.tickets:
            click.echo(f"  Would create ticket: {ticket.title}")
        return

    try:
        applied = apply_plan(plan)
    except SprintApplyError as exc:
        # exc.applied can be None (failure before any state accumulated, e.g.
        # the milestone lookup itself) — only banner when there is partial
        # state worth showing the operator.
        if isinstance(exc.applied, AppliedBuildout):
            click.echo("Partial progress before failure:")
            _echo_applied(exc.applied)
        raise

    click.echo("Buildout applied.")
    _echo_applied(applied)
