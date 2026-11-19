"""Tests for cw.codex_stage_post: the shared codex stage comment enqueue (#3707)."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_stage_post import post_stage_comment
from cw.events import read_events
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType
from cw.models.client import OutboxConfig
from cw.outbox import MAX_PART_CHARS
from cw.outbox_producer import content_marker
from cw.tracker_post import PART_TEXT_CHARS, PartPlan
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.outbox_builders import entries_for
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review_signals import VerdictCommentKind
    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_PR_URL = "https://github.com/o/r/pull/9"
_TEXT = f"PR: {_PR_URL}\n"
_PLAN_PRODUCER = "codex-plan"
_PR_LINK_PRODUCER = "codex-pr-link"
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


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _post(
    client: ClientConfig,
    worktree: Path,
    kind: VerdictCommentKind,
    *,
    text: str = _TEXT,
    artifact_path: Path | None = None,
    marker_prefix: str | None = None,
    marker_source: str | None = None,
) -> None:
    producer = _PR_LINK_PRODUCER if kind == "pr_link" else _PLAN_PRODUCER
    post_stage_comment(
        task=_make_ticket_task(ticket_id=_TICKET, client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-s",
        text=text,
        artifact_path=artifact_path,
        kind=kind,
        producer=producer,
        marker_prefix=marker_prefix or ("pr-link" if kind == "pr_link" else "plan"),
        marker_source=marker_source,
    )


@pytest.fixture
def no_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record any ``gh`` launch; the enqueue must make none."""
    launches: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "gh":
            launches.append(argv)
        return subprocess.CompletedProcess(argv, 128, b"", b"")

    monkeypatch.setattr("cw.gh._sp.run", _run)
    return launches


class TestEnqueue:
    def test_plan_enqueues(self, tmp_path: Path, no_gh: list[list[str]]) -> None:
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path), "plan")
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == _PLAN_PRODUCER
        assert produced.entry.marker == f"plan-{_sha16(''.join(produced.parts))}"
        assert produced.entry.redacted is False
        assert produced.entry.operator_authored is False
        assert produced.parts == [_TEXT]
        assert no_gh == []
        assert _events() == []

    def test_redaction_follows_the_part_plan(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _post(client, worktree, "plan", text=f"verdict: see {worktree}/private-note")
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.redacted is True
        assert str(worktree) not in "".join(produced.parts)

    def test_park_enqueues(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _post(client, worktree, "plan")
        _post(client, worktree, "plan", marker_prefix="park")
        plan, park = entries_for(client.name, ticket=_TICKET)
        assert plan.entry.producer == park.entry.producer == _PLAN_PRODUCER
        assert plan.entry.marker.startswith("plan-")
        assert park.entry.marker.startswith("park-")
        assert park.entry.marker == content_marker("park", "".join(park.parts))
        assert _events() == []

    def test_pr_link_enqueues(self, tmp_path: Path, no_gh: list[list[str]]) -> None:
        client = codex_tracker_client(tmp_path)
        _post(client, _worktree(tmp_path), "pr_link", marker_source=_PR_URL)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        assert produced.entry.producer == _PR_LINK_PRODUCER
        assert produced.entry.marker == f"pr-link-{_sha16(_PR_URL)}"
        assert produced.parts == [_TEXT]
        assert no_gh == []
        assert _events() == []

    def test_long_plan_parts_are_verbatim_labelled(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path)
        text = "## Summary\n\n" + "\n".join("x" * 90 for _ in range(1500))
        assert len(text) > PART_TEXT_CHARS
        _post(client, _worktree(tmp_path), "plan", text=text)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        count = len(produced.parts)
        assert count > 1
        for index, part in enumerate(produced.parts, 1):
            assert f"(part {index} of {count})" in part
            assert AGENT_COMMENT_MARKER not in part
        assert produced.entry.marker == f"plan-{_sha16(''.join(produced.parts))}"

    def test_an_enabled_false_client_still_enqueues(self, tmp_path: Path) -> None:
        client = codex_tracker_client(tmp_path).model_copy(
            update={"outbox": OutboxConfig(enabled=False)}
        )
        _post(client, _worktree(tmp_path), "plan")
        assert len(entries_for(client.name, ticket=_TICKET)) == 1
        assert _events() == []


@pytest.mark.parametrize("kind", _KINDS)
class TestSkipped:
    def test_a_refused_plan_is_skipped(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch(
            "cw.codex_stage_post.plan_prose_parts", side_effect=ValueError("no")
        ):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == "plan_refused"
        assert event["kind"] == kind
        assert event["entry_id"] is None
        assert entries_for(client.name) == []

    def test_empty_parts_are_a_refused_plan(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch(
            "cw.codex_stage_post.plan_prose_parts",
            return_value=PartPlan(bodies=(), redacted=False),
        ):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["reason"] == "plan_refused"
        assert entries_for(client.name) == []

    def test_a_body_over_the_part_cap_is_skipped(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        huge = PartPlan(bodies=("x" * (MAX_PART_CHARS + 1),), redacted=False)
        with patch("cw.codex_stage_post.plan_prose_parts", return_value=huge):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["reason"] == "body_too_long"
        assert event["entry_id"] is None
        assert entries_for(client.name) == []

    def test_an_enqueue_failure_is_skipped_with_a_null_entry_id(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, _worktree(tmp_path), kind)
        (event,) = _events()
        assert event["type"] == "review.verdict_comment_skipped"
        assert event["reason"] == "outbox_enqueue_failed"
        assert event["kind"] == kind
        assert event["entry_id"] is None
        assert event["tracker"] == "github-issues"
        assert event["session_id"] == "sess-s"
        assert event["reviewed_sha"] is None

    def test_a_duplicate_is_skipped_as_already_posted_with_the_entry_id(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        _post(client, worktree, kind)
        _post(client, worktree, kind)
        (produced,) = entries_for(client.name, ticket=_TICKET)
        (event,) = _events()
        assert event["reason"] == "already_posted"
        assert event["entry_id"] == produced.entry.id

    def test_a_stage_fault_is_swallowed(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        with patch("cw.codex_stage_post.enqueue_prose", side_effect=OSError("disk")):
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
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, _worktree(tmp_path), kind)
        assert f"{kind}_comment_skipped" in caplog.text

    def test_existing_artifact_is_named(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        artifact = worktree / ".cw" / "a.md"
        artifact.write_text(_TEXT, encoding="utf-8")
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, worktree, kind, artifact_path=artifact)
        (event,) = _events()
        assert event["artifact_path"] == str(artifact)

    def test_a_missing_artifact_is_null(
        self, tmp_path: Path, kind: VerdictCommentKind
    ) -> None:
        client = codex_tracker_client(tmp_path)
        worktree = _worktree(tmp_path)
        with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
            _post(client, worktree, kind, artifact_path=worktree / ".cw" / "no.md")
        (event,) = _events()
        assert event["artifact_path"] is None
