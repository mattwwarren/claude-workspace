"""Tests for ``cw.dispatch.adopted_head_gate`` (RFC 0016 R5, #2943).

The settle half runs against a real local bare origin (``make_pinned_origin``)
so ancestry is decided by real git; only the gh origin-head read is stubbed,
and the stub reads the bare origin's actual ref. The drift scan's claim-side
park runs through the real ``_claim_next_pending``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw._git import run_git
from cw.dev_queue import ADOPTED_HEAD_DRIFT_DISPOSITION, load_dev_queue, save_dev_queue
from cw.dispatch.adopted_head_gate import (
    ADOPT_DRIFT_MAX_PROBES_PER_TICK,
    ADOPT_SETTLE_MAX_PER_TICK,
    AdoptedHeadDrift,
    SettleResult,
    resolve_adopted_head_drifts,
    settle_adopted_pins,
)
from cw.dispatch.claim.screening import _claim_next_pending
from cw.dispatch.lanes import _resolve_adopted_head_drifts_if_gated
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from tests._adopt_pr_helpers import (
    ADOPT_CLIENT,
    adopt_client,
    make_adopted_task,
    make_pinned_origin,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests._adopt_pr_helpers import PinnedOrigin

_UNKNOWN = "e" * 40


@pytest.fixture
def pinned(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> PinnedOrigin:
    return make_pinned_origin(tmp_path, make_git_repo_with_origin)


@pytest.fixture
def client(pinned: PinnedOrigin) -> ClientConfig:
    return adopt_client(pinned.repo)


@pytest.fixture
def config() -> OrchestratorConfig:
    return OrchestratorConfig(
        tick_interval_seconds=30, per_client_max_parallel={ADOPT_CLIENT: 4}
    )


class _OriginHead:
    """Stub for ``branch_head_sha_on_origin`` reading the real bare origin."""

    def __init__(self, origin: Path, *, readable: bool = True) -> None:
        self.origin = origin
        self.readable = readable
        self.calls: list[str] = []

    def __call__(self, branch: str, **_kw: object) -> tuple[str | None, bool]:
        self.calls.append(branch)
        if not self.readable:
            return None, True
        return git_in(self.origin, "rev-parse", f"refs/heads/{branch}"), True


def _stub_head(
    monkeypatch: pytest.MonkeyPatch, origin: Path, *, readable: bool = True
) -> _OriginHead:
    stub = _OriginHead(origin, readable=readable)
    monkeypatch.setattr("cw.dispatch.adopted_head_gate.branch_head_sha_on_origin", stub)
    return stub


def _seed(pinned: PinnedOrigin, **overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "ticket_id": "T-1",
        "branch": pinned.branch,
        "adopted_head_sha": pinned.pin,
    }
    kwargs.update(overrides)
    task = make_adopted_task(**kwargs)
    save_dev_queue(DevQueueStore(tasks=[task]))
    return task


def _row() -> TicketTask:
    return load_dev_queue().tasks[0]


def _scan(
    client: ClientConfig, settle: SettleResult | None = None
) -> dict[str, AdoptedHeadDrift]:
    return resolve_adopted_head_drifts(
        client, load_dev_queue(), settle if settle is not None else SettleResult()
    )


class TestDriftScan:
    def test_non_adopted_row_makes_no_gh_call(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        save_dev_queue(
            DevQueueStore(tasks=[TicketTask(ticket_id="T-1", client=ADOPT_CLIENT)])
        )
        assert _scan(client) == {}
        assert stub.calls == []

    def test_pin_equals_origin_head_no_hit(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned)
        assert _scan(client) == {}

    def test_kill_check_mismatch_hits_and_claim_parks(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned)
        observed = pinned.push_descendant("foreign.py")

        drifts = _scan(client)

        assert set(drifts) == {"T-1"}
        hit = drifts["T-1"]
        assert (hit.pinned, hit.observed, hit.branch) == (
            pinned.pin,
            observed,
            pinned.branch,
        )
        assert hit.commit_range.count("\n") == 0
        assert not hit.commit_range.startswith("unavailable")
        claimed, _ = _claim_next_pending(
            ADOPT_CLIENT,
            lane="default",
            client=client,
            config=config,
            adopted_head_drifts=drifts,
        )
        assert claimed is None
        task = _row()
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == ADOPTED_HEAD_DRIFT_DISPOSITION
        assert task.blocked_reason == "adopted_head_moved_pre_dispatch"
        assert task.unproductive_attempts == 0
        assert task.adopted_head_sha == pinned.pin

    def test_attention_event_names_both_shas_and_range(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned)
        observed = pinned.push_descendant("foreign.py")
        drifts = _scan(client)
        _claim_next_pending(
            ADOPT_CLIENT,
            lane="default",
            client=client,
            config=config,
            adopted_head_drifts=drifts,
        )
        events = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        payload = events[-1].payload
        assert payload["paused_status"] == ADOPTED_HEAD_DRIFT_DISPOSITION
        assert payload["adopted_head_sha"] == pinned.pin
        assert payload["observed_head_sha"] == observed
        assert payload["commit_range"] == drifts["T-1"].commit_range
        assert payload["branch"] == pinned.branch
        assert "--repin-adopted-head" in str(payload["details"])

    def test_stale_hit_ignored_when_pin_advanced(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        config: OrchestratorConfig,
    ) -> None:
        _seed(pinned, adopted_head_sha="b" * 40)
        stale = {"T-1": AdoptedHeadDrift(pinned.branch, pinned.pin, "c" * 40, "")}
        claimed, _ = _claim_next_pending(
            ADOPT_CLIENT,
            lane="default",
            client=client,
            config=config,
            adopted_head_drifts=stale,
        )
        assert claimed is not None
        assert _row().status == QueueItemStatus.RUNNING

    def test_hit_ignored_when_row_flagged_since_snapshot(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        config: OrchestratorConfig,
    ) -> None:
        _seed(pinned, adopted_repin_due=True)
        hit = {"T-1": AdoptedHeadDrift(pinned.branch, pinned.pin, "c" * 40, "")}
        claimed, _ = _claim_next_pending(
            ADOPT_CLIENT,
            lane="default",
            client=client,
            config=config,
            adopted_head_drifts=hit,
        )
        assert claimed is None
        assert _row().status == QueueItemStatus.PENDING

    def test_gh_unreadable_fails_open(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin, readable=False)
        _seed(pinned)
        pinned.push_descendant("foreign.py")
        assert _scan(client) == {}

    def test_probe_cap_respected(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        rows = [
            make_adopted_task(
                ticket_id=f"T-{i}",
                branch=pinned.branch,
                adopted_head_sha=pinned.pin,
                adopted_pr_url=f"https://github.com/acme/widgets/pull/{i}",
            )
            for i in range(ADOPT_DRIFT_MAX_PROBES_PER_TICK + 3)
        ]
        save_dev_queue(DevQueueStore(tasks=rows))
        _scan(client)
        assert len(stub.calls) == ADOPT_DRIFT_MAX_PROBES_PER_TICK

    def test_non_pending_rows_not_scanned(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, status=QueueItemStatus.BLOCKED_ON_USER)
        assert _scan(client) == {}
        assert stub.calls == []

    def test_scan_probes_the_rows_own_branch(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        _seed(pinned)
        _scan(client)
        assert stub.calls == [pinned.branch]
        assert not pinned.branch.startswith(f"{client.feature_branch_prefix}/")

    def test_skipped_when_no_slots_but_settle_still_runs(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        pinned.push_descendant("foreign.py")
        hits = _resolve_adopted_head_drifts_if_gated(
            client, load_dev_queue(), available_client_slots=0
        )
        assert hits == {}
        assert stub.calls == [pinned.branch]  # the settle's read only
        assert _row().adopted_repin_due is False


class TestSettle:
    def test_descendant_advances_pin_and_same_tick_scan_is_clean(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        snapshot = load_dev_queue()
        observed = pinned.push_descendant("cw_push.py")

        settle = settle_adopted_pins(client, queue_snapshot=snapshot)

        assert settle.advanced == {"T-1": observed}
        assert settle.settled == {"T-1"}
        assert _row().adopted_head_sha == observed
        assert _row().adopted_repin_due is False
        assert resolve_adopted_head_drifts(client, snapshot, settle) == {}

    def test_equal_head_clears_flag_only(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        settle = settle_adopted_pins(client)
        assert settle.advanced == {}
        assert settle.settled == {"T-1"}
        assert _row().adopted_head_sha == pinned.pin
        assert _row().adopted_repin_due is False

    def test_kill_check_diverged_head_keeps_pin_and_scan_parks(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        snapshot = load_dev_queue()
        observed = pinned.push_diverged("rewrite.py")
        caplog.set_level(logging.WARNING, logger="cw.dispatch")

        settle = settle_adopted_pins(client, queue_snapshot=snapshot)

        assert settle.advanced == {}
        assert settle.settled == {"T-1"}
        assert _row().adopted_head_sha == pinned.pin
        assert _row().adopted_repin_due is False
        assert any("adopted_pin_settle_kept" in r.getMessage() for r in caplog.records)
        drifts = resolve_adopted_head_drifts(client, snapshot, settle)
        assert drifts["T-1"].observed == observed

    def test_git_error_keeps_pin_and_scan_parks(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The pin is absent locally, so ancestry answers None (fail closed).
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_head_sha=_UNKNOWN, adopted_repin_due=True)
        snapshot = load_dev_queue()
        settle = settle_adopted_pins(client, queue_snapshot=snapshot)
        assert _row().adopted_head_sha == _UNKNOWN
        assert _row().adopted_repin_due is False
        assert set(resolve_adopted_head_drifts(client, snapshot, settle)) == {"T-1"}

    def test_gh_unreadable_defers_and_next_tick_settles(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin, readable=False)
        _seed(pinned, adopted_repin_due=True)
        observed = pinned.push_descendant("cw_push.py")
        snapshot = load_dev_queue()

        settle = settle_adopted_pins(client, queue_snapshot=snapshot)

        assert settle.deferred == {"T-1"}
        assert _row().adopted_repin_due is True
        assert _row().adopted_head_sha == pinned.pin
        assert resolve_adopted_head_drifts(client, snapshot, settle) == {}
        stub.readable = True
        settle_adopted_pins(client, queue_snapshot=load_dev_queue())
        assert _row().adopted_head_sha == observed
        assert _row().adopted_repin_due is False

    def test_flagged_row_in_stale_snapshot_not_scanned(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The snapshot carries the flag but the settle did not reach the row.
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        pinned.push_descendant("cw_push.py")
        assert _scan(client, SettleResult()) == {}

    def test_foreign_push_after_settle_parks_next_claim(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        first = pinned.push_descendant("cw_push.py")
        settle_adopted_pins(client, queue_snapshot=load_dev_queue())
        assert _row().adopted_head_sha == first
        second = pinned.push_descendant("foreign.py")
        snapshot = load_dev_queue()
        settle = settle_adopted_pins(client, queue_snapshot=snapshot)
        drifts = resolve_adopted_head_drifts(client, snapshot, settle)
        assert drifts["T-1"].observed == second
        assert drifts["T-1"].pinned == first

    def test_settle_cap_respected(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        rows = [
            make_adopted_task(
                ticket_id=f"T-{i}",
                branch=pinned.branch,
                adopted_head_sha=pinned.pin,
                adopted_pr_url=f"https://github.com/acme/widgets/pull/{i}",
                adopted_repin_due=True,
            )
            for i in range(ADOPT_SETTLE_MAX_PER_TICK + 2)
        ]
        save_dev_queue(DevQueueStore(tasks=rows))
        settle = settle_adopted_pins(client)
        assert len(stub.calls) == ADOPT_SETTLE_MAX_PER_TICK
        assert len(settle.settled) == ADOPT_SETTLE_MAX_PER_TICK

    def test_snapshot_without_flag_skips_the_fresh_read(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_head(monkeypatch, pinned.origin)
        _seed(pinned)
        assert (
            settle_adopted_pins(client, queue_snapshot=load_dev_queue())
            == SettleResult()
        )
        assert stub.calls == []

    def test_fetch_makes_a_non_local_descendant_classifiable(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        observed = pinned.push_descendant("foreign.py")
        probe = run_git(
            ["cat-file", "-e", observed], cwd=pinned.repo, capture_output=True
        )
        assert probe.returncode != 0  # not local before the settle's fetch
        settle_adopted_pins(client)
        assert _row().adopted_head_sha == observed
        assert git_in(pinned.repo, "cat-file", "-t", observed) == "commit"

    def test_fetch_failure_fails_closed(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        pinned.push_descendant("foreign.py")
        git_in(pinned.repo, "remote", "set-url", "origin", str(pinned.origin) + "-gone")
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        settle_adopted_pins(client)
        assert _row().adopted_head_sha == pinned.pin
        assert any("adopted_pin_fetch_failed" in r.getMessage() for r in caplog.records)

    def test_fetch_oserror_is_logged(
        self,
        pinned: PinnedOrigin,
        client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _stub_head(monkeypatch, pinned.origin)
        _seed(pinned, adopted_repin_due=True)
        pinned.push_descendant("foreign.py")

        def _boom(*_a: object, **_kw: object) -> object:
            msg = "git missing"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree._tick_fetch._run_git", _boom)
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        settle_adopted_pins(client)
        assert _row().adopted_head_sha == pinned.pin
        assert any("git missing" in r.getMessage() for r in caplog.records)
