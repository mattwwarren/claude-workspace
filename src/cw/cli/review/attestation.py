"""``cw review attestation check`` — verify the review sign-off (#3040, RFC 0017).

A thin adapter from the worker's ``cw-context.json``, the live client config
and ``HEAD`` onto :func:`cw.review_approval.evaluate_review_attestation`. The
attestation is read from the context (worker-writable, so a claim); the review
mode, ``review.default_merge_tolerance``, ``default_branch`` and the renumber
rules come from the LIVE cw-owned client config, never from the context. The
contract is ``docs/headless-contract.md`` section 13. Lockless; it may run git.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.cli.review._group import review
from cw.cli.review.check_approval import _guard_root, _string_field
from cw.cli.review.converge import read_context
from cw.config import get_client
from cw.exceptions import CwError
from cw.models.client import CLIENT_CONTEXT_KEY
from cw.models.review_attestation import REVIEW_ATTESTATION_KEY
from cw.review_approval import (
    INVALID_SCHEMA_VERSION,
    INVALID_UNREADABLE,
    AttestationVerdict,
    evaluate_review_attestation,
    resolve_commit,
)
from cw.review_attestation_tolerance import DEFAULT_RENUMBER_PATTERNS
from cw.review_convergence import review_move_tolerance

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.models.client import RenumberPattern
    from cw.review_convergence import MergeTolerance

#: The first cw-context schema carrying ``queue_metadata.review_attestation``.
MIN_ATTESTATION_CONTEXT_SCHEMA = 20
_CONTEXT_RELATIVE_PATH = Path(".claude") / "cw-context.json"
_EXIT_INVALID = 1

_TICKET_HELP = (
    "Expected ticket id. Default: the context's top-level ticket_id. An "
    "attestation for another ticket is invalid:wrong_ticket."
)
_CLIENT_HELP = (
    "Client name to verify against and to load the live client config for. "
    "Default: the context's top-level client. An attestation for another "
    "client is invalid:wrong_client."
)
_WORKTREE_HELP = (
    "Directory to run git in; its repository root is the guard root, or the "
    "directory itself when it is not in a repository. Default: the current "
    "directory."
)
_CONTEXT_HELP = (
    "Path to cw-context.json. Default: <guard root>/.claude/cw-context.json."
)
_JSON_HELP = (
    "Print one JSON object (verdict, valid, detail, offending_commit, "
    "offending_path, attestation_head_sha, head_sha, engine, "
    "reviewer_backend, ticket_id, client) instead of the bare verdict line. "
    "The exit code is unchanged."
)


@review.group(name="attestation")
def attestation() -> None:
    """Verify the head-pinned review sign-off attestation (RFC 0017).

    The attestation is the record cw writes when a review exits clean: it pins
    the sign-off to one exact commit. ``check`` is the only subcommand. It tells
    a consumer, such as a project ship gate, whether that sign-off still covers
    HEAD, so a consumer never compares SHAs itself.
    """


def _live_client(name: str | None) -> ClientConfig | None:
    """The live client config, or None when unnamed, unknown or unreadable."""
    if name is None:
        return None
    try:
        return get_client(name)
    except (CwError, OSError, ValueError):
        return None


def _renumber_patterns(client: ClientConfig | None) -> tuple[RenumberPattern, ...]:
    """The default renumber rules plus the live client's own."""
    if client is None or client.review.attestation is None:
        return DEFAULT_RENUMBER_PATTERNS
    return (*DEFAULT_RENUMBER_PATTERNS, *client.review.attestation.renumber_patterns)


def _schema_ok(context: dict[str, object]) -> bool:
    schema = context.get("schema_version")
    return (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema >= MIN_ATTESTATION_CONTEXT_SCHEMA
    )


def _verdict(
    root: Path,
    context_path: Path | None,
    ticket: str | None,
    client_name: str | None,
) -> AttestationVerdict:
    ctx_path = context_path or root / _CONTEXT_RELATIVE_PATH
    context = read_context(ctx_path)
    if context is None:
        detail = f"{ctx_path} is missing, unreadable or not valid JSON"
        return AttestationVerdict(INVALID_UNREADABLE, detail)
    if not _schema_ok(context):
        detail = (
            "cw-context schema_version is below "
            f"{MIN_ATTESTATION_CONTEXT_SCHEMA} or malformed"
        )
        return AttestationVerdict(INVALID_SCHEMA_VERSION, detail)
    metadata = context.get("queue_metadata")
    raw = metadata.get(REVIEW_ATTESTATION_KEY) if isinstance(metadata, dict) else None
    expected_client = client_name or _string_field(context, CLIENT_CONTEXT_KEY)
    live = _live_client(expected_client)

    def merge_tolerance_for(sha: str) -> MergeTolerance | None:
        return review_move_tolerance(live, root, reviewed_sha=sha)

    return evaluate_review_attestation(
        raw,
        expected_ticket=ticket or _string_field(context, "ticket_id"),
        expected_client=expected_client,
        repo=root,
        head_sha=resolve_commit(root, "HEAD").sha,
        merge_tolerance_for=merge_tolerance_for,
        renumber_patterns=_renumber_patterns(live),
    )


@attestation.command(name="check")
@click.option("--ticket", help=_TICKET_HELP)
@click.option("--client", "client_name", help=_CLIENT_HELP)
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
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@handle_errors
def check(
    ticket: str | None,
    client_name: str | None,
    worktree: Path,
    context_path: Path | None,
    as_json: bool,
) -> None:
    """Print whether the review sign-off attestation still covers HEAD (RFC 0017).

    Reads ``queue_metadata.review_attestation`` from the worker's cw-context,
    cross-checks its ticket and client, and compares its head with HEAD through
    git. Prints exactly one line on stdout, ``valid`` or ``invalid:<reason>``
    (reason: missing, unreadable, schema_version, malformed, wrong_ticket,
    wrong_client, verdict_not_pass, head_unreadable, not_ancestor,
    span_too_large or untolerated_commit), with a detail line on stderr for an
    invalid verdict. Exits 0 when valid, 1 when invalid and 2 on a usage error;
    it fails closed, so any doubt is invalid.

    HEAD equal to the attested head is valid. Later commits keep it valid only
    when every one is a clean merge from the default branch, a CHANGELOG-only
    commit, a pure rename or a migration renumber. A merge is tolerated only
    for a clean-mode client with ``review.default_merge_tolerance`` on whose
    convergence ledger still names the attested head, read from the live client
    config and never from the context; a blocking-mode client tolerates no
    merge. Never writes a ref, the index or a worktree file; it may add
    unreachable git tree objects.
    """
    root = _guard_root(worktree.resolve())
    verdict = _verdict(root, context_path, ticket, client_name)
    if as_json:
        payload: dict[str, object] = {
            "verdict": verdict.token,
            "valid": verdict.valid,
        }
        payload.update(
            {
                field: getattr(verdict, field)
                for field in AttestationVerdict._fields
                if field != "token"
            }
        )
        click.echo(json.dumps(payload))
    else:
        click.echo(verdict.token)
        if not verdict.valid:
            click.echo(verdict.detail, err=True)
    if not verdict.valid:
        raise SystemExit(_EXIT_INVALID)
