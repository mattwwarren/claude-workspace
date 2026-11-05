"""CLI end-to-end: ``cw review constrain`` (#2964).

Records a binding operator constraint on the ticket's cw dev-queue row and
prints a human-readable mirror comment. Every case runs from an interactive
(``headless: false``) session, because the suite may itself run inside a
headless dispatch worker.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

import cw.config
from cw.cli import main
from cw.config import dev_queue_file
from cw.dev_queue.operator_constraints import (
    MAX_CONSTRAINT_BYTES,
    MAX_TOTAL_CONSTRAINT_BYTES,
)
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from tests._cli_review_helpers import (
    _OPERATOR,
    _enter_operator_machine,
    _write_session_context,
)
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    events_of,
    install_acme_client,
    make_task,
    queue_row,
    seed_queue,
)
from tests.conftest import _hold_flock, _write_project_config_yaml

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from cw.models import OrchestratorEvent, TicketTask

_TEXT = "Do not add `foo_bar`. Keep the row as the source of truth."
_EVENT = OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED
_NOW = "2026-11-05 10:00:00"
_NOW_ISO = "2026-11-05T10:00:00Z"
_HELP_FIRST_LINE = (
    "Record binding operator constraints on the cw dev-queue row and print a"
    " mirror comment (#2964)."
)
_DISK_FULL = "No space left on device"


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.REVIEW}
    kwargs.update(overrides)
    task = make_task(**kwargs)
    seed_queue([task])
    return task


def _invoke(
    runner: CliRunner,
    tmp_path: Path,
    text: str = _TEXT,
    *args: str,
    ticket: str = ACME_TICKET,
    client: str | None = ACME,
) -> Result:
    path = tmp_path / "constraints.md"
    path.write_text(text, encoding="utf-8")
    client_args = ["-c", client] if client is not None else []
    return runner.invoke(
        main,
        ["review", "constrain", ticket, "--file", str(path), *client_args, *args],
    )


def _events() -> list[OrchestratorEvent]:
    return events_of(_EVENT)


def _flat(text: str) -> str:
    return " ".join(text.split())


class TestRecord:
    def test_records_on_the_row_and_confirms(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        with freeze_time(_NOW):
            result = _invoke(runner, tmp_path, f"\n  {_TEXT}\n")
        assert result.exit_code == 0, result.output
        (entry,) = queue_row().operator_constraints
        assert entry.text == _TEXT
        assert entry.actor == _OPERATOR
        assert entry.round == 1
        assert entry.recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ") == _NOW_ISO
        stderr = _flat(result.stderr)
        size = len(_TEXT.encode("utf-8"))
        assert (
            f"Recorded constraint 1 for {ACME_TICKET} ({ACME}) by {_OPERATOR}"
            f" ({size} of {MAX_TOTAL_CONSTRAINT_BYTES} bytes used). Constraints"
            " are append-only; retraction is tracked in #2968."
        ) in stderr
        (event,) = _events()
        assert event.payload["actor_resolved"] is True

    def test_stdout_is_the_mirror_body(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        with freeze_time(_NOW):
            result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert result.stdout == (
            "### Binding operator constraint 1 (recorded via"
            " `cw review constrain`)\n\n"
            f"{_TEXT}\n\n"
            f"_Recorded on the cw dev-queue row by {_OPERATOR} at {_NOW_ISO}."
            " The codex review and fix loop read the row, not this comment;"
            " this comment is a human-readable mirror._\n\n"
            f"{AGENT_COMMENT_MARKER}\n"
        )
        assert result.stdout.rstrip().endswith(AGENT_COMMENT_MARKER)
        assert "auto-dev-preflight-resolutions" not in result.stdout

    def test_stdin_file(self, runner: CliRunner) -> None:
        _seed()
        result = runner.invoke(
            main,
            ["review", "constrain", ACME_TICKET, "--file", "-", "-c", ACME],
            input=f"{_TEXT}\n",
        )
        assert result.exit_code == 0, result.output
        assert [e.text for e in queue_row().operator_constraints] == [_TEXT]

    def test_second_entry_appends(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        _invoke(runner, tmp_path)
        result = _invoke(runner, tmp_path, "No new lock files.")
        assert result.exit_code == 0, result.output
        assert "Binding operator constraint 2" in result.stdout
        assert [e.round for e in queue_row().operator_constraints] == [1, 2]

    def test_duplicate_records_nothing_and_reprints_the_mirror(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        first = _invoke(runner, tmp_path)
        queue_bytes = dev_queue_file().read_bytes()
        again = _invoke(runner, tmp_path)
        assert again.exit_code == 0, again.output
        assert again.stdout == first.stdout
        assert (
            "Note: identical to constraint 1 already recorded on ticket"
            f" '{ACME_TICKET}'; nothing new recorded."
        ) in _flat(again.stderr)
        assert "Recorded constraint" not in again.stderr
        assert dev_queue_file().read_bytes() == queue_bytes
        assert len(_events()) == 1


class TestWorkerRefusal:
    def test_refused_inside_a_headless_worker(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        _write_session_context(tmp_path, headless=True)
        queue_bytes = dev_queue_file().read_bytes()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Refusing to record an operator constraint from inside a" in (
            _flat(result.stderr)
        )
        assert "`cw review constrain` outside the worker" in _flat(result.stderr)
        assert dev_queue_file().read_bytes() == queue_bytes
        assert _events() == []

    def test_refused_when_tmpdir_is_inside_a_worker(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        worker = tmp_path / "worker"
        _write_session_context(worker, headless=True)
        monkeypatch.setenv("TMPDIR", str(worker))
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert "inside a dispatch worker" in result.stderr
        assert queue_row().operator_constraints == []

    def test_refusal_precedes_reading_the_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _write_session_context(tmp_path, headless=True)
        result = runner.invoke(
            main,
            ["review", "constrain", ACME_TICKET, "--file", "/nonexistent", "-c", ACME],
        )
        assert result.exit_code == 1
        assert "inside a dispatch worker" in result.stderr
        assert "Cannot read constraints file" not in result.stderr


class TestClientResolution:
    def test_client_is_required_when_the_prefix_does_not_resolve(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        result = _invoke(runner, tmp_path, client=None)
        assert result.exit_code == 1
        assert f"Cannot resolve client for ticket '{ACME_TICKET}'" in result.stderr
        assert queue_row().operator_constraints == []

    def test_client_defaults_from_the_prefix_map(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        config_file = cw.config.orchestrator_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("linear_prefix_map:\n  ACM: acme\n", encoding="utf-8")
        result = _invoke(runner, tmp_path, client=None)
        assert result.exit_code == 0, result.output
        assert len(queue_row().operator_constraints) == 1


class TestTrackerIndependence:
    @pytest.mark.parametrize("system", ["github-issues", "linear", None])
    def test_records_on_every_tracker_with_no_tracker_call(
        self,
        system: str | None,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if system is not None:
            _write_project_config_yaml(
                tmp_path / "ws", f"tracking:\n  primary:\n    system: {system}\n"
            )

        def _no_network(*_args: object, **_kwargs: object) -> None:
            msg = "constrain must not call the tracker"
            raise AssertionError(msg)

        for target in (
            "cw.gh.fetch_issue_comments",
            "cw.gh.post_issue_comment",
            "cw.codex_review._context.core.fetch_issue_comments",
        ):
            monkeypatch.setattr(target, _no_network)
        _seed()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert len(queue_row().operator_constraints) == 1


@pytest.mark.usefixtures("fake_lock_clock")
class TestRowCannotBeRecorded:
    def test_no_row_fails_with_no_mirror(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "No dev-queue task found" in result.stderr
        assert AGENT_COMMENT_MARKER not in result.output
        assert _events() == []

    def test_store_write_failure_fails_with_no_mirror(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError(_DISK_FULL)

        monkeypatch.setattr("cw.dev_queue.operator_constraints.save_dev_queue", _raise)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert (
            f"Cannot record a constraint for ticket '{ACME_TICKET}': writing the"
            f" dev-queue failed (OSError: {_DISK_FULL}). The"
            " ticket.constraint_recorded audit event was already written but no"
            " constraint is on the row; re-run constrain."
        ) in _flat(result.stderr)
        assert len(_events()) == 1
        assert queue_row().operator_constraints == []

    def test_lock_timeout_records_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        queue_bytes = dev_queue_file().read_bytes()
        with _hold_flock(cw.config.dev_queue_lock()):
            result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Timed out after" in result.stderr
        assert dev_queue_file().read_bytes() == queue_bytes
        assert _events() == []


class TestFileRefusals:
    def test_invalid_utf8_is_refused(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        path = tmp_path / "bad.md"
        path.write_bytes(b"Do not add \xff.")
        result = runner.invoke(
            main, ["review", "constrain", ACME_TICKET, "--file", str(path), "-c", ACME]
        )
        assert result.exit_code == 1
        assert (
            f"Cannot read constraints file {path}: it is not valid UTF-8"
            " (invalid start byte at byte 11). Nothing was recorded."
        ) in _flat(result.stderr)
        assert queue_row().operator_constraints == []

    def test_missing_file_is_refused(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        path = tmp_path / "missing.md"
        result = runner.invoke(
            main, ["review", "constrain", ACME_TICKET, "--file", str(path), "-c", ACME]
        )
        assert result.exit_code == 1
        stderr = _flat(result.stderr)
        assert f"Cannot read constraints file {path}: could not read it" in stderr
        assert "FileNotFoundError" in stderr

    def test_blank_text_is_refused(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        result = _invoke(runner, tmp_path, "  \n ")
        assert result.exit_code == 1
        assert "the constraint text is blank" in result.stderr
        assert result.stdout == ""

    def test_per_entry_cap_is_refused(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        result = _invoke(runner, tmp_path, "a" * (MAX_CONSTRAINT_BYTES + 1))
        assert result.exit_code == 1
        stderr = _flat(result.stderr)
        assert "per-entry cap" in stderr
        assert "retraction is tracked in #2968" in stderr
        assert result.stdout == ""

    def test_total_cap_is_refused(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        for index in range(MAX_TOTAL_CONSTRAINT_BYTES // MAX_CONSTRAINT_BYTES):
            assert (
                _invoke(
                    runner, tmp_path, str(index + 1) * MAX_CONSTRAINT_BYTES
                ).exit_code
                == 0
            )
        result = _invoke(runner, tmp_path, "one more")
        assert result.exit_code == 1
        stderr = _flat(result.stderr)
        assert "total cap" in stderr
        assert "retraction is tracked in #2968" in stderr
        assert result.stdout == ""


class TestRowNotes:
    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        ],
    )
    def test_terminal_row_note(
        self, status: QueueItemStatus, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed(status=status)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert (
            f"Note: ticket '{ACME_TICKET}' only has a terminal row"
            f" ({status.value}). A constraint recorded on it is lost if the"
            " ticket is re-enqueued (`cw dev-queue add` creates a new row and"
            " nothing is carried forward): re-run this constrain on the new row."
        ) in _flat(result.stderr)

    def test_running_row_note(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed(status=QueueItemStatus.RUNNING, session_id="sess-1")
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert (
            f"Note: ticket '{ACME_TICKET}' has a RUNNING row. A codex review or"
            " fix loop already in progress loads constraints once per run and"
            " will not see this one until its next run."
        ) in _flat(result.stderr)
        assert "terminal row" not in result.stderr

    def test_pending_row_has_no_row_note(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        result = _invoke(runner, tmp_path)
        assert "RUNNING row" not in result.stderr
        assert "terminal row" not in result.stderr


class TestParsePreview:
    def test_matched_prohibitions_list_tokens_and_kinds(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        text = "Do not add `foo_bar` or `baz_qux`. Never add a lock. Keep it lean."
        result = _invoke(runner, tmp_path, text)
        assert result.exit_code == 0, result.output
        assert (
            "Parsed 2 prohibition sentence(s): forbidden tokens: baz_qux, foo_bar;"
            " forbidden kinds: lock. The fix loop parks a cycle that introduces"
            " one of them."
        ) in _flat(result.stderr)

    def test_matched_sentence_without_tokens_says_none(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        result = _invoke(runner, tmp_path, "Do not add more abstraction layers.")
        assert result.exit_code == 0, result.output
        assert "forbidden tokens: none; forbidden kinds: none." in _flat(result.stderr)

    def test_no_prohibition_prints_a_note(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        result = _invoke(runner, tmp_path, "Prefer the existing helper.")
        assert result.exit_code == 0, result.output
        stderr = _flat(result.stderr)
        assert "Note: no sentence matched a prohibition pattern" in stderr
        assert "the fix-loop guard has nothing to enforce" in stderr


class TestUnresolvedActor:
    def test_records_with_the_unresolved_literal(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        _seed()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert (
            "WARNING: could not resolve your GitHub identity (gh api user"
            " failed); the constraint is recorded with actor 'unresolved'"
        ) in _flat(result.stderr)
        assert queue_row().operator_constraints[0].actor == "unresolved"
        assert "row by unresolved at" in result.stdout
        assert _events()[0].payload["actor_resolved"] is False


class TestStaleLoopGuard:
    def test_stale_loop_warns_and_records(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert result.stderr.count("warning: dispatch loop") == 1
        assert len(queue_row().operator_constraints) == 1

    def test_warns_before_the_write_is_attempted(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert result.stderr.count("warning: dispatch loop") == 1

    def test_assessment_failure_drops_warning_and_keeps_exit_code(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()

        def _boom() -> object:
            msg = "denied"
            raise PermissionError(msg)

        monkeypatch.setattr(
            "cw.cli.dev_queue._loop_warning.assess_dispatch_loop", _boom
        )
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert "warning:" not in result.stderr
        assert "note:" not in result.stderr
        assert len(queue_row().operator_constraints) == 1


class TestHelp:
    def test_help_first_line_and_body(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["review", "constrain", "--help"])
        assert result.exit_code == 0, result.output
        text = _flat(result.stdout)
        assert _HELP_FIRST_LINE in text
        assert "#2968" in text
        assert "Restart `cw dev-queue serve`" in text
        assert "refused only inside a headless dispatch worker" in text
        assert "append-only" in text
        assert "The row is the source of truth" in text
        assert "fails, and prints no mirror, on every tracker" in text

    def test_review_group_lists_constrain(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["review", "--help"])
        assert result.exit_code == 0, result.output
        assert "constrain" in result.stdout


def test_dev_queue_tasks_json_shows_the_recorded_entry(
    runner: CliRunner, tmp_path: Path
) -> None:
    _seed()
    assert _invoke(runner, tmp_path).exit_code == 0
    result = runner.invoke(main, ["dev-queue", "tasks", "--json", "-c", ACME])
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    (entry,) = row["operator_constraints"]
    assert entry["text"] == _TEXT
    assert entry["round"] == 1
    assert entry["actor"] == _OPERATOR
