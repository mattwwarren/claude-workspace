"""Tests for :mod:`cw.codex_engine_defers` (#3636): the codex lane's engine-defer seam.

Findings come from the real ``decide_verdict`` / ``from_verdict`` path. The
verdict is built through ``engine_confirm_verdict`` and restamped to an
engine-sourced, fingerprinted roster, because that builder does not guarantee
an engine-only ``agents_run``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.codex_clean_confirm import LOOP_DEFER_RATIONALE
from cw.codex_engine_defers import engine_defer_findings, record_codex_engine_defers
from cw.dev_queue.storage import load_dev_queue
from cw.models import QueueItemStatus
from cw.review_convergence import ConvergenceError
from cw.review_engine.constants import ENGINE_ROLE
from cw.review_findings import ReviewVerdict
from tests._cli_review_helpers import _seed_review_row
from tests._review_attestation_helpers import engine_round_finding
from tests._review_convergence_helpers import engine_confirm_verdict
from tests._review_history_helpers import client_config

if TYPE_CHECKING:
    from pathlib import Path

_HEAD = f"{2:040x}"
_COVERAGE = "coverage.untested-branch"
_PLAIN = "style.naming"


def _settlements(ticket_id: str) -> list[tuple[str, list[str]]]:
    rows = [t for t in load_dev_queue().tasks if t.ticket_id == ticket_id]
    return [(s.head_sha, s.fingerprints) for s in rows[0].review_engine_settlements]


def _engine_verdict(
    *rules: str, roster: tuple[str, ...] = (ENGINE_ROLE,)
) -> ReviewVerdict:
    """An engine-sourced verdict with one fingerprinted SHOULD_FIX per *rule*."""
    base = engine_confirm_verdict(
        should_fix=[(f"sf {rule}", rule) for rule in rules],
        sha=_HEAD,
    )
    accepted = [
        item.model_copy(
            update={
                "finding": item.finding.model_copy(
                    update={"source": "engine", "fingerprint": f"fp-{index}"}
                )
            }
        )
        for index, item in enumerate(base.accepted)
    ]
    agents = [
        base.agents_run[0].model_copy(update={"reviewer_role": role}) for role in roster
    ]
    return base.model_copy(update={"accepted": accepted, "agents_run": agents})


class TestEngineDeferFindings:
    def test_engine_only_plain_should_fix_returns_defer_findings(self) -> None:
        findings = engine_defer_findings(
            _engine_verdict(_PLAIN, _PLAIN), LOOP_DEFER_RATIONALE
        )
        assert [finding.outcome for finding in findings] == ["defer", "defer"]
        assert {finding.rationale for finding in findings} == {LOOP_DEFER_RATIONALE}
        assert [finding.fingerprint for finding in findings] == ["fp-0", "fp-1"]

    def test_mixed_roster_returns_nothing(self) -> None:
        verdict = _engine_verdict(_PLAIN, roster=(ENGINE_ROLE, "Other Reviewer"))
        assert engine_defer_findings(verdict, LOOP_DEFER_RATIONALE) == []

    def test_a_built_in_verdict_returns_nothing(self) -> None:
        verdict = _engine_verdict(_PLAIN, roster=("Other Reviewer",))
        assert engine_defer_findings(verdict, LOOP_DEFER_RATIONALE) == []

    def test_a_coverage_rule_stays_a_fix_and_returns_nothing(self) -> None:
        verdict = _engine_verdict(_COVERAGE)
        assert engine_defer_findings(verdict, LOOP_DEFER_RATIONALE) == []

    def test_a_pass_with_no_gating_findings_returns_nothing(self) -> None:
        assert engine_defer_findings(_engine_verdict(), LOOP_DEFER_RATIONALE) == []

    def test_a_convergence_error_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_conv(*_args: object, **_kwargs: object) -> None:
            msg = "undecided"
            raise ConvergenceError(msg)

        monkeypatch.setattr("cw.codex_engine_defers.from_verdict", raise_conv)
        assert (
            engine_defer_findings(_engine_verdict(_PLAIN), LOOP_DEFER_RATIONALE) == []
        )


class TestRecordCodexEngineDefers:
    def test_engine_false_writes_nothing(self, tmp_path: Path) -> None:
        client = client_config(tmp_path)
        task = _seed_review_row(
            "T-ENG-OFF", client.name, status=QueueItemStatus.RUNNING
        )
        record_codex_engine_defers(
            task=task,
            client=client,
            head=_HEAD,
            findings=[engine_round_finding("fp-1")],
            engine=False,
        )
        assert _settlements("T-ENG-OFF") == []

    def test_empty_or_non_deferrable_findings_write_nothing(
        self, tmp_path: Path
    ) -> None:
        client = client_config(tmp_path)
        task = _seed_review_row("T-EMPTY", client.name, status=QueueItemStatus.RUNNING)
        record_codex_engine_defers(
            task=task, client=client, head=_HEAD, findings=[], engine=True
        )
        record_codex_engine_defers(
            task=task,
            client=client,
            head=_HEAD,
            findings=[engine_round_finding("fp-1", outcome="fix")],
            engine=True,
        )
        assert _settlements("T-EMPTY") == []

    def test_happy_path_writes_the_row_at_the_head(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = client_config(tmp_path)
        task = _seed_review_row("T-HAPPY", client.name, status=QueueItemStatus.RUNNING)
        with caplog.at_level(logging.WARNING):
            record_codex_engine_defers(
                task=task,
                client=client,
                head=_HEAD,
                findings=[engine_round_finding("fp-1"), engine_round_finding("fp-2")],
                engine=True,
            )
        assert _settlements("T-HAPPY") == [(_HEAD, ["fp-1", "fp-2"])]
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_a_refused_row_warns_once_and_never_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            msg = "save failed"
            raise OSError(msg)

        monkeypatch.setattr(
            "cw.dev_queue.review_engine_defers.record_engine_settlement", refuse
        )
        client = client_config(tmp_path)
        task = _seed_review_row("T-REFUSE", client.name, status=QueueItemStatus.RUNNING)
        with caplog.at_level(
            logging.WARNING, logger="cw.dev_queue.review_engine_defers"
        ):
            record_codex_engine_defers(
                task=task,
                client=client,
                head=_HEAD,
                findings=[engine_round_finding("fp-1")],
                engine=True,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
