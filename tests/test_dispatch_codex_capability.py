"""Tests for ``cw.dispatch.claim.codex_capability`` — the codex capability gate (#2908).

Mirrors ``tests/test_dispatch_pr_gate.py``'s naming/shape: one test module per
dispatch submodule. Covers the gate, the verdict cache TTL and the consecutive
park counter. The probe-classification matrix (timeout, unparseable output,
missing binary, healthy version) lives in ``tests/test_codex_executor.py`` and
is not duplicated here.

Every test is hermetic: the subprocess call and the clock are stubbed, so no
real ``codex`` binary (or its speed) is ever consulted.
"""

from __future__ import annotations

import subprocess
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch import (
    _CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS,
    _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD,
    _CODEX_CAPABILITY_PROBE_TTL_SECONDS,
    _cached_codex_capability_diagnosis,
    _codex_capability_gate,
    _reset_codex_capability_cache,
    dispatch_tick,
)
from cw.dispatch.claim.codex_capability import (
    _CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS,
)
from cw.executor import (
    CODEX_VERSION_PROBE_TIMEOUT,
    CODEX_VERSION_UNKNOWN,
    CodexCapabilityDiagnosis,
)
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    OrchestratorConfig,
    QueueItemStatus,
    StageExecutorConfig,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import ClientSpec, executor_backend_extra, write_clients_yaml
from tests._codex_review_helpers import _mk_codex_proc

if TYPE_CHECKING:
    from collections.abc import Iterator

_FROZEN_START = "2026-11-03 12:00:00"

# Probe latency (seconds) the loaded-shell-wrapper stub models: any budget below
# it times out. Sits above the ~2.3s warm wrapper latency with load headroom, and
# above the old 3s gate budget so the old constant fails the headroom test.
_LOADED_WRAPPER_LATENCY_SECONDS = 4.0

_TIMEOUT_DIAGNOSIS = CodexCapabilityDiagnosis(
    CODEX_VERSION_PROBE_TIMEOUT,
    f"codex --version timed out ({_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS}s)",
)
_UNKNOWN_DIAGNOSIS = CodexCapabilityDiagnosis(
    CODEX_VERSION_UNKNOWN, "could not parse version: junk"
)
_CAPABLE_DIAGNOSIS = CodexCapabilityDiagnosis(None, "codex-cli 0.136.0")


@pytest.fixture(autouse=True)
def _reset_capability_cache() -> Iterator[None]:
    """Clear the cache/park counter around each test.

    The "after" half keeps a leaked 10s timeout verdict from reaching another
    test in the same xdist worker.
    """
    _reset_codex_capability_cache()
    yield
    _reset_codex_capability_cache()


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch,
    stub: object,
) -> None:
    monkeypatch.setattr(
        "cw.dispatch.claim.codex_capability.codex_capability_diagnosis", stub
    )


