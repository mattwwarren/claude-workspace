"""Shared setup for the ``cw dev-queue show`` / ticket-view tests (#2691).

Holds only what ``test_dev_queue_next_commands.py``, ``test_ticket_view.py``
and ``test_cli_dev_queue_show.py`` share and ``tests/conftest.py`` lacks. No
``test_`` prefix, so pytest does not collect it (same convention as
``tests/_reconcile_helpers.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import concurrency_override_file
from cw.dev_queue import save_dev_queue
from cw.models import (
    DEFAULT_LANE,
    ConcurrencyOverrides,
    DevQueueStore,
    LaneConcurrencyOverride,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _make_daemon_session, _seed_sessions

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest

    from cw.models import Session, TicketTask


def seed_queue(tasks: Sequence[TicketTask], sessions: Sequence[Session] = ()) -> None:
    """Persist *tasks* as the whole dev queue and append *sessions* to state."""
    save_dev_queue(DevQueueStore(tasks=list(tasks)))
    if sessions:
        _seed_sessions(*sessions)


def bound_session_for(task: TicketTask, **session_overrides: object) -> Session:
    """A daemon ``Session`` whose ``id`` is *task*'s ``session_id``."""
    kwargs: dict[str, object] = {
        "id": task.session_id,
        "client": task.client,
        "name": f"{task.client}/auto-dev/{task.ticket_id}",
    }
    kwargs.update(session_overrides)
    return _make_daemon_session(**kwargs)


def write_lane_client(
    client: str,
    workspace: Path,
    *,
    lanes: Sequence[str] = (DEFAULT_LANE,),
    attempt_ceiling: int | bool | None = None,
    paused: bool = False,
) -> None:
    """Write a one-client ``clients.yaml`` whose lanes carry the given settings.

    ``attempt_ceiling`` and ``paused`` apply to every lane in *lanes*;
    ``attempt_ceiling=False`` is the lane's explicit "no ceiling".
    """
    lane_entries: list[dict[str, object]] = []
    for name in lanes:
        entry: dict[str, object] = {"name": name}
        if attempt_ceiling is not None:
            entry["attempt_ceiling"] = attempt_ceiling
        if paused:
            entry["paused"] = True
        lane_entries.append(entry)
    write_clients_yaml(
        ClientSpec(client, workspace, lanes=lane_entries), ensure_workspaces=True
    )


def patch_roster(
    monkeypatch: pytest.MonkeyPatch, live_ids: set[str] | None
) -> FakeNativeDaemonClient:
    """Point ``cw.ticket_view``'s roster read at a fake holding *live_ids*.

    ``None`` makes the roster unreadable (the fail-closed read returns None).
    """
    fake = FakeNativeDaemonClient()
    if live_ids is None:
        fake.roster_unreadable = True
    else:
        fake._live.update(live_ids)
    monkeypatch.setattr("cw.ticket_view.get_native_daemon_client", lambda: fake)
    return fake


def write_lane_override(
    client: str, lane: str, *, paused: bool | None, consecutive_spawn_errors: int
) -> None:
    """Write ``concurrency_overrides.json`` with one lane override."""
    overrides = ConcurrencyOverrides(
        lanes={
            f"{client}/{lane}": LaneConcurrencyOverride(
                paused=paused, consecutive_spawn_errors=consecutive_spawn_errors
            )
        }
    )
    path = concurrency_override_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(overrides.model_dump_json())
