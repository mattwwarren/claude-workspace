"""Tests for ``cw.dispatch.tracker_mcp_gate`` — pre-dispatch tracker-MCP gate (#2442).

Mirrors ``tests/test_dispatch_branch_freshness.py``'s fixture style: real ``git``
repos built in ``tmp_path`` via the shared ``make_git_repo`` fixture, with the
ticket branch's ``.claude/settings.json`` seeded through
``tests.conftest.commit_tracked_file``. The module under test runs a real
``git show <branch>:<settings_path>`` against the client's git dir, so there is
no subprocess seam worth stubbing.

Every unresolvable state asserts the fail-open contract (no hit — the spawn
proceeds): only an existing, parseable settings file whose ``enabledPlugins``
structurally names the configured plugin as absent or ``false`` is a gate hit.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.dispatch.tracker_mcp_gate as gate_module
from cw.backend_mcp_config import McpConfigReading
from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
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
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_CLIENT = "acme"
_TICKET = "T-2442"
_BRANCH = f"dev/{_TICKET}"
_PLUGIN = "linear@acme"
_SETTINGS = ".claude/settings.json"


@pytest.fixture(autouse=True)
def _reset_warn_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh warn-once dedupe map (process-lifetime state)."""
    monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._WARNED_FAIL_OPEN", {})


def _client(
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
        name=_CLIENT,
        workspace_path=repo,
        tracker_mcp_gate=gate,
        pipeline=make_stage_pipeline(executors or {}),
    )


def _enabled_gate(
    plugin_id: str | None = _PLUGIN, *, servers: dict[str, str] | None = None
) -> TrackerMcpGateConfig:
    return TrackerMcpGateConfig(enabled=True, plugin_id=plugin_id, servers=servers)


def _snapshot(*, stage: Stage = Stage.PLAN, ticket_id: str = _TICKET) -> DevQueueStore:
    return DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id=ticket_id,
                client=_CLIENT,
                status=QueueItemStatus.PENDING,
                stage=stage,
            )
        ]
    )


def _branch_with_file(
    make_git_repo: Callable[..., Path], relpath: str, content: str
) -> Path:
    """Repo whose ``dev/T-2442`` branch commits *relpath*; ``main`` checked out."""
    repo = make_git_repo("tmg-repo")
    git_in(repo, "checkout", "-b", _BRANCH)
    commit_tracked_file(repo, relpath, content)
    git_in(repo, "checkout", "main")
    return repo


def _branch_with_settings(make_git_repo: Callable[..., Path], settings: object) -> Path:
    return _branch_with_file(make_git_repo, _SETTINGS, json.dumps(settings))


def _resolve(client: ClientConfig, snapshot: DevQueueStore) -> dict[str, object]:
    from cw.dispatch.tracker_mcp_gate import resolve_tracker_mcp_gate_hits

    return dict(resolve_tracker_mcp_gate_hits(client, snapshot))


