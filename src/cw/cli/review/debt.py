"""The ``cw review debt`` group and the adjudicate filing hook (#3041, RFC 0017 D-7).

``cw review adjudicate`` calls :func:`run_debt_filing` after it stamps the
verdict and writes the artifact, so every ticketless ``defer`` entry becomes a
review-debt ticket at deferral time. The four subcommands are the stage-side
entry points:

- ``file``: finalize's re-file-once gate (Step 4d step 2a).
- ``emit``: write redacted payloads for an MCP tracker.
- ``record``: verify and record the IDs a worker filed through MCP.
- ``link-pr``: link filed tickets to the PR (Step 4d step 2b).

This module owns the CLI decisions the library does not make: the identity
(``-c``/``--ticket`` over ``.claude/cw-context.json``), the tracker mode, the
client's repo directory (every ``gh`` call and the config read use it, never
the process cwd), the redaction anchors, and the row writes, which run under
``dev_queue_lock`` only, after the ``gh`` calls. The default artifact exists
only inside a cw session (#3113). stdout is one JSON object; stderr carries
the human lines and the ``friction:`` lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import click

from cw._git import run_git
from cw.cli._base import handle_errors
from cw.cli.review._debt_session import (
    NO_SESSION_LINE,
    TICKET_ID_KEY,
    ensure_default_artifact,
    resolve_artifact,
    session_context,
)
from cw.cli.review._group import review
from cw.cli.review._settle_store import client_tracker
from cw.config import get_client, load_effective_config
from cw.dev_queue.review_debt_records import read_review_debt, record_review_debt
from cw.exceptions import CwError
from cw.gh import fetch_pr_view, fetch_repo_visibility
from cw.models.client import CLIENT_CONTEXT_KEY
from cw.models.review_debt_record import EMITTED_MODE, ReviewDebtTracker
from cw.redact_post_text import (
    git_toplevel,
    redact_post_text,
    redaction_anchors,
    resolve_home,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.review_convergence import (
    DEBT_CURRENT_ROUND_WINDOW,
    LEDGER_RELATIVE_PATH,
    load_ledger,
    stale_defer_keys,
)
from cw.review_debt_filing import (
    CAUSE_AUTO_CREATE_OFF,
    CAUSE_MCP,
    DETAIL_MAX_CHARS,
    KEY_DISPLAY_CHARS,
    PR_VIEW_TIMEOUT_S,
    TRACKER_LINEAR,
    VISIBILITY_TIMEOUT_S,
    AuditWarning,
    DebtEntry,
    DebtRunStatus,
    FilingContext,
    FilingFailure,
    FilingFailureKind,
    TrackerMode,
    apply_reported,
    artifact_debt_entries,
    auto_create_issues,
    debt_entry,
    emit_review_debt,
    emit_verify_failed,
    file_review_debt,
    link_pr,
    linkable,
    split_fixture_entries,
    tracker_mode,
    unticketed,
    verify_reported_ids,
    write_artifact_tickets,
)
from cw.sprint import GhClient
from cw.tracker import TRACKER_GITHUB_ISSUES
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import ClientConfig
    from cw.models.review_debt_record import ReviewDebtRecord
    from cw.review_adjudication import Adjudication

_log = logging.getLogger(__name__)

_DEFAULT_OUT_DIR = Path(".cw/review-debt")
_GIT_TIMEOUT_S = 10
_UNKNOWN_BRANCH = "unknown"
#: ``FilingContext.visibility`` on an MCP client: the payload goes to Linear,
#: not to a GitHub repo, so there is no repo visibility to gate on.
_VISIBILITY_NOT_APPLICABLE = "NOT_APPLICABLE"
_EXIT_FAILED = 1
_FRICTION_DEBT_FILING = "friction: debt_filing"
_FRICTION_DEBT_FILING_FAILED = "friction: debt_filing_failed"
_FRICTION_DEBT_AUDIT_WARNING = "friction: debt_audit_warning"
_FRICTION_DEBT_PR_LINK = "friction: debt_pr_link"
_FRICTION_DEBT_PR_LINK_FAILED = "friction: debt_pr_link_failed"
_FRICTION_SKIPPED_NO_CONTEXT = "skipped_no_context"
_FRICTION_SKIPPED_NO_PR = "skipped_no_pr"
_FRICTION_SKIPPED_NO_SESSION = "skipped_no_session"
_FRICTION_REFUSED_FIXTURE = "refused_fixture_entry"


class _FrictionRule(NamedTuple):
    """Canonical grammar for one emitted debt friction line."""

    prefix: str
    value_pattern: str


_DEBT_FRICTION_GRAMMAR = (
    _FrictionRule(
        _FRICTION_DEBT_FILING,
        rf"(?:{_FRICTION_SKIPPED_NO_CONTEXT}|{TrackerMode.SKIP_NO_TRACKER.value}"
        rf"|{_FRICTION_SKIPPED_NO_SESSION}|{_FRICTION_REFUSED_FIXTURE})",
    ),
    _FrictionRule(_FRICTION_DEBT_FILING_FAILED, r"[1-9][0-9]*"),
    _FrictionRule(_FRICTION_DEBT_AUDIT_WARNING, r"[1-9][0-9]*"),
    _FrictionRule(_FRICTION_DEBT_PR_LINK, _FRICTION_SKIPPED_NO_PR),
    _FrictionRule(_FRICTION_DEBT_PR_LINK_FAILED, r"[1-9][0-9]*"),
)
_DEBT_FRICTION_PREFIXES = tuple(rule.prefix for rule in _DEBT_FRICTION_GRAMMAR)

_CLIENT_HELP = (
    "Client that owns the queue ticket. Defaults to the client in "
    ".claude/cw-context.json."
)
_TICKET_HELP = (
    "Queue ticket whose deferrals are handled. Defaults to the ticket_id in "
    ".claude/cw-context.json."
)
_ARTIFACT_HELP = (
    "DEFERRED-REVIEW-FINDINGS artifact to read and update. Defaults to "
    ".cw/deferred-findings.md under the cw session worktree root; outside a "
    "session there is no default."
)


class Identity(NamedTuple):
    """The queue ticket a filing run belongs to."""

    client: str
    ticket: str


class _Plan(NamedTuple):
    """The decided per-run inputs: tracker mode and the client's repo."""

    identity: Identity
    mode: TrackerMode
    auto_create: bool
    cfg: ClientConfig | None
    #: Anchor for git, the session context and the emit directory; ``None``
    #: keeps the process cwd (#3041 codex filing runs outside the worktree).
    cwd: Path | None = None


