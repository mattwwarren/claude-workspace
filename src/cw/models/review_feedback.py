"""Typed review feedback carried from dev-queue rows into worker context.

The report is bounded and retained verbatim. Its contents are untrusted
review evidence; implementation-stage instructions define how workers verify
and use it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

REVIEW_FEEDBACK_KEY = "review_feedback"
"""The ``queue_metadata`` wire key carrying an operator-supplied review report."""

MAX_REVIEW_REPORT_BYTES = 64 * 1024
"""Maximum UTF-8 byte length of one verbatim review report."""


class ReviewFeedback(BaseModel):
    """One repo-qualified PR review captured against an exact head commit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int = Field(gt=0)
    reviewed_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    captured_at: datetime
    report: str

    @field_validator("repo")
    @classmethod
    def validate_repo_identity(cls, value: str) -> str:
        """Require exactly two non-dot path components in ``owner/repo`` form."""
        owner, repository = value.split("/")
        if owner in {".", ".."} or repository in {".", ".."}:
            msg = "repo must be a GitHub owner/repo slug"
            raise ValueError(msg)
        return value

    @field_validator("captured_at", mode="before")
    @classmethod
    def parse_captured_at(cls, value: object) -> object:
        """Parse only timezone-bearing ISO-8601 text or an existing datetime."""
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            return value
        iso_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(iso_value)
        except ValueError as exc:
            msg = "captured_at must be an ISO-8601 timestamp string"
            raise ValueError(msg) from exc

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Reject naive timestamps so capture time has an unambiguous instant."""
        if value.utcoffset() is None:
            msg = "captured_at must include a timezone"
            raise ValueError(msg)
        return value

    @field_validator("report")
    @classmethod
    def validate_report(cls, value: str) -> str:
        """Reject empty, invalid UTF-8, or oversized text without normalizing it."""
        if not value.strip():
            msg = "report must not be empty"
            raise ValueError(msg)
        try:
            report_size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            msg = "report must be valid UTF-8 text"
            raise ValueError(msg) from exc
        if report_size > MAX_REVIEW_REPORT_BYTES:
            msg = f"report must be at most {MAX_REVIEW_REPORT_BYTES} UTF-8 bytes"
            raise ValueError(msg)
        return value
