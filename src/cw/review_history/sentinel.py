"""The sentinel path of :mod:`cw.review_history`: the fallback without a ledger (#2790).

Pure. Builds the round views and summary from a session's ``AUTO_DEV_RESULT``
``review`` block and its ``review_round_<n>`` friction lines.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, get_args

from cw.models.review_history import (
    HistorySummary,
    RoundView,
    RoundViewKind,
    SeveritySplit,
)
from cw.review_history.inputs import _int, _Part, _series

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The ``kind=`` value of a friction line, mapped onto itself: every recorded
#: round kind is a view kind of the same name (``delta``, historical since
#: #2979, still parses for an old ledger). Derived
#: from the :data:`RoundViewKind` Literal minus ``initial``, which no friction
#: line carries.
_FRICTION_KINDS: dict[str, RoundViewKind] = {
    kind: kind for kind in get_args(RoundViewKind) if kind != "initial"
}

#: One ``review_round_<n>`` line of ``cw.review_convergence.friction_lines``.
#: The ``kind`` alternation is built from :data:`_FRICTION_KINDS`, so a kind
#: added to :data:`RoundViewKind` matches without a second edit.
_FRICTION_RE = re.compile(
    r"^review_round_(?P<round>\d+): kind=(?P<kind>"
    + "|".join(map(re.escape, _FRICTION_KINDS))
    + r") backend=(?P<backend>\S*) sha=(?P<sha>\S*) "
    r"new_accepted=(?P<new>\d+) settled=(?P<settled>\d+)$"
)


def _sentinel_part(
    sentinel: Mapping[str, object] | None,
    review: Mapping[str, object] | None,
    notes: list[str],
) -> _Part:
    rounds = _sentinel_rounds(sentinel, review, notes)
    if review is None:
        summary = HistorySummary(
            convergence_mode="unknown", new_accepted_series=_series(rounds)
        )
        return _Part(rounds, [], summary)
    deferred = _int(review.get("deferred"))
    if deferred:
        notes.append(
            f"sentinel reports {deferred} deferred finding(s); their text is "
            "recorded only in a converge ledger"
        )
    final_sha = review.get("final_round_clean_sha")
    backend = review.get("confirm_backend")
    converged = review.get("converged")
    summary = HistorySummary(
        convergence_mode="clean" if _is_clean_mode(review) else "blocking",
        rounds_run=_int(review.get("rounds_run")),
        converged=converged if isinstance(converged, bool) else None,
        final_clean_sha=final_sha if isinstance(final_sha, str) else None,
        fix_cycles_used=_int(review.get("fix_cycles_used")),
        confirm_backend=backend if isinstance(backend, str) else None,
        new_accepted_series=_series(rounds),
        deferred_count=deferred,
    )
    return _Part(rounds, [], summary)


def _is_clean_mode(review: Mapping[str, object]) -> bool:
    return review.get("convergence_mode") == "clean"


def _sentinel_rounds(
    sentinel: Mapping[str, object] | None,
    review: Mapping[str, object] | None,
    notes: list[str],
) -> list[RoundView]:
    friction = sentinel.get("friction_highlights") if sentinel is not None else None
    rounds = _parse_friction_lines(friction if isinstance(friction, list) else [])
    if rounds:
        return rounds
    if review is None:
        notes.append(
            "no session AUTO_DEV_RESULT with a review block found for this row"
        )
        return []
    if _is_clean_mode(review):
        notes.append(
            "the sentinel's review block is clean-mode but carries no "
            "review_round_<n> friction lines; per-round data not recorded"
        )
        return []
    notes.append(
        "rounds_unrecorded: a blocking-mode review records no per-round data; "
        "per-round history needs review.convergence: clean"
    )
    cycle0 = _blocking_cycle0(review)
    if cycle0 is None:
        notes.append(
            "the sentinel's review block carries no must_fix_initial/should_fix"
        )
        return []
    return [cycle0]


def _parse_friction_lines(lines: Sequence[object]) -> list[RoundView]:
    """Round views from ``review_round_<n>`` friction lines; others are ignored.

    The inverse of ``cw.review_convergence.friction_lines`` for the round
    lines (a test round-trips the two). The first line per round wins.
    """
    views: list[RoundView] = []
    seen: set[int] = set()
    for line in lines:
        match = _FRICTION_RE.match(line.strip()) if isinstance(line, str) else None
        if match is None or int(match["round"]) in seen:
            continue
        number = int(match["round"])
        seen.add(number)
        new = int(match["new"])
        kind = _FRICTION_KINDS[match["kind"]]
        views.append(
            RoundView(
                round=number,
                kind=kind,
                source="sentinel_friction",
                backend=match["backend"] or None,
                reviewed_sha=match["sha"] or None,
                new_accepted=new,
                settled_total=int(match["settled"]),
                clean=new == 0,
            )
        )
    return views


def _blocking_cycle0(review: Mapping[str, object]) -> RoundView | None:
    must = _int(review.get("must_fix_initial"))
    should = _int(review.get("should_fix"))
    if must is None or should is None:
        return None
    return RoundView(
        round=1,
        kind="initial",
        source="sentinel_cycle0",
        new_accepted=must + should,
        new_by_severity=SeveritySplit(must_fix=must, should_fix=should),
    )
