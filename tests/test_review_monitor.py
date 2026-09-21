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
import os
import subprocess
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


# ---------------------------------------------------------------------------
# register / drop / complete report a result (#2189)
# ---------------------------------------------------------------------------

_REPO = "acme/widgets"
_KEY = "acme/widgets#42"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point review_monitor's state at an isolated per-test directory."""
    central = tmp_path / "monitor-state"
    monkeypatch.setattr(_mod, "CENTRAL_STATE_DIR", central)
    monkeypatch.setattr(_mod, "LEGACY_STATE_FILE", tmp_path / "legacy-state.json")
    monkeypatch.setattr(_mod, "CANONICAL_REPO_PATHS", {})
    monkeypatch.delenv(_mod.CANONICAL_REPO_PATHS_ENV, raising=False)
    return central


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    """Run the real ``main()`` with *argv*; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["review_monitor.py", *argv])
    code = 0
    try:
        _mod.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _register_argv(sha: str = "abc123") -> list[str]:
    return [
        "register",
        "42",
        "--role",
        "author",
        "--repo",
        _REPO,
        "--repo-path",
        "/canon/widgets",
        "--sha",
        sha,
    ]


def test_register_cli_prints_one_line_json_on_success(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run_cli(monkeypatch, capsys, *_register_argv())

    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "registered": True,
        "key": _KEY,
        "sha": "abc123",
        "updated": False,
    }
    assert _mod.load_state(_REPO).monitored[_KEY].last_seen_sha == "abc123"


def test_register_cli_reregister_reports_updated_true(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv("abc123"))
    code, out, _err = _run_cli(monkeypatch, capsys, *_register_argv("def456"))

    assert code == 0
    assert json.loads(out) == {
        "registered": True,
        "key": _KEY,
        "sha": "def456",
        "updated": True,
    }
    pr = _mod.load_state(_REPO).monitored[_KEY]
    assert pr.last_seen_sha == "def456"
    assert pr.delta_base_sha == "def456"


def test_register_cli_with_threads_and_details_still_one_line(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run_cli(
        monkeypatch,
        capsys,
        *_register_argv(),
        "--threads",
        "t1",
        "t2",
        "--thread-details",
        '[{"id":"t1","file":"a.py","line":3}]',
    )

    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out)["registered"] is True
    pr = _mod.load_state(_REPO).monitored[_KEY]
    assert pr.our_threads == ["t1", "t2"]
    assert pr.thread_status["t1"].file == "a.py"
    assert pr.thread_status["t1"].line == 3


def test_register_cli_exits_nonzero_when_state_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A regular file where the state directory should be: the real mkdir in
    # save_state raises FileExistsError (an OSError) — no mocking needed.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(_mod, "CENTRAL_STATE_DIR", blocker)
    monkeypatch.setattr(_mod, "LEGACY_STATE_FILE", tmp_path / "legacy-state.json")
    monkeypatch.setattr(_mod, "CANONICAL_REPO_PATHS", {})
    monkeypatch.delenv(_mod.CANONICAL_REPO_PATHS_ENV, raising=False)

    code, out, err = _run_cli(monkeypatch, capsys, *_register_argv())

    assert code == 1
    assert out == ""
    assert "Error" in err
    assert _KEY in err
    assert "registered" not in out


def test_drop_cli_reports_dropped_true(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv())
    code, out, _err = _run_cli(monkeypatch, capsys, "drop", "42", "--repo", _REPO)

    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {"dropped": True, "key": _KEY}
    assert _KEY not in _mod.load_state(_REPO).monitored


def test_drop_cli_not_monitored_reports_false_exit_0(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run_cli(monkeypatch, capsys, "drop", "42", "--repo", _REPO)

    assert code == 0
    assert json.loads(out) == {"dropped": False, "key": _KEY}


def test_complete_cli_reports_completed_true_with_reason(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv())
    code, out, _err = _run_cli(
        monkeypatch, capsys, "complete", "42", "--repo", _REPO, "--reason", "approved"
    )

    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {"completed": True, "key": _KEY, "reason": "approved"}
    state = _mod.load_state(_REPO)
    assert _KEY not in state.monitored
    assert _KEY in state.completed


def test_complete_cli_not_monitored_reports_false_exit_0(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run_cli(monkeypatch, capsys, "complete", "42", "--repo", _REPO)

    assert code == 0
    assert json.loads(out) == {"completed": False, "key": _KEY, "reason": "merged"}


@pytest.mark.parametrize(
    "argv",
    [
        ["drop", "42", "--repo", _REPO],
        ["complete", "42", "--repo", _REPO],
    ],
    ids=["drop", "complete"],
)
def test_drop_and_complete_cli_exit_nonzero_on_state_write_error(
    argv: list[str],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv())

    disk_full_message = "disk full"

    def _raise_oserror(*_a: object, **_k: object) -> None:
        raise OSError(disk_full_message)

    monkeypatch.setattr(_mod, "save_state", _raise_oserror)
    code, out, err = _run_cli(monkeypatch, capsys, *argv)

    assert code == 1
    assert out == ""
    assert "Error:" in err
    assert argv[0] in err
    assert _KEY in err
    assert "disk full" in err


def test_register_subprocess_prints_result_line(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "GLOBAL_CLAUDE_REVIEW_MONITOR_DIR": str(tmp_path / "state"),
    }
    env.pop(_mod.CANONICAL_REPO_PATHS_ENV, None)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), *_register_argv()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["registered"] is True


# ---------------------------------------------------------------------------
# state-read failures must not read as successful mutations (#2189)
# ---------------------------------------------------------------------------

_UNREADABLE_MESSAGE = "state unreadable"


def _fail_reading(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make ``Path.read_text`` raise ``OSError`` for *target* only.

    Mocks the source of the exception (the read), so the write path stays
    healthy — the shape of the bug: a read that fails while a write succeeds.
    """
    real_read_text = Path.read_text

    def _read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise PermissionError(_UNREADABLE_MESSAGE)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)


