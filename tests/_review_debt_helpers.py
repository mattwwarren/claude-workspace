"""Shared builders for the review-debt filing tests (#3041)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.events import read_events
from cw.models.review_debt_record import ReviewDebtRecord
from cw.review_debt_filing import DebtEntry, FilingContext
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _write_project_config_yaml

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from cw.models import OrchestratorEventType

#: A fixed filing time, so records compare equal across builds.
DEBT_FILED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

#: The queue ticket the library tests file under.
DEBT_TICKET = "3041"


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


def _debt_entry(**overrides: object) -> DebtEntry:
    """A valid deferred entry; *overrides* replace any field."""
    base = DebtEntry(
        severity="SHOULD_FIX",
        file="src/cw/foo.py",
        summary="Helper swallows the error",
        rationale="Out of scope for this ticket",
        line=12,
        evidence="except Exception:\n    pass",
    )
    return base._replace(**overrides)


class FakeClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _debt_ctx(**overrides: object) -> FilingContext:
    """A GitHub filing context for :data:`DEBT_TICKET`; *overrides* replace fields."""
    base = FilingContext(
        ticket_id=DEBT_TICKET,
        client="acme",
        branch="dev/3041",
        ticket_ref=f"#{DEBT_TICKET}",
        tracker="github-issues",
        existing=(),
        visibility="PRIVATE",
        home="/home/alice",
        worktrees=("/srv/wt/acme",),
        now=lambda: DEBT_FILED_AT,
    )
    return dataclasses.replace(base, **overrides)


def _events_of(event_type: OrchestratorEventType) -> list[dict[str, object]]:
    """Payloads of every recorded *event_type* event, in order."""
    return [dict(e.payload) for e in read_events() if e.type == event_type]


def _clock_stepper(clock: FakeClock, step: float) -> Callable[[str], None]:
    """A ``FakeGh.on_call`` hook that advances *clock* by *step* per call."""

    def _advance(_name: str) -> None:
        clock.now += step

    return _advance


def _write_debt_config(
    root: Path, *, tracker: str | None = "github-issues", auto: bool | None = None
) -> None:
    """Write *root*'s project-config with the real ``tracking.primary`` nesting."""
    lines = ["tracking:", "  primary:"]
    if tracker is not None:
        lines.append(f"    system: {tracker}")
    if auto is not None:
        lines.append(f"    auto_create_issues: {'true' if auto else 'false'}")
    _write_project_config_yaml(root, "\n".join(lines) + "\n")


def _register_debt_client(
    root: Path,
    *,
    name: str = "acme",
    tracker: str | None = "github-issues",
    auto_create_issues: bool | None = None,
) -> Path:
    """Register *root* (an existing repo) as client *name* with a tracker config."""
    _write_debt_config(root, tracker=tracker, auto=auto_create_issues)
    write_clients_yaml(ClientSpec(name, workspace_path=root))
    return root


def _stub_visibility(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Patch the visibility probe ``cw review debt`` makes."""
    monkeypatch.setattr("cw.cli.review.debt.fetch_repo_visibility", lambda **_kw: value)
