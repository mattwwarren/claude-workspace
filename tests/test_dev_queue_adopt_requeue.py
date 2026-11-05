"""Requeue of an adopted row: the operator re-pin and the intake re-check (#2943).

RFC 0016 R10: ``requeue`` re-pins only an ``adopted_head_drift`` park, only
through the CLI-only ``repin_adopted_head`` keyword, and only when the old
pin is an ancestor of the origin head (real local git; only gh is stubbed).
Auto callers never pass the adoption keywords, so they can never release an
adoption park.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.adopt_pr import build_adopted_task, read_adopted_pr
from cw.cli.dev_queue._adopt import adopt_requeue_recheck
from cw.cli.dev_queue._requeue_echo import echo_requeue_outcome
from cw.dev_queue import (
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.adoption import AdoptedPrSnapshot
from cw.dev_queue.requeue import precheck_requeue, requeue_ticket
from cw.events import read_events
from cw.exceptions import RequeueStateError
from cw.models import (
    DevQueueStore,
    MustFixOverride,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.review_convergence import (
    ConvergenceLedger,
    Reconciled,
    reconcile_ledger_for_init,
)
from tests._adopt_pr_helpers import (
    ADOPT_CLIENT,
    ADOPT_PR_URL,
    FIXTURE_CONFLICTING,
    FIXTURE_GREEN_UNKNOWN,
    _pin_events,
    adopt_client,
    load_adopt_fixture,
    make_adopted_task,
    make_pinned_origin,
    stub_pr_view,
)
from tests._clients_yaml import write_clients_yaml
from tests._review_convergence_helpers import (
    FORK_POINT,
    TICKET_ID,
    confirmed_ledger,
    fresh_ledger,
    stopped_ledger,
)
from tests._ticket_view_helpers import patch_roster
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests._adopt_pr_helpers import PinnedOrigin

_TID = "T-1"
_SRC = Path(__file__).resolve().parents[1] / "src" / "cw"
_LATER = "2026-11-04T12:00:00Z"
_ADOPT_KEYWORDS = frozenset({"repin_adopted_head", "adopt_recheck"})


@pytest.fixture
def pinned(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> PinnedOrigin:
    origin = make_pinned_origin(tmp_path, make_git_repo_with_origin)
    write_clients_yaml(adopt_client(origin.repo))
    return origin


@pytest.fixture(autouse=True)
def _fake_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.dev_queue.requeue.get_native_daemon_client", FakeNativeDaemonClient
    )


def _stub_origin_head(monkeypatch: pytest.MonkeyPatch, head: str | None) -> list[str]:
    calls: list[str] = []

    def _fake(branch: str, **_kw: object) -> tuple[str | None, bool]:
        calls.append(branch)
        return head, True

    monkeypatch.setattr("cw.dev_queue.adoption.branch_head_sha_on_origin", _fake)
    return calls


def _seed_park(pinned: PinnedOrigin, disposition: str, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "ticket_id": _TID,
        "branch": pinned.branch,
        "adopted_head_sha": pinned.pin,
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": disposition,
        "review_approved_sha": pinned.pin,
        "review_approver_kind": "operator",
    }
    kwargs.update(overrides)
    save_dev_queue(DevQueueStore(tasks=[make_adopted_task(**kwargs)]))


class TestDriftRepin:
    def test_descendant_head_repins_and_clears_review_approval(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION, adopted_repin_due=True)
        head = pinned.push_descendant("foreign.py")
        git_in(pinned.repo, "fetch", "origin")
        calls = _stub_origin_head(monkeypatch, head)

        result = requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)

        task = load_dev_queue().tasks[0]
        assert calls == [pinned.branch]
        assert task.adopted_head_sha == head
        assert task.adopted_repin_due is False
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW
        assert task.review_approved_sha is None
        assert task.pr_url == ADOPT_PR_URL
        assert result["adopted_repinned"] is True
        assert result["adopted_head_sha_old"] == pinned.pin
        assert result["adopted_head_sha"] == head
        # The explicit path never records ancestry (its check already gated it).
        assert "adopted_ancestry_result" not in result

    def test_kill_check_diverged_head_refuses_and_keeps_pin(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        head = pinned.push_diverged("rewrite.py")
        git_in(pinned.repo, "fetch", "origin")
        _stub_origin_head(monkeypatch, head)

        with pytest.raises(RequeueStateError, match="does not descend"):
            requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)

        task = load_dev_queue().tasks[0]
        assert task.adopted_head_sha == pinned.pin
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == ADOPTED_HEAD_DRIFT_DISPOSITION

    def test_git_error_fails_closed(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The new head was never fetched: ancestry cannot be decided (None).
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        _stub_origin_head(monkeypatch, "e" * 40)
        with pytest.raises(RequeueStateError, match="could not be decided"):
            requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)
        assert load_dev_queue().tasks[0].adopted_head_sha == pinned.pin

    def test_gh_unreadable_refuses(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        _stub_origin_head(monkeypatch, None)
        with pytest.raises(RequeueStateError, match="unreadable"):
            requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_plain_requeue_refuses_with_command_hint(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        calls = _stub_origin_head(monkeypatch, pinned.pin)
        with pytest.raises(RequeueStateError) as exc:
            requeue_ticket(_TID, ADOPT_CLIENT)
        assert (
            f"cw dev-queue requeue {_TID} -c {ADOPT_CLIENT} --repin-adopted-head"
            in str(exc.value)
        )
        assert calls == []

    @pytest.mark.parametrize("stage_override", ["finalize"])
    def test_repin_past_review_refused(
        self,
        pinned: PinnedOrigin,
        monkeypatch: pytest.MonkeyPatch,
        stage_override: str,
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        _stub_origin_head(monkeypatch, pinned.pin)
        with pytest.raises(RequeueStateError, match="review or earlier"):
            requeue_ticket(_TID, ADOPT_CLIENT, stage_override, repin_adopted_head=True)

    def test_repin_of_row_at_finalize_refused(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION, stage=Stage.FINALIZE)
        _stub_origin_head(monkeypatch, pinned.pin)
        with pytest.raises(RequeueStateError, match="review or earlier"):
            requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)

    def test_repin_flag_on_non_drift_row_refused(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, "blocked")
        with pytest.raises(RequeueStateError, match="applies only to an"):
            requeue_ticket(_TID, ADOPT_CLIENT, repin_adopted_head=True)

    def test_result_keys_appear_only_on_a_repin(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, "blocked")
        result = requeue_ticket(_TID, ADOPT_CLIENT)
        assert "adopted_repinned" not in result
        assert load_dev_queue().tasks[0].adopted_head_sha == pinned.pin


class TestIntakeRecheck:
    @pytest.mark.parametrize(
        "disposition",
        [ADOPT_PR_NOT_GREEN_DISPOSITION, ADOPT_PR_STATE_UNKNOWN_DISPOSITION],
    )
    def test_without_recheck_refused(
        self, pinned: PinnedOrigin, disposition: str
    ) -> None:
        _seed_park(pinned, disposition)
        with pytest.raises(RequeueStateError, match="re-reads the PR"):
            requeue_ticket(_TID, ADOPT_CLIENT)

    def test_green_recheck_requeues_and_repins_to_fresh_head(
        self, pinned: PinnedOrigin
    ) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        fresh = "c" * 40
        recheck = AdoptedPrSnapshot(ADOPT_PR_URL, pinned.branch, fresh, False)
        result = requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=recheck)
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == fresh
        assert result["adopted_head_sha"] == fresh

    def test_recheck_for_another_pr_refused(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        recheck = AdoptedPrSnapshot(
            "https://github.com/acme/widgets/pull/9", pinned.branch, "c" * 40, False
        )
        with pytest.raises(RequeueStateError, match="but the row adopted"):
            requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=recheck)

    def test_recheck_on_other_park_refused(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, "blocked")
        recheck = AdoptedPrSnapshot(ADOPT_PR_URL, pinned.branch, "c" * 40, False)
        with pytest.raises(RequeueStateError, match="applies only to an adopt_pr_"):
            requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=recheck)


def _recheck(pinned: PinnedOrigin, head: str) -> AdoptedPrSnapshot:
    return AdoptedPrSnapshot(ADOPT_PR_URL, pinned.branch, head, False)


class TestRecheckAncestry:
    """#2970: the intake re-check re-pin records, logs and audits ancestry."""

    def test_descendant_head(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        head = pinned.push_descendant("x.py")
        git_in(pinned.repo, "fetch", "origin")

        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head)
        )

        assert result["adopted_ancestry_result"] == "descendant"
        assert result["adopted_head_sha_old"] == pinned.pin
        assert result["adopted_head_sha"] == head
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW
        assert task.adopted_head_sha == head

    def test_rewritten_head_is_not_descendant_and_clears_stale_approval(
        self, pinned: PinnedOrigin
    ) -> None:
        _seed_park(
            pinned,
            ADOPT_PR_NOT_GREEN_DISPOSITION,
            review_approved_at=datetime(2026, 11, 4, tzinfo=UTC),
            must_fix_override=MustFixOverride(
                actor="op",
                reason="r",
                reviewed_sha=pinned.pin,
                finding_ids=[("f", "id")],
                recorded_at=datetime(2026, 11, 4, tzinfo=UTC),
            ),
        )
        head = pinned.push_diverged("y.py")
        git_in(pinned.repo, "fetch", "origin")

        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head)
        )

        assert result["adopted_ancestry_result"] == "not_descendant"
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW
        assert task.adopted_head_sha == head
        assert task.review_approved_sha is None
        assert task.review_approved_at is None
        assert task.review_approver_kind is None
        assert task.must_fix_override is None

    def test_undecidable_ancestry_still_releases(self, pinned: PinnedOrigin) -> None:
        # The new head was never fetched; the old pin is local.
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        head = pinned.push_diverged("y.py")

        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head)
        )

        assert result["adopted_ancestry_result"] == "unknown"
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == head

    def test_unchanged_head_is_same(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)

        def _forbidden(*_a: object, **_kw: object) -> object:
            msg = "git was invoked for identical SHAs"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.dev_queue.adoption.is_ancestor", _forbidden)
        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, pinned.pin)
        )
        assert result["adopted_ancestry_result"] == "same"

    @pytest.mark.parametrize(
        "disposition",
        [ADOPT_PR_NOT_GREEN_DISPOSITION, ADOPT_PR_STATE_UNKNOWN_DISPOSITION],
    )
    def test_never_reviewed_row_releases_on_rewritten_head(
        self, pinned: PinnedOrigin, disposition: str
    ) -> None:
        _seed_park(
            pinned,
            disposition,
            review_approved_sha=None,
            review_approver_kind=None,
            ever_spawned=False,
        )
        head = pinned.push_diverged("y.py")
        git_in(pinned.repo, "fetch", "origin")

        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head)
        )

        assert result["adopted_ancestry_result"] == "not_descendant"
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == head

    def test_requeue_ticket_emits_no_event_under_the_lock(
        self, pinned: PinnedOrigin
    ) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        head = pinned.push_diverged("y.py")
        git_in(pinned.repo, "fetch", "origin")

        requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head))

        # Pins emit after the lock, from echo_requeue_outcome.
        assert _pin_events() == []

    @pytest.mark.parametrize(
        ("rewrite", "expected"), [(True, "not_descendant"), (False, "descendant")]
    )
    def test_log_line_carries_ancestry(
        self,
        pinned: PinnedOrigin,
        caplog: pytest.LogCaptureFixture,
        *,
        rewrite: bool,
        expected: str,
    ) -> None:
        caplog.set_level(logging.INFO, logger="cw.dev_queue.adoption")
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        push = pinned.push_diverged if rewrite else pinned.push_descendant
        head = push("y.py")
        git_in(pinned.repo, "fetch", "origin")

        requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head))

        (line,) = [
            r.getMessage()
            for r in caplog.records
            if "adopted_repinned_on_recheck" in r.getMessage()
        ]
        assert f"ancestry={expected}" in line
        assert f"old={pinned.pin}" in line
        assert f"new={head}" in line

    def test_recheck_cannot_target_finalize(self, pinned: PinnedOrigin) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        snap = _recheck(pinned, "c" * 40)
        with pytest.raises(RequeueStateError, match="review or earlier"):
            requeue_ticket(_TID, ADOPT_CLIENT, "finalize", adopt_recheck=snap)
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.adopted_head_sha == pinned.pin

    def test_precheck_refuses_recheck_at_finalize_without_io(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)

        def _no_subprocess(*_a: object, **_kw: object) -> object:
            msg = "precheck_requeue launched a subprocess"
            raise AssertionError(msg)

        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        with pytest.raises(RequeueStateError, match="review or earlier"):
            precheck_requeue(
                _TID,
                ADOPT_CLIENT,
                "finalize",
                native_daemon=FakeNativeDaemonClient(),
                adopt_recheck=_recheck(pinned, "c" * 40),
            )

    def test_end_to_end_rewrite_prints_and_audits(
        self,
        pinned: PinnedOrigin,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        patch_roster(monkeypatch, set())
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        head = pinned.push_diverged("y.py")
        git_in(pinned.repo, "fetch", "origin")

        result = requeue_ticket(
            _TID, ADOPT_CLIENT, adopt_recheck=_recheck(pinned, head)
        )
        echo_requeue_outcome(_TID, ADOPT_CLIENT, result)

        out = capsys.readouterr().out
        assert (
            f"  Re-pinned the adopted head {pinned.pin} -> {head}"
            " (history rewritten: the new head does not descend from the pin)\n"
        ) in out
        (event,) = _pin_events()
        assert event.payload["source"] == "requeue-recheck"
        assert event.payload["old_sha"] == pinned.pin
        assert event.payload["new_sha"] == head
        assert event.payload["observed_sha"] == head
        assert event.payload["ancestry_result"] == "not_descendant"
        (requeued,) = read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED])
        assert requeued.payload["adopted_ancestry_result"] == "not_descendant"


