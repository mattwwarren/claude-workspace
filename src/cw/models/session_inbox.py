"""One operator message in a session's inbound mailbox (GitHub #2212).

A pydantic model rather than a hand-rolled dict because the message is the
durable half of ``cw session send``: the command's reliability bar is that a
message survives on disk whether or not the session can be resumed right now,
so a malformed record is a lost operator answer, not a cosmetic defect.
``extra="forbid"`` plus ``AwareDatetime`` means a hand-edited or torn record
fails validation loudly instead of being half-read — the same posture as
:class:`~cw.models.park_comment_marker.ParkCommentMarker`.

``frozen=True`` because consumption is tracked by a cursor keyed on ``id``
(see :mod:`cw.session_inbox`); a message that could be mutated after append
would let the cursor point at a record that no longer says what was consumed.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class SessionInboxMessage(BaseModel):
    """An operator message queued for one cw session.

    ``author`` is AUDIT ONLY — it is never compared for authorization, which
    is why ``cw session send`` defaults it to the local OS username rather
    than resolving a GitHub login over the network.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    created_at: AwareDatetime
    author: str = Field(min_length=1)
    body: str = Field(min_length=1)
