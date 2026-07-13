"""The ``cw review`` CLI group (GitHub #1154, RFC 0011 S2).

``cw review register <pr-url>`` records a PR you were asked to review as a
watched PR (``DevQueueStore.watched_prs``). No ``list``/``remove`` subcommand
exists this slice (R11) — operators inspect ``dev_queue.json`` directly until a
later slice adds them.
"""

from __future__ import annotations

import click

from cw.cli._base import handle_errors, main
from cw.exceptions import CwError


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