class TestGateToggle:
    def test_gate_disabled_by_default_never_reads_settings_file(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6 case 4: ``tracker_mcp_gate=None`` never touches the branch."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        calls: list[str] = []

        def _spy(client: ClientConfig, branch: str, relpath: str) -> None:
            calls.append(branch)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._read_branch_json", _spy)
        client = ClientConfig(name=_CLIENT, workspace_path=repo)
        assert client.tracker_mcp_gate is None

        assert _resolve(client, _snapshot()) == {}
        assert calls == []

    def test_gate_explicitly_disabled_never_reads_settings_file(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        calls: list[str] = []

        def _spy(client: ClientConfig, branch: str, relpath: str) -> None:
            calls.append(branch)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._read_branch_json", _spy)
        client = _client(repo, TrackerMcpGateConfig(enabled=False, plugin_id=_PLUGIN))

        assert _resolve(client, _snapshot()) == {}
        assert calls == []


class TestPluginEnabled:
    def test_enabled_plugin_present_and_true_dict_shape_is_not_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """R6 case 1: the plugin is enabled on the branch -> spawn proceeds."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {_PLUGIN: True}})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

    def test_enabled_plugin_present_in_list_shape_is_not_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": [_PLUGIN]})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}


class TestFailOpen:
    def test_enabled_settings_file_missing_on_existing_branch_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """R6 case 2: an existing branch without the settings file fails open."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "missing" in warnings[0].getMessage()
        assert _BRANCH in warnings[0].getMessage()

    def test_fail_open_warning_is_logged_once_across_resolutions(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warn-once: the same fail-open reason is not re-logged every tick."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")
        client = _client(repo, _enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, _snapshot())
            _resolve(client, _snapshot())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_enabled_branch_does_not_exist_yet_fails_open(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A first-time PLAN dispatch has no branch yet: nothing to gate on."""
        repo = make_git_repo("tmg-nobranch")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        # The normal first-dispatch case is not warning-worthy.
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_enabled_malformed_json_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_file(make_git_repo, _SETTINGS, "{not json")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "malformed" in warnings[0].getMessage()

    def test_enabled_settings_top_level_not_an_object_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_settings(make_git_repo, [_PLUGIN])

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_enabled_plugins_field_missing_entirely_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Adopted assumption 5: no ``enabledPlugins`` key at all fails open."""
        repo = _branch_with_settings(make_git_repo, {"permissions": {"allow": []}})

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "enabledPlugins" in warnings[0].getMessage()

    def test_enabled_plugins_field_wrong_shape_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": _PLUGIN})

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "enabledPlugins" in warnings[0].getMessage()

    def test_enabled_plugin_non_bool_value_fails_open(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """A present key with a non-boolean value is ambiguous: fail open."""
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {_PLUGIN: "yes"}}
        )

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}


class TestGateHit:
    def _assert_hit(self, hits: dict[str, object], plugin_id: str = _PLUGIN) -> None:
        from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit

        assert hits == {
            _TICKET: TrackerMcpGateHit(
                branch=_BRANCH, file_inspected=_SETTINGS, expected=plugin_id
            )
        }

    def test_enabled_plugin_key_absent_from_dict_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """R6 case 3: the hit names the branch, the file, and the plugin id."""
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"github@acme": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_enabled_plugin_explicitly_false_in_dict_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {_PLUGIN: False}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_enabled_plugin_absent_from_list_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": ["other@acme"]})

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_plugin_id_match_is_exact_no_at_suffix_stripping(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"linear": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_plugin_id_match_is_exact_in_the_other_direction(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {_PLUGIN: True}})

        self._assert_hit(
            _resolve(_client(repo, _enabled_gate("linear")), _snapshot()),
            plugin_id="linear",
        )

    def test_plugin_id_match_is_case_sensitive(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"Linear@Acme": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_custom_settings_path_is_the_file_inspected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit

        custom = ".claude/settings.team.json"
        repo = _branch_with_file(
            make_git_repo, custom, json.dumps({"enabledPlugins": {}})
        )
        gate = TrackerMcpGateConfig(
            enabled=True, plugin_id=_PLUGIN, settings_path=custom
        )

        assert _resolve(_client(repo, gate), _snapshot()) == {
            _TICKET: TrackerMcpGateHit(
                branch=_BRANCH, file_inspected=custom, expected=_PLUGIN
            )
        }

    def test_impl_stage_task_is_gated(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        self._assert_hit(
            _resolve(_client(repo, _enabled_gate()), _snapshot(stage=Stage.IMPL))
        )


class TestScoping:
    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.FINALIZE])
    def test_review_and_finalize_stage_tasks_never_scanned(
        self, make_git_repo: Callable[..., Path], stage: Stage
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot(stage=stage)) == {}

    def test_non_pending_and_other_client_rows_never_scanned(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.PLAN,
                ),
                TicketTask(
                    ticket_id=_TICKET,
                    client="other-client",
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                ),
            ]
        )

        assert _resolve(_client(repo, _enabled_gate()), snapshot) == {}

    def test_git_unavailable_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing git binary (``OSError``) resolves to "not gated"."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        def _boom(*_args: str, **_kwargs: object) -> None:
            msg = "git: not found"
            raise OSError(msg)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._run_git", _boom)

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

    def test_clean_verdict_rearms_the_warn_once_dedupe(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A branch that resolves cleanly forgets its prior fail-open warning, so
        a later regression to the same reason is logged again."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")
        client = _client(repo, _enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, _snapshot())
            git_in(repo, "checkout", _BRANCH)
            commit_tracked_file(
                repo, _SETTINGS, json.dumps({"enabledPlugins": {_PLUGIN: True}})
            )
            _resolve(client, _snapshot())
            git_in(repo, "rm", "-q", _SETTINGS)
            git_in(repo, "commit", "-q", "-m", "drop settings")
            git_in(repo, "checkout", "main")
            _resolve(client, _snapshot())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2


