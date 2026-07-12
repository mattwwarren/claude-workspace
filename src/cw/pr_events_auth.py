"""HMAC signature verification for the POST /pr-event webhook endpoint (#930).

The endpoint accepts pushed GitHub Actions webhook payloads relayed over the
open internet (via a tunnel such as smee.io or cloudflared — see
``docs/dispatch-runbook.md`` §10 for the operator-side setup, which is
infrastructure, not code). When ``CW_PR_EVENTS_HMAC_SECRET`` is set, incoming
POST bodies must carry a valid ``X-Cw-Signature`` header computed as
``"sha256=" + hexdigest(HMAC-SHA256(secret, raw_body))``; requests failing
verification are rejected with 401. When the secret is unset the endpoint now
default-denies with 401 (#1127) unless the operator has explicitly passed
``cw pr-channel serve --allow-unsigned`` to restore the old open behavior.
``warn_if_unsigned_mode`` logs INFO for the safe default-deny posture and
WARNING only when ``--allow-unsigned`` has explicitly weakened it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

CW_PR_EVENTS_HMAC_SECRET_ENV = "CW_PR_EVENTS_HMAC_SECRET"
SIGNATURE_HEADER = "X-Cw-Signature"
SIGNATURE_PREFIX = "sha256="


def verify_signature(raw_body: bytes, *, header_value: str | None, secret: str) -> bool:
    """Return True iff *header_value* is a valid HMAC-SHA256 signature of *raw_body*.

    *header_value* must start with ``SIGNATURE_PREFIX``; a missing header or a
    malformed prefix is rejected (returns False), never raises. Uses
    ``hmac.compare_digest`` for constant-time comparison.

    Why: *header_value* and *secret* are both keyword-only despite sharing a
    ``str | None`` / ``str`` shape -- a positional call site could transpose
    them and still type-check, silently comparing the wrong values in this
    security-critical check (#930 review finding).
    """
    if header_value is None or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    provided = header_value[len(SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def warn_if_unsigned_mode(*, allow_unsigned: bool = False) -> None:
    """Log if ``CW_PR_EVENTS_HMAC_SECRET`` is unset.

    Called once from ``serve()`` — NOT ``make_app()`` — so tests and callers
    that construct the app directly (e.g. ``TestClient(make_app())``) don't
    spam this log on every app construction (#930 operator correction).

    When the secret is unset, ``/pr-event`` now default-denies (#1127) unless
    ``allow_unsigned=True`` (threaded from ``cw pr-channel serve
    --allow-unsigned``). The log level reflects actual risk: INFO for the
    now-safe default-deny posture, WARNING only when the operator has
    explicitly opted into the open, unauthenticated behavior.
    """
    if os.environ.get(CW_PR_EVENTS_HMAC_SECRET_ENV):
        return
    if allow_unsigned:
        logger.warning(
            "%s is unset and --allow-unsigned is set -- /pr-event accepts "
            "unsigned requests. Set this env var to enable HMAC "
            "authentication (see docs/dispatch-runbook.md).",
            CW_PR_EVENTS_HMAC_SECRET_ENV,
        )
    else:
        logger.info(
            "%s is unset -- /pr-event rejects unsigned requests by default. "
            "Set this env var to enable HMAC authentication, or pass "
            "--allow-unsigned to restore the old open behavior (see "
            "docs/dispatch-runbook.md).",
            CW_PR_EVENTS_HMAC_SECRET_ENV,
        )
