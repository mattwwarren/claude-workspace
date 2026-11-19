"""Durable artifacts and the outbox enqueue for one review verdict (#2807 split).

Moved verbatim out of :mod:`cw.codex_background` (which kept growing past the
module-size ceiling) and still called only from its Step 4b: the worktree
copies of a review verdict (``.claude/review-verdict.md``, the structured
``.claude/review-verdict.json`` and the zero-documents
``.claude/review-verdict-unparseable.md``) plus the prose enqueue that carries
the rendered verdict to the ticket (RFC 0019 O3, #3707). Every write here is
best-effort and never raises; see :func:`_persist_worktree_artifact`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.codex_review import CODEX_REVIEWER_LABEL
from cw.codex_review_signals import (
    emit_verdict_comment_skipped,
    tracker_label,
)
from cw.outbox_producer import SKIP_ENQUEUE_FAILED, enqueue_prose, try_content_marker
from cw.review_findings import (
    REVIEW_VERDICT_JSON_RELATIVE_PATH,
    render_review_verdict_envelope,
)

if TYPE_CHECKING:
    from cw.auto_dev_result import Blocker
    from cw.codex_review_signals import VerdictCommentKind
    from cw.models import ClientConfig
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
# ``file``/``summary`` fields as real fields rather than prose. Written
# through ``_persist_worktree_artifact`` directly, NOT through
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


#: The producer slug the codex verdict and unparseable comments enqueue under.
VERDICT_PRODUCER = "codex-verdict"


def _post_daemon_verdict_comment(
    ticket_id: str,
    review_text: str,
    *,
    client: ClientConfig,
    session_id: str,
    kind: VerdictCommentKind,
    reviewed_sha: str | None,
    artifact_path: Path | None,
) -> None:
    """Enqueue the rendered verdict as a ticket comment (best-effort, logged).

    The marker is ``<kind>-<reviewed_sha[:12]>-<sha256(review_text)[:16]>``, so an
    identical retry dedups and different text posts. The verdict is stored raw
    (``redacted=False``): it carries "### Settle a finding" JSON payloads copied
    verbatim, as the GitHub path always posted them. No tracker call happens
    here; the drain delivers the entry (O2).

    The event fires only when nothing was enqueued: a refused enqueue, an
    oversize body, or a duplicate. The durable copy at *artifact_path* stays the
    record in every case.
    """
    sha_part = (reviewed_sha or "none")[:12]
    marker = try_content_marker(f"{kind}-{sha_part}", review_text)
    tracker = tracker_label(client)
    if marker is None:
        reason = SKIP_ENQUEUE_FAILED
        _log.warning(
            "review_comment_skipped ticket=%s tracker=%s reason=%s: durable copy at %s",
            ticket_id,
            tracker,
            reason,
            artifact_path,
        )
        emit_verdict_comment_skipped(
            ticket_id=ticket_id,
            client=client.name,
            tracker=tracker,
            kind=kind,
            artifact_path=artifact_path,
            reviewed_sha=reviewed_sha,
            session_id=session_id,
            reason=reason,
            entry_id=None,
        )
        return
    outcome = enqueue_prose(
        client.name, ticket_id, VERDICT_PRODUCER, marker, (review_text,)
    )
    if outcome.skipped_reason is None:
        return
    _log.warning(
        "review_comment_skipped ticket=%s tracker=%s reason=%s: durable copy at %s",
        ticket_id,
        tracker,
        outcome.skipped_reason,
        artifact_path,
    )
    emit_verdict_comment_skipped(
        ticket_id=ticket_id,
        client=client.name,
        tracker=tracker,
        kind=kind,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
        reason=outcome.skipped_reason,
        entry_id=outcome.entry_id,
    )
