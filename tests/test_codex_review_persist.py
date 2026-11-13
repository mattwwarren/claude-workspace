"""Tests for cw.codex_review_persist — a review verdict's durable copies and post.

Relocated with the module itself out of ``tests/test_codex_background.py``
(#2807): the worktree artifacts (``_persist_review_verdict``,
``_persist_structured_review_verdict``, ``_persist_unparseable_artifact``) and
the best-effort tracker comment (``_post_review_comment``).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw.auto_dev_result import Blocker
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE
from cw.codex_review_persist import (
    REVIEW_VERDICT_OWNER_STAMP_FORMAT,
    _post_daemon_verdict_comment,
    _post_review_comment,
)
from cw.codex_review_signals import VerdictCommentKind
from cw.codex_runner import FakeCodexRunner
from cw.events import read_events
from cw.models import ClientConfig, OrchestratorEvent, OrchestratorEventType
from cw.models.client import TrackerMcpGateConfig
from cw.review_findings import ReviewVerdictEnvelope, consolidate_verdict
from cw.tracker_io import TrackerIoContext, plan_turn_parts
from tests._backend_mcp_helpers import McpStage, stage_mcp_config
from tests._shared.review_builders import _make_diff, _make_finding, _make_reviewer_doc
from tests._tool_fakes import FakeGhComment, tracker_io_turn_result

# ---------------------------------------------------------------------------
# _post_review_comment — Step 4b's best-effort GitHub write
# ---------------------------------------------------------------------------

# The identity keywords every caller passes (#3055); the skip event names them.
_IDENTITY: dict[str, Any] = {
    "client": "acme",
    "session_id": "sess-1",
    "kind": "verdict",
    "reviewed_sha": "abc123",
}


def _skip_events() -> list[OrchestratorEvent]:
    return read_events(
        consumer="test-3055-skip",
        event_types=[OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED],
    )


def _unverified_events() -> list[OrchestratorEvent]:
    return read_events(
        consumer="test-3057-unverified",
        event_types=[OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED],
    )


def _posted_events() -> list[OrchestratorEvent]:
    return read_events(
        consumer="test-3057-posted",
        event_types=[OrchestratorEventType.REVIEW_VERDICT_COMMENT_POSTED],
    )


def test_post_review_comment_suppresses_oserror() -> None:
    """_post_review_comment swallows OSError from a missing gh binary."""
    with patch("cw.gh._sp.run", side_effect=FileNotFoundError("no gh")):
        _post_review_comment("T-1", "findings", **_IDENTITY)


def test_post_review_comment_suppresses_timeout() -> None:
    """_post_review_comment swallows TimeoutExpired when gh hangs."""
    with patch(
        "cw.gh._sp.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        _post_review_comment("T-1", "findings", **_IDENTITY)


def test_post_review_comment_forwards_cwd() -> None:
    """#1279: _post_review_comment scopes the gh call to the client's repo."""
    want_cwd = Path("/some/client-a/repo")
    with patch("cw.codex_review_persist.post_issue_comment") as post_mock:
        _post_review_comment("T-1", "findings", cwd=want_cwd, **_IDENTITY)
    post_mock.assert_called_once_with("T-1", "findings", cwd=want_cwd)


def test_post_review_comment_logs_on_none_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gh call couldn't run at all (missing binary / timeout) -> warning, not silent."""
    with (
        patch("cw.codex_review_persist.post_issue_comment", return_value=None),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None, **_IDENTITY)
    assert any(
        "T-1" in r.message and "gh call failed" in r.message for r in caplog.records
    )


