"""Characterization tests for review-monitor status reporting (#2499).

Covers ``cmd_status`` (table and ``--json``) and ``cmd_status_all``'s
cross-repo merge (moving to ``review_monitor_lib/status.py``). The
``status --all`` table printer lives in the CLI and is covered in
``tests/test_review_monitor_cli.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tests import _review_monitor_helpers as helpers

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _status(**fields: Any) -> Any:
    return helpers.get("ThreadStatus")(**{"file": "a.py", "line": 1, **fields})


def test_status_empty_repo(
    review_monitor_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.get("cmd_status")(helpers.REPO)

    assert capsys.readouterr().out == (
        "No PRs currently monitored.\n\n0 completed PR(s) in history.\n"
    )


def test_status_table_rows_are_sorted_with_thread_and_review_columns(
    review_monitor_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.seed_prs(
        helpers.make_pr(
            pr_number=7,
            thread_status={"a": _status(resolved=True), "b": _status()},
            awaiting_rereview=True,
        ),
        helpers.make_pr(pr_number=12, role="reviewer", status="approved"),
        completed={"acme/widgets#1": {"reason": "merged"}},
    )

    helpers.get("cmd_status")(helpers.REPO)

    assert capsys.readouterr().out.splitlines() == [
        "PR                   ROLE       STATUS       THREADS    REVIEW",
        "-" * 70,
        "acme/widgets#12      reviewer   approved     n/a        ",
        "acme/widgets#7       author     watching     1/2        re-review",
        "",
        "1 completed PR(s) in history.",
    ]


def test_status_json_is_the_full_state(
    review_monitor_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.seed_prs(helpers.make_pr())

    helpers.get("cmd_status")(helpers.REPO, as_json=True)

    out = capsys.readouterr().out
    assert json.loads(out) == helpers.get("load_state")(helpers.REPO).to_dict()
    assert out.startswith('{\n  "monitored"')


def test_status_all_merges_every_repo(review_monitor_state_dir: Path) -> None:
    helpers.seed_prs(helpers.make_pr(), completed={"acme/widgets#1": {"r": 1}})
    helpers.seed_prs(
        helpers.make_pr(repo="zeta/app", pr_number=3),
        completed={"zeta/app#2": {"r": 2}},
    )

    combined = helpers.get("cmd_status_all")()

    assert set(combined.monitored) == {helpers.KEY, "zeta/app#3"}
    assert set(combined.completed) == {"acme/widgets#1", "zeta/app#2"}


def test_status_all_without_state_dir_is_empty(review_monitor_state_dir: Path) -> None:
    combined = helpers.get("cmd_status_all")()

    assert (combined.monitored, combined.completed) == ({}, {})
