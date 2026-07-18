"""The ``cw review`` CLI group (GitHub #1154, RFC 0011 S2; #1241).

``cw review register <pr-url>`` records a PR you were asked to review as a
watched PR (``DevQueueStore.watched_prs``). No ``list``/``remove`` subcommand
exists this slice (R11) — operators inspect ``dev_queue.json`` directly until a
later slice adds them.

``cw review consolidate <path>`` validates, dedupes, and aggregates a batch of
reviewer findings documents (the #1237 structured finding contract) into a
single :class:`~cw.review_findings.ReviewVerdict`. This is the Claude-native
adoption of the same wrapping the Codex adapter already performs in
``cw.codex_review`` (#1236) — the CLI is the machine-extraction boundary the
``/auto-dev-review`` command's coordinating session calls after each reviewer
subagent's ``REVIEW_FINDINGS`` block is extracted from its prose response.
"""

from __future__ import annotations

import click
from pydantic import BaseModel, Field, ValidationError

from cw.cli._base import handle_errors, main
from cw.exceptions import CwError
from cw.review_findings import (
    CapturedDiff,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    consolidate_verdict,
)


class _ConsolidateInput(BaseModel):
    """Request envelope for ``cw review consolidate`` (#1241).

    Bundles the already-typed #1237 documents with the raw diff text and
    ``reviewed_sha`` needed to build the :class:`CapturedDiff` that
    :func:`~cw.review_findings.consolidate_verdict` validates evidence
    against. Owned entirely by this CLI module — ``review_findings.py`` is
    consumed, not authored, by this ticket's scope (see plan Patterns Found).
    """

    documents: list[ReviewerFindingsDocument]
    diff: str
    reviewed_sha: str
    failed_reviewers: list[ReviewerRunFailure] = Field(default_factory=list)


def _build_captured_diff(diff_text: str) -> CapturedDiff:
    """Parse raw unified diff text into a :class:`CapturedDiff`.

    Reuses :func:`cw.codex_review._parse_unified_diff` (function-local import
    — that parser and this command's envelope both live in modules outside
    this ticket's touch-point contract; the codex module owns the parser and
    is not modified here) rather than duplicating the ~60-line unified-diff
    parser. Mirrors ``codex_review._capture_diff``'s post-subprocess body
    exactly: ``files`` is derived from ``file_line_text`` so it can never
    drift from the per-line content.
    """
    from cw.codex_review import _parse_unified_diff  # noqa: PLC0415

    file_diffs, file_line_text, _changed_files = _parse_unified_diff(diff_text)
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    return CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
    )


@main.group(name="review")
def review() -> None:
    """Operator review-request tracking (RFC 0011 S2)."""


@review.command(name="register")
@click.argument("pr_url")
@handle_errors
def review_register(pr_url: str) -> None:
    """Register a PR you were asked to review as a watched PR.

    Parses the GitHub PR URL, resolves your gh identity, reads the PR's live
    ``reviewRequests``, and records a watched PR when you are individually (not
    team-) requested. Prints the outcome reason and exits 0 for a non-error
    "not registered" case (team-targeted, not-you, already-registered); exits
    non-zero only when your identity cannot be resolved, the URL is
    unparseable, or the PR cannot be fetched.

    Limitation: this uses your process gh identity directly; the
    ``clients.yaml`` ``operator_github_login`` override is NOT honored here
    because a PR-scoped entry point has no client context (follow-up #1171).
    """
    from cw.gh import fetch_pr_view  # noqa: PLC0415
    from cw.operator_identity import cached_gh_login  # noqa: PLC0415
    from cw.pr_hydrate import (  # noqa: PLC0415
        _parse_pr_url,
        resolve_and_register_review_request,
    )

    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        msg = f"Could not parse a GitHub PR URL from: {pr_url!r}"
        raise CwError(msg)
    repo, pr_number = parsed

    # Why: bypass resolve_operator_login's operator_github_login override — no
    # client context exists at this PR-scoped entry point, so there is no
    # ClientConfig to consult. Honoring the override here is blocked on the
    # repo->client mapping and lands via follow-up #1171.
    operator_login = cached_gh_login()
    if operator_login is None:
        msg = (
            "Could not resolve your GitHub identity (gh api user failed)."
            " Ensure gh is installed and authenticated (gh auth status)."
        )
        raise CwError(msg)

    data = fetch_pr_view(pr_url)
    if data is None:
        msg = f"Could not fetch PR view for {pr_url} (gh pr view failed)."
        raise CwError(msg)
    review_requests = data.get("reviewRequests")
    reviewer_nodes = review_requests if isinstance(review_requests, list) else []

    registered, reason = resolve_and_register_review_request(
        repo=repo,
        pr_number=pr_number,
        pr_url=pr_url,
        reviewer_nodes=reviewer_nodes,
        operator_login=operator_login,
        source="cli",
        requester_login=None,
    )
    if registered:
        click.echo(f"Registered watched PR {repo}#{pr_number}.")
    else:
        click.echo(f"Not registered ({reason}).")


@review.command(name="consolidate")
@click.argument("path")
@handle_errors
def review_consolidate(path: str) -> None:
    """Validate, dedupe, and aggregate reviewer findings into a ReviewVerdict.

    PATH is a file path or '-' for stdin. Payload: {"documents": [...],
    "diff": "<raw unified diff text>", "reviewed_sha": "<sha>",
    "failed_reviewers": [...]} (failed_reviewers optional, default []).

    On success: exits 0, prints the ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    from cw.result import _format_errors, _read_json_payload  # noqa: PLC0415

    payload = _read_json_payload(path)
    try:
        parsed = _ConsolidateInput.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        raise click.exceptions.Exit(1) from exc

    diff = _build_captured_diff(parsed.diff)
    verdict = consolidate_verdict(
        parsed.documents,
        diff,
        parsed.reviewed_sha,
        failed_reviewers=parsed.failed_reviewers,
    )
    click.echo(verdict.model_dump_json(indent=2))
