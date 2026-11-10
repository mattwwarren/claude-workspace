"""The ``cw review debt`` group: file, emit, record, link-pr (#3041)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cw.config
from cw.cli import main
from cw.dev_queue.review_debt_records import read_review_debt, record_review_debt
from cw.models import OrchestratorEventType
from cw.review_adjudication import Adjudication
from cw.review_convergence import SettledEntry, finding_identity
from cw.review_debt_filing import debt_entry, pr_marker
from tests._cli_review_helpers import (
    _branch_repo,
    _seed_review_row,
    _session_repo,
    _write_session_context,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_convergence_helpers import (
    ledger_with_rounds,
    round_finding,
    write_ledger,
)
from tests._review_debt_helpers import (
    _debt_record,
    _events_of,
    _register_debt_client,
    _stub_visibility,
    _write_debt_config,
    write_deferred_findings,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

_TICKET = "3041"
_CLIENT = "acme"
_FIXTURES = Path(__file__).parent / "fixtures"
_CREATE_STDOUT = (_FIXTURES / "gh_issue_create_stdout.txt").read_text(encoding="utf-8")
_NETWORK_STDERR = (_FIXTURES / "gh_issue_create_stderr_network.txt").read_text(
    encoding="utf-8"
)
_PR_URL = "https://github.com/owner/repo/pull/9"
_ENTRY = Adjudication(
    severity="SHOULD_FIX",
    file="src/thing.py",
    summary="Helper swallows the error",
    outcome="defer",
    rationale="out of scope",
)


#: The suite's fixture finding path, which filing refuses (#3113).
_FIXTURE_ENTRY = _ENTRY.model_copy(update={"file": "src/cw/foo.py"})
_REFUSED = "friction: debt_filing: refused_fixture_entry"
_NO_SESSION = "friction: debt_filing: skipped_no_session"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cw session naming the ticket but no client (identity stays explicit)."""
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


def _artifact(repo: Path, *entries: Adjudication) -> Path:
    return write_deferred_findings(repo, *(entries or (_ENTRY,)))


def _debt(*args: str, stdin: str | None = None) -> Result:
    return CliRunner().invoke(
        main, ["review", "debt", *args, "-c", _CLIENT, "--ticket", _TICKET], input=stdin
    )


def _json(result: Result) -> dict[str, object]:
    data: dict[str, object] = json.loads(result.stdout)
    return data


def _github(repo: Path, monkeypatch: pytest.MonkeyPatch, *, auto: bool = True) -> None:
    _register_debt_client(repo, name=_CLIENT, auto_create_issues=auto)
    _stub_visibility(monkeypatch, "PRIVATE")
    _seed_review_row(_TICKET, _CLIENT)