# --- RFC 0018 S3 (#3055): per-backend server gate ---------------------------

_SERVER = "linear"
_OPENCODE_SERVERS = {OPENCODE_BACKEND: _SERVER}
_ALL_SERVERS = {OPENCODE_BACKEND: _SERVER, CODEX_BACKEND: _SERVER}


def _on(backend: str) -> dict[Stage, str | None]:
    return {Stage.PLAN: backend, Stage.IMPL: backend, Stage.REVIEW: backend}


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Wrap the gate's ``read_backend_mcp`` and record each call (per-tick test)."""
    calls: list[tuple[str, str]] = []
    real = gate_module.read_backend_mcp

    def _counting(backend: str, server: str, root: Path) -> McpConfigReading:
        calls.append((backend, server))
        return real(backend, server, root)

    monkeypatch.setattr(gate_module, "read_backend_mcp", _counting)
    return calls


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


_TRACKERS = ("github-issues", "linear", None)
_GATE_STATES: tuple[McpStage, ...] = ("enabled", "absent", "disabled", "malformed")


def _gated(backend: str, stage: Stage) -> bool:
    """Whether a (backend, stage) row is checked: opencode PLAN/IMPL, codex PLAN."""
    if backend == CODEX_BACKEND:
        return stage is Stage.PLAN
    return stage in (Stage.PLAN, Stage.IMPL)


def _expect_hit(
    tracker: str | None, backend: str, state: McpStage, stage: Stage
) -> bool:
    return (
        tracker == "linear"
        and state in ("absent", "disabled")
        and _gated(backend, stage)
    )


