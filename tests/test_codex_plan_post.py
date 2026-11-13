"""Tests for cw.codex_plan._post: the plan and park comment post (#3060 R9)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._post import (
    GITHUB_POST_FAILED,
    PARK_HEADER,
    post_plan_comment,
    render_park_comment,
)
from cw.codex_plan._result import stamp_plan_result
from cw.codex_plan._stations import stamp_markers
from cw.codex_runner import CodexRunResult
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.events import read_events
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR
from cw.tracker_io import TrackerIoContext, plan_turn_parts
from cw.tracker_post import (
    PART_TEXT_CHARS,
    PostedPart,
    PostReason,
    PostResult,
    PostStatus,
)
from tests._backend_mcp_helpers import codex_tracker_client, stage_mcp_config
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import (
    FakeGhComment,
    ScriptedCodexRunner,
    any_call,
    plan_sentinel,
    tracker_io_turn_result,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_DAY = date(2026, 1, 2)
_PLAN = "## Summary\n\nThe plan.\n\n**Scope tier:** small\n"
_EVENT_TYPES = [
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_POSTED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED,
    OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED,
]


def _result(tmp_path: Path, **overrides: Any) -> AutoDevResult:
    block = extract_block(plan_sentinel(**overrides))
    assert block is not None
    return stamp_plan_result(json.loads(block), ticket_id=_TICKET, worktree=tmp_path)


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
    text: str,
    *,
    runner: ScriptedCodexRunner | None = None,
    artifact: str = "plan.md",
) -> None:
    artifact_path = worktree / ".cw" / artifact
    artifact_path.write_text(text, encoding="utf-8")
    post_plan_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-p",
        runner=runner or ScriptedCodexRunner(),
        text=text,
        artifact_path=artifact_path,
    )


def _gh_ok(url_id: int) -> subprocess.CompletedProcess[bytes]:
    stdout = f"https://github.com/o/r/issues/7#issuecomment-{url_id}\n".encode()
    return subprocess.CompletedProcess(["gh"], 0, stdout, b"")


def _skip_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the shared poster's ``gh`` read-back (it has its own tests).

    These tests pin what the plan post hands the poster; the read-back is a
    separate verification that needs ``gh`` answers and real retry sleeps.
    """
    monkeypatch.setattr("cw.tracker_post._read_back", lambda *_a, **_k: None)