class _Outcome(NamedTuple):
    """What one file-or-emit run did, for the JSON line and stderr."""

    status: DebtRunStatus
    reason: str | None
    branch: str
    filed: int = 0
    deduped: int = 0
    emitted: int = 0
    failures: tuple[FilingFailure, ...] = ()
    unticketed: int = 0
    audit_warnings: tuple[AuditWarning, ...] = ()


def _str_value(context: dict[str, object] | None, key: str) -> str | None:
    value = context.get(key) if context is not None else None
    return value if isinstance(value, str) and value else None


def resolve_identity(client: str | None, ticket: str | None) -> Identity | None:
    """``-c``/``--ticket`` over the context's ``client``/``ticket_id``, or None."""
    context = session_context()
    resolved_client = client or _str_value(context, CLIENT_CONTEXT_KEY)
    resolved_ticket = ticket or _str_value(context, TICKET_ID_KEY)
    if resolved_client is None or resolved_ticket is None:
        return None
    return Identity(resolved_client, resolved_ticket)


def _require_identity(client: str | None, ticket: str | None) -> Identity:
    identity = resolve_identity(client, ticket)
    if identity is None:
        msg = (
            "no client or ticket: pass -c/--client and --ticket, or run inside a "
            "cw session whose .claude/cw-context.json names them"
        )
        raise click.UsageError(msg)
    try:
        get_client(identity.client)
    except CwError as exc:
        msg = f"unknown client '{identity.client}'"
        raise click.UsageError(msg) from exc
    return identity


