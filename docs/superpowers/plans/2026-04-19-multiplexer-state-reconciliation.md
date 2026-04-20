# Multiplexer/State Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when `sessions.json` has drifted from live multiplexer reality (tmux server death, cmux surface closure) and reconcile phantom sessions — marking them COMPLETED+CRASHED and reverting their dev-queue tickets to PENDING — via both cheap passive checks on read paths and an explicit `cw doctor --reap` command.

**Architecture:** Extend the `MultiplexerAdapter` protocol with an additive `list_surfaces()` method that returns the set of live surface refs for tmux/cmux/fake backends. A new `cw.reconcile` module computes a drift report (phantom sessions whose `surface_ref` is not in `list_surfaces()`) and applies it under the state lock — flipping phantoms to `COMPLETED`/`CRASHED`, recording `SESSION_COMPLETED` events with `crashed: true`, and reverting their `RUNNING` `TicketTask`s to `PENDING`. Passive reconciliation runs in `_check_and_mark_dead_sessions` (already a stub in `cli.py`), covering `cw status` and `cw list`. Active reconciliation runs at the top of `dispatch_tick`, ahead of `consume_completed_sessions`. Explicit reconciliation lands as `cw doctor --reap`. `cw start`'s existing "existing active session" short-circuit inherits passive reconciliation by calling the same helper before deciding to resume.

**Tech Stack:** Python 3.12+, Pydantic, Click, pytest, uv. Follows existing cw patterns: Protocol-typed adapters, file-locked state access, Pydantic models, fcntl locks, `FakeCmuxAdapter` for test injection.

---

## File Structure

**Files to create:**
- `src/cw/reconcile.py` — reconciliation logic (`ReconcileReport`, `compute_drift`, `reconcile`)
- `tests/test_reconcile.py` — unit tests for the reconcile module

**Files to modify:**
- `src/cw/cmux.py` — add `list_surfaces` to the `MultiplexerAdapter` Protocol; implement on `RealCmuxAdapter` (via `workspace.list` + `surface.list`) and `FakeCmuxAdapter` (in-memory set).
- `src/cw/tmux.py` — implement `list_surfaces` via `tmux list-panes -a -F <_PANE_FORMAT>`.
- `src/cw/cli.py` — flesh out `_check_and_mark_dead_sessions` (currently a stub at line ~350) to call `reconcile`; add `--reap` flag to `cw doctor`; use backend-aware surface-launching message.
- `src/cw/doctor.py` — add `run_doctor(reap: bool = False)` option that also runs reconciliation and reports reaped sessions.
- `src/cw/dispatch.py` — call `reconcile` at the top of each `dispatch_tick` (before `consume_completed_sessions`).
- `src/cw/session.py` — (1) backend-aware "Launching X surfaces" message at line ~221; (2) `start_session` passes through the reconciled state when checking for `existing` session.
- `tests/test_cmux.py` — `FakeCmuxAdapter.list_surfaces` behaviour test.
- `tests/test_tmux.py` — `TmuxAdapter.list_surfaces` parsing test (mocked subprocess).
- `tests/test_cli.py` — `_display_status` / `cw status` now reports reaped sessions; `cw doctor --reap`.
- `tests/test_dispatch.py` — `dispatch_tick` reconciles before claiming.
- `tests/test_session.py` — backend-aware launching message.

**Responsibility boundaries:**
- `cw.reconcile` owns: drift computation, state mutation under lock, event recording, ticket-task revert. Pure logic; never calls the adapter directly except through a single `list_surfaces()` hop.
- `cw.cmux` / `cw.tmux` own: adapter implementation detail. They know *how* to list surfaces; they don't know *why*.
- `cw.cli` / `cw.doctor` / `cw.dispatch` own: *when* to invoke reconciliation. They are the three call sites (passive, explicit, active).

---

## Task 1: Extend MultiplexerAdapter protocol with `list_surfaces`

**Why first:** every subsequent task consumes this method. Get the protocol pinned down before anything else reads/mutates state based on it.

**Files:**
- Modify: `src/cw/cmux.py:50-67` (Protocol), `src/cw/cmux.py:148-178` (FakeCmuxAdapter)
- Test: `tests/test_cmux.py`

- [ ] **Step 1: Write the failing test for `FakeCmuxAdapter.list_surfaces`**

Append to `tests/test_cmux.py`:

```python
def test_fake_adapter_list_surfaces_tracks_spawn_and_close() -> None:
    """FakeCmuxAdapter tracks live surfaces via spawn/close."""
    adapter = FakeCmuxAdapter()

    assert adapter.list_surfaces() == set()

    ref1 = adapter.spawn("ws-1", "echo hi")
    ref2 = adapter.spawn("ws-1", "echo bye")
    assert adapter.list_surfaces() == {ref1, ref2}

    adapter.close(ref1)
    assert adapter.list_surfaces() == {ref2}


def test_fake_adapter_close_unknown_ref_is_noop() -> None:
    """Closing a surface we never spawned must not raise."""
    adapter = FakeCmuxAdapter()
    adapter.close("never-spawned")  # must not raise
    assert adapter.list_surfaces() == set()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_cmux.py::test_fake_adapter_list_surfaces_tracks_spawn_and_close tests/test_cmux.py::test_fake_adapter_close_unknown_ref_is_noop -v`
