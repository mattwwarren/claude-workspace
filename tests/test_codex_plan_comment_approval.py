"""Run-level comment approval on a resumed codex PLAN round (#3465).

A large plan parked with no row approval; an operator-identity comment carrying
the approval token follows the park. When the scan returns the parked plan, its
fingerprint still matches and the plan completes with no row approval. A scan
that rewrites the plan consumes the approval: the plan parks again, and the
friction names the consumed comment. The control leaves the token out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_plan._result import (
    PLAN_MD_RELATIVE_PATH,
    REVIEW_MARKER_PREFIXES,
    write_plan_draft,
)
from cw.plan_fingerprint import LeadingBlock
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.plan_approval_builders import (
    APPROVAL_AT,
    PARK_AT,
    park_body,
    token_body,
    with_operator_ids,
)
from tests._shared.ticket_context import issue_envelope_json
from tests.test_codex_plan_run import (
    LARGE_PLAN,
    TICKET,
    Harness,
    add_stations,
    add_turn,
    harness,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

_VIEW = ("issue", "view", TICKET, "--json", "title,body,url,comments")
_CONSUMED = "plan_comment_approval_consumed: comment=c9"
_ANY_CONSUMED = "plan_comment_approval_consumed"
REWRITTEN_PLAN = LARGE_PLAN.replace("Add the widget.", "Add the gadget.")


def _snapshot(h: Harness, *, with_token: bool) -> None:
    comments: list[tuple[str, str, str]] = [("p1", park_body(), PARK_AT)]
    if with_token:
        comments.append(("c9", token_body(), APPROVAL_AT))
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(comments))


def _parked(h: Harness, *, with_token: bool = True) -> Harness:
    """*h* resumed from a parked large draft, with an operator identity bound."""
    write_plan_draft(h.worktree, LARGE_PLAN, book=LeadingBlock(applied="none"))
    _snapshot(h, with_token=with_token)
    h.client = with_operator_ids(codex_tracker_client(h.worktree))
    return h


def _consumed(result: AutoDevResult, prefix: str) -> list[str]:
    return [f for f in result.friction_highlights if f.startswith(prefix)]


def _assert_reparked_with_consumed_friction(h: Harness) -> None:
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert _consumed(result, _CONSUMED)


def test_an_operator_token_after_the_park_completes_the_plan(
    harness: Callable[..., Harness],
) -> None:
    h = _parked(harness())
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    result = h.run()
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert not _consumed(result, _ANY_CONSUMED)


def test_without_the_token_the_plan_parks_again(
    harness: Callable[..., Harness],
) -> None:
    h = _parked(harness(), with_token=False)
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert not _consumed(result, _ANY_CONSUMED)


def test_a_scan_that_rewrites_the_plan_consumes_the_approval(
    harness: Callable[..., Harness],
) -> None:
    """The approval was for the parked plan: a different plan needs a new one."""
    h = _parked(harness())
    add_stations(add_turn(h.runner, REWRITTEN_PLAN, tier="large"))
    _assert_reparked_with_consumed_friction(h)
    assert len(h.plan_calls) == 1


def test_a_rewrite_differing_only_by_cleaning_and_whitespace_still_approves(
    harness: Callable[..., Harness],
) -> None:
    """The comparison uses the draft normal form, not the model's raw text."""
    marker = REVIEW_MARKER_PREFIXES[0]
    noisy = f"{marker} 2026-11-10 v2 -->\n{LARGE_PLAN.rstrip()}"
    assert not noisy.endswith("\n")
    h = _parked(harness())
    add_stations(add_turn(h.runner, noisy, tier="large"))
    result = h.run()
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