@pytest.mark.parametrize(
    "argv",
    [
        _register_argv("def456"),
        ["drop", "42", "--repo", _REPO],
        ["complete", "42", "--repo", _REPO],
    ],
    ids=["register", "drop", "complete"],
)
def test_mutation_cli_exits_nonzero_when_state_unreadable(
    argv: list[str],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv("abc123"))
    state_file = _mod.state_path_for_repo(_REPO)
    before = state_file.read_bytes()

    _fail_reading(monkeypatch, state_file)
    code, out, err = _run_cli(monkeypatch, capsys, *argv)

    assert code == 1
    assert out == ""
    assert "Error:" in err
    assert argv[0] in err
    assert _KEY in err
    assert _UNREADABLE_MESSAGE in err
    # The unreadable file is left exactly as it was — never replaced by fresh state.
    assert state_file.read_bytes() == before


def test_load_state_strict_raises_on_unreadable_state_file(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, capsys, *_register_argv())
    _fail_reading(monkeypatch, _mod.state_path_for_repo(_REPO))

    with pytest.raises(OSError, match=_UNREADABLE_MESSAGE):
        _mod.load_state(_REPO, strict=True)


def test_load_state_default_degrades_to_empty_on_unreadable_state_file(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every non-mutation caller relies on this graceful fallback.
    _run_cli(monkeypatch, capsys, *_register_argv())
    _fail_reading(monkeypatch, _mod.state_path_for_repo(_REPO))

    with caplog.at_level("WARNING"):
        state = _mod.load_state(_REPO)

    assert state.monitored == {}
    assert any(_UNREADABLE_MESSAGE in r.message for r in caplog.records)


def test_load_state_strict_still_starts_fresh_on_corrupt_json(
    state_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Corrupt-but-readable content is a separate concern: strict does not touch it.
    state_dir.mkdir(parents=True)
    _mod.state_path_for_repo(_REPO).write_text("{not json")

    with caplog.at_level("WARNING"):
        state = _mod.load_state(_REPO, strict=True)

    assert state.monitored == {}
    assert any("Corrupt monitor state file" in r.message for r in caplog.records)


def test_register_cli_malformed_thread_details_exits_nonzero(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        *_register_argv(),
        "--thread-details",
        "[not json",
    )

    assert code == 1
    assert out == ""
    assert "Error:" in err
    assert _KEY in err
    assert "Traceback" not in err
    assert not _mod.state_path_for_repo(_REPO).exists()