Expected: FAIL with `AttributeError: 'FakeCmuxAdapter' object has no attribute 'list_surfaces'`

- [ ] **Step 3: Extend Protocol and FakeCmuxAdapter**

Edit `src/cw/cmux.py`. In the `MultiplexerAdapter` Protocol (around line 50), add after `identify`:

```python
    def list_surfaces(self) -> set[str]:
        """Return the set of surface refs currently known to the backend.

        Used by reconciliation to detect phantom sessions: any surface_ref
        in cw state that is *not* in this set is assumed dead. Implementers
        should return an empty set if the multiplexer server is unreachable,
        so callers cannot accidentally interpret "server down" as "all
        surfaces still alive".
        """
        ...
```

In `FakeCmuxAdapter.__init__`, add a live-set after `self.calls`:

```python
        self._live: set[str] = set()
```

In `FakeCmuxAdapter.spawn`, after computing `ref`:

```python
        self._live.add(ref)
```

In `FakeCmuxAdapter.close`, replace with:

```python
    def close(self, surface_ref: str) -> None:
        """Record call and drop from live set (idempotent)."""
        self.calls["close"].append((surface_ref,))
        self._live.discard(surface_ref)
```

Append new method:

```python
    def list_surfaces(self) -> set[str]:
        """Return the current in-memory live-surface set (copy)."""
        self.calls.setdefault("list_surfaces", []).append(())
        return set(self._live)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_cmux.py -v`
Expected: PASS, no previously-passing tests regress.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_cmux.py -v`
Expected: zero violations, zero errors, all pass.

- [ ] **Step 6: Commit**

```bash
git add src/cw/cmux.py tests/test_cmux.py
git commit -m "feat(cmux): add list_surfaces to MultiplexerAdapter protocol"
```

---

## Task 2: Implement `list_surfaces` on `TmuxAdapter`

**Files:**
- Modify: `src/cw/tmux.py`
- Test: `tests/test_tmux.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tmux.py`:

```python
def test_tmux_list_surfaces_parses_pane_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_surfaces parses `tmux list-panes -a -F` output into a set."""
    import subprocess
    from cw.tmux import TmuxAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        assert cmd[:4] == ["tmux", "list-panes", "-a", "-F"]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="cw-client-a:0.0\ncw-client-a:0.1\ncw-client-b:1.0\n",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = TmuxAdapter()

    assert adapter.list_surfaces() == {
        "cw-client-a:0.0",
        "cw-client-a:0.1",
        "cw-client-b:1.0",
    }


def test_tmux_list_surfaces_returns_empty_on_server_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tmux server is not running, list-panes returns non-zero; we return empty."""
    import subprocess
    from cw.tmux import TmuxAdapter

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="no server running on /tmp/tmux-1000/default\n",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = TmuxAdapter()
    assert adapter.list_surfaces() == set()
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_tmux.py::test_tmux_list_surfaces_parses_pane_refs -v`
Expected: FAIL with `AttributeError: 'TmuxAdapter' object has no attribute 'list_surfaces'`

- [ ] **Step 3: Implement `list_surfaces` on TmuxAdapter**

Edit `src/cw/tmux.py`. After `identify` (~line 110), append:

```python
    def list_surfaces(self) -> set[str]:
        """Return the set of live tmux pane refs across all sessions.

        Empty set when the tmux server is not running — callers in
        reconciliation rely on this invariant to avoid false positives.
        """
        result = self._run(
            ["list-panes", "-a", "-F", _PANE_FORMAT],
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return set()
        return {line for line in result.stdout.strip().splitlines() if line}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_tmux.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_tmux.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/tmux.py tests/test_tmux.py
git commit -m "feat(tmux): implement list_surfaces via list-panes -a"
```

---

## Task 3: Implement `list_surfaces` on `RealCmuxAdapter`

**Why last among adapter tasks:** macOS-only, can't be integration-tested in CI. Unit-test the call-shape via a monkeypatched `_call`.

**Files:**
- Modify: `src/cw/cmux.py:75-145` (RealCmuxAdapter)
- Test: `tests/test_cmux.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cmux.py`:

```python
def test_real_cmux_list_surfaces_aggregates_across_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_surfaces calls workspace.list then surface.list for each workspace."""
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(self, method, params):  # noqa: ANN001, ANN202
        calls.append((method, dict(params)))
        if method == "workspace.list":
            return {"workspaces": [
                {"id": "ws-a", "title": "client-a"},
                {"id": "ws-b", "title": "client-b"},
            ]}
        if method == "surface.list":
            ws_id = params["workspace_id"]
            if ws_id == "ws-a":
                return {"surfaces": [{"id": "surf-a1"}, {"id": "surf-a2"}]}
            return {"surfaces": [{"id": "surf-b1"}]}
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))

    assert adapter.list_surfaces() == {"surf-a1", "surf-a2", "surf-b1"}
    assert calls[0][0] == "workspace.list"
    assert {c[1]["workspace_id"] for c in calls if c[0] == "surface.list"} == {
        "ws-a", "ws-b",
    }


def test_real_cmux_list_surfaces_returns_empty_on_socket_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cmux socket is down, return empty set instead of raising."""
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter
    from cw.exceptions import CwError

    def fake_call(self, method, params):  # noqa: ANN001, ANN202
        raise CwError("connection refused")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))
    assert adapter.list_surfaces() == set()
