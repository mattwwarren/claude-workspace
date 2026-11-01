"""Tests for cw.codex_review_persist — a review verdict's durable copies and post.

Relocated with the module itself out of ``tests/test_codex_background.py``
(#2807): the worktree artifacts (``_persist_review_verdict``,
``_persist_structured_review_verdict``, ``_persist_unparseable_artifact``) and
the best-effort tracker comment (``_post_review_comment``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cw.auto_dev_result import Blocker
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE
from cw.codex_review_persist import (
    REVIEW_VERDICT_OWNER_STAMP_FORMAT,
    _post_review_comment,
)
from cw.review_findings import ReviewVerdictEnvelope, consolidate_verdict
from tests.conftest import _make_diff, _make_finding, _make_reviewer_doc

# ---------------------------------------------------------------------------
# _post_review_comment — Step 4b's best-effort GitHub write
# ---------------------------------------------------------------------------


def test_post_review_comment_suppresses_oserror() -> None:
    """_post_review_comment swallows OSError from a missing gh binary."""
    with patch("cw.gh._sp.run", side_effect=FileNotFoundError("no gh")):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_suppresses_timeout() -> None:
    """_post_review_comment swallows TimeoutExpired when gh hangs."""
    with patch(
        "cw.gh._sp.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_forwards_cwd() -> None:
    """#1279: _post_review_comment scopes the gh call to the client's repo."""
    want_cwd = Path("/some/client-a/repo")
    with patch("cw.codex_review_persist.post_issue_comment") as post_mock:
        _post_review_comment("T-1", "findings", cwd=want_cwd)
    post_mock.assert_called_once_with("T-1", "findings", cwd=want_cwd)


def test_post_review_comment_logs_on_none_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gh call couldn't run at all (missing binary / timeout) -> warning, not silent."""
    with (
        patch("cw.codex_review_persist.post_issue_comment", return_value=None),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None)
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
            "GEN-1", "findings", cwd=None, tracker="linear", artifact_path=artifact
        )
    post_mock.assert_not_called()
    assert any(
        "review_comment_skipped" in r.message
        and "GEN-1" in r.message
        and "linear" in r.message
        and str(artifact) in r.message
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
        _post_review_comment("T-1", "findings", cwd=None, tracker=tracker)
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
        _post_review_comment("T-1", "findings", cwd=None)
    assert any(
        "T-1" in r.message and "1" in r.message and "invalid issue format" in r.message
        for r in caplog.records
    )
