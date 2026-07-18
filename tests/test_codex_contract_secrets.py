"""Secrets-denylist guard for the live codex contract suite (#1238).

This module carries NO ``pytest.mark.integration`` marker and NO skip gate, so
it is always collected under the default ``-m 'not integration'`` CI step. The
live suite (``tests/test_codex_contract_live.py``) imports
:func:`_assert_no_secrets_leaked` and calls it on every captured subprocess
artifact so a codex CLI that ever echoes a credential fails the nightly run
loudly rather than leaking it into a job log.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# Matches an OpenAI-style secret token (``sk-`` followed by 20+ url-safe chars).
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _known_secret_values() -> list[str]:
    """Return the runtime secret values to deny, skipping empty ones.

    Sources: ``OPENAI_API_KEY`` and every ``CODEX_*``-prefixed environment
    variable actually present in ``os.environ``. Empty values are skipped —
    an empty needle would match every text.
    """
    values: list[str] = []
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        values.append(api_key)
    for name, value in os.environ.items():
        if name.startswith("CODEX_") and value:
            values.append(value)
    return values


def _assert_no_secrets_leaked(*texts: str) -> None:
    """Raise ``AssertionError`` if any *text* contains a known secret.

    Checks each text against the runtime values of ``OPENAI_API_KEY`` and any
    ``CODEX_*``-prefixed environment variable, plus the ``sk-`` token pattern.
    """
    secret_values = _known_secret_values()
    for text in texts:
        for secret in secret_values:
            assert secret not in text, "captured text leaked a known secret value"
        assert not _SK_TOKEN_RE.search(text), (
            "captured text contains an sk-... secret-shaped token"
        )


class TestAssertNoSecretsLeaked:
    """Unit coverage for the always-collected secrets denylist."""

    def test_env_secret_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "super-secret-token-value")
        try:
            _assert_no_secrets_leaked("output contains super-secret-token-value here")
        except AssertionError:
            return
        msg = "expected AssertionError for leaked env secret"
        raise AssertionError(msg)

    def test_sk_pattern_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear real env secrets so only the pattern branch fires.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        try:
            _assert_no_secrets_leaked("leaked sk-ABCDEFGHIJKLMNOPQRSTUVWX token")
        except AssertionError:
            return
        msg = "expected AssertionError for sk- pattern"
        raise AssertionError(msg)

    def test_benign_text_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # Ordinary review output with no secret-shaped substrings.
        _assert_no_secrets_leaked("Reviewer found a naming nit on line 5.")
