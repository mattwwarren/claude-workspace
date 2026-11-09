"""``cw review signoff render`` — the PR-body sign-off section (#3043, RFC 0017 II2).

A thin adapter: the verifier verdict comes from
:func:`cw.cli.review.attestation.attestation_verdict`, the opt-in and the
redaction anchors from the LIVE client config, the accepted-debt rows from the
deferred-findings artifact, and the section itself from
:mod:`cw.review_signoff`. It never calls ``gh``, never writes a file or a ref,
and prints the new PR body on stdout for the caller's single ``gh pr edit``.
Contract: ``docs/headless-contract.md`` section 13.7.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import click
from pydantic import ValidationError

from cw.cli._base import handle_errors
from cw.cli.review._group import review
from cw.cli.review.attestation import (
    CONTEXT_RELATIVE_PATH,
    attestation_verdict,
    live_client,
    raw_attestation,
)
from cw.cli.review.check_approval import _guard_root, _string_field
from cw.cli.review.converge import read_context
from cw.models.client import CLIENT_CONTEXT_KEY
from cw.models.review_attestation import ReviewAttestation
from cw.redact_post_text import git_toplevel, redaction_anchors, resolve_home
from cw.review_adjudication import read_deferred_findings_md
from cw.review_signoff import (
    DebtView,
    SignoffResult,
    SkipReason,
    build_debt_view,
    compose_body,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.review_adjudication import Adjudication
    from cw.review_approval import AttestationVerdict

DEFERRED_RELATIVE_PATH = Path(".cw") / "deferred-findings.md"
_EXIT_UNCHANGED = 3

_BODY_FILE_HELP = (
    "File holding the PR body to start from (adjudication and override sections "
    "already composed, or the unmodified body). Required; must exist."
)
_CURRENT_BODY_FILE_HELP = (
    "File holding the PR body as it stands on the PR. Exit 3 when the new body "
    "equals it, ignoring CRLF and trailing newlines. Default: --body-file."
)
_PR_HEAD_HELP = (
    "The PR head commit (gh pr view --json headRefOid). Required. The section is "
    "written only when it equals HEAD."
)
_DEFERRED_HELP = (
    "DEFERRED-REVIEW-FINDINGS artifact that supplies the accepted-debt ticket "
    "links. Default: <guard root>/.cw/deferred-findings.md."
)
_TICKET_HELP = "Expected ticket id. Default: the context's top-level ticket_id."
_CLIENT_HELP = (
    "Client name to load the live client config for. Default: the context's "
    "top-level client."
)
_WORKTREE_HELP = (
    "Directory to run git in; its repository root is the guard root, or the "
    "directory itself when it is not in a repository. Default: the current "
    "directory."
)
_CONTEXT_HELP = (
    "Path to cw-context.json. Default: <guard root>/.claude/cw-context.json."
)


@review.group(name="signoff")
def signoff() -> None:
    """Write the head-pinned review sign-off into a PR body (RFC 0017).

    ``render`` is the only subcommand. It composes the ``## Review sign-off``
    section from the review attestation and prints the whole new PR body; the
    caller makes the single ``gh pr edit``.
    """


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def _emit(new: str, current: str) -> None:
    """Print the new body, or exit 3 with nothing printed when it is unchanged."""
    if _normalized(new) == _normalized(current):
        raise SystemExit(_EXIT_UNCHANGED)
    click.echo(new, nl=False)


def _gate(
    verdict: AttestationVerdict, pr_head: str, context: dict[str, object]
) -> ReviewAttestation | SkipReason:
    """The attestation to write, or the reason it is not written."""
    if not verdict.valid:
        return SkipReason(verdict.token)
    if verdict.head_sha != pr_head.lower():
        return SkipReason("head_mismatch")
    try:
        return ReviewAttestation.model_validate(raw_attestation(context))
    except ValidationError:
        return SkipReason("invalid:malformed")


def _read_entries(path: Path) -> list[Adjudication] | None:
    """The artifact entries, or None when it is unreadable."""
    try:
        return read_deferred_findings_md(path)
    except (ValueError, OSError):
        return None


def _debt_for(gate: ReviewAttestation | SkipReason, deferred: Path) -> DebtView:
    count = (
        len(gate.deferrals)
        if isinstance(gate, ReviewAttestation) and gate.deferrals is not None
        else None
    )
    return build_debt_view(_read_entries(deferred), count)


def _compose(
    body: str,
    gate: ReviewAttestation | SkipReason,
    *,
    pr_head: str,
    deferred: Path,
    client: ClientConfig,
    root: Path,
    context: dict[str, object],
) -> SignoffResult:
    return compose_body(
        body,
        gate=gate,
        pr_head=pr_head,
        debt=_debt_for(gate, deferred),
        home=resolve_home(os.environ),
        worktrees=redaction_anchors(client, git_toplevel(root), context),
    )


@signoff.command(name="render")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help=_BODY_FILE_HELP,
)
@click.option(
    "--current-body-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=_CURRENT_BODY_FILE_HELP,
)
@click.option("--pr-head", required=True, help=_PR_HEAD_HELP)
@click.option(
    "--deferred-findings",
    type=click.Path(path_type=Path),
    help=_DEFERRED_HELP,
)
@click.option("--ticket", help=_TICKET_HELP)
@click.option("--client", "-c", "client_name", help=_CLIENT_HELP)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path),
    default=".",
    show_default=False,
    help=_WORKTREE_HELP,
)
@click.option(
    "--context", "context_path", type=click.Path(path_type=Path), help=_CONTEXT_HELP
)
@handle_errors
def render(
    body_file: Path,
    current_body_file: Path | None,
    pr_head: str,
    deferred_findings: Path | None,
    ticket: str | None,
    client_name: str | None,
    worktree: Path,
    context_path: Path | None,
) -> None:
    """Print the PR body with the ``## Review sign-off`` section applied (RFC 0017).

    Reads the body from ``--body-file`` and prints the new body on stdout. It
    never calls ``gh``. The section is written only when the client opted in with
    ``review.attestation.pr_section`` (read from the live client config), the
    review sign-off attestation is ``valid`` at HEAD (the verifier behind ``cw
    review attestation check``) and ``--pr-head`` equals HEAD. When the client
    opted in but the section cannot be written, any existing section is removed.
    A client that has not opted in gets its body back unchanged and nothing on
    stderr.

    Exits 0 and prints the new body when it differs from ``--current-body-file``
    (default: ``--body-file``). Exits 3 and prints nothing when it is the same, so
    the caller skips ``gh pr edit``. Exits 2 on a usage error and 1 on an
    unexpected failure. An invalid attestation is never an error: the body comes
    back without the section.

    Each skip, strip or repair prints one ``friction: review_signoff: <token>``
    line on stderr; copy it verbatim into ``friction_highlights``. Never writes a
    file or a ref; it may run git.
    """
    root = _guard_root(worktree.resolve())
    body = body_file.read_bytes().decode("utf-8")
    current = (
        body
        if current_body_file is None
        else current_body_file.read_bytes().decode("utf-8")
    )
    context = read_context(context_path or root / CONTEXT_RELATIVE_PATH) or {}
    client = live_client(client_name or _string_field(context, CLIENT_CONTEXT_KEY))
    attestation_cfg = None if client is None else client.review.attestation
    if client is None or attestation_cfg is None or not attestation_cfg.pr_section:
        _emit(body, current)
        return
    verdict = attestation_verdict(root, context_path, ticket, client_name)
    result = _compose(
        body,
        _gate(verdict, pr_head, context),
        pr_head=pr_head.lower(),
        deferred=deferred_findings or root / DEFERRED_RELATIVE_PATH,
        client=client,
        root=root,
        context=context,
    )
    for line in result.frictions:
        click.echo(line, err=True)
    _emit(result.body, current)
