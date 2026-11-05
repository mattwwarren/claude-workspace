"""``cw dev-queue adopted-prs -c <client>`` -- finalize Step 4a inventory (#2949)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.cli.dev_queue.adopted_prs import adopted_sibling_lines
from cw.models import ClientConfig, PrState, QueueItemStatus, TicketTask
from tests._adopt_pr_helpers import ADOPT_CLIENT, make_adopted_task
from tests._clients_yaml import write_clients_yaml
from tests._ticket_view_helpers import invoke_dev_queue, seed_queue, stdout_lines

ADOPT_REPO = "https://github.com/acme/widgets"
ADOPTED_URL_2933 = f"{ADOPT_REPO}/pull/2933"
FOREIGN_BRANCH = "feat/review-feedback-context"


def _row(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "ticket_id": "ADOPT-1",
        "branch": FOREIGN_BRANCH,
        "adopted_head_sha": None,
        "adopted_pr_url": ADOPTED_URL_2933,
        "pr_url": ADOPTED_URL_2933,
        "status": QueueItemStatus.PENDING,
    }
    kwargs.update(overrides)
    return make_adopted_task(**kwargs)


@pytest.mark.parametrize(
    "status",
    [
        QueueItemStatus.PENDING,
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ],
)
def test_live_statuses_print_a_line(status: QueueItemStatus) -> None:
    seed_queue([_row(status=status)])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == ["2933 " + FOREIGN_BRANCH]


def test_adopted_row_prints_pr_number_and_foreign_branch() -> None:
    seed_queue([_row()])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == [f"2933 {FOREIGN_BRANCH}"]


@pytest.mark.parametrize(
    ("pr_state", "pr_url", "expected"),
    [
        (PrState(state="OPEN"), ADOPTED_URL_2933, True),
        (PrState(state="MERGED"), ADOPTED_URL_2933, False),
        (PrState(state="CLOSED"), ADOPTED_URL_2933, False),
        (None, ADOPTED_URL_2933, True),
        (None, None, False),
    ],
    ids=["open", "merged", "closed", "unhydrated-with-url", "unhydrated-no-url"],
)
def test_completed_rows_follow_pr_liveness(
    pr_state: PrState | None, pr_url: str | None, expected: bool
) -> None:
    seed_queue(
        [
            _row(
                status=QueueItemStatus.COMPLETED,
                pr_state=pr_state,
                pr_url=pr_url,
            )
        ]
    )
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == ([f"2933 {FOREIGN_BRANCH}"] if expected else [])


@pytest.mark.parametrize("status", [QueueItemStatus.FAILED, QueueItemStatus.CANCELLED])
def test_terminal_non_completed_rows_print_nothing(status: QueueItemStatus) -> None:
    seed_queue([_row(status=status)])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == []


def test_non_adopted_row_never_appears() -> None:
    plain = make_adopted_task(
        ticket_id="PLAIN-42",
        branch="dev/42",
        adopted_head_sha=None,
        adopted_pr_url=None,
        pr_url=None,
    )
    seed_queue([plain])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == []


def test_other_clients_rows_are_excluded() -> None:
    seed_queue([_row(client="other-client")])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == []


def test_multiple_rows_keep_queue_order() -> None:
    seed_queue(
        [
            _row(
                ticket_id="ADOPT-B",
                adopted_pr_url=f"{ADOPT_REPO}/pull/20",
                pr_url=f"{ADOPT_REPO}/pull/20",
                branch="feat/b",
            ),
            _row(
                ticket_id="ADOPT-A",
                adopted_pr_url=f"{ADOPT_REPO}/pull/10",
                pr_url=f"{ADOPT_REPO}/pull/10",
                branch="feat/a",
            ),
        ]
    )
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == ["20 feat/b", "10 feat/a"]


def test_unparseable_url_prints_dash_for_number() -> None:
    seed_queue([_row(adopted_pr_url="not-a-pr-url")])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == [f"- {FOREIGN_BRANCH}"]


def test_branch_resolves_through_client_prefix_when_row_has_no_branch(
    tmp_path: Path,
) -> None:
    write_clients_yaml(
        ClientConfig(
            name=ADOPT_CLIENT,
            workspace_path=tmp_path / "ws",
            feature_branch_prefix="team",
        ),
        ensure_workspaces=True,
    )
    seed_queue([_row(branch=None, adopted_head_sha=None)])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == ["2933 team/ADOPT-1"]


def test_unknown_client_falls_back_to_dev_prefix() -> None:
    seed_queue([_row(client="ghost", branch=None, adopted_head_sha=None)])
    result = invoke_dev_queue("adopted-prs", "-c", "ghost")
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == ["2933 dev/ADOPT-1"]


def test_empty_queue_prints_nothing_and_exits_zero() -> None:
    seed_queue([])
    result = invoke_dev_queue("adopted-prs", "-c", ADOPT_CLIENT)
    assert result.exit_code == 0, result.output
    assert stdout_lines(result) == []


def test_client_option_is_required() -> None:
    result = invoke_dev_queue("adopted-prs")
    assert result.exit_code == 2
    assert "--client" in result.output


def test_adopted_sibling_lines_pure_helper_matches_cli_contract() -> None:
    lines = adopted_sibling_lines(
        [_row(), _row(ticket_id="X-2", client="other")], ADOPT_CLIENT, None
    )
    assert lines == [f"2933 {FOREIGN_BRANCH}"]