class TestGitHub:
    def test_stamped_plan_posts_with_one_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        _write_tracker_config(client.workspace_path, TRACKER_GITHUB_ISSUES)
        worktree = _worktree(tmp_path)
        _skip_readback(monkeypatch)
        bodies: list[str] = []

        def _run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if argv[0] != "gh":
                return subprocess.CompletedProcess(argv, 128, b"", b"")
            stdin = kwargs.get("input")
            bodies.append(stdin.decode() if isinstance(stdin, bytes) else "")
            return _gh_ok(len(bodies))

        monkeypatch.setattr("cw.gh._sp.run", _run)
        stamped = stamp_markers(_PLAN, today=_DAY)
        _post(client, worktree, stamped)
        (body,) = bodies
        assert body.count(AGENT_COMMENT_MARKER) == 1
        assert _plan_body_signoff_ok(body)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == "plan"
        assert event["reviewed_sha"] is None
        assert event["tracker"] == TRACKER_GITHUB_ISSUES

    def test_long_plan_splits_with_markers_in_part_one_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _skip_readback(monkeypatch)
        calls: list[str] = []

        def _run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if argv[0] != "gh":
                return subprocess.CompletedProcess(argv, 128, b"", b"")
            stdin = kwargs.get("input")
            calls.append(stdin.decode() if isinstance(stdin, bytes) else "")
            return _gh_ok(len(calls))

        monkeypatch.setattr("cw.gh._sp.run", _run)
        long_plan = _PLAN + "\n".join(f"- line {i} " + "w" * 80 for i in range(1500))
        stamped = stamp_markers(long_plan, today=_DAY)
        assert len(stamped) > PART_TEXT_CHARS
        _post(client, worktree, stamped)
        assert len(calls) > 1
        assert _plan_body_signoff_ok(calls[0])
        assert all("plan-spec-reviewed" not in body for body in calls[1:])
        assert all(body.count(AGENT_COMMENT_MARKER) == 1 for body in calls)
        (event,) = _events()
        assert [p["index"] for p in event["posted"]] == list(range(1, len(calls) + 1))
        assert event["posted"][0]["comment_id"] == "1"

    def test_gh_failure_skips_and_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        calls: list[list[str]] = []

        def _run(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if argv[0] == "gh":
                calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, b"", b"nope")

        monkeypatch.setattr("cw.gh._sp.run", _run)
        long_plan = _PLAN + "\n".join("x" * 90 for _ in range(1500))
        _post(client, worktree, long_plan)
        assert len(calls) == 1
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == GITHUB_POST_FAILED
        assert event["kind"] == "plan"

    def test_unplannable_text_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        FakeGhComment().install(monkeypatch)
        with patch(
            "cw.codex_stage_post.plan_turn_parts", side_effect=ValueError("no plan")
        ):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["reason"] == "plan_refused"

    def test_a_raised_fault_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.codex_stage_post.post_parts", side_effect=OSError("disk")):
            _post(client, worktree, _PLAN)
        assert _events() == []

    def test_a_partially_public_post_is_unverified_not_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Part 1 is public, part 2 failed: the operator must check the ticket."""
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        partial = PostResult(
            PostStatus.PARTIAL,
            PostReason.POST_FAILED,
            (
                PostedPart(
                    index=1, url="https://github.com/o/r/issues/7#issuecomment-41"
                ),
            ),
            2,
        )
        with patch("cw.codex_stage_post.post_parts", return_value=partial):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_unverified"
        assert event["kind"] == "plan"
        assert event["reason"] == GITHUB_POST_FAILED
        assert event["public_state"] == "possible"
        assert event["posted"] == [
            {
                "index": 1,
                "comment_id": "41",
                "url": "https://github.com/o/r/issues/7#issuecomment-41",
            }
        ]

    def test_a_first_part_unknown_post_is_unverified_without_public_parts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        unknown = PostResult(PostStatus.PARTIAL, PostReason.POST_OUTCOME_UNKNOWN, (), 1)
        with patch("cw.codex_stage_post.post_parts", return_value=unknown):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_unverified"
        assert event["reason"] == GITHUB_POST_FAILED

    def test_a_refused_first_part_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is public when the first part is refused: the post is skipped."""
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        refused = PostResult(PostStatus.REFUSED, PostReason.POST_FAILED, (), 1)
        with patch("cw.codex_stage_post.post_parts", return_value=refused):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == GITHUB_POST_FAILED


def _linear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ClientConfig, Path]:
    client = codex_tracker_client(tmp_path, {"codex": "linear"})
    _write_tracker_config(client.workspace_path, TRACKER_LINEAR)
    stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
    return client, _worktree(tmp_path)


def _turn_runner(
    client: ClientConfig, worktree: Path, text: str, **kwargs: Any
) -> ScriptedCodexRunner:
    runner = ScriptedCodexRunner()
    parts = plan_turn_parts(
        text, TrackerIoContext(runner=runner, client=client, worktree=worktree)
    )
    result = CodexRunResult(
        returncode=0,
        stdout="",
        stderr="",
        output_file_content=tracker_io_turn_result(_TICKET, parts, **kwargs),
    )
    return runner.add(any_call, result)


class TestNonGitHub:
    def test_verified_post_emits_posted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, worktree = _linear(tmp_path, monkeypatch)
        _post(client, worktree, _PLAN, runner=_turn_runner(client, worktree, _PLAN))
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_posted"
        assert event["kind"] == "plan"
        assert event["client"] == "acme"
        assert event["tracker"] == TRACKER_LINEAR
        assert event["session_id"] == "sess-p"
        assert event["reviewed_sha"] is None
        assert event["artifact_path"] == str(worktree / ".cw" / "plan.md")
        # #3353: the hash is of the marked, redacted body the turn was given.
        ctx = TrackerIoContext(
            runner=ScriptedCodexRunner(), client=client, worktree=worktree
        )
        expected = hashlib.sha256(
            plan_turn_parts(_PLAN, ctx)[0].encode("utf-8")
        ).hexdigest()
        assert event["content_sha256"] == expected

    def test_refusal_emits_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, worktree = _linear(tmp_path, monkeypatch)
        stage_mcp_config(monkeypatch, tmp_path, "codex", "disabled")
        _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["kind"] == "plan"
        assert event["reason"] == "tracker_mcp_not_enabled"

    def test_unverified_outcome_emits_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, worktree = _linear(tmp_path, monkeypatch)

        def _mutate(doc: dict[str, Any]) -> None:
            doc["parts"][0]["readback_tail"] = "tampered"

        runner = _turn_runner(client, worktree, _PLAN, mutate=_mutate)
        _post(client, worktree, _PLAN, runner=runner)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_unverified"
        assert event["kind"] == "plan"
        assert event["public_state"] != "none"
        assert event["posted"]
        assert "marker_survived" in event

    def test_nothing_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, worktree = _linear(tmp_path, monkeypatch)
        runner = ScriptedCodexRunner().add(
            any_call, CodexRunResult(returncode=1, stdout="", stderr="down")
        )
        _post(client, worktree, _PLAN, runner=runner)
        assert len(runner.calls) == 1
        (event,) = _events()
        assert event["reason"] == "turn_exit_nonzero"