def _require_row(identity: Identity) -> list[ReviewDebtRecord]:
    """Require the queue row while preserving the command's failure exit code."""
    try:
        return read_review_debt(identity.ticket, identity.client)
    except CwError as exc:
        msg = (
            f"no dev-queue task found for ticket '{identity.ticket}' "
            f"in client '{identity.client}'"
        )
        raise CwError(msg) from exc


def _branch(cwd: Path | None = None) -> str:
    """``git rev-parse --abbrev-ref HEAD`` in *cwd* (default: the process cwd)."""
    try:
        result = run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _UNKNOWN_BRANCH
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else _UNKNOWN_BRANCH


def _plan(identity: Identity) -> _Plan:
    mode = tracker_mode(client_tracker(identity.client).system)
    if mode is TrackerMode.SKIP_NO_TRACKER:
        return _Plan(identity, mode, auto_create=False, cfg=None)
    cfg = get_client(identity.client)
    auto = mode is TrackerMode.GITHUB and auto_create_issues(cfg.workspace_path)
    return _Plan(identity, mode, auto_create=auto, cfg=cfg)


def _repo(plan: _Plan) -> Path | None:
    return _git_dir(plan.cfg) if plan.cfg is not None else None


def _visibility(plan: _Plan) -> str | None:
    if plan.mode is TrackerMode.MCP:
        return _VISIBILITY_NOT_APPLICABLE
    return fetch_repo_visibility(timeout=VISIBILITY_TIMEOUT_S, cwd=_repo(plan))


def _filing_context(
    plan: _Plan,
    existing: tuple[ReviewDebtRecord, ...] | None,
    visibility: str | None,
) -> FilingContext:
    if plan.cfg is None:
        msg = "debt filing needs a resolved client"
        raise CwError(msg)
    ticket = plan.identity.ticket
    github = plan.mode is TrackerMode.GITHUB
    anchors = redaction_anchors(
        plan.cfg, git_toplevel(plan.cwd), session_context(plan.cwd)
    )
    return FilingContext(
        ticket_id=ticket,
        client=plan.identity.client,
        branch=_branch(plan.cwd),
        ticket_ref=f"#{ticket}" if github and ticket.isdigit() else ticket,
        tracker=cast(
            "ReviewDebtTracker",
            TRACKER_GITHUB_ISSUES if github else TRACKER_LINEAR,
        ),
        existing=existing,
        visibility=visibility,
        home=resolve_home(os.environ),
        worktrees=tuple(anchors),
    )


def merge_entries(
    artifact: Sequence[DebtEntry], applied: Sequence[DebtEntry]
) -> list[DebtEntry]:
    """One entry per key: the artifact's, with this call's evidence and line.

    An entry applied in this call carries the evidence the artifact drops;
    the artifact carries any ``ticket:`` already written.
    """
    by_key: dict[str, DebtEntry] = {}
    for entry in artifact:
        prior = by_key.get(entry.key)
        if prior is None or (entry.ticket and not prior.ticket):
            by_key[entry.key] = entry
    for entry in applied:
        prior = by_key.get(entry.key)
        by_key[entry.key] = entry._replace(ticket=prior.ticket) if prior else entry
    return list(by_key.values())


def _record(identity: Identity, records: Sequence[ReviewDebtRecord]) -> None:
    if records:
        record_review_debt(identity.ticket, identity.client, records)


def _write_back(path: Path | None, records: Sequence[ReviewDebtRecord]) -> None:
    if path is not None:
        write_artifact_tickets(path, records)


def _emit_friction(prefix: str, value: str) -> None:
    rule = next((r for r in _DEBT_FRICTION_GRAMMAR if r.prefix == prefix), None)
    if rule is None:
        msg = f"unsupported debt friction prefix: {prefix}"
        raise ValueError(msg)
    line = f"{prefix}: {value}"
    pattern = rf"{re.escape(rule.prefix)}: {rule.value_pattern}"
    if re.fullmatch(pattern, line) is None:
        msg = f"unsupported debt friction line: {line}"
        raise ValueError(msg)
    click.echo(line, err=True)


