"""Unit tests for the deterministic impl-guard staleness/regress verdict (#1794).

Structural sibling of tests/test_check_plan_scope_conformance.py — same
argparse-in/JSON-verdict-out shape, same script location convention.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "scripts"
    / "check_impl_guard_staleness.py"
)


def _run(*args: str) -> tuple[int, dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict[str, Any] = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload


def test_no_comments_no_regress_not_stale(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text("[]")
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["stale"] is False
    assert verdict["reasons"] == []


def test_comment_after_head_is_stale(tmp_path: Path) -> None:
    """The #1784 incident shape: comment at 23:28:16 EDT, HEAD at 22:53:27 EDT."""
    comments = tmp_path / "comments.json"
    # 23:28:16 EDT = 03:28:16Z the next day.
    comments.write_text(json.dumps([{"createdAt": "2026-08-11T03:28:16Z"}]))
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",  # 02:53:27Z
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["stale"] is True
    assert "stale_comment_after_head" in verdict["reasons"]


def test_comment_before_head_not_stale(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"createdAt": "2026-08-10T20:00:00Z"}]))
    _code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert verdict["stale"] is False


def test_regressed_into_stage_forces_stale_even_with_no_new_comments(
    tmp_path: Path,
) -> None:
    """#1794 R1: presence of --regressed-into-stage alone is sufficient --
    it is already a per-arrival signal (dispatch clears it after this exact
    stage's spawn), never a cumulative count to threshold against."""
    comments = tmp_path / "comments.json"
    comments.write_text("[]")
    _code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
        "--regressed-into-stage",
        "impl",
    )
    assert verdict["stale"] is True
    assert verdict["reasons"] == ["regressed_to_impl"]


def test_empty_regressed_into_stage_behaves_as_absent(tmp_path: Path) -> None:
    """jq's `// empty` on a null queue_metadata.regressed_into_stage yields an
    empty string, not an omitted flag -- must not be mistaken for "regressed"."""
    comments = tmp_path / "comments.json"
    comments.write_text("[]")
    _code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
        "--regressed-into-stage",
        "",
    )
    assert verdict["stale"] is False
    assert verdict["reasons"] == []


def test_both_signals_fire_together(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"createdAt": "2026-08-11T03:28:16Z"}]))
    _code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
        "--regressed-into-stage",
        "impl",
    )
    assert sorted(verdict["reasons"]) == [
        "regressed_to_impl",
        "stale_comment_after_head",
    ]


def test_accepts_snake_case_created_at_too(tmp_path: Path) -> None:
    """Comments re-materialized by the guard itself use created_at, not createdAt."""
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"created_at": "2026-08-11T03:28:16Z"}]))
    _code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert verdict["stale"] is True


def test_malformed_comments_file_degrades_comment_evidence_only(tmp_path: Path) -> None:
    """#1794 follow-up: a bad --comments-file no longer forces exit 2 -- it
    degrades only the comment-staleness half of the verdict."""
    comments = tmp_path / "comments.json"
    comments.write_text("not json")
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["stale"] is False
    assert verdict["comments_load_failed"] is True


def test_missing_comments_file_degrades_comment_evidence_only(tmp_path: Path) -> None:
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(tmp_path / "does-not-exist.json"),
    )
    assert code == 0
    assert verdict["stale"] is False
    assert verdict["comments_load_failed"] is True


def test_non_list_comments_file_degrades_comment_evidence_only(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps({"comments": []}))
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["stale"] is False
    assert verdict["comments_load_failed"] is True


def test_malformed_comments_file_does_not_mask_regress_marker(tmp_path: Path) -> None:
    """The money test: a broken comments fetch must not discard an
    independently-sourced, valid --regressed-into-stage signal (GitHub #1794
    follow-up -- this is the exact defect class the ticket exists to fix,
    reintroduced under a narrower trigger by the original exit-2 design)."""
    comments = tmp_path / "comments.json"
    comments.write_text("not json")
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
        "--regressed-into-stage",
        "impl",
    )
    assert code == 0
    assert verdict["stale"] is True
    assert verdict["reasons"] == ["regressed_to_impl"]
    assert verdict["comments_load_failed"] is True


def test_malformed_head_commit_at_exits_2(tmp_path: Path) -> None:
    """Fail open on an unparseable git timestamp, same convention as a
    malformed comments file — the caller treats exit 2 as "cannot determine"."""
    comments = tmp_path / "comments.json"
    comments.write_text("[]")
    code, _verdict = _run(
        "--head-commit-at",
        "not-a-timestamp",
        "--comments-file",
        str(comments),
    )
    assert code == 2


def test_missing_comments_field_in_entry_is_skipped_not_fatal(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"body": "no timestamp here"}]))
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["stale"] is False


def test_verdict_reports_newest_comment_and_head(tmp_path: Path) -> None:
    """The verdict is the operator-facing audit surface: it must echo both
    timestamps it compared, plus the raw regress marker it was handed."""
    comments = tmp_path / "comments.json"
    comments.write_text(
        json.dumps(
            [
                {"createdAt": "2026-08-09T10:00:00Z"},
                {"createdAt": "2026-08-11T03:28:16Z"},
                {"createdAt": "2026-08-10T10:00:00Z"},
            ]
        )
    )
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
        "--regressed-into-stage",
        "impl",
    )
    assert code == 0
    assert verdict["head_commit_at"] == "2026-08-10T22:53:27-04:00"
    assert verdict["newest_comment_at"] == "2026-08-11T03:28:16Z"
    assert verdict["regressed_into_stage"] == "impl"


def test_newest_comment_at_is_null_when_no_timestamps(tmp_path: Path) -> None:
    comments = tmp_path / "comments.json"
    comments.write_text("[]")
    code, verdict = _run(
        "--head-commit-at",
        "2026-08-10T22:53:27-04:00",
        "--comments-file",
        str(comments),
    )
    assert code == 0
    assert verdict["newest_comment_at"] is None
    assert verdict["regressed_into_stage"] == ""
