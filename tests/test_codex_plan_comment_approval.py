"""Run-level comment approval on a resumed codex PLAN round (#3465).

A large plan parked with no row approval; an operator-identity comment carrying
the approval token follows the park. The scan returns the same plan, so the
parked fingerprint still matches and the plan completes with no row approval.
The control leaves the token out and parks again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_plan._approval import APPROVAL_LINE
from cw.codex_plan._post import PARK_HEADER
from cw.codex_plan._result import PLAN_MD_RELATIVE_PATH, write_plan_draft
from cw.gh import agent_marked_body
from cw.plan_fingerprint import LeadingBlock
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.bindings_builders import bindings_github_cli
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

    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

OPERATOR = "operator-login"
_VIEW = ("issue", "view", TICKET, "--json", "title,body,url,comments")
_PARK_AT = "2026-11-10T10:00:00Z"
_APPROVE_AT = "2026-11-10T11:00:00Z"


def _park_body() -> str:
    return agent_marked_body(f"{PARK_HEADER}\n\n1. Which store?")


def _token_body() -> str:
    return f"Approved.\n\n{APPROVAL_LINE}\n"


def _operator_client(h: Harness) -> ClientConfig:
    base = bindings_github_cli()
    tickets = base.tickets
    assert tickets is not None
    bindings = base.model_copy(
        update={"tickets": tickets.model_copy(update={"operator_ids": [OPERATOR]})}
    )
    return codex_tracker_client(h.worktree).model_copy(update={"bindings": bindings})


def _snapshot(h: Harness, *, with_token: bool) -> None:
    comments: list[tuple[str, str, str]] = [("p1", _park_body(), _PARK_AT)]
    if with_token:
        comments.append(("c9", _token_body(), _APPROVE_AT))
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(comments))


def _park_draft(h: Harness) -> None:
    write_plan_draft(h.worktree, LARGE_PLAN, book=LeadingBlock(applied="none"))


def test_an_operator_token_after_the_park_completes_the_plan(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _park_draft(h)
    _snapshot(h, with_token=True)
    h.client = _operator_client(h)
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    result = h.run()
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()


def test_without_the_token_the_plan_parks_again(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _park_draft(h)
    _snapshot(h, with_token=False)
    h.client = _operator_client(h)
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
