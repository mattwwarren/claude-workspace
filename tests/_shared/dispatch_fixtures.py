"""Dispatch, repair and inbox fixtures: state/events/workspace dirs, client
configs, repair workspaces and the isolated backend config layers. A
registered plugin. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    OrchestratorEventType,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime

    from cw.dispatch.tick import DispatchTickResult
    from tests._changelog_repair_helpers import FakeArmScript, FakeGhHead, LogEntry


@pytest.fixture
def repair_workspace(tmp_path: Path) -> Path:
    """The client workspace both conflict-repair recipes' tests run against."""
    path = tmp_path / "client"
    path.mkdir()
    return path


@pytest.fixture
def log() -> list[LogEntry]:
    """The shared call/event log the conflict-repair fakes append to."""
    return []


@pytest.fixture
def head(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> FakeGhHead:
    """The gh head read and disarm fake, installed for a conflict-repair test."""
    from tests._changelog_repair_helpers import FakeGhHead

    return FakeGhHead(log).install(monkeypatch)


@pytest.fixture
def arm(repair_workspace: Path) -> FakeArmScript:
    """The arm-script fake beside the conflict-repair client workspace."""
    from tests._changelog_repair_helpers import FakeArmScript

    return FakeArmScript(repair_workspace)


@pytest.fixture
def repair_events(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> None:
    """Record the recipe's events into ``log``.

    Not autouse: conftest autouse would apply to the whole suite. The two
    conflict-repair test modules opt in with
    ``pytestmark = pytest.mark.usefixtures("repair_events")``.
    """
    from tests._changelog_repair_helpers import track_events

    track_events(monkeypatch, log)


@pytest.fixture
def tmp_state_dir(tmp_config_dir: Path) -> Path:
    """Return the state directory within tmp_config_dir."""
    return tmp_config_dir / ".local" / "share" / "cw"


@pytest.fixture
def tmp_events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.EVENTS_DIR to tmp_path."""
    events_dir = tmp_path / ".local" / "share" / "cw" / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    return events_dir


def _spy_inbox_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the offset of every ``_read_lines_after_offset`` call."""
    import cw.events as events_mod

    reads: list[int] = []
    real_reader = events_mod._read_lines_after_offset

    def counting_reader(offset: int, anchor: bytes = b"") -> object:
        reads.append(offset)
        return real_reader(offset, anchor)

    monkeypatch.setattr(events_mod, "_read_lines_after_offset", counting_reader)
    return reads


def _spy_parse_lines(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the line count of every ``_parse_lines`` call."""
    import cw.events as events_mod

    parsed: list[int] = []
    real_parse = events_mod._parse_lines

    def counting_parse(lines: list[str]) -> object:
        parsed.append(len(lines))
        return real_parse(lines)

    monkeypatch.setattr(events_mod, "_parse_lines", counting_parse)
    return parsed


def _seed_inbox_with_cursor(old: int, newer: int) -> tuple[str, list[str]]:
    """Record *old* events, a cursor event, then *newer* events.

    Returns ``(cursor_id, newer_ids)``. The caller must have the events dir
    redirected (``tmp_events_dir``, or the autouse ``tmp_config_dir``).
    """
    from cw.events import record_event

    event_type = OrchestratorEventType.PR_REGISTERED
    for i in range(old):
        record_event(event_type, {"n": i})
    cursor = record_event(event_type, {"n": "cursor"})
    newer_ids = [record_event(event_type, {"n": old + i}).id for i in range(newer)]
    return cursor.id, newer_ids


# ---------------------------------------------------------------------------
# Dispatch fixtures (hoisted from tests/test_dispatch.py, #2503)
# ---------------------------------------------------------------------------


# Env vars that point codex or opencode at a config layer outside the
# per-test HOME (#3055). Cleared by ``isolated_backend_layers``.
_BACKEND_CONFIG_ENV_VARS = (
    "CODEX_HOME",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)


@pytest.fixture
def isolated_backend_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide every backend MCP config layer the per-test HOME does not own.

    Clears the codex/opencode config env vars and empties
    ``cw.backend_mcp_config._SYSTEM_LAYERS`` so a developer's real
    ``/etc/opencode`` or ``/etc/codex`` never reaches a test. HOME is already
    isolated by the autouse ``_isolate_home`` fixture (#3055).
    """
    import cw.backend_mcp_config

    for name in _BACKEND_CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cw.backend_mcp_config, "_SYSTEM_LAYERS", {})


@pytest.fixture
def tmp_dispatch_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; state isolation is handled by the autouse fixture."""
    return tmp_config_dir


@pytest.fixture
def workspace_dir(make_git_repo: Callable[[str], Path]) -> Path:
    """Return a real git repo to host the fake client.

    dispatch_tick now calls ``create_worktree`` on this dir, so it must
    be a real git repo with at least one commit.
    """
    return make_git_repo("workspace/test-project")


@pytest.fixture
def sample_client_config(workspace_dir: Path, tmp_path: Path) -> ClientConfig:
    """A ClientConfig for use with dispatch tests.

    Sets worktree_base to a tmp_path subdirectory so create_worktree
    writes test worktrees under tmp_path (not ~/.cw/wt/), preventing
    stale-directory accumulation across test runs.
    """
    return ClientConfig(
        name="test-client",
        workspace_path=workspace_dir,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        blocked_result_requeue_enabled=True,
    )


@pytest.fixture
def simple_config() -> OrchestratorConfig:
    """OrchestratorConfig with cap=1 for test-client."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 1},
    )


@pytest.fixture
def breaker_config() -> OrchestratorConfig:
    """OrchestratorConfig with a low lane circuit-breaker threshold for tests.

    ``simple_config`` plus ``lane_circuit_breaker_threshold=2``. Shared by
    ``tests/test_dispatch.py`` and ``tests/test_dispatch_lane_circuit.py``
    (#2696); a test needing other auto-resume knobs uses
    ``breaker_config.model_copy(update={...})``.
    """
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 1},
        lane_circuit_breaker_threshold=2,
        lane_circuit_auto_resume_minutes=15,
    )


