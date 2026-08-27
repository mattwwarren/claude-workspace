"""The ``cw review consolidate`` command and its document-loading helpers.

``cw review consolidate <path>`` validates, dedupes, and aggregates a batch of
reviewer findings documents (the #1237 structured finding contract) into a
single :class:`~cw.review_findings.ReviewVerdict`. This is the Claude-native
adoption of the same wrapping the Codex adapter already performs in
``cw.codex_review`` (#1236) — the CLI is the machine-extraction boundary the
``/auto-dev-review`` command's coordinating session calls after each reviewer
subagent's ``REVIEW_FINDINGS`` block is extracted from its prose response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field, ValidationError, model_validator

from cw.cli._base import handle_errors
from cw.cli.review._diff_integrity import (
    _NO_BASE_CHECK_HELP,
    _check_no_duplicate_hunks,
    _check_not_placeholder_diff,
    _require_base_xor_no_base_check,
    _run_base_check_if_requested,
)
from cw.cli.review._group import (
    _build_captured_diff,
    _parse_payload_or_exit,
    review,
)
from cw.exceptions import DocumentsFromReadError
from cw.review_findings import (
    RejectedFinding,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    _rescue_findings,
    consolidate_verdict,
    parse_reviewer_document,
)


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

    ``documents_pre_validation_rejected`` (#2042) closes the gap #2029 left
    open: rescuing a schema-invalid ``findings[]`` item without losing its
    siblings previously worked only for ``--documents-from``, because that
    path calls :func:`~cw.review_findings.parse_reviewer_document` per file
    before Pydantic ever sees the payload. The inline ``documents`` array had
    no such boundary — it was validated by this class's own
    ``list[ReviewerFindingsDocument]`` field, which is all-or-nothing the same
    way. ``_rescue_inline_documents`` (a ``model_validator(mode="before")``)
    now runs :func:`~cw.review_findings._rescue_findings` per raw document
    before that field validation happens, substituting each document's
    reduced (survivors-only) payload back in and stashing casualties here.
    Pydantic's own native ``list[ReviewerFindingsDocument]`` validation then
    does final construction on the reduced payload, so a residual structural
    failure (e.g. every finding rescued away, leaving the document unable to
    satisfy its own invariants) keeps its correct ``documents.<i>:`` location
    for free. The hook guards on ``data`` being a dict whose ``documents`` key
    is a list, no-oping otherwise -- see its own docstring for why.
    """

    documents: list[ReviewerFindingsDocument] = Field(default_factory=list)
    documents_pre_validation_rejected: list[RejectedFinding] = Field(
        default_factory=list
    )
    diff: str
    reviewed_sha: str
    failed_reviewers: list[ReviewerRunFailure] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _rescue_inline_documents(cls, data: Any) -> Any:
        """Pre-filter each inline document's findings before Pydantic's own
        `list[ReviewerFindingsDocument]` field validation runs (#2042).

        No-ops (returns `data` unchanged) unless `data` is a dict and its
        `documents` key is a list -- anything else falls through to Pydantic's
        own native top-level/field validation, which already raises the
        correct clean error for a malformed envelope. Mirrors
        `_rescue_findings`'s own non-dict/non-list guard one level up: this
        hook must never index/`.get()` into a shape it hasn't confirmed,
        since an unguarded AttributeError/TypeError here would NOT be caught
        by `_parse_payload_or_exit` (ValidationError only) or `handle_errors`
        (CwError only), and would regress today's clean exit-1 contract into
        an unhandled traceback.
        """
        if not isinstance(data, dict):
            return data
        raw_documents = data.get("documents")
        if not isinstance(raw_documents, list):
            return data
        reduced_documents: list[object] = []
        rejected: list[RejectedFinding] = []
        for raw_document in raw_documents:
            reduced, doc_rejected = _rescue_findings(raw_document)
            reduced_documents.append(reduced)
            rejected.extend(doc_rejected)
        return {
            **data,
            "documents": reduced_documents,
            "documents_pre_validation_rejected": rejected,
        }


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


def _load_reviewer_document(
    path: Path,
) -> tuple[ReviewerFindingsDocument, list[RejectedFinding]]:
    """Read one reviewer findings document, naming *path* on any failure.

    Returns ``(document, rejected)`` since #2029: a findings[] item that cannot
    become a :class:`Finding` is now reported as a ``"schema_invalid"``
    rejection alongside its surviving siblings, instead of destroying the whole
    document. The ``ValidationError`` wrapping below still fires — but only for
    a genuinely STRUCTURAL failure, one the per-finding rescue could not
    resolve.
    """
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
        return parse_reviewer_document(payload)
    except ValidationError as exc:
        msg = f"--documents-from rejected {path} as a reviewer document: {exc}"
        raise DocumentsFromReadError(msg) from exc


def _load_documents_from(
    source: Path,
) -> tuple[list[ReviewerFindingsDocument], list[RejectedFinding]]:
    """Every reviewer findings document *source* selects, in filename order.

    The second element aggregates every file's parse-time rejects (#2029), in
    the same filename order, for ``consolidate_verdict``'s
    ``pre_validation_rejected``.
    """
    documents: list[ReviewerFindingsDocument] = []
    rejected: list[RejectedFinding] = []
    for path in _resolve_documents_from_files(source):
        document, file_rejected = _load_reviewer_document(path)
        documents.append(document)
        rejected.extend(file_rejected)
    return documents, rejected


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
    help=_NO_BASE_CHECK_HELP,
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
    # Deliberately NOT `resolved_worktree`, which --no-tree-evidence nulls:
    # the base check has nothing to do with tree-existence relaxation and
    # must behave identically whether or not that flag is passed.
    _run_base_check_if_requested(parsed.diff, base, parsed.reviewed_sha, worktree)

    if no_tree_evidence:
        resolved_worktree = None
    else:
        resolved_worktree = worktree if worktree is not None else Path.cwd()

    # #2042: both the --documents-from path and the inline `documents` array
    # now report parse-time rejects -- the latter via
    # `_ConsolidateInput._rescue_inline_documents`, which runs ahead of this
    # class's own `list[ReviewerFindingsDocument]` field validation.
    if documents_from is not None:
        documents, pre_validation_rejected = _load_documents_from(documents_from)
    else:
        documents = parsed.documents
        pre_validation_rejected = parsed.documents_pre_validation_rejected
    diff = _build_captured_diff(parsed.diff)
    verdict = consolidate_verdict(
        documents,
        diff,
        parsed.reviewed_sha,
        worktree=resolved_worktree,
        failed_reviewers=parsed.failed_reviewers,
        pre_validation_rejected=pre_validation_rejected,
    )
    click.echo(verdict.model_dump_json(indent=2))