def test_post_review_comment_skips_gh_on_non_github_tracker(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """#2095: a Linear-tracked ticket never reaches gh (the call could only
    fail with 'invalid issue format'); the skip is a WARNING that names the
    tracker and the durable artifact, not a swallowed subprocess failure."""
    artifact = tmp_path / ".claude" / "review-verdict.md"
    with (
        patch("cw.codex_review_persist.post_issue_comment") as post_mock,
        caplog.at_level("WARNING"),
    ):
        _post_review_comment(
            "GEN-1",
            "findings",
            cwd=None,
            tracker="linear",
            artifact_path=artifact,
            **_IDENTITY,
        )
    post_mock.assert_not_called()
    assert any(
        "review_comment_skipped" in r.message
        and "GEN-1" in r.message
        and "linear" in r.message
        and str(artifact) in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("kind", "reviewed_sha", "expected_sha"),
    [("verdict", "abc123", "abc123"), ("unparseable", "", None)],
)
def test_post_review_comment_skip_records_the_event(
    tmp_path: Path,
    kind: VerdictCommentKind,
    reviewed_sha: str,
    expected_sha: str | None,
) -> None:
    """#3055 (Case A): the non-GitHub skip emits review.verdict_comment_skipped
    with the full payload; an empty captured sha (git failure) is null."""
    artifact = tmp_path / ".claude" / "review-verdict.md"
    with patch("cw.codex_review_persist.post_issue_comment") as post_mock:
        _post_review_comment(
            "GEN-1",
            "findings",
            client="acme",
            session_id="sess-1",
            kind=kind,
            reviewed_sha=reviewed_sha,
            tracker="linear",
            artifact_path=artifact,
        )
    post_mock.assert_not_called()
    (event,) = _skip_events()
    assert event.correlation_id == "GEN-1"
    assert list(event.payload.items()) == [
        ("ticket_id", "GEN-1"),
        ("client", "acme"),
        ("tracker", "linear"),
        ("backend", "codex"),
        ("kind", kind),
        ("reason", "non_github_tracker"),
        ("artifact_path", str(artifact)),
        ("reviewed_sha", expected_sha),
        ("session_id", "sess-1"),
    ]


def test_post_review_comment_skip_without_artifact_records_null_path() -> None:
    _post_review_comment(
        "GEN-1", "findings", tracker="linear", artifact_path=None, **_IDENTITY
    )
    (event,) = _skip_events()
    assert event.payload["artifact_path"] is None


def test_post_review_comment_github_path_posts_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3055: the GitHub path is unchanged -- gh records the call, no event."""
    fake = FakeGhComment().install(monkeypatch)
    _post_review_comment(
        "7", "findings", tracker="github-issues", artifact_path=None, **_IDENTITY
    )
    assert len(fake.calls) == 1
    assert fake.calls[0][:3] == ["gh", "issue", "comment"]
    assert fake.bodies[0].startswith("findings")
    assert _skip_events() == []


def test_post_review_comment_unset_tracker_posts_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open polarity kept: an unresolvable tracker still posts."""
    fake = FakeGhComment().install(monkeypatch)
    _post_review_comment("7", "findings", tracker=None, **_IDENTITY)
    assert len(fake.calls) == 1
    assert _skip_events() == []


def test_post_review_comment_skip_event_failure_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing event write never escapes _post_review_comment (A15)."""
    with (
        patch(
            "cw.codex_review_signals.record_event",
            side_effect=OSError("inbox locked"),
        ),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("GEN-1", "findings", tracker="linear", **_IDENTITY)
    assert any(
        "codex_review_signal_failed" in r.message and "GEN-1" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("tracker", [None, "github-issues"])
def test_post_review_comment_posts_on_github_or_unknown_tracker(
    tracker: str | None,
) -> None:
    """Fail-open: an unresolvable tracker, or a positively-GitHub one, posts."""
    with patch("cw.codex_review_persist.post_issue_comment") as post_mock:
        post_mock.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=b"", stderr=b""
        )
        _post_review_comment("T-1", "findings", cwd=None, tracker=tracker, **_IDENTITY)
    post_mock.assert_called_once_with("T-1", "findings", cwd=None)


def test_persist_review_verdict_writes_durable_copy(tmp_path: Path) -> None:
    """#2095: the rendered verdict lands in .claude/review-verdict.md, stamped
    with its owning ticket_id/reviewed_sha (#2279)."""
    from cw.codex_review_persist import (
        REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
        _persist_review_verdict,
    )

    path = _persist_review_verdict(
        tmp_path, "## Verdict\n", ticket_id="2279", reviewed_sha="deadbeef"
    )
    assert path == tmp_path / REVIEW_VERDICT_COMMENT_RELATIVE_PATH
    written = path.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="2279", reviewed_sha="deadbeef"
        ).splitlines()[0]
    )
    assert written.endswith("## Verdict\n")