def _pending(
    entries: Sequence[DebtEntry], existing: tuple[ReviewDebtRecord, ...] | None
) -> list[DebtEntry]:
    ticketed = {r.key for r in existing or () if r.ticket_id is not None}
    return [entry for entry in unticketed(entries) if entry.key not in ticketed]


def _screened(entries: Sequence[DebtEntry]) -> list[DebtEntry]:
    """*entries* minus the test-fixture ones, each refused on stderr (#3113)."""
    fileable, refused = split_fixture_entries(entries)
    for entry in refused:
        key = entry.key[:KEY_DISPLAY_CHARS]
        line = f"{entry.file}: {key} is a test-fixture entry; remove it"
        click.echo(f"debt filing refused [fixture_path] {line}", err=True)
    if refused:
        _emit_friction(_FRICTION_DEBT_FILING, _FRICTION_REFUSED_FIXTURE)
    return fileable


def _no_session() -> _Outcome:
    """Skip a run with no artifact: no session gives a default (#3113)."""
    click.echo(NO_SESSION_LINE, err=True)
    return _Outcome(DebtRunStatus.SKIPPED, _FRICTION_SKIPPED_NO_SESSION, _branch())


def _file(
    plan: _Plan,
    entries: Sequence[DebtEntry],
    existing: tuple[ReviewDebtRecord, ...] | None,
    artifact: Path | None,
) -> _Outcome:
    visibility = _visibility(plan) if existing is not None else None
    ctx = _filing_context(plan, existing, visibility)
    result = file_review_debt(entries, ctx, GhClient(cwd=_repo(plan)))
    _record(plan.identity, result.records)
    _write_back(artifact, [*(existing or ()), *result.records])
    failed = tuple(result.failures)
    recovered = {r.key for r in result.records} & set(result.deduped_keys)
    return _Outcome(
        status=DebtRunStatus.INCOMPLETE if failed else DebtRunStatus.COMPLETE,
        reason=None,
        branch=ctx.branch,
        filed=len(result.records) - len(recovered),
        deduped=len(result.deduped_keys),
        failures=failed,
        unticketed=len({failure.key for failure in failed}),
        audit_warnings=result.audit_warnings,
    )


def _emit(
    plan: _Plan,
    entries: Sequence[DebtEntry],
    existing: tuple[ReviewDebtRecord, ...],
    out_dir: Path,
) -> tuple[_Outcome, list[tuple[str, str]], bool]:
    """Emit payloads; return the outcome, the ``(key, path)`` list and refusal."""
    ctx = _filing_context(plan, existing, _visibility(plan))
    cause = CAUSE_MCP if plan.mode is TrackerMode.MCP else CAUSE_AUTO_CREATE_OFF
    result = emit_review_debt(entries, ctx, out_dir, cause=cause)
    _record(plan.identity, result.records)
    files = [(f.key, str(f.path)) for f in result.files]
    status = (
        DebtRunStatus.MCP_PENDING
        if plan.mode is TrackerMode.MCP
        else DebtRunStatus.EMITTED_ONLY
    )
    outcome = _Outcome(
        status=DebtRunStatus.INCOMPLETE if result.refused else status,
        reason=(
            FilingFailureKind.VISIBILITY_UNDETERMINED.value if result.refused else None
        ),
        branch=ctx.branch,
        emitted=sum(1 for f in result.files if f.written),
        unticketed=len(_pending(entries, existing)),
        audit_warnings=tuple(result.audit_warnings),
    )
    return outcome, files, result.refused


def _file_or_emit(
    plan: _Plan,
    entries: Sequence[DebtEntry],
    artifact: Path | None,
    existing: tuple[ReviewDebtRecord, ...] | None,
) -> _Outcome:
    """The shared run behind ``adjudicate`` and ``cw review debt file``."""
    if plan.mode is TrackerMode.SKIP_NO_TRACKER:
        return _Outcome(
            DebtRunStatus.SKIPPED, TrackerMode.SKIP_NO_TRACKER.value, _branch(plan.cwd)
        )
    if not _pending(entries, existing):
        _write_back(artifact, existing or ())
        return _Outcome(DebtRunStatus.COMPLETE, None, _branch(plan.cwd))
    if existing is None or (plan.mode is TrackerMode.GITHUB and plan.auto_create):
        return _file(plan, entries, existing, artifact)
    out_dir = _DEFAULT_OUT_DIR if plan.cwd is None else plan.cwd / _DEFAULT_OUT_DIR
    return _emit(plan, entries, existing, out_dir)[0]


