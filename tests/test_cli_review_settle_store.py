"""Tests for ``cw.cli.review._settle_store``: settle records onto the row (#2701).

``cw review settle`` records its ledger on the ticket's cw dev-queue row, which
every review pass reads first on every tracker. When the row cannot be
recorded the outcome depends on the client's tracker (R1): a client whose
``tracking.primary.system`` is exactly ``github-issues`` degrades to
mirror-only, because the GitHub review pass reads the printed marker back;
every other tracker, and an unresolvable one, fails, because a marker no
review pass reads would be inert. The table below drives every failure mode
across every tracker shape.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

import cw.config
from cw.cli import main
from cw.codex_review import _prepare_review_pass
from cw.codex_runner import FakeCodexRunner
from cw.dev_queue import find_ticket_row, load_dev_queue
from cw.events import read_events
from cw.models import QueueItemStatus
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    parse_finding_disposition_block,
)
from tests._cli_review_helpers import (
    _SETTLE_REASON,
    _enter_operator_machine,
    _seed_review_row,
    _settle_entry,
    _settle_payload,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _hold_flock, _write_project_config_yaml, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from cw.models import TicketTask

_CLIENT = "acme"
_TICKET = "T-2701"
_MARKER_HEADING = "## Review Finding Dispositions"
_DISK_FULL = "No space left on device"


def _system(system: str) -> str:
    return f"tracking:\n  primary:\n    system: {system}\n"


#: tracker id -> (project-config.yaml text or None for no file, configured
#: client?, a fragment the failure message must carry naming the tracker).
_TRACKERS: dict[str, tuple[str | None, bool, str]] = {
    "github": (_system("github-issues"), True, "github-issues"),
    "linear": (_system("linear"), True, "'linear'"),
    "jira": (_system("jira"), True, "'jira'"),
    "unset-system": ("tracking:\n  primary: {}\n", True, "unset"),
    "no-project-config": (None, True, "unset"),
    "unconfigured-client": (None, False, "unresolvable"),
}
_NON_GITHUB = [tracker for tracker in _TRACKERS if tracker != "github"]


@pytest.fixture(autouse=True)
def _operator_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enter_operator_machine(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _configure(tracker: str, tmp_path: Path) -> Path:
    """Set up the *tracker* table row; returns the client's workspace path."""
    config, configured, _label = _TRACKERS[tracker]
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    if configured:
        write_clients_yaml(ClientSpec(_CLIENT, workspace_path=workspace))
    if config is not None:
        _write_project_config_yaml(workspace, config)
    return workspace


def _settle(
    runner: CliRunner,
    out: Path,
    payload: dict[str, Any] | None = None,
    *args: str,
) -> Result:
    return runner.invoke(
        main,
        [
            "review",
            "settle",
            "--reason",
            _SETTLE_REASON,
            "--ticket",
            _TICKET,
            *args,
            "--out",
            str(out),
            "-",
        ],
        input=json.dumps(payload or _settle_payload()),
    )


def _rows() -> list[TicketTask]:
    return [
        t
        for t in load_dev_queue().tasks
        if t.ticket_id == _TICKET and t.client == _CLIENT
    ]


