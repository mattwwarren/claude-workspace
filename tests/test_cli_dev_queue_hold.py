"""``cw dev-queue hold`` CLI: refusals, conversion, repeat and watch (#2321)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    SessionStatus,
    TicketTask,
    WatchedPr,
)
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION
from cw.models.pr_ref import PrRef
from cw.reconcile.tasks import release_stale_gated_tasks
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    events_of,
    gh_pr_view_json,
    install_acme_client,
    invoke_dev_queue,
    make_task,
    queue_row,
    seed_queue,
)
from tests._tool_fakes import FakeGhCli

_HOLD_ARGS = ("hold", ACME_TICKET, "--until-pr-merged", "9", "-c", ACME)
_OPEN_PR = gh_pr_view_json("foo/bar", 9)
_REF_9 = PrRef(number=9)


@pytest.fixture
def repo_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside any dispatch worktree; origin resolves to ``foo/bar``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "cw.cli.dev_queue.hold._resolve_repo_slug", lambda _git_dir: "foo/bar"
    )


def _watches() -> list[WatchedPr]:
    return list(load_dev_queue().watched_prs)


def test_happy_path_holds_pending_row_and_registers_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    row = queue_row()
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == DEPENDENCY_HOLD_DISPOSITION
    assert row.blocked_reason == "dependency_unmerged"
    assert row.blocked_on_pr == _REF_9
    assert row.session_id is None
    assert "Held ACM-1 in acme dev-queue until PR #9 merges" in result.output
    # The echoed row exposes the PR the hold waits on (#2321 R7).
    assert "blocked_on_pr: 9" in result.output
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.correlation_id == ACME_TICKET
    assert held.payload["outcome"] == "held"
    assert held.payload["pr_number"] == 9
    assert held.payload["repo"] == "foo/bar"
    [watch] = _watches()
    assert watch.source == "dependency_hold"
    assert watch.client == ACME
    assert watch.pr_number == 9
    assert len(fake.records) == 1


def test_lock_discipline_one_gh_call_outside_any_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    assert invoke_dev_queue(*_HOLD_ARGS).exit_code == 0
    assert all(snapshot == () for snapshot in fake.lock_snapshots)
    assert len(fake.records) == 1


def test_refuses_row_that_already_has_a_pr_and_leaves_it_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition="merge_pending",
                blocked_reason="automerge_not_armed",
                pr_url="https://github.com/foo/bar/pull/5",
            )
        ]
    )
    before = queue_row().model_dump()
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "row already has PR #5; a hold applies before a PR exists" in result.output
    assert queue_row().model_dump() == before
    assert fake.records == []
    assert _watches() == []


@pytest.mark.parametrize(
    "session_status",
    [SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED],
)
def test_refuses_row_bound_to_live_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    session_status: SessionStatus,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    task = make_task(status=QueueItemStatus.BLOCKED_ON_USER, session_id="sess-1")
    seed_queue([task], [bound_session_for(task, status=session_status)])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "close the session first" in result.output
    assert "cw spawn close" in result.output
    assert queue_row().session_id == "sess-1"
    assert fake.records == []
    assert _watches() == []


def test_worker_park_is_converted_to_operator_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    task = make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition=DEPENDENCY_HOLD_DISPOSITION,
        blocked_reason="dependency_unmerged",
        blocked_on_pr=9,
        session_id="sess-1",
    )
    seed_queue([task], [bound_session_for(task, status=SessionStatus.COMPLETED)])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert "updated (converted from worker park)" in result.output
    assert "unchanged" not in result.output
    row = queue_row()
    assert row.session_id is None
    assert row.blocked_on_pr == _REF_9
    assert len(fake.records) == 1
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["outcome"] == "updated"
    assert held.payload["converted_from_worker_park"] is True
    assert held.payload["cleared_session_id"] == "sess-1"


def test_repeat_on_operator_hold_is_unchanged_with_zero_gh_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition=DEPENDENCY_HOLD_DISPOSITION,
                blocked_reason="dependency_unmerged",
                blocked_on_pr=9,
            )
        ]
    )
    before = queue_row().model_dump()
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert "blocked_on_pr: 9" in result.output
    assert queue_row().model_dump() == before
    assert fake.records == []
    assert _watches() == []
    assert events_of(OrchestratorEventType.TICKET_HELD) == []


@pytest.mark.parametrize(
    ("pr_view", "message"),
    [
        (
            json.dumps({"state": "MERGED", "url": "https://github.com/foo/bar/pull/9"}),
            "already merged; requeue instead",
        ),
        (
            json.dumps({"state": "CLOSED", "url": "https://github.com/foo/bar/pull/9"}),
            "closed unmerged",
        ),
        (
            json.dumps(
                {"state": "OPEN", "url": "https://github.com/other/repo/pull/9"}
            ),
            "is not in client acme's repo foo/bar",
        ),
    ],
)
def test_refuses_pr_that_is_merged_closed_or_cross_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    pr_view: str,
    message: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=pr_view)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert message in result.output
    assert queue_row().status == QueueItemStatus.PENDING
    assert _watches() == []
    assert len(fake.records) == 1


def test_refuses_unverifiable_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    FakeGhCli().install(monkeypatch).reply("pr", "view", returncode=1, stderr="nope")

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "could not verify PR #9" in result.output
    assert queue_row().status == QueueItemStatus.PENDING


def test_refuses_running_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "RUNNING" in result.output
    assert queue_row().status == QueueItemStatus.RUNNING
    assert fake.records == []


def test_collision_with_another_client_refused_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    save_dev_queue(
        DevQueueStore(
            tasks=[make_task()],
            watched_prs=[
                WatchedPr(
                    pr_url="https://github.com/foo/bar/pull/9",
                    repo="foo/bar",
                    pr_number=9,
                    source="cli",
                    client="other",
                )
            ],
        )
    )
    FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "already watched by client other" in result.output
    assert queue_row().status == QueueItemStatus.PENDING
    assert [w.client for w in _watches()] == ["other"]


def test_previous_disposition_is_reported_when_overwriting_a_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition="awaiting_operator",
                blocked_reason="agent_block",
            )
        ]
    )
    FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert (
        "replaced disposition awaiting_operator, blocked_reason agent_block"
        in result.output
    )
    assert queue_row().disposition == DEPENDENCY_HOLD_DISPOSITION
    # A same-status overwrite emits no task.transition; ticket.held is the
    # durable audit record of what it replaced.
    assert events_of(OrchestratorEventType.TASK_TRANSITION) == []
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["previous_disposition"] == "awaiting_operator"
    assert held.payload["previous_blocked_reason"] == "agent_block"


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_until_pr_merged_must_be_a_positive_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])

    result = invoke_dev_queue("hold", ACME_TICKET, "--until-pr-merged", bad, "-c", ACME)

    assert result.exit_code == 2


# --- #3634: cross-repo and ticket-id references ---------------------------

_OR_9 = PrRef(repo="o/r", number=9)
_OR_URL = "https://github.com/o/r/pull/9"


def _norm(text: str) -> str:
    """Normalise whitespace, rejoining a word Click wrapped at its hyphen."""
    return re.sub(r"(?<=\w-) (?=\w)", "", " ".join(text.split()))


@pytest.fixture
def unresolved_repo_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Run outside any worktree; origin is unresolvable. Yields the git dirs."""
    monkeypatch.chdir(tmp_path)
    calls: list[Path] = []

    def _unresolved(git_dir: Path) -> None:
        calls.append(git_dir)

    monkeypatch.setattr("cw.cli.dev_queue.hold._resolve_repo_slug", _unresolved)
    return calls


