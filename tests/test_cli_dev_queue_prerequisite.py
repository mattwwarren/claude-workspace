"""``cw dev-queue prerequisite`` CLI: declare, remove and refusals (#3791)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.models import QueueItemStatus, Stage
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION
from cw.models.pr_ref import PrRef
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)
from tests._shared.merge_prerequisite_builders import (
    patch_repo_slug,
    prerequisite_entry,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    gh_pr_view_json,
    install_acme_client,
    invoke_dev_queue,
    make_task,
    queue_row,
    seed_queue,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_DECLARE = ("prerequisite", ACME_TICKET, "-c", ACME, "--pr", "9")
_REMOVE = (*_DECLARE, "--remove")
_OPEN_PR = gh_pr_view_json("foo/bar", 9)
_ADOPTED = "https://github.com/foo/bar/pull/77"


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The acme client, outside any worker, origin ``foo/bar``."""
    install_acme_client(tmp_path, monkeypatch)
    patch_repo_slug(monkeypatch, tmp_path, "foo/bar")


def _gh(monkeypatch: pytest.MonkeyPatch, stdout: str | None = _OPEN_PR) -> FakeGhCli:
    fake = FakeGhCli().install(monkeypatch)
    if stdout is not None:
        fake.reply("pr", "view", stdout=stdout)
    return fake


def _flat(result: Result) -> str:
    return " ".join(result.output.split())


def _held(**overrides: object) -> object:
    fields: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": DEPENDENCY_HOLD_DISPOSITION,
        "blocked_reason": DEPENDENCY_UNMERGED_BLOCKER_REASON,
        "blocked_on_pr": 9,
    }
    fields.update(overrides)
    return make_task(**fields)


