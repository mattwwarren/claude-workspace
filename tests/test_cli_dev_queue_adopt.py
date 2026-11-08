"""CLI tests for ``cw dev-queue add --adopt-pr`` and ``requeue --repin-adopted-head``.

RFC 0016 (#2943). gh is stubbed at ``cw.adopt_pr.fetch_pr_view`` with the
captured ``gh pr view`` fixtures; the dev queue and clients.yaml are real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli.dev_queue._adopt import _commit_is_local
from cw.dev_queue import (
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from tests._adopt_pr_helpers import (
    FIXTURE_CONFLICTING,
    FIXTURE_FAILING_CLOSED,
    FIXTURE_GREEN_UNKNOWN,
    _pin_events,
    load_adopt_fixture,
    make_adopted_task,
    make_repair_feedback,
    stub_pr_view,
)
from tests._clients_yaml import write_clients_yaml
from tests._ticket_view_helpers import invoke_dev_queue, patch_roster
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from click.testing import Result

    from cw.models.review_feedback import ReviewFeedback

_CLIENT = "test-client"
_PR = "2933"
_URL = "https://github.com/mattwwarren/claude-workspace/pull/2933"
_HEAD = "bb649bbc53cd4972d5c8d4c50b11030ae3ab57aa"
_BRANCH = "feat/review-feedback-context"
_REPO = "mattwwarren/claude-workspace"
#: A distinctive report line no CLI output or event may echo.
_REPORT_CANARY = "REPORT-CANARY-2944 untrusted evidence text"


@pytest.fixture(autouse=True)
def _client(tmp_path: Path) -> ClientConfig:
    client = ClientConfig(name=_CLIENT, workspace_path=tmp_path / "ws")
    write_clients_yaml(client, ensure_workspaces=True)
    return client


def _green() -> dict[str, object]:
    # mutated from capture: mergeable
    return load_adopt_fixture(FIXTURE_GREEN_UNKNOWN, mergeable="MERGEABLE")


def _adopt(*extra: str) -> Result:
    return invoke_dev_queue("add", _PR, "--adopt-pr", _PR, "-c", _CLIENT, *extra)


def _feedback(**overrides: object) -> ReviewFeedback:
    """Feedback bound to the ``2933`` captures' repo, PR and head."""
    kwargs: dict[str, object] = {
        "repo": _REPO,
        "pr_number": int(_PR),
        "reviewed_sha": _HEAD,
        "report": f"CW PR REPAIR FEEDBACK\n{_REPORT_CANARY}\n",
    }
    kwargs.update(overrides)
    return make_repair_feedback(**kwargs)


def _stub_synth(
    monkeypatch: pytest.MonkeyPatch, feedback: ReviewFeedback | None = None
) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def _fake(repo: str, pr_number: int, **_kw: object) -> ReviewFeedback:
        calls.append((repo, pr_number))
        return feedback or _feedback()

    monkeypatch.setattr("cw.adopt_pr.synthesize_pr_feedback", _fake)
    return calls


def _feedback_file(tmp_path: Path, feedback: ReviewFeedback | None = None) -> str:
    path = tmp_path / "feedback.json"
    path.write_text((feedback or _feedback()).model_dump_json(), encoding="utf-8")
    return str(path)


_ADOPT_HELP = (
    "Adopt open PR N as the one TICKET, pinned to its head (RFC 0016). A green PR"
    " enters at review; a PR with a merge conflict or failing CI enters at impl"
    " with synthesized repair feedback. To supply the feedback yourself, add"
    " --stage impl and --review-feedback-file."
)
_FEEDBACK_HELP = (
    "JSON file containing one bounded review report. Requires exactly one"
    " ticket and explicit --stage impl. With --adopt-pr it replaces the"
    " synthesized repair feedback and must name the adopted PR's repo, number and"
    " current head SHA."
)


