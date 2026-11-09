"""``cw review adjudicate`` files review debt at deferral time (#3041, R2/R3/R5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue.review_debt_records import read_review_debt
from cw.review_adjudication import Adjudication, render_deferred_findings_md
from cw.review_debt_filing import artifact_debt_entries
from tests import _real_cw_guard
from tests._cli_review_helpers import (
    _accepted_payload,
    _defer_entry,
    _seed_review_row,
    _session_repo,
    _verdict_payload,
    _write_session_context,
)
from tests._review_debt_helpers import (
    _register_debt_client,
    _stub_visibility,
    write_deferred_findings,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

    from tests._host_probe_guard import LaunchRecorder

_TICKET = "3041"
_CLIENT = "acme"
_FIXTURES = Path(__file__).parent / "fixtures"
_CREATE_STDOUT = (_FIXTURES / "gh_issue_create_stdout.txt").read_text(encoding="utf-8")
_AUTH_STDERR = (_FIXTURES / "gh_issue_create_stderr_auth.txt").read_text(
    encoding="utf-8"
)
_URL = _CREATE_STDOUT.strip()
#: A real-looking path; ``src/cw/foo.py`` is the suite's fixture path, refused.
_FILE = "src/thing.py"
_FIXTURE_FILE = "src/cw/foo.py"
_SKIPPED_NO_SESSION = "friction: debt_filing: skipped_no_session"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cw session naming the ticket but no client (identity unresolved)."""
    return _session_repo(make_git_repo, monkeypatch, ticket_id=_TICKET)


