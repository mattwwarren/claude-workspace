"""The codex lane files its SHOULD_FIX defers as review debt (#3137)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_review_debt import _FILERS, file_codex_deferrals
from cw.review_adjudication import read_deferred_findings_md
from cw.review_convergence import SettledEntry, finding_identity
from cw.review_debt_filing import DEFERRED_FINDINGS_RELATIVE_PATH
from tests._review_convergence_helpers import fresh_ledger, round_finding
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.review_convergence import ConvergenceLedger

_LOGGER = "cw.codex_review_debt"
_NOW = "2026-11-01T00:00:00Z"
_RUN_FILING = "run_debt_filing"


class _Filer:
    """Records each ``run_debt_filing`` call the codex lane makes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        client: str | None,
        ticket: str | None,
        artifact: Path | None,
        applied: list[Any],
        *,
        cwd: Path | None = None,
    ) -> None:
        self.calls.append(
            {
                "client": client,
                "ticket": ticket,
                "artifact": artifact,
                "applied": applied,
                "cwd": cwd,
            }
        )


@pytest.fixture
def filer(monkeypatch: pytest.MonkeyPatch) -> _Filer:
    recorder = _Filer()
    monkeypatch.setitem(_FILERS, _RUN_FILING, recorder)
    return recorder


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


def test_only_new_defers_are_filed_with_worktree_cwd(
    tmp_path: Path, client: ClientConfig, filer: _Filer
) -> None:
    findings = [
        round_finding("SHOULD_FIX", "Keep this", outcome="defer", rationale="why"),
        round_finding("MUST_FIX", "Fixed one", outcome="fix"),
        round_finding("SHOULD_FIX", "Rejected one", outcome="reject", rationale="no"),
    ]

    _call(tmp_path, client, findings, _ledger())

    assert len(filer.calls) == 1
    call = filer.calls[0]
    assert call["ticket"] == "101"
    assert call["cwd"] == tmp_path
    assert call["artifact"] == tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    assert [a.summary for a in call["applied"]] == ["Keep this"]
    applied = call["applied"][0]
    assert applied.rationale == "why"
    assert applied.round == 2
    assert applied.recorded_at == _NOW


def test_artifact_is_written_before_filing(
    tmp_path: Path, client: ClientConfig, filer: _Filer
) -> None:
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    written = read_deferred_findings_md(artifact)
    assert [entry.summary for entry in written] == ["Kept"]


def test_already_settled_defer_is_not_refiled(
    tmp_path: Path, client: ClientConfig, filer: _Filer
) -> None:
    findings = [
        round_finding("SHOULD_FIX", "Old claim", outcome="defer", rationale="r")
    ]

    _call(tmp_path, client, findings, _ledger(_settled("Old claim")))

    assert filer.calls == []
    assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_nothing_new_does_no_io(
    tmp_path: Path, client: ClientConfig, filer: _Filer
) -> None:
    _call(
        tmp_path, client, [round_finding("MUST_FIX", "Open", outcome="fix")], _ledger()
    )

    assert filer.calls == []
    assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_filer_failure_is_logged_not_raised(
    tmp_path: Path,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        msg = "gh went away"
        raise OSError(msg)

    monkeypatch.setitem(_FILERS, _RUN_FILING, _boom)
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    expected = (
        f"codex review-debt filing failed ticket=101 client={client.name} "
        "session=sess-1"
    )
    assert expected in caplog.text


def test_unregistered_filer_keeps_the_artifact(
    tmp_path: Path,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delitem(_FILERS, _RUN_FILING, raising=False)
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    assert "review-debt filer not registered" in caplog.text
    written = read_deferred_findings_md(tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH)
    assert [entry.summary for entry in written] == ["Kept"]


def test_handoff_is_logged_at_info(
    tmp_path: Path,
    client: ClientConfig,
    filer: _Filer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    findings = [round_finding("SHOULD_FIX", "Kept", outcome="defer", rationale="r")]

    _call(tmp_path, client, findings, _ledger())

    assert "codex review-debt deferrals handed to filing" in caplog.text
    assert "count=1" in caplog.text