@pytest.fixture
def review_monitor_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolate every path ``.claude/scripts/review_monitor.py`` writes (#2499).

    Points the script's central state dir, legacy state file, ship-it pending
    inbox and Desktop action queue under *tmp_path* (all are bound at import,
    so the autouse ``tmp_config_dir`` / ``_isolate_home`` never reach them),
    empties the canonical repo-path overrides, and clears the
    ``functools.lru_cache`` on ``_get_our_username`` at setup and teardown so
    no test sees another's faked GitHub login. Returns the central state dir.
    Logic: ``tests/_review_monitor_helpers.isolate_state``.
    """
    from tests import _review_monitor_helpers as helpers

    central = helpers.isolate_state(monkeypatch, tmp_path)
    yield central
    helpers.get("_get_our_username").cache_clear()


# ---------------------------------------------------------------------------
# Usage-limit window and clear helpers (#3547)
# ---------------------------------------------------------------------------


def advancing_tick(
    real_tick: Callable[..., DispatchTickResult],
    captured: list[dict[str, datetime]],
    *,
    after: Mapping[int, Callable[[], None]],
    stop_on_call: int = 3,
) -> Callable[..., DispatchTickResult]:
    """A ``dispatch_tick`` stand-in for multi-tick ``run_dispatch_loop`` tests.

    Each call records the ``usage_limited_until`` it was handed into
    *captured*, raises ``KeyboardInterrupt`` on call *stop_on_call*, and
    otherwise runs *real_tick* and then ``after[n]()`` (1-based) when present,
    so a hook lands inside the tick, before the loop's post-tick arm. The
    three older local copies in ``tests/test_dispatch.py`` stay as they are.
    """
    calls = 0

    def _tick(*args: object, **kwargs: object) -> DispatchTickResult:
        nonlocal calls
        calls += 1
        windows = kwargs.get("usage_limited_until")
        if not isinstance(windows, dict):
            msg = f"dispatch_tick was called without a window mapping: {windows!r}"
            raise TypeError(msg)
        captured.append(dict(windows))
        if calls == stop_on_call:
            raise KeyboardInterrupt
        result = real_tick(*args, **kwargs)
        hook = after.get(calls)
        if hook is not None:
            hook()
        return result

    return _tick


def seed_usage_limit_window(client: str, until: datetime) -> None:
    """Persist one client's usage-limit window through the production writer."""
    from cw.dispatch_state import merge_and_save_usage_limited_until

    merge_and_save_usage_limited_until({client: until})


def seed_stamped_row(
    client: str, ticket_id: str, *, next_eligible_at: datetime, **overrides: object
) -> None:
    """Enqueue one PENDING row carrying a ``next_eligible_at`` stamp."""
    from cw.dev_queue import add_ticket
    from tests._shared.session_seeding import _make_ticket_task

    task = _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        next_eligible_at=next_eligible_at,
        **overrides,
    )
    if not add_ticket(task):
        msg = f"seed_stamped_row: {client}/{ticket_id} was not inserted"
        raise AssertionError(msg)


def write_client_with_operator(
    workspace_path: Path,
    *,
    name: str = "test-client",
    login: str = "operator-login",
    worker_model: str | None = None,
) -> None:
    """Write one client whose ``operator_github_login`` is set, so no gh runs."""
    from tests._clients_yaml import ClientSpec, write_clients_yaml

    write_clients_yaml(
        ClientSpec(
            name,
            workspace_path,
            worker_model=worker_model,
            extra={"operator_github_login": login},
        ),
        ensure_workspaces=True,
    )