@pytest.mark.raw_git_allowed  # reads this checkout's index, outside the sandbox
def test_review_verdict_file_is_git_ignored() -> None:
    """#2279: the per-ticket verdict must never be committed — tracked, it
    merged to main and every sibling branch inherited another ticket's verdict,
    which finalize then read as its own (#2205)."""
    repo_root = Path(__file__).resolve().parent.parent
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/review-verdict.md" in ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".claude/review-verdict.md"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode != 0, "review-verdict.md is still tracked"


@pytest.mark.raw_git_allowed  # reads this checkout's index, outside the sandbox
def test_review_verdict_json_file_is_git_ignored() -> None:
    """#2223: the structured verdict envelope is a per-ticket artifact too —
    same never-committed contract as its .md sibling (#2279/#2205)."""
    repo_root = Path(__file__).resolve().parent.parent
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/review-verdict.json" in ignored
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".claude/review-verdict.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode != 0, "review-verdict.json is still tracked"


def test_persist_review_verdict_stamp_handles_hyphenated_ticket_id(
    tmp_path: Path,
) -> None:
    """A Linear-style ticket_id (e.g. GEN-1) keeps the stamp line parse-friendly."""
    from cw.codex_review_persist import _persist_review_verdict

    path = _persist_review_verdict(
        tmp_path, "## Verdict\n", ticket_id="GEN-1", reviewed_sha="cafef00d"
    )
    assert path is not None
    written = path.read_text(encoding="utf-8")
    assert (
        written.splitlines()[0]
        == REVIEW_VERDICT_OWNER_STAMP_FORMAT.format(
            ticket_id="GEN-1", reviewed_sha="cafef00d"
        ).splitlines()[0]
    )


def test_persist_review_verdict_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write failure is logged and returns None; it never raises into the
    daemon thread's success path."""
    from cw.codex_review_persist import _persist_review_verdict

    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert (
            _persist_review_verdict(
                tmp_path, "x", ticket_id="T-1", reviewed_sha="deadbeef"
            )
            is None
        )
    assert any("review_verdict_persist_failed" in r.message for r in caplog.records)


def test_persist_structured_review_verdict_writes_json_without_stamp(
    tmp_path: Path,
) -> None:
    """#2223 round 2: the structured verdict is written through
    ``_persist_worktree_artifact`` directly, NOT ``_persist_review_verdict``
    -- that helper's Markdown ownership-comment stamp would make the JSON
    unparseable. Ownership rides as native fields instead (``ticket_id`` on
    the envelope, ``reviewed_sha`` already required on ``verdict``), so the
    file parses with a plain ``model_validate_json``/``json.loads`` -- no
    line-splitting needed."""
    from cw.codex_review_persist import _persist_structured_review_verdict

    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )
    path = _persist_structured_review_verdict(tmp_path, verdict, ticket_id="T-1")
    assert path == tmp_path / ".claude" / "review-verdict.json"
    assert path is not None
    written = path.read_text(encoding="utf-8")
    envelope = ReviewVerdictEnvelope.model_validate_json(written)
    assert envelope.ticket_id == "T-1"
    assert envelope.verdict.reviewed_sha == "deadbeef"
    assert envelope.verdict.accepted[0].finding.evidence == "def broken():"