```

Also make sure the test file already imports `Path`; if not, add `from pathlib import Path` and `import pytest` at the top (check existing imports first and only add what's missing).

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_cmux.py::test_real_cmux_list_surfaces_aggregates_across_workspaces -v`
Expected: FAIL with `AttributeError` on `list_surfaces`.

- [ ] **Step 3: Implement `list_surfaces` on RealCmuxAdapter**

Edit `src/cw/cmux.py`. After `identify` in `RealCmuxAdapter` (~line 145), append:

```python
    def list_surfaces(self) -> set[str]:
        """Return the set of live cmux surface IDs across all workspaces.

        Returns an empty set if the cmux socket is unreachable — the
        reconciler uses that as a "can't tell" signal and leaves state
        untouched (see :mod:`cw.reconcile`).
        """
        try:
            workspaces_raw = self._call("workspace.list", {})
        except CwError:
            return set()
        workspaces: list[dict[str, Any]] = workspaces_raw.get("workspaces", [])
        live: set[str] = set()
        for ws in workspaces:
            ws_id = ws.get("id")
            if not ws_id:
                continue
            try:
                resp = self._call("surface.list", {"workspace_id": ws_id})
            except CwError:
                continue
            for surf in resp.get("surfaces", []):
                surf_id = surf.get("id")
                if surf_id:
                    live.add(surf_id)
        return live
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_cmux.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_cmux.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/cmux.py tests/test_cmux.py
git commit -m "feat(cmux): implement list_surfaces on RealCmuxAdapter"
```

---

## Task 4: Create `cw.reconcile` module — pure drift computation

**Why split from apply:** drift computation is pure and easy to test. The write half (under lock, mutating state and dev-queue) is harder and lives in the next task. Keep them separable so tests can assert the "what changed" without the side effects.

**Files:**
- Create: `src/cw/reconcile.py`
- Create: `tests/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reconcile.py`:

```python
"""Unit tests for cw.reconcile."""

from __future__ import annotations

from datetime import UTC, datetime

from cw.cmux import FakeCmuxAdapter
from cw.models import (
    ClientConfig,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.reconcile import compute_drift


def _mk_session(
    sid: str,
    surface_ref: str | None,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    return Session(
        id=sid,
        name=f"client-a/{sid}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        status=status,
        workspace_path=ClientConfig(
            name="client-a", workspace_path="/tmp/ws"
        ).workspace_path,
        surface_ref=surface_ref,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def test_compute_drift_empty_state_returns_empty_report() -> None:
    adapter = FakeCmuxAdapter()
    state = CwState()
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == []


def test_compute_drift_flags_active_session_with_missing_surface() -> None:
    adapter = FakeCmuxAdapter()  # no surfaces
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == ["s1"]


def test_compute_drift_ignores_backgrounded_completed_and_refless() -> None:
    adapter = FakeCmuxAdapter()
    state = CwState(sessions=[
        _mk_session("s-bg", "ref1", status=SessionStatus.BACKGROUNDED),
        _mk_session("s-done", "ref2", status=SessionStatus.COMPLETED),
        _mk_session("s-noref", None, status=SessionStatus.ACTIVE),
    ])
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == []


def test_compute_drift_respects_live_set() -> None:
    adapter = FakeCmuxAdapter()
    live_ref = adapter.spawn("ws", "echo hi")  # registers in live set
    state = CwState(sessions=[
        _mk_session("alive", live_ref),
        _mk_session("dead", "gone"),
    ])
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == ["dead"]


def test_compute_drift_empty_live_set_from_adapter_is_reconciled(
    monkeypatch,
) -> None:
    """Adapters return empty on backend outage. That's 'no surfaces alive' —
    so everything ACTIVE/IDLE with a surface_ref is phantom. The reconciler
    trusts the adapter; callers who want "don't touch state when backend is
    down" must guard before calling.
    """
    adapter = FakeCmuxAdapter()
    state = CwState(sessions=[
        _mk_session("s1", "r1"),
        _mk_session("s2", "r2", status=SessionStatus.IDLE),
    ])
    report = compute_drift(state, adapter)
    assert set(report.phantom_session_ids) == {"s1", "s2"}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cw.reconcile'`

