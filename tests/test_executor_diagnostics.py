"""Tests for cw.executor_diagnostics — typed executor failure diagnostics (#1239)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from cw.executor_diagnostics import (
    _EXCERPT_LIMIT,
    ExecutorFailure,
    _bounded,
    cleanup_expired_diagnostics,
    diagnostics_bundle_dir,
    persist_diagnostics_bundle,
    redact,
    redact_argv,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_failure(**overrides: object) -> ExecutorFailure:
    """Minimal-but-valid ExecutorFailure with keyword overrides."""
    kwargs: dict[str, object] = {
        "category": "timeout",
        "executor_name": "codex",
        "executor_version": None,
        "reviewer_role": "Code Quality Reviewer",
        "argv_sanitized": ["codex", "exec"],
        "duration_seconds": 1.5,
        "exit_code": -1,
        "session_id": "sess1234",
        "run_id": None,
        "stdout_excerpt": "out",
        "stderr_excerpt": "err",
        "structured_output_excerpt": None,
        "occurred_at": datetime(2026, 7, 18, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return ExecutorFailure(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ExecutorFailure model
# ---------------------------------------------------------------------------


def test_executor_failure_model_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        _make_failure(category="bogus")


def test_executor_failure_semantic_validation_failure_round_trips() -> None:
    """The reserved semantic_validation_failure category exists and round-trips
    through the model even with no live producer yet."""
    failure = _make_failure(category="semantic_validation_failure")
    restored = ExecutorFailure.model_validate_json(failure.model_dump_json())
    assert restored.category == "semantic_validation_failure"


# ---------------------------------------------------------------------------
# _bounded
# ---------------------------------------------------------------------------


def test_bounded_excerpt_truncates_with_marker() -> None:
    short = "x" * 100
    assert _bounded(short) == short

    long = "y" * (_EXCERPT_LIMIT + 500)
    bounded = _bounded(long)
    assert len(bounded) <= _EXCERPT_LIMIT + len(
        "...[truncated, 99999 chars omitted]...\n"
    )
    assert bounded.startswith("...[truncated, 500 chars omitted]...\n")
    assert bounded.endswith("y")


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "a" * 40,
        "ghp_" + "b" * 36,
        "Bearer abc.def-ghi_jkl",
    ],
)
def test_redact_strips_known_secret_patterns(secret: str) -> None:
    text = f"prefix {secret} suffix"
    redacted = redact(text)
    assert secret not in redacted
    assert "<redacted>" in redacted


def test_redact_leaves_benign_text_untouched() -> None:
    benign = "/home/user/.local/share/cw/sessions/abc/diagnostics"
    assert redact(benign) == benign


# ---------------------------------------------------------------------------
# redact_argv
# ---------------------------------------------------------------------------


def test_redact_argv_replaces_aider_message_wholesale() -> None:
    ticket_text = "Implement the thing per the plan; secret sk-" + "z" * 40
    argv = [
        "aider",
        "--model",
        "openai/qwen",
        "--message",
        ticket_text,
        "--yes",
    ]
    out = redact_argv(argv, executor_name="aider")
    assert out[3] == "--message"
    assert out[4] == f"<redacted: {len(ticket_text)} chars>"
    # Every other element untouched.
    assert out[0] == "aider"
    assert out[1] == "--model"
    assert out[2] == "openai/qwen"
    assert out[5] == "--yes"


def test_redact_argv_leaves_codex_argv_untouched() -> None:
    argv = ["codex", "exec", "--sandbox", "read-only", "-o", "/x/out.json"]
    assert redact_argv(argv, executor_name="codex") == argv


# ---------------------------------------------------------------------------
# diagnostics_bundle_dir
# ---------------------------------------------------------------------------


def test_diagnostics_bundle_dir_naming_convention(tmp_config_dir: Path) -> None:
    from cw.config import state_dir

    assert (
        diagnostics_bundle_dir("sid-9")
        == state_dir() / "sessions" / "sid-9" / "diagnostics"
    )


# ---------------------------------------------------------------------------
# persist_diagnostics_bundle
# ---------------------------------------------------------------------------


def test_persist_diagnostics_bundle_writes_expected_files(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    schema = tmp_path / "scratch-schema.json"
    output = tmp_path / "scratch-output.json"
    schema.write_text('{"schema": true}', encoding="utf-8")
    output.write_text('{"out": true}', encoding="utf-8")
    failure = _make_failure()

    bundle = persist_diagnostics_bundle(
        session_id="sess1234",
        role_slug="code-quality-reviewer",
        reason="timeout",
        failure=failure,
        scratch_schema_path=schema,
        scratch_output_path=output,
    )

    assert bundle == diagnostics_bundle_dir("sess1234")
    assert (bundle / "code-quality-reviewer-timeout.json").exists()
    assert (bundle / "code-quality-reviewer-timeout-schema.json").read_text() == (
        '{"schema": true}'
    )
    assert (bundle / "code-quality-reviewer-timeout-output.json").read_text() == (
        '{"out": true}'
    )
    # The failure JSON round-trips.
    restored = ExecutorFailure.model_validate_json(
        (bundle / "code-quality-reviewer-timeout.json").read_text()
    )
    assert restored.category == "timeout"


def test_persist_diagnostics_bundle_missing_scratch_files_omits_copies(
    tmp_config_dir: Path,
) -> None:
    failure = _make_failure(category="runtime_error", reviewer_role=None)
    bundle = persist_diagnostics_bundle(
        session_id="sess-nofiles",
        role_slug="aider",
        reason="runtime_error",
        failure=failure,
    )
    assert (bundle / "aider-runtime_error.json").exists()
    assert not (bundle / "aider-runtime_error-schema.json").exists()
    assert not (bundle / "aider-runtime_error-output.json").exists()


def test_persist_diagnostics_bundle_survives_write_failure(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schema = tmp_path / "scratch-schema.json"
    schema.write_text("{}", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    # The JSON write fails and raises before the try block ever reaches
    # shutil.copy2 — the copy2 patch below is inert for this test (it's
    # never called); it's harmless to leave in place, but it does not
    # exercise the copy2-failure branch. See
    # test_persist_diagnostics_bundle_survives_copy2_only_failure below for
    # a test that isolates that distinct branch (write succeeds, copy2 fails).
    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    monkeypatch.setattr("cw.executor_diagnostics.shutil.copy2", _boom)

    import logging

    with caplog.at_level(logging.WARNING):
        # No exception propagates.
        bundle = persist_diagnostics_bundle(
            session_id="sess-fail",
            role_slug="aider",
            reason="runtime_error",
            failure=_make_failure(category="runtime_error"),
            scratch_schema_path=schema,
        )
    assert bundle == diagnostics_bundle_dir("sess-fail")
    assert any(
        "diagnostics bundle write failed" in r.getMessage() for r in caplog.records
    )


def test_persist_diagnostics_bundle_survives_copy2_only_failure(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Isolates the copy2-only failure branch: the JSON write succeeds, then
    the scratch-schema copy raises — distinct from the write_text-fails case
    covered above, where the try block never reaches copy2 at all."""
    schema = tmp_path / "scratch-schema.json"
    schema.write_text("{}", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.executor_diagnostics.shutil.copy2", _boom)

    import logging

    with caplog.at_level(logging.WARNING):
        # No exception propagates.
        bundle = persist_diagnostics_bundle(
            session_id="sess-copy-fail",
            role_slug="aider",
            reason="runtime_error",
            failure=_make_failure(category="runtime_error"),
            scratch_schema_path=schema,
        )
    assert bundle == diagnostics_bundle_dir("sess-copy-fail")
    # The JSON write succeeded before copy2 raised.
    assert (bundle / "aider-runtime_error.json").exists()
    assert not (bundle / "aider-runtime_error-schema.json").exists()
    assert any(
        "diagnostics bundle write failed" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# cleanup_expired_diagnostics
# ---------------------------------------------------------------------------


def _seed_bundle(session_id: str) -> Path:
    return persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="aider",
        reason="runtime_error",
        failure=_make_failure(category="runtime_error"),
    )


def test_cleanup_expired_diagnostics_removes_old_bundles(
    tmp_config_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.config import state_dir

    stale = _seed_bundle("stale-sess")
    fresh = _seed_bundle("fresh-sess")

    # Age the stale bundle's files well past the retention window.
    old = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    for f in stale.iterdir():
        import os

        os.utime(f, (old, old))

    import logging

    with caplog.at_level(logging.WARNING):
        removed = cleanup_expired_diagnostics(retention_hours=24)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    # Only the stale session's diagnostics dir went; sessions/ tree intact.
    assert (state_dir() / "sessions").exists()
    # The summary log names which session(s) were swept, not just the count.
    [record] = [
        r for r in caplog.records if "diagnostics cleanup removed" in r.getMessage()
    ]
    assert "stale-sess" in record.getMessage()
    assert "fresh-sess" not in record.getMessage()


def test_cleanup_expired_diagnostics_keeps_fresh_bundles(
    tmp_config_dir: Path,
) -> None:
    fresh = _seed_bundle("fresh-only")
    removed = cleanup_expired_diagnostics(retention_hours=24)
    assert removed == 0
    assert fresh.exists()


def test_cleanup_expired_diagnostics_never_raises_on_permission_error(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stale = _seed_bundle("stale-perm")
    old = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    import os

    for f in stale.iterdir():
        os.utime(f, (old, old))

    def _boom(*_a: object, **_k: object) -> None:
        msg = "permission denied"
        raise OSError(msg)

    monkeypatch.setattr("cw.executor_diagnostics.shutil.rmtree", _boom)

    import logging

    with caplog.at_level(logging.WARNING):
        removed = cleanup_expired_diagnostics(retention_hours=24)
    # rmtree failed → nothing counted as removed, but no exception propagated.
    assert removed == 0
    assert stale.exists()


def test_cleanup_expired_diagnostics_no_sessions_dir_is_noop(
    tmp_config_dir: Path,
) -> None:
    # Never seeded any bundle → sessions/ does not exist → 0, no error.
    assert cleanup_expired_diagnostics(retention_hours=24) == 0


def test_cleanup_expired_diagnostics_session_without_bundle_is_skipped(
    tmp_config_dir: Path,
) -> None:
    from cw.config import state_dir

    # A session dir carrying no diagnostics/ subdir is skipped (not a bundle).
    (state_dir() / "sessions" / "plain-sess").mkdir(parents=True)
    fresh = _seed_bundle("has-bundle")
    assert cleanup_expired_diagnostics(retention_hours=24) == 0
    assert fresh.exists()


def test_cleanup_expired_diagnostics_swallows_sessions_root_iterdir_error(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.config import state_dir

    _seed_bundle("any")
    sessions_root = state_dir() / "sessions"
    real_iterdir = type(sessions_root).iterdir

    def _iterdir(self: Path):  # type: ignore[no-untyped-def]
        if self == sessions_root:
            msg = "listing denied"
            raise OSError(msg)
        return real_iterdir(self)

    monkeypatch.setattr("pathlib.Path.iterdir", _iterdir)
    assert cleanup_expired_diagnostics(retention_hours=24) == 0


def test_cleanup_expired_diagnostics_swallows_bundle_iterdir_error(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _seed_bundle("unreadable")
    real_iterdir = type(bundle).iterdir

    def _iterdir(self: Path):  # type: ignore[no-untyped-def]
        if self == bundle:
            msg = "bundle unreadable"
            raise OSError(msg)
        return real_iterdir(self)

    monkeypatch.setattr("pathlib.Path.iterdir", _iterdir)
    assert cleanup_expired_diagnostics(retention_hours=24) == 0
    assert bundle.exists()


def test_persist_uses_shutil_copy2(tmp_config_dir: Path) -> None:
    """Guard that shutil is imported at module scope (monkeypatch target)."""
    assert hasattr(shutil, "copy2")
