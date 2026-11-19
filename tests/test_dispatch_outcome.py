"""Tests for ``cw.dispatch.claim.outcome`` — the shared pre-spawn park (#3744).

``park_before_spawn`` parks a claimed RUNNING row BLOCKED_ON_USER and, only for
an environmental park (``count_streak=True``), advances the consecutive-park
streak that engages the lane breaker at ``_PARK_STREAK_CIRCUIT_THRESHOLD``
(R7). A static-capability park never counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.dispatch.claim import outcome
from cw.dispatch.claim.outcome import (
    _PARK_STREAK_CIRCUIT_THRESHOLD,
    park_before_spawn,
    reset_park_streak,
)
from cw.models import QueueItemStatus, StageExecutorConfig, TicketTask

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cw.models import ClientConfig


@pytest.fixture(autouse=True)
def _fresh_streak() -> Iterator[None]:
    reset_park_streak()
    yield
    reset_park_streak()


def _running(ticket_id: str) -> TicketTask:
    add_ticket(TicketTask(ticket_id=ticket_id, client="test-client"))
    store = load_dev_queue()
    task = store.tasks[-1]
    task.status = QueueItemStatus.RUNNING
    task.session_id = "sess-1"
    save_dev_queue(store)
    return task


def _park(
    client: ClientConfig, ticket_id: str, *, count: bool
) -> outcome._SpawnOutcome:
    return park_before_spawn(
        _running(ticket_id),
        client,
        disposition="codex_not_found",
        breadcrumbs="codex binary not found",
        count_streak=count,
    )


def test_a_park_moves_the_row_blocked_on_user(
    sample_client_config: ClientConfig,
) -> None:
    result = _park(sample_client_config, "T-1", count=True)

    row = load_dev_queue().tasks[0]
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == "codex_not_found"
    assert row.session_id is None
    assert row.unproductive_attempts == 0
    assert result.capability_parked is True
    assert result.error == "codex_not_found"


def test_counted_parks_engage_the_breaker_at_the_threshold(
    sample_client_config: ClientConfig,
) -> None:
    results = [
        _park(sample_client_config, f"T-{i}", count=True)
        for i in range(_PARK_STREAK_CIRCUIT_THRESHOLD)
    ]

    assert [r.spawn_error for r in results] == [False, False, True]
    assert outcome._park_streak[0] == _PARK_STREAK_CIRCUIT_THRESHOLD


def test_a_static_park_never_counts(sample_client_config: ClientConfig) -> None:
    """R7: a static-capability park neither advances nor engages the streak."""
    for i in range(_PARK_STREAK_CIRCUIT_THRESHOLD - 1):
        _park(sample_client_config, f"T-c{i}", count=True)

    static = [
        _park(sample_client_config, f"T-s{i}", count=False)
        for i in range(_PARK_STREAK_CIRCUIT_THRESHOLD)
    ]

    assert not any(r.spawn_error for r in static)
    assert all(r.capability_parked for r in static)
    assert outcome._park_streak[0] == _PARK_STREAK_CIRCUIT_THRESHOLD - 1


def test_a_capable_probe_resets_the_streak(
    sample_client_config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.dispatch.claim.preflight_reach import preflight_before_spawn
    from cw.executor import CodexCapabilityDiagnosis
    from cw.models import CODEX_BACKEND

    for i in range(_PARK_STREAK_CIRCUIT_THRESHOLD - 1):
        _park(sample_client_config, f"T-{i}", count=True)
    monkeypatch.setattr(
        "cw.dispatch.claim.preflight_reach._cached_codex_capability_diagnosis",
        lambda: CodexCapabilityDiagnosis(None, "codex-cli 0.136.0"),
    )
    monkeypatch.setattr(
        "cw.dispatch.claim.preflight_reach.resolve_executor_config",
        lambda *_a, **_k: StageExecutorConfig(backend=CODEX_BACKEND),
    )

    assert preflight_before_spawn(_running("T-ok"), sample_client_config) is None
    assert outcome._park_streak[0] == 0
    assert not _park(sample_client_config, "T-after", count=True).spawn_error
