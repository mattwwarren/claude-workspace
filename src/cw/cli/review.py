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

``cw review check-voided <path>`` (#1814) runs between consolidate and
adjudicate: it suppresses findings a prior pass's operator decision already
settled, and renders the durable record of those decisions back out for
posting to the ticket. It is the Claude-native half of a mechanism the codex
backend reaches through ``cw.codex_review`` instead — same library function,
same outcome, no coordinating session required on that side.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click
from pydantic import BaseModel, Field, ValidationError

from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors, main
from cw.exceptions import (
    CwError,
    DiffBaseMismatchError,
    DocumentsFromReadError,
    DuplicatedHunkError,
    PlaceholderDiffError,
)
from cw.review_adjudication import (
    REJECTED_ENTRY_SEVERITY,
    Adjudication,
    VoidedFinding,
    apply_adjudication,
    apply_voided_suppression,
    matched_adjudications,
    merge_deferred_adjudications,
    parse_deferred_findings_md,
    parse_voided_findings_block,
    render_deferred_findings_md,
    render_voided_findings_block,
    verify_fixed_dispositions,
)
from cw.review_findings import (
    CapturedDiff,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewVerdict,
    consolidate_verdict,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# #1924 placeholder-diff detection. Whole-value-only tokens: a payload whose
# `diff` strips down to exactly one of these never carried a diff at all.
# Deliberately narrow, mirroring `_is_placeholder_sentinel_text`'s discipline
# in `cw.auto_dev_result.parse` — do NOT broaden to "looks templated", since
# silently rejecting a genuine diff is a strictly worse bug than the one this
# catches.
_PLACEHOLDER_DIFF_TOKENS = frozenset({"<diff here>", "<insert diff>", "..."})

# Below this many stripped characters, text with no `diff --git` header at all
# is a stub rather than a diff. The floor alone is never sufficient: a real but
# heavily-truncated diff can be shorter than this, which is why the check
# requires the CONJUNCTION of "under the floor" and "no header".
_PLACEHOLDER_LENGTH_FLOOR = 40

# This module's own header matchers, deliberately independent of
# `cw.codex_review._diff`'s: that module owns a full unified-diff parser whose
# per-file buffers are never hunk-separated (it never resets on a bare `@@`
# line), so it cannot answer "did this hunk appear twice" and these guards must
# scan the text themselves.
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)
_DIFF_GIT_PATHS_RE = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")


def _check_not_placeholder_diff(diff_text: str) -> None:
    """Reject a ``diff`` field that never carried a real diff (#1924).

    Two independent triggers: an exact (case-sensitive) match against
    :data:`_PLACEHOLDER_DIFF_TOKENS` at any length, or the conjunction of
    "shorter than :data:`_PLACEHOLDER_LENGTH_FLOOR`" and "carries no
    ``diff --git`` header". A real diff containing ``...`` somewhere in a body
    line is untouched — the token match is whole-value-only.
    """
    stripped = diff_text.strip()
    if stripped in _PLACEHOLDER_DIFF_TOKENS:
        msg = (
            f"The payload's diff is the unresolved placeholder {stripped!r}, not "
            "a real unified diff. Capture the diff with `git diff` and pass its "
            "verbatim output."
        )
        raise PlaceholderDiffError(msg)
    if (
        len(stripped) < _PLACEHOLDER_LENGTH_FLOOR
        and _DIFF_GIT_HEADER_RE.search(diff_text) is None
    ):
        msg = (
            f"The payload's diff is {len(stripped)} characters and carries no "
            "`diff --git` header, so it cannot be a real unified diff. Capture "
            "the diff with `git diff` and pass its verbatim output."
        )
        raise PlaceholderDiffError(msg)


def _diff_git_path(header_line: str) -> str:
    """The b-side path named by a ``diff --git`` header, or the line itself."""
    match = _DIFF_GIT_PATHS_RE.match(header_line)
    return match.group("b") if match else header_line


def _iter_file_sections(diff_text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(path, body_lines)`` per ``diff --git`` section of *diff_text*."""
    current_path: str | None = None
    body: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current_path is not None:
                yield current_path, body
            current_path = _diff_git_path(raw)
            body = []
            continue
        if current_path is not None:
            body.append(raw)
    if current_path is not None:
        yield current_path, body


def _iter_hunks(body: list[str]) -> Iterator[str]:
    """Yield each ``@@``-headed hunk of one file section as a single string."""
    current: list[str] | None = None
    for raw in body:
        if raw.startswith("@@"):
            if current is not None:
                yield "\n".join(current)
            current = [raw]
            continue
        if current is not None:
            current.append(raw)
    if current is not None:
        yield "\n".join(current)


def _check_no_duplicate_hunks(diff_text: str) -> None:
    """Reject a diff repeating the same hunk for the same file (#1924).

    The duplicate key is ``(file path, hunk header + body)``, so the same hunk
    text under two different files — the same one-line change applied to two
    modules — is legitimate and passes. ``seen`` spans the whole document, not
    one section, so a diff concatenated with itself is caught even though each
    copy is internally consistent.
    """
    seen: set[tuple[str, str]] = set()
    for path, body in _iter_file_sections(diff_text):
        for hunk in _iter_hunks(body):
            key = (path, hunk)
            if key in seen:
                header = hunk.splitlines()[0]
                msg = (
                    f"The payload's diff repeats the same hunk for {path}: "
                    f"{header!r} appears more than once with identical content. "
                    "A diff reconstructed by hand is not evidence — re-capture "
                    "it with `git diff`."
                )
                raise DuplicatedHunkError(msg)
            seen.add(key)


def _check_diff_matches_base(
    diff_text: str, base: str, reviewed_sha: str, worktree: Path
) -> None:
    """Reject a payload whose diff is not ``git diff <base>...<sha>`` (#1924).

    Exact string equality after trimming a single trailing newline from each
    side — deliberately not a semantic diff comparison. The point is to prove
    the payload text came out of git verbatim; anything that "means the same
    thing" but does not match byte-for-byte was retyped. Called from both
    ``review_consolidate`` (#1924) and ``review_verify_fixes`` (#1988).
    """
    completed = subprocess.run(
        ["git", "diff", "--no-color", f"{base}...{reviewed_sha}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited {completed.returncode}"
        msg = (
            f"Could not compute `git diff {base}...{reviewed_sha}` in {worktree}: "
            f"{detail}"
        )
        raise DiffBaseMismatchError(msg)
    if diff_text.removesuffix("\n") != completed.stdout.removesuffix("\n"):
        msg = (
            f"The payload's diff does not match `git diff {base}...{reviewed_sha}` "
            f"in {worktree} (payload {len(diff_text)} chars, git "
            f"{len(completed.stdout)} chars). Pass the verbatim git output."
        )
        raise DiffBaseMismatchError(msg)


def _require_base_xor_no_base_check(base: str | None, no_base_check: bool) -> None:
    """Enforce --base/--no-base-check as a required alternatives pair (#1988).

    Why not click's own `required=True` (the dev-queue-prune precedent,
    src/cw/cli/dev_queue/crud.py): --base and --no-base-check are
    alternatives, so requiring either outright would forbid the other.
    This reproduces the same guarantee -- you cannot silently omit
    diff-integrity verification -- as a UsageError raised before any
    payload parsing runs. Shared by ``review_consolidate`` and
    ``review_verify_fixes`` so the two commands cannot silently diverge.
    """
    if base is None and not no_base_check:
        msg = "Must pass either --base <ref> or --no-base-check."
        raise click.UsageError(msg)
    if base is not None and no_base_check:
        msg = "--base and --no-base-check are mutually exclusive."
        raise click.UsageError(msg)


def _resolve_documents_from_files(source: Path) -> list[Path]:
    """The files ``--documents-from`` *source* selects, in filename order.

    A path that exists and is a directory is read as ``<source>/*.json``
    (non-recursive); anything else is evaluated as a glob pattern against
    ``source.parent``. Zero matches is a valid outcome in either branch — a
    round in which every reviewer failed writes no documents at all. A source
    whose parent directory does not exist is not, since nothing could ever
    match it.
    """
    if source.exists() and source.is_dir():
        return sorted(source.glob("*.json"))
    if not source.parent.is_dir():
        msg = (
            f"--documents-from path {source} cannot be read: its parent "
            f"directory {source.parent} does not exist."
        )
        raise DocumentsFromReadError(msg)
    return sorted(source.parent.glob(source.name))


def _load_reviewer_document(path: Path) -> ReviewerFindingsDocument:
    """Read one reviewer findings document, naming *path* on any failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"--documents-from could not read {path}: {exc}"
        raise DocumentsFromReadError(msg) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--documents-from could not parse {path} as JSON: {exc}"
        raise DocumentsFromReadError(msg) from exc
    try:
        return ReviewerFindingsDocument.model_validate(payload)
    except ValidationError as exc:
        msg = f"--documents-from rejected {path} as a reviewer document: {exc}"
        raise DocumentsFromReadError(msg) from exc


def _load_documents_from(source: Path) -> list[ReviewerFindingsDocument]:
    """Every reviewer findings document *source* selects, in filename order."""
    return [_load_reviewer_document(p) for p in _resolve_documents_from_files(source)]


class _ConsolidateInput(BaseModel):
    """Request envelope for ``cw review consolidate`` (#1241).

    Bundles the already-typed #1237 documents with the raw diff text and
    ``reviewed_sha`` needed to build the :class:`CapturedDiff` that
    :func:`~cw.review_findings.consolidate_verdict` validates evidence
    against. Owned entirely by this CLI module — ``review_findings.py`` is
    consumed, not authored, by this ticket's scope (see plan Patterns Found).

    ``documents`` defaults to ``[]`` since #1924: the preferred producer path
    is ``--documents-from``, which reads each reviewer's findings document off
    disk verbatim rather than having the coordinating session retype them into
    an inline array. When that option is set, any ``documents`` still present
    on this envelope is ignored.
    """

    documents: list[ReviewerFindingsDocument] = Field(default_factory=list)
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
    """Request envelope for ``cw review verify-fixes`` (#1805).

    ``reviewed_sha`` (#1988) is the fix-cycle branch tip the payload's
    ``diff`` was captured against — only consumed when ``--base`` is passed,
    as the second argument to the ``git diff <base>...<reviewed_sha>``
    verification. It is independent of ``verdict.reviewed_sha`` (the
    Checkpoint-3a sha the verdict was frozen at); the command never
    cross-checks the two.
    """

    verdict: ReviewVerdict
    diff: str
    reviewed_sha: str


class _CheckVoidedInput(BaseModel):
    """Request envelope for ``cw review check-voided`` (#1814).

    ``comment_bodies`` are the live-fetched ticket comments the coordinating
    session already holds (mandatory per #1730's "comments are live, not
    cached" rule) — every prior pass's voided-findings sentinel is parsed back
    out of them. ``new_voided_entries`` are the ones this pass just settled at
    Checkpoint 3a step 4c.

    ``ticket_id`` is required because it is this path's ``correlation_id``
    source: the codex path reads it off its ``TicketTask``, and there is no
    equivalent object here, so it has to come in on the payload rather than
    leaving the mandatory suppression event uncorrelated.
    """

    verdict: ReviewVerdict
    ticket_id: str
    comment_bodies: list[str] = Field(default_factory=list)
    new_voided_entries: list[VoidedFinding] = Field(default_factory=list)


class _CheckVoidedOutput(BaseModel):
    """Response envelope for ``cw review check-voided`` (#1814).

    Two values, not one: the suppressed verdict continues Checkpoint 3a, and
    the adjudications are appended verbatim to the session's ``ADJUDICATIONS``
    array so the later ``cw review adjudicate`` pass re-stamps the same outcome
    from the same single source of truth.
    """

    verdict: ReviewVerdict
    adjudications: list[Adjudication]


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
@click.option(
    "--documents-from",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Read reviewer findings documents from this directory or "
        "glob pattern instead of the PATH payload's documents field "
        "(any documents there are ignored once this is set). A path "
        "that exists and is a directory is read as <path>/*.json, "
        "sorted lexicographically by filename; anything else is "
        "evaluated as a glob pattern directly. Defaults to None, "
        "which keeps documents sourced from the payload."
    ),
)
@click.option(
    "--base",
    default=None,
    type=str,
    help=(
        "Compare the payload's diff text against the real `git diff "
        "<base>...<reviewed_sha>` output and reject the payload if "
        "they differ, guarding against a hand-typed or corrupted "
        "diff. Resolves the repo root from --worktree, falling back "
        "to the current directory. Mutually exclusive with "
        "--no-base-check; exactly one of the two must be given."
    ),
)
@click.option(
    "--no-base-check",
    is_flag=True,
    default=False,
    help=(
        "Skip --base verification entirely: the payload's diff will "
        "NOT be checked against real git history, and findings may "
        "be adjudicated against an artifact nobody verified. For "
        "non-git-backed synthetic payloads (tests) and human "
        "post-hoc recovery debugging only — never for pipeline use, "
        "which always passes --base. Mutually exclusive with "
        "--base; exactly one of the two must be given."
    ),
)
@handle_errors
def review_consolidate(
    path: str,
    worktree: Path | None,
    no_tree_evidence: bool,
    documents_from: Path | None,
    base: str | None,
    no_base_check: bool,
) -> None:
    """Validate, dedupe, and aggregate reviewer findings into a ReviewVerdict.

    PATH is a file path or '-' for stdin. Payload: {"documents": [...],
    "diff": "<raw unified diff text>", "reviewed_sha": "<sha>",
    "failed_reviewers": [...]} (documents and failed_reviewers optional,
    default []).

    --worktree sets the tree root used to accept non-diff-anchored findings
    that still exist on disk (defaults to the current directory).
    --no-tree-evidence disables that relaxation entirely, restoring
    diff-anchored-only evidence regardless of --worktree or cwd.

    --documents-from reads each reviewer's findings document off disk instead
    of from the payload, so a coordinating session Writes them verbatim rather
    than retyping them into an inline array (the paraphrase risk #1924
    closes). It takes a directory (read as <dir>/*.json) or a glob pattern;
    matches are consolidated in lexicographic filename order, and the
    payload's own documents field is ignored entirely when it is set.

    --base verifies the payload's diff text is byte-identical to the real
    `git diff <base>...<reviewed_sha>` output, resolved from --worktree (or
    the current directory), and rejects the payload otherwise. It is
    independent of --no-tree-evidence: the check runs identically either way.
    Exactly one of --base/--no-base-check must be given; --no-base-check
    skips this check entirely and is for tests and human recovery debugging
    only, never for pipeline use.

    The payload's diff is always screened for two integrity defects first,
    with or without --base: a placeholder that never carried a diff, and the
    same hunk repeated for the same file.

    On success: exits 0, prints the ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr — or,
    for an integrity-guard rejection, a plain error message; exits 2 if
    neither or both of --base/--no-base-check are given.
    """
    _require_base_xor_no_base_check(base, no_base_check)

    parsed = _parse_payload_or_exit(path, _ConsolidateInput)

    _check_not_placeholder_diff(parsed.diff)
    _check_no_duplicate_hunks(parsed.diff)
    if base is not None:
        # Deliberately NOT `resolved_worktree`, which --no-tree-evidence nulls:
        # the base check has nothing to do with tree-existence relaxation and
        # must behave identically whether or not that flag is passed.
        base_check_worktree = worktree if worktree is not None else Path.cwd()
        _check_diff_matches_base(
            parsed.diff, base, parsed.reviewed_sha, base_check_worktree
        )

    if no_tree_evidence:
        resolved_worktree = None
    else:
        resolved_worktree = worktree if worktree is not None else Path.cwd()

    documents = (
        _load_documents_from(documents_from)
        if documents_from is not None
        else parsed.documents
    )
    diff = _build_captured_diff(parsed.diff)
    verdict = consolidate_verdict(
        documents,
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
        "(the .cw/deferred-findings.md artifact Stage 4 Step 4d "
        "consumes), merging them with any prior content already at "
        "this path rather than overwriting it. Each newly-applied "
        "entry is stamped with a round number and a recorded_at "
        "timestamp; a pre-#1840 legacy-shaped file (no round/date "
        "stamps) is read and merged without error. Nothing is "
        "written when there is nothing to record — every finding "
        "was fixed and no prior content exists to preserve."
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
    verdict's `unmatched_adjudication_count` so the approval gate can see it,
    and is excluded from the rendered `--deferred-findings-out` artifact (an
    entry nobody's disposition reflects must not appear there as if it did).

    When --deferred-findings-out is given, any prior content already at
    that path is read back and merged with this round's rejected/deferred
    entries rather than overwritten. Entries dedupe by content fingerprint
    (severity, file, line_start, line_end, evidence, summary, outcome,
    rationale) — excluding `round`/`recorded_at` — so an identical
    re-adjudication collapses to one entry while a genuine outcome flip
    (e.g. REJECT then later DEFER for the same finding) accumulates as
    two. Only entries newly applied by this call are stamped with a
    `round` number and a `recorded_at` timestamp; entries already present
    in the prior file — including ones written before this stamping
    existed — are carried through unchanged. Content matching neither the
    current nor the pre-#1840 shape is a hard failure (CwError); an
    absent or empty prior file is simply "nothing to merge".

    On success: exits 0, prints the stamped ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr —
    except a malformed --deferred-findings-out prior file, which exits 1
    with a plain CwError message instead of the field.path format.
    """
    parsed = _parse_payload_or_exit(path, _AdjudicateInput)
    verdict = apply_adjudication(parsed.verdict, parsed.adjudications)

    if deferred_findings_out is not None:
        applied = matched_adjudications(parsed.verdict.accepted, parsed.adjudications)
        _write_deferred_findings(deferred_findings_out, applied)

    click.echo(verdict.model_dump_json(indent=2))


def _read_prior_deferred(path: Path) -> list[Adjudication]:
    """The entries already recorded at *path*, or ``[]`` when there are none.

    A parse failure is fatal rather than a silent restart from empty: the
    alternative is overwriting durable prior records with this round's alone,
    which is #1840's own bug wearing a different hat.
    """
    if not path.exists():
        return []
    try:
        return parse_deferred_findings_md(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        msg = (
            f"Could not parse the existing --deferred-findings-out file at "
            f"{path}: {exc}. Refusing to overwrite it — inspect or remove it "
            "by hand, then re-run."
        )
        raise CwError(msg) from exc


def _artifact_entry(entry: Adjudication, next_round: int, now: str) -> Adjudication:
    """*entry* stamped with this round's context, in the artifact's field space.

    Two things happen here, both required for the merge to behave:

    - **Stamping.** ``round``/``recorded_at`` are filled in exactly the way
      :func:`_stamp_voided_at` fills ``voided_at``: the coordinating session
      supplies the judgment, the CLI supplies the clock, and a value already
      present is never overwritten.
    - **Projection.** ``line_start``/``line_end``/``evidence`` (and, for a
      rejected entry, ``severity``) are reduced to what the rendered artifact
      actually records. The artifact has never carried them, so an entry read
      back from a prior round cannot have them either; leaving them on this
      round's entries would make an identical re-adjudication miss its own
      prior record and duplicate it. Nothing observable is lost — every
      dropped field is one :func:`render_deferred_findings_md` never writes.
    """
    update: dict[str, object] = {
        "line_start": None,
        "line_end": None,
        "evidence": "",
    }
    if entry.outcome == "reject":
        update["severity"] = REJECTED_ENTRY_SEVERITY
    if entry.round is None:
        update["round"] = next_round
    if not entry.recorded_at.strip():
        update["recorded_at"] = now
    return entry.model_copy(update=update)


def _write_deferred_findings(path: Path, applied: list[Adjudication]) -> None:
    """Merge *applied* into the artifact at *path* and re-render it (#1840)."""
    prior = _read_prior_deferred(path)
    next_round = max((e.round for e in prior if e.round is not None), default=0) + 1
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamped = [_artifact_entry(entry, next_round, now) for entry in applied]
    rendered = render_deferred_findings_md(merge_deferred_adjudications(prior, stamped))
    # "" means there is nothing to record at all — every finding was fixed and
    # no prior content exists to preserve. The documented rule is to omit the
    # file entirely rather than leave an empty artifact behind.
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)


def _stamp_voided_at(entry: VoidedFinding) -> VoidedFinding:
    """Fill a blank ``voided_at`` with now, leaving a supplied one alone.

    The coordinating session supplies the judgment; the CLI supplies the
    clock. Re-stamping an entry that already carries a date would rewrite
    history on every idempotent re-post.
    """
    if entry.voided_at.strip():
        return entry
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return entry.model_copy(update={"voided_at": now})


@review.command(name="check-voided")
@click.argument("path")
@click.option(
    "--voided-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also render the merged voided-findings record to this path, as a "
        "postable '## Voided Review Findings' ticket comment. Nothing is "
        "written when there is no void to record."
    ),
)
@handle_errors
def review_check_voided(path: str, voided_findings_out: Path | None) -> None:
    """Suppress findings an operator already voided on a prior pass (#1814).

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the
    ReviewVerdict from `cw review consolidate`>, "ticket_id": "<id>",
    "comment_bodies": ["<live-fetched ticket comment>", ...],
    "new_voided_entries": [{"severity": ..., "file": ..., "summary": ...,
    "evidence": ..., "operator_comment_id": ..., "operator_comment_excerpt":
    ..., "original_rationale": ...}]}.

    A finding is suppressed only when its content anchor — severity, file,
    summary, and evidence — matches a recorded void exactly. File and line
    position are deliberately NOT the identity: a voided finding whose code
    moved still matches, and a genuinely new finding at the voided one's old
    line never does.

    Each suppression stamps `disposition="rejected"`, drops the finding from
    `must_fix`/`blocking`, and emits one `review.finding_voided` event
    correlated to `ticket_id`.

    On success: exits 0, prints {"verdict": ..., "adjudications": [...]} to
    stdout. Append the adjudications verbatim to your ADJUDICATIONS array.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _CheckVoidedInput)
    merged = [
        *parse_voided_findings_block(parsed.comment_bodies),
        *(_stamp_voided_at(entry) for entry in parsed.new_voided_entries),
    ]
    verdict, adjudications = apply_voided_suppression(
        parsed.verdict, merged, ticket_id=parsed.ticket_id
    )

    if voided_findings_out is not None:
        rendered = render_voided_findings_block(merged)
        # "" means there is nothing to record — omit the artifact entirely
        # rather than leave an empty one behind, same rule as
        # --deferred-findings-out.
        if rendered:
            voided_findings_out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(voided_findings_out, rendered)

    output = _CheckVoidedOutput(verdict=verdict, adjudications=adjudications)
    click.echo(output.model_dump_json(indent=2))


@review.command(name="verify-fixes")
@click.argument("path")
@click.option(
    "--worktree",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Worktree root used to run the --base git-diff verification "
        "(defaults to the current directory)."
    ),
)
@click.option(
    "--base",
    default=None,
    type=str,
    help=(
        "Compare the payload's diff text against the real `git diff "
        "<base>...<reviewed_sha>` output and reject the payload if "
        "they differ, guarding against a hand-typed or corrupted "
        "diff. `base` is the sha the reviewed verdict was frozen at "
        "(e.g. the Checkpoint-3a tip); `reviewed_sha` is the "
        "payload's own fix-cycle branch tip. Resolves the repo root "
        "from --worktree, falling back to the current directory. "
        "Mutually exclusive with --no-base-check; exactly one of "
        "the two must be given."
    ),
)
@click.option(
    "--no-base-check",
    is_flag=True,
    default=False,
    help=(
        "Skip --base verification entirely: the payload's diff will "
        "NOT be checked against real git history, and findings may "
        "be adjudicated against an artifact nobody verified. For "
        "non-git-backed synthetic payloads (tests) and human "
        "post-hoc recovery debugging only — never for pipeline use, "
        "which always passes --base. Mutually exclusive with "
        "--base; exactly one of the two must be given."
    ),
)
@handle_errors
def review_verify_fixes(
    path: str,
    worktree: Path | None,
    base: str | None,
    no_base_check: bool,
) -> None:
    """Downgrade 'fixed' dispositions the fix-cycle diff does not substantiate.

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the adjudicated
    ReviewVerdict>, "diff": "<raw unified diff text of the fix cycles>",
    "reviewed_sha": "<fix-cycle branch tip>"}.

    A "fixed" finding whose cited file/line the diff never touched becomes
    "dropped", with the reason in `disposition_detail`. Record-only: no gate
    is re-evaluated and no fix cycle is triggered — the caller surfaces the
    downgrade in friction_highlights.

    --base verifies the payload's diff text is byte-identical to the real
    `git diff <base>...<reviewed_sha>` output, resolved from --worktree (or
    the current directory), and rejects the payload otherwise. Exactly one
    of --base/--no-base-check must be given; --no-base-check skips this
    check entirely and is for tests and human recovery debugging only,
    never for pipeline use.

    On success: exits 0, prints the downgraded ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr; exits
    2 if neither or both of --base/--no-base-check are given.
    """
    _require_base_xor_no_base_check(base, no_base_check)

    parsed = _parse_payload_or_exit(path, _VerifyFixesInput)
    if base is not None:
        base_check_worktree = worktree if worktree is not None else Path.cwd()
        _check_diff_matches_base(
            parsed.diff, base, parsed.reviewed_sha, base_check_worktree
        )
    verdict = verify_fixed_dispositions(
        parsed.verdict, _build_captured_diff(parsed.diff)
    )
    click.echo(verdict.model_dump_json(indent=2))
