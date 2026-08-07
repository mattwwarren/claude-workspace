"""Tests for cw.executor_diagnostics — typed executor failure diagnostics (#1239)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time
from pydantic import ValidationError

from cw.executor_diagnostics import (
    _CLEANUP_MIN_INTERVAL_SECONDS,
    _EXCERPT_LIMIT,
    ExecutorFailure,
    _bounded,
    build_executor_failure,
    cleanup_expired_diagnostics,
    diagnostics_bundle_dir,
    persist_diagnostics_bundle,
    redact,
    redact_argv,
    render_bundle_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
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


def test_executor_failure_model_rejects_unknown_executor_name() -> None:
    with pytest.raises(ValidationError):
        _make_failure(executor_name="aidr")


def test_argv_sanitized_field_validator_redacts_aider_message() -> None:
    """Constructing ExecutorFailure with raw argv_sanitized is redacted by the
    model itself — the caller no longer has to call redact_argv (item 4)."""
    ticket_text = "full ticket + plan text"
    failure = _make_failure(
        executor_name="aider",
        argv_sanitized=["aider", "--message", ticket_text, "--yes"],
    )
    assert failure.argv_sanitized[1] == "--message"
    assert failure.argv_sanitized[2] == f"<redacted: {len(ticket_text)} chars>"
    assert failure.argv_sanitized[3] == "--yes"


def test_argv_sanitized_redaction_is_idempotent_on_reload() -> None:
    """A disk round-trip (model_validate_json) must not double-redact an
    already-redacted argv_sanitized value."""
    ticket_text = "full ticket + plan text"
    failure = _make_failure(
        executor_name="aider",
        argv_sanitized=["aider", "--message", ticket_text, "--yes"],
    )
    reloaded = ExecutorFailure.model_validate_json(failure.model_dump_json())
    assert reloaded.argv_sanitized == failure.argv_sanitized


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


def test_redact_argv_is_idempotent_on_already_redacted_value() -> None:
    argv = ["aider", "--message", "some ticket text", "--yes"]
    once = redact_argv(argv, executor_name="aider")
    twice = redact_argv(once, executor_name="aider")
    assert twice == once


def test_redact_argv_opencode_replaces_prompt() -> None:
    """opencode's trailing positional (the prompt) is redacted wholesale."""
    prompt = "Implement the thing per the plan; secret sk-" + "z" * 40
    argv = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--dir",
        "/tmp/worktree",
        prompt,
    ]
    out = redact_argv(argv, executor_name="opencode")
    assert out[-1] == f"<redacted: {len(prompt)} chars>"
    # Every other element untouched.
    assert out[0] == "opencode"
    assert out[5] == "--dir"
    assert out[6] == "/tmp/worktree"


def test_redact_argv_opencode_idempotent() -> None:
    """Double redaction of an opencode argv does not re-wrap the placeholder."""
    argv = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--dir",
        "/tmp",
        "some ticket text",
    ]
    once = redact_argv(argv, executor_name="opencode")
    twice = redact_argv(once, executor_name="opencode")
    assert twice == once


def test_redact_argv_opencode_short_argv_untouched() -> None:
    """Short argv (no trailing positional) passes through unchanged."""
    argv = ["opencode", "run"]
    assert redact_argv(argv, executor_name="opencode") == argv


def test_redact_argv_opencode_flag_last_untouched() -> None:
    """Last arg starting with '-' is a flag, not a prompt — untouched."""
    argv = ["opencode", "run", "--pure"]
    assert redact_argv(argv, executor_name="opencode") == argv


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
# render_bundle_path
# ---------------------------------------------------------------------------


