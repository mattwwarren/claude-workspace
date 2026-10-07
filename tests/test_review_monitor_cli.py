"""Characterization tests for the review-monitor CLI (#2499).

Pins the argparse contract against a golden snapshot and dispatches every
subcommand through the real ``main()`` (moving to
``review_monitor_lib/cli.py``). Every test here runs under the
``review_monitor_state_dir`` fixture (autouse below), so ``consume-pending``
and ``enqueue-action`` only ever see tmp copies of the pending inbox and the
Desktop queue; ``run_cli`` additionally refuses to dispatch while any of those
paths holds its real, import-time value.

The golden fixture ``tests/fixtures/review_monitor_cli_contract.json`` was
generated once against the unsplit monolith, from the repo root, with::

    uv run python -c 'from tests._review_monitor_helpers import get, \
extract_cli_contract, format_cli_contract, CLI_CONTRACT_FIXTURE; \
CLI_CONTRACT_FIXTURE.write_text(format_cli_contract(extract_cli_contract(\
get("_build_argument_parser")())))'

Regenerate it only for an intentional CLI change, and review the diff.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-10-05T16:00:00+00:00"
_PR_VIEW = json.dumps(
    {
        "state": "OPEN",
        "headRefOid": "deadbeef",
        "statusCheckRollup": list(helpers.ROLLUP_GREEN),
        "mergeStateStatus": "CLEAN",
    }
)


@pytest.fixture(autouse=True)
def _isolated(review_monitor_state_dir: Path) -> None:
    """Every CLI test runs against tmp state, inbox and queue paths."""


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> helpers.FakeCommands:
    """Seed acme/widgets#42 and route every gh call the subcommands make."""
    thread = helpers.get("ThreadStatus")(file="a.py", line=3)
    review = helpers.get("CommentReviewRef")(
        review_id="5", author="bob", submitted_at="2026-09-02", body="hm"
    )
    helpers.seed_prs(
        helpers.make_pr(
            repo_path=str(tmp_path / "no-checkout"),
            our_threads=["t1"],
            thread_status={"t1": thread},
            comment_reviews={"5": review},
            slack_channel="C1",
            slack_ts="1.0",
        )
    )
    fake = helpers.FakeCommands()
    fake.add(("gh", "pr", "view"), _PR_VIEW)
    fake.add(("gh", "api", "user"), "matt-w")
    fake.add(("gh", "review", "view"), '{"threads": []}')
    fake.add(("gh", "api", "repos/acme/widgets/pulls/42/reviews"), "[]")
    fake.add(("gh", "search", "prs"), "[]")
    return fake.install(monkeypatch)


def test_cli_tests_never_resolve_real_inbox_or_queue(tmp_path: Path) -> None:
    for name in helpers.GUARDED_PATHS:
        assert helpers.get(name).is_relative_to(tmp_path), name


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("PENDING_INBOX_DIR", ["consume-pending"]),
        (
            "DESKTOP_QUEUE_DIR",
            ["enqueue-action", "--type", "cron_failure", "--payload", "{}"],
        ),
    ],
)
def test_run_cli_refuses_to_dispatch_against_real_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    argv: list[str],
) -> None:
    helpers.patch(monkeypatch, name, helpers.import_time_value(name))

    with pytest.raises(AssertionError, match=name):
        helpers.run_cli(monkeypatch, capsys, *argv)


def test_parser_matches_golden_contract() -> None:
    contract = helpers.extract_cli_contract(helpers.get("_build_argument_parser")())
    golden_text = helpers.CLI_CONTRACT_FIXTURE.read_text()

    assert json.loads(golden_text) == contract
    assert golden_text == helpers.format_cli_contract(contract)
    assert len(contract["commands"]) == 24


_DISPATCH: list[tuple[list[str], object]] = [
    (helpers.register_argv("abc999"), {"registered": True, "updated": True}),
    (["drop", "42", "--repo", helpers.REPO], {"dropped": True}),
    (
        ["complete", "42", "--repo", helpers.REPO, "--reason", "abandoned"],
        {"completed": True, "reason": "abandoned"},
    ),
    (["set-status", "42", "--repo", helpers.REPO, "--status", "approved"], ""),
    (
        ["confirm-thread", "42", "--repo", helpers.REPO, "--thread", "t1"],
        {"confirmed": "t1", "status": "ready_to_approve"},
    ),
    (
        ["ack-delta", "42", "--repo", helpers.REPO, "--sha", "s9"],
        {"delta_base_sha": "s9", "acked": True},
    ),
    (["nudge-ok", "42", "--repo", helpers.REPO], {"allowed": False}),
    (["record-nudge", "42", "--repo", helpers.REPO], ""),
    (
        [
            "mark-comment-review",
            "42",
            "--repo",
            helpers.REPO,
            "--review-id",
            "5",
            "--classification",
            "neutral",
        ],
        {"ok": True, "classification": "neutral"},
    ),
    (["mark-notified", "42", "--repo", helpers.REPO, "--state", "ci_failing"], ""),
    (["mark-escalated", "42", "--repo", helpers.REPO], ""),
    (
        ["slack-thread-cursor", "42", "--repo", helpers.REPO],
        {"slack_channel": "C1", "slack_ts": "1.0"},
    ),
    (
        ["update-slack-cursor", "42", "--repo", helpers.REPO, "--last-seen-ts", "9.9"],
        "",
    ),
    (
        ["record-auto-fix", "42", "--repo", helpers.REPO],
        {"attempts_today": 1, "capped": False},
    ),
    (["record-channel-bump", "42", "--repo", helpers.REPO], {"channel_bump_count": 1}),
    (["status", "--repo", helpers.REPO, "--json"], {"completed": {}}),
    (["list-repos"], "acme/widgets\n"),
    (["check", "42", "--repo", helpers.REPO], {"pr_state": "OPEN", "changed": False}),
    (["consume-pending"], {"consumed": [], "skipped": [], "purged": []}),
    (["catchup"], {"marked": []}),
    (["pending-channel-bumps"], []),
    (
        ["discover", "--repo", helpers.REPO, "--repo-path", "/r"],
        {"registered": [], "skipped": []},
    ),
    (
        ["recover-reviews", "--repo", helpers.REPO, "--repo-path", "/r", "--days", "2"],
        {"recovered": []},
    ),
    (
        ["enqueue-action", "--type", "cron_failure", "--payload", '{"n": 1}'],
        {"action": "cron_failure"},
    ),
]