@pytest.mark.usefixtures("isolated_backend_layers")
class TestBackendServerGate:
    @pytest.mark.parametrize("tracker", _TRACKERS)
    @pytest.mark.parametrize("backend", [OPENCODE_BACKEND, CODEX_BACKEND])
    @pytest.mark.parametrize("state", _GATE_STATES)
    @pytest.mark.parametrize("stage", [Stage.PLAN, Stage.IMPL, Stage.REVIEW])
    def test_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tracker: str | None,
        backend: str,
        state: McpStage,
        stage: Stage,
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, backend, state)
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        client = _client(
            tmp_path / "ws",
            _enabled_gate(None, servers=_ALL_SERVERS),
            executors=_on(backend),
            tracker=tracker,
        )
        reads = _count_reads(monkeypatch)

        hits = _resolve(client, _snapshot(stage=stage))

        if _expect_hit(tracker, backend, state, stage):
            assert hits == {
                _TICKET: TrackerMcpGateHit(
                    branch=_BRANCH,
                    file_inspected=str(path),
                    expected=_SERVER,
                    backend=backend,
                )
            }
        else:
            assert hits == {}
        if tracker != "linear" or not _gated(backend, stage):
            assert reads == []

    @pytest.mark.parametrize("servers", [_OPENCODE_SERVERS, {CODEX_BACKEND: "a.b"}])
    def test_codex_plan_with_unusable_server_is_a_hit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, servers: dict[str, str]
    ) -> None:
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        client = _client(
            tmp_path / "ws",
            _enabled_gate(None, servers=servers),
            executors=_on(CODEX_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)
        hits = _resolve(client, _snapshot(stage=Stage.PLAN))
        (hit,) = hits.values()
        assert hit.backend == CODEX_BACKEND
        assert hit.file_inspected == "clients.yaml tracker_mcp_gate.servers"
        assert hit.expected == servers.get(CODEX_BACKEND, "servers.codex (unset)")
        assert reads == []

    @pytest.mark.parametrize("tracker", ["github-issues", None])
    def test_codex_plan_without_server_off_a_tracker_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tracker: str | None
    ) -> None:
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        client = _client(
            tmp_path / "ws",
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(CODEX_BACKEND),
            tracker=tracker,
        )
        reads = _count_reads(monkeypatch)
        assert _resolve(client, _snapshot(stage=Stage.PLAN)) == {}
        assert reads == []

    def test_unset_tracker_warns_once_and_never_reads(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
        )
        reads = _count_reads(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, _snapshot()) == {}
            assert _resolve(client, _snapshot()) == {}

        assert reads == []
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "opencode:tracker" in warnings[0]
        assert "tracker unset" in warnings[0]

    def test_unknown_reading_warns_once_naming_the_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "missing")
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, _snapshot()) == {}
            assert _resolve(client, _snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert f"opencode:{path}" in warnings[0]
        assert "does not exist" in warnings[0]

    def test_probe_found_layer_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        (tmp_path / ".git").mkdir()
        (tmp_path / "opencode.json").write_text("{}", encoding="utf-8")
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, _snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "not parsed" in warnings[0]

    def test_enabled_reading_rearms_the_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, _snapshot())
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "enabled")
            _resolve(client, _snapshot())
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, _snapshot())

        assert len(_warnings(caplog)) == 2

    def test_hit_rearms_the_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, _snapshot())
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
            assert _resolve(client, _snapshot()) != {}
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, _snapshot())

        assert len(_warnings(caplog)) == 2

    def test_one_config_read_per_tick_per_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = _client(
            tmp_path,
            _enabled_gate(None, servers=_OPENCODE_SERVERS),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )
        snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=f"T-{n}",
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                )
                for n in range(3)
            ]
        )
        reads = _count_reads(monkeypatch)

        hits = _resolve(client, snapshot)

        assert sorted(hits) == ["T-0", "T-1", "T-2"]
        assert reads == [(OPENCODE_BACKEND, _SERVER)]

    def test_opencode_row_under_plugin_only_gate_is_not_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A12: the plugin path is claude-native only."""
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = _client(
            tmp_path,
            _enabled_gate(_PLUGIN),
            executors=_on(OPENCODE_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)

        assert _resolve(client, _snapshot()) == {}
        assert reads == []

    def test_local_backend_row_is_not_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(
            tmp_path,
            _enabled_gate(_PLUGIN, servers=_ALL_SERVERS),
            executors=_on("local"),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)

        assert _resolve(client, _snapshot()) == {}
        assert reads == []

    def test_claude_row_under_servers_only_gate_is_skipped(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        calls: list[str] = []

        def _spy(client: ClientConfig, branch: str, relpath: str) -> None:
            calls.append(branch)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._read_branch_json", _spy)
        client = _client(repo, _enabled_gate(None, servers=_OPENCODE_SERVERS))

        assert _resolve(client, _snapshot()) == {}
        assert calls == []

    def test_mixed_gate_evaluates_each_row_by_its_backend(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A plugin_id+servers gate: claude row by plugin, opencode row by server."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        git_in(repo, "checkout", "-b", "dev/T-OC")
        git_in(repo, "checkout", "main")
        config_path = stage_mcp_config(
            monkeypatch, tmp_path, OPENCODE_BACKEND, "disabled"
        )
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=repo,
            tracker_mcp_gate=_enabled_gate(_PLUGIN, servers=_OPENCODE_SERVERS),
            lanes=[
                {
                    "name": "oc",
                    "pipeline": make_stage_pipeline(_on(OPENCODE_BACKEND)),
                }
            ],
        )
        _write_tracker_config(repo, "linear")
        snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                ),
                TicketTask(
                    ticket_id="T-OC",
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.IMPL,
                    lane="oc",
                ),
            ]
        )

        hits = _resolve(client, snapshot)

        assert hits == {
            _TICKET: TrackerMcpGateHit(
                branch=_BRANCH,
                file_inspected=_SETTINGS,
                expected=_PLUGIN,
                backend=CLAUDE_NATIVE_BACKEND,
            ),
            "T-OC": TrackerMcpGateHit(
                branch="dev/T-OC",
                file_inspected=str(config_path),
                expected=_SERVER,
                backend=OPENCODE_BACKEND,
            ),
        }
