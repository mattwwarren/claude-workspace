"""Tests for cw.codex_finalize._post: the PR-link ticket comment (#3062 R11).

The comment is enqueued to the outbox through the shared
``cw.codex_stage_post`` helpers (RFC 0019 O3); these tests pin what FINALIZE
adds: the ``codex-pr-link`` entry, the text, the free-text and already-posted
skips, that nothing reads ``gh``, and that a fault never escapes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_finalize._post import (
    SKIP_ALREADY_POSTED,
    SKIP_FREE_TEXT,
    compose_pr_link_comment,
    post_pr_link_comment,
)
from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.tracker import TRACKER_LINEAR
from tests._backend_mcp_helpers import codex_tracker_client, stage_mcp_config
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.outbox_builders import entries_for
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_PR_URL = "https://github.com/o/r/pull/9"
_EVENT_TYPES = [
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_POSTED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED,
]


def _events() -> list[dict[str, Any]]:
    return [
        {"type": event.type.value, **event.payload}
        for event in read_events(event_types=_EVENT_TYPES)
    ]


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _post(
    client: ClientConfig,
    worktree: Path,
    *,
    plan_source: str = "generated",
) -> None:
    post_pr_link_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-s",
        pr_url=_PR_URL,
        plan_source=plan_source,
    )


def _linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    client = codex_tracker_client(tmp_path, {"codex": "linear"})
    _write_tracker_config(client.workspace_path, TRACKER_LINEAR)
    stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
    return client


class TestComposeText:
    def test_is_the_pr_link(self) -> None:
        assert compose_pr_link_comment(_PR_URL) == f"PR: {_PR_URL}\n"

    def test_names_the_parent_only_when_cw_knows_it(self) -> None:
        text = compose_pr_link_comment(_PR_URL, parent_pr=5)
        assert text.startswith(f"PR: {_PR_URL}\n")
        assert "Created as draft — stacked behind PR #5" in text
        assert "stacked" not in compose_pr_link_comment(_PR_URL)


class TestEnqueue:
    def test_pr_link_enqueues_without_a_gh_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = FakeGhCli().install(monkeypatch)
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path))
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == "codex-pr-link"
        assert produced.entry.marker.startswith("pr-link-")
        assert _PR_URL in "".join(produced.parts)
        assert gh.calls == []
        assert _events() == []

    def test_duplicate_is_already_posted(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path))
        _post(client, _worktree(tmp_path))
        (produced,) = entries_for(client.name, ticket=_TICKET)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == SKIP_ALREADY_POSTED == "already_posted"
        assert event["entry_id"] == produced.entry.id
        assert event["kind"] == "pr_link"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None

    def test_a_different_pr_is_a_distinct_entry(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path))
        post_pr_link_comment(
            task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
            client=client,
            worktree=_worktree(tmp_path),
            sid="sess-s",
            pr_url="https://github.com/o/r/pull/10",
            plan_source="generated",
        )
        assert len(entries_for(client.name, ticket=_TICKET)) == 2
        assert _events() == []

    def test_free_text_ticket_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gh = FakeGhCli().install(monkeypatch)
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path), plan_source="free_text")
        assert entries_for(client.name) == []
        assert gh.calls == []
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == SKIP_FREE_TEXT == "free_text_ticket"
        assert event["kind"] == "pr_link"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None

    def test_linear_client_enqueues_pr_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        _post(client, _worktree(tmp_path))
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == "codex-pr-link"
        assert _PR_URL in "".join(produced.parts)
        assert _events() == []

    def test_a_failed_enqueue_is_a_skipped_event(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, _worktree(tmp_path))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == "outbox_enqueue_failed"
        assert event["kind"] == "pr_link"
        assert entries_for(client.name) == []


class TestNeverHoldsTheRow:
    def test_an_enqueue_fault_never_escapes(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            assert _post(client, _worktree(tmp_path)) is None

    def test_a_planner_fault_never_escapes_and_leaves_no_event(
        self, tmp_path: Path
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.codex_stage_post.plan_prose_parts", side_effect=OSError):
            assert _post(client, _worktree(tmp_path)) is None
        assert _events() == []
        assert entries_for(client.name) == []

    def test_the_events_are_json_serialisable(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path))
        _post(client, _worktree(tmp_path))
        json.dumps(_events())
