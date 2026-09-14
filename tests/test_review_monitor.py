"""Tests for .claude/scripts/review_monitor.py.

Uses importlib to load the script directly (it lives outside the src/ tree),
following tests/test_prep_pr_finalize.py's convention.

The ``reviews``/``comments`` fixture payloads below are hand-authored literals
restricted to documented REST fields (``id``, ``pull_request_review_id``,
``line``, ``path``, ``body``, ``state``, ``submitted_at``, ``user.login``) —
they are NOT captured from a live API response, since none was available for
this ticket.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "review_monitor.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("review_monitor", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("review_monitor", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def _make_pr() -> Any:
    return _mod.MonitoredPR(
        role="author",
        repo="acme/widgets",
        repo_path="/tmp/widgets",
        pr_number=42,
        last_seen_sha="deadbeef",
    )


def test_blank_body_own_review_reconstructed_from_inline_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = [
        {
            "pull_request_review_id": 100,
            "line": 12,
            "path": "foo.py",
            "body": "nit: rename this",
        },
        {
            "pull_request_review_id": 100,
            "line": 30,
            "path": "foo.py",
            "body": "consider a guard clause",
        },
    ]
    monkeypatch.setattr(_mod, "_run_gh", lambda *_a, **_k: json.dumps(comments))
    inline_by_review = _mod._fetch_inline_comment_bodies_by_review("acme/widgets", 42)

    pr = _make_pr()
    reviews = [
        {
            "id": 100,
            "state": "COMMENTED",
            "body": "",
            "submitted_at": "2026-09-01T00:00:00Z",
            "user": {"login": "matt-w"},
        }
    ]
    _mod._collect_new_comment_reviews(
        pr,
        reviews,
        formal_cutoff="",
        our_username="matt-w",
        inline_by_review=inline_by_review,
    )

    assert "100" in pr.comment_reviews
    body = pr.comment_reviews["100"].body
    assert "nit: rename this" in body
    assert "consider a guard clause" in body


def test_blank_body_other_authors_review_still_skipped() -> None:
    pr = _make_pr()
    reviews = [
        {
            "id": 100,
            "state": "COMMENTED",
            "body": "",
            "submitted_at": "2026-09-01T00:00:00Z",
            "user": {"login": "someone-else"},
        }
    ]
    inline_by_review = {"100": ["a comment"]}
    _mod._collect_new_comment_reviews(
        pr,
        reviews,
        formal_cutoff="",
        our_username="matt-w",
        inline_by_review=inline_by_review,
    )
    assert "100" not in pr.comment_reviews


def test_no_inline_comments_skips_extra_gh_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _make_pr()
    reviews = [
        {
            "id": 200,
            "state": "COMMENTED",
            "body": "already has content",
            "submitted_at": "2026-09-01T00:00:00Z",
            "user": {"login": "matt-w"},
        }
    ]
    calls: list[list[str]] = []

    def _fake_run_gh(args: list[str], repo: str | None = None) -> str:
        calls.append(args)
        if args and args[0] == "api" and args[1].endswith("/reviews"):
            return json.dumps(reviews)
        return ""

    monkeypatch.setattr(_mod, "_run_gh", _fake_run_gh)
    _mod._refresh_comment_reviews(
        pr, "acme/widgets", 42, sha_changed=False, our_username="matt-w"
    )

    assert len(calls) == 1
    assert not any("pulls/42/comments" in call[-1] for call in calls)


def test_outdated_line_none_comments_excluded_from_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = [
        {
            "pull_request_review_id": 100,
            "line": None,
            "path": "foo.py",
            "body": "outdated comment",
        },
        {
            "pull_request_review_id": 100,
            "line": 5,
            "path": "foo.py",
            "body": "live comment",
        },
    ]
    monkeypatch.setattr(_mod, "_run_gh", lambda *_a, **_k: json.dumps(comments))
    result = _mod._fetch_inline_comment_bodies_by_review("acme/widgets", 42)
    assert result["100"] == ["live comment"]


def test_fetch_inline_comment_bodies_handles_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mod, "_run_gh", lambda *_a, **_k: "not json")
    result = _mod._fetch_inline_comment_bodies_by_review("acme/widgets", 42)
    assert result == {}


def test_blank_body_own_review_no_matching_inline_comments_still_skipped() -> None:
    pr = _make_pr()
    reviews = [
        {
            "id": 100,
            "state": "COMMENTED",
            "body": "",
            "submitted_at": "2026-09-01T00:00:00Z",
            "user": {"login": "matt-w"},
        }
    ]
    inline_by_review = {"999": ["unrelated"]}
    _mod._collect_new_comment_reviews(
        pr,
        reviews,
        formal_cutoff="",
        our_username="matt-w",
        inline_by_review=inline_by_review,
    )
    assert "100" not in pr.comment_reviews


def test_no_env_var_falls_back_to_given_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_mod.CANONICAL_REPO_PATHS_ENV, raising=False)
    assert _mod._canonical_repo_path("acme/widgets", "/tmp/wt") == "/tmp/wt"


def test_no_env_var_falls_back_to_repo_dict_when_no_given_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_mod.CANONICAL_REPO_PATHS_ENV, raising=False)
    monkeypatch.setattr(
        _mod, "CANONICAL_REPO_PATHS", {"acme/widgets": "/canonical/widgets"}
    )
    assert _mod._canonical_repo_path("acme/widgets", "/tmp/wt") == "/canonical/widgets"


def test_env_var_overrides_given_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        _mod.CANONICAL_REPO_PATHS_ENV,
        json.dumps({"acme/widgets": "/home/x/clones/widgets"}),
    )
    assert (
        _mod._canonical_repo_path("acme/widgets", "/tmp/wt") == "/home/x/clones/widgets"
    )


def test_env_var_only_overrides_matching_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        _mod.CANONICAL_REPO_PATHS_ENV,
        json.dumps({"other/repo": "/home/x/clones/other"}),
    )
    assert _mod._canonical_repo_path("acme/widgets", "/tmp/wt") == "/tmp/wt"


def test_malformed_json_falls_back_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_mod.CANONICAL_REPO_PATHS_ENV, "{not json")
    with caplog.at_level("WARNING"):
        result = _mod._canonical_repo_path("acme/widgets", "/tmp/wt")
    assert result == "/tmp/wt"
    assert any(_mod.CANONICAL_REPO_PATHS_ENV in r.message for r in caplog.records)


def test_non_object_json_falls_back_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_mod.CANONICAL_REPO_PATHS_ENV, json.dumps(["a", "b"]))
    with caplog.at_level("WARNING"):
        result = _mod._canonical_repo_path("acme/widgets", "/tmp/wt")
    assert result == "/tmp/wt"
    assert any(_mod.CANONICAL_REPO_PATHS_ENV in r.message for r in caplog.records)


def test_non_string_value_falls_back_and_warns_with_key_and_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_mod.CANONICAL_REPO_PATHS_ENV, json.dumps({"acme/widgets": 123}))
    with caplog.at_level("WARNING"):
        result = _mod._canonical_repo_path("acme/widgets", "/tmp/wt")
    assert result == "/tmp/wt"
    messages = [r.message for r in caplog.records]
    assert any(_mod.CANONICAL_REPO_PATHS_ENV in m for m in messages)
    assert any("acme/widgets" in m for m in messages)
    assert any("int" in m for m in messages)


def test_empty_string_env_var_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_mod.CANONICAL_REPO_PATHS_ENV, "")
    with caplog.at_level("WARNING"):
        result = _mod._canonical_repo_path("acme/widgets", "/tmp/wt")
    assert result == "/tmp/wt"
    assert any(_mod.CANONICAL_REPO_PATHS_ENV in r.message for r in caplog.records)


def test_empty_path_entry_warns_and_is_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_mod.CANONICAL_REPO_PATHS_ENV, json.dumps({"acme/widgets": ""}))
    with caplog.at_level("WARNING"):
        result = _mod._canonical_repo_path("acme/widgets", "/tmp/wt")
    assert result == "/tmp/wt"
    messages = [r.message for r in caplog.records]
    assert any(_mod.CANONICAL_REPO_PATHS_ENV in m for m in messages)
    assert any("acme/widgets" in m for m in messages)
