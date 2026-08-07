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

# Credential-bearing CODEX_* variable names always count as secrets,
# regardless of value length (#1712) -- matches OPENAI_API_KEY's
# unconditional inclusion below.
_CODEX_CREDENTIAL_NAME_RE = re.compile(
    r"_(?:KEY|TOKEN|SECRET|AUTH|PASSWORD|CREDENTIAL)(?:_|$)"
)

# Non-credential-named CODEX_* values only count as secrets at or above
# this length, matching _SK_TOKEN_RE's 20-char precedent (#1712) --
# shorter values here are ordinary flags/enums/paths/ids, not leaked
# credentials.
_CODEX_VALUE_LENGTH_FLOOR = 20


def _known_secret_values() -> list[str]:
    """Return the runtime secret values to deny, skipping empty ones.

    Sources: ``OPENAI_API_KEY`` and every ``CODEX_*``-prefixed environment
    variable actually present in ``os.environ``, classified in two branches:
    a variable whose name matches ``_CODEX_CREDENTIAL_NAME_RE`` (KEY, TOKEN,
    SECRET, AUTH, PASSWORD, CREDENTIAL) always counts, regardless of value
    length; any other ``CODEX_*`` variable counts only when its value is at
    least ``_CODEX_VALUE_LENGTH_FLOOR`` characters long. Empty values are
    skipped — an empty needle would match every text.
    """
    values: list[str] = []
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        values.append(api_key)
    for name, value in os.environ.items():
        if not name.startswith("CODEX_") or not value:
            continue
        if _CODEX_CREDENTIAL_NAME_RE.search(name):
            values.append(value)
        elif len(value) >= _CODEX_VALUE_LENGTH_FLOOR:
            # Deliberate: no shape carve-out for path-like/session-ID-like
            # values (#1712 round 2). A carve-out here is an allowlist
            # wearing a costume and rots the same way. Failure directions
            # are asymmetric -- a false positive is a loud, diagnosable
            # test failure; a false negative is a silent credential leak.
            # A long ambient path/ID occasionally tripping this is an
            # accepted, intentional trade-off, not a bug to "fix" by
            # re-adding shape exclusions.
            values.append(value)
    return values


def _assert_no_secrets_leaked(*texts: str) -> None:
    """Raise ``AssertionError`` if any *text* contains a known secret.

    Checks each text against the runtime value of ``OPENAI_API_KEY``, any
    ``CODEX_*``-prefixed environment variable classified as a secret by
    :func:`_known_secret_values` (credential-named regardless of length, or
    non-credential-named at or above the 20-char floor), and the ``sk-``
    token pattern.
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

    def test_codex_auth_token_name_raises_at_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_AUTH_TOKEN", "abcdefghijklmnopqrstuvwx")
        try:
            _assert_no_secrets_leaked("output contains abcdefghijklmnopqrstuvwx here")
        except AssertionError:
            return
        msg = "expected AssertionError for leaked CODEX_AUTH_TOKEN value"
        raise AssertionError(msg)

    def test_codex_short_credential_named_var_raises_under_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_API_KEY", "abc")
        try:
            _assert_no_secrets_leaked("output contains abc here")
        except AssertionError:
            return
        msg = "expected AssertionError for short CODEX_API_KEY value"
        raise AssertionError(msg)

    def test_codex_generic_short_values_pass_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_FEATURE_FLAG", "1")
        monkeypatch.setenv("CODEX_LEGACY_MODE", "0")
        monkeypatch.setenv("CODEX_WORKDIR", "/opt/codex/bin")
        monkeypatch.setenv("CODEX_REQUEST_ID", "req_9f2ab3")
        _assert_no_secrets_leaked(
            "flag=1 mode=0 workdir=/opt/codex/bin request_id=req_9f2ab3"
        )

    def test_codex_generic_value_at_or_above_floor_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Proves the accepted round-2 trade-off: a non-credential-named
        # CODEX_* value at/above the length floor is still a needle, even
        # though it is path-shaped rather than a real credential (#1712).
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_WORKDIR", "/opt/codex/bin/long/session/path")
        try:
            _assert_no_secrets_leaked(
                "output contains /opt/codex/bin/long/session/path here"
            )
        except AssertionError:
            return
        msg = "expected AssertionError for CODEX_WORKDIR value at/above length floor"
        raise AssertionError(msg)

    def test_codex_authority_url_name_does_not_false_match_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Proves the credential-name regex's trailing word boundary: AUTH
        # inside AUTHORITY must not false-match (#1712 round 2), unlike the
        # genuine CODEX_AUTH_TOKEN case covered above.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_AUTHORITY_URL", "http://x")
        _assert_no_secrets_leaked("output contains http://x here")
