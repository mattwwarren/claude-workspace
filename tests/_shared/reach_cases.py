"""Plain builders for the tick-side binding reachability tests (RFC 0019 H2b, #3744).

Functions, not fixtures (precedent ``tests/_shared/stage_preflight_builders.py``).
The one-off capture of the old tracker-MCP gate's parks and the parity replay
in ``tests/test_preflight_reachability.py`` build every case from
:func:`build_case`, so the two sides of the parity table cannot drift apart.

A case key is one of:

- ``server/<tracker>/<backend>/<state>/<stage>``: an opencode or codex row
  whose user-layer MCP config is staged in *state*;
- ``claude/<shape>/<stage>``: a claude-native row whose ticket branch carries
  the settings *shape*;
- ``unbound/<tracker>/<servers>/<stage>``: a codex row whose ``servers.codex``
  is unset or not a valid name;
- ``local/<stage>``: a ``local`` row under a gate naming everything.

``_gated`` and :func:`expect_server_hit` are hand-written tables, deliberately
not derived from ``STAGE_NEEDS``: they are the parity check's independent
oracle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.models import (
    CODEX_BACKEND,
    OPENCODE_BACKEND,
    DevQueueStore,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.client import ClientConfig, TrackerMcpGateConfig
from tests._backend_mcp_helpers import McpStage, stage_mcp_config
from tests._doctor_helpers import make_stage_pipeline
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.git_repos import branch_with_file, branch_with_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import pytest

#: Client name, ticket id, branch, plugin id and settings path of every case.
CLIENT: Final = "acme"
TICKET: Final = "T-2442"
BRANCH: Final = f"dev/{TICKET}"
PLUGIN: Final = "linear@acme"
SETTINGS: Final = ".claude/settings.json"
#: The MCP server name the backend captures carry.
SERVER: Final = "linear"

OPENCODE_SERVERS: Final = {OPENCODE_BACKEND: SERVER}
ALL_SERVERS: Final = {OPENCODE_BACKEND: SERVER, CODEX_BACKEND: SERVER}

#: ``None`` is an unset tracker (no ``project-config.yaml``).
TRACKERS: Final = ("github-issues", "linear", None)
GATE_STATES: Final[tuple[McpStage, ...]] = (
    "enabled",
    "absent",
    "disabled",
    "malformed",
)
STAGES: Final = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)

#: claude-native branch shapes: the committed content, or a sentinel.
_NO_BRANCH: Final = "<no-branch>"
_MISSING_FILE: Final = "<missing-file>"
_MALFORMED: Final = "<malformed>"
CLAUDE_SHAPES: Final[Mapping[str, object]] = {
    "dict_true": {"enabledPlugins": {PLUGIN: True}},
    "dict_false": {"enabledPlugins": {PLUGIN: False}},
    "dict_absent": {"enabledPlugins": {"github@acme": True}},
    "list_present": {"enabledPlugins": [PLUGIN]},
    "list_absent": {"enabledPlugins": ["other@acme"]},
    "non_bool": {"enabledPlugins": {PLUGIN: "yes"}},
    "wrong_shape": {"enabledPlugins": PLUGIN},
    "no_key": {"permissions": {"allow": []}},
    "top_list": [PLUGIN],
    "case_mismatch": {"enabledPlugins": {"Linear@Acme": True}},
    "at_suffix": {"enabledPlugins": {"linear": True}},
    "malformed": _MALFORMED,
    "missing_file": _MISSING_FILE,
    "no_branch": _NO_BRANCH,
}
#: ``unset`` names no codex server; ``invalid`` names one failing ``is_valid_name``.
UNBOUND_SERVERS: Final[Mapping[str, dict[str, str]]] = {
    "unset": OPENCODE_SERVERS,
    "invalid": {CODEX_BACKEND: "a.b"},
}


def tracker_label(tracker: str | None) -> str:
    return "unset" if tracker is None else tracker


def _tracker_of(label: str) -> str | None:
    return None if label == "unset" else label


def on(backend: str) -> dict[Stage, str | None]:
    """Every stage of the pipeline on *backend*."""
    return dict.fromkeys(STAGES, backend)


def enabled_gate(
    plugin_id: str | None = PLUGIN,
    *,
    servers: dict[str, str] | None = None,
    enabled: bool = True,
) -> TrackerMcpGateConfig:
    return TrackerMcpGateConfig(enabled=enabled, plugin_id=plugin_id, servers=servers)


def reach_client(
    repo: Path,
    gate: TrackerMcpGateConfig | None,
    *,
    executors: Mapping[Stage, str | None] | None = None,
    tracker: str | None = None,
) -> ClientConfig:
    """A gate client; *executors* sets stage backends, *tracker* the tracker."""
    if tracker is not None:
        _write_tracker_config(repo, tracker)
    return ClientConfig(
        name=CLIENT,
        workspace_path=repo,
        tracker_mcp_gate=gate,
        pipeline=make_stage_pipeline(executors or {}),
    )


def snapshot(*, stage: Stage = Stage.PLAN, ticket_id: str = TICKET) -> DevQueueStore:
    return DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id=ticket_id,
                client=CLIENT,
                status=QueueItemStatus.PENDING,
                stage=stage,
            )
        ]
    )


def gated(backend: str, stage: Stage) -> bool:
    """Whether a server row is checked: opencode PLAN/IMPL, codex PLAN/FINALIZE."""
    if backend == CODEX_BACKEND:
        return stage in (Stage.PLAN, Stage.FINALIZE)
    return stage in (Stage.PLAN, Stage.IMPL)


def expect_server_hit(
    tracker: str | None, backend: str, state: McpStage, stage: Stage
) -> bool:
    return (
        tracker == "linear"
        and state in ("absent", "disabled")
        and gated(backend, stage)
    )


def all_case_keys() -> list[str]:
    """Every case key of the parity table, sorted."""
    keys = [
        f"server/{tracker_label(t)}/{b}/{s}/{st.value}"
        for t in TRACKERS
        for b in (OPENCODE_BACKEND, CODEX_BACKEND)
        for s in GATE_STATES
        for st in STAGES
    ]
    keys += [f"claude/{shape}/{st.value}" for shape in CLAUDE_SHAPES for st in STAGES]
    keys += [
        f"unbound/{tracker_label(t)}/{servers}/{st.value}"
        for t in TRACKERS
        for servers in UNBOUND_SERVERS
        for st in STAGES
    ]
    keys += [f"local/{st.value}" for st in STAGES]
    return sorted(keys)


def _claude_repo(make_git_repo: Callable[..., Path], shape: str) -> Path:
    content = CLAUDE_SHAPES[shape]
    if content == _NO_BRANCH:
        return make_git_repo("reach-nobranch")
    if content == _MISSING_FILE:
        return branch_with_file(
            make_git_repo, "feature.py", "x = 1\n", branch=BRANCH, name="reach-repo"
        )
    if content == _MALFORMED:
        return branch_with_file(
            make_git_repo, SETTINGS, "{not json", branch=BRANCH, name="reach-repo"
        )
    return branch_with_settings(
        make_git_repo, content, branch=BRANCH, name="reach-repo"
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    # Its own .git stops the MCP reader's project-layer walk here (#3055).
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    return workspace


def build_case(
    key: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    enabled: bool = True,
) -> tuple[ClientConfig, DevQueueStore]:
    """Stage the environment for *key* and return its client and snapshot.

    *enabled* ``False`` builds the same case with the gate opted out (D-C3).
    """
    kind, *parts = key.split("/")
    stage = Stage(parts[-1])
    if kind == "server":
        tracker, backend = _tracker_of(parts[0]), parts[1]
        state = next(s for s in GATE_STATES if s == parts[2])
        stage_mcp_config(monkeypatch, tmp_path, backend, state)
        gate = enabled_gate(None, servers=dict(ALL_SERVERS), enabled=enabled)
        client = reach_client(
            _workspace(tmp_path), gate, executors=on(backend), tracker=tracker
        )
    elif kind == "claude":
        repo = _claude_repo(make_git_repo, parts[0])
        client = reach_client(repo, enabled_gate(enabled=enabled), tracker="linear")
    elif kind == "unbound":
        servers = dict(UNBOUND_SERVERS[parts[1]])
        gate = enabled_gate(None, servers=servers, enabled=enabled)
        client = reach_client(
            _workspace(tmp_path),
            gate,
            executors=on(CODEX_BACKEND),
            tracker=_tracker_of(parts[0]),
        )
    else:
        gate = enabled_gate(servers=dict(ALL_SERVERS), enabled=enabled)
        client = reach_client(
            _workspace(tmp_path), gate, executors=on("local"), tracker="linear"
        )
    return client, snapshot(stage=stage)
