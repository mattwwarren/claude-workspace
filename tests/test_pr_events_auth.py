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

# Fake secret for HMAC test fixtures, not a real credential -- assigned to a
# module constant (rather than inlined at call sites) so ruff's S106
# heuristic, which flags string literals passed to secret-shaped keyword
# arguments, doesn't fire on this test-only value.
_TEST_KEY = "s3cr3t"


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


class TestVerifySignature:
    def test_correct_signature_verifies(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign(_TEST_KEY, body)
        assert verify_signature(body, header_value=header, secret=_TEST_KEY) is True

    def test_incorrect_secret_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign("wrong-secret", body)
        assert verify_signature(body, header_value=header, secret=_TEST_KEY) is False

    def test_tampered_body_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        header = _sign(_TEST_KEY, body)
        assert (
            verify_signature(
                b'{"repo": "acme/gadgets"}', header_value=header, secret=_TEST_KEY
            )
            is False
        )

    def test_malformed_prefix_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        bare_digest = hmac.new(_TEST_KEY.encode(), body, hashlib.sha256).hexdigest()
        assert (
            verify_signature(body, header_value=bare_digest, secret=_TEST_KEY) is False
        )

    def test_missing_header_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        assert verify_signature(body, header_value=None, secret=_TEST_KEY) is False

    def test_empty_header_rejected(self) -> None:
        body = b'{"repo": "acme/widgets"}'
        assert verify_signature(body, header_value="", secret=_TEST_KEY) is False


class TestWarnIfUnsignedMode:
    def test_info_when_secret_unset_and_allow_unsigned_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Secret unset + default-deny (allow_unsigned=False) is now safe -- INFO, not WARNING."""
        monkeypatch.delenv(CW_PR_EVENTS_HMAC_SECRET_ENV, raising=False)
        with caplog.at_level(logging.INFO):
            warn_if_unsigned_mode()
        assert CW_PR_EVENTS_HMAC_SECRET_ENV in caplog.text
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    def test_warns_when_secret_unset_and_allow_unsigned_true(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Operator explicitly opted into the open posture -- WARNING."""
        monkeypatch.delenv(CW_PR_EVENTS_HMAC_SECRET_ENV, raising=False)
        with caplog.at_level(logging.INFO):
            warn_if_unsigned_mode(allow_unsigned=True)
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert CW_PR_EVENTS_HMAC_SECRET_ENV in caplog.text

    def test_no_warning_when_secret_set(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        with caplog.at_level(logging.WARNING):
            warn_if_unsigned_mode()
        assert caplog.text == ""

    def test_no_warning_when_secret_set_and_allow_unsigned_true(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """allow_unsigned is ignored once a secret is configured -- no log either way."""
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        with caplog.at_level(logging.WARNING):
            warn_if_unsigned_mode(allow_unsigned=True)
        assert caplog.text == ""

    def test_no_warning_when_secret_set_to_empty_string(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty-string env var is falsy — treated the same as unset."""
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "")
        with caplog.at_level(logging.INFO):
            warn_if_unsigned_mode()
        assert CW_PR_EVENTS_HMAC_SECRET_ENV in caplog.text