def test_persist_structured_review_verdict_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sibling of ``test_persist_review_verdict_degrades_on_oserror`` for the
    JSON artifact: a write failure on the shared ``_persist_worktree_artifact``
    contract is logged and returns None, never raises."""
    from cw.codex_review_persist import _persist_structured_review_verdict

    verdict = consolidate_verdict(
        [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))],
        _make_diff(),
        reviewed_sha="deadbeef",
    )
    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert (
            _persist_structured_review_verdict(tmp_path, verdict, ticket_id="T-1")
            is None
        )
    assert any("review_verdict_persist_failed" in r.message for r in caplog.records)


def test_persist_unparseable_review_artifact_writes_worktree_copy(
    tmp_path: Path,
) -> None:
    """#2280: a zero-documents park writes its own durable worktree copy.

    Sibling of :func:`_persist_review_verdict`, for the park a
    ``ReviewVerdict`` was never produced for -- carries the blocker's reason
    and details instead of a rendered verdict comment.
    """
    from cw.codex_review_persist import (
        REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH,
        _persist_unparseable_artifact,
    )

    blocker = Blocker(
        stage="stage3_review",
        reason=CODEX_REVIEW_UNPARSEABLE,
        details="editor (codex_timeout); reviewer (invalid_json)",
    )

    path = _persist_unparseable_artifact(
        tmp_path, blocker, ticket_id="1234", reviewed_sha="deadbeef"
    )

    assert path == tmp_path / REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH
    written = path.read_text(encoding="utf-8")
    assert "ticket_id=1234" in written
    assert "reviewed_sha=deadbeef" in written
    assert CODEX_REVIEW_UNPARSEABLE in written
    assert "editor (codex_timeout); reviewer (invalid_json)" in written


def test_persist_unparseable_review_artifact_degrades_on_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write failure is logged and returns None; never raises (#2280)."""
    from cw.codex_review_persist import _persist_unparseable_artifact

    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")
    blocker = Blocker(
        stage="stage3_review", reason=CODEX_REVIEW_UNPARSEABLE, details="x"
    )
    with caplog.at_level("WARNING"):
        assert (
            _persist_unparseable_artifact(
                tmp_path, blocker, ticket_id="1234", reviewed_sha="deadbeef"
            )
            is None
        )
    assert any("review_verdict_persist_failed" in r.message for r in caplog.records)