def _hold_args(*option: str) -> tuple[str, ...]:
    return ("hold", ACME_TICKET, *option, "-c", ACME)


def _dep_row(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"ticket_id": "ACM-2", "pr_url": _OR_URL}
    kwargs.update(overrides)
    return make_task(**kwargs)


def _held_on(ref: PrRef) -> TicketTask:
    return make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition=DEPENDENCY_HOLD_DISPOSITION,
        blocked_reason="dependency_unmerged",
        blocked_on_pr=ref,
    )


def test_help_text_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = invoke_dev_queue("hold", "--help")

    assert result.exit_code == 0
    output = _norm(result.output)
    for expected in (
        "PR this row waits on: a same-repo number (9) or owner/repo#N."
        " Released when it merges. Give this or --until-ticket-merged.",
        "Ticket in this client whose PR this row waits on. Resolved once, now,"
        " to that ticket's recorded PR, which must be open."
        " Give this or --until-pr-merged.",
        "Hold a row until a PR merges.",
        "Name the PR with --until-pr-merged (a same-repo number, or owner/repo#N)"
        " or with --until-ticket-merged (another ticket in this client that"
        " already has a PR). Give exactly one. Refuses a row that already has a"
        " PR or a live session. Stops no session: close a live session first"
        " (`cw spawn close`). A worker park with the same PR is converted to an"
        " operator hold. The serve tick releases the row on merge, and only the"
        " PR's own recorded merged state counts: a COMPLETED dependency ticket"
        " never releases a hold. If the PR closes unmerged the row stays held"
        " and a session.reap_proposed event names it.",
    ):
        assert _norm(expected) in output


@pytest.mark.parametrize(
    "option",
    [(), ("--until-pr-merged", "9", "--until-ticket-merged", "ACM-2")],
)
def test_requires_exactly_one_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    option: tuple[str, ...],
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    before = queue_row().model_dump()
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_hold_args(*option))

    assert result.exit_code == 2
    assert "give exactly one of --until-pr-merged or --until-ticket-merged" in (
        result.output
    )
    assert fake.records == []
    assert queue_row().model_dump() == before


