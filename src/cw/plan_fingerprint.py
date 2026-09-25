"""Shared validation for plan-draft content fingerprints."""

import re

# A plan-draft fingerprint is a full SHA-256 digest in lowercase hex.
PLAN_DRAFT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def is_plan_draft_fingerprint(value: str) -> bool:
    """Return whether *value* is a 64-character lowercase hex digest."""
    return PLAN_DRAFT_FINGERPRINT_RE.fullmatch(value) is not None
