"""The snapshot refresh never runs on the dispatch tick or in reconcile (RFC 0019 T2).

``refresh_snapshot`` and ``snapshot_for_stage`` are stage-entry operations: they
run inside a worker's stage (codex REVIEW per pass, codex IMPL, codex FINALIZE),
never on the dispatcher. A tracker call on the tick would hold up every client's
dispatch. Pinned two ways: a behavioural run of ``dispatch_tick`` and
``reconcile()`` with both entry points rigged to raise, and a static check that
nothing under ``src/cw/dispatch/`` or ``src/cw/reconcile/`` imports or names them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch import dispatch_tick
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import reconcile
from tests._clients_yaml import write_clients_yaml
from tests._doctor_helpers import make_stage_pipeline
from tests._ticket_view_helpers import events_of

if TYPE_CHECKING:
    from collections.abc import Iterator

_SRC = Path(__file__).resolve().parent.parent / "src" / "cw"
_GUARDED_DIRS = ("dispatch", "reconcile")
_REFRESH_MODULE = "cw.ticket_snapshot._refresh"
_REFRESH_NAMES = frozenset({"snapshot_for_stage", "refresh_snapshot"})


class _RefreshOnTickError(AssertionError):
    """Raised when a rigged refresh entry point runs on the tick."""


def _rig_refresh_to_raise(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _forbidden(name: str) -> object:
        def _raise(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            msg = f"{name} ran outside a stage"
            raise _RefreshOnTickError(msg)

        return _raise

    for name in _REFRESH_NAMES:
        monkeypatch.setattr(f"{_REFRESH_MODULE}.{name}", _forbidden(name))
        monkeypatch.setattr(f"cw.ticket_snapshot.{name}", _forbidden(name))
    # The binding module bound ``snapshot_for_stage`` at import; rig that too.
    monkeypatch.setattr(
        "cw.ticket_snapshot._binding.snapshot_for_stage",
        _forbidden("snapshot_for_stage"),
    )
    return calls


@pytest.fixture
def codex_review_client(sample_client_config: ClientConfig) -> ClientConfig:
    client = sample_client_config.model_copy(
        update={"pipeline": make_stage_pipeline({Stage.REVIEW: CODEX_BACKEND})}
    )
    write_clients_yaml(client)
    return client


@pytest.fixture
def queue(codex_review_client: ClientConfig) -> None:
    """A PENDING codex REVIEW row and a RUNNING row, on the same client."""
    add_ticket(
        TicketTask(
            ticket_id="GEN-REVIEW",
            client=codex_review_client.name,
            stage=Stage.REVIEW,
        )
    )
    add_ticket(
        TicketTask(
            ticket_id="GEN-RUNNING",
            client=codex_review_client.name,
            status=QueueItemStatus.RUNNING,
            session_id="sess-running",
        )
    )


def test_dispatch_tick_never_refreshes_a_snapshot(
    queue: None,
    codex_review_client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rig_refresh_to_raise(monkeypatch)
    config = OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={codex_review_client.name: 3},
    )

    dispatch_tick(config, native_daemon=FakeNativeDaemonClient())

    assert calls == []
    # The tick really ran over this client's queue (not a vacuous empty pass).
    (tick,) = events_of(OrchestratorEventType.DISPATCH_TICK)
    assert tick.payload["client"] == codex_review_client.name
    assert tick.payload["pending"] == 1


def test_reconcile_never_refreshes_a_snapshot(
    queue: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rig_refresh_to_raise(monkeypatch)

    report = reconcile()

    assert calls == []
    assert report is not None
    # reconcile really visited the RUNNING row (its session is not on the roster).
    rows = {t.ticket_id: t for t in load_dev_queue().tasks}
    assert rows["GEN-RUNNING"].status is not QueueItemStatus.RUNNING


def test_the_rigging_itself_raises_when_a_refresh_entry_point_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.ticket_snapshot import _refresh

    calls = _rig_refresh_to_raise(monkeypatch)
    with pytest.raises(_RefreshOnTickError):
        vars(_refresh)["snapshot_for_stage"]()
    assert calls == ["snapshot_for_stage"]


# --- static half ---


def _guarded_files() -> Iterator[Path]:
    for directory in _GUARDED_DIRS:
        yield from sorted((_SRC / directory).rglob("*.py"))


def _offences(source: str) -> list[str]:
    """Imports of the refresh module and any mention of its entry points."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name == _REFRESH_MODULE]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _REFRESH_MODULE:
                found.append(module)
            found += [
                f"{module}.{a.name}" for a in node.names if a.name in _REFRESH_NAMES
            ]
        elif isinstance(node, ast.Name) and node.id in _REFRESH_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _REFRESH_NAMES:
            found.append(node.attr)
    return found


def test_the_guarded_trees_exist_and_are_scanned() -> None:
    files = list(_guarded_files())
    assert any(path.parent.name == "dispatch" for path in files)
    assert any(path.parent.name == "reconcile" for path in files)
    assert len(files) > 20


def test_no_dispatch_or_reconcile_module_imports_or_names_the_refresh() -> None:
    offenders = {
        str(path.relative_to(_SRC)): hits
        for path in _guarded_files()
        if (hits := _offences(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "snippet",
    [
        "import cw.ticket_snapshot._refresh",
        "from cw.ticket_snapshot._refresh import refresh_snapshot",
        "from cw.ticket_snapshot import snapshot_for_stage",
        "x = snapshot_for_stage",
        "ticket_snapshot.refresh_snapshot(task)",
    ],
)
def test_scanner_flags_each_offending_form(snippet: str) -> None:
    assert _offences(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "from cw.ticket_snapshot import read_for_stage",
        "from cw.ticket_snapshot._store import read",
        "import cw.ticket_snapshot",
    ],
)
def test_scanner_passes_the_row_only_readers(snippet: str) -> None:
    assert _offences(snippet) == []