def _fresh_client_without_old_pin(
    pinned: PinnedOrigin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """A client clone that never saw the old pin; gh stubbed green at the new head.

    ``--no-local`` is required: a plain local-path clone copies the whole
    ``objects/`` directory, unreachable objects included, so the old pin would
    still be present.
    """
    git_in(pinned.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    head = pinned.push_diverged("y.py")
    clone = tmp_path / "fresh-client"
    git_in(tmp_path, "clone", "--no-local", str(pinned.origin), str(clone))
    with pytest.raises(subprocess.CalledProcessError):
        git_in(clone, "cat-file", "-e", f"{pinned.pin}^{{commit}}")
    write_clients_yaml(adopt_client(clone))
    _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
    # mutated from capture: mergeable, url, headRefName, headRefOid
    stub_pr_view(
        monkeypatch,
        load_adopt_fixture(
            FIXTURE_GREEN_UNKNOWN,
            mergeable="MERGEABLE",
            url=ADOPT_PR_URL,
            headRefName=pinned.branch,
            headRefOid=head,
        ),
    )
    return clone, head


class TestRecheckFetchesOldPin:
    """The old pin may be absent locally (force-pushed away); fetch it by SHA."""

    def test_old_pin_absent_is_fetched_so_rewrite_is_decided(
        self,
        pinned: PinnedOrigin,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone, _head = _fresh_client_without_old_pin(pinned, tmp_path, monkeypatch)

        snap = adopt_requeue_recheck(
            load_dev_queue().tasks[0], repin_adopted_head=False
        )
        assert snap is not None
        result = requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=snap)

        assert result["adopted_ancestry_result"] == "not_descendant"
        git_in(clone, "cat-file", "-e", f"{pinned.pin}^{{commit}}")

    def test_old_pin_absent_and_fetch_fails_degrades_to_unknown_and_still_releases(
        self,
        pinned: PinnedOrigin,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clone, head = _fresh_client_without_old_pin(pinned, tmp_path, monkeypatch)
        fetched: list[str] = []

        def _failed_fetch(_repo: Path, ref: str) -> None:
            fetched.append(ref)

        monkeypatch.setattr(
            "cw.cli.dev_queue._adopt.fetch_adopted_branch", _failed_fetch
        )
        snap = adopt_requeue_recheck(
            load_dev_queue().tasks[0], repin_adopted_head=False
        )
        assert snap is not None
        result = requeue_ticket(_TID, ADOPT_CLIENT, adopt_recheck=snap)

        assert fetched == [pinned.branch, pinned.pin]
        assert result["adopted_ancestry_result"] == "unknown"
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == head

    def test_local_old_pin_is_not_refetched(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPT_PR_NOT_GREEN_DISPOSITION)
        # mutated from capture: mergeable, url, headRefName, headRefOid
        stub_pr_view(
            monkeypatch,
            load_adopt_fixture(
                FIXTURE_GREEN_UNKNOWN,
                mergeable="MERGEABLE",
                url=ADOPT_PR_URL,
                headRefName=pinned.branch,
                headRefOid="c" * 40,
            ),
        )
        fetched: list[str] = []

        def _fetch(_repo: Path, ref: str) -> None:
            fetched.append(ref)

        monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
        adopt_requeue_recheck(load_dev_queue().tasks[0], repin_adopted_head=False)
        assert fetched == [pinned.branch]


_STAMPERS = {
    "adopt_pr.py",
    "dev_queue/adoption.py",
    "dev_queue/dispositions.py",
    "dev_queue/__init__.py",
    "doctor/wedge/_constants.py",
}
_DISPOSITION_NAMES = frozenset(
    {
        "ADOPT_PR_NOT_GREEN_DISPOSITION",
        "ADOPT_PR_STATE_UNKNOWN_DISPOSITION",
        "adopt_pr_not_green",
        "adopt_pr_state_unknown",
    }
)


def _mentions_disposition(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        names: tuple[object, ...] = ()
        if isinstance(node, ast.Name):
            names = (node.id,)
        elif isinstance(node, ast.Attribute):
            names = (node.attr,)
        elif isinstance(node, ast.alias):
            names = (node.name,)
        elif isinstance(node, ast.Constant):
            names = (node.value,)
        if any(n in _DISPOSITION_NAMES for n in names):
            return True
    return False


def _calls_transition_task_status(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "transition_task_status"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "transition_task_status"
            )
        )
        for node in ast.walk(tree)
    )


class TestAdoptParkInvariant:
    """An ``adopt_pr_*`` park is stamped only at intake, on a never-run row.

    So a re-check re-pin never meets a stale review ledger or approval. The
    answer to "must the requeue reset review state?" is therefore "no", and
    these tests pin the premise. #2944 (repair routing, RFC 0016 D-5) may enter
    a conflicted PR at impl with synthesized feedback; that change must
    revisit ``_repin_to_recheck`` and these tests.
    """

    @pytest.mark.parametrize(
        ("fixture", "disposition"),
        [
            (FIXTURE_CONFLICTING, ADOPT_PR_NOT_GREEN_DISPOSITION),
            (FIXTURE_GREEN_UNKNOWN, ADOPT_PR_STATE_UNKNOWN_DISPOSITION),
        ],
    )
    def test_intake_park_row_has_never_run_a_session(
        self,
        pinned: PinnedOrigin,
        monkeypatch: pytest.MonkeyPatch,
        fixture: str,
        disposition: str,
    ) -> None:
        stub_pr_view(monkeypatch, load_adopt_fixture(fixture))
        client = adopt_client(pinned.repo)
        intake = read_adopted_pr(client, 2933, sleep=lambda _s: None)
        task = build_adopted_task("2933", client, intake, lane="default")

        assert task.disposition == disposition
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.ever_spawned is False
        assert task.session_id is None
        assert task.stage == Stage.REVIEW
        assert task.review_approved_sha is None
        assert task.review_approved_at is None
        assert task.review_approver_kind is None
        assert task.must_fix_override is None

    def test_dispositions_are_stamped_only_by_build_adopted_task(self) -> None:
        mentioning: set[str] = set()
        callers: set[str] = set()
        for path in sorted(_SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = str(path.relative_to(_SRC))
            if _mentions_disposition(tree):
                mentioning.add(rel)
                if _calls_transition_task_status(tree):
                    callers.add(rel)
        msg = (
            "a new module names an adopt_pr_* disposition: revisit"
            " `_repin_to_recheck`'s docstring and #2944"
        )
        assert mentioning == _STAMPERS, msg
        assert callers == {"adopt_pr.py"}, msg


class TestPrecheck:
    def test_precheck_refuses_drift_without_io(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)

        def _no_subprocess(*_a: object, **_kw: object) -> object:
            msg = "precheck_requeue launched a subprocess"
            raise AssertionError(msg)

        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        with pytest.raises(RequeueStateError, match="--repin-adopted-head"):
            precheck_requeue(_TID, ADOPT_CLIENT, native_daemon=FakeNativeDaemonClient())

    def test_precheck_with_flag_does_no_io(
        self, pinned: PinnedOrigin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)

        def _no_subprocess(*_a: object, **_kw: object) -> object:
            msg = "precheck_requeue launched a subprocess"
            raise AssertionError(msg)

        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        precheck_requeue(
            _TID,
            ADOPT_CLIENT,
            native_daemon=FakeNativeDaemonClient(),
            repin_adopted_head=True,
        )


class TestLedgerReset:
    """R10's "resets the ledger" is the engine's release at the next init."""

    def _reconcile(
        self,
        existing: ConvergenceLedger,
        head: str,
        *,
        default_merge_tolerated: bool = False,
        history_rewritten: bool = False,
    ) -> Reconciled:
        return reconcile_ledger_for_init(
            existing,
            fresh=fresh_ledger(),
            ticket_id=TICKET_ID,
            fork_point=FORK_POINT,
            history_rewritten=history_rewritten,
            head_sha=head,
            max_cycles=8,
            now=_LATER,
            default_merge_tolerated=default_merge_tolerated,
        )

    def test_converged_ledger_on_repinned_head_reopens(self) -> None:
        result = self._reconcile(confirmed_ledger(sha="old-pin"), "new-pin")
        assert result.ledger.status == "in_progress"

    def test_stopped_ledger_on_repinned_head_releases_new_epoch(self) -> None:
        stopped = stopped_ledger("oscillation", sha="old-pin", stop_identities=("A",))
        result = self._reconcile(stopped, "new-pin")
        assert result.released is not None
        assert result.released.cause == "new_commits"

    def test_rewritten_history_returns_a_fresh_ledger_with_a_reset_reason(
        self,
    ) -> None:
        result = self._reconcile(
            confirmed_ledger(sha="old-pin"), "new-pin", history_rewritten=True
        )
        assert result.reset_reason is not None
        assert result.ledger.status == "in_progress"

    def test_tolerated_merge_exception_is_documented_not_overridden(self) -> None:
        converged = confirmed_ledger(sha="old-pin")
        kept = self._reconcile(converged, "new-pin", default_merge_tolerated=True)
        assert kept.ledger.status == "converged"
        assert kept.default_merge_tolerated is True


class TestOtherCallers:
    """R10: the auto callers' shapes never release an adoption park."""

    @pytest.mark.parametrize(
        "disposition",
        [
            ADOPTED_HEAD_DRIFT_DISPOSITION,
            ADOPT_PR_NOT_GREEN_DISPOSITION,
            ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
        ],
    )
    def test_bare_drain_shape_refuses(
        self, pinned: PinnedOrigin, disposition: str
    ) -> None:
        _seed_park(pinned, disposition)
        with pytest.raises(RequeueStateError):
            requeue_ticket(_TID, ADOPT_CLIENT)

    def test_drain_never_selects_adoption_parks(self) -> None:
        from cw.dev_queue.gates import DRAIN_DISPOSITIONS

        for disposition in (
            ADOPTED_HEAD_DRIFT_DISPOSITION,
            ADOPT_PR_NOT_GREEN_DISPOSITION,
            ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
        ):
            assert disposition not in DRAIN_DISPOSITIONS

    def test_spawn_close_requeue_reraises_for_parked_adopted_row(
        self, pinned: PinnedOrigin
    ) -> None:
        from cw.cli.spawn import _spawn_close_requeue_impl

        _seed_park(pinned, ADOPTED_HEAD_DRIFT_DISPOSITION)
        with pytest.raises(RequeueStateError):
            _spawn_close_requeue_impl(
                session_id="dead1234", ticket_id=_TID, client=ADOPT_CLIENT
            )
        assert load_dev_queue().tasks[0].adopted_head_sha == pinned.pin

    def test_recipe_shape_on_completed_row_keeps_pin(
        self, pinned: PinnedOrigin
    ) -> None:
        _seed_park(pinned, "shipped", status=QueueItemStatus.COMPLETED)
        result = requeue_ticket(
            _TID,
            ADOPT_CLIENT,
            from_completed=True,
            from_cancelled=True,
            from_failed=True,
        )
        task = load_dev_queue().tasks[0]
        assert result["from_completed_applied"] is True
        assert task.adopted_head_sha == pinned.pin
        assert task.pr_url == ADOPT_PR_URL

    def test_adoption_keywords_passed_only_by_the_cli(self) -> None:
        """Source scan: only cli/dev_queue/crud.py passes the adoption keywords."""
        offenders = {
            str(path.relative_to(_SRC))
            for path in sorted(_SRC.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and any(kw.arg in _ADOPT_KEYWORDS for kw in node.keywords)
        }
        # requeue.py forwards its own parameters under the short names
        # ``repin`` / ``recheck``, so the CLI is the only call-site.
        assert offenders == {"cli/dev_queue/crud.py"}