class TestProbeHeadroom:
    """The real probe, driven through the gate's cache, with a stubbed subprocess."""

    def test_loaded_wrapper_latency_clears_gate_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrapper that takes ~4s under load must still report capable (#2908).

        Fails on the old 3s budget (3 < 4.0), passes at 8s.
        """
        monkeypatch.setattr(
            "cw.executor.core.shutil.which", lambda _name: "/usr/bin/codex"
        )

        def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout = kwargs["timeout"]
            assert isinstance(timeout, (int, float))
            if timeout < _LOADED_WRAPPER_LATENCY_SECONDS:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return _mk_codex_proc("codex-cli 0.136.0\n")

        monkeypatch.setattr("cw.executor.core.subprocess.run", _run)

        probe = _cached_codex_capability_diagnosis()

        assert _CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS > 2.3
        assert probe.diagnosis is None

    def test_hung_codex_reports_probe_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A binary that never returns is a timeout, not an unknown version."""
        monkeypatch.setattr(
            "cw.executor.core.shutil.which", lambda _name: "/usr/bin/codex"
        )

        def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout = kwargs["timeout"]
            assert isinstance(timeout, (int, float))
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr("cw.executor.core.subprocess.run", _run)

        probe = _cached_codex_capability_diagnosis()

        assert probe.diagnosis == CODEX_VERSION_PROBE_TIMEOUT
        assert f"timed out ({_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS}s)" in probe.detail


class TestVerdictCacheTtl:
    """Timeout verdicts are cached briefly, measured from probe completion."""

    def test_timeout_verdict_dedupes_after_slow_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe slower than the TTL itself must still leave a dedupe window.

        Fails if ``checked_at`` is stamped before the probe: elapsed would be
        TTL + 1 >= TTL, so the immediate second call would re-probe. Ticking only
        the 8s gate timeout would not discriminate (8 < 10 still hits).
        """
        calls: list[int] = []
        with freeze_time(_FROZEN_START) as frozen:

            def _slow_probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
                calls.append(1)
                frozen.tick(
                    delta=timedelta(
                        seconds=_CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS + 1
                    )
                )
                return _TIMEOUT_DIAGNOSIS

            _stub_probe(monkeypatch, _slow_probe)
            first = _cached_codex_capability_diagnosis()
            second = _cached_codex_capability_diagnosis()

        assert first == second == _TIMEOUT_DIAGNOSIS
        assert len(calls) == 1, "immediate second call must hit the cache"

    def test_timeout_verdict_reused_then_reprobed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reused 9s after the probe returns, re-probed at 11s."""
        assert (
            _CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS
            < _CODEX_CAPABILITY_PROBE_TTL_SECONDS
        )
        calls: list[int] = []
        with freeze_time(_FROZEN_START) as frozen:

            def _slow_probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
                calls.append(1)
                frozen.tick(
                    delta=timedelta(seconds=_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS)
                )
                return _TIMEOUT_DIAGNOSIS

            _stub_probe(monkeypatch, _slow_probe)
            _cached_codex_capability_diagnosis()
            frozen.tick(delta=timedelta(seconds=9))
            _cached_codex_capability_diagnosis()
            assert len(calls) == 1, "verdict must be reused 9s after probe return"
            frozen.tick(delta=timedelta(seconds=2))
            _cached_codex_capability_diagnosis()

        assert len(calls) == 2, "verdict must be re-probed 11s after probe return"

    def test_timeout_verdict_recovers_after_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the timeout verdict expires, a healthy probe is picked up."""
        verdicts = [_TIMEOUT_DIAGNOSIS, _CAPABLE_DIAGNOSIS]

        def _probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
            return verdicts.pop(0)

        _stub_probe(monkeypatch, _probe)
        with freeze_time(_FROZEN_START) as frozen:
            first = _cached_codex_capability_diagnosis()
            frozen.tick(
                delta=timedelta(seconds=_CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS + 1)
            )
            second = _cached_codex_capability_diagnosis()

        assert first.diagnosis == CODEX_VERSION_PROBE_TIMEOUT
        assert second.diagnosis is None

    def test_non_timeout_failure_keeps_full_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shorter TTL is keyed on the diagnosis: unknown-version keeps 60s."""
        calls: list[int] = []

        def _probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
            calls.append(1)
            return _UNKNOWN_DIAGNOSIS

        _stub_probe(monkeypatch, _probe)
        reuse_after = _CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS + 20
        assert reuse_after < _CODEX_CAPABILITY_PROBE_TTL_SECONDS
        with freeze_time(_FROZEN_START) as frozen:
            _cached_codex_capability_diagnosis()
            frozen.tick(delta=timedelta(seconds=reuse_after))
            _cached_codex_capability_diagnosis()

        assert len(calls) == 1, "unknown-version verdict must still be reused at +30s"


class TestGateTimeoutPark:
    """A timeout verdict parks through the unchanged gate path (D5)."""

    def test_timeout_verdict_parks_blocked_on_user(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(
            ClientSpec.from_config(
                sample_client_config, extra=executor_backend_extra("plan", "codex")
            )
        )
        add_ticket(TicketTask(ticket_id="GEN-CDX-T1", client="test-client"))
        _stub_probe(monkeypatch, lambda **_kwargs: _TIMEOUT_DIAGNOSIS)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == CODEX_VERSION_PROBE_TIMEOUT
        assert task.session_id is None

    def test_timeout_parks_trip_breaker_at_threshold(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Timeout parks count toward the breaker exactly like unknown-version."""
        _stub_probe(monkeypatch, lambda **_kwargs: _TIMEOUT_DIAGNOSIS)
        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability.resolve_executor_config",
            lambda *_a, **_k: StageExecutorConfig(backend=CODEX_BACKEND),
        )

        outcomes = []
        for i in range(_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD):
            add_ticket(TicketTask(ticket_id=f"GEN-CDX-T2-{i}", client="test-client"))
            task = load_dev_queue().tasks[-1]
            outcomes.append(_codex_capability_gate(task, sample_client_config))

        assert all(o is not None and o.capability_parked for o in outcomes)
        assert all(
            o is not None and o.error == CODEX_VERSION_PROBE_TIMEOUT for o in outcomes
        )
        below = outcomes[: _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD - 1]
        assert all(o is not None and not o.spawn_error for o in below)
        at_threshold = outcomes[-1]
        assert at_threshold is not None
        assert at_threshold.spawn_error
