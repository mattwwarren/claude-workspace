"""Tests for ``cw.dispatch.pr_gate`` — the pre-dispatch open-PR gate (#1862).

Mirrors ``tests/test_dispatch_branch_freshness.py``'s naming/shape, but stubs
``pr_exists_for_branch`` rather than building real git repos: the unit under
test is the *gating decision* (which PENDING tasks already have an open PR),
not the ``gh`` invocation, which ``tests/test_gh.py`` already covers.

Every failure mode asserts the fail-open contract — a transient ``gh`` error or
an absent ``gh`` binary must NEVER gate a claim, because a false positive parks
a healthy ticket and costs an operator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.dispatch.pr_gate import (
    _MAX_PROBES_PER_TICK,
    _OPEN_PR_PROBE_TTL_SECONDS,
    resolve_stale_pr_ticket_ids,
)
from cw.dispatch_state import (
    OpenPrProbeCache,
    load_open_pr_probe_cache,
    save_open_pr_probe_entry,
)
from cw.models import ClientConfig, DevQueueStore, QueueItemStatus, Stage
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_CLIENT = "test-client"


@pytest.fixture
def pr_gate_client(tmp_path: Path) -> ClientConfig:
    """A minimal worktree-mode ClientConfig whose git dir exists."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return ClientConfig(
        name=_CLIENT,
        workspace_path=repo,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
    )


class _ProbeStub:
    """Records every ``pr_exists_for_branch`` call and returns a scripted result."""

    def __init__(self, result: tuple[bool | None, bool] = (True, True)) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(
        self, branch: str, *, timeout: int = 0, cwd: object = None
    ) -> tuple[bool | None, bool]:
        self.calls.append(branch)
        return self.result


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch, result: tuple[bool | None, bool] = (True, True)
) -> _ProbeStub:
    stub = _ProbeStub(result)
    monkeypatch.setattr("cw.dispatch.pr_gate.pr_exists_for_branch", stub)
    return stub


