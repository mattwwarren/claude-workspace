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

``cw review adjudicate <path>`` and ``cw review verify-fixes <path>`` (#1805)
are the two steps after that: the first stamps the session's own FIX / REJECT
/ DEFER decisions into the verdict (and renders the matching
``.cw/deferred-findings.md``), the second downgrades any ``"fixed"``
disposition the fix-cycle diff does not substantiate. Adjudication stays a
judgment call made by the coordinating session — these commands only make its
outcome machine-readable instead of re-typed into two places.
"""

from __future__ import annotations

from pathlib import Path

import click
from pydantic import BaseModel, Field, ValidationError

from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors, main
from cw.exceptions import CwError
from cw.review_adjudication import (
    Adjudication,
    apply_adjudication,
    render_deferred_findings_md,
    verify_fixed_dispositions,
)
from cw.review_findings import (
    CapturedDiff,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewVerdict,
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


class _AdjudicateInput(BaseModel):
    """Request envelope for ``cw review adjudicate`` (#1805).

    The verdict is the one ``cw review consolidate`` printed at Checkpoint 3a;
    the adjudications are one entry per finding the coordinating session
    bucket-sorted. Same envelope shape as :class:`_ConsolidateInput` — owned by
    this CLI module, not by the library it calls.
    """

    verdict: ReviewVerdict
    adjudications: list[Adjudication] = Field(default_factory=list)


class _VerifyFixesInput(BaseModel):
    """Request envelope for ``cw review verify-fixes`` (#1805)."""

    verdict: ReviewVerdict
    diff: str


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
    from cw.codex_review import _parse_unified_diff

    file_diffs, file_line_text, file_window_text, _changed_files = _parse_unified_diff(
        diff_text
    )
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    return CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=file_window_text,
    )


def _parse_payload_or_exit[InputT: BaseModel](path: str, model: type[InputT]) -> InputT:
    """Read PATH ('-' for stdin) and validate it against *model*, or exit 1.

    The three ``cw review`` payload commands share one failure shape —
    ``field.path: message`` lines on stderr, exit 1 — so they share the
    reading and validating too rather than letting three copies drift.
    """
    from cw.result import _format_errors, _read_json_payload

    payload = _read_json_payload(path)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        raise click.exceptions.Exit(1) from exc


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

    Your GitHub identity resolves via the same precedence
    ``resolve_operator_login_for_repo`` uses everywhere else: the PR's repo
    in ``orchestrator.yaml``'s ``operator_github_login_by_repo`` map wins when
    set, otherwise your process gh identity (RFC 0011 follow-up #1171).
    """
    from cw.config import load_orchestrator_config
    from cw.gh import fetch_pr_view
    from cw.operator_identity import cached_gh_login, resolve_operator_login_for_repo
    from cw.pr_hydrate import (
        _parse_pr_url,
        resolve_and_register_review_request,
    )

    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        msg = f"Could not parse a GitHub PR URL from: {pr_url!r}"
        raise CwError(msg)
    repo, pr_number = parsed

    config = load_orchestrator_config()
    operator_login = resolve_operator_login_for_repo(
        repo, config, fallback=cached_gh_login()
    )
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
@click.option(
    "--worktree",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Worktree root for unanchored-finding tree-existence checks "
        "(defaults to the current directory)."
    ),
)
@click.option(
    "--no-tree-evidence",
    is_flag=True,
    default=False,
    help=(
        "Disable tree-existence relaxation for unanchored findings; "
        "restores diff-anchored-only evidence even when --worktree is "
        "set or inferred from the current directory."
    ),
)
@handle_errors
def review_consolidate(
    path: str, worktree: Path | None, no_tree_evidence: bool
) -> None:
    """Validate, dedupe, and aggregate reviewer findings into a ReviewVerdict.

    PATH is a file path or '-' for stdin. Payload: {"documents": [...],
    "diff": "<raw unified diff text>", "reviewed_sha": "<sha>",
    "failed_reviewers": [...]} (failed_reviewers optional, default []).

    --worktree sets the tree root used to accept non-diff-anchored findings
    that still exist on disk (defaults to the current directory).
    --no-tree-evidence disables that relaxation entirely, restoring
    diff-anchored-only evidence regardless of --worktree or cwd.

    On success: exits 0, prints the ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _ConsolidateInput)

    if no_tree_evidence:
        resolved_worktree = None
    else:
        resolved_worktree = worktree if worktree is not None else Path.cwd()

    diff = _build_captured_diff(parsed.diff)
    verdict = consolidate_verdict(
        parsed.documents,
        diff,
        parsed.reviewed_sha,
        worktree=resolved_worktree,
        failed_reviewers=parsed.failed_reviewers,
    )
    click.echo(verdict.model_dump_json(indent=2))


@review.command(name="adjudicate")
@click.argument("path")
@click.option(
    "--deferred-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also render the rejected/deferred adjudications to this path "
        "(the .cw/deferred-findings.md artifact Stage 4 Step 4d consumes). "
        "Nothing is written when every finding was fixed."
    ),
)
@handle_errors
def review_adjudicate(path: str, deferred_findings_out: Path | None) -> None:
    """Stamp adjudication outcomes into a ReviewVerdict (#1805).

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the
    ReviewVerdict from `cw review consolidate`>, "adjudications": [{"severity":
    ..., "file": ..., "line_start": ..., "line_end": ..., "evidence": ...,
    "summary": ..., "outcome": "fix|reject|defer", "rationale": ...}]}.

    Each accepted finding is stamped from its matching adjudication entry;
    a finding no entry covers is stamped "dropped", and blocking/must_fix/
    review.deferred are recomputed from the stamped result. An entry matching
    no finding never fails the command — it is counted in the printed
    verdict's `unmatched_adjudication_count` so the approval gate can see it.

    On success: exits 0, prints the stamped ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _AdjudicateInput)
    verdict = apply_adjudication(parsed.verdict, parsed.adjudications)

    if deferred_findings_out is not None:
        rendered = render_deferred_findings_md(parsed.adjudications)
        # "" means every finding was fixed — the documented rule is to omit
        # the file entirely rather than leave an empty artifact behind.
        if rendered:
            deferred_findings_out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(deferred_findings_out, rendered)

    click.echo(verdict.model_dump_json(indent=2))


@review.command(name="verify-fixes")
@click.argument("path")
@handle_errors
def review_verify_fixes(path: str) -> None:
    """Downgrade 'fixed' dispositions the fix-cycle diff does not substantiate.

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the adjudicated
    ReviewVerdict>, "diff": "<raw unified diff text of the fix cycles>"}.

    A "fixed" finding whose cited file/line the diff never touched becomes
    "dropped", with the reason in `disposition_detail`. Record-only: no gate
    is re-evaluated and no fix cycle is triggered — the caller surfaces the
    downgrade in friction_highlights.

    On success: exits 0, prints the downgraded ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _VerifyFixesInput)
    verdict = verify_fixed_dispositions(
        parsed.verdict, _build_captured_diff(parsed.diff)
    )
    click.echo(verdict.model_dump_json(indent=2))
