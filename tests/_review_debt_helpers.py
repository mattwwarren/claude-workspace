"""Shared builders for the review-debt filing tests (#3041)."""

from __future__ import annotations

from datetime import UTC, datetime

from cw.models.review_debt_record import ReviewDebtRecord

#: A fixed filing time, so records compare equal across builds.
DEBT_FILED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _debt_record(**overrides: object) -> ReviewDebtRecord:
    """A valid ``filed`` record; *overrides* replace any field."""
    fields: dict[str, object] = {
        "key": "a" * 64,
        "tracker": "github-issues",
        "ticket_id": "101",
        "ticket_url": "https://github.com/owner/repo/issues/101",
        "filed_at": DEBT_FILED_AT,
        "mode": "filed",
    }
    fields.update(overrides)
    return ReviewDebtRecord.model_validate(fields)
