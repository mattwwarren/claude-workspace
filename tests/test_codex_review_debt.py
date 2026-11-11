"""The codex lane records its SHOULD_FIX defers in the session artifact (#3137)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from cw import codex_review_debt
from cw.codex_review_debt import file_codex_deferrals
from cw.review_adjudication import read_deferred_findings_md
from cw.review_convergence import SettledEntry, finding_identity
from cw.review_debt_filing import DEFERRED_FINDINGS_RELATIVE_PATH
from tests._review_convergence_helpers import fresh_ledger, round_finding
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.review_convergence import ConvergenceLedger

_LOGGER = "cw.codex_review_debt"
_NOW = "2026-11-01T00:00:00Z"


@pytest.fixture
def client(sample_client_config: ClientConfig) -> ClientConfig:
    return sample_client_config


def _settled(summary: str) -> SettledEntry:
    return SettledEntry(
        identity=finding_identity("SHOULD_FIX", "src/cw/foo.py", summary),
        outcome="defer",
        rationale="already settled",
        round=1,
    )


def _ledger(*settled: SettledEntry) -> ConvergenceLedger:
    return fresh_ledger().model_copy(update={"settled": list(settled)})


def _call(
    worktree: Path,
    client: ClientConfig,
    findings: list[Any],
    ledger: ConvergenceLedger,
) -> None:
    file_codex_deferrals(
        task=_make_ticket_task(ticket_id="101", client=client.name),
        client=client,
        worktree=worktree,
        sid="sess-1",
        findings=findings,
        ledger=ledger,
        round_number=2,
        now=_NOW,
    )


def test_only_new_defers_are_written_to_the_worktree_artifact(
    tmp_path: Path, client: ClientConfig
) -> None:
    findings = [
        round_finding("SHOULD_FIX", "Keep this", outcome="defer", rationale="why"),
        round_finding("MUST_FIX", "Fixed one", outcome="fix"),
        round_finding("SHOULD_FIX", "Rejected one", outcome="reject", rationale="no"),
    ]

    _call(tmp_path, client, findings, _ledger())

    written = read_deferred_findings_md(tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH)
    assert [entry.summary for entry in written] == ["Keep this"]
    assert written[0].rationale == "why"
    assert written[0].round == 2
    assert not written[0].ticket


def test_later_round_merges_into_the_artifact(
    tmp_path: Path, client: ClientConfig
) -> None:
    first = [round_finding("SHOULD_FIX", "One", outcome="defer", rationale="r")]
    second = [round_finding("SHOULD_FIX", "Two", outcome="defer", rationale="r")]

    _call(tmp_path, client, first, _ledger())
    _call(tmp_path, client, second, _ledger())

    written = read_deferred_findings_md(tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH)
    assert [entry.summary for entry in written] == ["One", "Two"]


def test_already_settled_defer_is_not_rewritten(
    tmp_path: Path, client: ClientConfig
) -> None:
    findings = [
        round_finding("SHOULD_FIX", "Old claim", outcome="defer", rationale="r")
    ]

    _call(tmp_path, client, findings, _ledger(_settled("Old claim")))

    assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_nothing_new_does_no_io(tmp_path: Path, client: ClientConfig) -> None:
    _call(
        tmp_path, client, [round_finding("MUST_FIX", "Open", outcome="fix")], _ledger()
    )

    assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_write_failure_is_logged_not_raised(
    tmp_path: Path,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        msg = "disk went away"
        raise OSError(msg)

    monkeypatch.setattr(codex_review_debt, "_write_artifact", _boom)
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    expected = (
        f"codex review-debt artifact write failed ticket=101 client={client.name} "
        "session=sess-1"
    )
    assert expected in caplog.text


def test_record_is_logged_at_info(
    tmp_path: Path, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    assert "codex review-debt deferrals recorded in artifact" in caplog.text
    assert "count=1" in caplog.text


def test_module_has_no_filer_registry() -> None:
    # Filing moved to finalize (`cw review debt file`); a registry would only
    # let a per-round filer back in.
    assert not hasattr(codex_review_debt, "register_debt_filer")
