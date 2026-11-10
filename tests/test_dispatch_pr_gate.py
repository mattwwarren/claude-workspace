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

from cw.config import get_client
from cw.dev_queue import (
    STALE_DISPATCH_GATE_DISPOSITION,
    _stage_regress,
    load_dev_queue,
    requeue_ticket,
    transition_task_status,
)
from cw.dev_queue.dispositions import _PRE_DISPATCH_STALE_PR_REASON
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
from cw.gh import OpenPrLookup
from cw.models import ClientConfig, DevQueueStore, QueueItemStatus, Stage
from cw.queue_rows import _apply_spawn_success_fields
from tests._adopt_pr_helpers import make_repair_feedback
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    make_blocked,
    queue_row,
    seed_queue,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_CLIENT = "test-client"
_PR_URL = "https://github.com/acme/widgets/pull/42"
_OPEN = OpenPrLookup(open_pr=True, gh_available=True, url=_PR_URL)
_NO_PR = OpenPrLookup(open_pr=False, gh_available=True)


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

    def __init__(self, result: OpenPrLookup = _OPEN) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(
        self, branch: str, *, timeout: int = 0, cwd: object = None
    ) -> OpenPrLookup:
        self.calls.append(branch)
        return self.result


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch, result: OpenPrLookup = _OPEN
) -> _ProbeStub:
    stub = _ProbeStub(result)
    monkeypatch.setattr("cw.dispatch.pr_gate.pr_exists_for_branch", stub)
    return stub


def _cache_ticket_id(ticket_id: str, branch: str | None = None) -> str:
    return f"{ticket_id}::{branch or f'dev/{ticket_id}'}"


def _feedback_store(stage: Stage, pr_number: int = 42) -> DevQueueStore:
    """One PENDING row whose review feedback names PR *pr_number* (no adoption)."""
    return DevQueueStore(
        tasks=[
            _make_ticket_task(
                ticket_id="GEN-fb",
                client=_CLIENT,
                stage=stage,
                review_feedback=make_repair_feedback(pr_number=pr_number),
            )
        ]
    )


def _regressed_store(
    stage: Stage, *, regressed_into: Stage | None, high_water: Stage | None
) -> DevQueueStore:
    """One PENDING row carrying the regress marker and stage high water (#3248).

    Module-local beside ``_feedback_store``, not in ``conftest.py``: it is a
    pr_gate-specific row shape paired with this module's ``_stub_probe`` seam.
    """
    return DevQueueStore(
        tasks=[
            _make_ticket_task(
                ticket_id="GEN-reg",
                client=_CLIENT,
                stage=stage,
                regressed_into_stage=regressed_into,
                stage_high_water=high_water,
            )
        ]
    )


_PARKED = {"GEN-reg": _PR_URL}
_REGRESS_CASES = [
    pytest.param(Stage.IMPL, Stage.IMPL, Stage.REVIEW, {}, id="released-review"),
    pytest.param(Stage.IMPL, Stage.IMPL, Stage.FINALIZE, {}, id="released-finalize"),
    pytest.param(Stage.IMPL, None, Stage.REVIEW, _PARKED, id="fresh-review-parks"),
    pytest.param(Stage.IMPL, None, Stage.FINALIZE, _PARKED, id="fresh-finalize-parks"),
    pytest.param(Stage.IMPL, None, None, _PARKED, id="fresh-no-high-water-parks"),
    pytest.param(Stage.IMPL, Stage.IMPL, Stage.PLAN, _PARKED, id="shallow-plan-parks"),
    pytest.param(Stage.IMPL, Stage.IMPL, Stage.IMPL, _PARKED, id="shallow-impl-parks"),
    pytest.param(Stage.IMPL, Stage.IMPL, None, _PARKED, id="marker-no-high-water"),
    pytest.param(Stage.IMPL, Stage.PLAN, Stage.FINALIZE, _PARKED, id="other-marker"),
    pytest.param(Stage.PLAN, Stage.PLAN, Stage.REVIEW, _PARKED, id="plan-row-parks"),
    pytest.param(Stage.PLAN, Stage.IMPL, Stage.REVIEW, _PARKED, id="plan-marker-parks"),
    pytest.param(
        Stage.PLAN, Stage.IMPL, Stage.FINALIZE, _PARKED, id="plan-finalize-parks"
    ),
]