@pytest.mark.usefixtures("acme")
class TestDeclare:
    def test_happy_path_one_gh_call_outside_any_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task()])
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 0, result.output
        assert len(fake.records) == 1
        assert all(snapshot == () for snapshot in fake.lock_snapshots)
        row = queue_row()
        [entry] = row.merge_prerequisites
        assert entry.pr == PrRef(number=9)
        assert entry.repo == "foo/bar"
        assert entry.actor != ""
        assert row.status == QueueItemStatus.PENDING
        assert row.disposition is None
        assert row.blocked_on_pr is None
        assert "Recorded merge prerequisite PR #9 for ACM-1 in acme" in _flat(result)
        # The resulting row is echoed after the confirmation line.
        assert "status: pending" in result.output.splitlines()

    def test_repeat_is_unchanged_with_no_gh_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(merge_prerequisites=[prerequisite_entry(9)])])
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 0, result.output
        assert fake.records == []
        assert "already has merge prerequisite PR #9 (unchanged)" in _flat(result)

    def test_a_racing_declare_reports_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-gh read missed it, the locked core saw it: still unchanged."""
        seed_queue([make_task(merge_prerequisites=[prerequisite_entry(9)])])
        monkeypatch.setattr(
            "cw.cli.dev_queue.prerequisite.has_prerequisite", lambda _t, _r: False
        )
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 0, result.output
        assert len(fake.records) == 1
        assert "already has merge prerequisite PR #9 (unchanged)" in _flat(result)
        assert len(queue_row().merge_prerequisites) == 1

    def test_review_row_with_an_adopted_pr_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(stage=Stage.REVIEW, adopted_pr_url=_ADOPTED)])
        _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 0, result.output
        assert [e.pr.number for e in queue_row().merge_prerequisites] == [9]

    @pytest.mark.parametrize(
        ("stdout", "message"),
        [
            (None, "could not verify PR #9"),
            (gh_pr_view_json("foo/bar", 9, "MERGED"), "PR #9 is already merged"),
            (gh_pr_view_json("foo/bar", 9, "CLOSED"), "PR #9 is closed unmerged"),
            (
                gh_pr_view_json("other/repo", 9),
                "PR #9 is not in client acme's repo foo/bar",
            ),
        ],
        ids=["unverified", "merged", "closed", "other-repo"],
    )
    def test_gh_refusals_leave_the_row_unmutated(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str | None, message: str
    ) -> None:
        seed_queue([make_task()])
        before = queue_row().model_dump()
        _gh(monkeypatch, stdout)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert message in _flat(result)
        assert queue_row().model_dump() == before

    def test_unresolved_slug_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_repo_slug(monkeypatch, tmp_path, None)
        seed_queue([make_task()])
        before = queue_row().model_dump()
        _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert "could not resolve the github repo for client acme" in _flat(result)
        assert queue_row().model_dump() == before

    @pytest.mark.parametrize(
        "status", [QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED]
    )
    def test_terminal_row_is_refused_before_gh(
        self, monkeypatch: pytest.MonkeyPatch, status: QueueItemStatus
    ) -> None:
        seed_queue([make_task(status=status)])
        before = queue_row().model_dump()
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert "a merge prerequisite applies only to a row" in _flat(result)
        assert fake.records == []
        assert queue_row().model_dump() == before

    def test_unknown_ticket_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task()])
        fake = _gh(monkeypatch)

        result = invoke_dev_queue("prerequisite", "NOPE-1", "-c", ACME, "--pr", "9")

        assert result.exit_code == 1
        assert "No dev-queue task found" in _flat(result)
        assert fake.records == []

    def test_cap_reached_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entries = [prerequisite_entry(n) for n in range(10, 18)]
        seed_queue([make_task(merge_prerequisites=entries)])
        before = queue_row().model_dump()
        _gh(monkeypatch)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert "already has 8 merge prerequisites" in _flat(result)
        assert queue_row().model_dump() == before


@pytest.mark.usefixtures("acme")
class TestRemove:
    def test_remove_releases_with_no_gh_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([_held(merge_prerequisites=[prerequisite_entry(9)])])
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(*_REMOVE)

        assert result.exit_code == 0, result.output
        assert fake.records == []
        assert queue_row().status == QueueItemStatus.PENDING
        assert "released the dependency hold" in _flat(result)

    def test_remove_restamps_to_the_next_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue(
            [_held(merge_prerequisites=[prerequisite_entry(9), prerequisite_entry(10)])]
        )
        _gh(monkeypatch)

        result = invoke_dev_queue(*_REMOVE)

        assert result.exit_code == 0, result.output
        assert queue_row().blocked_on_pr == PrRef(number=10)
        assert "the hold now waits on PR #10" in _flat(result)

    def test_remove_entry_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task(merge_prerequisites=[prerequisite_entry(9)])])
        _gh(monkeypatch)

        result = invoke_dev_queue(*_REMOVE)

        assert result.exit_code == 0, result.output
        assert queue_row().merge_prerequisites == []
        assert "Removed merge prerequisite PR #9 from ACM-1" in _flat(result)
        assert "released" not in _flat(result)

    def test_remove_unknown_entry_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task()])
        _gh(monkeypatch)

        result = invoke_dev_queue(*_REMOVE)

        assert result.exit_code == 1
        assert "has no merge prerequisite on PR #9" in _flat(result)


@pytest.mark.usefixtures("acme")
class TestUsageRefusals:
    def test_inside_a_dispatch_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task()])
        before = queue_row().model_dump()
        fake = _gh(monkeypatch)
        monkeypatch.setattr(
            "cw.cli._hook_io.find_cw_context", lambda _path: {"headless": True}
        )

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert "operator verb" in _flat(result)
        assert fake.records == []
        assert queue_row().model_dump() == before

    def test_cross_repo_ref_points_at_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task()])
        before = queue_row().model_dump()
        fake = _gh(monkeypatch)

        result = invoke_dev_queue(
            "prerequisite", ACME_TICKET, "-c", ACME, "--pr", "acme/widgets#9"
        )

        assert result.exit_code == 2
        assert "cw dev-queue hold --until-pr-merged" in _flat(result)
        assert fake.records == []
        assert queue_row().model_dump() == before

    @pytest.mark.parametrize("value", ["0", "abc", "ACM-2"])
    def test_bad_pr_values_are_usage_errors(self, value: str) -> None:
        seed_queue([make_task()])

        result = invoke_dev_queue(
            "prerequisite", ACME_TICKET, "-c", ACME, "--pr", value
        )

        assert result.exit_code == 2
        assert "is not a PR reference" in _flat(result)

    def test_missing_pr_is_a_usage_error(self) -> None:
        result = invoke_dev_queue("prerequisite", ACME_TICKET, "-c", ACME)

        assert result.exit_code == 2
        assert "--pr" in result.output


@pytest.mark.usefixtures("acme")
class TestLoopGuard:
    def test_stale_loop_refuses_before_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task()])
        before = queue_row().model_dump()
        fake = _gh(monkeypatch)
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        stderr = " ".join(result.stderr.split())
        assert "Cannot safely record a merge prerequisite" in stderr
        assert "Restart `cw dev-queue serve`" in stderr
        assert fake.records == []
        assert queue_row().model_dump() == before

    def test_unknown_loop_identity_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task()])
        fake = _gh(monkeypatch)
        write_loop_record(with_identity=False)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 1
        assert "Cannot safely record a merge prerequisite" in result.stderr
        assert fake.records == []
        assert queue_row().merge_prerequisites == []

    @pytest.mark.parametrize("with_loop", [True, False])
    def test_matching_or_absent_loop_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, with_loop: bool
    ) -> None:
        seed_queue([make_task()])
        _gh(monkeypatch)
        if with_loop:
            write_loop_record(commit=COMMIT_A)
            patch_checkout_identity(monkeypatch, commit=COMMIT_A)

        result = invoke_dev_queue(*_DECLARE)

        assert result.exit_code == 0, result.output
        assert "dispatch loop" not in result.stderr
        assert len(queue_row().merge_prerequisites) == 1

    def test_remove_under_a_stale_loop_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(merge_prerequisites=[prerequisite_entry(9)])])
        _gh(monkeypatch)
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)

        result = invoke_dev_queue(*_REMOVE)

        assert result.exit_code == 0, result.output
        assert queue_row().merge_prerequisites == []


def test_help_names_the_contract() -> None:
    result = invoke_dev_queue("prerequisite", "--help")

    assert result.exit_code == 0
    text = _flat(result)
    assert "--pr" in text
    assert "--remove" in text
    assert "same-repo" in text
    assert "operator" in text
