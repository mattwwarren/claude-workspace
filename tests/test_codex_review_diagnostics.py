"""Tests for shared codex-review session diagnostics persistence."""

from __future__ import annotations

import json

import pytest

from cw.codex_review._diagnostics import _persist_session_diagnostics_json
from cw.config import diagnostics_dir


def test_writes_base_and_discriminated_filenames() -> None:
    for discriminator, expected in (
        (None, "profile.json"),
        ("high", "profile-high.json"),
    ):
        _persist_session_diagnostics_json(
            session_id="s-shared-diag",
            filename="profile.json",
            payload={"value": discriminator},
            log_label="test profile",
            discriminator=discriminator,
        )
        data = json.loads(
            (diagnostics_dir("s-shared-diag") / expected).read_text(encoding="utf-8")
        )
        assert data["session_id"] == "s-shared-diag"
        assert data["value"] == discriminator
        assert data["recorded_at"]


def test_write_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    message = "read-only filesystem"

    def _boom(_session_id: str) -> object:
        raise OSError(message)

    monkeypatch.setattr("cw.codex_review._diagnostics.diagnostics_dir", _boom)
    with caplog.at_level("WARNING"):
        _persist_session_diagnostics_json(
            session_id="s-shared-diag-fail",
            filename="profile.json",
            payload={},
            log_label="test profile",
        )
    assert "test profile diagnostics write failed" in caplog.text