class TestAdd:
    def test_adopts_one_row_and_echoes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert f"Adopted {_URL} as {_PR}" in result.stdout
        assert "(draft; finalize marks it ready)" in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.PENDING
        assert (task.branch, task.adopted_head_sha, task.adopted_pr_url) == (
            _BRANCH,
            _HEAD,
            _URL,
        )
        (event,) = read_events(event_types=[OrchestratorEventType.TICKET_ENQUEUED])
        assert event.payload["adopted_pr_url"] == _URL
        assert event.payload["branch"] == _BRANCH
        assert event.payload["adopted_head_sha"] == _HEAD
        assert event.payload["stage"] == "review"

    def test_explicit_review_stage_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, _green())
        assert _adopt("--stage", "review").exit_code == 0

    def test_multiple_tickets_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub_pr_view(monkeypatch, _green())
        result = invoke_dev_queue("add", "1", "2", "--adopt-pr", _PR, "-c", _CLIENT)
        assert result.exit_code == 2
        assert "exactly one TICKET" in result.output
        assert calls == []

    def test_non_review_stage_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_pr_view(monkeypatch, _green())
        result = _adopt("--stage", "finalize")
        assert result.exit_code == 2
        assert "enters at review or impl" in result.output

    def test_stage_impl_without_feedback_file_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub_pr_view(monkeypatch, _green())
        result = _adopt("--stage", "impl")
        assert result.exit_code == 2
        assert "requires --review-feedback-file" in result.output
        assert calls == []
        assert load_dev_queue().tasks == []

    @pytest.mark.parametrize(
        "stage", [(), ("--stage", "review")], ids=["none", "review"]
    )
    def test_feedback_file_without_stage_impl_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: tuple[str, ...]
    ) -> None:
        calls = stub_pr_view(monkeypatch, _green())
        result = _adopt(*stage, "--review-feedback-file", _feedback_file(tmp_path))
        assert result.exit_code == 2
        assert "--review-feedback-file requires explicit --stage impl." in (
            result.output
        )
        assert calls == []

    def test_stage_review_on_non_green_pr_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        synth = _stub_synth(monkeypatch)
        result = _adopt("--stage", "review")
        assert result.exit_code == 1
        assert "needs a green PR" in result.output
        assert "CONFLICTING" in result.output
        assert load_dev_queue().tasks == []
        assert synth == []

    def test_feedback_file_enters_at_impl_and_echoes_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        synth = _stub_synth(monkeypatch)
        result = _adopt(
            "--stage", "impl", "--review-feedback-file", _feedback_file(tmp_path)
        )
        assert result.exit_code == 0, result.output
        assert (
            f"Adopted {_URL} as {_PR} -> {_CLIENT} at impl, pinned {_HEAD}"
            " (draft; finalize marks it ready); repair feedback from"
            " --review-feedback-file."
        ) in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.stage == Stage.IMPL
        assert task.status == QueueItemStatus.PENDING
        assert task.review_feedback == _feedback()
        assert synth == []

    def test_green_pr_with_feedback_file_enters_at_impl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub_pr_view(monkeypatch, _green())
        result = _adopt(
            "--stage", "impl", "--review-feedback-file", _feedback_file(tmp_path)
        )
        assert result.exit_code == 0, result.output
        assert "repair feedback from --review-feedback-file." in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.stage == Stage.IMPL
        assert task.review_feedback == _feedback()

    def test_feedback_file_mismatch_refuses_without_a_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        path = _feedback_file(tmp_path, _feedback(reviewed_sha="c" * 40))
        result = _adopt("--stage", "impl", "--review-feedback-file", path)
        assert result.exit_code == 1
        assert "--review-feedback-file reviewed_sha" in result.output
        assert load_dev_queue().tasks == []

    def test_fork_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # mutated from capture: isCrossRepository
        stub_pr_view(monkeypatch, load_adopt_fixture(isCrossRepository=True))
        result = _adopt()
        assert result.exit_code == 1
        assert "fork PRs" in result.output
        assert load_dev_queue().tasks == []

    def test_closed_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_FAILING_CLOSED))
        result = invoke_dev_queue("add", "2777", "--adopt-pr", "2777", "-c", _CLIENT)
        assert result.exit_code == 1
        assert "not OPEN" in result.output

    def test_live_row_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    make_adopted_task(
                        ticket_id="OTHER",
                        client=_CLIENT,
                        adopted_pr_url=_URL,
                        branch=_BRANCH,
                    )
                ]
            )
        )
        stub_pr_view(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 1
        assert "already carried by live row 'OTHER'" in result.output

    def test_existing_ticket_row_refused_distinctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id=_PR, client=_CLIENT, status=QueueItemStatus.CANCELLED
                    )
                ]
            )
        )
        stub_pr_view(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 1
        assert "already has a row" in result.output
        assert "already queued" not in result.output

    def test_non_green_pr_enters_at_impl_and_echoes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        synth = _stub_synth(monkeypatch)
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert (
            f"Adopted {_URL} as {_PR} -> {_CLIENT} at impl, pinned {_HEAD}"
            " (draft; finalize marks it ready); repair feedback synthesized"
            " (mergeable: CONFLICTING (resolve the merge conflict))."
        ) in result.stdout
        assert "at impl, pinned" in result.stdout
        assert "repair feedback synthesized (mergeable: CONFLICTING" in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL
        assert task.review_feedback == _feedback()
        assert task.adopted_head_sha == _HEAD
        assert synth == [(_REPO, int(_PR))]
        (enqueued,) = read_events(event_types=[OrchestratorEventType.TICKET_ENQUEUED])
        assert enqueued.payload["stage"] == "impl"
        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_report_is_never_echoed_or_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        _stub_synth(monkeypatch)
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert _REPORT_CANARY not in result.output
        for args in (("show", _PR, "-c", _CLIENT), ("show", _PR, "--json")):
            shown = invoke_dev_queue(*args)
            assert shown.exit_code == 0, shown.output
            assert _REPORT_CANARY not in shown.output
        assert all(_REPORT_CANARY not in str(e.payload) for e in read_events())

    def test_state_unknown_intake_parks_and_emits_attention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.adopt_pr.ADOPT_UNKNOWN_POLL_INTERVAL_SECONDS", 0.0)
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_GREEN_UNKNOWN))
        synth = _stub_synth(monkeypatch)
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert f"parked {ADOPT_PR_STATE_UNKNOWN_DISPOSITION}" in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.REVIEW
        (event,) = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert event.payload["paused_status"] == ADOPT_PR_STATE_UNKNOWN_DISPOSITION
        assert event.payload["adopted_head_sha"] == _HEAD
        assert event.payload["pr_url"] == _URL
        assert synth == []

    def test_unverified_tracker_prints_a_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_pr_view(monkeypatch, _green())
        result = invoke_dev_queue("add", "ABC-1", "--adopt-pr", _PR, "-c", _CLIENT)
        assert result.exit_code == 0, result.output
        assert "existence not verified" in result.stderr

    def test_help_names_the_flags(self) -> None:
        assert "--adopt-pr" in invoke_dev_queue("add", "--help").output
        assert "--repin-adopted-head" in invoke_dev_queue("requeue", "--help").output

    def test_help_describes_both_adopt_routes(self) -> None:
        result = invoke_dev_queue("add", "--help")
        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        assert _ADOPT_HELP in text
        assert _FEEDBACK_HELP in text
        assert "replaces the synthesized repair feedback" in text
        assert "at review, pinned to its head" not in text