@pytest.fixture
def plain_repo(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A git checkout with no cw-context: not a cw session."""
    return _session_repo(make_git_repo, monkeypatch)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    fake = FakeGhCli().install(monkeypatch)
    fake.reply("gh", "label", "create")
    fake.reply("gh", "issue", "list", stdout="[]")
    fake.reply("gh", "issue", "create", stdout=_CREATE_STDOUT)
    return fake


def _payload(*entries: dict[str, Any], files: tuple[str, ...] = (_FILE,)) -> str:
    accepted = [_accepted_payload(line_start=2, line_end=2, file=f) for f in files]
    return json.dumps(
        {"verdict": _verdict_payload(*accepted), "adjudications": list(entries)}
    )


def _adjudicate(*args: str, payload: str | None = None) -> Result:
    return CliRunner().invoke(
        main,
        ["review", "adjudicate", "-", *args],
        input=payload if payload is not None else _payload(_defer_entry(file=_FILE)),
    )


def _fixture_payload() -> str:
    """The incident payload: a deferral naming the suite's fixture path."""
    return _payload(_defer_entry(), files=(_FIXTURE_FILE,))


def _github_client(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register_debt_client(repo, name=_CLIENT, auto_create_issues=True)
    _stub_visibility(monkeypatch, "PRIVATE")
    _seed_review_row(_TICKET, _CLIENT)


def _creates(cli: FakeGhCli) -> list[list[str]]:
    return [argv for argv in cli.argvs() if argv[:3] == ["gh", "issue", "create"]]


def test_no_session_and_no_override_skips_loudly(plain_repo: Path) -> None:
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0
    assert _SKIPPED_NO_SESSION in result.stderr
    assert "not inside a cw session" in result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["accepted"][0]["disposition"] == "deferred"


def test_context_free_deferrals_do_not_write_the_default_artifact(
    plain_repo: Path,
) -> None:
    # #3113: outside a cw session there is no default artifact to persist to.
    result = _adjudicate()
    assert result.exit_code == 0
    assert _SKIPPED_NO_SESSION in result.stderr
    assert not (plain_repo / ".cw").exists()


def test_a_session_without_a_client_skips_with_no_context(repo: Path) -> None:
    # Regression (#3041 MUST_FIX): the skip must not drop the deferral. The
    # default artifact is the only rediscovery point for a later retry.
    result = _adjudicate()
    assert result.exit_code == 0
    assert "friction: debt_filing: skipped_no_context" in result.stderr
    entries = artifact_debt_entries(repo / ".cw" / "deferred-findings.md")
    assert len(entries) == 1
    assert entries[0].ticket == ""


def test_nothing_deferred_outside_a_session_is_silent(plain_repo: Path) -> None:
    result = _adjudicate(payload=_payload())
    assert result.exit_code == 0
    assert result.stderr == ""


def test_nothing_to_file_is_silent(repo: Path) -> None:
    result = _adjudicate(payload=_payload())
    assert result.exit_code == 0
    assert result.stderr == ""


def test_the_context_supplies_client_and_ticket(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    _write_session_context(repo, headless=True, client=_CLIENT, ticket_id=_TICKET)
    result = _adjudicate()
    assert result.exit_code == 0, result.output
    assert len(_creates(cli)) == 1
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert (record.ticket_url, record.mode) == (_URL, "filed")
    assert all(call.cwd == repo for call in cli.records)
    assert all(snapshot == () for snapshot in cli.lock_snapshots)


def test_flags_override_the_context(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    _write_session_context(repo, headless=True, client="other", ticket_id="1")
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0, result.output
    assert len(read_review_debt(_TICKET, _CLIENT)) == 1


def test_an_unset_tracker_skips(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register_debt_client(repo, name=_CLIENT, tracker=None)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0
    assert "friction: debt_filing: skipped_no_tracker" in result.stderr


def test_filing_runs_without_deferred_findings_out(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0, result.output
    assert len(_creates(cli)) == 1
    body = cli.records[-1].body_file or ""
    assert "def broken():" in body  # in-memory evidence reaches the ticket


def test_the_artifact_gets_ticket_lines_after_filing(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    out = repo / ".cw" / "deferred-findings.md"
    result = _adjudicate(
        "-c", _CLIENT, "--ticket", _TICKET, "--deferred-findings-out", str(out)
    )
    assert result.exit_code == 0, result.output
    assert f"  ticket: {_URL}\n" in out.read_text(encoding="utf-8")


def _baseline(out: Path) -> None:
    baseline = Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/legacy.py",
        summary="pre-existing baselined mypy errors in src/cw/legacy.py",
        outcome="defer",
        rationale="baselined debt",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_deferred_findings_md([baseline]), encoding="utf-8")


def test_baseline_hand_written_entries_are_filed_from_the_merged_artifact(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    out = repo / ".cw" / "deferred-findings.md"
    _baseline(out)
    result = _adjudicate(
        "-c", _CLIENT, "--ticket", _TICKET, "--deferred-findings-out", str(out)
    )
    assert result.exit_code == 0, result.output
    assert len(_creates(cli)) == 2
    assert out.read_text(encoding="utf-8").count("  ticket: ") == 2
    baseline_body = cli.records[2].body_file or ""
    assert "(evidence not recorded)" in baseline_body


def test_a_gh_failure_still_writes_the_artifact_and_the_verdict(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    cli.reply("gh", "issue", "create", returncode=1, stderr=_AUTH_STDERR)
    out = repo / ".cw" / "deferred-findings.md"
    result = _adjudicate(
        "-c", _CLIENT, "--ticket", _TICKET, "--deferred-findings-out", str(out)
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["accepted"][0]["disposition"] == "deferred"
    assert "  ticket: " not in out.read_text(encoding="utf-8")
    assert "debt filing failed [auth] " in result.stderr
    assert "friction: debt_filing_failed: 1" in result.stderr
    assert read_review_debt(_TICKET, _CLIENT) == []


def test_the_row_write_happens_after_filing(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    order: list[str] = []
    real = cli

    def tracking_run(cmd: list[str], **kwargs: object) -> object:
        order.append("gh")
        return real(cmd, **kwargs)

    def tracking_record(*_a: object, **_k: object) -> list[object]:
        order.append("row")
        return []

    monkeypatch.setattr("cw.gh.filing._sp.run", tracking_run)
    monkeypatch.setattr("cw.cli.review.debt.record_review_debt", tracking_record)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0, result.output
    assert order[-1] == "row"
    assert "row" not in order[:-1]


def test_a_missing_row_creates_nothing(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_debt_client(repo, name=_CLIENT, auto_create_issues=True)
    _stub_visibility(monkeypatch, "PRIVATE")
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0
    assert cli.records == []
    assert "friction: debt_filing_failed: 1" in result.stderr


def test_auto_create_false_emits_instead(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_debt_client(repo, name=_CLIENT, auto_create_issues=False)
    _stub_visibility(monkeypatch, "PUBLIC")
    _seed_review_row(_TICKET, _CLIENT)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0, result.output
    assert _creates(cli) == []
    assert len(list((repo / ".cw" / "review-debt").glob("*.json"))) == 1
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert (record.mode, record.ticket_id) == ("emitted", None)


def test_an_unreadable_client_degrades(repo: Path) -> None:
    result = _adjudicate("-c", "ghost", "--ticket", _TICKET)
    assert result.exit_code == 0
    assert "friction: debt_filing: skipped_no_tracker" in result.stderr


def test_a_cw_error_during_filing_degrades(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_debt_client(repo, name=_CLIENT, auto_create_issues=True)

    def _boom(_identity: object) -> object:
        msg = "client config unreadable"
        raise OSError(msg)

    monkeypatch.setattr("cw.cli.review.debt._plan", _boom)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0
    assert "debt filing failed [other] -: client config unreadable" in result.stderr
    assert "friction: debt_filing_failed: 1" in result.stderr


def test_a_fixture_entry_is_refused_at_the_filing_step(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET, payload=_fixture_payload())
    assert result.exit_code == 0, result.output
    assert _creates(cli) == []
    assert f"debt filing refused [fixture_path] {_FIXTURE_FILE}: " in result.stderr
    assert "friction: debt_filing: refused_fixture_entry" in result.stderr
    assert json.loads(result.stdout)["accepted"][0]["disposition"] == "deferred"


def test_a_mixed_run_files_the_real_entry_and_refuses_the_fixture(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_client(repo, monkeypatch)
    payload = _payload(
        _defer_entry(file=_FILE), _defer_entry(), files=(_FILE, _FIXTURE_FILE)
    )
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET, payload=payload)
    assert result.exit_code == 0, result.output
    assert len(_creates(cli)) == 1
    assert "friction: debt_filing: refused_fixture_entry" in result.stderr
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert record.ticket_url == _URL


def _cw_tree(cw_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(cw_dir)): path.read_bytes()
        for path in sorted(cw_dir.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def dispatch_worktree(
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    launch_recorder: LaunchRecorder,
) -> Path:
    """A dispatch-like worktree: a session context and a real ``.cw/`` artifact.

    The guard is re-armed on this worktree's ``.cw/`` so the incident shape
    can be provoked without touching the real checkout's.
    """
    root = _session_repo(
        make_git_repo, monkeypatch, "worktree", client=_CLIENT, ticket_id=_TICKET
    )
    baseline = Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/legacy.py",
        summary="pre-existing debt",
        outcome="defer",
        rationale="real deferral",
    )
    write_deferred_findings(root, baseline)
    _real_cw_guard.arm(root / ".cw", (), launch_recorder)
    return root


def test_the_guard_catches_the_incident_pattern(
    dispatch_worktree: Path, launch_recorder: LaunchRecorder
) -> None:
    """cwd = the worktree, no isolation, no --deferred-findings-out (#3113)."""
    before = _cw_tree(dispatch_worktree / ".cw")
    result = _adjudicate(payload=_fixture_payload())
    assert "debt filing failed [other]" in result.stderr
    violations = launch_recorder.drain()
    assert violations
    assert {v.kind for v in violations} == {"cw_write"}
    assert _cw_tree(dispatch_worktree / ".cw") == before


def test_the_isolated_pattern_writes_nothing(
    dispatch_worktree: Path,
    tmp_path: Path,
    launch_recorder: LaunchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _cw_tree(dispatch_worktree / ".cw")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = _adjudicate(payload=_fixture_payload())
    assert result.exit_code == 0
    assert launch_recorder.violations() == []
    assert _cw_tree(dispatch_worktree / ".cw") == before
    assert not (elsewhere / ".cw").exists()
    assert _SKIPPED_NO_SESSION in result.stderr


@pytest.mark.parametrize("shape", ["git_no_context", "not_git", "no_ticket_id"])
def test_adjudicate_outside_a_session_writes_no_artifact(
    shape: str,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shape == "not_git":
        cwd = tmp_path / "plain"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
    else:
        cwd = _session_repo(make_git_repo, monkeypatch)
    if shape == "no_ticket_id":
        _write_session_context(cwd, headless=True, client=_CLIENT)
    result = _adjudicate("-c", _CLIENT, "--ticket", _TICKET)
    assert result.exit_code == 0
    assert not (cwd / ".cw" / "deferred-findings.md").exists()
    assert _SKIPPED_NO_SESSION in result.stderr