class TestFile:
    def test_files_and_exits_0_when_complete(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        artifact = _artifact(repo)
        result = _debt("file")
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert (data["status"], data["filed"], data["unticketed"]) == ("complete", 1, 0)
        assert data["branch"] == "feature"
        assert "  ticket: " in artifact.read_text(encoding="utf-8")

    def test_a_re_run_dedupes_by_row(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo)
        assert _debt("file").exit_code == 0
        calls = len(cli.records)
        again = _debt("file")
        assert again.exit_code == 0
        assert len(cli.records) == calls
        assert _json(again)["status"] == "complete"

    def test_exits_1_when_failures_remain(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo)
        cli.reply("gh", "issue", "create", returncode=1, stderr=_NETWORK_STDERR)
        result = _debt("file")
        assert result.exit_code == 1
        data = _json(result)
        assert (data["status"], data["failed"], data["unticketed"]) == (
            "incomplete",
            1,
            1,
        )
        assert "debt filing failed [network] " in result.stderr
        assert "friction: debt_filing_failed: 1" in result.stderr

    def test_unknown_visibility_is_incomplete(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _stub_visibility(monkeypatch, None)
        _artifact(repo)
        result = _debt("file")
        assert result.exit_code == 1
        assert "[visibility_undetermined]" in result.stderr
        assert cli.records == []

    def test_an_unset_tracker_is_skipped_with_exit_0(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker=None)
        _seed_review_row(_TICKET, _CLIENT)
        result = _debt("file")
        assert result.exit_code == 0
        assert _json(result)["reason"] == "skipped_no_tracker"

    def test_auto_create_false_is_emitted_only(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch, auto=False)
        _artifact(repo)
        result = _debt("file")
        assert result.exit_code == 0, result.output
        assert (_json(result)["status"], _json(result)["emitted"]) == (
            "emitted_only",
            1,
        )
        assert cli.records == []

    def test_a_linear_client_is_mcp_pending(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        _artifact(repo)
        result = _debt("file")
        assert result.exit_code == 1
        assert _json(result)["status"] == "mcp_pending"

    def test_missing_identity_is_a_usage_error(self, repo: Path) -> None:
        result = CliRunner().invoke(main, ["review", "debt", "file"])
        assert result.exit_code == 2
        assert "no client or ticket" in result.output

    def test_an_unparseable_artifact_is_exit_1(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        bad = repo / ".cw" / "deferred-findings.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("garbage\n")
        assert _debt("file").exit_code == 1


def test_every_gh_call_targets_the_named_clients_repo(
    make_git_repo: Callable[..., Path],
    cli: FakeGhCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soundness note 2: ``-c`` pins the repo, never the process cwd."""
    session_repo, _, _ = _branch_repo(make_git_repo, "session-repo")
    client_repo = make_git_repo("other-client-repo")
    _write_debt_config(client_repo, auto=True)
    _write_debt_config(session_repo, tracker=None)
    write_clients_yaml(
        ClientSpec(_CLIENT, workspace_path=client_repo),
        ClientSpec("session", workspace_path=session_repo),
    )
    seen: list[object] = []
    monkeypatch.setattr(
        "cw.cli.review.debt.fetch_repo_visibility",
        lambda **kw: seen.append(kw["cwd"]) or "PRIVATE",
    )
    _seed_review_row(_TICKET, _CLIENT)
    _write_session_context(session_repo, headless=True, ticket_id=_TICKET)
    monkeypatch.chdir(session_repo)
    _artifact(session_repo)
    result = _debt("file")
    assert result.exit_code == 0, result.output
    assert seen == [client_repo]
    assert cli.records
    assert {call.cwd for call in cli.records} == {client_repo}


class TestEmit:
    def test_writes_payloads_and_lists_them(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        _artifact(repo)
        out = repo / "payloads"
        result = _debt("emit", "--out-dir", str(out))
        assert result.exit_code == 0, result.output
        data = _json(result)
        key = debt_entry(_ENTRY).key
        assert data["status"] == "emitted"
        assert data["files"] == [{"key": key, "path": str(out / f"{key}.json")}]
        again = _debt("emit", "--out-dir", str(out))
        assert _json(again)["files"] == data["files"]

    def test_nothing_to_emit(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        result = _debt("emit")
        assert (result.exit_code, _json(result)["status"]) == (0, "nothing_to_emit")

    def test_skipped_without_a_tracker(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="notion")
        _seed_review_row(_TICKET, _CLIENT)
        result = _debt("emit")
        assert (result.exit_code, _json(result)["status"]) == (0, "skipped")

    def test_refused_on_unknown_visibility(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch, auto=False)
        _stub_visibility(monkeypatch, None)
        _artifact(repo)
        result = _debt("emit")
        assert (result.exit_code, _json(result)["status"]) == (1, "refused")

    def test_a_missing_row_is_exit_1(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        result = _debt("emit")
        assert result.exit_code == 1
        assert "no dev-queue task found" in result.output


def _emitted_row(key: str) -> None:
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(
        _TICKET,
        _CLIENT,
        [
            _debt_record(
                key=key,
                tracker="linear",
                mode="emitted",
                ticket_id=None,
                ticket_url=None,
            )
        ],
    )


def _linear(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _register_debt_client(repo, name=_CLIENT, tracker="linear")
    cw.config.orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
    cw.config.orchestrator_config_file().write_text(
        "linear_prefix_map:\n  ENG: acme\n  OPS: other\n", encoding="utf-8"
    )
    key = debt_entry(_ENTRY).key
    _emitted_row(key)
    _artifact(repo)
    return key


class TestRecord:
    def test_success_records_and_writes_tickets_back(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _linear(repo, monkeypatch)
        url = "https://linear.app/acme/issue/ENG-7/x"
        ids = json.dumps([{"key": key, "ticket_id": "ENG-7", "url": url}])
        result = _debt("record", "--ids", "-", stdin=ids)
        assert result.exit_code == 0, result.output
        assert _json(result)["status"] == "recorded"
        (record,) = read_review_debt(_TICKET, _CLIENT)
        assert (record.ticket_id, record.ticket_url, record.mode) == (
            "ENG-7",
            url,
            "emitted",
        )
        artifact = repo / ".cw" / "deferred-findings.md"
        assert f"  ticket: {url}\n" in artifact.read_text(encoding="utf-8")

    def test_a_verify_failure_is_exit_1_and_records_nothing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _linear(repo, monkeypatch)
        ids_file = repo / "ids.json"
        ids_file.write_text(
            json.dumps([{"key": key, "ticket_id": "OPS-7", "url": "x/OPS-7"}])
        )
        result = _debt("record", "--ids", str(ids_file))
        assert result.exit_code == 1
        assert _json(result)["failures"] == [
            {"key": key, "reason": "prefix_not_allowed"}
        ]
        assert "debt verify failed [prefix_not_allowed]" in result.stderr
        assert read_review_debt(_TICKET, _CLIENT)[0].ticket_id is None
        (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_VERIFY_FAILED)
        assert event["count"] == 1

    def test_an_unreadable_ids_file_is_exit_1(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _linear(repo, monkeypatch)
        result = _debt("record", "--ids", str(repo / "missing.json"))
        assert result.exit_code == 1
        assert _json(result)["failures"] == [
            {"key": None, "reason": "unreadable_ids_file"}
        ]

    def test_nothing_expected(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        result = _debt("record", "--ids", "-", stdin="[]")
        assert (result.exit_code, _json(result)["status"]) == (0, "nothing_expected")

    def test_ids_is_required(self, repo: Path) -> None:
        assert _debt("record").exit_code == 2


def _filed_row() -> None:
    _seed_review_row(_TICKET, _CLIENT)
    record_review_debt(_TICKET, _CLIENT, [_debt_record()])


class TestLinkPr:
    def _script(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "pr", "view", stdout=json.dumps({"url": _PR_URL}))
        cli.reply("gh", "issue", "view", stdout=json.dumps({"body": "debt body"}))
        cli.reply("gh", "issue", "edit")
        cli.reply("gh", "issue", "comment")

    def test_links_and_is_idempotent(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_debt_client(repo, name=_CLIENT)
        _filed_row()
        self._script(cli)
        result = _debt("link-pr", "--pr", "9")
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert (data["status"], data["linked"], data["pr_url"]) == (
            "linked",
            1,
            _PR_URL,
        )
        edit = next(c for c in cli.records if c.argv[:3] == ["gh", "issue", "edit"])
        assert pr_marker(_PR_URL) in (edit.body_file or "")
        assert read_review_debt(_TICKET, _CLIENT)[0].pr_url == _PR_URL
        calls = len(cli.records)
        again = _debt("link-pr", "--pr", "9")
        assert (_json(again)["status"], len(cli.records)) == ("nothing_to_link", calls)
        assert all(call.cwd == repo for call in cli.records)

    def test_without_a_pr_reports_the_no_pr_friction(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT)
        _filed_row()
        result = _debt("link-pr")
        assert result.exit_code == 0
        assert _json(result)["status"] == "skipped_no_pr"
        assert "friction: debt_pr_link: skipped_no_pr" in result.stderr

    def test_an_unresolvable_pr_degrades(self, repo: Path, cli: FakeGhCli) -> None:
        _register_debt_client(repo, name=_CLIENT)
        _filed_row()
        cli.reply("gh", "pr", "view", returncode=1)
        result = _debt("link-pr", "--pr", "9")
        assert result.exit_code == 0
        assert _json(result)["status"] == "degraded"
        assert "friction: debt_pr_link_failed: 1" in result.stderr

    def test_a_comment_failure_degrades(self, repo: Path, cli: FakeGhCli) -> None:
        _register_debt_client(repo, name=_CLIENT)
        _filed_row()
        self._script(cli)
        cli.reply("gh", "issue", "comment", returncode=1)
        result = _debt("link-pr", "--pr", "9")
        assert result.exit_code == 0
        assert (_json(result)["status"], _json(result)["failed"]) == ("degraded", 1)
        assert "friction: debt_pr_link_failed: 1" in result.stderr

    def test_an_unset_tracker_never_reaches_gh(
        self, repo: Path, cli: FakeGhCli
    ) -> None:
        # Regression (#3041 MUST_FIX): with no GitHub tracker, GhClient(cwd=None)
        # would act on the process repo. The link must skip before any gh call.
        _register_debt_client(repo, name=_CLIENT, tracker=None)
        _filed_row()
        result = _debt("link-pr", "--pr", "9")
        assert result.exit_code == 0, result.output
        assert (_json(result)["status"], _json(result)["skipped"]) == (
            "nothing_to_link",
            1,
        )
        assert "friction: debt_filing: skipped_no_tracker" in result.stderr
        assert cli.records == []

    def test_emitted_records_are_skipped(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _emitted_row("k1")
        result = _debt("link-pr", "--pr", "9")
        assert _json(result)["skipped"] == 1

    def test_a_missing_row_is_exit_1(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT)
        result = _debt("link-pr", "--pr", "9")
        assert result.exit_code == 1
        assert "no dev-queue task found" in result.output

    def test_pr_must_be_an_integer(self, repo: Path) -> None:
        assert _debt("link-pr", "--pr", "nine").exit_code == 2


def test_filed_at_is_a_utc_timestamp(
    repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github(repo, monkeypatch)
    _artifact(repo)
    before = datetime.now(UTC)
    assert _debt("file").exit_code == 0
    (record,) = read_review_debt(_TICKET, _CLIENT)
    assert record.filed_at >= before


class TestOutsideASession:
    """No default artifact outside a cw session (#3113)."""

    def test_file_reads_nothing_and_skips(
        self, plain_repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(plain_repo, monkeypatch)
        _artifact(plain_repo)
        result = _debt("file")
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert (data["status"], data["reason"], data["filed"]) == (
            "skipped",
            "skipped_no_session",
            0,
        )
        assert _NO_SESSION in result.stderr
        assert cli.records == []

    def test_file_with_an_explicit_artifact_files(
        self, plain_repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(plain_repo, monkeypatch)
        artifact = _artifact(plain_repo)
        result = _debt("file", "--deferred-findings", str(artifact))
        assert result.exit_code == 0, result.output
        assert (_json(result)["status"], _json(result)["filed"]) == ("complete", 1)
        assert "  ticket: " in artifact.read_text(encoding="utf-8")

    def test_emit_skips(self, plain_repo: Path) -> None:
        _register_debt_client(plain_repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        _artifact(plain_repo)
        result = _debt("emit", "--out-dir", str(plain_repo / "payloads"))
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert (data["status"], data["reason"], data["count"]) == (
            "skipped",
            "skipped_no_session",
            0,
        )
        assert _NO_SESSION in result.stderr
        assert not (plain_repo / "payloads").exists()

    def test_emit_with_an_explicit_artifact_emits(self, plain_repo: Path) -> None:
        _register_debt_client(plain_repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        artifact = _artifact(plain_repo)
        out = plain_repo / "payloads"
        result = _debt(
            "emit", "--deferred-findings", str(artifact), "--out-dir", str(out)
        )
        assert (result.exit_code, _json(result)["status"]) == (0, "emitted")

    def test_record_records_without_writing_back(
        self, plain_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _linear(plain_repo, monkeypatch)
        url = "https://linear.app/acme/issue/ENG-7/x"
        ids = json.dumps([{"key": key, "ticket_id": "ENG-7", "url": url}])
        result = _debt("record", "--ids", "-", stdin=ids)
        assert result.exit_code == 0, result.output
        assert read_review_debt(_TICKET, _CLIENT)[0].ticket_id == "ENG-7"
        artifact = plain_repo / ".cw" / "deferred-findings.md"
        assert "  ticket: " not in artifact.read_text(encoding="utf-8")

    def test_the_identity_error_still_comes_first(self, plain_repo: Path) -> None:
        result = CliRunner().invoke(main, ["review", "debt", "file"])
        assert result.exit_code == 2
        assert "no client or ticket" in result.output


class TestFixtureEntries:
    """A fixture-path entry in the artifact is never filed (#3113)."""

    def test_file_refuses_it(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _FIXTURE_ENTRY)
        result = _debt("file")
        assert result.exit_code == 0, result.output
        assert (_json(result)["status"], _json(result)["unticketed"]) == (
            "complete",
            0,
        )
        assert "debt filing refused [fixture_path] src/cw/foo.py: " in result.stderr
        assert _REFUSED in result.stderr
        assert cli.records == []

    def test_emit_refuses_it(self, repo: Path) -> None:
        _register_debt_client(repo, name=_CLIENT, tracker="linear")
        _seed_review_row(_TICKET, _CLIENT)
        _artifact(repo, _FIXTURE_ENTRY)
        result = _debt("emit", "--out-dir", str(repo / "payloads"))
        assert (result.exit_code, _json(result)["status"]) == (0, "nothing_to_emit")
        assert _REFUSED in result.stderr

    def test_a_ticketed_fixture_entry_is_not_reported(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _FIXTURE_ENTRY.model_copy(update={"ticket": "#5"}))
        result = _debt("file")
        assert result.exit_code == 0, result.output
        assert "refused" not in result.stderr


_STALE = _ENTRY.model_copy(update={"summary": "Fixed in a later round"})
_CURRENT = _ENTRY.model_copy(update={"summary": "Still raised in the last round"})
_STALE_NOTE = "debt filing skipped 1 deferral(s) not raised in the last 2 review rounds"


def _ledger_with_defers(
    repo: Path, last_seen: list[tuple[Adjudication, int | None]]
) -> None:
    """Write a four-round ledger whose settled defers carry *last_seen*."""
    base = ledger_with_rounds(*([round_finding("MUST_FIX", f"F{i}")] for i in range(4)))
    settled = [
        SettledEntry(
            identity=finding_identity(entry.severity, entry.file, entry.summary),
            outcome="defer",
            rationale="later",
            round=1,
            debt_key=debt_entry(entry).key,
            last_seen_round=seen,
        )
        for entry, seen in last_seen
    ]
    write_ledger(repo, base.model_copy(update={"settled": settled}))


class TestStaleDeferFilter:
    """``file`` skips defers the final review rounds no longer raise."""

    def test_a_stale_defer_is_skipped_and_stays_in_the_artifact(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        artifact = _artifact(repo, _STALE, _CURRENT)
        _ledger_with_defers(repo, [(_STALE, 1), (_CURRENT, 4)])
        result = _debt("file")
        assert result.exit_code == 0, result.output
        assert (_json(result)["filed"], _json(result)["unticketed"]) == (1, 0)
        assert _STALE_NOTE in result.stderr
        creates = [c for c in cli.records if c.argv[:3] == ["gh", "issue", "create"]]
        assert len(creates) == 1
        assert "Still raised in the last round" in (creates[0].body_file or "")
        text = artifact.read_text(encoding="utf-8")
        assert "Fixed in a later round" in text
        assert text.count("  ticket: ") == 1

    def test_an_unknown_last_seen_round_is_filed(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _STALE)
        _ledger_with_defers(repo, [(_STALE, None)])
        result = _debt("file")
        assert _json(result)["filed"] == 1
        assert _STALE_NOTE not in result.stderr

    def test_a_ticketed_stale_defer_is_left_alone(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _STALE.model_copy(update={"ticket": "#5"}), _CURRENT)
        _ledger_with_defers(repo, [(_STALE, 1), (_CURRENT, 4)])
        result = _debt("file")
        assert _json(result)["filed"] == 1
        assert _STALE_NOTE not in result.stderr

    def test_no_ledger_files_everything(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _STALE, _CURRENT)
        result = _debt("file")
        assert _json(result)["filed"] == 2
        assert "ledger unreadable" not in result.stderr

    def test_an_unreadable_ledger_files_everything_and_says_so(
        self, repo: Path, cli: FakeGhCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _github(repo, monkeypatch)
        _artifact(repo, _STALE, _CURRENT)
        ledger = repo / ".cw" / "review-convergence.json"
        ledger.write_text("{not json", encoding="utf-8")
        result = _debt("file")
        assert result.exit_code == 0, result.output
        assert _json(result)["filed"] == 2
        assert "convergence ledger unreadable (ValidationError)" in result.stderr