def _report(outcome: _Outcome) -> None:
    """The per-failure stderr lines, then the run's one friction line."""
    for failure in outcome.failures:
        click.echo(
            f"debt filing failed [{failure.kind.value}] "
            f"{failure.key[:KEY_DISPLAY_CHARS]}: "
            f"{failure.detail}",
            err=True,
        )
    for warning in outcome.audit_warnings:
        click.echo(
            f"warning: debt audit event {warning.event_type.value} could not be "
            f"recorded: {warning.detail}",
            err=True,
        )
    if outcome.audit_warnings:
        _emit_friction(
            _FRICTION_DEBT_AUDIT_WARNING,
            str(len(outcome.audit_warnings)),
        )
    if outcome.status is DebtRunStatus.SKIPPED:
        reason = outcome.reason or TrackerMode.SKIP_NO_TRACKER.value
        _emit_friction(_FRICTION_DEBT_FILING, reason)
    elif outcome.failures:
        _emit_friction(
            _FRICTION_DEBT_FILING_FAILED,
            str(len(outcome.failures)),
        )


def _emit_json(obj: dict[str, object]) -> None:
    click.echo(json.dumps(obj, indent=2, sort_keys=True))


def run_debt_filing(
    client: str | None,
    ticket: str | None,
    artifact: Path | None,
    applied: Sequence[Adjudication],
    *,
    cwd: Path | None = None,
) -> None:
    """``cw review adjudicate``'s hook: file every ticketless deferral.

    Never raises and never changes the caller's exit code. Silent when there
    is no ticketless deferral at all (nothing to file, so nothing skipped).
    Outside a cw session with no *artifact* it reads and writes nothing
    (``skipped_no_session``, #3113). With deferrals but no identity it skips
    loudly (friction ``debt_filing: skipped_no_context``). *cwd* anchors git,
    the session context and the emit directory for a caller outside the
    session worktree; ``None`` keeps the process cwd.
    """
    applied_entries = [debt_entry(a) for a in applied if a.outcome == DEFERRED_OUTCOME]
    try:
        filing_artifact = resolve_artifact(artifact)
        if filing_artifact is None:
            if unticketed(applied_entries):
                _report(_no_session())
            return
        if artifact is None:
            # Persist before any skip: with no identity the deferrals must still
            # survive in the default artifact for a later `cw review debt file`.
            ensure_default_artifact(filing_artifact, applied)
        prior = artifact_debt_entries(filing_artifact)
        entries = _screened(merge_entries(prior, applied_entries))
        if not unticketed(entries):
            return
        identity = resolve_identity(client, ticket)
        if identity is None:
            click.echo(
                "debt filing skipped: no client/ticket (pass -c/--ticket, or run "
                "inside a cw session)",
                err=True,
            )
            _emit_friction(_FRICTION_DEBT_FILING, _FRICTION_SKIPPED_NO_CONTEXT)
            return
        plan = _plan(identity)._replace(cwd=cwd)
        if plan.mode is not TrackerMode.SKIP_NO_TRACKER and unticketed(entries):
            try:
                existing = tuple(_require_row(identity))
            except CwError:
                # Preserve one failure per candidate when the row disappeared;
                # the filing library also guarantees that no gh call is made.
                outcome = _file(plan, entries, None, filing_artifact)
                _report(outcome)
                return
        else:
            existing = None
        outcome = _file_or_emit(plan, entries, filing_artifact, existing)
    except (CwError, OSError) as exc:
        # Same redaction and cap as a library failure detail; no FilingContext
        # exists on this path, so the home and worktree anchors are passed direct.
        detail = redact_post_text(
            str(exc), home=resolve_home(os.environ), worktrees=()
        )[:DETAIL_MAX_CHARS]
        click.echo(
            f"debt filing failed [{FilingFailureKind.OTHER.value}] -: {detail}",
            err=True,
        )
        _emit_friction(_FRICTION_DEBT_FILING_FAILED, str(max(1, len(applied_entries))))
        return
    _report(outcome)


