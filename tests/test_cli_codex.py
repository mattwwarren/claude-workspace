"""Tests for the ``cw codex`` CLI entry points (#2386, #2389)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.config import codex_legacy_recovery_file, save_state
from cw.dev_queue import add_ticket
from cw.models import CwState, QueueItemStatus, Stage, TicketTask
from tests._codex_recovery_helpers import _STARTED_AT
from tests.conftest import _make_daemon_session, _write_backend_clients_yaml

_FIRST_RUN_AT = "2026-02-01 12:00:00"
_FIRST_RUN_ISO = "2026-02-01T12:00:00+00:00"


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


# --------------------------------------------------------------------------- #
# cw codex migrate-legacy (#2389, RFC 0014 B1)
# --------------------------------------------------------------------------- #


def _migrate(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, ["codex", "migrate-legacy", *args])
    return result.exit_code, result.output


def _seed_unscannable_legacy_session(tmp_config_dir: Path, tmp_path: Path) -> None:
    """One live legacy codex session with no worktree: a partial run's cause."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_backend_clients_yaml(tmp_config_dir, workspace, "codex")
    session = _make_daemon_session(
        id="T-blind",
        name="client-a/auto-dev/T-blind",
        worktree_path=None,
        started_at=_STARTED_AT,
    )
    save_state(CwState(sessions=[session]))
    add_ticket(
        TicketTask(
            ticket_id="T-blind",
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
        )
    )


def test_migrate_legacy_help_documents_json(tmp_config_dir: Path) -> None:
    code, output = _migrate("--help")

    assert code == 0, output
    assert "--json" in output


@freeze_time(_FIRST_RUN_AT)
def test_migrate_legacy_first_run_reports_completed_with_counts(
    tmp_config_dir: Path,
) -> None:
    code, output = _migrate()

    assert code == 0, output
    assert f"completed at {_FIRST_RUN_ISO}" in output
    assert "already completed" not in output
    assert "scanned: 0" in output
    assert "skipped_writer_live: 0" in output


def test_migrate_legacy_second_run_reports_already_completed(
    tmp_config_dir: Path,
) -> None:
    with freeze_time(_FIRST_RUN_AT):
        _migrate()
    with freeze_time("2026-02-02 08:00:00"):
        code, output = _migrate()

    assert code == 0, output
    assert f"already completed at {_FIRST_RUN_ISO}" in output
    assert "requeued: 0" in output


@freeze_time(_FIRST_RUN_AT)
def test_migrate_legacy_json_completed_then_already_completed(
    tmp_config_dir: Path,
) -> None:
    code, output = _migrate("--json")
    assert code == 0, output
    first = json.loads(output)
    assert first["status"] == "completed"
    assert first["counts"] == {
        "scanned": 0,
        "requeued": 0,
        "parked": 0,
        "failed": 0,
        "skipped_already_handled": 0,
        "skipped_writer_live": 0,
    }
    assert first["unresolved"] == []

    code, output = _migrate("--json")

    assert code == 0, output
    assert json.loads(output)["status"] == "already_completed"


def test_migrate_legacy_partial_exits_one_and_lists_unresolved(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    _seed_unscannable_legacy_session(tmp_config_dir, tmp_path)

    code, output = _migrate("--json")

    assert code == 1, output
    report = json.loads(output)
    assert report["status"] == "partial"
    assert report["counts"]["scanned"] == 1
    assert report["counts"]["skipped_writer_live"] == 1
    assert report["unresolved"] == [
        {
            "session_id": "T-blind",
            "client": "client-a",
            "ticket_id": "T-blind",
            "reason": "worktree_unset",
        }
    ]


def test_migrate_legacy_partial_text_names_each_unresolved_session(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    _seed_unscannable_legacy_session(tmp_config_dir, tmp_path)

    code, output = _migrate()

    assert code == 1, output
    assert "partial" in output
    assert "T-blind" in output
    assert "worktree_unset" in output


def test_migrate_legacy_corrupt_marker_fails_through_handle_errors(
    tmp_config_dir: Path,
) -> None:
    path = codex_legacy_recovery_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    code, output = _migrate()

    assert code == 1
    assert "Error:" in output
    assert str(path) in output