def _seed_park(disposition: str) -> None:
    save_dev_queue(
        DevQueueStore(
            tasks=[
                make_adopted_task(
                    ticket_id=_PR,
                    client=_CLIENT,
                    branch=_BRANCH,
                    adopted_head_sha="a" * 40,
                    adopted_pr_url=_URL,
                    pr_url=_URL,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    disposition=disposition,
                )
            ]
        )
    )


def _requeue_result(**overrides: object) -> dict[str, object]:
    """The complete ``requeue_ticket`` result dict for a REVIEW -> REVIEW requeue."""
    result: dict[str, object] = {
        "from_stage": "review",
        "to_stage": "review",
        "regressed": False,
        "regress_attempts": 0,
        "regress_inferred": False,
        "already_pending": False,
        "attempts_reset": False,
        "prior_unproductive_attempts": 0,
        "attempts_reset_was_capped": False,
        "from_cancelled_applied": False,
        "from_failed_applied": False,
        "from_completed_applied": False,
        "adopted_repinned": True,
        "adopted_head_sha_old": "a" * 40,
        "adopted_head_sha": "c" * 40,
    }
    result.update(overrides)
    return result


def _stub_green_recheck(
    monkeypatch: pytest.MonkeyPatch, order: list[str] | None = None
) -> list[str]:
    """Stub the roster, the gh read (green) and the recheck fetches.

    Returns the list the fetch recorder appends ``fetch <ref>`` to.
    """
    patch_roster(monkeypatch, set())
    stub_pr_view(monkeypatch, _green())
    fetched: list[str] = [] if order is None else order

    def _fetch(repo: Path, branch: str) -> None:
        fetched.append(f"fetch {branch}")

    monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
    return fetched


