"""Characterization tests for review-monitor thread tracking (#2499).

Covers deferral detection, login extraction, author-thread discovery, thread
status updates, lifecycle status transitions, ``_refresh_threads`` and the
merged-PR deferred-thread follow-up collector (moving to
``review_monitor_lib/threads.py``). ``gh review view --json`` payloads are
hand-authored from the fields the code reads (``id``, ``path``, ``line``,
``isResolved``, ``comments[].author``/``body``/``url``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests import _review_monitor_helpers as helpers

_ME = "matt-w"


def _comment(author: object, body: str = "looks off", url: str = "") -> dict[str, Any]:
    return {"author": author, "body": body, "url": url}


def _gh_thread(tid: str, *comments: dict[str, Any], **fields: object) -> dict[str, Any]:
    return {"id": tid, "path": "a.py", "line": 10, "comments": list(comments), **fields}


def _review_view(*threads: dict[str, Any]) -> str:
    return json.dumps({"threads": list(threads)})


def _status(**fields: object) -> Any:
    return helpers.get("ThreadStatus")(**{"file": "a.py", "line": 10, **fields})


@pytest.mark.parametrize(
    "text",
    [
        "Will do in a follow-up",
        "followup ticket filed",
        "That belongs in a separate PR",
        "out of scope here",
        "next sprint",
        "will address later",
        "tracking in #12",
    ],
)
def test_is_deferral_matches_each_pattern(text: str) -> None:
    assert helpers.get("is_deferral")(text) is True


def test_is_deferral_ignores_plain_replies() -> None:
    assert helpers.get("is_deferral")("Fixed in abc123, thanks!") is False


@pytest.mark.parametrize(
    ("comment", "login"),
    [
        ({"author": "bob"}, "bob"),
        ({"author": {"login": "carol"}}, "carol"),
        ({"author": {}}, ""),
        ({"author": None}, ""),
        ({}, ""),
    ],
)
def test_extract_login_handles_str_dict_and_other(
    comment: dict[str, Any], login: str
) -> None:
    assert helpers.get("_extract_login")(comment) == login


def test_discover_author_threads_adds_only_new_threads_by_others() -> None:
    pr = helpers.make_pr(
        our_threads=["known"], thread_status={"seeded": _status(line=1)}
    )
    threads = [
        _gh_thread("", _comment("bob")),
        _gh_thread("empty"),
        _gh_thread("mine", _comment(_ME)),
        _gh_thread("known", _comment("bob")),
        _gh_thread("new", _comment({"login": "bob"}), path="b.py", line=4),
        _gh_thread("seeded", _comment("bob")),
    ]

    helpers.get("_discover_author_threads")(pr, threads, _ME)

    assert pr.our_threads == ["known", "new", "seeded"]
    assert pr.thread_status["new"] == _status(file="b.py", line=4)
    assert pr.thread_status["seeded"].line == 1


@pytest.mark.parametrize(
    ("role", "replies", "replied", "deferred"),
    [
        ("reviewer", [], False, False),
        ("reviewer", [_comment(_ME)], False, False),
        ("reviewer", [_comment("author")], True, False),
        ("author", [_comment("bob")], False, False),
        ("author", [_comment(_ME, "fixed")], True, False),
        ("author", [_comment(_ME, "follow-up ticket")], True, True),
    ],
)
def test_update_thread_status_per_role(
    role: str, replies: list[dict[str, Any]], replied: bool, deferred: bool
) -> None:
    ts = _status(replied=True, deferred=True)
    thread = _gh_thread("t", _comment("opener"), *replies, isResolved=True)

    helpers.get("_update_thread_status")(ts, thread, role, _ME)

    assert ts.resolved is True
    assert ts.replied is replied
    assert ts.deferred is deferred


@pytest.mark.parametrize(
    ("status", "addressed", "changed", "is_draft", "expected"),
    [
        ("watching", True, False, False, "ready_to_approve"),
        ("watching", False, False, False, "watching"),
        ("ready_to_approve", True, True, False, "watching"),
        ("approved", True, True, False, "watching"),
        ("watching", True, True, False, "watching"),
        ("approved", True, False, False, "approved"),
        ("ready_to_approve", True, False, True, "watching"),
        ("approved", True, False, True, "watching"),
        ("watching", True, False, True, "watching"),
    ],
)
def test_apply_status_transitions_table(
    status: str, addressed: bool, changed: bool, is_draft: bool, expected: str
) -> None:
    pr = helpers.make_pr(
        status=status, thread_status={"t": _status(resolved=addressed)}
    )

    helpers.get("_apply_status_transitions")(pr, changed, is_draft=is_draft)

    assert pr.status == expected


def test_refresh_threads_updates_tracked_and_discovers_for_author(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _review_view(
        _gh_thread("t1", _comment("bob"), _comment(_ME, "done")),
        _gh_thread("t2", _comment("carol"), isResolved=True, path="c.py", line=2),
    )
    fake = helpers.FakeCommands().add(("gh", "review", "view"), payload)
    fake.install(monkeypatch)
    pr = helpers.make_pr(our_threads=["t1", "gone"])

    updates = helpers.get("_refresh_threads")(pr, 42, _ME)

    assert pr.our_threads == ["t1", "gone", "t2"]
    assert set(updates) == {"t1", "t2"}
    assert updates["t1"]["replied"] is True
    assert updates["t2"] == _status(file="c.py", line=2, resolved=True).to_dict()
    assert fake.calls == [(("gh", "review", "view", "42", "--json"), helpers.REPO)]


def test_refresh_threads_reviewer_skips_discovery_and_tolerates_bad_json(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(("gh", "review", "view"), "not json").install(
        monkeypatch
    )
    pr = helpers.make_pr(role="reviewer", our_threads=["t1"])

    assert helpers.get("_refresh_threads")(pr, 42, _ME) == {}
    assert pr.our_threads == ["t1"]


def test_refresh_threads_seeds_status_for_tracked_thread_without_one(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _review_view(_gh_thread("t1", _comment(_ME), _comment("author")))
    helpers.FakeCommands().add(("gh", "review", "view"), payload).install(monkeypatch)
    pr = helpers.make_pr(role="reviewer", our_threads=["t1"])

    updates = helpers.get("_refresh_threads")(pr, 42, _ME)

    assert updates["t1"]["replied"] is True
    assert pr.thread_status["t1"].file == "a.py"


def test_deferred_followup_without_deferred_threads_makes_no_call(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = helpers.FakeCommands().install(monkeypatch)
    pr = helpers.make_pr(thread_status={"t": _status(replied=True)})

    assert helpers.get("_collect_deferred_threads_for_followup")(pr, 42) == []
    assert fake.calls == []


def test_deferred_followup_unparseable_review_is_empty(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(("gh", "review", "view"), "").install(monkeypatch)
    pr = helpers.make_pr(thread_status={"t": _status(deferred=True)})

    assert helpers.get("_collect_deferred_threads_for_followup")(pr, 42) == []


def test_deferred_followup_reports_each_deferred_thread(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _review_view(
        _gh_thread(
            "d1",
            _comment({"login": "bob"}, "rename this", url="https://gh/c1"),
            _comment("bob", "ping"),
            _comment(_ME, "follow-up: tracking in #9"),
        ),
        _gh_thread("d2", path="x.py"),
        _gh_thread("d3", _comment("carol", "nit"), _comment(_ME, "sure")),
        _gh_thread("other", _comment("dave", "unrelated")),
    )
    fake = helpers.FakeCommands()
    fake.add(("gh", "review", "view"), payload).add(("gh", "api", "user"), _ME)
    fake.install(monkeypatch)
    pr = helpers.make_pr(
        thread_status={
            "d1": _status(deferred=True),
            "d2": _status(deferred=True),
            "d3": _status(deferred=True),
            "other": _status(),
        }
    )

    result = helpers.get("_collect_deferred_threads_for_followup")(pr, 42)

    assert result == [
        {
            "thread_id": "d1",
            "file": "a.py",
            "line": 10,
            "reviewer": "bob",
            "reviewer_comment": "rename this",
            "deferral_reply": "follow-up: tracking in #9",
            "url": "https://gh/c1",
        },
        {
            "thread_id": "d3",
            "file": "a.py",
            "line": 10,
            "reviewer": "carol",
            "reviewer_comment": "nit",
            "deferral_reply": "",
            "url": "",
        },
    ]
