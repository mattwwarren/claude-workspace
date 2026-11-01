"""The per-shard coverage record of a sharded review round (#2864).

A confirm round whose diff is over the backend's prompt limit is split into
shards, and every reviewer role reviews every shard. One :class:`ShardRecord`
per shard is stamped on :attr:`ReviewVerdict.shards`, so the ledger and the
sentinel can say how many shards there were and whether each was reviewed.

Lives in its own submodule only to keep ``_models`` under the module-size
ceiling; import it from :mod:`cw.review_findings`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ShardRecord(BaseModel):
    """One shard of a sharded review round.

    ``index`` is the 1-based shard number, the same ``i`` as the ``shard i/n``
    annotations on a shard's failures. ``files`` are the repo paths in the
    shard, exactly as the diff's ``b/`` header names them (the same strings as
    the round's changed files); across a round's shards they are disjoint and
    their union is every changed file. ``prompt_chars`` is the longest rendered
    prompt over the roster roles for this shard, never above the limit.
    ``roles_reviewed`` lists, in roster order, the roles that returned a
    non-failed document (``ok`` or ``degraded``) for this shard. ``reviewed``
    is True only when every roster role is in ``roles_reviewed``: a role
    failure of any kind, or a ``failed`` document, makes it False.
    """

    index: int
    files: list[str]
    prompt_chars: int
    roles_reviewed: list[str] = Field(default_factory=list)
    reviewed: bool = False