@review.group(name="debt")
def debt() -> None:
    """File, emit, record and link review-debt tickets (RFC 0017 D-7)."""


_client_option = click.option("-c", "--client", default=None, help=_CLIENT_HELP)
_ticket_option = click.option("--ticket", default=None, help=_TICKET_HELP)
_artifact_option = click.option(
    "--deferred-findings",
    "artifact",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help=_ARTIFACT_HELP,
)


def _stale_keys(ledger_path: Path) -> frozenset[str]:
    """Debt keys of settled defers the final review rounds no longer raise.

    Empty when the ledger is absent or unreadable: a read failure must never
    drop debt, so it is reported and every entry stays fileable.
    """
    try:
        ledger = load_ledger(ledger_path)
    except (OSError, ValueError) as exc:
        _log.warning(
            "review-debt stale filter skipped: ledger unreadable path=%s",
            ledger_path,
            exc_info=True,
        )
        click.echo(
            f"debt filing: convergence ledger unreadable ({exc.__class__.__name__}); "
            "filing every unticketed deferral",
            err=True,
        )
        return frozenset()
    return stale_defer_keys(ledger) if ledger is not None else frozenset()


def _current(entries: Sequence[DebtEntry], artifact: Path) -> list[DebtEntry]:
    """*entries* minus unticketed defers the final review rounds no longer raise.

    Review rounds record every SHOULD_FIX defer in the artifact, and a reviewer
    rewords or a fix removes a finding from round to round, so filing the whole
    artifact files findings that no longer apply. The ledger beside *artifact*
    says which defers one of the last ``DEBT_CURRENT_ROUND_WINDOW`` recorded
    rounds raised. A ticketed entry is never touched, and the artifact keeps
    every entry.
    """
    stale = _stale_keys(artifact.parent / LEDGER_RELATIVE_PATH.name)
    kept = [entry for entry in entries if entry.ticket or entry.key not in stale]
    skipped = len(entries) - len(kept)
    if skipped:
        click.echo(
            f"debt filing skipped {skipped} deferral(s) not raised in the last "
            f"{DEBT_CURRENT_ROUND_WINDOW} review rounds",
            err=True,
        )
    return kept


def _file_artifact(identity: Identity, artifact: Path) -> _Outcome:
    plan = _plan(identity)
    entries = _current(_screened(artifact_debt_entries(artifact)), artifact)
    existing = (
        tuple(_require_row(identity))
        if plan.mode is not TrackerMode.SKIP_NO_TRACKER and unticketed(entries)
        else None
    )
    return _file_or_emit(plan, entries, artifact, existing)


@debt.command(name="file")
@_client_option
@_ticket_option
@_artifact_option
@handle_errors
def debt_file(client: str | None, ticket: str | None, artifact: Path | None) -> None:
    """File one review-debt ticket per ticketless deferral (RFC 0017 D-7).

    Reads the DEFERRED-REVIEW-FINDINGS artifact, files every defer entry that
    has no ticket yet on a GitHub-tracked client, writes ticket: lines back and
    records the rows. Safe to re-run: deduplicates by row record, then by body
    marker; outside a cw session it needs --deferred-findings, else skips.
    Prints one JSON object. Exits 0 when complete, skipped or emitted-only; 1
    when any entry remains unfiled; 2 on a usage error.
    """
    identity = _require_identity(client, ticket)
    path = resolve_artifact(artifact)
    outcome = _file_artifact(identity, path) if path is not None else _no_session()
    _report(outcome)
    _emit_json(
        {
            "status": outcome.status,
            "reason": outcome.reason,
            "client": identity.client,
            "ticket": identity.ticket,
            "branch": outcome.branch,
            "filed": outcome.filed,
            "deduped": outcome.deduped,
            "emitted": outcome.emitted,
            "failed": len(outcome.failures),
            "unticketed": outcome.unticketed,
        }
    )
    if outcome.status in {DebtRunStatus.INCOMPLETE, DebtRunStatus.MCP_PENDING}:
        sys.exit(_EXIT_FAILED)


