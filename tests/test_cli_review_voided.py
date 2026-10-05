"""Tests for ``cw review check-voided`` in ``cw.cli.review.voided`` (#1814).

Moved out of ``tests/test_cli_review_commands.py`` with the command itself
(#2319), so the test modules keep mirroring the ``src/cw/cli/review/``
package seams one-to-one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication import (
    VoidedFinding,
    parse_voided_findings_block,
    render_voided_findings_block,
)
from tests._cli_review_helpers import _TICKET, _accepted_payload, _verdict_payload

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _voided_payload(**overrides: object) -> dict[str, Any]:
    """A raw ``VoidedFinding`` dict lined up with ``_accepted_payload``."""
    payload: dict[str, Any] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "evidence": "def broken():",
        "operator_comment_id": "mattwwarren@2026-08-11T02:43:30Z",
        "operator_comment_excerpt": "intentional; do not re-raise",
        "voided_at": "2026-08-11T02:43:30Z",
        "original_rationale": "deliberate design choice",
    }
    payload.update(overrides)
    return payload


class TestReviewCheckVoidedCommand:
    """#1814: ``cw review check-voided`` is the Claude path's suppression hop."""

    def _payload(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
            "ticket_id": _TICKET,
            "comment_bodies": [],
            "new_voided_entries": [],
        }
        payload.update(overrides)
        return payload

    def test_existing_sentinel_comment_suppresses_a_re_derived_finding(
        self, runner: CliRunner
    ) -> None:
        body = render_voided_findings_block(
            [VoidedFinding.model_validate(_voided_payload())]
        )
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(comment_bodies=["prose", body])),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["blocking"] is False
        assert out["verdict"]["must_fix"] == []
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"
        assert [a["outcome"] for a in out["adjudications"]] == ["reject"]
        assert out["adjudications"][0]["rationale"].strip()
        # Identity fields come off the matched FINDING, not the void, so a
        # later `cw review adjudicate` pass over the same array still matches.
        assert out["adjudications"][0]["line_start"] == 2

    def test_new_entries_suppress_without_any_prior_comment(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(new_voided_entries=[_voided_payload()])),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"

    def test_emitted_event_correlates_to_the_payload_ticket_id(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(new_voided_entries=[_voided_payload()])),
        )

        assert result.exit_code == 0, result.output
        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET

    def test_no_match_leaves_the_verdict_blocking(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(
                self._payload(
                    new_voided_entries=[_voided_payload(summary="a different bug")]
                )
            ),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["blocking"] is True
        assert out["adjudications"] == []

    def test_malformed_payload_prints_field_path_errors(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps({"ticket_id": "T-1"}),
        )

        assert result.exit_code == 1
        assert "verdict" in result.output

    def test_voided_findings_out_writes_the_merged_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "nested" / "voided-findings-comment.md"
        prior = _voided_payload(summary="an earlier void", evidence="def earlier():")
        body = render_voided_findings_block([VoidedFinding.model_validate(prior)])
        result = runner.invoke(
            main,
            [
                "review",
                "check-voided",
                "--voided-findings-out",
                str(out_path),
                "-",
            ],
            input=json.dumps(
                self._payload(
                    comment_bodies=[body], new_voided_entries=[_voided_payload()]
                )
            ),
        )

        assert result.exit_code == 0, result.output
        merged = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in merged] == ["an earlier void", "Bug here"]

    def test_voided_findings_out_writes_nothing_when_there_is_nothing_to_record(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(self._payload()),
        )

        assert result.exit_code == 0, result.output
        assert not out_path.exists()

    def test_absent_voided_at_is_stamped_by_the_cli(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The Claude session supplies the judgment; the CLI supplies the clock."""
        out_path = tmp_path / "voided-findings-comment.md"
        entry = _voided_payload()
        del entry["voided_at"]
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(self._payload(new_voided_entries=[entry])),
        )

        assert result.exit_code == 0, result.output
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert written[0].voided_at != ""