class TestReviewFeedbackRelease:
    """#3018: the feedback release applies to an IMPL-stage row only."""

    def test_impl_row_with_feedback_for_the_open_pr_is_released(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        assert (
            resolve_stale_pr_ticket_ids(pr_gate_client, _feedback_store(Stage.IMPL))
            == {}
        )

    def test_plan_row_with_the_same_feedback_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(
            pr_gate_client, _feedback_store(Stage.PLAN)
        ) == {"GEN-fb": _PR_URL}

    def test_impl_row_with_feedback_for_another_pr_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(
            pr_gate_client, _feedback_store(Stage.IMPL, pr_number=43)
        ) == {"GEN-fb": _PR_URL}

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [(Stage.IMPL, {}), (Stage.PLAN, {"GEN-fb": _PR_URL})],
    )
    def test_cached_verdict_gets_the_same_stage_check(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
        expected: dict[str, str],
    ) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-fb"),
            OpenPrProbeCache(probed_at=now, has_open_pr=True, pr_url=_PR_URL),
        )
        stub = _stub_probe(monkeypatch, _OPEN)

        gated = resolve_stale_pr_ticket_ids(
            pr_gate_client, _feedback_store(stage), now=now
        )

        assert gated == expected
        assert stub.calls == []


class TestResolveStalePrTicketIds:
    """The PLAN/IMPL-stage PENDING scan and its fail-open contract."""

    def test_plan_stage_pending_with_open_pr_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
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

        assert gated == {"GEN-1862": _PR_URL}
        assert stub.calls == ["dev/GEN-1862"]

    def test_impl_stage_pending_with_open_pr_is_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-impl",
                    client=_CLIENT,
                    stage=Stage.IMPL,
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {
            "GEN-impl": _PR_URL
        }

    def test_feature_branch_prefix_is_respected(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The probed branch comes from ``resolve_feature_branch`` (#1862, #2948)."""
        repo = tmp_path / "prefixed"
        repo.mkdir(parents=True, exist_ok=True)
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=repo,
            default_branch="main",
            feature_branch_prefix="feature",
            worktree_base=tmp_path / "worktrees",
        )
        stub = _stub_probe(monkeypatch, _OPEN)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(ticket_id="GEN-42", client=_CLIENT, stage=Stage.PLAN)
            ]
        )

        resolve_stale_pr_ticket_ids(client, store)

        assert stub.calls == ["feature/GEN-42"]

    def test_explicit_row_branch_does_not_reuse_default_branch_cache(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
        default_row = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-branch-cache", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )
        adopted_row = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-branch-cache",
                    client=_CLIENT,
                    stage=Stage.PLAN,
                    branch="feature/adopted-cache",
                )
            ]
        )

        resolve_stale_pr_ticket_ids(pr_gate_client, default_row)
        resolve_stale_pr_ticket_ids(pr_gate_client, adopted_row)

        assert stub.calls == ["dev/GEN-branch-cache", "feature/adopted-cache"]

    def test_duplicate_ticket_rows_cache_each_resolved_branch(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-duplicate", client=_CLIENT, stage=Stage.PLAN
                ),
                _make_ticket_task(
                    ticket_id="GEN-duplicate",
                    client=_CLIENT,
                    stage=Stage.PLAN,
                    branch="feature/adopted-duplicate",
                ),
            ]
        )

        resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert stub.calls == ["dev/GEN-duplicate", "feature/adopted-duplicate"]

    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.FINALIZE])
    def test_later_stages_are_never_probed(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
    ) -> None:
        """Stage-scoped: REVIEW/FINALIZE legitimately have an open PR (#1862)."""
        stub = _stub_probe(monkeypatch, _OPEN)
        store = DevQueueStore(
            tasks=[_make_ticket_task(ticket_id="GEN-late", client=_CLIENT, stage=stage)]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
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
        stub = _stub_probe(monkeypatch, _OPEN)
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

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
        assert stub.calls == []

    def test_other_clients_tasks_are_never_probed(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-other", client="other-client", stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
        assert stub.calls == []

    def test_no_open_pr_is_not_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _NO_PR)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-clean", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}

    @pytest.mark.parametrize(
        "probe_result",
        [
            pytest.param(OpenPrLookup(None, True), id="transient-gh-error"),
            pytest.param(OpenPrLookup(None, False), id="gh-binary-absent"),
        ],
    )
    def test_fails_open_on_unreliable_probe(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        probe_result: OpenPrLookup,
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

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}

    def test_unreliable_probe_is_not_cached(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transient failure must re-probe next tick, not latch a false negative."""
        stub = _stub_probe(monkeypatch, OpenPrLookup(None, True))
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

    @pytest.mark.parametrize(
        "probe_result",
        [
            pytest.param(OpenPrLookup(None, True), id="transient-gh-error"),
            pytest.param(OpenPrLookup(None, False), id="gh-binary-absent"),
        ],
    )
    def test_fail_open_writes_no_cache_entry(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        probe_result: OpenPrLookup,
    ) -> None:
        """An unreliable lookup records neither a verdict nor a URL (#2740)."""
        _stub_probe(monkeypatch, probe_result)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-flaky", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
        assert load_open_pr_probe_cache() == {}

    def test_reliable_no_open_pr_is_not_gated_and_cached_false(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reliable ``(False, None)`` probe is not mistaken for an open PR.

        Pins the truthy-tuple hazard: the resolver must unpack the probe
        result before testing it, not ``if probed:`` on the tuple (#2740).
        """
        _stub_probe(monkeypatch, _NO_PR)
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-clean", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
        cached = load_open_pr_probe_cache()[
            f"{_CLIENT}/{_cache_ticket_id('GEN-clean')}"
        ]
        assert cached.has_open_pr is False
        assert cached.pr_url is None

    def test_open_pr_without_url_is_still_gated(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bool is the gate contract; a missing URL only leaves it None."""
        _stub_probe(monkeypatch, OpenPrLookup(True, True))
        store = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-nourl", client=_CLIENT, stage=Stage.PLAN
                )
            ]
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {"GEN-nourl": None}


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
        stub = _stub_probe(monkeypatch, _OPEN)
        store = self._store()

        first = resolve_stale_pr_ticket_ids(pr_gate_client, store)
        second = resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert first == second == {"GEN-cache": _PR_URL}
        assert stub.calls == ["dev/GEN-cache"]

    def test_fresh_probe_persists_the_pr_url(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cache carries the URL so a cache-hit tick still records it."""
        _stub_probe(monkeypatch, _OPEN)

        resolve_stale_pr_ticket_ids(pr_gate_client, self._store())

        cached = load_open_pr_probe_cache()[
            f"{_CLIENT}/{_cache_ticket_id('GEN-cache')}"
        ]
        assert cached.has_open_pr is True
        assert cached.pr_url == _PR_URL

    def test_legacy_cache_entry_without_url_gates_with_none(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-#2740 entry (no ``pr_url``) still gates, recording no URL."""
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(probed_at=datetime.now(UTC), has_open_pr=True),
        )
        stub = _stub_probe(monkeypatch, _OPEN)

        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {
            "GEN-cache": None
        }
        assert stub.calls == []

    def test_expired_entry_reprobe_returns_the_new_url(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After TTL expiry a different open PR replaces the cached one."""
        probed_at = datetime.now(UTC)
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(
                probed_at=probed_at,
                has_open_pr=True,
                pr_url="https://github.com/acme/widgets/pull/1",
            ),
        )
        new_url = "https://github.com/acme/widgets/pull/2"
        stub = _stub_probe(monkeypatch, OpenPrLookup(True, True, new_url))
        later = probed_at + timedelta(seconds=_OPEN_PR_PROBE_TTL_SECONDS + 1)

        gated = resolve_stale_pr_ticket_ids(pr_gate_client, self._store(), now=later)

        assert gated == {"GEN-cache": new_url}
        assert stub.calls == ["dev/GEN-cache"]
        assert (
            load_open_pr_probe_cache()[
                f"{_CLIENT}/{_cache_ticket_id('GEN-cache')}"
            ].pr_url
            == new_url
        )

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
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(probed_at=stale_at, has_open_pr=True),
        )
        stub = _stub_probe(monkeypatch, _NO_PR)

        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {}
        assert stub.calls == ["dev/GEN-cache"]

    def test_cached_false_verdict_is_reused(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(probed_at=datetime.now(UTC), has_open_pr=False),
        )
        stub = _stub_probe(monkeypatch, _OPEN)

        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {}
        assert stub.calls == []

    def test_ttl_seconds_override_is_honoured(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(
                probed_at=datetime.now(UTC) - timedelta(seconds=30), has_open_pr=False
            ),
        )
        stub = _stub_probe(monkeypatch, _OPEN)

        gated = resolve_stale_pr_ticket_ids(
            pr_gate_client, self._store(), ttl_seconds=5
        )

        assert gated == {"GEN-cache": _PR_URL}
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
            _cache_ticket_id("GEN-cache"),
            OpenPrProbeCache(probed_at=probed_at, has_open_pr=False),
        )
        stub = _stub_probe(monkeypatch, _OPEN)

        gated = resolve_stale_pr_ticket_ids(
            pr_gate_client,
            self._store(),
            now=probed_at + timedelta(seconds=_OPEN_PR_PROBE_TTL_SECONDS + 1),
        )

        assert gated == {"GEN-cache": _PR_URL}
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
        stub = _stub_probe(monkeypatch, _OPEN)
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
        stub = _stub_probe(monkeypatch, _OPEN)
        store = self._store(_MAX_PROBES_PER_TICK + 1)
        overflow_ticket_id = f"GEN-cap-{_MAX_PROBES_PER_TICK}"

        resolve_stale_pr_ticket_ids(pr_gate_client, store)

        assert (
            f"{_CLIENT}/{_cache_ticket_id(overflow_ticket_id)}"
            not in load_open_pr_probe_cache()
        )
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
                _cache_ticket_id(f"GEN-cap-{i}"),
                OpenPrProbeCache(probed_at=now, has_open_pr=True),
            )
        stub = _stub_probe(monkeypatch, _OPEN)
        store = self._store(count)

        gated = resolve_stale_pr_ticket_ids(pr_gate_client, store, now=now)

        assert stub.calls == []
        assert len(gated) == count


class TestAdoptedRowFilter:
    """RFC 0016 R8 (#2943): an adopted row's own PR is not a stale dispatch."""

    _BRANCH = "feature/ext-pr"

    def _store(self, stage: Stage = Stage.IMPL) -> DevQueueStore:
        return DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-adopt",
                    client=_CLIENT,
                    stage=stage,
                    branch=self._BRANCH,
                    adopted_head_sha="a" * 40,
                    adopted_pr_url=_PR_URL,
                    pr_url=_PR_URL,
                )
            ]
        )

    def test_own_adopted_pr_is_not_stale(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {}
        assert stub.calls == [self._BRANCH]

    def test_different_open_pr_on_branch_is_stale(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = "https://github.com/acme/widgets/pull/43"
        _stub_probe(
            monkeypatch, OpenPrLookup(open_pr=True, gh_available=True, url=other)
        )
        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {
            "GEN-adopt": other
        }

    def test_open_pr_without_url_is_stale(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, OpenPrLookup(open_pr=True, gh_available=True))
        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store()) == {
            "GEN-adopt": None
        }

    def test_cached_entry_gets_the_same_filter_and_stays_unfiltered(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-adopt", self._BRANCH),
            OpenPrProbeCache(probed_at=now, has_open_pr=True, pr_url=_PR_URL),
        )
        stub = _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store(), now=now) == {}
        assert stub.calls == []
        cached = load_open_pr_probe_cache()
        assert any(entry.has_open_pr for entry in cached.values())

    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.FINALIZE])
    def test_review_and_finalize_stages_are_untouched(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
    ) -> None:
        stub = _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(pr_gate_client, self._store(stage)) == {}
        assert stub.calls == []


