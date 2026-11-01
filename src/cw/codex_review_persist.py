"""Durable artifacts and the tracker post for one review verdict (#2807 split).

Moved verbatim out of :mod:`cw.codex_background` (which kept growing past the
module-size ceiling) and still called only from its Step 4b: the worktree
copies of a review verdict (``.claude/review-verdict.md``, the structured
``.claude/review-verdict.json`` and the zero-documents
``.claude/review-verdict-unparseable.md``) plus the best-effort tracker comment
that posts the rendered verdict. Every write here is best-effort and never
raises; see :func:`_persist_worktree_artifact`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.codex_review import CODEX_REVIEWER_LABEL
from cw.gh import post_issue_comment
from cw.review_findings import (
    REVIEW_VERDICT_JSON_RELATIVE_PATH,
    render_review_verdict_envelope,
)
from cw.tracker import TRACKER_GITHUB_ISSUES

if TYPE_CHECKING:
    from cw.auto_dev_result import Blocker
    from cw.review_findings import ReviewVerdict

_log = logging.getLogger(__name__)


# Durable copy of the rendered review verdict, written into the worktree's
# ``.claude/`` beside ``cw-context.json`` before any tracker post is attempted
# (#2095). On a tracker the daemon cannot write to (Linear -- ADR-0013 keeps
# cw's only programmatic tracker client GitHub-only), this file IS the record.
#
# Git-ignored, not tracked (#2279, ADR-0016 follow-up F10). It was tracked
# until then, so each ticket's ``git add -A`` committed its own verdict, the
# merge carried it to ``main``, and every sibling branch's next base sync
# delivered some OTHER ticket's verdict into its worktree -- where finalize
# reads it as the authority on whether to halt (#2205). The provenance header
# below is the second half of that fix: a consumer can verify the file names
# the ticket and sha it is about to act on, and refuse a mismatch.
REVIEW_VERDICT_COMMENT_RELATIVE_PATH = Path(".claude") / "review-verdict.md"

# Ownership-stamp marker and line format prefixed onto the durable verdict
# file (#2279), hoisted so the writer and every test assertion share one
# source instead of repeating the literal.
REVIEW_VERDICT_OWNER_MARKER = "cw-review-verdict-owner"
REVIEW_VERDICT_OWNER_STAMP_FORMAT = (
    "<!-- " + REVIEW_VERDICT_OWNER_MARKER + " ticket_id={ticket_id} "
    "reviewed_sha={reviewed_sha} -->\n"
)


def _persist_worktree_artifact(
    worktree: Path, text: str, *, relative_path: Path
) -> Path | None:
    """Write *text* to ``worktree / relative_path``, best-effort (#2223).

    The shared mkdir/atomic-write/catch-OSError/never-raise contract every
    worktree-persisted artifact needs, factored out so a caller that must NOT
    prefix a Markdown ownership-comment stamp (e.g. a JSON artifact, which the
    stamp would make unparseable) doesn't have to fork the write logic to get
    it (#2223 review round 2). :func:`_persist_review_verdict` is the
    stamp-prefixing caller; :func:`_persist_structured_review_verdict` is the
    plain one.

    Returns the path written, or None when the write failed (logged). Never
    raises: every caller runs on the daemon thread's success path after the
    sentinel has already been persisted, and a filesystem hiccup must not turn
    a completed review into an unexpected-error completion.
    """
    path = worktree / relative_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
    except OSError as exc:
        _log.warning("review_verdict_persist_failed path=%s: %s", path, exc)
        return None
    return path


def _persist_review_verdict(
    worktree: Path,
    review_text: str,
    *,
    ticket_id: str,
    reviewed_sha: str,
    relative_path: Path = REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
) -> Path | None:
    """Write *review_text* to the worktree's durable verdict file, best-effort,
    prefixed with an ownership stamp (``ticket_id``/``reviewed_sha``) (#2279).

    *relative_path* defaults to the rendered-verdict destination but is
    overridable so :func:`_persist_unparseable_artifact` (#2280) can reuse
    this same write path for its blocker-shaped artifact rather than keeping
    a second mkdir/atomic-write/log implementation.

    Returns the path written, or None when the write failed (logged). Never
    raises: see :func:`_persist_worktree_artifact`.
    """
    stamped_text = (
        REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id=ticket_id, reviewed_sha=reviewed_sha
        )
        + review_text
    )
    return _persist_worktree_artifact(
        worktree, stamped_text, relative_path=relative_path
    )


# Durable copy of the STRUCTURED review verdict, beside REVIEW_VERDICT_
# COMMENT_RELATIVE_PATH's rendered ``.md`` (#2223/#1108). Where the ``.md``
# gives a human the rendered comment text, this gives a consumer (e.g.
# ``cw review check-voided``) the per-finding ``evidence``/``severity``/
# ``file``/``summary`` fields as real fields rather than prose — closing the
# gap where an operator adjudicating a ``codex_must_fix_findings`` park with
# the fix loop disabled had no source for ``check-voided``'s evidence anchor.
# Written through ``_persist_worktree_artifact`` directly, NOT through
# ``_persist_review_verdict`` (#2223 review round 2 correction of round 1):
# that helper's Markdown ownership-comment prefix makes the file invalid
# JSON. Ownership instead rides as native fields (``ticket_id`` on the
# wrapping ``ReviewVerdictEnvelope``, ``reviewed_sha`` already required on
# ``verdict`` itself) -- see :func:`_persist_structured_review_verdict`. The
# path constant, REVIEW_VERDICT_JSON_RELATIVE_PATH, lives in cw.review_findings
# beside the envelope model (#2205).


def _persist_structured_review_verdict(
    worktree: Path,
    verdict: ReviewVerdict,
    *,
    ticket_id: str,
    relative_path: Path = REVIEW_VERDICT_JSON_RELATIVE_PATH,
) -> Path | None:
    """Write *verdict* to the worktree as JSON, wrapped for provenance (#2223).

    Renders via :func:`~cw.review_findings.render_review_verdict_envelope`
    (``ticket_id`` + ``verdict``, the latter already carrying its own
    ``reviewed_sha``) and delegates the actual write to
    :func:`_persist_worktree_artifact` directly -- never
    :func:`_persist_review_verdict`, whose Markdown ownership-comment prefix
    would make the result invalid JSON.

    Returns the path written, or None when the write failed (logged). Never
    raises: see :func:`_persist_worktree_artifact`.
    """
    envelope_text = render_review_verdict_envelope(verdict, ticket_id=ticket_id)
    return _persist_worktree_artifact(
        worktree, envelope_text, relative_path=relative_path
    )


# Durable copy of a zero-documents park's blocker, written beside
# review-verdict.md (#2280). ``verdict`` is None on this path -- every
# reviewer failed, so there is no ReviewVerdict to render -- so this carries
# the blocker's reason/details instead of a rendered verdict comment.
REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH = (
    Path(".claude") / "review-verdict-unparseable.md"
)


def _persist_unparseable_artifact(
    worktree: Path,
    blocker: Blocker,
    *,
    ticket_id: str,
    reviewed_sha: str,
    reviewer_label: str = CODEX_REVIEWER_LABEL,
) -> Path | None:
    """Write *blocker*'s reason/details to the worktree, best-effort (#2280).

    Renders the blocker-shaped text this path carries (there is no
    ReviewVerdict to hand to :func:`render_verdict_comment`) and delegates
    the actual write -- mkdir, ownership stamp, atomic write, never-raises
    logging -- to :func:`_persist_review_verdict`, pointed at this artifact's
    own path, so the two callers share one writer instead of two.
    *reviewer_label* (#2791) names the backend in the header, so a Claude
    confirm failure is never filed as a codex one.
    """
    text = (
        f"# {reviewer_label} review unparseable\n\n"
        f"reason: {blocker.reason}\n\n{blocker.details}\n"
    )
    return _persist_review_verdict(
        worktree,
        text,
        ticket_id=ticket_id,
        reviewed_sha=reviewed_sha,
        relative_path=REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH,
    )


def _post_review_comment(
    ticket_id: str,
    review_text: str,
    *,
    cwd: Path | None = None,
    tracker: str | None = None,
    artifact_path: Path | None = None,
) -> None:
    """Post codex review findings as a GitHub issue comment (best-effort, logged).

    Delegates to the shared ``cw.gh.post_issue_comment`` primitive. A failed
    post is logged at warning (ticket_id, returncode, stderr) rather than
    swallowed silently — for the CODEX_MUST_FIX_FINDINGS path this comment is
    the only destination for the finding text (GitHub #1391).

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).

    *tracker* is the client's resolved ``tracking.primary.system`` (#2095).
    ``gh issue comment`` is GitHub-only: against a Linear-tracked ticket id it
    can only fail (``invalid issue format``), and every Linear ticket's review
    verdict was being lost that way. When the tracker is positively known to
    be non-GitHub the call is skipped and a WARNING names the tracker and the
    durable *artifact_path* (see :func:`_persist_review_verdict`) that now
    carries the verdict instead, so the omission is visible rather than a
    swallowed subprocess failure. Fail-open on an unresolvable tracker, the
    same polarity as every other tracker gate in cw.
    """
    if tracker is not None and tracker != TRACKER_GITHUB_ISSUES:
        _log.warning(
            "review_comment_skipped ticket=%s tracker=%s: the review verdict"
            " cannot be posted from the daemon (gh is GitHub-only); durable"
            " copy at %s",
            ticket_id,
            tracker,
            artifact_path,
        )
        return
    result = post_issue_comment(ticket_id, review_text, cwd=cwd)
    if result is None:
        _log.warning("review_comment_post_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "review_comment_post_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )
