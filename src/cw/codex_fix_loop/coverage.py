"""The fix loop's carried re-review coverage gap (#2877).

From cycle 1 on, each re-review covers only the delta since the previous
reviewed head. A delta over the lane's prompt limit is reviewed shard by shard
(:func:`cw.codex_review.run_prepared_roles`), and a sharded re-review can leave
part of its delta unread: a reviewer role that failed in some shard, or a shard
no role fully reviewed. A later cycle's re-review covers only the changes made
after it, so it cannot stand in for that unread part.

:class:`DeltaGap` is the value the loop carries for it, beside cycle 0's own
``cycle0_unread`` (#2880): the merged coverage gap of every sharded re-review
that had one, and the cycles they came from. It is never cleared, and the clean
exit refuses over it (:func:`cw.codex_fix_loop.park._refuse_unread_coverage`).
An unsharded re-review is never folded, so a delta within the limit leaves the
loop exactly as it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.review_convergence import UnreadFindings, unread_from_verdict

if TYPE_CHECKING:
    from cw.review_findings import ReviewVerdict


class DeltaGap(NamedTuple):
    """The merged coverage gap of the fix loop's sharded re-reviews.

    ``unread`` holds only coverage: ``failed_roles`` is the first-seen-order
    union over the folded cycles and ``unreviewed_shards`` their sum, while
    ``rejected_must_fix`` and ``discarded_run_failures`` stay 0 (findings lost
    from a delta that WAS read are not a coverage gap). ``cycles`` lists, in
    order, the cycles whose re-review had a gap.
    """

    unread: UnreadFindings = UnreadFindings()
    cycles: tuple[int, ...] = ()

    @property
    def coverage_gap(self) -> bool:
        """True once any sharded re-review left part of its delta unread."""
        return bool(self.cycles)

    def fold(self, verdict: ReviewVerdict, cycle: int) -> DeltaGap:
        """*self* with *cycle*'s re-review gap merged in, else *self* unchanged.

        Only a sharded *verdict* (non-empty ``shards``) whose
        :func:`~cw.review_convergence.unread_from_verdict` tally has a coverage
        gap is folded; any other verdict returns this same object.
        """
        if not verdict.shards:
            return self
        unread = unread_from_verdict(verdict)
        if not unread.coverage_gap:
            return self
        merged = UnreadFindings(
            failed_roles=tuple(
                dict.fromkeys((*self.unread.failed_roles, *unread.failed_roles))
            ),
            unreviewed_shards=self.unread.unreviewed_shards + unread.unreviewed_shards,
        )
        return DeltaGap(merged, (*self.cycles, cycle))


#: The loop's starting value: no re-review has left anything unread.
NO_DELTA_GAP = DeltaGap()
