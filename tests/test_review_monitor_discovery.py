"""Characterization tests for review-monitor PR discovery and recovery (#2499).

Covers ``cmd_discover`` (open author PRs) and ``cmd_recover_reviews`` with its
helpers ``_search_reviewed_prs``, ``_our_reviews``, ``_our_unresolved_threads``
and ``_recover_one_review`` (moving to ``review_monitor_lib/discovery.py``).
``gh search prs`` / reviews / ``gh review view`` payloads are hand-authored
from the fields the code reads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers

if TYPE_CHECKING:
    from pathlib import Path

_ME = "matt-w"
_NOW = "2026-10-08T12:00:00+00:00"
_SEARCH = ("gh", "search", "prs")


def _head_sha_route(number: int) -> tuple[str, ...]:
    return ("gh", "pr", "view", str(number), "--json", "headRefOid")


def _reviews_route(number: int) -> tuple[str, ...]:
    return ("gh", "api", f"repos/acme/widgets/pulls/{number}/reviews")


def _threads_route(number: int) -> tuple[str, ...]:
    return ("gh", "review", "view", str(number))


def _thread(tid: str, opener: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "id": tid,
        "path": "a.py",
        "line": 4,
        "isResolved": resolved,
        "comments": [{"author": {"login": opener}, "body": "x"}],
    }


@freeze_time(_NOW)
def test_discover_registers_new_author_prs_and_skips_monitored(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [{"number": 1, "title": "a"}, {"number": 2}, {"number": "3"}]
    fake = helpers.FakeCommands()
    fake.add(_SEARCH, json.dumps(found)).add(_head_sha_route(1), " sha-one \n")
    fake.install(monkeypatch)
    helpers.seed_prs(helpers.make_pr(pr_number=2))

    result = helpers.get("cmd_discover")(helpers.REPO, 3, "/canon/widgets")

    assert result == {"registered": [1], "skipped": [2], "repo": helpers.REPO}
    pr = helpers.stored_pr("acme/widgets#1")
    assert (pr.role, pr.last_seen_sha, pr.repo_path) == (
        "author",
        "sha-one",
        "/canon/widgets",
    )
    assert fake.calls[0] == (
        (
            *_SEARCH,
            "--repo",
            helpers.REPO,
            "--author",
            "@me",
            "--state",
            "open",
            "--created",
            ">=2026-10-05",
            "--json",
            "number,title",
            "--limit",
            "100",
        ),
        None,
    )
    assert fake.calls[1] == (
        (*_head_sha_route(1), "--jq", ".headRefOid"),
        helpers.REPO,
    )


def test_discover_with_unparseable_search_registers_nothing(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(_SEARCH, "").install(monkeypatch)

    assert helpers.get("cmd_discover")(helpers.REPO, 7, "/r") == {
        "registered": [],
        "skipped": [],
        "repo": helpers.REPO,
    }


@freeze_time(_NOW)
def test_search_reviewed_prs_excludes_own_and_malformed(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [
        {"number": 1, "author": {"login": "bob"}},
        {"number": 2, "author": {"login": _ME}},
        {"number": "3", "author": {"login": "bob"}},
        {"number": 4, "author": None},
    ]
    fake = helpers.FakeCommands().add(_SEARCH, json.dumps(found)).install(monkeypatch)

    assert helpers.get("_search_reviewed_prs")(helpers.REPO, 2, _ME) == [1, 4]
    argv = fake.argvs()[0]
    assert argv[argv.index("--reviewed-by") + 1] == "@me"
    assert argv[argv.index("--updated") + 1] == ">=2026-10-06"


@pytest.mark.parametrize(
    ("helper", "route"),
    [
        ("_search_reviewed_prs", _SEARCH),
        ("_our_reviews", _reviews_route(42)),
        ("_our_unresolved_threads", _threads_route(42)),
    ],
)
def test_discovery_queries_tolerate_unparseable_output(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    route: tuple[str, ...],
) -> None:
    helpers.FakeCommands().add(route, "oops").install(monkeypatch)
    args = (
        (helpers.REPO, 7, _ME)
        if helper == "_search_reviewed_prs"
        else (42, helpers.REPO, _ME)
    )

    assert helpers.get(helper)(*args) == []


def test_our_reviews_filters_by_login_with_jq(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = [{"id": 1, "state": "COMMENTED"}]
    fake = helpers.FakeCommands().add(_reviews_route(42), json.dumps(reviews))
    fake.install(monkeypatch)

    assert helpers.get("_our_reviews")(42, helpers.REPO, _ME) == reviews
    assert fake.calls == [
        (
            (*_reviews_route(42), "--jq", '[.[] | select(.user.login=="matt-w")]'),
            None,
        )
    ]


def test_our_unresolved_threads_keeps_only_our_open_threads(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = [
        _thread("mine", _ME),
        _thread("resolved", _ME, resolved=True),
        _thread("theirs", "bob"),
        {"id": "", "comments": [{"author": _ME}]},
        {"id": "empty", "comments": []},
    ]
    fake = helpers.FakeCommands().add(
        _threads_route(42), json.dumps({"threads": threads})
    )
    fake.install(monkeypatch)

    result = helpers.get("_our_unresolved_threads")(42, helpers.REPO, _ME)

    assert [t["id"] for t in result] == ["mine"]
    assert fake.calls[0][1] == helpers.REPO


def _recovery_fake(monkeypatch: pytest.MonkeyPatch) -> helpers.FakeCommands:
    """Search hits 1-6; each PR's reviews/threads chosen to land in one bucket."""
    fake = helpers.FakeCommands()
    fake.add(("gh", "api", "user"), _ME)
    fake.add(
        _SEARCH,
        json.dumps([{"number": n, "author": {"login": "bob"}} for n in range(1, 7)]),
    )
    approved = [{"state": "COMMENTED", "commit_id": "a"}, {"state": "APPROVED"}]
    fake.add(_reviews_route(3), json.dumps(approved))
    fake.add(_reviews_route(4), json.dumps([{"state": "COMMENTED", "commit_id": "c4"}]))
    fake.add(_threads_route(4), json.dumps({"threads": [_thread("x", "bob")]}))
    fake.add(
        _reviews_route(5), json.dumps([{"state": "COMMENTED", "commit_id": " c5 "}])
    )
    fake.add(_threads_route(5), json.dumps({"threads": [_thread("t5", _ME)]}))
    fake.add(_reviews_route(6), "[]")
    fake.add(_threads_route(6), json.dumps({"threads": [_thread("t6", _ME)]}))
    fake.add(_head_sha_route(6), "head6\n")
    return fake.install(monkeypatch)


def test_recover_reviews_sorts_every_search_hit_into_one_bucket(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _recovery_fake(monkeypatch)
    helpers.seed_prs(
        helpers.make_pr(pr_number=1),
        completed={"acme/widgets#2": {"reason": "approved"}},
    )

    result = helpers.get("cmd_recover_reviews")(helpers.REPO, 7, "/canon/widgets")

    assert result == {
        "recovered": [5, 6],
        "skipped_monitored": [1],
        "skipped_completed": [2],
        "skipped_already_approved": [3],
        "skipped_no_open_threads": [4],
        "repo": helpers.REPO,
    }
    five = helpers.stored_pr("acme/widgets#5")
    assert (five.role, five.last_seen_sha, five.our_threads) == (
        "reviewer",
        "c5",
        ["t5"],
    )
    assert five.thread_status["t5"].line == 4
    assert helpers.stored_pr("acme/widgets#6").last_seen_sha == "head6"
    assert _threads_route(3) not in fake.argvs()
