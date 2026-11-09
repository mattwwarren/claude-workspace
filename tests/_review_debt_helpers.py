"""Shared builders for the review-debt filing tests (#3041)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.events import read_events
from cw.models.review_debt_record import ReviewDebtRecord
from cw.review_adjudication import Adjudication, render_deferred_findings_md
from cw.review_debt_filing import DebtCloseState, DebtEntry, FilingContext
from cw.review_debt_filing._marker import debt_marker, pr_marker, scope_prefix
from tests._cli_review_helpers import _seed_review_row
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _write_project_config_yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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


def _debt_body(
    key: str | None,
    *,
    queue_ticket: str | None = DEBT_TICKET,
    pr_urls: Sequence[str] = (),
) -> str:
    """A debt issue body: scope line, key marker, then one marker per PR URL.

    A None *queue_ticket* writes a first line that names no queue ticket; a
    None *key* writes no key marker.
    """
    first = (
        "A hand-written first line."
        if queue_ticket is None
        else f"{scope_prefix(f'#{queue_ticket}')} `dev/{queue_ticket}`."
    )
    lines = [first, "", "Deferred finding text."]
    if key is not None:
        lines += ["", debt_marker(key)]
    lines += [pr_marker(url) for url in pr_urls]
    return "\n".join(lines) + "\n"


def _sweep_state(clock: FakeClock | None = None) -> DebtCloseState:
    """A fresh close-sweep state on *clock* (a new :class:`FakeClock` by default)."""
    return DebtCloseState(clock=clock or FakeClock())


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


def write_deferred_findings(root: Path, *entries: Adjudication) -> Path:
    """Write ``<root>/.cw/deferred-findings.md`` for ``entries``; return its path."""
    path = root / ".cw" / "deferred-findings.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_deferred_findings_md(list(entries)), encoding="utf-8")
    return path


class DebtFilingCall(NamedTuple):
    """One recorded ``run_debt_filing`` call (#3042, #3137 ``cwd``)."""

    client: str | None
    ticket: str | None
    artifact: Path | None
    applied: list[Adjudication]
    cwd: Path | None = None


def stub_run_debt_filing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str = "cw.cli.review.converge_confirm.run_debt_filing",
) -> list[DebtFilingCall]:
    """Record the calls to ``run_debt_filing`` at *target* (#3042, #3137)."""
    calls: list[DebtFilingCall] = []

    def _record(
        client: str | None,
        ticket: str | None,
        artifact: Path | None,
        applied: Sequence[Adjudication],
        *,
        cwd: Path | None = None,
    ) -> None:
        calls.append(DebtFilingCall(client, ticket, artifact, list(applied), cwd))

    monkeypatch.setattr(target, _record)
    return calls


class CodexDeferralCall(NamedTuple):
    """One recorded ``file_codex_deferrals`` call (#3137)."""

    task: object
    client: object
    worktree: Path
    sid: str
    findings: list[object]
    ledger: object
    round_number: int


def stub_file_codex_deferrals(
    monkeypatch: pytest.MonkeyPatch,
) -> list[CodexDeferralCall]:
    """Record the codex lane's defer-filing calls at both call sites (#3137).

    Patches the two caller bindings, not the defining module, so a test that
    imports ``cw.codex_review_debt`` directly still runs the real function.
    """
    calls: list[CodexDeferralCall] = []

    def _record(
        *,
        task: object,
        client: object,
        worktree: Path,
        sid: str,
        findings: Sequence[object],
        ledger: object,
        round_number: int,
        now: str,
    ) -> None:
        del now
        calls.append(
            CodexDeferralCall(
                task, client, worktree, sid, list(findings), ledger, round_number
            )
        )

    for target in (
        "cw.codex_loop_rounds.file_codex_deferrals",
        "cw.codex_clean_confirm.file_codex_deferrals",
    ):
        monkeypatch.setattr(target, _record)
    return calls


def register_confirm_client(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "acme",
    ticket: str = DEBT_TICKET,
) -> None:
    """Register *root* as a GitHub-tracked client that files review debt (#3042).

    Wraps the client registration, the tracker config, a private-visibility
    stub and the queue row the filing run reads, so a real ``record-round``
    filing run creates tickets through a fake ``gh``.
    """
    _register_debt_client(root, name=name, auto_create_issues=True)
    _stub_visibility(monkeypatch, "PRIVATE")
    _seed_review_row(ticket, name)