def _arrange_failure(case: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Arrange *case* and return the reason text the output must carry."""
    if case == "missing_row":
        return "No dev-queue task found"
    _seed_review_row(_TICKET, _CLIENT)
    if case == "oserror_on_save":

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError(_DISK_FULL)

        monkeypatch.setattr("cw.dev_queue.review_records.save_dev_queue", _raise)
        return _DISK_FULL
    assert case == "lock_timeout"
    return "Timed out after 0.5s"


def _invoke_case(case: str, runner: CliRunner, out: Path) -> Result:
    """Run the settle; a ``lock_timeout`` case holds the dev-queue lock across it.

    The class carries the conftest ``fake_lock_clock``, so the bounded
    acquisition gives up after a fake 0.5s with no wall-clock wait.
    """
    if case == "lock_timeout":
        with _hold_flock(cw.config.dev_queue_lock()):
            return _settle(runner, out, None, "-c", _CLIENT)
    return _settle(runner, out, None, "-c", _CLIENT)


_CASES = ["missing_row", "oserror_on_save", "lock_timeout"]


@pytest.mark.usefixtures("fake_lock_clock")
class TestRowCannotBeRecorded:
    """R1: the tracker decides between degrade-to-mirror and hard failure."""

    @pytest.mark.parametrize("case", _CASES)
    def test_github_degrades_to_mirror_only(
        self,
        case: str,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _configure("github", tmp_path)
        reason = _arrange_failure(case, monkeypatch)
        out = tmp_path / "marker.md"

        result = _invoke_case(case, runner, out)

        assert result.exit_code == 0, result.output
        assert result.stdout.startswith(_MARKER_HEADING)
        ledger, refused = parse_finding_disposition_block([result.stdout])
        assert refused == []
        assert len(ledger) == 1
        stderr = " ".join(result.stderr.split())
        assert "WARNING: this settle was NOT recorded on the cw dev-queue row" in (
            stderr
        )
        assert reason in stderr
        assert f"ticket '{_TICKET}'" in stderr
        assert f"client '{_CLIENT}'" in stderr
        assert out.exists()
        assert len(read_events()) == 1
        assert all(row.finding_dispositions == {} for row in _rows())

    @pytest.mark.parametrize("tracker", _NON_GITHUB)
    def test_non_github_missing_row_refuses_and_records_nothing(
        self, tracker: str, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure(tracker, tmp_path)
        out = tmp_path / "marker.md"

        result = _settle(runner, out, None, "-c", _CLIENT)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert not out.exists()
        assert read_events() == []
        stderr = " ".join(result.stderr.split())
        assert "Cannot record this settle: No dev-queue task found" in stderr
        assert _TRACKERS[tracker][2] in stderr
        assert "Nothing was recorded" in stderr

    @pytest.mark.parametrize("case", ["oserror_on_save", "lock_timeout"])
    @pytest.mark.parametrize("tracker", _NON_GITHUB)
    def test_non_github_write_failure_fails_after_the_events(
        self,
        tracker: str,
        case: str,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _configure(tracker, tmp_path)
        reason = _arrange_failure(case, monkeypatch)
        out = tmp_path / "marker.md"

        result = _invoke_case(case, runner, out)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert not out.exists()
        assert len(read_events()) == 1
        stderr = " ".join(result.stderr.split())
        assert (
            "The settle audit events were recorded, but the dev-queue row write failed"
        ) in stderr
        assert reason in stderr
        assert _TRACKERS[tracker][2] in stderr
        assert all(row.finding_dispositions == {} for row in _rows())

    def test_a_row_that_vanishes_after_the_preflight_fails_non_github(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The lock-free pre-flight saw a row; the locked write found none."""
        _configure("linear", tmp_path)
        monkeypatch.setattr(
            "cw.cli.review._settle_store.find_ticket_row", lambda *_args: None
        )
        out = tmp_path / "marker.md"

        result = _settle(runner, out, None, "-c", _CLIENT)

        assert result.exit_code == 1
        stderr = " ".join(result.stderr.split())
        assert "the dev-queue row write failed: No dev-queue task found" in stderr
        assert not out.exists()
        assert len(read_events()) == 1


class TestRecordedOnTheRow:
    @freeze_time("2026-10-16T12:00:00Z")
    def test_success_writes_the_markers_ledger_onto_the_row(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure("linear", tmp_path)
        _seed_review_row(_TICKET, _CLIENT)
        out = tmp_path / "marker.md"

        result = _settle(runner, out, None, "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        ledger, refused = parse_finding_disposition_block([result.stdout])
        assert refused == []
        assert _rows()[0].finding_dispositions == ledger
        assert parse_finding_disposition_block([out.read_text(encoding="utf-8")])[
            0
        ] == (ledger)

    def test_a_reversal_replaces_a_rejection_on_the_row(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure("linear", tmp_path)
        _seed_review_row(_TICKET, _CLIENT)
        out = tmp_path / "marker.md"
        with freeze_time("2026-10-16T12:00:00Z"):
            assert _settle(runner, out, None, "-c", _CLIENT).exit_code == 0
        with freeze_time("2026-10-16T13:00:00Z"):
            result = _settle(
                runner,
                out,
                _settle_payload(_settle_entry(outcome="REVERSED")),
                "-c",
                _CLIENT,
            )

        assert result.exit_code == 0, result.output
        (entry,) = _rows()[0].finding_dispositions.values()
        assert entry.outcome == "REVERSED"
        assert entry.recorded_at == "2026-10-16T13:00:00Z"

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        ],
    )
    def test_a_terminal_row_records_with_a_note(
        self, status: QueueItemStatus, runner: CliRunner, tmp_path: Path
    ) -> None:
        """R9: a terminal row's ledger is not carried onto a re-enqueued row."""
        _configure("linear", tmp_path)
        _seed_review_row(_TICKET, _CLIENT, status=status)
        out = tmp_path / "marker.md"

        result = _settle(runner, out, None, "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        stderr = " ".join(result.stderr.split())
        assert (
            f"Note: ticket '{_TICKET}' only has a terminal row ({status.value})"
        ) in stderr
        assert "re-run this settle on the new row" in stderr
        assert result.stdout.startswith(_MARKER_HEADING)
        assert "Note:" not in result.stdout
        assert len(_rows()[0].finding_dispositions) == 1

    def test_an_older_settle_than_the_row_holds_is_reported(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure("linear", tmp_path)
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        newer = FindingDisposition(
            outcome="ACCEPTED",
            rationale="later decision",
            recorded_at="2099-01-01T00:00:00Z",
            actor="someone-else",
            reviewed_sha="abc1234",
            summary="Bug here",
        )
        _seed_review_row(_TICKET, _CLIENT, ledger={key: newer})
        out = tmp_path / "marker.md"

        result = _settle(runner, out, None, "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        stderr = " ".join(result.stderr.split())
        assert "Note: 1 settled finding(s) were not applied to the row" in stderr
        assert key in stderr
        assert f"cw review dispositions {_TICKET} -c {_CLIENT}" in stderr
        assert _rows()[0].finding_dispositions == {key: newer}

    def test_the_client_defaults_from_the_prefix_map(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure("linear", tmp_path)
        cw.config.orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
        cw.config.orchestrator_config_file().write_text(
            "linear_prefix_map:\n  T: acme\n", encoding="utf-8"
        )
        _seed_review_row(_TICKET, _CLIENT)
        out = tmp_path / "marker.md"

        result = _settle(runner, out)

        assert result.exit_code == 0, result.output
        assert len(_rows()[0].finding_dispositions) == 1

    def test_an_unresolvable_client_without_c_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _configure("linear", tmp_path)
        out = tmp_path / "marker.md"

        result = runner.invoke(
            main,
            [
                "review",
                "settle",
                "--reason",
                _SETTLE_REASON,
                "--ticket",
                "2701",
                "--out",
                str(out),
                "-",
            ],
            input=json.dumps(_settle_payload()),
        )

        assert result.exit_code == 1
        assert "Cannot resolve client for ticket '2701'" in result.stderr
        assert "--client" in result.stderr
        assert result.stdout == ""
        assert not out.exists()
        assert read_events() == []


class TestNoTrackerIO:
    @pytest.mark.parametrize("tracker", ["linear", "no-project-config"])
    def test_a_cli_settle_is_honoured_by_the_next_review_pass(
        self,
        tracker: str,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Acceptance 1, settle half: no tracker I/O on a non-GitHub tracker."""
        repo = make_git_repo("wt-2701-e2e")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        git_in(repo, "add", "mod.py")
        git_in(repo, "commit", "-m", "add mod.py")
        head = git_in(repo, "rev-parse", "HEAD")
        config = _TRACKERS[tracker][0]
        if config is not None:
            _write_project_config_yaml(repo, config)
        write_clients_yaml(ClientSpec(_CLIENT, workspace_path=repo))
        _seed_review_row(_TICKET, _CLIENT)
        payload = _settle_payload(_settle_entry(file="mod.py", reviewed_sha=head))

        result = _settle(runner, tmp_path / "marker.md", payload, "-c", _CLIENT)
        assert result.exit_code == 0, result.output

        def _no_tracker_io(*_args: object, **_kwargs: object) -> None:
            msg = "the review pass must not fetch tracker comments here"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.core.fetch_issue_comments", _no_tracker_io
        )
        task = find_ticket_row(_TICKET, _CLIENT)
        prepared = _prepare_review_pass(
            task, repo, "main", runner=FakeCodexRunner(), session_id="s-2701-e2e"
        )

        assert prepared.finding_dispositions == task.finding_dispositions
        assert len(prepared.finding_dispositions) == 1
        assert prepared.roles
        for role in prepared.roles:
            assert (
                "## Previously Adjudicated Findings" in prepared.prompts_by_role[role]
            )