@debt.command(name="emit")
@_client_option
@_ticket_option
@_artifact_option
@click.option(
    "--out-dir",
    default=_DEFAULT_OUT_DIR,
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for the payload files. Defaults to .cw/review-debt/.",
)
@handle_errors
def debt_emit(
    client: str | None, ticket: str | None, artifact: Path | None, out_dir: Path
) -> None:
    """Write one redacted ticket payload per unticketed deferral (RFC 0017 D-7).

    For trackers filed through MCP, or when auto_create_issues is false. A
    re-emit never changes an existing payload file; outside a cw session it
    needs --deferred-findings, else skips. Prints the payload paths as JSON;
    iterate that list, not the directory.
    """
    plan = _plan(_require_identity(client, ticket))
    path = resolve_artifact(artifact)
    base: dict[str, object] = {"out_dir": str(out_dir), "count": 0, "files": []}
    if plan.mode is TrackerMode.SKIP_NO_TRACKER or path is None:
        reason = TrackerMode.SKIP_NO_TRACKER.value
        if path is None:
            click.echo(NO_SESSION_LINE, err=True)
            reason = _FRICTION_SKIPPED_NO_SESSION
        _emit_friction(_FRICTION_DEBT_FILING, reason)
        _emit_json({**base, "status": DebtRunStatus.SKIPPED, "reason": reason})
        return
    existing = tuple(_require_row(plan.identity))
    outcome, files, refused = _emit(
        plan, _screened(artifact_debt_entries(path)), existing, out_dir
    )
    _report(outcome)
    if refused:
        _emit_json(
            {
                **base,
                "status": DebtRunStatus.REFUSED,
                "reason": FilingFailureKind.VISIBILITY_UNDETERMINED.value,
            }
        )
        sys.exit(_EXIT_FAILED)
    listed = [{"key": key, "path": path} for key, path in files]
    _emit_json(
        {
            **base,
            "status": DebtRunStatus.EMITTED if files else DebtRunStatus.NOTHING_TO_EMIT,
            "reason": None,
            "count": len(files),
            "files": listed,
        }
    )


def _read_ids(ids: str) -> str | None:
    if ids == "-":
        return click.get_text_stream("stdin").read()
    try:
        return Path(ids).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _allowed_prefixes(client: str) -> frozenset[str]:
    prefix_map = load_effective_config().linear_prefix_map
    return frozenset(p for p, owner in prefix_map.items() if owner == client)


@debt.command(name="record")
@click.option(
    "--ids",
    required=True,
    help="JSON file of [{key, ticket_id, url}] the worker reported, or '-' for stdin.",
)
@_client_option
@_ticket_option
@_artifact_option
@handle_errors
def debt_record(
    ids: str, client: str | None, ticket: str | None, artifact: Path | None
) -> None:
    """Verify and record the ticket IDs a worker filed through MCP (RFC 0017 D-7).

    Checks the shape of the reported IDs only; cw has no Linear client, so it
    cannot prove the tickets exist. All-or-nothing: any failure records
    nothing. Exits 0 when recorded, 1 when verification fails.
    """
    identity = _require_identity(client, ticket)
    plan = _plan(identity)
    if plan.mode is not TrackerMode.MCP:
        msg = "cw review debt record requires a Linear/MCP tracker"
        raise CwError(msg)
    records = _require_row(identity)
    expected = [r for r in records if r.mode == EMITTED_MODE and r.ticket_id is None]
    result = verify_reported_ids(
        _read_ids(ids),
        frozenset(r.key for r in expected),
        _allowed_prefixes(identity.client),
    )
    if result.failures:
        pairs = [(f.key, f.reason) for f in result.failures]
        for key, reason in pairs:
            click.echo(f"debt verify failed [{reason}] {key or '-'}", err=True)
        if plan.cfg is not None:
            emit_verify_failed(_filing_context(plan, tuple(records), None), pairs)
        _emit_json(
            {
                "status": DebtRunStatus.VERIFY_FAILED,
                "recorded": [],
                "failures": [{"key": k, "reason": r} for k, r in pairs],
            }
        )
        sys.exit(_EXIT_FAILED)
    updated = apply_reported(expected, result.reported)
    _record(identity, updated)
    _write_back(resolve_artifact(artifact), updated)
    _emit_json(
        {
            "status": DebtRunStatus.RECORDED
            if updated
            else DebtRunStatus.NOTHING_EXPECTED,
            "recorded": [
                {"key": i.key, "ticket_id": i.ticket_id, "url": i.url}
                for i in result.reported
            ],
            "failures": [],
        }
    )