- [ ] **Step 3: Create the module**

Create `src/cw/reconcile.py`:

```python
"""Reconcile cw session state with live multiplexer surfaces.

The authoritative view of what is running lives in the multiplexer
(tmux/cmux/fake). :func:`compute_drift` compares ``sessions.json`` against
that view and returns a :class:`ReconcileReport` naming the sessions that
have gone phantom — active/idle rows whose ``surface_ref`` no longer maps
to any live surface. :func:`reconcile` (Task 5) applies the report under
the state lock.

The split is deliberate: ``compute_drift`` is pure and testable in
isolation; ``reconcile`` does the side-effecting work (state mutation,
event emission, dev-queue revert).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.models import SessionStatus

if TYPE_CHECKING:
    from cw.cmux import MultiplexerAdapter
    from cw.models import CwState


# Only these two statuses imply "the multiplexer should have a surface".
# BACKGROUNDED sessions intentionally have no pane (that's the whole point);
# COMPLETED is terminal. Both are ignored by reconciliation.
_LIVE_STATUSES: frozenset[SessionStatus] = frozenset({
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
})


@dataclass(frozen=True)
class ReconcileReport:
    """What reconciliation would do / did.

    ``phantom_session_ids`` — sessions whose ``surface_ref`` is not in the
    live set. Ordered by the original order in ``state.sessions``.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`, empty after
    :func:`compute_drift`.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)