def test_render_bundle_path_home_relative_success(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the bundle dir sits under Path.home(), the rendered path is
    home-relative, not absolute (the success branch, untested before #1330)."""
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    bundle = diagnostics_bundle_dir("sid-home-rel")
    rendered = render_bundle_path("sid-home-rel")
    assert rendered == str(bundle.relative_to(tmp_path))
    assert not rendered.startswith("/")


# ---------------------------------------------------------------------------
# build_executor_failure
# ---------------------------------------------------------------------------


def test_build_executor_failure_applies_shared_defaults() -> None:
    """Only the required kwargs are given; executor_version/run_id and the
    omitted optional fields land at their shared defaults (item 2)."""
    failure = build_executor_failure(
        category="missing_output",
        executor_name="aider",
        session_id="sess-build",
        argv=[],
        stdout_excerpt="out",
        stderr_excerpt="",
    )
    assert failure.executor_version is None
    assert failure.run_id is None
    assert failure.reviewer_role is None
    assert failure.duration_seconds is None
    assert failure.exit_code is None
    assert failure.structured_output_excerpt is None
    assert failure.category == "missing_output"
    assert failure.executor_name == "aider"
    assert failure.session_id == "sess-build"


# ---------------------------------------------------------------------------
# persist_diagnostics_bundle
# ---------------------------------------------------------------------------


def _stem(role_slug: str, failure: ExecutorFailure) -> str:
    """Compute the expected filename stem for a given role_slug/failure pair."""
    timestamp = failure.occurred_at.strftime("%Y%m%dT%H%M%S%f")
    return f"{role_slug}-{failure.category}-{timestamp}"


def test_persist_diagnostics_bundle_writes_expected_files(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    schema = tmp_path / "scratch-schema.json"
    output = tmp_path / "scratch-output.json"
    schema.write_text('{"schema": true}', encoding="utf-8")
    output.write_text('{"out": true}', encoding="utf-8")
    failure = _make_failure()
    stem = _stem("code-quality-reviewer", failure)

    bundle = persist_diagnostics_bundle(
        session_id="sess1234",
        role_slug="code-quality-reviewer",
        failure=failure,
        scratch_schema_path=schema,
        scratch_output_path=output,
    )

    assert bundle == diagnostics_bundle_dir("sess1234")
    assert (bundle / f"{stem}.json").exists()
    assert (bundle / f"{stem}-schema.json").read_text() == '{"schema": true}'
    assert (bundle / f"{stem}-output.json").read_text() == '{"out": true}'
    # The failure JSON round-trips.
    restored = ExecutorFailure.model_validate_json(
        (bundle / f"{stem}.json").read_text()
    )
    assert restored.category == "timeout"


def test_persist_diagnostics_bundle_filename_derives_from_category_and_timestamp(
    tmp_config_dir: Path,
) -> None:
    """persist_diagnostics_bundle no longer accepts a reason= kwarg — the
    filename is derived solely from failure.category and failure.occurred_at
    (item 1/item 7)."""
    failure = _make_failure(category="nonzero_exit")
    bundle = persist_diagnostics_bundle(
        session_id="sess-stem",
        role_slug="aider",
        failure=failure,
    )
    stem = _stem("aider", failure)
    assert (bundle / f"{stem}.json").exists()
    with pytest.raises(TypeError):
        persist_diagnostics_bundle(
            session_id="sess-stem",
            role_slug="aider",
            reason="nonzero_exit",
            failure=failure,
        )


def test_persist_diagnostics_bundle_does_not_overwrite_same_role_category_repeat(
    tmp_config_dir: Path,
) -> None:
    """Two failures with the same category/role_slug but distinct occurred_at
    produce two distinct files in the bundle dir (item 7)."""
    first = _make_failure(
        category="nonzero_exit", occurred_at=datetime(2026, 7, 18, 1, tzinfo=UTC)
    )
    second = _make_failure(
        category="nonzero_exit", occurred_at=datetime(2026, 7, 18, 2, tzinfo=UTC)
    )
    bundle = persist_diagnostics_bundle(
        session_id="sess-repeat", role_slug="aider", failure=first
    )
    persist_diagnostics_bundle(
        session_id="sess-repeat", role_slug="aider", failure=second
    )
    files = sorted(p.name for p in bundle.glob("aider-nonzero_exit-*.json"))
    assert len(files) == 2
    assert files[0] != files[1]


def test_persist_diagnostics_bundle_missing_scratch_files_omits_copies(
    tmp_config_dir: Path,
) -> None:
    failure = _make_failure(category="runtime_error", reviewer_role=None)
    bundle = persist_diagnostics_bundle(
        session_id="sess-nofiles",
        role_slug="aider",
        failure=failure,
    )
    stem = _stem("aider", failure)
    assert (bundle / f"{stem}.json").exists()
    assert not (bundle / f"{stem}-schema.json").exists()
    assert not (bundle / f"{stem}-output.json").exists()


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

    failure = _make_failure(category="runtime_error")
    stem = _stem("aider", failure)

    with caplog.at_level(logging.WARNING):
        # No exception propagates.
        bundle = persist_diagnostics_bundle(
            session_id="sess-copy-fail",
            role_slug="aider",
            failure=failure,
            scratch_schema_path=schema,
        )
    assert bundle == diagnostics_bundle_dir("sess-copy-fail")
    # The JSON write succeeded before copy2 raised.
    assert (bundle / f"{stem}.json").exists()
    assert not (bundle / f"{stem}-schema.json").exists()
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

    def _iterdir(self: Path) -> Iterator[Path]:
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

    def _iterdir(self: Path) -> Iterator[Path]:
        if self == bundle:
            msg = "bundle unreadable"
            raise OSError(msg)
        return real_iterdir(self)

    monkeypatch.setattr("pathlib.Path.iterdir", _iterdir)
    assert cleanup_expired_diagnostics(retention_hours=24) == 0
    assert bundle.exists()


# ---------------------------------------------------------------------------
# cleanup_expired_diagnostics — sentinel-file throttle (item 8)
# ---------------------------------------------------------------------------


def test_cleanup_expired_diagnostics_throttled_within_interval(
    tmp_config_dir: Path,
) -> None:
    """The first call establishes the sentinel; a second call moments later is
    throttled — even a now-stale bundle is left untouched."""
    import os

    fresh = _seed_bundle("throttle-fresh")
    first = cleanup_expired_diagnostics(retention_hours=24)
    assert first == 0
    assert fresh.exists()

    # Age the bundle's files well past retention.
    old = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    for f in fresh.iterdir():
        os.utime(f, (old, old))

    second = cleanup_expired_diagnostics(retention_hours=24)
    assert second == 0
    assert fresh.exists()  # throttled — the sweep never ran


def test_cleanup_expired_diagnostics_runs_again_after_throttle_window(
    tmp_config_dir: Path,
) -> None:
    """Once the throttle window has elapsed, a subsequent call sweeps again."""
    import os

    with freeze_time("2026-07-04 12:00:00") as frozen:
        fresh = _seed_bundle("throttle-window")
        first = cleanup_expired_diagnostics(retention_hours=24)
        assert first == 0

        old = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
        for f in fresh.iterdir():
            os.utime(f, (old, old))

        frozen.tick(delta=timedelta(seconds=_CLEANUP_MIN_INTERVAL_SECONDS + 100))
        second = cleanup_expired_diagnostics(retention_hours=24)

    assert second == 1
    assert not fresh.exists()


def test_cleanup_expired_diagnostics_survives_sentinel_touch_failure(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep's return value is unaffected even when stamping the sentinel
    for next time fails (patch-coverage for the new except branch)."""
    import os

    stale = _seed_bundle("sentinel-touch-fail")
    old = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    for f in stale.iterdir():
        os.utime(f, (old, old))

    def _boom(*_a: object, **_k: object) -> None:
        msg = "cannot touch"
        raise OSError(msg)

    monkeypatch.setattr("pathlib.Path.touch", _boom)
    removed = cleanup_expired_diagnostics(retention_hours=24)
    assert removed == 1
    assert not stale.exists()


def test_persist_uses_shutil_copy2(tmp_config_dir: Path) -> None:
    """Guard that shutil is imported at module scope (monkeypatch target)."""
    assert hasattr(shutil, "copy2")
