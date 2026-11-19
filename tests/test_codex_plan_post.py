"""Tests for cw.codex_plan._post: the plan and park comment post (#3060 R9)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._post import (
    PARK_HEADER,
    post_plan_comment,
    render_park_comment,
)
from cw.codex_plan._result import stamp_plan_result
from cw.codex_plan._stations import stamp_markers
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.events import read_events
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType
from cw.models.client import OutboxConfig
from cw.outbox_producer import content_marker
from cw.tracker_post import PART_TEXT_CHARS
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.outbox_builders import entries_for
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import plan_sentinel

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
    park: bool = False,
    artifact: str = "plan.md",
) -> None:
    artifact_path = worktree / ".cw" / artifact
    artifact_path.write_text(text, encoding="utf-8")
    post_plan_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-p",
        text=text,
        artifact_path=artifact_path,
        park=park,
    )


def _no_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make any ``gh`` launch fail the test; the enqueue never shells out."""
    launches: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "gh":
            launches.append(argv)
        return subprocess.CompletedProcess(argv, 128, b"", b"")

    monkeypatch.setattr("cw.gh._sp.run", _run)
    return launches


class TestEnqueue:
    def test_stamped_plan_enqueues_with_the_plan_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        launches = _no_gh(monkeypatch)
        stamped = stamp_markers(_PLAN, today=_DAY)
        _post(client, worktree, stamped)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == "codex-plan"
        assert produced.entry.marker == content_marker("plan", "".join(produced.parts))
        assert produced.entry.operator_authored is False
        (body,) = produced.parts
        assert _plan_body_signoff_ok(body)
        assert AGENT_COMMENT_MARKER not in body
        assert launches == []
        assert _events() == []

    def test_park_enqueues_with_the_park_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        launches = _no_gh(monkeypatch)
        text = f"{PARK_HEADER}\n\nNo open ambiguities or premises.\n"
        _post(client, worktree, text, park=True, artifact="plan-draft.md")
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == "codex-plan"
        assert produced.entry.marker == content_marker("park", "".join(produced.parts))
        assert launches == []

    def test_the_prefix_separates_a_plan_from_a_park_of_the_same_text(
        self, tmp_path: Path
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _post(client, worktree, _PLAN)
        _post(client, worktree, _PLAN, park=True)
        plan, park = entries_for(client.name, ticket=_TICKET)
        assert plan.entry.marker.startswith("plan-")
        assert park.entry.marker.startswith("park-")
        assert plan.entry.marker.split("-", 1)[1] == park.entry.marker.split("-", 1)[1]
        assert _events() == []

    def test_long_plan_splits_with_markers_in_part_one_only(
        self, tmp_path: Path
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        long_plan = _PLAN + "\n".join(f"- line {i} " + "w" * 80 for i in range(1500))
        stamped = stamp_markers(long_plan, today=_DAY)
        assert len(stamped) > PART_TEXT_CHARS
        _post(client, worktree, stamped)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        parts = produced.parts
        assert len(parts) > 1
        assert "(part 1 of " in parts[0]
        assert _plan_body_signoff_ok(parts[0])
        assert all("plan-spec-reviewed" not in part for part in parts[1:])
        assert all(AGENT_COMMENT_MARKER not in part for part in parts)

    def test_unplannable_text_is_skipped(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch(
            "cw.codex_stage_post.plan_prose_parts", side_effect=ValueError("no plan")
        ):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == "plan_refused"
        assert event["kind"] == "plan"
        assert entries_for(client.name) == []

    def test_a_raised_fault_is_caught(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.codex_stage_post.enqueue_prose", side_effect=OSError("disk")):
            _post(client, worktree, _PLAN)
        assert _events() == []

    def test_an_enqueue_failure_is_skipped_with_a_null_entry_id(
        self, tmp_path: Path
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, worktree, _PLAN)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == "outbox_enqueue_failed"
        assert event["entry_id"] is None
        assert event["tracker"] == "github-issues"
        assert event["session_id"] == "sess-p"
        assert event["artifact_path"] == str(worktree / ".cw" / "plan.md")

    def test_a_duplicate_is_skipped_as_already_posted(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _post(client, worktree, _PLAN)
        _post(client, worktree, _PLAN)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        (event,) = _events()
        assert event["reason"] == "already_posted"
        assert event["entry_id"] == produced.entry.id

    def test_an_enabled_false_client_still_enqueues(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path).model_copy(
            update={"outbox": OutboxConfig(enabled=False)}
        )
        _post(client, _worktree(tmp_path), _PLAN)
        assert len(entries_for(client.name, ticket=_TICKET)) == 1
        assert _events() == []


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
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(
                client,
                worktree,
                "## Pending Verification Scan\n",
                park=True,
                artifact="plan-draft.md",
            )
        (event,) = _events()
        assert event["artifact_path"] == str(worktree / ".cw" / "plan-draft.md")

    def test_absent_artifact_is_null(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            post_plan_comment(
                task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
                client=client,
                worktree=worktree,
                sid="sess-p",
                text=_PLAN,
                artifact_path=worktree / ".cw" / "missing.md",
            )
        (event,) = _events()
        assert event["artifact_path"] is None

    def test_free_standing_artifact_path_null(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            post_plan_comment(
                task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
                client=client,
                worktree=worktree,
                sid="sess-p",
                text=_PLAN,
                artifact_path=None,
            )
        (event,) = _events()
        assert event["artifact_path"] is None