class TestResolveStalePrTicketIds:
    """The PLAN/IMPL-stage PENDING scan and its fail-open contract."""

    def test_plan_stage_pending_with_open_pr_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-1862",
                    client=_CLIENT,
                    stage=Stage.PLAN,
                )
            ]
        )

        gated = resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert gated == frozenset({"GEN-1862"})
        assert stub.calls == ["dev/GEN-1862"]

    def test_impl_stage_pending_with_open_pr_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-impl",
                    client=_CLIENT,
                    stage=Stage.IMPL,
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset(
            {"GEN-impl"}
        )

    def test_feature_branch_prefix_is_respected(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The probed branch key comes from ``feature_branch_key`` (#1862)."""
        repo = tmp_path / "prefixed"
        repo.mkdir(parents=True, exist_ok=True)
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=repo,
            default_branch="main",
            feature_branch_prefix="feature",
            worktree_base=tmp_path / "worktrees",
        )
        stub = _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(ticket_id="GEN-42", client=_CLIENT, stage=Stage.PLAN)
            ]
        )

        resolve_stale_pr_ticket_ids(client, store)

        assert stub.calls == ["feature/GEN-42"]

    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.FINALIZE])
    def test_later_stages_are_never_probed(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
    ) -> None:
        """Stage-scoped: REVIEW/FINALIZE legitimately have an open PR (#1862)."""
        stub = _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[_make_ticket_task(ticket_id="GEN-late", client=_CLIENT, stage=stage)]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset()
        assert stub.calls == []

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.RUNNING,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.COMPLETED,
        ],
    )
    def test_non_pending_statuses_are_never_probed(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        status: QueueItemStatus,
    ) -> None:
        stub = _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-busy",
                    client=_CLIENT,
                    stage=Stage.PLAN,
                    status=status,
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset()
        assert stub.calls == []

    def test_other_clients_tasks_are_never_probed(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, (True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-other", client="other-client", stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset()
        assert stub.calls == []

    def test_no_open_pr_is_not_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, (False, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-clean", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset()

    @pytest.mark.parametrize(
        "probe_result",
        [
            pytest.param((None, True), id="transient-gh-error"),
            pytest.param((None, False), id="gh-binary-absent"),
        ],
    )
    def test_fails_open_on_unreliable_probe(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        probe_result: tuple[bool | None, bool],
    ) -> None:
        """Never gate a claim on an unreliable network signal (#1862)."""
        _stub_probe(monkeypatch, probe_result)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-flaky", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == frozenset()

    def test_unreliable_probe_is_not_cached(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transient failure must re-probe next tick, not latch a false negative."""
        stub = _stub_probe(monkeypatch, (None, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-retry", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        resolve_stale_pr_ticket_ids(pr_gate_client, store)
        resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert stub.calls == ["dev/GEN-retry", "dev/GEN-retry"]


class TestOpenPrProbeCacheReuse:
    """TTL cache behaviour: one ``gh`` call per ticket per TTL window."""

    def _store(self) -> DevQueueStore:
        return DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-cache", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

    def test_second_call_within_ttl_reuses_cached_verdict(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, (True, True))
        store = self._store()

        first = resolve_stale_pr_ticket_ids(pr_gate_client, store)
        second = resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert first == second == frozenset({"GEN-cache"})
        assert stub.calls == ["dev/GEN-cache"]

    def test_expired_entry_triggers_a_fresh_probe(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale_at = datetime.now(UTC) - timedelta(
            seconds=_OPEN_PR_PROBE_TTL_SECONDS + 60
        )
        save_open_pr_probe_entry(
            _CLIENT,
            "GEN-cache",
            OpenPrProbeCache(probed_at=stale_at, has_open_pr=True),
        )
        stub = _stub_probe(monkeypatch, (False, True))

        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == frozenset()
        assert stub.calls == ["dev/GEN-cache"]

    def test_cached_false_verdict_is_reused(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_open_pr_probe_entry(
            _CLIENT,
            "GEN-cache",
            OpenPrProbeCache(probed_at=datetime.now(UTC), has_open_pr=False),
        )
        stub = _stub_probe(monkeypatch, (True, True))

        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == frozenset()
        assert stub.calls == []

    def test_ttl_seconds_override_is_honoured(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_open_pr_probe_entry(
            _CLIENT,
            "GEN-cache",
            OpenPrProbeCache(
                probed_at=datetime.now(UTC) - timedelta(seconds=30), has_open_pr=False
            ),
        )
        stub = _stub_probe(monkeypatch, (True, True))

        gated = resolve_stale_pr_ticket_ids(
            pr_gate_client, self._store(), ttl_seconds=5
        )

        assert gated == frozenset({"GEN-cache"})
        assert stub.calls == ["dev/GEN-cache"]

    def test_now_override_drives_expiry(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed_at = datetime.now(UTC)
        save_open_pr_probe_entry(
            _CLIENT,
            "GEN-cache",
            OpenPrProbeCache(probed_at=probed_at, has_open_pr=False),
        )
        stub = _stub_probe(monkeypatch, (True, True))

        gated = resolve_stale_pr_ticket_ids(
            pr_gate_client,
            self._store(),
            now=probed_at + timedelta(seconds=_OPEN_PR_PROBE_TTL_SECONDS + 1),
        )

        assert gated == frozenset({"GEN-cache"})
        assert stub.calls == ["dev/GEN-cache"]


class TestPerTickProbeCap:
    """The #1862 perf follow-up: fresh probes are capped per call.

    A cold cache with more candidates than the cap is probed incrementally
    across ticks rather than fanning out an unbounded number of serial ``gh``
    subprocess calls in one call.
    """

    def _store(self, count: int) -> DevQueueStore:
        return DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=f"GEN-cap-{i}", client=_CLIENT, stage=Stage.PLAN
                )
                for i in range(count)
            ]
        )

    def test_probes_are_capped_at_max_per_tick(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, (True, True))
        store = self._store(_MAX_PROBES_PER_TICK + 5)

        gated = resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert len(stub.calls) == _MAX_PROBES_PER_TICK
        assert len(gated) == _MAX_PROBES_PER_TICK

    def test_capped_out_candidates_are_uncached_and_reprobed_next_call(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A candidate left unprobed by the cap gets no cache entry, so a
        later call (once earlier candidates are cache-warm) reaches it."""
        stub = _stub_probe(monkeypatch, (True, True))
        store = self._store(_MAX_PROBES_PER_TICK + 1)
        overflow_ticket_id = f"GEN-cap-{_MAX_PROBES_PER_TICK}"

        resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert overflow_ticket_id not in load_open_pr_probe_cache()
        assert len(stub.calls) == _MAX_PROBES_PER_TICK

        stub.calls.clear()
        resolve_stale_pr_ticket_ids(pr_gate_client, store)

        # The first _MAX_PROBES_PER_TICK candidates are now cache-warm (no
        # re-probe); only the previously-capped-out candidate is fresh.
        assert stub.calls == [f"dev/{overflow_ticket_id}"]

    def test_cache_hits_do_not_count_against_the_cap(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only fresh probes are capped -- an all-cache-hit call of any size
        makes zero gh calls and gates every stale candidate."""
        now = datetime.now(UTC)
        count = _MAX_PROBES_PER_TICK + 5
        for i in range(count):
            save_open_pr_probe_entry(
                _CLIENT,
                f"GEN-cap-{i}",
                OpenPrProbeCache(probed_at=now, has_open_pr=True),
            )
        stub = _stub_probe(monkeypatch, (True, True))
        store = self._store(count)

        gated = resolve_stale_pr_ticket_ids(pr_gate_client, store, now=now)

        assert stub.calls == []
        assert len(gated) == count