class TestParkComment:
    def test_ambiguity_park_for_small_plan_does_not_request_approval(
        self, tmp_path: Path
    ) -> None:
        result = _result(
            tmp_path,
            tier="small",
            status="ambiguities_pending_resolution",
            ambiguities=[{"question": "Which store?"}],
            next_actions=["user_resolve_ambiguities"],
            plan_draft_fingerprint=None,
        )
        text = render_park_comment(
            ticket_id=_TICKET,
            client_name="acme",
            result=result,
            draft_body="DRAFT BODY",
            approved_fingerprint=None,
            advisory_findings=[],
        )
        assert "### How to answer" in text
        assert "### Approval requested" not in text

    def test_ambiguities_park_comment(self, tmp_path: Path) -> None:
        result = _result(
            tmp_path,
            tier="large",
            status="ambiguities_pending_resolution",
            ambiguities=[{"question": "Which store?", "recommendation": "(a)"}],
            premises=[{"claim": "The API is stable"}],
            next_actions=["user_resolve_ambiguities"],
            plan_draft_fingerprint=None,
        )
        result = result.model_copy(update={"plan_draft_fingerprint": "d" * 64})
        text = render_park_comment(
            ticket_id=_TICKET,
            client_name="acme",
            result=result,
            draft_body="DRAFT BODY",
            approved_fingerprint="e" * 64,
            advisory_findings=["- Plan Reviewer [SHOULD_FIX] tighten it"],
        )
        assert text.startswith(PARK_HEADER)
        assert "1. Which store?" in text
        assert "   - recommendation: (a)" in text
        assert "2. The API is stable" in text
        assert "### How to answer" in text
        assert f"cw dev-queue resolve {_TICKET} -c acme" in text
        assert "### Advisory plan-review findings" in text
        assert "### Approval requested" in text
        assert "d" * 64 in text
        assert "e" * 64 in text
        assert "### Draft plan (unreviewed — context only)\n\nDRAFT BODY" in text
        assert "auto-dev-preflight-resolutions" not in text

    def test_approval_park_without_items(self, tmp_path: Path) -> None:
        result = _result(tmp_path, tier="large", status="plan_pending_approval")
        text = render_park_comment(
            ticket_id=_TICKET,
            client_name="acme",
            result=result,
            draft_body="DRAFT",
            approved_fingerprint=None,
            advisory_findings=[],
            mismatch=True,
        )
        assert "No open ambiguities or premises." in text
        assert "### How to answer" not in text
        assert "fingerprint mismatch" in text
        assert "approved fingerprint: `none`" in text

    def test_park_post_names_the_draft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        FakeGhComment().install(monkeypatch)
        _post(
            client, worktree, "## Pending Verification Scan\n", artifact="plan-draft.md"
        )
        (event,) = _events()
        assert event["artifact_path"] == str(worktree / ".cw" / "plan-draft.md")

    def test_absent_artifact_is_null(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        FakeGhComment().install(monkeypatch)
        post_plan_comment(
            task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
            client=client,
            worktree=worktree,
            sid="sess-p",
            runner=ScriptedCodexRunner(),
            text=_PLAN,
            artifact_path=worktree / ".cw" / "missing.md",
        )
        (event,) = _events()
        assert event["artifact_path"] is None
