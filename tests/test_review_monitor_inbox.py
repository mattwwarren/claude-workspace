"""Characterization tests for the review-monitor inbox and action queue (#2499).

Covers ``cmd_consume_pending`` (the ship-it file-drop inbox),
``_file_is_stale``, ``_desktop_queue_filename`` and ``cmd_enqueue_action``
(moving to ``review_monitor_lib/inbox.py``). Every test runs under the
``review_monitor_state_dir`` fixture, which points ``PENDING_INBOX_DIR`` and
``DESKTOP_QUEUE_DIR`` (both bound at import) under ``tmp_path``; the real
``/tmp/review-monitor/pending`` is only ever read as a value, never touched.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers

_STALE_SECONDS = 25 * 3600


def _drop(name: str, content: str, *, stale: bool = False) -> Path:
    inbox = helpers.get("PENDING_INBOX_DIR")
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(content)
    if stale:
        old = time.time() - _STALE_SECONDS
        os.utime(path, (old, old))
    return path


def _payload(**overrides: object) -> str:
    data: dict[str, object] = {
        "pr": "42",
        "repo": helpers.REPO,
        "slack_channel": "C0123",
        "slack_ts": "1700000000.1",
        "sha": "abc123",
    }
    data.update(overrides)
    return json.dumps({k: v for k, v in data.items() if v is not None})


def test_pending_inbox_is_the_cross_process_tmp_literal() -> None:
    real = helpers.import_time_value("PENDING_INBOX_DIR")

    assert real == Path("/tmp/review-monitor/pending")
    assert str(real) == "/tmp/review-monitor/pending"


def test_isolation_moves_inbox_and_queue_off_real_paths(
    review_monitor_state_dir: Path, tmp_path: Path
) -> None:
    for name in ("PENDING_INBOX_DIR", "DESKTOP_QUEUE_DIR"):
        assert helpers.get(name).is_relative_to(tmp_path)
        assert helpers.get(name) != helpers.import_time_value(name)


def test_consume_pending_without_inbox_dir_is_empty(
    review_monitor_state_dir: Path,
) -> None:
    assert helpers.get("cmd_consume_pending")() == {
        "consumed": [],
        "skipped": [],
        "purged": [],
    }


def test_consume_pending_registers_and_removes_valid_drops(
    review_monitor_state_dir: Path,
) -> None:
    drop = _drop("a.json", _payload(repo_path="/canon/widgets"))
    note = _drop("README.txt", "not a drop")

    summary = helpers.get("cmd_consume_pending")()

    assert summary == {"consumed": [helpers.KEY], "skipped": [], "purged": []}
    assert not drop.exists()
    assert note.exists()
    pr = helpers.stored_pr()
    assert (pr.role, pr.last_seen_sha, pr.repo_path) == (
        "author",
        "abc123",
        "/canon/widgets",
    )
    assert (pr.slack_channel, pr.slack_ts) == ("C0123", "1700000000.1")


@pytest.mark.parametrize(
    ("content", "stale", "bucket"),
    [
        ("{not json", False, "skipped"),
        ("{not json", True, "purged"),
        (_payload(slack_ts=None), False, "skipped"),
        (_payload(pr="forty-two"), True, "purged"),
    ],
    ids=["unreadable-fresh", "unreadable-stale", "invalid-fresh", "invalid-stale"],
)
def test_consume_pending_keeps_bad_drops_until_stale(
    review_monitor_state_dir: Path,
    caplog: pytest.LogCaptureFixture,
    content: str,
    stale: bool,
    bucket: str,
) -> None:
    drop = _drop("bad.json", content, stale=stale)

    with caplog.at_level("WARNING"):
        summary = helpers.get("cmd_consume_pending")()

    assert summary[bucket] == ["bad.json"]
    assert summary["consumed"] == []
    assert drop.exists() is (bucket == "skipped")
    assert any("cmd_consume_pending" in r.message for r in caplog.records)
    assert helpers.get("load_state")(helpers.REPO).monitored == {}


def test_file_is_stale_treats_unstatable_path_as_stale(tmp_path: Path) -> None:
    now = datetime.now(UTC)

    assert helpers.get("_file_is_stale")(tmp_path / "vanished.json", now) is True


@pytest.mark.parametrize(
    ("action", "repo", "pr_number", "filename"),
    [
        ("nudge", "acme/widgets", 42, "nudge-acme_widgets-42.json"),
        ("dm_escalation", "zeta/app", 7, "dm_escalation-zeta_app-7.json"),
        ("channel_bump", "acme/widgets", 42, "channel_bump.json"),
        ("cron_failure", None, None, "cron_failure.json"),
    ],
)
def test_desktop_queue_filename_is_deterministic(
    action: str, repo: str | None, pr_number: int | None, filename: str
) -> None:
    assert helpers.get("_desktop_queue_filename")(action, repo, pr_number) == filename


@pytest.mark.parametrize(
    ("action", "repo", "pr_number", "message"),
    [
        ("tweet", None, None, "unknown action type 'tweet'"),
        ("nudge", None, 42, "action 'nudge' requires both --repo and --pr"),
        ("dm_escalation", "acme/widgets", None, "requires both --repo and --pr"),
    ],
)
def test_enqueue_action_rejects_bad_requests(
    review_monitor_state_dir: Path,
    action: str,
    repo: str | None,
    pr_number: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        helpers.get("cmd_enqueue_action")(action, {}, repo=repo, pr_number=pr_number)

    assert not helpers.get("DESKTOP_QUEUE_DIR").exists()


def _queued(name: str) -> dict[str, Any]:
    queued: dict[str, Any] = json.loads(
        (helpers.get("DESKTOP_QUEUE_DIR") / name).read_text()
    )
    return queued


def test_enqueue_action_writes_then_refreshes_preserving_queued_at(
    review_monitor_state_dir: Path,
) -> None:
    enqueue = helpers.get("cmd_enqueue_action")
    first_at = "2026-10-05T16:00:00+00:00"

    with freeze_time(first_at):
        first = enqueue("nudge", {"text": "hi"}, repo=helpers.REPO, pr_number=42)
    with freeze_time("2026-10-05T17:00:00+00:00"):
        second = enqueue("nudge", {"text": "again"}, repo=helpers.REPO, pr_number=42)

    path = helpers.get("DESKTOP_QUEUE_DIR") / "nudge-acme_widgets-42.json"
    assert first == {"enqueued": str(path), "action": "nudge", "queued_at": first_at}
    assert second["queued_at"] == first_at
    assert _queued(path.name) == {
        "action": "nudge",
        "repo": helpers.REPO,
        "pr_number": 42,
        "queued_at": first_at,
        "refreshed_at": "2026-10-05T17:00:00+00:00",
        "sent_at": None,
        "payload": {"text": "again"},
    }
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@freeze_time("2026-10-05T16:00:00+00:00")
def test_enqueue_action_over_corrupt_file_restarts_queued_at(
    review_monitor_state_dir: Path,
) -> None:
    queue = helpers.get("DESKTOP_QUEUE_DIR")
    queue.mkdir(parents=True)
    (queue / "channel_bump.json").write_text("{torn")

    result = helpers.get("cmd_enqueue_action")("channel_bump", {"prs": [1]})

    assert result["queued_at"] == "2026-10-05T16:00:00+00:00"
    assert _queued("channel_bump.json")["payload"] == {"prs": [1]}