class TestRegressedOwnBranchRelease:
    """#3248: a row regressed into IMPL after reaching review/finalize owns the
    open PR on its branch, so the duplicate-work gate releases it."""

    @pytest.mark.parametrize(
        ("stage", "regressed_into", "high_water", "expected"), _REGRESS_CASES
    )
    def test_fresh_probe_matches_the_table(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
        regressed_into: Stage | None,
        high_water: Stage | None,
        expected: dict[str, str],
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        store = _regressed_store(
            stage, regressed_into=regressed_into, high_water=high_water
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == expected

    @pytest.mark.parametrize(
        ("stage", "regressed_into", "high_water", "expected"), _REGRESS_CASES
    )
    def test_cached_verdict_matches_the_table(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        stage: Stage,
        regressed_into: Stage | None,
        high_water: Stage | None,
        expected: dict[str, str],
    ) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            _CLIENT,
            _cache_ticket_id("GEN-reg"),
            OpenPrProbeCache(probed_at=now, has_open_pr=True, pr_url=_PR_URL),
        )
        stub = _stub_probe(monkeypatch, _OPEN)
        store = _regressed_store(
            stage, regressed_into=regressed_into, high_water=high_water
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store, now=now) == expected
        assert stub.calls == []

    def test_cache_stays_unfiltered_for_exempt_row(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_probe(monkeypatch, _OPEN)
        store = _regressed_store(
            Stage.IMPL, regressed_into=Stage.IMPL, high_water=Stage.FINALIZE
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store) == {}
        cached = load_open_pr_probe_cache()[f"{_CLIENT}/{_cache_ticket_id('GEN-reg')}"]
        assert cached.has_open_pr is True
        assert cached.pr_url == _PR_URL

    @pytest.mark.parametrize("cached", [False, True], ids=["fresh", "cached"])
    @pytest.mark.parametrize(
        ("regressed_into", "expected"),
        [(Stage.IMPL, {}), (None, {"GEN-reg": None})],
        ids=["exempt-released", "fresh-row-still-parks"],
    )
    def test_open_pr_without_url_releases_an_exempt_row_but_parks_a_non_exempt_row(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        cached: bool,
        regressed_into: Stage | None,
        expected: dict[str, str | None],
    ) -> None:
        """The task-only exemption ignores the found URL (R2); A11 still parks
        a row that only the adopted or review-feedback exemptions could cover."""
        now = datetime.now(UTC)
        if cached:
            save_open_pr_probe_entry(
                _CLIENT,
                _cache_ticket_id("GEN-reg"),
                OpenPrProbeCache(probed_at=now, has_open_pr=True, pr_url=None),
            )
            _stub_probe(monkeypatch, _OPEN)
        else:
            _stub_probe(monkeypatch, OpenPrLookup(open_pr=True, gh_available=True))
        store = _regressed_store(
            Stage.IMPL, regressed_into=regressed_into, high_water=Stage.FINALIZE
        )

        assert resolve_stale_pr_ticket_ids(pr_gate_client, store, now=now) == expected


class TestRegressMarkerTiming:
    """#3248 R3: the exemption holds at the first claim, through the REAL seams.

    No hand-built marker: the regress and requeue paths stamp it themselves.
    """

    @staticmethod
    def _finalize_row() -> TicketTask:
        return _make_ticket_task(
            ticket_id="GEN-reg",
            client=_CLIENT,
            stage=Stage.FINALIZE,
            status=QueueItemStatus.RUNNING,
            stage_high_water=Stage.FINALIZE,
            pr_url=_PR_URL,
        )

    def test_finalize_self_heal_regress_is_released_at_the_first_claim(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = self._finalize_row()
        _stage_regress(task, Stage.IMPL)
        assert task.regressed_into_stage == Stage.IMPL
        assert task.pr_url is None  # premise correction: the PENDING seam clears it
        assert task.status == QueueItemStatus.PENDING

        _stub_probe(monkeypatch, _OPEN)
        assert (
            resolve_stale_pr_ticket_ids(pr_gate_client, DevQueueStore(tasks=[task]))
            == {}
        )

    def test_operator_regress_from_review_is_released_at_the_first_claim(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daemon = install_acme_client(tmp_path, monkeypatch)
        seed_queue([make_blocked(stage=Stage.REVIEW, stage_high_water=Stage.REVIEW)])

        requeue_ticket(
            ACME_TICKET,
            ACME,
            stage_override="impl",
            allow_regress=True,
            native_daemon=daemon,
        )

        task = queue_row()
        assert task.stage == Stage.IMPL
        assert task.status == QueueItemStatus.PENDING
        assert task.regressed_into_stage == Stage.IMPL

        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(get_client(ACME), load_dev_queue()) == {}

    def test_marker_consumed_at_spawn_success_parks_the_next_claim(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1801 boundary: a spawn that succeeds consumes the marker; if that
        session then dies with no sentinel, the open PR parks the next claim."""
        task = self._finalize_row()
        _stage_regress(task, Stage.IMPL)
        _apply_spawn_success_fields(task, session_id="s-2")
        task.session_id = None

        assert task.regressed_into_stage is None
        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(
            pr_gate_client, DevQueueStore(tasks=[task])
        ) == {"GEN-reg": _PR_URL}


class TestGateParkStampDoesNotSelfExempt:
    """#2740 park stamp: a row parked by the gate and released through the seam
    re-parks; the stamped pr_url must not exempt it."""

    def test_park_stamped_row_released_through_the_seam_re_parks(
        self,
        tmp_config_dir: Path,
        pr_gate_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = _make_ticket_task(ticket_id="GEN-reg", client=_CLIENT, stage=Stage.IMPL)
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=STALE_DISPATCH_GATE_DISPOSITION,
            blocked_reason=_PRE_DISPATCH_STALE_PR_REASON,
            pr_url=_PR_URL,
            unproductive=False,
        )
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.pr_url is None

        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(
            pr_gate_client, DevQueueStore(tasks=[task])
        ) == {"GEN-reg": _PR_URL}


class TestPlainRequeueOfGateParkedRow:
    """#3248 R4(d): characterizes CURRENT behaviour. A plain requeue of a
    gate-parked row clears the regress marker and pr_url, so it re-parks while
    its PR stays open (documented in headless-contract.md)."""

    @pytest.mark.parametrize(
        "marker", [None, Stage.IMPL], ids=["consumed", "still-set"]
    )
    def test_plain_requeue_clears_the_marker_and_the_row_re_parks(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        marker: Stage | None,
    ) -> None:
        daemon = install_acme_client(tmp_path, monkeypatch)
        seed_queue(
            [
                make_blocked(
                    stage=Stage.IMPL,
                    stage_high_water=Stage.FINALIZE,
                    regressed_into_stage=marker,
                    disposition=STALE_DISPATCH_GATE_DISPOSITION,
                    blocked_reason=_PRE_DISPATCH_STALE_PR_REASON,
                    pr_url=_PR_URL,
                )
            ]
        )

        requeue_ticket(ACME_TICKET, ACME, native_daemon=daemon)

        task = queue_row()
        assert task.status == QueueItemStatus.PENDING
        assert task.pr_url is None
        assert task.regressed_into_stage is None

        _stub_probe(monkeypatch, _OPEN)
        assert resolve_stale_pr_ticket_ids(get_client(ACME), load_dev_queue()) == {
            ACME_TICKET: _PR_URL
        }
