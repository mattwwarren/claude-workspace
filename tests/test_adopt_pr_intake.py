"""Fixture-driven tests for the RFC 0016 adoption intake (``cw.adopt_pr``, #2943).

Every payload is a captured ``gh pr view`` response (``tests/fixtures/
gh_pr_view_adopt_*.json``) loaded through ``load_adopt_fixture``. A variant
with no real capture mutates exactly one top-level field and says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.adopt_pr import (
    ADOPT_UNKNOWN_MAX_POLLS,
    ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS,
    AdoptIntake,
    build_adopted_task,
    read_adopted_pr,
    verify_ticket_exists,
)
from cw.dev_queue import (
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
)
from cw.exceptions import AdoptionRefusedError
from cw.models import QueueItemStatus, Stage
from tests._adopt_pr_helpers import (
    FIXTURE_CONFLICTING,
    FIXTURE_FAILING_CLOSED,
    FIXTURE_GREEN_UNKNOWN,
    adopt_client,
    load_adopt_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_PR = 2933
_CAPTURED_HEAD = "bb649bbc53cd4972d5c8d4c50b11030ae3ab57aa"
_CAPTURED_BRANCH = "feat/review-feedback-context"
_CAPTURED_URL = "https://github.com/mattwwarren/claude-workspace/pull/2933"


@pytest.fixture
def client(tmp_path: Path) -> ClientConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return adopt_client(repo)


class _Reads:
    """A scripted ``fetch_pr_view``: returns each payload in turn, then the last."""

    def __init__(self, *payloads: dict[str, object] | None) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, pr_ref: str, **kw: object) -> dict[str, object] | None:
        self.calls.append((pr_ref, kw))
        index = min(len(self.calls), len(self.payloads)) - 1
        return self.payloads[index]


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _stub(
    monkeypatch: pytest.MonkeyPatch, *payloads: dict[str, object] | None
) -> _Reads:
    reads = _Reads(*payloads)
    monkeypatch.setattr("cw.adopt_pr.fetch_pr_view", reads)
    return reads


def _green() -> dict[str, object]:
    # mutated from capture: mergeable (the capture's UNKNOWN made MERGEABLE)
    return load_adopt_fixture(FIXTURE_GREEN_UNKNOWN, mergeable="MERGEABLE")


def _read(client: ClientConfig, sleep: _Sleeps | None = None) -> AdoptIntake:
    return read_adopted_pr(client, _PR, sleep=sleep or _Sleeps())


class TestRefusalPrecedence:
    def test_gh_unreadable_refuses_first(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, None)
        with pytest.raises(AdoptionRefusedError, match="gh could not read"):
            _read(client)

    def test_fork_refused_before_state(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: isCrossRepository (on the CLOSED #2777 capture,
        # so the fork refusal must win over the state refusal)
        _stub(
            monkeypatch,
            load_adopt_fixture(FIXTURE_FAILING_CLOSED, isCrossRepository=True),
        )
        with pytest.raises(AdoptionRefusedError, match="fork PRs"):
            _read(client)

    def test_closed_pr_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, load_adopt_fixture(FIXTURE_FAILING_CLOSED))
        with pytest.raises(AdoptionRefusedError, match="it is CLOSED, not OPEN"):
            _read(client)

    def test_merged_pr_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: state
        _stub(monkeypatch, load_adopt_fixture(FIXTURE_GREEN_UNKNOWN, state="MERGED"))
        with pytest.raises(AdoptionRefusedError, match="it is MERGED"):
            _read(client)

    def test_branch_charset_refusal_names_the_branch(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: headRefName ('#' is a legal git ref, not ours)
        _stub(monkeypatch, load_adopt_fixture(headRefName="feat/issue#1"))
        with pytest.raises(AdoptionRefusedError, match=r"'feat/issue#1' fails the row"):
            _read(client)

    def test_malformed_head_oid_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: headRefOid
        _stub(monkeypatch, load_adopt_fixture(headRefOid="bb649bb"))
        with pytest.raises(AdoptionRefusedError, match="not a full SHA"):
            _read(client)

    def test_unparseable_url_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: url
        _stub(monkeypatch, load_adopt_fixture(url="not-a-pr-url"))
        with pytest.raises(AdoptionRefusedError, match="unparseable PR url"):
            _read(client)

    def test_slug_mismatch_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, _green())
        monkeypatch.setattr(
            "cw.adopt_pr.repo_slug_mismatch", lambda *_a: "acme/other-repo"
        )
        with pytest.raises(AdoptionRefusedError, match="acme/other-repo"):
            _read(client)

    def test_reads_with_the_adopt_field_list_in_the_client_repo(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.gh import PR_ADOPT_FIELDS

        reads = _stub(monkeypatch, _green())
        _read(client)
        pr_ref, kw = reads.calls[0]
        assert pr_ref == str(_PR)
        assert kw == {"fields": PR_ADOPT_FIELDS, "cwd": client.workspace_path}


class TestUnknownRepoll:
    def test_unknown_for_every_poll_parks_state_unknown(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = _stub(monkeypatch, load_adopt_fixture(FIXTURE_GREEN_UNKNOWN))
        sleeps = _Sleeps()
        intake = _read(client, sleeps)
        assert intake.disposition == ADOPT_PR_STATE_UNKNOWN_DISPOSITION
        assert ADOPT_UNKNOWN_MAX_POLLS > 1
        assert len(reads.calls) == ADOPT_UNKNOWN_MAX_POLLS
        assert intake.polls == ADOPT_UNKNOWN_MAX_POLLS
        assert sleeps.calls == [ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS] * (
            ADOPT_UNKNOWN_MAX_POLLS - 1
        )
        assert intake.blocked_reason == "adopt_pr_state_unknown_at_intake"

    def test_unknown_then_mergeable_proceeds(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = _stub(monkeypatch, load_adopt_fixture(FIXTURE_GREEN_UNKNOWN), _green())
        sleeps = _Sleeps()
        intake = _read(client, sleeps)
        assert intake.disposition is None
        assert len(reads.calls) == 2
        assert sleeps.calls == [ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS]

    def test_unknown_beats_red_ci(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing = load_adopt_fixture(FIXTURE_FAILING_CLOSED)["statusCheckRollup"]
        # mutated from capture: statusCheckRollup (the #2777 capture's checks)
        _stub(monkeypatch, load_adopt_fixture(statusCheckRollup=failing))
        assert _read(client).disposition == ADOPT_PR_STATE_UNKNOWN_DISPOSITION

    def test_refusal_on_a_repoll_still_refuses(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, load_adopt_fixture(FIXTURE_GREEN_UNKNOWN), None)
        with pytest.raises(AdoptionRefusedError):
            _read(client)


class TestNotGreen:
    def test_conflicting_capture_names_one_condition(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        intake = _read(client)
        assert intake.disposition == ADOPT_PR_NOT_GREEN_DISPOSITION
        assert intake.reasons == (
            "mergeable: CONFLICTING (resolve the merge conflict)",
        )
        assert intake.blocked_reason == "adopt_pr_not_green_at_intake"

    def test_failing_ci_only_names_one_condition(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mutated from capture: state (the CLOSED #2777 capture made OPEN)
        _stub(monkeypatch, load_adopt_fixture(FIXTURE_FAILING_CLOSED, state="OPEN"))
        intake = _read(client)
        assert intake.disposition == ADOPT_PR_NOT_GREEN_DISPOSITION
        assert intake.reasons == ("failing CI: test (macos-latest, 3.13)",)

    def test_conflicting_and_failing_names_both(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing = load_adopt_fixture(FIXTURE_FAILING_CLOSED)["statusCheckRollup"]
        # mutated from capture: statusCheckRollup (the #2777 capture's checks)
        _stub(
            monkeypatch,
            load_adopt_fixture(FIXTURE_CONFLICTING, statusCheckRollup=failing),
        )
        intake = _read(client)
        assert len(intake.reasons) == 2
        assert "CONFLICTING" in intake.details
        assert "failing CI" in intake.details

    def test_pending_ci_and_mergeable_is_green(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rollup = load_adopt_fixture()["statusCheckRollup"]
        assert isinstance(rollup, list)
        pending = [dict(rollup[0], status="IN_PROGRESS", conclusion=None), *rollup[1:]]
        # mutated from capture: statusCheckRollup (one CheckRun made in-progress)
        payload = _green()
        payload["statusCheckRollup"] = pending
        _stub(monkeypatch, payload)
        assert _read(client).disposition is None

    def test_draft_is_green_and_carries_is_draft(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, _green())
        intake = _read(client)
        assert intake.disposition is None
        assert intake.reasons == ()
        assert intake.snapshot.is_draft is True
        assert intake.snapshot.head_sha == _CAPTURED_HEAD
        assert intake.snapshot.branch == _CAPTURED_BRANCH
        assert intake.snapshot.pr_url == _CAPTURED_URL


class TestTicketExistence:
    def test_pr_as_its_own_ticket_needs_no_read(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.adopt_pr.fetch_issue_body", lambda *_a, **_kw: pytest.fail("read")
        )
        assert verify_ticket_exists(client, str(_PR), _PR) is None

    def test_github_issues_unreadable_refuses(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.adopt_pr.resolve_tracker", lambda _root: "github-issues"
        )
        monkeypatch.setattr("cw.adopt_pr.fetch_issue_body", lambda *_a, **_kw: None)
        with pytest.raises(AdoptionRefusedError, match="could not read issue 42"):
            verify_ticket_exists(client, "42", _PR)

    def test_github_issues_readable_passes(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.adopt_pr.resolve_tracker", lambda _root: "github-issues"
        )
        monkeypatch.setattr("cw.adopt_pr.fetch_issue_body", lambda *_a, **_kw: "body")
        assert verify_ticket_exists(client, "42", _PR) is None

    def test_other_tracker_is_skipped_with_a_note(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.adopt_pr.resolve_tracker", lambda _root: "linear")
        note = verify_ticket_exists(client, "ABC-1", _PR)
        assert note is not None
        assert "not verified" in note


class TestBuildAdoptedTask:
    def _intake(
        self,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict[str, object],
    ) -> AdoptIntake:
        _stub(monkeypatch, payload)
        return _read(client)

    def test_green_row_is_pending_review_entry(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = build_adopted_task(
            "42", client, self._intake(client, monkeypatch, _green()), lane="default"
        )
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW
        assert task.branch == _CAPTURED_BRANCH
        assert task.adopted_head_sha == _CAPTURED_HEAD
        assert task.adopted_pr_url == _CAPTURED_URL
        assert task.pr_url == _CAPTURED_URL
        assert task.adopted_repin_due is False

    def test_park_row_is_blocked_never_completed(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intake = self._intake(
            client, monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING)
        )
        task = build_adopted_task("42", client, intake, lane="default")
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == ADOPT_PR_NOT_GREEN_DISPOSITION
        assert task.blocked_reason == "adopt_pr_not_green_at_intake"
        assert task.pr_url == _CAPTURED_URL
        assert task.unproductive_attempts == 0

    @pytest.mark.parametrize("ticket_id", ["../2933", "-x", "a b"])
    def test_unsafe_ticket_id_refused(
        self, client: ClientConfig, monkeypatch: pytest.MonkeyPatch, ticket_id: str
    ) -> None:
        intake = self._intake(client, monkeypatch, _green())
        with pytest.raises(AdoptionRefusedError, match="Invalid ticket_id"):
            build_adopted_task(ticket_id, client, intake, lane="default")
