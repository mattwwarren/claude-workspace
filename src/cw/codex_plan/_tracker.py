"""The codex PLAN resume round's read of the tracker state (#3464).

cw, not the model, computes what the resumed round compares: the tracker
fingerprint (``operator_comment=<id|none>|body_sha=<sha>``) and the
resolutions source identity (``row:<N>:<sha>``, ``comment:<id>``,
``body:<sha>`` or ``none``). Both are read from one ticket snapshot through
the trust evaluator (:func:`~cw.ticket_snapshot.bound_comments` with
``Purpose.OPERATOR_TEXT``), so the two rule sets (GitHub and every other
tracker) are the evaluator's, not ours. On top of it cw drops the comments
that are pipeline-authored without the agent marker: a pipeline fixed header
or a plan of record. ``minimized`` is never consulted.

Snapshot order is oldest first, as the tracker returned it; "newest" is the
LAST qualifying comment in that order, never a timestamp max.

The functions take the snapshot and the :class:`SnapshotContext` as
arguments and only classify; the resume gate decides what to do.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.codex_fix_loop.constraints import RESOLUTIONS_MARKER
from cw.codex_plan._post import PARK_HEADER
from cw.codex_plan._settle import unlabelled
from cw.context_queue_metadata import operator_resolutions_source
from cw.gh import _PLAN_MARKER, PIPELINE_FIXED_HEADERS
from cw.plan_fingerprint import compute_body_sha
from cw.ticket_snapshot import Purpose, bound_comments

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.operator_resolution import OperatorResolution
    from cw.ticket_snapshot import SnapshotContext, TicketSnapshot

#: The ``none`` value of a source identity and of ``operator_comment``.
SOURCE_NONE: Final = "none"
COMMENT_SOURCE_PREFIX: Final = "comment:"
BODY_SOURCE_PREFIX: Final = "body:"
ROW_SOURCE_PREFIX: Final = "row:"

# A markdown ATX heading line (the body section starts at the last one).
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")


@dataclass(frozen=True)
class AuthorityComment:
    """One entry of the ordered authority stream (#3465).

    Unfiltered by the fingerprint's operator rules, so the staleness guard can
    see a later park header or operator comment that the filtered ``comments``
    tuple cannot express. ``operator`` is an operator-authority comment,
    ``approver`` one that may carry a plan-approval token (operator identity),
    ``header`` a pipeline fixed-header comment and ``park`` a park header.
    """

    id: str
    body: str
    operator: bool
    approver: bool
    header: bool
    park: bool


@dataclass(frozen=True)
class TrackerObservation:
    """What one ticket snapshot shows the resumed round.

    ``observed`` is False when there is no snapshot or the context cannot
    read comments: the comment and body channels are then unobserved and
    every other field is empty. ``comments`` are the operator-authority
    comments (``{id, body, ...}`` dicts), oldest first.
    """

    observed: bool
    comments: tuple[dict[str, object], ...] = ()
    body: str = ""
    #: The ordered authority stream, oldest first (empty when unobserved).
    authority_stream: tuple[AuthorityComment, ...] = ()

    @property
    def operator_comment(self) -> str:
        """The newest operator-authority comment id, or ``none``."""
        return str(self.comments[-1]["id"]) if self.comments else SOURCE_NONE

    @property
    def body_sha(self) -> str:
        return compute_body_sha(self.body)

    @property
    def fingerprint(self) -> str | None:
        """The ``plan-stage-last-evaluated`` value, ``None`` when unobserved."""
        if not self.observed:
            return None
        return f"operator_comment={self.operator_comment}|body_sha={self.body_sha}"

    def marker_comment_ids(self) -> list[str]:
        """The ids of the comments carrying the resolutions marker, oldest first."""
        return [
            str(comment["id"])
            for comment in self.comments
            if RESOLUTIONS_MARKER in _body(comment)
        ]


@dataclass(frozen=True)
class ResolutionsSource:
    """The round's current resolutions source and the friction its choice adds.

    ``current`` is ``None`` when it cannot be determined: no row resolution
    and the comment and body channels are unobserved.
    """

    current: str | None
    friction: tuple[str, ...] = ()


def _body(comment: dict[str, object]) -> str:
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def is_pipeline_authored(body: str) -> bool:
    """A fixed-header pipeline comment or a plan of record (not agent-marked)."""
    return body.lstrip().startswith(PIPELINE_FIXED_HEADERS) or _PLAN_MARKER in body


def has_pipeline_header(body: str) -> bool:
    """A pipeline fixed header, with an optional ``(part i of N)`` label stripped."""
    text, _, _ = unlabelled(body)
    return text.startswith(PIPELINE_FIXED_HEADERS)


def _authority_stream(
    snapshot: TicketSnapshot, ctx: SnapshotContext
) -> tuple[AuthorityComment, ...]:
    """The ordered stream: operator comments and pipeline-header comments."""
    operator_ids = {
        str(c["id"]) for c in bound_comments(snapshot, ctx, Purpose.OPERATOR_TEXT)
    }
    approver_ids = {
        str(c["id"]) for c in bound_comments(snapshot, ctx, Purpose.APPROVAL_COMMENT)
    }
    stream: list[AuthorityComment] = []
    for comment in snapshot.comments:
        header = has_pipeline_header(comment.body)
        operator = (
            not header
            and comment.id in operator_ids
            and not is_pipeline_authored(comment.body)
        )
        if not (header or operator):
            continue
        text, index, _ = unlabelled(comment.body)
        stream.append(
            AuthorityComment(
                id=comment.id,
                body=comment.body,
                operator=operator,
                approver=operator and comment.id in approver_ids,
                header=header,
                park=index == 1 and text.startswith(PARK_HEADER),
            )
        )
    return tuple(stream)


def observe_tracker(
    snapshot: TicketSnapshot | None, ctx: SnapshotContext
) -> TrackerObservation:
    """Classify *snapshot* under *ctx*; unobserved when absent or unreadable."""
    if snapshot is None or not ctx.comments_readable:
        return TrackerObservation(observed=False)
    comments = tuple(
        comment
        for comment in bound_comments(snapshot, ctx, Purpose.OPERATOR_TEXT)
        if not is_pipeline_authored(_body(comment))
    )
    return TrackerObservation(
        observed=True,
        comments=comments,
        body=snapshot.body,
        authority_stream=_authority_stream(snapshot, ctx),
    )


def body_resolutions_section(body: str) -> str | None:
    """The body's resolutions section, stripped; ``None`` without the marker.

    The section runs from the last markdown heading before the (first)
    marker line through the marker line; with no heading before it, from
    the start of the body.
    """
    lines = body.splitlines()
    marker_index = next(
        (i for i, line in enumerate(lines) if RESOLUTIONS_MARKER in line), None
    )
    if marker_index is None:
        return None
    start = 0
    for i in range(marker_index, -1, -1):
        if _HEADING.match(lines[i]):
            start = i
            break
    return "\n".join(lines[start : marker_index + 1]).strip()


def body_source(body: str) -> str | None:
    """``body:<sha>`` of the body's resolutions section, ``None`` without one."""
    section = body_resolutions_section(body)
    if section is None:
        return None
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    return f"{BODY_SOURCE_PREFIX}{digest}"


def _mirror_friction(ids: Sequence[str], highest_round: int) -> tuple[str, ...]:
    return tuple(
        f"resolutions-comment-ignored-as-mirror: comment {comment_id} carries the "
        "preflight marker but the dev-queue row already holds operator_resolutions "
        f"(round {highest_round}); record it with cw dev-queue resolve"
        for comment_id in ids
    )


def discover_resolutions_source(
    resolutions: Sequence[OperatorResolution], observation: TrackerObservation
) -> ResolutionsSource:
    """The round's current resolutions source identity.

    Row first: a non-empty row list is the sole source, and each observed
    (human-posted) marker comment is an un-tallied mirror. Otherwise a
    marker-bearing body section outranks comments (body-over-comment, so a
    comment echoing the body never trips the multi-marker rule); otherwise
    the newest marker comment wins, with a ``multi-marker`` friction line when
    there is more than one. ``none`` when nothing carries the marker.
    """
    row = operator_resolutions_source(resolutions)
    if row is not None:
        highest = max(resolution.round for resolution in resolutions)
        mirrors = observation.marker_comment_ids() if observation.observed else []
        return ResolutionsSource(row, _mirror_friction(mirrors, highest))
    if not observation.observed:
        return ResolutionsSource(None)
    body = body_source(observation.body)
    if body is not None:
        return ResolutionsSource(body)
    markers = observation.marker_comment_ids()
    if not markers:
        return ResolutionsSource(SOURCE_NONE)
    friction: tuple[str, ...] = ()
    if len(markers) > 1:
        friction = (
            f"multi-marker: {len(markers)} resolution comments found — newest "
            "(last in snapshot order) used; consolidate via /harden-ticket when "
            "convenient",
        )
    return ResolutionsSource(f"{COMMENT_SOURCE_PREFIX}{markers[-1]}", friction)


def source_observed(source: str, observation: TrackerObservation) -> bool:
    """Whether a ``comment:`` or ``body:`` *source* is positively observed.

    A comment source is observed when that comment is still a bound marker
    comment; a body source when the body's resolutions section is still
    present (whatever its digest). Absence is never evidence of a change; an
    unobserved round has no marker comments and an empty body.
    """
    if source.startswith(COMMENT_SOURCE_PREFIX):
        comment_id = source.removeprefix(COMMENT_SOURCE_PREFIX)
        return comment_id in observation.marker_comment_ids()
    if source.startswith(BODY_SOURCE_PREFIX):
        return body_source(observation.body) is not None
    return False


__all__ = [
    "BODY_SOURCE_PREFIX",
    "COMMENT_SOURCE_PREFIX",
    "ROW_SOURCE_PREFIX",
    "SOURCE_NONE",
    "AuthorityComment",
    "ResolutionsSource",
    "TrackerObservation",
    "body_resolutions_section",
    "body_source",
    "discover_resolutions_source",
    "has_pipeline_header",
    "is_pipeline_authored",
    "observe_tracker",
    "source_observed",
]
