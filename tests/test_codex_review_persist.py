"""Tests for cw.codex_review_persist — a review verdict's durable copies and enqueue.

Relocated with the module itself out of ``tests/test_codex_background.py``
(#2807): the worktree artifacts (``_persist_review_verdict``,
``_persist_structured_review_verdict``, ``_persist_unparseable_artifact``) and
the best-effort outbox enqueue of the verdict comment
(``_post_daemon_verdict_comment``, RFC 0019 O3, #3707).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from cw import codex_review_persist
from cw.auto_dev_result import Blocker
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE
from cw.codex_review_persist import (
    REVIEW_VERDICT_OWNER_STAMP_FORMAT,
    VERDICT_PRODUCER,
    _post_daemon_verdict_comment,
)
from cw.codex_review_signals import VerdictCommentKind
from cw.events import read_events
from cw.models import ClientConfig, OrchestratorEvent, OrchestratorEventType
from cw.models.client import OutboxConfig
from cw.outbox import MAX_PART_CHARS, OutboxRole
from cw.review_findings import ReviewVerdictEnvelope, consolidate_verdict
from tests._doctor_helpers import make_client
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.outbox_builders import entries_for
from tests._shared.review_builders import _make_diff, _make_finding, _make_reviewer_doc
from tests._tool_fakes import FakeGhCli

# ---------------------------------------------------------------------------
# _post_daemon_verdict_comment — the verdict comment is enqueued, never posted
# ---------------------------------------------------------------------------

_CLIENT = "acme"
_TICKET = "T-1"
_SHA = "abc123def4567890abcdef1234567890abcdef12"


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGhCli]:
    """A scripted ``subprocess.run``; the test fails if any ``gh`` call was made."""
    fake = FakeGhCli()
    monkeypatch.setattr("subprocess.run", fake)
    yield fake
    assert fake.calls == []


def _client(tmp_path: Path, tracker: str | None = None) -> ClientConfig:
    if tracker is not None:
        _write_tracker_config(tmp_path, tracker)
    return make_client(name=_CLIENT, workspace_path=tmp_path)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _post(
    client: ClientConfig,
    text: str = "findings",
    *,
    ticket: str = _TICKET,
    kind: VerdictCommentKind = "verdict",
    reviewed_sha: str | None = _SHA,
    artifact_path: Path | None = None,
) -> None:
    _post_daemon_verdict_comment(
        ticket,
        text,
        client=client,
        session_id="sess-1",
        kind=kind,
        reviewed_sha=reviewed_sha,
        artifact_path=artifact_path,
    )


def _skip_events() -> list[OrchestratorEvent]:
    return read_events(
        consumer="test-3707-skip",
        event_types=[OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED],
    )


def test_verdict_enqueues(tmp_path: Path, gh: FakeGhCli) -> None:
    _post(_client(tmp_path), "findings")

    ((entry, parts),) = entries_for(_CLIENT)
    assert VERDICT_PRODUCER == "codex-verdict"
    assert entry.producer == "codex-verdict"
    assert entry.role is OutboxRole.TICKETS
    assert entry.ticket == _TICKET
    assert entry.marker == f"verdict-{_SHA[:12]}-{_digest('findings')}"
    assert parts == ["findings"]
    assert entry.operator_authored is False
    assert entry.redacted is False
    assert _skip_events() == []


def test_unparseable_enqueues(tmp_path: Path, gh: FakeGhCli) -> None:
    client = _client(tmp_path)
    _post(client, "no documents", kind="unparseable")
    _post(client, "no documents", kind="unparseable", reviewed_sha=None, ticket="T-2")

    (first, second) = entries_for(_CLIENT)
    assert first.entry.marker == f"unparseable-{_SHA[:12]}-{_digest('no documents')}"
    assert second.entry.marker == f"unparseable-none-{_digest('no documents')}"
    assert first.parts == ["no documents"]
    assert _skip_events() == []


def test_identical_retry_dedups_different_text_posts(
    tmp_path: Path, gh: FakeGhCli
) -> None:
    client = _client(tmp_path)
    _post(client, "findings")
    ((existing, _),) = entries_for(_CLIENT)
    assert _skip_events() == []

    _post(client, "findings")
    (event,) = _skip_events()
    assert event.payload["reason"] == "already_posted"
    assert event.payload["entry_id"] == existing.id
    assert len(entries_for(_CLIENT)) == 1

    _post(client, "different findings")
    assert len(entries_for(_CLIENT)) == 2
    assert len(_skip_events()) == 1


def test_enqueue_failure_emits_skipped_outbox_enqueue_failed(
    tmp_path: Path, gh: FakeGhCli, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path, "linear")
    artifact = tmp_path / ".claude" / "review-verdict.md"
    with (
        patch("cw.outbox_producer.enqueue", side_effect=OSError("disk full")),
        caplog.at_level("WARNING", logger="cw.codex_review_persist"),
    ):
        _post(client, artifact_path=artifact)

    (event,) = _skip_events()
    assert event.correlation_id == _TICKET
    assert list(event.payload.items()) == [
        ("ticket_id", _TICKET),
        ("client", _CLIENT),
        ("tracker", "linear"),
        ("backend", "codex"),
        ("kind", "verdict"),
        ("reason", "outbox_enqueue_failed"),
        ("artifact_path", str(artifact)),
        ("reviewed_sha", _SHA),
        ("session_id", "sess-1"),
        ("entry_id", None),
    ]
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("review_comment_skipped ")
        and "linear" in m
        and "outbox_enqueue_failed" in m
        and str(artifact) in m
        for m in messages
    )
    assert entries_for(_CLIENT) == []


def test_unset_tracker_label_is_github_issues(tmp_path: Path, gh: FakeGhCli) -> None:
    client = _client(tmp_path)
    with patch("cw.outbox_producer.enqueue", side_effect=OSError("x")):
        _post(client)

    (event,) = _skip_events()
    assert event.payload["tracker"] == "github-issues"


def test_body_too_long_emits_skipped(tmp_path: Path, gh: FakeGhCli) -> None:
    _post(_client(tmp_path), "x" * (MAX_PART_CHARS + 1))

    (event,) = _skip_events()
    assert event.payload["reason"] == "body_too_long"
    assert event.payload["entry_id"] is None
    assert entries_for(_CLIENT) == []


@pytest.mark.parametrize(
    ("tracker", "label"),
    [(None, "github-issues"), ("github-issues", "github-issues"), ("linear", "linear")],
)
def test_github_unset_and_linear_clients_all_enqueue(
    tmp_path: Path, gh: FakeGhCli, tracker: str | None, label: str
) -> None:
    client = _client(tmp_path, tracker)
    _post(client, "findings")
    assert len(entries_for(_CLIENT, ticket=_TICKET)) == 1
    assert _skip_events() == []

    with patch("cw.outbox_producer.enqueue", side_effect=OSError("x")):
        _post(client, "findings", ticket="T-2")

    (event,) = _skip_events()
    assert event.payload["tracker"] == label


def test_event_failure_is_best_effort(
    tmp_path: Path, gh: FakeGhCli, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing event write never escapes _post_daemon_verdict_comment (A15)."""
    client = _client(tmp_path, "linear")
    with (
        patch("cw.outbox_producer.enqueue", side_effect=OSError("x")),
        patch(
            "cw.codex_review_signals.record_event",
            side_effect=OSError("inbox locked"),
        ),
        caplog.at_level("WARNING"),
    ):
        _post(client)

    assert any(
        "codex_review_signal_failed" in r.message and _TICKET in r.message
        for r in caplog.records
    )


def test_enabled_false_client_still_enqueues(tmp_path: Path, gh: FakeGhCli) -> None:
    client = _client(tmp_path).model_copy(
        update={"outbox": OutboxConfig(enabled=False)}
    )
    _post(client)

    assert len(entries_for(_CLIENT)) == 1
    assert _skip_events() == []


def test_no_review_comment_gh_path_remains() -> None:
    assert not hasattr(codex_review_persist, "_post_review_comment")
    assert not hasattr(codex_review_persist, "_post_via_tracker_io")


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
