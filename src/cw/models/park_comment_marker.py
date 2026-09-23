"""The worker-recorded park-comment marker (GitHub #2135).

One on-disk shape with two independent readers, so it lives once here rather
than beside either of them: ``cw.cli.signal_park`` writes it into the
worktree's ``.claude/cw-context.json`` and ``cw.cli.stop_hook`` reads it back
out on the next Stop. That is the same reason ``AGENT_SPAWN_STAMP_KEY`` and its
accessors sit in ``cw.models.orchestrator_config``; this pair gets its own
module only because that one is already past the module-size convention.

A pydantic model rather than a hand-rolled dict read because the marker is the
sole evidence for an automatic dev-queue row mutation. ``extra="forbid"`` plus
``AwareDatetime`` means a hand-edited or corrupted marker fails validation and
is treated as absent, which costs one deferral — the direction every failure
in this feature falls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from cw.models.enums import Stage

if TYPE_CHECKING:
    from collections.abc import Mapping

# The cw-context.json key the marker travels under. A sibling of
# ``agent_spawn_stamp`` and ``busy_wait_guard``, written through the same
# ``_write_cw_context_locked`` primitive, and deliberately NOT part of
# CW_CONTEXT_SCHEMA_VERSION: spawn never writes it, and an absent key is
# defined as "no marker".
PARK_COMMENT_MARKER_KEY = "park_comment_marker"


class ParkCommentMarker(BaseModel):
    """The worker's RECORDED CLAIM that it is taking a park/blocker exit (#2135).

    Not an observation by cw that a tracker comment exists — cw never checks
    the tracker. A headless worker runs ``cw signal-park`` after its park
    comment has posted and before it emits its sentinel; if the sentinel then
    never lands, this is what lets ``cw signal-stop`` park the row as
    ``stopped_without_sentinel`` instead of leaving it RUNNING for the liveness
    ladder.

    ``session_id`` and ``ticket_id`` are SELF-REFERENTIAL as a staleness guard:
    the writer reads them out of ``cw-context.json`` and the Stop hook compares
    them against that same file, so those two checks compare a file with
    itself. Only ``stage`` is checked against an independent source (the
    RUNNING dev-queue row). The real staleness guard is that ``cw.spawn``
    rewrites ``cw-context.json`` wholesale on every dispatch, so a marker from
    an earlier leg cannot survive a re-dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(min_length=1)
    stage: Stage
    session_id: str = Field(min_length=1)
    # AUDIT ONLY. Never compared to the wall clock: no age, no expiry, no
    # threshold -- any of those would make this a timer, which ARCHITECTURE.md
    # §7.13 forbids for an automatic actor. Its single non-audit use is as the
    # ordering pivot against transcript record timestamps in the Stop hook's
    # false-park guard, a comparison that can only ever SUPPRESS a park (the
    # mutation is keyed on the marker's presence, never on its age).
    # ``covers()`` does not read it at all.
    posted_at: AwareDatetime

    def covers(self, *, session_id: str, ticket_id: str, stage: Stage) -> bool:
        """Whether this marker is evidence for exactly this session/ticket/stage."""
        return (
            self.session_id == session_id
            and self.ticket_id == ticket_id
            and self.stage is stage
        )


def read_park_comment_marker(context: Mapping[str, object]) -> ParkCommentMarker | None:
    """Return the marker in *context*, or ``None`` when there isn't a valid one.

    A malformed marker is an absent marker, SILENTLY: no warning and no logger
    call. The writer is cw code, so the only ways to reach this branch are a
    hand edit or corruption, and the sole consequence is that the Stop hook
    defers exactly as it did before #2135 — nothing an operator needs told
    about on a hook that fires at every turn boundary.
    """
    raw = context.get(PARK_COMMENT_MARKER_KEY)
    if raw is None:
        return None
    try:
        return ParkCommentMarker.model_validate(raw)
    except ValidationError:
        return None
