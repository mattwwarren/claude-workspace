"""Tests for cw.codex_finalize._post: the PR-link ticket comment (#3062 R11).

The post goes through the shared ``cw.codex_stage_post`` helpers; these tests
pin what FINALIZE adds: the ``kind="pr_link"`` events, the text, the free-text
and already-posted skips, and that a post fault never escapes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_finalize._post import (
    PR_LINK_KIND,
    SKIP_ALREADY_POSTED,
    SKIP_FREE_TEXT,
    compose_pr_link_comment,
    post_pr_link_comment,
)
from cw.codex_runner import CodexRunResult
from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR
from cw.tracker_io import TrackerIoContext, plan_turn_parts
from cw.tracker_post import PostedPart, PostReason, PostResult, PostStatus
from tests._backend_mcp_helpers import codex_tracker_client, stage_mcp_config
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import ScriptedCodexRunner, any_call, tracker_io_turn_result

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_PR_URL = "https://github.com/o/r/pull/9"
_ISSUE_URL = "https://github.com/o/r/issues/7#issuecomment-41"
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
    runner: ScriptedCodexRunner | None = None,
    plan_source: str = "generated",
) -> None:
    post_pr_link_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-s",
        runner=runner or ScriptedCodexRunner(),
        pr_url=_PR_URL,
        plan_source=plan_source,
    )


def _posted_ok() -> PostResult:
    return PostResult(
        PostStatus.POSTED, None, (PostedPart(index=1, url=_ISSUE_URL),), 1
    )


def _no_comments() -> Any:
    return patch("cw.codex_finalize._post.fetch_issue_comments", return_value=[])


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


class TestGitHub:
    def test_posts_the_link_through_the_shared_poster(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with (
            _no_comments(),
            patch("cw.codex_stage_post.post_parts", return_value=_posted_ok()) as post,
        ):
            _post(client, _worktree(tmp_path))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == PR_LINK_KIND == "pr_link"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None
        assert event["tracker"] == TRACKER_GITHUB_ISSUES
        assert event["posted"] == [{"index": 1, "comment_id": "41", "url": _ISSUE_URL}]
        assert post.call_args.args[0] == _TICKET
        assert any(_PR_URL in part for part in map(str, post.call_args.args[1]))

    def test_a_refused_post_is_a_skipped_event(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        refused = PostResult(PostStatus.REFUSED, PostReason.POST_FAILED, (), 1)
        with (
            _no_comments(),
            patch("cw.codex_stage_post.post_parts", return_value=refused),
        ):
            _post(client, _worktree(tmp_path))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["kind"] == "pr_link"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None

    def test_a_partial_post_is_an_unverified_event(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        partial = PostResult(
            PostStatus.PARTIAL,
            PostReason.POST_FAILED,
            (PostedPart(index=1, url=_ISSUE_URL),),
            1,
        )
        with (
            _no_comments(),
            patch("cw.codex_stage_post.post_parts", return_value=partial),
        ):
            _post(client, _worktree(tmp_path))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_unverified"
        assert event["kind"] == "pr_link"

    def test_a_comment_already_carrying_the_url_is_not_posted_again(
        self, tmp_path: Path
    ) -> None:
        client = codex_tracker_client(tmp_path)
        seen = [{"body": f"PR: {_PR_URL}\n"}]
        with (
            patch("cw.codex_finalize._post.fetch_issue_comments", return_value=seen),
            patch("cw.codex_stage_post.post_parts") as post,
        ):
            _post(client, _worktree(tmp_path))
        post.assert_not_called()
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == SKIP_ALREADY_POSTED == "already_posted"
        assert event["kind"] == "pr_link"

    @pytest.mark.parametrize("comments", [None, [], [{"body": "unrelated"}, {}]])
    def test_an_unreadable_or_unrelated_thread_still_posts(
        self, tmp_path: Path, comments: list[dict[str, object]] | None
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with (
            patch(
                "cw.codex_finalize._post.fetch_issue_comments", return_value=comments
            ),
            patch("cw.codex_stage_post.post_parts", return_value=_posted_ok()) as post,
        ):
            _post(client, _worktree(tmp_path))
        post.assert_called_once()


class TestFreeText:
    def test_a_free_text_ticket_skips_with_a_reason(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with (
            patch("cw.codex_stage_post.post_parts") as post,
            patch("cw.codex_finalize._post.fetch_issue_comments") as fetch,
        ):
            _post(client, _worktree(tmp_path), plan_source="free_text")
        post.assert_not_called()
        fetch.assert_not_called()
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == SKIP_FREE_TEXT == "free_text_ticket"
        assert event["kind"] == "pr_link"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None


class TestOtherTracker:
    def test_posts_through_the_a2_tracker_io_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        worktree = _worktree(tmp_path)
        runner = ScriptedCodexRunner()
        parts = plan_turn_parts(
            f"PR: {_PR_URL}\n",
            TrackerIoContext(runner=runner, client=client, worktree=worktree),
        )
        runner.add(
            any_call,
            CodexRunResult(
                returncode=0,
                stdout="",
                stderr="",
                output_file_content=tracker_io_turn_result(_TICKET, parts),
            ),
        )
        with patch("cw.codex_finalize._post.fetch_issue_comments") as fetch:
            _post(client, worktree, runner=runner)
        fetch.assert_not_called()
        assert len(runner.calls) == 1
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == "pr_link"
        assert event["tracker"] == TRACKER_LINEAR
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None

    def test_a_disabled_server_is_a_skipped_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        stage_mcp_config(monkeypatch, tmp_path, "codex", "disabled")
        _post(client, _worktree(tmp_path))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["kind"] == "pr_link"


class TestNeverHoldsTheRow:
    @pytest.mark.parametrize(
        "where",
        [
            "cw.codex_finalize._post.fetch_issue_comments",
            "cw.codex_stage_post.post_parts",
        ],
    )
    def test_a_post_fault_never_escapes(self, tmp_path: Path, where: str) -> None:
        client = codex_tracker_client(tmp_path)
        with patch(where, side_effect=OSError("disk")):
            assert _post(client, _worktree(tmp_path)) is None

    def test_a_fault_leaves_no_event(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.codex_finalize._post.fetch_issue_comments", side_effect=OSError):
            _post(client, _worktree(tmp_path))
        assert _events() == []

    def test_the_events_are_json_serialisable(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        with (
            _no_comments(),
            patch("cw.codex_stage_post.post_parts", return_value=_posted_ok()),
        ):
            _post(client, _worktree(tmp_path))
        json.dumps(_events())