@pytest.mark.parametrize("bad", ["o/r", "0", "-1", "a/b/c#1"])
def test_malformed_ref_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])

    result = invoke_dev_queue(*_hold_args("--until-pr-merged", bad))

    assert result.exit_code == 2
    assert (
        f"{bad!r} is not a PR reference; use a positive number (9)"
        " or owner/repo#N (acme/widgets#9)"
    ) in _norm(result.output)


def test_cross_repo_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = (
        FakeGhCli()
        .install(monkeypatch)
        .reply("pr", "view", stdout=gh_pr_view_json("o/r", 9))
    )

    result = invoke_dev_queue(*_hold_args("--until-pr-merged", "o/r#9"))

    assert result.exit_code == 0, result.output
    [argv] = fake.argvs()
    assert argv[:4] == ["gh", "pr", "view", _OR_URL]
    assert queue_row().blocked_on_pr == _OR_9
    [watch] = _watches()
    assert watch.repo == "o/r"
    assert watch.cross_repo is True
    assert watch.source == "dependency_hold"
    assert "Held ACM-1 in acme dev-queue until PR o/r#9 merges" in result.output
    assert "blocked_on_pr: o/r#9" in result.output
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["pr_repo"] == "o/r"
    assert held.payload["previous_pr_repo"] is None
    assert held.payload["via_ticket"] is None
    assert held.payload["repo"] == "o/r"
    assert held.payload["pr_number"] == 9
    assert all(snapshot == () for snapshot in fake.lock_snapshots)


def test_same_repo_happy_path_has_null_pr_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    assert invoke_dev_queue(*_HOLD_ARGS).exit_code == 0
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["pr_repo"] is None
    assert _watches()[0].cross_repo is False


def test_explicit_own_repo_ref_normalises_to_same_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_hold_args("--until-pr-merged", "FOO/Bar#9"))

    assert result.exit_code == 0, result.output
    [argv] = fake.argvs()
    assert argv[:4] == ["gh", "pr", "view", "9"]
    assert queue_row().blocked_on_pr == _REF_9
    [watch] = _watches()
    assert watch.cross_repo is False
    assert watch.repo == "foo/bar"


@pytest.mark.parametrize(
    ("option", "reply", "message"),
    [
        (
            "o/r#9",
            gh_pr_view_json("x/y", 9),
            "gh resolved PR o/r#9 to x/y#9; a hold is never accepted on a mismatch",
        ),
        (
            "9",
            gh_pr_view_json("o/r", 9),
            "PR #9 is not in client acme's repo foo/bar;"
            " name a PR in another repo as owner/repo#N",
        ),
        (
            "o/r#9",
            gh_pr_view_json("o/r", 9, "MERGED"),
            "PR o/r#9 is already merged; requeue instead (`cw dev-queue requeue`)",
        ),
        (
            "o/r#9",
            gh_pr_view_json("o/r", 9, "CLOSED"),
            "PR o/r#9 is closed unmerged; nothing to wait for",
        ),
        (
            "o/r#9",
            gh_pr_view_json("o/r", 9, "DRAFTY"),
            "could not verify PR o/r#9 (gh returned no usable state);"
            " a hold is never accepted unverified",
        ),
        (
            "o/r#9",
            json.dumps({"state": "OPEN"}),
            "could not verify PR o/r#9 (gh returned no usable state);"
            " a hold is never accepted unverified",
        ),
    ],
)
def test_cross_repo_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    option: str,
    reply: str,
    message: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=reply)

    result = invoke_dev_queue(*_hold_args("--until-pr-merged", option))

    assert result.exit_code != 0
    assert message in _norm(result.output)
    assert queue_row().status == QueueItemStatus.PENDING
    assert _watches() == []
    assert events_of(OrchestratorEventType.TICKET_HELD) == []
    assert len(fake.records) == 1


def test_cross_repo_failing_gh_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    FakeGhCli().install(monkeypatch).reply("pr", "view", returncode=1, stderr="no")

    result = invoke_dev_queue(*_hold_args("--until-pr-merged", "o/r#9"))

    assert result.exit_code != 0
    assert "could not verify PR o/r#9 (gh returned no usable state)" in result.output
    assert _watches() == []


_UNRESOLVED = (
    "could not resolve the github repo for client acme;"
    " a hold needs the client's origin remote"
)


@pytest.mark.parametrize(
    "option", [("--until-pr-merged", "o/r#9"), ("--until-ticket-merged", "ACM-2")]
)
def test_explicit_or_ticket_ref_needs_the_slug_before_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved_repo_slug: list[Path],
    option: tuple[str, str],
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task(), _dep_row()])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_hold_args(*option))

    assert result.exit_code != 0
    assert _UNRESOLVED in _norm(result.output)
    assert fake.records == []
    assert len(unresolved_repo_slug) == 1


