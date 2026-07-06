"""Tests for cw.pr_events_auth — HMAC signature verification (#930)."""

from __future__ import annotations

import hashlib
import hmac
import logging

import pytest

from cw.pr_events_auth import (
    CW_PR_EVENTS_HMAC_SECRET_ENV,
    SIGNATURE_PREFIX,
    verify_signature,
    warn_if_unsigned_mode,
)


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


class TestVerifySignature:
    def test_correct_signature_verifies(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign("s3cr3t", body)
        assert verify_signature(body, header, "s3cr3t") is True

    def test_incorrect_secret_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign("wrong-secret", body)
        assert verify_signature(body, header, "s3cr3t") is False

    def test_tampered_body_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign("s3cr3t", body)
        assert verify_signature(b'{"repo": "acme/gadgets"}', header, "s3cr3t") is False

    def test_malformed_prefix_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        bare_digest = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        assert verify_signature(body, bare_digest, "s3cr3t") is False

    def test_missing_header_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        assert verify_signature(body, None, "s3cr3t") is False

    def test_empty_header_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        assert verify_signature(body, "", "s3cr3t") is False


class TestWarnIfUnsignedMode:
    def test_warns_when_secret_unset(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv(CW_PR_EVENTS_HMAC_SECRET_ENV, raising=False)
        with caplog.at_level(logging.WARNING):
            warn_if_unsigned_mode()
        assert CW_PR_EVENTS_HMAC_SECRET_ENV in caplog.text

    def test_no_warning_when_secret_set(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        with caplog.at_level(logging.WARNING):
            warn_if_unsigned_mode()
        assert caplog.text == ""

    def test_no_warning_when_secret_set_to_empty_string(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty-string env var is falsy — treated the same as unset."""
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "")
        with caplog.at_level(logging.WARNING):
            warn_if_unsigned_mode()
        assert CW_PR_EVENTS_HMAC_SECRET_ENV in caplog.text
