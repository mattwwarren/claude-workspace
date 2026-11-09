"""Tests for the review-debt filing primitives in ``cw.gh.filing`` (#3041).

Every ``gh`` call goes through :class:`tests._tool_fakes.FakeGhCli`; no test
reaches a real ``gh``. The stdout and stderr payloads come from captured runs
in ``tests/fixtures/gh_*`` (redacted), except the missing-label stderr, which
could not be captured without a write to a real repo (see that test).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cw import gh
from cw.gh import (
    GhFailure,
    LabelledIssue,
    close_issue,
    ensure_label,
    fetch_issue_state,
    list_open_issues_by_label,
)
from cw.sprint import GhClient
from tests._tool_fakes import FakeGhCli

_FIXTURES = Path(__file__).parent / "fixtures"
_CREATE_STDOUT = (_FIXTURES / "gh_issue_create_stdout.txt").read_text(encoding="utf-8")
_AUTH_STDERR = (_FIXTURES / "gh_issue_create_stderr_auth.txt").read_text(
    encoding="utf-8"
)
_NETWORK_STDERR = (_FIXTURES / "gh_issue_create_stderr_network.txt").read_text(
    encoding="utf-8"
)
_LIST_STDOUT = (_FIXTURES / "gh_issue_list_review_debt.json").read_text(
    encoding="utf-8"
)
# UNCAPTURED: the probe that would capture this (`gh issue edit --add-label`
# with a label that does not exist) is a write to a real repo and was denied
# during #3041. This is the hypothesis the LABEL classifier encodes; replace it
# with a real capture when one is available.
_LABEL_STDERR_HYPOTHESIS = "could not add label: 'review-debt' not found\n"

_LIST_PREFIX = ("gh", "issue", "list")
_CREATE_PREFIX = ("gh", "issue", "create")
_LABEL_PREFIX = ("gh", "label", "create")
_REPO = Path("/tmp/client-repo")
_LIMIT = 200


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    return FakeGhCli().install(monkeypatch)


class TestEnsureLabel:
    def test_runs_label_create_force_with_color_and_description(
        self, cli: FakeGhCli
    ) -> None:
        cli.reply(*_LABEL_PREFIX)
        assert ensure_label("review-debt", "d4c5f9", "Debt", cwd=_REPO) is None
        assert cli.argvs() == [
            [
                "gh",
                "label",
                "create",
                "review-debt",
                "--force",
                "--color",
                "d4c5f9",
                "--description",
                "Debt",
            ]
        ]
        assert cli.records[0].cwd == _REPO

    def test_classifies_an_auth_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_LABEL_PREFIX, returncode=1, stderr=_AUTH_STDERR)
        assert ensure_label("review-debt", "d4c5f9", "Debt") is GhFailure.AUTH

    def test_a_timeout_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_LABEL_PREFIX, raises=subprocess.TimeoutExpired(["gh"], 1))
        assert ensure_label("review-debt", "d4c5f9", "Debt") is GhFailure.NETWORK

    def test_forwards_the_timeout(self, cli: FakeGhCli) -> None:
        cli.reply(*_LABEL_PREFIX)
        ensure_label("review-debt", "d4c5f9", "Debt", timeout=7)
        assert cli.records[0].timeout == 7


class TestCreateIssueDebt:
    def test_no_milestone_makes_no_patch_call(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, stdout=_CREATE_STDOUT)
        result = gh.create_issue("t", "body", labels=["review-debt"], cwd=_REPO)
        assert result.number == 3070
        assert result.url == _CREATE_STDOUT.strip()
        assert result.failure is None
        assert [argv[:3] for argv in cli.argvs()] == [list(_CREATE_PREFIX)]
        assert cli.records[0].cwd == _REPO
        assert cli.records[0].body_file == "body"

    def test_a_timeout_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, raises=subprocess.TimeoutExpired(["gh"], 1))
        result = gh.create_issue("t", "b", labels=[])
        assert result.failure is GhFailure.NETWORK
        assert result.number is None

    def test_an_oserror_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, raises=OSError("no gh"))
        result = gh.create_issue("t", "b", labels=[])
        assert result.failure is GhFailure.NETWORK
        assert "no gh" in result.detail

    def test_captured_network_stderr_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, returncode=1, stderr=_NETWORK_STDERR)
        result = gh.create_issue("t", "b", labels=[])
        assert result.failure is GhFailure.NETWORK
        assert result.detail == _NETWORK_STDERR.splitlines()[0]

    def test_captured_auth_stderr_is_an_auth_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, returncode=1, stderr=_AUTH_STDERR)
        assert gh.create_issue("t", "b", labels=[]).failure is GhFailure.AUTH

    def test_missing_label_stderr_is_a_label_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, returncode=1, stderr=_LABEL_STDERR_HYPOTHESIS)
        assert gh.create_issue("t", "b", labels=[]).failure is GhFailure.LABEL

    def test_unrecognised_stderr_is_other(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, returncode=1, stderr="")
        result = gh.create_issue("t", "b", labels=[])
        assert result.failure is GhFailure.OTHER
        assert result.detail == ""

    def test_forwards_cwd_to_the_milestone_attach(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, stdout=_CREATE_STDOUT)
        cli.reply("gh", "api", stdout='{"milestone": {"number": 4}}')
        result = gh.create_issue("t", "b", labels=[], milestone=4, cwd=_REPO)
        assert result.number == 3070
        assert [call.cwd for call in cli.records] == [_REPO, _REPO]


class TestListOpenIssuesByLabel:
    def test_runs_the_exact_listing_and_never_searches(self, cli: FakeGhCli) -> None:
        cli.reply(*_LIST_PREFIX, stdout=_LIST_STDOUT)
        lookup = list_open_issues_by_label("review-debt", cwd=_REPO)
        argv = cli.argvs()[0]
        assert argv == [
            "gh",
            "issue",
            "list",
            "--label",
            "review-debt",
            "--state",
            "open",
            "--json",
            "number,body",
            "--limit",
            "200",
        ]
        assert "--search" not in argv
        assert cli.records[0].cwd == _REPO
        assert lookup.failure is None
        assert lookup.truncated is False
        assert lookup.issues == (
            LabelledIssue(3069, "<redacted issue body>"),
            LabelledIssue(2998, "<redacted issue body>"),
        )

    def test_a_full_page_is_flagged_truncated(self, cli: FakeGhCli) -> None:
        page = [{"body": "x", "number": n} for n in range(_LIMIT)]
        cli.reply(*_LIST_PREFIX, stdout=json.dumps(page))
        lookup = list_open_issues_by_label("review-debt")
        assert lookup.truncated is True
        assert len(lookup.issues) == _LIMIT

    def test_a_gh_failure_is_typed(self, cli: FakeGhCli) -> None:
        cli.reply(*_LIST_PREFIX, returncode=1, stderr=_AUTH_STDERR)
        lookup = list_open_issues_by_label("review-debt")
        assert lookup.failure is GhFailure.AUTH
        assert lookup.issues == ()

    def test_a_timeout_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_LIST_PREFIX, raises=subprocess.TimeoutExpired(["gh"], 1))
        assert list_open_issues_by_label("x").failure is GhFailure.NETWORK

    @pytest.mark.parametrize("stdout", ["not json", '{"a": 1}'])
    def test_an_unparseable_listing_is_other(self, cli: FakeGhCli, stdout: str) -> None:
        cli.reply(*_LIST_PREFIX, stdout=stdout)
        assert list_open_issues_by_label("x").failure is GhFailure.OTHER

    def test_malformed_items_fail_closed(self, cli: FakeGhCli) -> None:
        payload = [1, {"number": "7", "body": "b"}, {"number": 8, "body": None}]
        payload.append({"number": 9, "body": "kept"})
        cli.reply(*_LIST_PREFIX, stdout=json.dumps(payload))
        lookup = list_open_issues_by_label("x")
        assert lookup.failure is GhFailure.OTHER
        assert lookup.issues == ()


_CLOSE_PREFIX = ("gh", "issue", "close")
_MULTILINE_COMMENT = "Closing as not planned.\n\ncw review-debt key `abc`\n\n<!-- x -->"


class TestCloseIssue:
    def test_runs_one_close_with_the_comment_as_a_single_argv_element(
        self, cli: FakeGhCli
    ) -> None:
        cli.reply(*_CLOSE_PREFIX)
        result = close_issue(
            7, _MULTILINE_COMMENT, reason="not planned", timeout=9, cwd=_REPO
        )
        assert result is None
        assert cli.argvs() == [
            [
                "gh",
                "issue",
                "close",
                "7",
                "--comment",
                _MULTILINE_COMMENT,
                "--reason",
                "not planned",
            ]
        ]
        record = cli.records[0]
        assert (record.cwd, record.timeout, record.stdin) == (_REPO, 9, None)

    def test_never_uses_a_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, object]] = []

        def _run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            seen.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr("cw.gh._sp.run", _run)
        assert close_issue(7, "c", reason="not planned") is None
        assert "shell" not in seen[0]
        assert seen[0]["timeout"] == 30

    def test_a_timeout_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CLOSE_PREFIX, raises=subprocess.TimeoutExpired(["gh"], 1))
        assert close_issue(7, "c", reason="not planned") is GhFailure.NETWORK

    def test_an_oserror_is_a_network_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CLOSE_PREFIX, raises=OSError("no gh"))
        assert close_issue(7, "c", reason="not planned") is GhFailure.NETWORK

    def test_captured_auth_stderr_is_an_auth_failure(self, cli: FakeGhCli) -> None:
        cli.reply(*_CLOSE_PREFIX, returncode=1, stderr=_AUTH_STDERR)
        assert close_issue(7, "c", reason="not planned") is GhFailure.AUTH

    def test_unrecognised_stderr_is_other(self, cli: FakeGhCli) -> None:
        cli.reply(*_CLOSE_PREFIX, returncode=1, stderr="")
        assert close_issue(7, "c", reason="not planned") is GhFailure.OTHER


_VIEW_PREFIX = ("gh", "issue", "view")


class TestFetchIssueState:
    def test_parses_the_state(self, cli: FakeGhCli) -> None:
        cli.reply(*_VIEW_PREFIX, stdout='{"state": "CLOSED"}')
        assert fetch_issue_state("12", 4, cwd=_REPO) == "CLOSED"
        assert cli.argvs() == [["gh", "issue", "view", "12", "--json", "state"]]
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, 4)

    def test_a_non_zero_exit_is_unknown(self, cli: FakeGhCli) -> None:
        cli.reply(*_VIEW_PREFIX, returncode=1, stderr=_AUTH_STDERR)
        assert fetch_issue_state("12", 4) is None

    def test_a_timeout_is_unknown(self, cli: FakeGhCli) -> None:
        cli.reply(*_VIEW_PREFIX, raises=subprocess.TimeoutExpired(["gh"], 1))
        assert fetch_issue_state("12", 4) is None

    @pytest.mark.parametrize("stdout", ["not json", "[1]", '{"state": 3}', "{}"])
    def test_bad_output_is_unknown(self, cli: FakeGhCli, stdout: str) -> None:
        cli.reply(*_VIEW_PREFIX, stdout=stdout)
        assert fetch_issue_state("12", 4) is None


def test_update_issue_body_forwards_cwd(cli: FakeGhCli) -> None:
    cli.reply("gh", "issue", "edit")
    assert gh.update_issue_body(5, "new", cwd=_REPO) is True
    assert cli.records[0].cwd == _REPO


class TestGhClientDebtCalls:
    """``GhClient`` forwards its ``cwd`` and an explicit timeout on each call."""

    def test_create_issue(self, cli: FakeGhCli) -> None:
        cli.reply(*_CREATE_PREFIX, stdout=_CREATE_STDOUT)
        result = GhClient(cwd=_REPO).create_issue("t", "b", labels=[], timeout=3)
        assert result.number == 3070
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, 3)

    def test_ensure_label(self, cli: FakeGhCli) -> None:
        cli.reply(*_LABEL_PREFIX)
        assert GhClient(cwd=_REPO).ensure_label("l", "c", "d") is None
        assert cli.records[0].cwd == _REPO

    def test_list_open_issues_by_label(self, cli: FakeGhCli) -> None:
        cli.reply(*_LIST_PREFIX, stdout=_LIST_STDOUT)
        lookup = GhClient(cwd=_REPO).list_open_issues_by_label("l", timeout=4)
        assert len(lookup.issues) == 2
        assert cli.records[0].timeout == 4

    def test_fetch_issue_body(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "issue", "view", stdout='{"body": "hello"}')
        assert GhClient(cwd=_REPO).fetch_issue_body("12") == "hello"
        assert cli.records[0].cwd == _REPO

    def test_post_issue_comment_true_on_success(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "issue", "comment")
        assert GhClient(cwd=_REPO).post_issue_comment("12", "hi", timeout=2) is True
        assert cli.records[0].stdin is not None

    def test_post_issue_comment_false_on_failure(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "issue", "comment", returncode=1)
        assert GhClient().post_issue_comment("12", "hi") is False

    def test_post_issue_comment_false_when_gh_cannot_run(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "issue", "comment", raises=OSError("gone"))
        assert GhClient().post_issue_comment("12", "hi") is False

    def test_update_issue_body(self, cli: FakeGhCli) -> None:
        cli.reply("gh", "issue", "edit")
        assert GhClient(cwd=_REPO).update_issue_body(5, "b", timeout=6) is True
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, 6)

    @pytest.mark.parametrize(
        ("timeout", "expected"), [(None, gh.FETCH_COMMENTS_TIMEOUT), (3, 3)]
    )
    def test_fetch_issue_state(
        self, cli: FakeGhCli, timeout: int | None, expected: int
    ) -> None:
        cli.reply(*_VIEW_PREFIX, stdout='{"state": "OPEN"}')
        assert GhClient(cwd=_REPO).fetch_issue_state("12", timeout=timeout) == "OPEN"
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, expected)

    @pytest.mark.parametrize(("timeout", "expected"), [(None, 15), (3, 3)])
    def test_fetch_pr_state(
        self, cli: FakeGhCli, timeout: int | None, expected: int
    ) -> None:
        url = "https://github.com/o/r/pull/4"
        cli.reply("gh", "pr", "view", stdout='{"state": "MERGED"}')
        assert GhClient(cwd=_REPO).fetch_pr_state(url, timeout=timeout) == "MERGED"
        assert cli.argvs() == [["gh", "pr", "view", url, "--json", "state"]]
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, expected)

    @pytest.mark.parametrize("stdout", ["{}", '{"state": null}', "not json"])
    def test_fetch_pr_state_unknown(self, cli: FakeGhCli, stdout: str) -> None:
        cli.reply("gh", "pr", "view", stdout=stdout)
        assert GhClient().fetch_pr_state("https://github.com/o/r/pull/4") is None

    @pytest.mark.parametrize(("timeout", "expected"), [(None, 30), (8, 8)])
    def test_close_issue(
        self, cli: FakeGhCli, timeout: int | None, expected: int
    ) -> None:
        cli.reply(*_CLOSE_PREFIX)
        client = GhClient(cwd=_REPO)
        assert client.close_issue(7, "c", reason="r", timeout=timeout) is None
        assert cli.argvs()[0][-2:] == ["--reason", "r"]
        assert (cli.records[0].cwd, cli.records[0].timeout) == (_REPO, expected)