def test_bare_repeat_with_unresolvable_slug_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved_repo_slug: list[Path],
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([_held_on(_REF_9)])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert fake.records == []
    assert unresolved_repo_slug == []


def test_bare_number_resolves_slug_after_the_gh_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved_repo_slug: list[Path],
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert _UNRESOLVED in _norm(result.output)
    assert len(fake.records) == 1
    assert len(unresolved_repo_slug) == 1
    assert queue_row().status == QueueItemStatus.PENDING


@pytest.mark.parametrize(
    "option", [("--until-pr-merged", "o/r#9"), ("--until-ticket-merged", "ACM-2")]
)
def test_repeat_of_cross_repo_or_ticket_hold_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    option: tuple[str, str],
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([_held_on(_OR_9), _dep_row()])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_hold_args(*option))

    assert result.exit_code == 0, result.output
    assert "ACM-1 already held until PR o/r#9 merges (unchanged)." in result.output
    assert fake.records == []
    assert events_of(OrchestratorEventType.TICKET_HELD) == []


def test_until_ticket_merged_holds_on_the_dependency_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task(), _dep_row()])
    fake = (
        FakeGhCli()
        .install(monkeypatch)
        .reply("pr", "view", stdout=gh_pr_view_json("o/r", 9))
    )

    result = invoke_dev_queue(*_hold_args("--until-ticket-merged", "ACM-2"))

    assert result.exit_code == 0, result.output
    assert queue_row().blocked_on_pr == _OR_9
    assert (
        "Held ACM-1 in acme dev-queue until PR o/r#9 merges (via ticket ACM-2)"
        in result.output
    )
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["via_ticket"] == "ACM-2"
    assert [w.cross_repo for w in _watches()] == [True]
    assert all(snapshot == () for snapshot in fake.lock_snapshots)


def test_completed_dependency_ticket_never_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    """R5: a COMPLETED dependency is held on its PR; status releases nothing."""
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [make_task(), _dep_row(status=QueueItemStatus.COMPLETED, session_id=None)]
    )
    FakeGhCli().install(monkeypatch).reply(
        "pr", "view", stdout=gh_pr_view_json("o/r", 9)
    )

    assert (
        invoke_dev_queue(*_hold_args("--until-ticket-merged", "ACM-2")).exit_code == 0
    )
    assert release_stale_gated_tasks() == []
    store = load_dev_queue()
    store.watched_prs[0].pr_state = PrState(state="OPEN")
    save_dev_queue(store)
    assert release_stale_gated_tasks() == []
    assert queue_row().status == QueueItemStatus.BLOCKED_ON_USER


def test_until_ticket_merged_reads_adopted_pr_url_and_own_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_slug: None
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    dep = _dep_row(pr_url=None, adopted_pr_url="https://github.com/foo/bar/pull/9")
    seed_queue([make_task(), dep])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_hold_args("--until-ticket-merged", "ACM-2"))

    assert result.exit_code == 0, result.output
    assert queue_row().blocked_on_pr == _REF_9
    assert fake.argvs()[0][:4] == ["gh", "pr", "view", "9"]
    assert [w.cross_repo for w in _watches()] == [False]


@pytest.mark.parametrize(
    ("rows", "dep", "message"),
    [
        (
            [_dep_row(pr_url=None)],
            "ACM-2",
            "ticket ACM-2 in client acme has no PR yet;"
            " hold on the PR number once it exists (--until-pr-merged)",
        ),
        (
            [_dep_row(pr_url="https://example.com/x")],
            "ACM-2",
            "ticket ACM-2 in client acme records PR url https://example.com/x,"
            " which is not a github pull request url;"
            " hold on the PR with --until-pr-merged",
        ),
        (
            [],
            "ACM-9",
            "No dev-queue task found for ticket 'ACM-9' in client 'acme'.",
        ),
        (
            [],
            "ACM-1",
            "ticket ACM-1 cannot be held on its own PR;"
            " name a different ticket with --until-ticket-merged",
        ),
        (
            [_dep_row()],
            "ACM-2",
            "PR o/r#9 is already merged; requeue instead (`cw dev-queue requeue`)",
        ),
    ],
)
def test_until_ticket_merged_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    rows: list[TicketTask],
    dep: str,
    message: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task(), *rows])
    FakeGhCli().install(monkeypatch).reply(
        "pr", "view", stdout=gh_pr_view_json("o/r", 9, "MERGED")
    )

    result = invoke_dev_queue(*_hold_args("--until-ticket-merged", dep))

    assert result.exit_code != 0
    assert message in _norm(result.output)
    assert queue_row().status == QueueItemStatus.PENDING
    assert _watches() == []