def _requeue_with(
    monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[Result, list[dict[str, object]]]:
    """Run ``requeue`` on the not-green park with ``requeue_ticket`` stubbed."""
    kwargs_seen: list[dict[str, object]] = []

    def _requeue(*_a: object, **kw: object) -> dict[str, object]:
        kwargs_seen.append(dict(kw))
        return _requeue_result(**overrides)

    monkeypatch.setattr("cw.cli.dev_queue.crud.requeue_ticket", _requeue)
    return invoke_dev_queue("requeue", _PR, "-c", _CLIENT), kwargs_seen


class TestRequeue:
    def test_repin_flag_fetches_then_passes_the_keyword(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPTED_HEAD_DRIFT_DISPOSITION)
        patch_roster(monkeypatch, set())
        order: list[str] = []

        def _fetch(repo: Path, branch: str) -> None:
            order.append(f"fetch {branch}")

        def _requeue(*_a: object, **kw: object) -> dict[str, object]:
            order.append("requeue")
            assert kw["repin_adopted_head"] is True
            return _requeue_result()

        monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
        monkeypatch.setattr("cw.cli.dev_queue.crud.requeue_ticket", _requeue)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT, "--repin-adopted-head")
        assert result.exit_code == 0, result.output
        assert order == [f"fetch {_BRANCH}", "requeue"]
        assert (
            f"Re-pinned the adopted head {'a' * 40} -> {'c' * 40}.\n" in result.stdout
        )
        (event,) = read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED])
        assert event.payload["adopted_head_sha"] == "c" * 40
        # The explicit path decided no ancestry here: no audit event, no key.
        assert "adopted_ancestry_result" not in event.payload
        assert _pin_events() == []

    def test_recheck_rewritten_prints_history_rewritten_and_audits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        _stub_green_recheck(monkeypatch)
        result, _ = _requeue_with(monkeypatch, adopted_ancestry_result="not_descendant")
        assert result.exit_code == 0, result.output
        assert (
            f"  Re-pinned the adopted head {'a' * 40} -> {'c' * 40}"
            " (history rewritten: the new head does not descend from the pin)\n"
        ) in result.stdout
        (pin_event,) = _pin_events()
        assert pin_event.payload["source"] == "requeue-recheck"
        assert pin_event.payload["ancestry_result"] == "not_descendant"
        assert pin_event.payload["old_sha"] == "a" * 40
        assert pin_event.payload["new_sha"] == "c" * 40
        assert pin_event.payload["observed_sha"] == "c" * 40
        assert pin_event.payload["client"] == _CLIENT
        assert pin_event.payload["ticket_id"] == _PR
        (requeued,) = read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED])
        assert requeued.payload["adopted_ancestry_result"] == "not_descendant"

    def test_recheck_unknown_prints_undecidable_suffix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        _stub_green_recheck(monkeypatch)
        result, _ = _requeue_with(monkeypatch, adopted_ancestry_result="unknown")
        assert result.exit_code == 0, result.output
        assert (
            f"  Re-pinned the adopted head {'a' * 40} -> {'c' * 40}"
            " (ancestry could not be decided: the new head may not descend"
            " from the pin)\n"
        ) in result.stdout
        assert "history rewritten" not in result.stdout
        (pin_event,) = _pin_events()
        assert pin_event.payload["ancestry_result"] == "unknown"

    @pytest.mark.parametrize("ancestry", ["descendant", "same"])
    def test_recheck_plain_line_for_descendant_and_same(
        self, monkeypatch: pytest.MonkeyPatch, ancestry: str
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        _stub_green_recheck(monkeypatch)
        result, _ = _requeue_with(monkeypatch, adopted_ancestry_result=ancestry)
        assert result.exit_code == 0, result.output
        assert f"  Re-pinned the adopted head {'a' * 40} -> {'c' * 40}.\n" in (
            result.stdout
        )
        (pin_event,) = _pin_events()
        assert pin_event.payload["ancestry_result"] == ancestry

    def test_not_green_recheck_fetches_after_green_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        patch_roster(monkeypatch, set())
        pr_calls = stub_pr_view(monkeypatch, _green())
        order: list[str] = []

        def _fetch(repo: Path, branch: str) -> None:
            assert len(pr_calls) == 1, "fetched before the PR was re-read"
            order.append(f"fetch {branch}")

        def _requeue(*_a: object, **_kw: object) -> dict[str, object]:
            order.append("requeue")
            return _requeue_result(adopted_ancestry_result="unknown")

        monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
        monkeypatch.setattr("cw.cli.dev_queue.crud.requeue_ticket", _requeue)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 0, result.output
        # The non-git workspace has no old pin locally, so it is fetched too.
        assert order == [f"fetch {_BRANCH}", f"fetch {'a' * 40}", "requeue"]

    def test_still_red_recheck_does_no_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        patch_roster(monkeypatch, set())
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        order: list[str] = []

        def _fetch(repo: Path, branch: str) -> None:
            order.append(f"fetch {branch}")

        monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        assert "stays parked" in result.output
        assert order == []

    def test_plain_requeue_of_drift_park_refused_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPTED_HEAD_DRIFT_DISPOSITION)
        patch_roster(monkeypatch, set())
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        assert "--repin-adopted-head" in result.output
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_not_green_park_still_red_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        patch_roster(monkeypatch, set())
        calls = stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        assert "stays parked" in result.output
        assert calls == [_PR]
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_not_green_park_now_green_requeues_and_repins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION)
        _stub_green_recheck(monkeypatch)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 0, result.output
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == _HEAD


