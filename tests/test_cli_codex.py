"""Tests for the ``cw codex run`` CLI entry point (#2386)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cw.cli import main


def test_codex_run_help_documents_both_stages(tmp_config_dir: Path) -> None:
    result = CliRunner().invoke(main, ["codex", "run", "--help"])
    assert result.exit_code == 0, result.output
    assert "review" in result.output
    assert "impl" in result.output


def test_codex_run_stage_impl_exits_nonzero_naming_1550(tmp_config_dir: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "codex",
            "run",
            "--stage",
            "impl",
            "--session-id",
            "sess-1",
            "T-1",
        ],
    )
    assert result.exit_code != 0
    assert "#1550" in result.output


def test_codex_run_stage_review_invokes_driver(tmp_config_dir: Path) -> None:
    with patch("cw.cli.codex.run_codex_review_stage") as driver_mock:
        result = CliRunner().invoke(
            main,
            [
                "codex",
                "run",
                "--stage",
                "review",
                "--session-id",
                "sess-1",
                "T-1",
            ],
        )

    assert result.exit_code == 0, result.output
    driver_mock.assert_called_once_with(
        ticket_id="T-1",
        session_id="sess-1",
        wall_clock_budget_seconds=None,
    )


def test_codex_run_stage_review_forwards_wall_clock_budget(
    tmp_config_dir: Path,
) -> None:
    with patch("cw.cli.codex.run_codex_review_stage") as driver_mock:
        result = CliRunner().invoke(
            main,
            [
                "codex",
                "run",
                "--stage",
                "review",
                "--session-id",
                "sess-1",
                "--wall-clock-budget-seconds",
                "120",
                "T-1",
            ],
        )

    assert result.exit_code == 0, result.output
    driver_mock.assert_called_once_with(
        ticket_id="T-1",
        session_id="sess-1",
        wall_clock_budget_seconds=120,
    )


def test_codex_run_missing_session_id_errors(tmp_config_dir: Path) -> None:
    result = CliRunner().invoke(main, ["codex", "run", "--stage", "review", "T-1"])
    assert result.exit_code != 0
    assert "session-id" in result.output.lower()