def compute_drift(state: CwState, adapter: MultiplexerAdapter) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in ``adapter.list_surfaces()``.

    This function does not mutate state.
    """
    live = adapter.list_surfaces()
    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref not in live:
            phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_reconcile.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/reconcile.py tests/test_reconcile.py
git commit -m "feat(reconcile): add compute_drift pure drift computation"
```

---

## Task 5: Implement `reconcile` — apply drift under lock, revert tickets

**Files:**
- Modify: `src/cw/reconcile.py`
- Modify: `tests/test_reconcile.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reconcile.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from cw.config import save_state, load_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    CompletionReason,
    DevQueueStore,
    QueueItemStatus,
    SessionOrigin,
    TicketTask,
)
from cw.reconcile import reconcile


def test_reconcile_marks_phantom_completed_crashed(
    tmp_config_dir: Path,  # noqa: ARG001 -- autouse redirects state paths
) -> None:
    """reconcile flips phantom sessions to COMPLETED/CRASHED and persists."""
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    save_state(state)

    adapter = FakeCmuxAdapter()  # empty live set → s1 is phantom
    report = reconcile(adapter)

    assert report.phantom_session_ids == ["s1"]
    reloaded = load_state()
    s1 = reloaded.find_by_name_or_id("s1")
    assert s1 is not None
    assert s1.status == SessionStatus.COMPLETED
    assert s1.completed_reason == CompletionReason.CRASHED
    assert s1.completed_at is not None


def test_reconcile_reverts_daemon_session_ticket_to_pending(
    tmp_config_dir: Path,  # noqa: ARG001
) -> None:
    """When a DAEMON session for a ticket is phantom, revert its task."""
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-1"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    report = reconcile(FakeCmuxAdapter())

    assert "TKT-1" in report.reverted_ticket_ids
    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING


def test_reconcile_noop_when_no_phantoms(
    tmp_config_dir: Path,  # noqa: ARG001
) -> None:
    adapter = FakeCmuxAdapter()
    live_ref = adapter.spawn("ws", "echo")
    sess = _mk_session("alive", live_ref)
    save_state(CwState(sessions=[sess]))

    report = reconcile(adapter)
    assert report.phantom_session_ids == []
    assert report.reverted_ticket_ids == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL with `ImportError: cannot import name 'reconcile' from 'cw.reconcile'`.

- [ ] **Step 3: Implement `reconcile`**

Edit `src/cw/reconcile.py`. Extend imports at the top:

```python
from datetime import UTC, datetime

from cw.config import load_state, save_state
from cw.dev_queue import _lock as _dev_queue_lock
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
```

Append to the module:

```python
# Daemon session names follow "client/auto-dev/<ticket-id>" (see
# src/cw/dispatch.py::dispatch_tick where the label is constructed).
_AUTO_DEV_LABEL_PREFIX = "auto-dev/"


def _ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(_AUTO_DEV_LABEL_PREFIX):
        return tail[len(_AUTO_DEV_LABEL_PREFIX) :]
    return None


def reconcile(adapter: MultiplexerAdapter) -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.
    """
    state = load_state()
    drift = compute_drift(state, adapter)
    if not drift.phantom_session_ids:
        return drift

    phantom_set = set(drift.phantom_session_ids)
    now = datetime.now(UTC)
    reverted: list[str] = []

    ticket_ids_to_revert: list[str] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        if session.origin is SessionOrigin.DAEMON:
            ticket_id = _ticket_id_for_session(session.name)
            if ticket_id:
                ticket_ids_to_revert.append(ticket_id)
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "crashed": True,
            },
        )

    save_state(state)

    if ticket_ids_to_revert:
        with _dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id in ticket_ids_to_revert
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.PENDING
                    reverted.append(task.ticket_id)
            if reverted:
                save_dev_queue(store)

    return ReconcileReport(
        phantom_session_ids=drift.phantom_session_ids,
        reverted_ticket_ids=reverted,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_reconcile.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/reconcile.py tests/test_reconcile.py
git commit -m "feat(reconcile): apply drift and revert phantom tickets"
```

---

## Task 6: Wire passive reconciliation into `cw status` / `cw list`

**Context:** `cli.py:350-356` has `_check_and_mark_dead_sessions` as a stub with TODO. It's already called from `_display_status`. Replace the stub with a call into `reconcile`, display any reaped session names, and have `_display_sessions` call it too.

**Files:**
- Modify: `src/cw/cli.py` (stub function + `_display_sessions`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_display_status_reconciles_phantom_active_sessions(
    tmp_config_dir,  # noqa: ANN001, ARG001
    sample_client,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """`cw status` reports and reaps sessions with missing surfaces."""
    from click.testing import CliRunner
    from cw.cli import main
    from cw.config import load_state, save_state
    from cw.models import CwState, Session, SessionPurpose, SessionStatus
    from cw.cmux import FakeCmuxAdapter

    save_state(CwState(sessions=[
        Session(
            id="phantom1",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
            surface_ref="gone",
        ),
    ]))

    # Force cli.py to use a fresh FakeCmuxAdapter so list_surfaces is empty.
    monkeypatch.setattr("cw.cli.get_cmux_adapter", FakeCmuxAdapter)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Reaped phantom session" in result.output
    assert "client-a/impl" in result.output

    reloaded = load_state()
    reaped = reloaded.find_by_name_or_id("phantom1")
    assert reaped is not None
    assert reaped.status == SessionStatus.COMPLETED
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_cli.py::test_display_status_reconciles_phantom_active_sessions -v`
Expected: FAIL (currently the stub returns `[]`; message not emitted).

- [ ] **Step 3: Replace the stub**

Edit `src/cw/cli.py`. At the top, add to the existing `cw.reconcile` import (or create one):

```python
from cw.reconcile import reconcile
```

Replace `_check_and_mark_dead_sessions` (~line 350-356) with:

```python
def _check_and_mark_dead_sessions(_state: CwState) -> list[str]:
    """Reconcile state with the live multiplexer and return reaped session names.

    Cheap passive reconciliation: called from every read path (``cw status``,
    ``cw list``, ``cw start``). The reconciler is idempotent and returns an
    empty list when nothing changed. When the adapter cannot reach its
    backend (empty live set from ``list_surfaces``), everything active with
    a surface_ref gets flagged — that is the intended behaviour: we treat
    "backend unreachable" as "nothing running".
    """
    try:
        adapter = get_cmux_adapter()
    except CwError:
        return []
    report = reconcile(adapter)
    if not report.phantom_session_ids:
        return []
    # load_state() again after reconcile so we can name the reaped sessions.
    reloaded = load_state()
    names: list[str] = []
    for sid in report.phantom_session_ids:
        sess = reloaded.find_by_name_or_id(sid)
        if sess is not None:
            names.append(sess.name)
    return names
```

Update `_display_status` (~line 359) to print the list of names instead of the old per-session line. Change:

```python
    dead = _check_and_mark_dead_sessions(state)
    for s in dead:
        click.echo(f"Detected crashed session: {s.name} (crashed)")
```

to:

```python
    dead = _check_and_mark_dead_sessions(state)
    for name in dead:
        click.echo(f"Reaped phantom session: {name}")
    if dead:
        # State mutated, reload so active/backgrounded lists reflect truth.
        state = load_state()
```

Add the same reconcile call at the top of `_display_sessions` (~line 323). Immediately after `state = load_state()`:

```python
    dead = _check_and_mark_dead_sessions(state)
    for name in dead:
        click.echo(f"Reaped phantom session: {name}")
    if dead:
        state = load_state()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS. Also re-run any existing tests for `_display_status` and verify they still pass — the old "Detected crashed session:" message is replaced; if any existing test asserted on that string, update it to "Reaped phantom session:".

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_cli.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/cli.py tests/test_cli.py
git commit -m "feat(cli): reconcile phantom sessions on status/list read"
```

---

## Task 7: Passive reconciliation guard in `cw start`

**Context:** `start_session` (`src/cw/session.py:194-204`) checks `state.find_session(...)` for an existing ACTIVE/BACKGROUNDED session. If that "active" record is phantom (the tmux pane died), cw skips spawning and wrongly says "Session already active". Reconcile first.

**Files:**
- Modify: `src/cw/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session.py`:

```python
def test_start_session_reaps_phantom_before_existing_check(
    tmp_config_dir,  # noqa: ANN001, ARG001
    sample_client,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """If the 'existing active' session is phantom, start_session reaps and spawns fresh."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import init_client, load_state, save_state
    from cw.models import CwState, Session, SessionPurpose, SessionStatus
    from cw.session import start_session

    init_client(
        sample_client.name,
        sample_client.workspace_path,
        default_branch="main",
    )

    save_state(CwState(sessions=[
        Session(
            id="phantom",
            name=f"{sample_client.name}/impl",
            client=sample_client.name,
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
            surface_ref="gone-ref",
        ),
    ]))

    adapter = FakeCmuxAdapter()  # empty live set
    start_session(sample_client.name, "impl", adapter=adapter)

    reloaded = load_state()
    # Phantom got reaped
    phantom = reloaded.find_by_name_or_id("phantom")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED
    # New sessions spawned (at least one spawn call)
    assert len(adapter.calls["spawn"]) >= 1
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_session.py::test_start_session_reaps_phantom_before_existing_check -v`
Expected: FAIL — `start_session` short-circuits on the phantom ACTIVE session and never spawns.

- [ ] **Step 3: Call reconcile in start_session**

Edit `src/cw/session.py`. Add to imports at the top:

```python
from cw.reconcile import reconcile
```

In `start_session`, immediately after `state = load_state()` (~line 173), insert:

```python
    # Reap phantom sessions so we don't short-circuit on a dead "active" row.
    reconcile(adapter)
    state = load_state()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_session.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/session.py tests/test_session.py
git commit -m "feat(session): reconcile phantoms before start's existing-session check"
```

---

## Task 8: Active reconciliation in `dispatch_tick`

**Context:** `dispatch_tick` counts `running_count` from sessions currently ACTIVE/IDLE. If phantoms inflate that count, new tickets never get claimed. Call reconcile at the top of the tick, before counting.

**Files:**
- Modify: `src/cw/dispatch.py`
- Test: `tests/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dispatch.py`:

```python
def test_dispatch_tick_reconciles_phantoms_before_counting(
    tmp_config_dir,  # noqa: ANN001, ARG001
    sample_client,  # noqa: ANN001
) -> None:
    """Phantom DAEMON sessions do not block new dispatch."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import init_client, save_state, load_state
    from cw.dev_queue import save_dev_queue
    from cw.dispatch import dispatch_tick
    from cw.models import (
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        QueueItemStatus,
        Session,
        SessionOrigin,
        SessionPurpose,
        SessionStatus,
        TicketTask,
    )

    init_client(
        sample_client.name,
        sample_client.workspace_path,
        default_branch="main",
    )

    # Cap = 1, one ACTIVE phantom DAEMON session for the same client,
    # and one PENDING ticket. Without reconciliation, running_count == 1
    # would equal the cap and dispatch would spawn nothing.
    save_state(CwState(sessions=[
        Session(
            id="phantom-daemon",
            name=f"{sample_client.name}/auto-dev/TKT-OLD",
            client=sample_client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
            surface_ref="dead",
        ),
    ]))
    save_dev_queue(DevQueueStore(tasks=[
        TicketTask(
            ticket_id="TKT-OLD",
            client=sample_client.name,
            status=QueueItemStatus.RUNNING,
        ),
        TicketTask(
            ticket_id="TKT-NEW",
            client=sample_client.name,
            status=QueueItemStatus.PENDING,
        ),
    ]))

    adapter = FakeCmuxAdapter()
    config = OrchestratorConfig(per_client_max_parallel={sample_client.name: 1})

    spawned = dispatch_tick(config, adapter=adapter)
    assert spawned == 1

    # TKT-OLD reverted-then-maybe-reclaimed; TKT-NEW was also pending.
    # Either order is acceptable, but at least one must now be RUNNING
    # and the phantom must be marked COMPLETED.
    reloaded = load_state()
    assert reloaded.find_by_name_or_id("phantom-daemon").status == SessionStatus.COMPLETED
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_dispatch.py::test_dispatch_tick_reconciles_phantoms_before_counting -v`
Expected: FAIL — `spawned == 0` because the phantom inflates `running_count`.

- [ ] **Step 3: Call reconcile in dispatch_tick**

Edit `src/cw/dispatch.py`. Add to imports:

```python
from cw.reconcile import reconcile
```

At the top of `dispatch_tick` (~line 83), after `resolved_adapter = adapter or get_cmux_adapter()`, add:

```python
    reconcile(resolved_adapter)
```

And at the top of `run_dispatch_loop` body (~line 221-223), ensure reconcile runs each tick (it will, via `dispatch_tick`). No change needed there.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_dispatch.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): reconcile phantoms at start of each dispatch_tick"
```

---

## Task 9: Explicit reconciliation via `cw doctor --reap`

**Files:**
- Modify: `src/cw/doctor.py`, `src/cw/cli.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_doctor.py`:

```python
def test_run_doctor_reap_flag_reconciles_and_reports(
    tmp_config_dir,  # noqa: ANN001, ARG001
    sample_client,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """run_doctor(reap=True) invokes reconcile and reports reaped sessions."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import save_state, load_state
    from cw.doctor import run_doctor
    from cw.models import CwState, Session, SessionPurpose, SessionStatus

    save_state(CwState(sessions=[
        Session(
            id="phantom",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client.workspace_path,
            surface_ref="gone",
        ),
    ]))
    monkeypatch.setattr("cw.doctor.get_cmux_adapter", FakeCmuxAdapter)

    report = run_doctor(reap=True)
    reap_checks = [c for c in report.checks if c.name == "reconciliation"]
    assert len(reap_checks) == 1
    assert reap_checks[0].ok is True
    assert "phantom" in reap_checks[0].detail or "client-a/impl" in reap_checks[0].detail

    reloaded = load_state()
    phantom = reloaded.find_by_name_or_id("phantom")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED


def test_cw_doctor_cli_reap_flag(
    tmp_config_dir,  # noqa: ANN001, ARG001
    monkeypatch,  # noqa: ANN001
) -> None:
    """The CLI `cw doctor --reap` forwards the flag."""
    from click.testing import CliRunner
    from cw.cli import main
    from cw.cmux import FakeCmuxAdapter

    monkeypatch.setattr("cw.doctor.get_cmux_adapter", FakeCmuxAdapter)
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--reap"])
    assert result.exit_code == 0
    assert "reconciliation" in result.output
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: FAIL — `run_doctor` does not accept `reap`; CLI has no `--reap`.

- [ ] **Step 3: Implement the flag**

Edit `src/cw/doctor.py`. Add imports:

```python
from cw.cmux import get_cmux_adapter
from cw.reconcile import reconcile
```

Change signature of `run_doctor`:

```python
def run_doctor(*, reap: bool = False) -> DoctorReport:
    """Run every preflight check and return a populated report.

    When *reap* is True, also run multiplexer/state reconciliation and
    append a ``reconciliation`` check summarising the number of reaped
    sessions and reverted tickets.
    """
    backend = _resolve_backend_name()
    report = DoctorReport(version=__version__, backend=backend)
    report.checks.append(CheckResult("resolved backend", ok=True, detail=backend.value))
    report.checks.append(_check_backend_binary(backend))
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    report.checks.append(_check_state_file())
    report.checks.append(_check_dev_queue())
    if reap:
        report.checks.append(_check_reconcile())
    return report


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        adapter = get_cmux_adapter()
    except Exception as exc:  # noqa: BLE001 -- backend may be unreachable
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"adapter unavailable: {exc}",
        )
    reconcile_report = reconcile(adapter)
    reaped = len(reconcile_report.phantom_session_ids)
    reverted = len(reconcile_report.reverted_ticket_ids)
    if reaped == 0 and reverted == 0:
        return CheckResult("reconciliation", ok=True, detail="no phantoms")
    return CheckResult(
        "reconciliation",
        ok=True,
        detail=(
            f"reaped {reaped} session(s), reverted {reverted} ticket(s); "
            f"ids: {reconcile_report.phantom_session_ids}"
        ),
    )
```

Edit `src/cw/cli.py`. Update the `doctor` command:

```python
@main.command()
@click.option(
    "--reap",
    is_flag=True,
    help="Also reconcile state with the live multiplexer and reap phantoms.",
)
@handle_errors
def doctor(reap: bool) -> None:
    """Run environment preflight checks and print a health report.

    Reports the resolved backend, backend binary/daemon availability,
    config file locations and validity, and state file parseability.
    With ``--reap`` also reconciles cw's session state with the live
    multiplexer, marking phantom sessions COMPLETED and reverting their
    tickets to PENDING. Exits non-zero if any check fails.
    """
    report = run_doctor(reap=reap)
    click.echo(format_report(report))
    if not report.ok:
        raise click.exceptions.Exit(1)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/test_doctor.py tests/test_cli.py -v`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/cw/doctor.py src/cw/cli.py tests/test_doctor.py
git commit -m "feat(doctor): add --reap flag for explicit reconciliation"
```

---

## Task 10: Backend-aware surface-launching message

**Files:**
- Modify: `src/cw/session.py:221`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session.py`:

```python
def test_start_session_launch_message_names_backend(
    tmp_config_dir,  # noqa: ANN001, ARG001
    sample_client,  # noqa: ANN001
    capsys,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
) -> None:
    """The 'Launching X surfaces...' message names the backend in use."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import init_client
    from cw.models import BackendName
    from cw.session import start_session

    init_client(
        sample_client.name,
        sample_client.workspace_path,
        default_branch="main",
    )
    monkeypatch.setattr("cw.session._resolve_backend_name", lambda: BackendName.TMUX)

    start_session(sample_client.name, "impl", adapter=FakeCmuxAdapter())
    out = capsys.readouterr().out
    assert "Launching tmux surfaces" in out
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_session.py::test_start_session_launch_message_names_backend -v`
Expected: FAIL — message is hardcoded to "cmux".

- [ ] **Step 3: Make message backend-aware**

Edit `src/cw/session.py`. Add import at the top:

```python
from cw.cmux import _resolve_backend_name
```

Replace line ~221:

```python
    click.echo(f"Launching cmux surfaces for {client_name}...")
```

with:

```python
    backend = _resolve_backend_name()
    click.echo(f"Launching {backend.value} surfaces for {client_name}...")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS.

- [ ] **Step 5: Run full quality gate**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v`
Expected: all 541+ tests pass (existing) plus the new ones. Zero ruff/mypy issues.

- [ ] **Step 6: Commit**

```bash
git add src/cw/session.py tests/test_session.py
git commit -m "feat(session): use backend-aware surface-launching message"
```

---

## Task 11: End-to-end smoke test and CHANGELOG

**Context:** Final sanity pass. Run the whole suite, exercise the new `cw doctor --reap` path manually against a fake backend, and document.

**Files:**
- Modify: `CHANGELOG.md` (if present; otherwise skip)
- No new code — this task is all verification.

- [ ] **Step 1: Run the full quality gate on the whole tree**

Run: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v --cov=cw`
Expected: zero ruff violations, zero mypy errors, 100% pass rate. Coverage on `src/cw/reconcile.py` should be ≥95%.

- [ ] **Step 2: Smoke the CLI with the fake backend**

Run:

```bash
CW_BACKEND=fake uv run cw doctor --reap
```

Expected: output ends with `status: healthy`, a line `[OK] reconciliation — no phantoms` is present, exit code 0.

- [ ] **Step 3: Update CHANGELOG.md**

If `CHANGELOG.md` exists at the repo root, prepend a new section (check the existing format first and follow it exactly). Suggested content:

```markdown
## Unreleased

### Added
- Multiplexer/state reconciliation. Phantom sessions (tmux/cmux surfaces
  that no longer exist) are detected and reaped automatically on `cw status`,
  `cw list`, `cw start`, and at the top of each `dispatch_tick`. Explicit
  reconciliation is available via `cw doctor --reap`.
- `MultiplexerAdapter.list_surfaces()` on the adapter protocol; implemented
  for tmux, cmux (macOS), and fake backends.

### Changed
- `start_session`'s "Launching ... surfaces" message now names the active
  backend (tmux/cmux/fake).
- Dev-queue `TicketTask`s associated with reaped DAEMON sessions revert
  from RUNNING to PENDING so the dispatch loop retries them.
```

If no `CHANGELOG.md` exists, skip this step.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md  # only if step 3 made changes
git commit -m "docs: note multiplexer state reconciliation in changelog" \
    --allow-empty
```

Use `--allow-empty` only if step 3 was skipped so the pipeline still ends with a commit. Otherwise drop it.

---

## Self-Review Notes

**Spec coverage:**
- (1) Reconciliation primitive → Tasks 4-5 (`cw.reconcile` module with `compute_drift` + `reconcile`).
- (2a) On-read reconciliation → Task 6 (`_check_and_mark_dead_sessions` replaces stub; wired into `_display_status` and `_display_sessions`) and Task 7 (`start_session`).
- (2b) On-write reconciliation → Task 8 (`dispatch_tick`).
- (2c) Explicit command → Task 9 (`cw doctor --reap`).
- (3) State transition → phantoms flip to `COMPLETED` with `CompletionReason.CRASHED`; no new enum needed (CRASHED already exists).
- (4) Dev-queue coupling policy → **revert RUNNING TicketTask to PENDING** so the dispatch loop retries. Task 5 implements; Task 8 verifies end-to-end.
- (5) Backend-aware launch message → Task 10.

**Non-goals honoured:**
- No background daemon added.
- Protocol extension is strictly additive (one new method, no renames, no removals). `CmuxAdapter` alias retained.

**Placeholder scan:** no TBDs. Every code step ships concrete code.

**Type consistency check:**
- `ReconcileReport.phantom_session_ids: list[str]` (session IDs) — consumed by `cli._check_and_mark_dead_sessions` which translates IDs to names via `find_by_name_or_id`. Consistent.
- `reconcile(adapter)` signature is stable across Tasks 5, 6, 7, 8, 9.
- `list_surfaces() -> set[str]` signature matches between Protocol (Task 1), TmuxAdapter (Task 2), RealCmuxAdapter (Task 3), and FakeCmuxAdapter (Task 1).
- `CompletionReason.CRASHED` already exists in `models.py` — no enum changes needed.