def test_post_review_comment_logs_on_nonzero_returncode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero gh exit -> warning carries ticket_id, returncode, and stderr."""
    fake_result = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout=b"", stderr=b"invalid issue format"
    )
    with (
        patch("cw.codex_review_persist.post_issue_comment", return_value=fake_result),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None, **_IDENTITY)
    assert any(
        "T-1" in r.message and "1" in r.message and "invalid issue format" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# _post_review_comment — the tracker-IO turn on a non-GitHub tracker (#3057)
# ---------------------------------------------------------------------------


def _tracker_io(
    tmp_path: Path,
    runner: FakeCodexRunner,
    monkeypatch: pytest.MonkeyPatch,
    state: McpStage = "enabled",
) -> TrackerIoContext:
    stage_mcp_config(monkeypatch, tmp_path, "codex", state)
    workspace = tmp_path / "workspace"
    # Its own .git stops the MCP reader's project-layer walk here (#3055).
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    client = ClientConfig(
        name="acme",
        workspace_path=workspace,
        default_branch="main",
        tracker_mcp_gate=TrackerMcpGateConfig(
            enabled=True, servers={"codex": "linear"}
        ),
    )
    return TrackerIoContext(runner=runner, client=client, worktree=tmp_path)


def _post_linear(context: TrackerIoContext, artifact: Path | None = None) -> None:
    _post_review_comment(
        "GEN-1",
        "findings",
        tracker="linear",
        artifact_path=artifact,
        tracker_io=context,
        **_IDENTITY,
    )


@pytest.mark.usefixtures("isolated_backend_layers")
def test_tracker_io_verified_post_skips_gh_and_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch)
    runner.output_file_content = tracker_io_turn_result(
        "GEN-1", plan_turn_parts("findings", context)
    )
    with patch("cw.codex_review_persist.post_issue_comment") as post_mock:
        _post_linear(context)
    post_mock.assert_not_called()
    assert len(runner.calls) == 1
    assert _skip_events() == []
    assert _unverified_events() == []
    (event,) = _posted_events()
    assert event.payload["ticket_id"] == "GEN-1"
    assert event.payload["posted"] == [
        {
            "index": 1,
            "comment_id": "cmt-1",
            "url": "https://linear.app/acme/issue/GEN-1/x#comment-cmt-1",
        }
    ]
    assert event.payload["client"] == "acme"
    assert event.payload["session_id"] == "sess-1"
    assert event.payload["reviewed_sha"] == "abc123"
    expected = hashlib.sha256(
        plan_turn_parts("findings", context)[0].encode("utf-8")
    ).hexdigest()
    assert event.payload["content_sha256"] == expected
    assert event.payload["content_sha256"] != hashlib.sha256(b"findings").hexdigest()
    assert event.payload["posted_at"]


@pytest.mark.usefixtures("isolated_backend_layers")
def test_audit_hash_covers_the_redacted_marked_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3353: the hash is of the redacted body the turn was given, not the raw text."""
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch)
    raw = f"findings: see {tmp_path}/secret-note"
    runner.output_file_content = tracker_io_turn_result(
        "GEN-1", plan_turn_parts(raw, context)
    )
    _post_review_comment(
        "GEN-1",
        raw,
        tracker="linear",
        artifact_path=None,
        tracker_io=context,
        **_IDENTITY,
    )
    (event,) = _posted_events()
    body = plan_turn_parts(raw, context)[0]
    assert str(tmp_path) not in body
    assert event.payload["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert event.payload["content_sha256"] != hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.usefixtures("isolated_backend_layers")
def test_tracker_io_not_enabled_records_the_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch, "disabled")
    artifact = tmp_path / ".claude" / "review-verdict.md"
    _post_linear(context, artifact)
    assert runner.calls == []
    (event,) = _skip_events()
    assert event.payload["reason"] == "tracker_mcp_not_enabled"
    assert event.payload["artifact_path"] == str(artifact)
    assert _unverified_events() == []


@pytest.mark.usefixtures("isolated_backend_layers")
def test_tracker_io_mismatch_records_the_unverified_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch)
    runner.output_file_content = tracker_io_turn_result(
        "GEN-2", plan_turn_parts("findings", context)
    )
    with caplog.at_level("WARNING"):
        _post_linear(context)
    assert _skip_events() == []
    (event,) = _unverified_events()
    assert event.correlation_id == "GEN-1"
    assert event.payload["reason"] == "ticket_mismatch"
    assert event.payload["public_state"] == "confirmed"
    assert event.payload["posted"] == [
        {
            "index": 1,
            "comment_id": "cmt-1",
            "url": "https://linear.app/acme/issue/GEN-2/x#comment-cmt-1",
        }
    ]
    assert event.payload["marker_survived"] is True
    assert any("review_comment_unverified" in r.message for r in caplog.records)


@pytest.mark.usefixtures("isolated_backend_layers")
def test_tracker_io_timeout_records_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _tracker_io(tmp_path, FakeCodexRunner(simulate_timeout=True), monkeypatch)
    _post_linear(context)
    (event,) = _unverified_events()
    assert event.payload["reason"] == "turn_timeout"
    assert event.payload["public_state"] == "possible"
    assert event.payload["posted"] == []
    assert event.payload["marker_survived"] is None


