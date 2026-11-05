"""Shared builders for the RFC 0016 adoption tests (#2943).

One adopted-row builder, one loader for the captured ``gh pr view`` payloads
and one real-git pinned origin, shared by the intake, policy, requeue, gate,
lock-discipline and CLI tests (precedent ``tests/_ticket_view_helpers.py``).

The fixtures under ``tests/fixtures/gh_pr_view_adopt_*.json`` are live
``gh pr view <N> --json state,isCrossRepository,isDraft,mergeable,
mergeStateStatus,headRefName,headRefOid,baseRefName,url,
closingIssuesReferences,statusCheckRollup`` captures, committed verbatim:

- ``2933``: an OPEN draft, ``mergeable: UNKNOWN``, five SUCCESS CheckRuns;
- ``2933_conflicting``: the same PR re-captured once GitHub computed it
  ``CONFLICTING`` / ``DIRTY``;
- ``2777_failing``: a CLOSED PR with one ``FAILURE`` CheckRun (``MERGEABLE``).

A variant a test cannot find a real capture for mutates exactly one top-level
field through :func:`load_adopt_fixture`'s keywords, and the call site says so
with a ``# mutated from capture: <field>`` comment.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.events import read_events
from cw.models import ClientConfig, OrchestratorEventType, Stage, TicketTask
from tests.conftest import (
    _make_ticket_task,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from cw.events import OrchestratorEvent

_FIXTURES = Path(__file__).parent / "fixtures"

ADOPT_CLIENT = "test-client"
ADOPT_BRANCH = "feature/ext-pr"
ADOPT_PIN = "a" * 40
ADOPT_PR_URL = "https://github.com/acme/widgets/pull/7"

#: The captured fixtures by short name.
FIXTURE_GREEN_UNKNOWN = "2933"
FIXTURE_CONFLICTING = "2933_conflicting"
FIXTURE_FAILING_CLOSED = "2777_failing"


def make_adopted_task(**overrides: object) -> TicketTask:
    """An adopted review-entry row; *overrides* replace any default."""
    kwargs: dict[str, object] = {
        "client": ADOPT_CLIENT,
        "branch": ADOPT_BRANCH,
        "adopted_head_sha": ADOPT_PIN,
        "adopted_pr_url": ADOPT_PR_URL,
        "pr_url": ADOPT_PR_URL,
        "stage": Stage.REVIEW,
    }
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def load_adopt_fixture(
    name: str = FIXTURE_GREEN_UNKNOWN, **mutations: object
) -> dict[str, object]:
    """A deep copy of a captured payload with only the named fields replaced."""
    path = _FIXTURES / f"gh_pr_view_adopt_{name}.json"
    payload: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    for key, value in mutations.items():
        if key not in payload:
            msg = f"{key!r} is not a field of the {name} capture"
            raise KeyError(msg)
        payload[key] = value
    return payload


def stub_pr_view(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | None
) -> list[str]:
    """Stub ``cw.adopt_pr.fetch_pr_view`` with *payload*; returns the PR refs read."""
    calls: list[str] = []

    def _fake(pr_ref: str, **_kw: object) -> dict[str, object] | None:
        calls.append(pr_ref)
        return payload

    monkeypatch.setattr("cw.adopt_pr.fetch_pr_view", _fake)
    return calls


def _pin_events() -> list[OrchestratorEvent]:
    """Return adoption-pin transition events from the real event log."""
    return [
        event
        for event in read_events(event_types=[OrchestratorEventType.TASK_TRANSITION])
        if event.payload.get("transition") == "adoption_pin"
    ]


def adopt_client(repo: Path, *, name: str = ADOPT_CLIENT) -> ClientConfig:
    """A worktree-mode ``ClientConfig`` whose git dir is *repo*."""
    return ClientConfig(
        name=name,
        workspace_path=repo,
        default_branch="main",
        worktree_base=repo.parent / "worktrees",
    )


class PinnedOrigin(NamedTuple):
    """A real client repo plus bare origin with *branch* pinned at ``pin``."""

    repo: Path
    origin: Path
    branch: str
    pin: str
    push_descendant: Callable[[str], str]
    push_diverged: Callable[[str], str]


def make_pinned_origin(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    *,
    branch: str = ADOPT_BRANCH,
) -> PinnedOrigin:
    """Build a pinned origin; the push helpers return the new origin head.

    ``push_descendant`` commits on top of the current origin tip from a side
    clone (a foreign push the client repo has not fetched).
    ``push_diverged`` force-pushes a rewrite whose parent is the pin's parent,
    so the pin is no longer an ancestor of the origin head.
    """
    repo, origin = make_git_repo_with_origin("adopt-client", branch=branch)
    pin = git_in(repo, "rev-parse", "HEAD")
    side = tmp_path / "side-clone"

    def _push_descendant(filename: str) -> str:
        return push_commit_to_origin(origin, branch, side, filename)

    def _push_diverged(filename: str) -> str:
        rewrite = tmp_path / "rewrite-clone"
        if not rewrite.exists():
            git_in(tmp_path, "clone", str(origin), str(rewrite))
        git_in(rewrite, "fetch", "origin")
        git_in(rewrite, "checkout", "-B", branch, f"{pin}~1")
        commit_tracked_file(rewrite, filename)
        git_in(rewrite, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
        return git_in(rewrite, "rev-parse", "HEAD")

    return PinnedOrigin(repo, origin, branch, pin, _push_descendant, _push_diverged)