class TestRequeueNowNonGreen:
    """A state-unknown or legacy park that is now non-green refuses (D4, c5)."""

    @pytest.mark.parametrize(
        "disposition",
        [ADOPT_PR_STATE_UNKNOWN_DISPOSITION, LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION],
    )
    def test_refusal_names_remove_then_re_add(
        self, monkeypatch: pytest.MonkeyPatch, disposition: str
    ) -> None:
        _seed_park(disposition)
        before = load_dev_queue().tasks[0]
        patch_roster(monkeypatch, set())
        stub_pr_view(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        synth = _stub_synth(monkeypatch)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        text = " ".join(result.output.split())
        assert "stays parked" in text
        assert f"{_URL} is now not green (mergeable: CONFLICTING" in text
        assert "a plain requeue cannot re-route a parked review row to impl" in text
        assert f"`cw dev-queue remove {_PR} -c {_CLIENT}`" in text
        assert f"`cw dev-queue add {_PR} --adopt-pr {_PR} -c {_CLIENT}`" in text
        (after,) = load_dev_queue().tasks
        assert after.status == QueueItemStatus.BLOCKED_ON_USER
        assert after == before
        assert synth == []


class TestCommitIsLocal:
    """``_commit_is_local`` never raises: a git failure reads as "not local"."""

    @pytest.mark.parametrize(
        "error",
        [OSError("no git"), subprocess.TimeoutExpired(cmd="git", timeout=10)],
        ids=["oserror", "timeout"],
    )
    def test_git_failure_is_not_local(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> object:
            raise error

        monkeypatch.setattr("cw.cli.dev_queue._adopt.run_git", _raise)
        assert _commit_is_local(tmp_path, "a" * 40) is False

    def test_non_repo_is_not_local(self, tmp_path: Path) -> None:
        assert _commit_is_local(tmp_path, "a" * 40) is False