def test_github_tracker_with_context_keeps_the_gh_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub path unchanged: gh posts, the turn never runs."""
    fake = FakeGhComment().install(monkeypatch)
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch)
    for tracker in ("github-issues", None):
        _post_review_comment(
            "7", "findings", tracker=tracker, tracker_io=context, **_IDENTITY
        )
    assert len(fake.calls) == 2
    assert runner.calls == []


_SURFACE_FAULTS: list[Exception] = [
    OSError("disk"),
    ValueError("v"),
    RuntimeError("boom"),
    LookupError("k"),
    TypeError("t"),
    AttributeError("a"),
    subprocess.SubprocessError("s"),
]


@pytest.mark.usefixtures("isolated_backend_layers")
@pytest.mark.parametrize("fault", _SURFACE_FAULTS, ids=lambda f: type(f).__name__)
def test_tracker_io_surface_fault_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: Exception,
) -> None:
    """#3366: a fault surfacing the verified outcome cannot escape or re-queue."""
    runner = FakeCodexRunner()
    context = _tracker_io(tmp_path, runner, monkeypatch)
    runner.output_file_content = tracker_io_turn_result(
        "GEN-1", plan_turn_parts("findings", context)
    )
    with (
        patch("cw.codex_review_persist.surface_tracker_io_outcome", side_effect=fault),
        caplog.at_level("WARNING", logger="cw.codex_review_persist"),
    ):
        _post_linear(context)
    (record,) = [r for r in caplog.records if "tracker_io_surface_failed" in r.message]
    assert record.exc_info is not None
    assert record.exc_info[1] is fault
    assert len(runner.calls) == 1


@pytest.mark.usefixtures("isolated_backend_layers")
def test_turn_and_surface_both_fault_nothing_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#3366: the turn fault falls through to the surface; both are logged."""
    context = _tracker_io(tmp_path, FakeCodexRunner(), monkeypatch)
    with (
        patch(
            "cw.codex_review_persist.post_parts_via_turn",
            side_effect=RuntimeError("turn"),
        ),
        patch(
            "cw.codex_review_persist.surface_tracker_io_outcome",
            side_effect=OSError("surface"),
        ),
        caplog.at_level("WARNING", logger="cw.codex_review_persist"),
    ):
        _post_linear(context)
    messages = [r.message for r in caplog.records]
    assert any("tracker_io_turn_failed" in m for m in messages)
    assert any("tracker_io_surface_failed" in m for m in messages)


@pytest.mark.parametrize(
    "fault", [RuntimeError("boom"), KeyError("k"), OSError("disk")]
)
def test_tracker_io_turn_fault_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: Exception,
) -> None:
    """R2: no turn exception reaches the caller (it would revert the row)."""
    context = _tracker_io(tmp_path, FakeCodexRunner(), monkeypatch)
    with (
        patch("cw.codex_review_persist.post_parts_via_turn", side_effect=fault),
        caplog.at_level("WARNING", logger="cw.codex_review_persist"),
    ):
        _post_linear(context)
    (record,) = [r for r in caplog.records if "tracker_io_turn_failed" in r.message]
    assert record.exc_info is not None
    assert record.exc_info[1] is fault
    assert _skip_events() == []
    (event,) = _unverified_events()
    assert event.payload["reason"] == "turn_exception"
    assert event.payload["public_state"] == "possible"
    assert event.payload["posted"] == []


def test_daemon_verdict_helper_carries_the_daemon_tracker_io_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(ticket_id: str, text: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("cw.codex_review_persist._post_review_comment", fake_post)
    client = ClientConfig(name="acme", workspace_path=tmp_path, default_branch="main")
    runner = FakeCodexRunner()
    _post_daemon_verdict_comment(
        "GEN-9",
        "findings",
        client=client,
        runner=runner,
        worktree=tmp_path,
        session_id="bg-9",
        kind="verdict",
        reviewed_sha="abc",
        artifact_path=None,
    )
    context = captured["tracker_io"]
    assert isinstance(context, TrackerIoContext)
    assert context.runner is runner
    assert context.client is client
    assert context.worktree == tmp_path
    assert captured["client"] == "acme"
    assert captured["session_id"] == "bg-9"
    assert {"cwd", "tracker", "artifact_path", "reviewed_sha"} <= captured.keys()
