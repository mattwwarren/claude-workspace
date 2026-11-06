"""Shared builders and fakes for the red-shipped-PR tests (#2945)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cw import gh
from cw.dev_queue.shipped_red import red_fingerprint
from cw.models import (
    PrState,
    QueueItemStatus,
    ShippedRedEpisode,
    Stage,
    TicketTask,
)
from tests._adopt_pr_helpers import (
    ADOPT_PR_URL,
    FIXTURE_FAILING_CLOSED,
    load_adopt_fixture,
)
from tests._changelog_repair_helpers import DISARM_OK, FakeGhHead
from tests._ticket_view_helpers import make_task

if TYPE_CHECKING:
    import pytest

FIXTURES = Path(__file__).parent / "fixtures"
HEAD_SHA = "a" * 40
CHECK = "test (macos-latest, 3.13)"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def load_capture(name: str) -> Any:
    """A verbatim JSON capture from ``tests/fixtures``."""
    return json.loads((FIXTURES / name).read_text())


def rollup_with_conclusion(conclusion: str) -> list[dict[str, Any]]:
    """The captured PR 2777 rollup with its one COMPLETED FAILURE entry mutated."""
    rollup = copy.deepcopy(
        load_adopt_fixture(FIXTURE_FAILING_CLOSED)["statusCheckRollup"]
    )
    mutated = [
        e
        for e in rollup
        if e.get("status") == "COMPLETED" and e.get("conclusion") == "FAILURE"
    ]
    assert len(mutated) == 1
    mutated[0]["conclusion"] = conclusion  # mutated from capture: conclusion
    return rollup


def make_episode(
    *,
    names: list[str] | None = None,
    alerted: bool = True,
    head_sha: str | None = HEAD_SHA,
    evaluated_at: datetime = NOW,
    required: list[str] | None = None,
    **overrides: Any,
) -> ShippedRedEpisode:
    """A latch; *alerted* stamps ``alerted_at`` and the episode head."""
    shown = names if names is not None else [CHECK]
    req = required if required is not None else list(shown)
    base: dict[str, Any] = {
        "hydrated_fingerprint": red_fingerprint(shown),
        "fingerprint": red_fingerprint(req) if alerted else None,
        "required_checks": req if alerted else [],
        "head_sha": head_sha if alerted else None,
        "evaluated_at": evaluated_at,
        "alerted_at": evaluated_at if alerted else None,
    }
    base.update(overrides)
    return ShippedRedEpisode(**base)


def make_red_row(
    *,
    alerted: bool = False,
    failing: list[str] | None = None,
    **overrides: Any,
) -> TicketTask:
    """A COMPLETED, shipped, FINALIZE row with a red hydrated OPEN PR."""
    names = failing if failing is not None else [CHECK]
    kwargs: dict[str, Any] = {
        "status": QueueItemStatus.COMPLETED,
        "stage": Stage.FINALIZE,
        "disposition": "shipped",
        "pr_url": ADOPT_PR_URL,
        "session_id": "sess-1",
        "pr_state": PrState(state="OPEN", ci_ok=False, failing_checks=names),
    }
    if alerted:
        kwargs["shipped_red"] = make_episode(names=names)
    kwargs.update(overrides)
    return make_task(**kwargs)


class FakeRequiredChecks:
    """Recording stub of ``gh.fetch_required_failing_checks``.

    *result* is a list of check names (failing), ``[]`` (a successful lookup
    with none failing) or ``None`` (unknown); ``[]`` and ``None`` differ.
    """

    def __init__(self, result: list[str] | None) -> None:
        self.result = result
        self.calls = 0

    def __call__(
        self, pr_ref: str, *, timeout: int = 15, cwd: Path | None = None
    ) -> list[gh.RequiredCheck] | None:
        self.calls += 1
        if self.result is None:
            return None
        return [gh.RequiredCheck(n, "CI", f"https://x/{n}") for n in self.result]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeRequiredChecks:
        monkeypatch.setattr(gh, "fetch_required_failing_checks", self)
        return self


def install_red_gh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    armed: bool = True,
    required: list[str] | None = None,
    disarm: str = DISARM_OK,
    oid: str = HEAD_SHA,
) -> tuple[FakeGhHead, FakeRequiredChecks]:
    """Wire the head fake and the required-checks fake onto ``cw.gh``."""
    head = FakeGhHead([], armed=armed, oid=oid, disarm=disarm).install(monkeypatch)
    req = FakeRequiredChecks([CHECK] if required is None else required)
    return head, req.install(monkeypatch)