def test_dispatch_table_covers_every_subcommand() -> None:
    golden = json.loads(helpers.CLI_CONTRACT_FIXTURE.read_text())

    assert {argv[0] for argv, _ in _DISPATCH} == set(golden["commands"])


@freeze_time(_NOW)
@pytest.mark.parametrize(
    ("argv", "expected"), _DISPATCH, ids=[argv[0] for argv, _ in _DISPATCH]
)
def test_main_dispatches_subcommand(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: object,
) -> None:
    code, out, err = helpers.run_cli(monkeypatch, capsys, *argv)

    assert (code, err) == (0, "")
    if isinstance(expected, str):
        assert out == expected
    elif isinstance(expected, dict):
        parsed: dict[str, Any] = json.loads(out)
        assert {k: parsed[k] for k in expected} == expected
    else:
        assert json.loads(out) == expected


@freeze_time(_NOW)
def test_void_mutations_persist_through_main(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = ["42", "--repo", helpers.REPO]
    for argv in (
        ["set-status", *repo, "--status", "approved"],
        ["record-nudge", *repo],
        ["mark-notified", *repo, "--state", "ci_failing"],
        ["mark-escalated", *repo],
        ["update-slack-cursor", *repo, "--last-seen-ts", "9.9"],
    ):
        helpers.run_cli(monkeypatch, capsys, *argv)

    pr = helpers.stored_pr()
    assert pr.status == "approved"
    assert (pr.nudge_count, pr.last_notified_state, pr.escalation_count) == (
        1,
        "ci_failing",
        1,
    )
    assert pr.slack_last_seen_ts == "9.9"


def test_list_repos_json(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert helpers.run_cli(monkeypatch, capsys, "list-repos", "--json") == (
        0,
        '["acme/widgets"]\n',
        "",
    )


def test_status_derives_repo_from_checkout(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded.add(("gh", "repo", "view"), helpers.REPO)

    code, out, _err = helpers.run_cli(monkeypatch, capsys, "status")

    assert code == 0
    assert out.splitlines()[2].startswith("acme/widgets#42")
    assert seeded.argvs()[-1] == (
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner",
        "--jq",
        ".nameWithOwner",
    )


def test_status_without_derivable_repo_exits_1(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded.add(("gh", "repo", "view"), "")

    code, out, err = helpers.run_cli(monkeypatch, capsys, "status")

    assert (code, out) == (1, "")
    assert err.startswith("Error: --repo is required when --all is not set")


def test_status_all_table_and_json(
    seeded: helpers.FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _code, table, _err = helpers.run_cli(monkeypatch, capsys, "status", "--all")
    _code, raw, _err = helpers.run_cli(monkeypatch, capsys, "status", "--all", "--json")

    assert table.splitlines() == [
        f"{'PR':<40} ROLE       STATUS       THREADS ADDRESSED",
        "-" * 80,
        f"{helpers.KEY:<40} author     watching     0/1",
        "",
        "0 completed PR(s) in history.",
    ]
    assert set(json.loads(raw)["monitored"]) == {helpers.KEY}


def test_status_all_with_nothing_monitored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert helpers.run_cli(monkeypatch, capsys, "status", "--all") == (
        0,
        "No PRs currently monitored across any repo.\n\n"
        "0 completed PR(s) in history.\n",
        "",
    )


def test_enqueue_action_malformed_payload_propagates_decode_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unlike register/drop/complete, enqueue-action is not wrapped in
    # _emit_mutation_result: a malformed --payload escapes main() as a
    # traceback rather than an "Error:" line (pinned as-is by #2499).
    with pytest.raises(json.JSONDecodeError):
        helpers.run_cli(
            monkeypatch,
            capsys,
            "enqueue-action",
            "--type",
            "cron_failure",
            "--payload",
            "{bad",
        )

    assert not helpers.get("DESKTOP_QUEUE_DIR").exists()


@pytest.mark.parametrize(
    "argv",
    [[], ["register", "42"], ["enqueue-action", "--type", "tweet", "--payload", "{}"]],
    ids=["no-subcommand", "missing-required", "bad-choice"],
)
def test_argparse_rejects_invalid_invocations_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    code, out, err = helpers.run_cli(monkeypatch, capsys, *argv)

    assert (code, out) == (2, "")
    assert "error:" in err