def _link_counts(status: str, pr_url: str | None, **counts: int) -> dict[str, object]:
    base: dict[str, object] = {
        "status": status,
        "pr_url": pr_url,
        "linked": 0,
        "already_linked": 0,
        "failed": 0,
        "skipped": 0,
    }
    return {**base, **counts}


def _pr_url(plan: _Plan, pr: int) -> str | None:
    view = fetch_pr_view(
        str(pr), timeout=PR_VIEW_TIMEOUT_S, fields="url", cwd=_repo(plan)
    )
    url = view.get("url") if view is not None else None
    return url if isinstance(url, str) and url else None


@debt.command(name="link-pr")
@click.option(
    "--pr",
    type=int,
    default=None,
    help="PR number to link. Omit to only report records that have no PR.",
)
@_client_option
@_ticket_option
@handle_errors
def debt_link_pr(pr: int | None, client: str | None, ticket: str | None) -> None:
    """Link filed review-debt tickets to the PR (RFC 0017 D-7).

    For each filed record without a pr_url, adds the
    <!-- cw-review-debt-pr: <url> --> marker line to the ticket body and posts
    one comment linking the PR, then sets pr_url. Idempotent. Never blocks
    finalize: link failures print a friction line and exit 0.
    """
    identity = _require_identity(client, ticket)
    records = _require_row(identity)
    unlinked = [r for r in records if linkable(r) and r.pr_url is None]
    skipped = sum(1 for r in records if not linkable(r))
    if pr is None:
        if unlinked:
            _emit_friction(_FRICTION_DEBT_PR_LINK, _FRICTION_SKIPPED_NO_PR)
        _emit_json(_link_counts(DebtRunStatus.SKIPPED_NO_PR, None, skipped=skipped))
        return
    if not unlinked:
        already = len(records) - skipped
        _emit_json(
            _link_counts(
                DebtRunStatus.NOTHING_TO_LINK,
                None,
                already_linked=already,
                skipped=skipped,
            )
        )
        return
    plan = _plan(identity)
    if plan.mode is not TrackerMode.GITHUB:
        # No GitHub tracker: never reach gh, which would act on the process
        # cwd's repository (an unset or unknown tracker must not link anything).
        _emit_friction(_FRICTION_DEBT_FILING, TrackerMode.SKIP_NO_TRACKER.value)
        _emit_json(
            _link_counts(
                DebtRunStatus.NOTHING_TO_LINK,
                None,
                skipped=skipped + len(unlinked),
            )
        )
        return
    pr_url = _pr_url(plan, pr)
    if pr_url is None:
        click.echo(f"debt pr link failed: could not resolve PR #{pr}", err=True)
        _emit_friction(_FRICTION_DEBT_PR_LINK_FAILED, str(len(unlinked)))
        _emit_json(
            _link_counts(
                DebtRunStatus.DEGRADED, None, failed=len(unlinked), skipped=skipped
            )
        )
        return
    result = link_pr(records, pr_url, GhClient(cwd=_repo(plan)))
    _record(identity, result.records)
    if result.failed:
        _emit_friction(_FRICTION_DEBT_PR_LINK_FAILED, str(result.failed))
    status = DebtRunStatus.DEGRADED if result.failed else DebtRunStatus.LINKED
    _emit_json(
        _link_counts(
            status,
            pr_url,
            linked=result.linked,
            already_linked=result.already_linked,
            failed=result.failed,
            skipped=result.skipped,
        )
    )
