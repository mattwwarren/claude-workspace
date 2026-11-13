"""Tests for cw.codex_stage_post: the shared codex stage comment post (#3062)."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_runner import CodexRunResult
from cw.codex_stage_post import (
    GITHUB_POST_FAILED,
    StagePost,
    post_stage_comment,
    tracker_label,
)
from cw.codex_usage import UsageAccumulator
from cw.events import read_events
from cw.gh import agent_marked_body
from cw.models import OrchestratorEventType
from cw.models.usage import UsageStage
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR
from cw.tracker_io import TrackerIoContext, plan_turn_parts
from cw.tracker_post import PostedPart, PostReason, PostResult, PostStatus
from tests._backend_mcp_helpers import codex_tracker_client, stage_mcp_config
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import (
    ScriptedCodexRunner,
    any_call,
    codex_usage_stdout,
    tracker_io_turn_result,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review_signals import VerdictCommentKind
    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_TEXT = "PR: https://github.com/o/r/pull/9\n"
_KINDS: list[VerdictCommentKind] = ["plan", "pr_link"]
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
    (worktree / ".cw").mkdir(parents=True, exist_ok=True)
    return worktree


def _post(
    client: ClientConfig,
    worktree: Path,
    kind: VerdictCommentKind,
    *,
    runner: ScriptedCodexRunner | None = None,
    artifact_path: Path | None = None,
) -> None:
    post_stage_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-s",
        runner=runner or ScriptedCodexRunner(),
        text=_TEXT,
        artifact_path=artifact_path,
        kind=kind,
    )


def _result(status: PostStatus, parts: tuple[PostedPart, ...] = ()) -> PostResult:
    return PostResult(status, PostReason.POST_FAILED, parts, 1)


_URL = "https://github.com/o/r/issues/7#issuecomment-41"


@pytest.mark.parametrize("kind", _KINDS)
class TestGitHub:
    def test_success_is_posted(self, tmp_path: Path, kind: VerdictCommentKind) -> None:
        client = codex_tracker_client(tmp_path)
        ok = PostResult(PostStatus.POSTED, None, (PostedPart(index=1, url=_URL),), 1)
        with patch("cw.codex_stage_post.post_parts", return_value=ok):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == kind
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] is None
        assert event["posted"] == [{"index": 1, "comment_id": "41", "url": _URL}]

    def test_success_hashes_the_marked_redacted_body(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        """#3353: the GitHub path hashes what post_issue_comment posts, not raw text."""
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        raw = f"verdict: see {worktree}/private-note"
        ok = PostResult(PostStatus.POSTED, None, (PostedPart(index=1, url=_URL),), 1)
        with patch("cw.codex_stage_post.post_parts", return_value=ok):
            post_stage_comment(
                task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
                client=client,
                worktree=worktree,
                sid="sess-s",
                runner=ScriptedCodexRunner(),
                text=raw,
                artifact_path=None,
                kind=kind,
            )
        (event,) = _events()
        ctx = TrackerIoContext(
            runner=ScriptedCodexRunner(), client=client, worktree=worktree
        )
        posted_body = agent_marked_body(plan_turn_parts(raw, ctx, marked=False)[0])
        assert str(worktree) not in posted_body
        expected = hashlib.sha256(posted_body.encode("utf-8")).hexdigest()
        assert event["content_sha256"] == expected
        assert event["content_sha256"] != hashlib.sha256(raw.encode()).hexdigest()

    def test_refused_is_skipped(self, tmp_path: Path, kind: VerdictCommentKind) -> None:
        client = codex_tracker_client(tmp_path)
        with patch(
            "cw.codex_stage_post.post_parts",
            return_value=_result(PostStatus.REFUSED),
        ):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == GITHUB_POST_FAILED
        assert event["kind"] == kind

    def test_partial_is_unverified(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        partial = _result(PostStatus.PARTIAL, (PostedPart(index=1, url=_URL),))
        with patch("cw.codex_stage_post.post_parts", return_value=partial):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_unverified"
        assert event["kind"] == kind
        assert event["public_state"] == "possible"

    def test_unplannable_text_is_skipped(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.codex_stage_post.plan_turn_parts", side_effect=ValueError("no")):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["reason"] == "plan_refused"

    def test_a_stage_fault_is_swallowed(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.codex_stage_post.post_parts", side_effect=OSError("disk")):
            _post(client, _worktree(tmp_path), kind)
        assert _events() == []

    def test_skip_logs_carry_the_kind_label(
        self,
        tmp_path: Path,
        kind: VerdictCommentKind,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = codex_tracker_client(tmp_path)
        caplog.set_level(logging.WARNING, logger="cw.codex_stage_post")
        with patch(
            "cw.codex_stage_post.post_parts",
            return_value=_result(PostStatus.REFUSED),
        ):
            _post(client, _worktree(tmp_path), kind)
        assert f"{kind}_comment_skipped" in caplog.text

    def test_existing_artifact_is_named(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        artifact = worktree / ".cw" / "a.md"
        artifact.write_text(_TEXT, encoding="utf-8")
        with patch(
            "cw.codex_stage_post.post_parts",
            return_value=_result(PostStatus.REFUSED),
        ):
            _post(client, worktree, kind, artifact_path=artifact)
        (event,) = _events()
        assert event["artifact_path"] == str(artifact)


def _linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    client = codex_tracker_client(tmp_path, {"codex": "linear"})
    _write_tracker_config(client.workspace_path, TRACKER_LINEAR)
    stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
    return client


@pytest.mark.parametrize("kind", _KINDS)
class TestOtherTracker:
    def test_verified_turn_is_posted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: VerdictCommentKind,
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        worktree = _worktree(tmp_path)
        runner = ScriptedCodexRunner()
        parts = plan_turn_parts(
            _TEXT, TrackerIoContext(runner=runner, client=client, worktree=worktree)
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
        _post(client, worktree, kind, runner=runner)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == kind
        assert event["tracker"] == TRACKER_LINEAR

    def test_the_post_relabels_the_runner_so_the_turn_records_as_tracker_io(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: VerdictCommentKind,
    ) -> None:
        """#3063: the caller's recording runner is relabelled for the post."""
        client = _linear(tmp_path, monkeypatch)
        worktree = _worktree(tmp_path)
        scripted = ScriptedCodexRunner()
        parts = plan_turn_parts(
            _TEXT, TrackerIoContext(runner=scripted, client=client, worktree=worktree)
        )
        scripted.add(
            any_call,
            CodexRunResult(
                returncode=0,
                stdout=codex_usage_stdout(input_tokens=200, output_tokens=20),
                stderr="",
                output_file_content=tracker_io_turn_result(_TICKET, parts),
            ),
        )
        acc = UsageAccumulator("s-post", _TICKET, {})
        runner = acc.recording_runner(UsageStage.PLAN, scripted)

        _post(client, worktree, kind, runner=runner)

        snapshot = acc.snapshot()
        assert snapshot is not None
        assert list(snapshot.usage.by_stage) == [UsageStage.TRACKER_IO]

    def test_refused_turn_is_skipped_with_kind_label(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: VerdictCommentKind,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        stage_mcp_config(monkeypatch, tmp_path, "codex", "disabled")
        caplog.set_level(logging.WARNING)
        _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["kind"] == kind
        assert f"{kind}_comment_skipped" in caplog.text


class TestTrackerLabel:
    def test_github_default(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        assert tracker_label(client) == (TRACKER_GITHUB_ISSUES, True)

    def test_other_tracker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _linear(tmp_path, monkeypatch)
        assert tracker_label(client) == (TRACKER_LINEAR, False)


def test_identity_carries_the_kind(tmp_path: Path) -> None:
    client = codex_tracker_client(tmp_path)
    post = StagePost(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        tracker=TRACKER_GITHUB_ISSUES,
        artifact_path=None,
        sid="s",
        kind="pr_link",
    )
    assert post.identity.kind == "pr_link"
    assert post.identity.reviewed_sha is None


def test_no_gh_launch_for_a_refused_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real poster is reached; a failing ``gh`` is a skipped event."""
    client = codex_tracker_client(tmp_path)

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"nope")

    monkeypatch.setattr("cw.gh._sp.run", _run)
    _post(client, _worktree(tmp_path), "pr_link")
    (event,) = _events()
    assert event["reason"] == GITHUB_POST_FAILED
