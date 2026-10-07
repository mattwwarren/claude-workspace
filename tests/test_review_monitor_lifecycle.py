"""Characterization tests for review-monitor PR lifecycle commands (#2499).

Covers ``_normalize_thread_ids``, ``cmd_register``'s create and re-anchor
paths, ``ack-delta``, ``set-status``, ``confirm-thread`` and the Slack cursor
commands (moving to ``review_monitor_lib/lifecycle.py``). The canonical-path
override and the register/drop/complete CLI result lines are covered in
``tests/test_review_monitor.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers


def _status(**fields: object) -> Any:
    return helpers.get("ThreadStatus")(**{"file": "a.py", "line": 3, **fields})


@pytest.mark.parametrize(
    ("raw", "ids"),
    [
        (None, []),
        ([], []),
        (["t1", "t2"], ["t1", "t2"]),
        (["t1,t2", " t3 ,", ","], ["t1", "t2", "t3"]),
    ],
)
def test_normalize_thread_ids_splits_comma_joined_values(
    raw: list[str] | None, ids: list[str]
) -> None:
    assert helpers.get("_normalize_thread_ids")(raw) == ids


def test_register_creates_pr_with_threads_details_and_slack(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.patch(monkeypatch, "CANONICAL_REPO_PATHS", {helpers.REPO: "/canon"})

    result = helpers.get("cmd_register")(
        42,
        "reviewer",
        helpers.REPO,
        "/tmp/agent-worktree",
        "sha1",
        review_id="R1",
        threads=["t1,t2"],
        thread_details=[{"id": "t1", "file": "a.py", "line": 3}],
        slack_channel="C1",
        slack_ts="1.0",
    )

    assert result == {
        "registered": True,
        "key": helpers.KEY,
        "sha": "sha1",
        "updated": False,
    }
    pr = helpers.stored_pr()
    assert (pr.role, pr.repo_path, pr.our_review_id) == ("reviewer", "/canon", "R1")
    assert pr.our_threads == ["t1", "t2"]
    assert pr.thread_status == {"t1": _status()}
    assert (pr.slack_channel, pr.slack_ts) == ("C1", "1.0")


def test_register_update_reanchors_merges_and_heals(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(
        helpers.make_pr(
            repo_path="/stale/worktree",
            delta_base_sha="old-base",
            our_threads=["t1"],
            thread_status={"t1": _status(resolved=True)},
            slack_channel="C1",
            slack_ts="1.0",
            nudge_count=3,
        )
    )

    result = helpers.get("cmd_register")(
        42,
        "author",
        helpers.REPO,
        "/canon/widgets",
        "sha2",
        threads=["t1", "t2"],
        thread_details=[
            {"id": "t1", "file": "other.py", "line": 9},
            {"id": "t2", "file": "b.py", "line": None},
        ],
        slack_ts="2.0",
    )

    assert result["updated"] is True
    pr = helpers.stored_pr()
    assert pr.repo_path == "/canon/widgets"
    assert (pr.last_seen_sha, pr.delta_base_sha) == ("sha2", "sha2")
    assert pr.our_threads == ["t1", "t2"]
    assert pr.thread_status["t1"] == _status(resolved=True)
    assert pr.thread_status["t2"] == _status(file="b.py", line=None)
    assert (pr.slack_channel, pr.slack_ts) == ("C1", "2.0")
    assert pr.nudge_count == 3


def test_register_update_keeps_slack_fields_when_omitted(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(helpers.make_pr(slack_channel="C1", slack_ts="1.0"))

    helpers.get("cmd_register")(42, "author", helpers.REPO, "/r", "sha2")

    pr = helpers.stored_pr()
    assert (pr.slack_channel, pr.slack_ts) == ("C1", "1.0")


def test_ack_delta_unknown_pr_reports_error(review_monitor_state_dir: Path) -> None:
    assert helpers.get("cmd_ack_delta")(42, helpers.REPO, "sha9") == {
        "error": f"PR {helpers.KEY} not found in monitored"
    }


@freeze_time("2026-10-05T16:00:00+00:00")
def test_ack_delta_advances_only_the_delta_baseline(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(helpers.make_pr(last_seen_sha="head", delta_base_sha="base"))

    result = helpers.get("cmd_ack_delta")(42, helpers.REPO, "head")

    assert result == {"pr_number": 42, "delta_base_sha": "head", "acked": True}
    pr = helpers.stored_pr()
    assert (pr.last_seen_sha, pr.delta_base_sha) == ("head", "head")
    assert pr.last_checked_at == "2026-10-05T16:00:00+00:00"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cmd_set_status", ("approved",)),
        ("cmd_confirm_thread", ("t1",)),
        ("cmd_update_slack_cursor", ("5.0",)),
    ],
)
def test_void_lifecycle_commands_warn_on_unknown_pr(
    review_monitor_state_dir: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
) -> None:
    with caplog.at_level("WARNING"):
        assert helpers.get(command)(42, helpers.REPO, *args) is None

    assert any(
        command in r.message and helpers.KEY in r.message for r in caplog.records
    )
    assert capsys.readouterr().out == ""


def test_set_status_persists(review_monitor_state_dir: Path) -> None:
    helpers.seed_prs(helpers.make_pr())

    helpers.get("cmd_set_status")(42, helpers.REPO, "approved")

    assert helpers.stored_pr().status == "approved"


def test_confirm_thread_unknown_thread_warns_and_changes_nothing(
    review_monitor_state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    helpers.seed_prs(helpers.make_pr(thread_status={"t1": _status()}))

    with caplog.at_level("WARNING"):
        helpers.get("cmd_confirm_thread")(42, helpers.REPO, "t9")

    assert any("thread 't9' not tracked" in r.message for r in caplog.records)
    assert helpers.stored_pr().thread_status["t1"].code_changed is False


@pytest.mark.parametrize(
    ("others", "status", "unaddressed"),
    [({}, "ready_to_approve", []), ({"t2": _status()}, "watching", ["t2"])],
    ids=["last-thread", "one-left"],
)
def test_confirm_thread_marks_code_changed_and_prints_summary(
    review_monitor_state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    others: dict[str, Any],
    status: str,
    unaddressed: list[str],
) -> None:
    helpers.seed_prs(helpers.make_pr(thread_status={"t1": _status(), **others}))

    helpers.get("cmd_confirm_thread")(42, helpers.REPO, "t1")

    assert json.loads(capsys.readouterr().out) == {
        "confirmed": "t1",
        "status": status,
        "all_addressed": not unaddressed,
        "unaddressed": unaddressed,
    }
    pr = helpers.stored_pr()
    assert pr.thread_status["t1"].code_changed is True
    assert pr.status == status


def test_slack_cursor_round_trip(review_monitor_state_dir: Path) -> None:
    cursor = helpers.get("cmd_slack_thread_cursor")
    assert cursor(42, helpers.REPO) == {
        "error": f"PR {helpers.KEY} not found in monitored"
    }
    helpers.seed_prs(helpers.make_pr(slack_channel="C1", slack_ts="1.0"))

    helpers.get("cmd_update_slack_cursor")(42, helpers.REPO, "1.5")

    assert cursor(42, helpers.REPO) == {
        "slack_channel": "C1",
        "slack_ts": "1.0",
        "slack_last_seen_ts": "1.5",
    }
