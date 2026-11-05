"""Requeue of an adopted row: the operator re-pin and the intake re-check (#2943).

RFC 0016 R10: ``requeue`` re-pins only an ``adopted_head_drift`` park, only
through the CLI-only ``repin_adopted_head`` keyword, and only when the old
pin is an ancestor of the origin head (real local git; only gh is stubbed).
Auto callers never pass the adoption keywords, so they can never release an
adoption park.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import (
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.adoption import AdoptedPrSnapshot
from cw.dev_queue.requeue import precheck_requeue, requeue_ticket
from cw.exceptions import RequeueStateError
from cw.models import DevQueueStore, QueueItemStatus, Stage
from cw.native_daemon import FakeNativeDaemonClient
from cw.review_convergence import (
    ConvergenceLedger,
    Reconciled,
    reconcile_ledger_for_init,
)
from tests._adopt_pr_helpers import (
    ADOPT_CLIENT,
    ADOPT_PR_URL,
    adopt_client,
    make_adopted_task,
    make_pinned_origin,
)
from tests._clients_yaml import write_clients_yaml
from tests._review_convergence_helpers import (
    FORK_POINT,
    TICKET_ID,
    confirmed_ledger,
    fresh_ledger,
    stopped_ledger,
)
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
        delta_confirm_pending: bool = False,
    ) -> Reconciled:
        return reconcile_ledger_for_init(
            existing,
            fresh=fresh_ledger(),
            ticket_id=TICKET_ID,
            fork_point=FORK_POINT,
            history_rewritten=False,
            head_sha=head,
            max_cycles=8,
            now=_LATER,
            default_merge_tolerated=default_merge_tolerated,
            delta_confirm_pending=delta_confirm_pending,
        )

    def test_converged_ledger_on_repinned_head_reopens(self) -> None:
        result = self._reconcile(confirmed_ledger(sha="old-pin"), "new-pin")
        assert result.ledger.status == "in_progress"

    def test_stopped_ledger_on_repinned_head_releases_new_epoch(self) -> None:
        stopped = stopped_ledger("oscillation", sha="old-pin", stop_identities=("A",))
        result = self._reconcile(stopped, "new-pin")
        assert result.released is not None
        assert result.released.cause == "new_commits"

    def test_tolerated_merge_exception_is_documented_not_overridden(self) -> None:
        converged = confirmed_ledger(sha="old-pin")
        kept = self._reconcile(converged, "new-pin", default_merge_tolerated=True)
        assert kept.ledger.status == "converged"
        pending = self._reconcile(converged, "new-pin", delta_confirm_pending=True)
        assert pending.ledger.status == "converged"
        assert pending.delta_confirm_pending is True


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
