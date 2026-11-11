"""Unit tests for cw.reconcile.tasks.stale_gate.

release_stale_gated_tasks (GitHub #1713).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import freezegun
import pytest

from cw.auto_dev_result import GATE_TIMEOUT_BLOCKER_REASON
from cw.config import (
    save_state,
)
from cw.dev_queue import GATE_TIMEOUT_DISPOSITION, load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Stage,
    TicketTask,
    WatchedPr,
)
from cw.pr_hydrate import hydrate_pr_states
from cw.reconcile import (
    release_stale_gated_tasks,
)
from cw.reconcile.stale_dispatch_watch import register_stale_dispatch_watched_prs
from cw.reconcile.tasks import (
    _closed_pr_numbers_by_client,
    _is_variant_a_gate_task,
    _merged_pr_numbers_by_client,
)
from tests._reconcile_helpers import (
    _client_with_lane,
    gate_blocked_last_result,
    park_via_routing,
)
from tests._shared.session_seeding import _make_ticket_task

# ---------------------------------------------------------------------------
# release_stale_gated_tasks (GitHub #1713)
#
# Conftest inventory (binding operator resolution #3): reuses
# tests.conftest._make_ticket_task for every TicketTask construction and
# tests._reconcile_helpers._client_with_lane for the lane-override test. No
# hand-rolled builders introduced.
# ---------------------------------------------------------------------------


def _emit_pr_merged(*, client: str, ticket_id: str, repo: str, pr_number: int) -> None:
    """Emit a PR_MERGED event matching apply_pr_state_observation's payload
    shape (pr_hydrate.py's ``base`` dict: {repo, pr_number, ticket_id, client})."""
    record_event(
        OrchestratorEventType.PR_MERGED,
        {
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": ticket_id,
            "client": client,
        },
        correlation_id=ticket_id,
    )


def test_release_stale_gated_tasks_signal_only_stamps_and_emits(
    tmp_config_dir: Path,
) -> None:
    """Variant A (merge_pending, own PR merged) under default signal_only:
    status unchanged, stale_gate_detected_at stamped, SESSION_REAP_PROPOSED
    emitted with reason=STALE_GATE, proposed_action=variant_a."""
    task = _make_ticket_task(
        ticket_id="SG-A1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/42",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A1", repo="foo/bar", pr_number=42)

    released = release_stale_gated_tasks()

    assert released == ["SG-A1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A1")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["reason"] == ReapReason.STALE_GATE.value
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_a"


def test_release_stale_gated_tasks_automerge_not_armed_via_pr_info_fallback(
    tmp_config_dir: Path,
) -> None:
    """Variant A detection fires identically for an automerge_not_armed park
    (disposition='blocked', blocked_reason='automerge_not_armed') whose
    pr_url was already populated via dispatch/routing.py's pr_info fallback
    (tested separately in test_dispatch.py)."""
    task = _make_ticket_task(
        ticket_id="SG-A2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="blocked",
        blocked_reason="automerge_not_armed",
        pr_url="https://github.com/foo/bar/pull/43",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A2", repo="foo/bar", pr_number=43)

    released = release_stale_gated_tasks()

    assert released == ["SG-A2"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A2")
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER


def _gate_timeout_parked_task(ticket_id: str, pr_number: int) -> TicketTask:
    """A #3372 reuse-path gate-timeout park, as routing stamps it."""
    return _make_ticket_task(
        ticket_id=ticket_id,
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=Stage.FINALIZE,
        disposition=GATE_TIMEOUT_DISPOSITION,
        blocked_reason=GATE_TIMEOUT_BLOCKER_REASON,
        pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
        session_id=f"sess-{ticket_id}",
    )


def test_release_stale_gated_tasks_gate_timeout_signal_only_stamps(
    tmp_config_dir: Path,
) -> None:
    """#3372: a gate_timeout park whose PR merged on its own (auto-merge was
    left as found) is a Variant A release; signal_only stamps, no mutation."""
    task = _gate_timeout_parked_task("SG-GT1", 61)
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-GT1", repo="foo/bar", pr_number=61)

    released = release_stale_gated_tasks()

    assert released == ["SG-GT1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GT1")
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.disposition == GATE_TIMEOUT_DISPOSITION


def test_release_stale_gated_tasks_gate_timeout_auto_policy_completes(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3372: under reap_policy=auto a merged gate_timeout park ships."""
    task = _gate_timeout_parked_task("SG-GT2", 62)
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )
    _emit_pr_merged(client="client-a", ticket_id="SG-GT2", repo="foo/bar", pr_number=62)

    released = release_stale_gated_tasks()

    assert released == ["SG-GT2"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GT2")
    assert reloaded.status == QueueItemStatus.COMPLETED
    assert reloaded.disposition == "shipped"
    assert reloaded.session_id is None


@pytest.mark.parametrize(
    ("status", "disposition", "blocked_reason"),
    [
        pytest.param(
            QueueItemStatus.PENDING,
            GATE_TIMEOUT_DISPOSITION,
            GATE_TIMEOUT_BLOCKER_REASON,
            id="wrong-status",
        ),
        pytest.param(
            QueueItemStatus.BLOCKED_ON_USER,
            GATE_TIMEOUT_DISPOSITION,
            "agent_block",
            id="gate-timeout-disposition-other-reason",
        ),
        pytest.param(
            QueueItemStatus.BLOCKED_ON_USER,
            "blocked",
            GATE_TIMEOUT_BLOCKER_REASON,
            id="blocked-disposition-gate-timeout-reason",
        ),
    ],
)
def test_is_variant_a_gate_task_rejects_partial_gate_timeout_pair(
    status: QueueItemStatus, disposition: str, blocked_reason: str
) -> None:
    """#3372: the new Variant A arm keys on the stamped (disposition, reason)
    pair on a BLOCKED_ON_USER row, never on either half alone."""
    task = _make_ticket_task(
        ticket_id="SG-GT-NEG",
        status=status,
        disposition=disposition,
        blocked_reason=blocked_reason,
        pr_url="https://github.com/foo/bar/pull/63",
    )
    assert _is_variant_a_gate_task(task) is False


def test_is_variant_a_gate_task_accepts_gate_timeout_pair() -> None:
    assert _is_variant_a_gate_task(_gate_timeout_parked_task("SG-GT3", 64)) is True


def test_release_stale_gated_tasks_auto_policy_completes_variant_a(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: Variant A completes to shipped and still emits
    SESSION_REAP_PROPOSED -- the event fires regardless of policy."""
    task = _make_ticket_task(
        ticket_id="SG-A3",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/44",
        session_id="sess-a3",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )
    _emit_pr_merged(client="client-a", ticket_id="SG-A3", repo="foo/bar", pr_number=44)

    released = release_stale_gated_tasks()

    assert released == ["SG-A3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A3")
    assert reloaded.status == QueueItemStatus.COMPLETED
    assert reloaded.disposition == "shipped"
    assert reloaded.session_id is None

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A3"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_a"


def test_release_stale_gated_tasks_auto_policy_requeues_variant_b(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: Variant B (blocked behind another ticket's PR that
    has since merged) requeues to PENDING, clears session_id, still emits
    SESSION_REAP_PROPOSED."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKER",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/50",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=50,
        session_id="sess-b1",
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-B1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B1")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-B1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


def test_release_stale_gated_tasks_auto_policy_requeues_variant_b_closed_unmerged(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: Variant B (blocked behind another ticket's PR that
    has since been CLOSED unmerged, GitHub #1920) requeues to PENDING,
    clears session_id, still emits SESSION_REAP_PROPOSED -- mirrors the
    MERGED case above."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKERC",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/150",
        pr_state=PrState(state="CLOSED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B1C",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=150,
        session_id="sess-b1c",
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-B1C"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B1C")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-B1C"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


# GitHub #1862/#1902 — the verbatim agent sentinel shape a stale_dispatch park
# is routed from; mirrors TestStaleDispatchSentinelRouting._last_result() in
# tests/test_dispatch.py so the two stay recognizably the same payload.
_STALE_DISPATCH_LAST_RESULT: dict[str, object] = {
    "status": "stale_dispatch",
    "blocker": {
        "stage": "stage1_pre_flight",
        "reason": "pr_already_open",
        "details": "PR #1899 (dev/1862) is open and awaiting review.",
    },
    "commits": [],
    "review": {"must_fix_initial": 0, "should_fix": 0},
}
_STALE_DISPATCH_PR_NUMBER = 1899
_STALE_DISPATCH_SLUG = "foo/bar"


def _register_and_hydrate_merged_watch(
    monkeypatch: pytest.MonkeyPatch, *, state: str = "MERGED"
) -> None:
    """Run the two real production passes that give the park a PR-state source.

    ``register_stale_dispatch_watched_prs`` is called as a STANDALONE step
    (not inline with the stamping above), so what this exercises is the
    retroactive per-tick rescan of an already-parked row — binding A2's
    "a park stamped before this feature existed must still be registered".
    ``hydrate_pr_states`` then fills the watch's ``pr_state`` through the same
    ``_derive_pr_state`` seam a real serve tick uses.
    """
    monkeypatch.setattr(
        "cw.reconcile.stale_dispatch_watch._resolve_repo_slug",
        lambda _git_dir: _STALE_DISPATCH_SLUG,
    )
    register_stale_dispatch_watched_prs()
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
    monkeypatch.setattr(
        "cw.pr_hydrate.fetch_pr_view",
        lambda *_a, **_kw: {
            "state": state,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
            "reviewDecision": "",
            "isDraft": False,
            "reviewRequests": [],
            "comments": [],
        },
    )
    hydrate_pr_states(OrchestratorConfig())
    # Pin what makes these tests load-bearing rather than incidental: the
    # blocking PR's state reaches the release scan ONLY through the watch --
    # no task row in the store carries that pr_url for the task-scan half of
    # _merged_pr_numbers_by_client to find.
    store = load_dev_queue()
    assert all(t.pr_url is None for t in store.tasks)
    assert len(store.watched_prs) == 1
    assert store.watched_prs[0].pr_number == _STALE_DISPATCH_PR_NUMBER
    assert store.watched_prs[0].pr_state is not None
    assert store.watched_prs[0].pr_state.state == state


def test_release_stale_gated_tasks_auto_policy_requeues_stale_dispatch_variant_b(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: a stale_dispatch/pr_already_open park (the agent-
    reported GitHub #1862 sentinel, extended per #1902) requeues to PENDING
    once its own blocking PR is observed MERGED.

    End-to-end through the production path #1927 closed: the row is parked by
    ``apply_staged_decision`` (not hand-built), the blocking PR's state comes
    from a ``WatchedPr`` registered by a standalone retroactive rescan and
    hydrated by the real ``hydrate_pr_states`` pass, and no other task row
    carries that PR anywhere in the store.
    """
    park_via_routing(
        "SG-SD1", "client-a", tmp_path, "stale_dispatch", _STALE_DISPATCH_LAST_RESULT
    )
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch)
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-SD1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD1")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition
    # #2717: stale_dispatch is not a gate-block, so the release has no record.
    assert reloaded.gate_block is None

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-SD1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


def test_release_stale_gated_tasks_auto_policy_requeues_stale_dispatch_variant_b_closed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: a stale_dispatch/pr_already_open park requeues to
    PENDING once its own blocking PR is observed CLOSED unmerged (GitHub
    #1920) -- mirrors the MERGED case above, same end-to-end production
    path (#1927)."""
    park_via_routing(
        "SG-SD1C", "client-a", tmp_path, "stale_dispatch", _STALE_DISPATCH_LAST_RESULT
    )
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch, state="CLOSED")
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-SD1C"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD1C")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-SD1C"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


def test_release_stale_gated_tasks_stale_dispatch_open_pr_not_released_end_to_end(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same production path, blocking PR still OPEN: no release, no stamp.

    The no-false-release half of the #1927 contract — proves the new
    ``WatchedPr`` source releases on MERGED specifically, not merely on the
    watch existing.
    """
    park_via_routing(
        "SG-SD-OPEN",
        "client-a",
        tmp_path,
        "stale_dispatch",
        _STALE_DISPATCH_LAST_RESULT,
    )
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch, state="OPEN")
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    assert release_stale_gated_tasks() == []

    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD-OPEN")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == _STALE_DISPATCH_PR_NUMBER
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_stale_dispatch_still_open_pr_is_noop(
    tmp_config_dir: Path,
) -> None:
    """A stale_dispatch park whose blocking PR is still OPEN (not yet
    merged) must not be released -- the ticket's explicit no-false-release
    case (#1902). Narrow predicate-edge fixture: hand-built rows keep this
    test focused on the cross-reference predicate itself; the end-to-end
    production path is covered by
    test_release_stale_gated_tasks_stale_dispatch_open_pr_not_released_end_to_end."""
    blocking = _make_ticket_task(
        ticket_id="SG-SD-BLOCKER2",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_url="https://github.com/foo/bar/pull/71",
        pr_state=PrState(state="OPEN"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-SD2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="stale_dispatch",
        blocked_reason="pr_already_open",
        blocked_on_pr=71,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD2")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == 71


def test_release_stale_gated_tasks_stale_dispatch_signal_only_stamps(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default signal_only (ADR-0006): the same end-to-end production path as
    the AUTO test above stamps ``stale_gate_detected_at`` and leaves
    ``status`` untouched -- no destructive mutation without an opt-in."""
    park_via_routing(
        "SG-SD3", "client-a", tmp_path, "stale_dispatch", _STALE_DISPATCH_LAST_RESULT
    )
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch)

    released = release_stale_gated_tasks()

    assert released == ["SG-SD3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD3")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == _STALE_DISPATCH_PR_NUMBER


def test_release_stale_gated_tasks_stale_dispatch_signal_only_stamps_closed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default signal_only (ADR-0006): the same end-to-end production path as
    the AUTO CLOSED test above stamps ``stale_gate_detected_at`` and leaves
    ``status`` untouched -- no destructive mutation without an opt-in
    (GitHub #1920)."""
    park_via_routing(
        "SG-SD3C", "client-a", tmp_path, "stale_dispatch", _STALE_DISPATCH_LAST_RESULT
    )
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch, state="CLOSED")

    released = release_stale_gated_tasks()

    assert released == ["SG-SD3C"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD3C")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == _STALE_DISPATCH_PR_NUMBER


# GitHub #2717: the blocking COMPLETED row's PR number, aligned with the
# parked row's ``blocked_on_pr`` (``gate_blocked_last_result(50)``).
_GATE_BLOCKING_PR = 50
_GATE_FROZEN_NOW = "2026-07-16 12:00:00"
_GATE_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _append_gate_blocking_row(state: str) -> None:
    """Append the COMPLETED row whose PR the gate-parked row waits behind."""
    store = load_dev_queue()
    store.tasks.append(
        _make_ticket_task(
            ticket_id="SG-GB-BLOCKER",
            client="client-a",
            status=QueueItemStatus.COMPLETED,
            pr_url=f"https://github.com/foo/bar/pull/{_GATE_BLOCKING_PR}",
            pr_state=PrState(state=state),
        )
    )
    save_dev_queue(store)
    save_state(CwState(sessions=[]))


def _set_auto_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )


@pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
def test_release_variant_b_auto_clears_gate_block_wait_keeps_count(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_state: str,
) -> None:
    """#2717: the AUTO release fires on MERGED/CLOSED evidence, so the gate's
    own backoff wait is cleared (the row is claimable at once) while the run
    count is kept for a re-park on the same fingerprint."""
    with freezegun.freeze_time(_GATE_FROZEN_NOW):
        park_via_routing(
            "SG-GB1",
            "client-a",
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(_GATE_BLOCKING_PR),
        )
    _append_gate_blocking_row(pr_state)
    _set_auto_reap(monkeypatch)

    released = release_stale_gated_tasks()

    assert released == ["SG-GB1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GB1")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.next_eligible_at is None
    assert reloaded.gate_block is not None
    assert reloaded.gate_block.count == 1


def test_release_variant_b_signal_only_leaves_gate_block_wait(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """#2717: the default signal_only policy only stamps the detection; the
    gate's wait and record are untouched."""
    with freezegun.freeze_time(_GATE_FROZEN_NOW):
        parked = park_via_routing(
            "SG-GB2",
            "client-a",
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(_GATE_BLOCKING_PR),
        )
    _append_gate_blocking_row("MERGED")

    released = release_stale_gated_tasks()

    assert released == ["SG-GB2"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GB2")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.next_eligible_at == parked.next_eligible_at
    assert reloaded.gate_block == parked.gate_block


def test_release_variant_b_auto_preserves_usage_limit_window(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2717: a later usage-limit window written over the gate's wait is not
    the gate's to clear (``next_eligible_at != gate_block.until``)."""
    with freezegun.freeze_time(_GATE_FROZEN_NOW):
        park_via_routing(
            "SG-GB3",
            "client-a",
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(_GATE_BLOCKING_PR),
        )
    usage_limit_until = _GATE_NOW + timedelta(hours=4)
    store = load_dev_queue()
    store.tasks[0].next_eligible_at = usage_limit_until
    save_dev_queue(store)
    _append_gate_blocking_row("MERGED")
    _set_auto_reap(monkeypatch)

    assert release_stale_gated_tasks() == ["SG-GB3"]

    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GB3")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.next_eligible_at == usage_limit_until


def test_repark_after_auto_release_same_pr_escalates(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2717 Assumption 6: the release keeps the count, so a worker that
    re-reports the same blocking PR continues the run (count 2, 120 s)."""
    with freezegun.freeze_time(_GATE_FROZEN_NOW):
        park_via_routing(
            "SG-GB4",
            "client-a",
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(_GATE_BLOCKING_PR),
        )
    _append_gate_blocking_row("MERGED")
    _set_auto_reap(monkeypatch)
    assert release_stale_gated_tasks() == ["SG-GB4"]
    released_row = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-GB4")

    with freezegun.freeze_time(_GATE_FROZEN_NOW):
        reparked = park_via_routing(
            "SG-GB4",
            "client-a",
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(_GATE_BLOCKING_PR),
            task=released_row,
        )

    assert reparked.gate_block is not None
    assert reparked.gate_block.count == 2
    assert reparked.next_eligible_at == _GATE_NOW + timedelta(seconds=120)


def test_is_variant_b_gate_task_stale_dispatch_requires_matching_blocked_reason(
    tmp_config_dir: Path,
) -> None:
    """Guards the predicate's second `and`: a stale_dispatch-disposition row
    with a mismatched blocked_reason must not be released, even with a
    non-null blocked_on_pr and a merged blocking PR available in the
    cross-reference index. Narrow predicate-edge fixture -- hand-built rows
    are deliberate here (the production park always carries the matching
    reason, so it cannot produce this row)."""
    blocking = _make_ticket_task(
        ticket_id="SG-SD-BLOCKER4",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/73",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-SD4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="stale_dispatch",
        blocked_reason="some_other_reason",
        blocked_on_pr=73,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD4")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == 73


def _make_watched_pr(
    *,
    pr_number: int = 90,
    client: str | None = "client-a",
    state: str = "MERGED",
) -> WatchedPr:
    return WatchedPr(
        pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
        repo="foo/bar",
        pr_number=pr_number,
        client=client,
        source="stale_dispatch_park",
        pr_state=PrState(state=state),
    )


class TestMergedPrNumbersByClient:
    """The Variant B cross-reference index, now fed by two sources (#1927):
    task rows' hydrated ``pr_state`` and client-tagged ``WatchedPr`` entries.
    """

    def test_merged_client_tagged_watch_contributes(self) -> None:
        store = DevQueueStore(watched_prs=[_make_watched_pr()])
        assert _merged_pr_numbers_by_client(store) == {"client-a": {90}}

    def test_unmerged_watch_excluded(self) -> None:
        store = DevQueueStore(watched_prs=[_make_watched_pr(state="OPEN")])
        assert _merged_pr_numbers_by_client(store) == {}

    def test_watch_without_pr_state_excluded(self) -> None:
        watched = _make_watched_pr()
        watched.pr_state = None
        assert _merged_pr_numbers_by_client(DevQueueStore(watched_prs=[watched])) == {}

    def test_null_client_watch_excluded(self) -> None:
        """An operator-registered (webhook/cli) watch carries no client
        context, so its bare PR number cannot be scoped to one repo."""
        store = DevQueueStore(watched_prs=[_make_watched_pr(client=None)])
        assert _merged_pr_numbers_by_client(store) == {}

    def test_other_client_watch_does_not_leak(self) -> None:
        store = DevQueueStore(watched_prs=[_make_watched_pr(client="client-b")])
        merged = _merged_pr_numbers_by_client(store)
        assert merged.get("client-a", set()) == set()
        assert merged == {"client-b": {90}}

    def test_task_and_watch_sources_union_within_a_client(self) -> None:
        task = _make_ticket_task(
            ticket_id="SG-U1",
            client="client-a",
            pr_url="https://github.com/foo/bar/pull/91",
            pr_state=PrState(state="MERGED"),
        )
        store = DevQueueStore(tasks=[task], watched_prs=[_make_watched_pr()])
        assert _merged_pr_numbers_by_client(store) == {"client-a": {90, 91}}


class TestClosedPrNumbersByClient:
    """The Variant B cross-reference index's CLOSED-unmerged sibling
    (GitHub #1920) -- mirrors TestMergedPrNumbersByClient's six sub-tests,
    targeting ``_closed_pr_numbers_by_client``, plus a cross-contamination
    check that MERGED and CLOSED sets never leak into each other."""

    def test_closed_client_tagged_watch_contributes(self) -> None:
        store = DevQueueStore(watched_prs=[_make_watched_pr(state="CLOSED")])
        assert _closed_pr_numbers_by_client(store) == {"client-a": {90}}

    def test_open_watch_excluded(self) -> None:
        store = DevQueueStore(watched_prs=[_make_watched_pr(state="OPEN")])
        assert _closed_pr_numbers_by_client(store) == {}

    def test_watch_without_pr_state_excluded(self) -> None:
        watched = _make_watched_pr(state="CLOSED")
        watched.pr_state = None
        assert _closed_pr_numbers_by_client(DevQueueStore(watched_prs=[watched])) == {}

    def test_null_client_watch_excluded(self) -> None:
        """An operator-registered (webhook/cli) watch carries no client
        context, so its bare PR number cannot be scoped to one repo."""
        store = DevQueueStore(
            watched_prs=[_make_watched_pr(client=None, state="CLOSED")]
        )
        assert _closed_pr_numbers_by_client(store) == {}

    def test_other_client_watch_does_not_leak(self) -> None:
        store = DevQueueStore(
            watched_prs=[_make_watched_pr(client="client-b", state="CLOSED")]
        )
        closed = _closed_pr_numbers_by_client(store)
        assert closed.get("client-a", set()) == set()
        assert closed == {"client-b": {90}}

    def test_task_and_watch_sources_union_within_a_client(self) -> None:
        task = _make_ticket_task(
            ticket_id="SG-U2",
            client="client-a",
            pr_url="https://github.com/foo/bar/pull/91",
            pr_state=PrState(state="CLOSED"),
        )
        store = DevQueueStore(
            tasks=[task], watched_prs=[_make_watched_pr(state="CLOSED")]
        )
        assert _closed_pr_numbers_by_client(store) == {"client-a": {90, 91}}

    def test_merged_watch_does_not_leak_into_closed_index(self) -> None:
        """A MERGED-state watch must not appear in the CLOSED index, and a
        CLOSED-state watch must not appear in the MERGED index -- the two
        cross-reference sets are disjoint by construction."""
        merged_watch = _make_watched_pr(pr_number=90, state="MERGED")
        closed_watch = _make_watched_pr(pr_number=91, state="CLOSED")
        store = DevQueueStore(watched_prs=[merged_watch, closed_watch])
        assert _merged_pr_numbers_by_client(store) == {"client-a": {90}}
        assert _closed_pr_numbers_by_client(store) == {"client-a": {91}}


def test_release_stale_gated_tasks_variant_b_signal_only_stamps(
    tmp_config_dir: Path,
) -> None:
    """Default signal_only: Variant B stamps stale_gate_detected_at without
    mutating status, symmetric with Variant A's signal_only test."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKER2",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/60",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B3",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=60,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == ["SG-B3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B3")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == 60


def test_release_stale_gated_tasks_variant_b_signal_only_stamps_closed_unmerged(
    tmp_config_dir: Path,
) -> None:
    """Default signal_only: Variant B stamps stale_gate_detected_at without
    mutating status, for a blocking PR that was CLOSED unmerged (GitHub
    #1920) -- mirrors the MERGED signal_only test above."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKER2C",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/151",
        pr_state=PrState(state="CLOSED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B3C",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=151,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == ["SG-B3C"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B3C")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == 151


def test_release_stale_gated_tasks_variant_b_ignores_merged_task_without_pr_url(
    tmp_config_dir: Path,
) -> None:
    """A MERGED pr_state task with no pr_url (or an unparseable one) is
    excluded from the cross-reference index rather than raising."""
    blocking_no_url = _make_ticket_task(
        ticket_id="SG-B-BLOCKER3",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_state=PrState(state="MERGED"),
    )
    blocking_bad_url = _make_ticket_task(
        ticket_id="SG-B-BLOCKER4",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="not-a-github-pr-url",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=61,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking_no_url, blocking_bad_url, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B4")
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_variant_b_no_cross_reference_is_noop(
    tmp_config_dir: Path,
) -> None:
    """Variant B blocking PR absent from the queue entirely (documents the
    residual cross-reference-only blind spot): no mutation, no false-positive
    stamp."""
    blocked = _make_ticket_task(
        ticket_id="SG-B2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=999,
    )
    save_dev_queue(DevQueueStore(tasks=[blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B2")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is None
    assert reloaded.blocked_on_pr == 999


def test_release_stale_gated_tasks_idempotent_cursor(
    tmp_config_dir: Path,
) -> None:
    """Two consecutive calls with no new events between them: the second call
    is a no-op (mirrors retire_merged_prs's idempotency shape)."""
    task = _make_ticket_task(
        ticket_id="SG-A4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/45",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A4", repo="foo/bar", pr_number=45)

    first = release_stale_gated_tasks()
    second = release_stale_gated_tasks()

    assert first == ["SG-A4"]
    assert second == []


def test_release_stale_gated_tasks_ignores_non_gate_blocked_on_user(
    tmp_config_dir: Path,
) -> None:
    """A BLOCKED_ON_USER row with an unrelated disposition (signoff_gate)
    whose PR merges must not over-fire on every pr.merged event."""
    task = _make_ticket_task(
        ticket_id="SG-A5",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="signoff_gate",
        pr_url="https://github.com/foo/bar/pull/46",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A5", repo="foo/bar", pr_number=46)

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A5")
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_lane_reap_policy_override(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane-level reap_policy=auto overrides a global signal_only default,
    mirroring park_terminal_sibling_tasks's existing precedence tests."""
    task = _make_ticket_task(
        ticket_id="SG-A6",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/47",
        lane="special",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    auto_lane_client = _client_with_lane(
        "client-a", "special", reap_policy=ReapPolicy.AUTO
    )
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_clients", lambda: {"client-a": auto_lane_client}
    )
    _emit_pr_merged(client="client-a", ticket_id="SG-A6", repo="foo/bar", pr_number=47)

    released = release_stale_gated_tasks()

    assert released == ["SG-A6"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A6")
    assert reloaded.status == QueueItemStatus.COMPLETED
    assert reloaded.disposition == "shipped"


def test_release_stale_gated_tasks_reap_proposed_carries_real_session_id(
    tmp_config_dir: Path,
) -> None:
    """SESSION_REAP_PROPOSED must carry the task's real session_id, not a
    hardcoded None (GitHub #1713 review SHOULD_FIX 5). The concrete
    downstream consumer, cli/orchestrate.py's _drain_reap_proposals, does
    ``payload.get("session_id", "")`` and looks that up against
    state.sessions -- a hardcoded None means the key IS present with value
    None, so ``.get`` returns None (not the "" default) and the lookup
    always misses, silently discarding which session was live when the
    gate was detected stale. Mirrors park_terminal_sibling_tasks's
    orig_session_id precedent: captured BEFORE the release helper clears
    task.session_id."""
    task = _make_ticket_task(
        ticket_id="SG-A8",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/49",
        session_id="live-session-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A8", repo="foo/bar", pr_number=49)

    released = release_stale_gated_tasks()

    assert released == ["SG-A8"]
    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A8"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["session_id"] == "live-session-1"


def test_release_stale_gated_tasks_event_not_lost_when_save_dev_queue_raises(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR_MERGED event must not be lost if save_dev_queue raises mid-call
    (GitHub #1713 review MUST_FIX 1: cursor-advances-before-persist bug).

    Pre-fix, ``advance_cursor`` ran unconditionally inside the Variant A
    loop, before the batched ``save_dev_queue`` that actually persists the
    release. A crash (or a raising ``save_dev_queue`` -- disk full, lock
    I/O) in that window permanently consumed the ``PR_MERGED`` event (fires
    once per PR, first-observation dedup) with no corresponding mutation
    ever written, leaving the task stuck BLOCKED_ON_USER forever. This test
    simulates that failure and confirms a retry, once ``save_dev_queue``
    works again, still sees (and releases) the task -- proving the cursor
    was not advanced on the failing call.
    """
    task = _make_ticket_task(
        ticket_id="SG-A7",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/48",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A7", repo="foo/bar", pr_number=48)

    real_save_dev_queue = save_dev_queue

    def _raise_once(store: DevQueueStore) -> None:
        msg = "simulated save_dev_queue failure"
        raise OSError(msg)

    monkeypatch.setattr("cw.reconcile.tasks.save_dev_queue", _raise_once)

    with pytest.raises(OSError, match="simulated save_dev_queue failure"):
        release_stale_gated_tasks()

    # The mutation never persisted -- task is unchanged on disk.
    still_blocked = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A7")
    assert still_blocked.status == QueueItemStatus.BLOCKED_ON_USER
    assert still_blocked.stale_gate_detected_at is None

    # save_dev_queue works normally now. If the cursor had been advanced on
    # the failing call, this second pass would silently drop the event and
    # return []. It must instead release the task, proving the event was
    # not lost.
    monkeypatch.setattr("cw.reconcile.tasks.save_dev_queue", real_save_dev_queue)
    released = release_stale_gated_tasks()

    assert released == ["SG-A7"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A7")
    assert reloaded.stale_gate_detected_at is not None
